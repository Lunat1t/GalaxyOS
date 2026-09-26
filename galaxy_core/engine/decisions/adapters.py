"""Adapters for executing System One Decision contracts.

Includes:
- BaseDecisionAdapter: Abstract adapter interface.
- LocalLLMDecisionAdapter: Offline deterministic & constrained LLM adapter.
- TypeSafeJevAdapter: Official cloud TypeSafe Jev API adapter.
"""
from __future__ import annotations

import abc
import json
import os
import re
import time
from typing import Any, Callable
import urllib.request
import urllib.error

from .primitives import (
    Choice,
    DecisionRequest,
    DecisionResponse,
    Noul,
    Score,
)


class BaseDecisionAdapter(abc.ABC):
    """Abstract contract for executing decision requests."""

    @abc.abstractmethod
    def decide(self, request: DecisionRequest) -> DecisionResponse:
        """Evaluate questions against state and return structured DecisionResponse."""
        pass


class TypeSafeJevAdapter(BaseDecisionAdapter):
    """Client for TypeSafe Jev API (POST https://api.typesafe.ai/v1/systemone)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.typesafe.ai/v1/systemone",
        timeout_seconds: float = 5.0,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        self.base_url = base_url
        self.timeout = timeout_seconds

    def is_available(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        if not self.is_available():
            raise RuntimeError("TypeSafe Jev API key is not configured")

        request.validate()
        payload = request.to_dict()
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            self.base_url,
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Galaxy-Moons-JevAdapter/2.2",
            },
            method="POST",
        )

        start = time.monotonic()
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if "127.0.0.1" in self.base_url or "localhost" in self.base_url
            else urllib.request.build_opener()
        )
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status_code = resp.status
                body = resp.read().decode("utf-8")
                raw = json.loads(body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"TypeSafe API error HTTP {e.code}: {err_body}") from e
        except Exception as e:
            raise RuntimeError(f"TypeSafe API connection failed: {e}") from e

        elapsed_ms = (time.monotonic() - start) * 1000.0

        answers: dict[str, Any] = {}
        distributions: dict[str, Any] = {}
        confidences: dict[str, float] = {}

        for q_id, q_result in raw.get("answers", {}).items():
            answers[q_id] = q_result.get("value")
            distributions[q_id] = q_result.get("distribution")
            confidences[q_id] = float(q_result.get("confidence") or 0.8)

        return DecisionResponse(
            answers=answers,
            distributions=distributions,
            confidences=confidences,
            raw_response=raw,
            latency_ms=round(elapsed_ms, 2),
            provider="typesafe-jev",
        )


class LocalLLMDecisionAdapter(BaseDecisionAdapter):
    """Deterministic local System One adapter.

    Operates either via:
    1. A custom local LLM function (`llm_callable`) with constrained schema.
    2. Calibrated heuristic and pattern classifier for SecOps, risk, and routing.
    """

    # High-risk bash / system patterns
    DESTRUCTIVE_COMMAND_PATTERNS = [
        re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\b|--recursive)", re.I),
        re.compile(r"\b(drop|truncate|delete\s+from)\b", re.I),
        re.compile(r"\bgit\s+(push\s+--force|reset\s+--hard|clean\s+-fd)", re.I),
        re.compile(r"\b(mkfs|dd\s+if=|fdisk|chmod\s+-R\s+777)\b", re.I),
        re.compile(r">\s*/dev/sd[a-z]", re.I),
        re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.I),  # fork bomb
    ]

    SAFE_READONLY_PATTERNS = [
        re.compile(r"(?:^|[\s:;`'\"|&])(ls|cat|head|tail|git\s+status|git\s+diff|git\s+log|pwd|grep|find)\b", re.I),
        re.compile(r"(?:^|[\s:;`'\"|&])(sqlite3\s+.*\.db\s+['\"].*select)\b", re.I),
        re.compile(r"(?:^|[\s:;`'\"|&])(python3?\s+-m\s+unittest|pytest|node\s+.*test)\b", re.I),
    ]

    SEMANTIC_SYNONYMS: dict[str, list[str]] = {
        "verify": ["test", "verification", "check", "assert", "qa", "moon"],
        "moon": ["test", "verification", "qa", "verify"],
        "code": ["implement", "develop", "code"],
        "write": ["write", "create", "author", "compose"],
        "deploy": ["deploy", "ship", "release"],
        "audit": ["audit", "security", "scan", "mars"],
        "plan": ["plan", "design", "sun", "interview"],
    }

    def __init__(self, llm_callable: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None):
        self.llm_callable = llm_callable

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        request.validate()
        start = time.monotonic()

        state_str = str(request.state).lower()
        answers: dict[str, Any] = {}
        distributions: dict[str, Any] = {}
        confidences: dict[str, float] = {}

        for q_id, question in request.questions.items():
            if isinstance(question, Noul):
                val, prob = self._evaluate_noul(state_str, question)
                answers[q_id] = val
                distributions[q_id] = {"true": prob, "false": round(1.0 - prob, 4)}
                confidences[q_id] = round(max(prob, 1.0 - prob), 4)

            elif isinstance(question, Choice):
                selected, dist, conf = self._evaluate_choice(state_str, question)
                answers[q_id] = selected
                distributions[q_id] = dist
                confidences[q_id] = conf

            elif isinstance(question, Score):
                exp, dist, conf = self._evaluate_score(state_str, question)
                answers[q_id] = exp
                distributions[q_id] = dist
                confidences[q_id] = conf

        elapsed_ms = (time.monotonic() - start) * 1000.0

        return DecisionResponse(
            answers=answers,
            distributions=distributions,
            confidences=confidences,
            raw_response={"status": "ok", "count": len(request.questions)},
            latency_ms=round(elapsed_ms, 2),
            provider="local-system-one",
        )

    def _evaluate_noul(self, state: str, question: Noul) -> tuple[bool, float]:
        inst = question.instructions.lower()

        # Is destructive / dangerous
        if any(w in inst for w in ("destructive", "danger", "harmful", "delete", "destroy", "risk")):
            is_destructive = any(p.search(state) for p in self.DESTRUCTIVE_COMMAND_PATTERNS)
            if is_destructive:
                return True, 0.98
            is_safe = any(p.search(state) for p in self.SAFE_READONLY_PATTERNS)
            if is_safe:
                return False, 0.05
            # Default mild certainty
            return False, 0.20

        # Is safe / readonly
        if any(w in inst for w in ("safe", "readonly", "read-only", "permit", "allow")):
            is_destructive = any(p.search(state) for p in self.DESTRUCTIVE_COMMAND_PATTERNS)
            if is_destructive:
                return False, 0.02
            is_safe = any(p.search(state) for p in self.SAFE_READONLY_PATTERNS)
            if is_safe:
                return True, 0.96
            return True, 0.70

        # General text matching
        words = [w for w in re.findall(r"\w+", inst) if len(w) > 3]
        matches = sum(1 for w in words if w in state)
        prob = min(0.95, max(0.05, matches / max(1, len(words))))
        return (prob >= 0.5), round(prob, 4)

    def _evaluate_choice(self, state: str, question: Choice) -> tuple[str, dict[str, float], float]:
        inst = question.instructions.lower()
        options = question.options

        # Special case: SecOps policy actions
        norm_opts = [o.lower() for o in options]
        if set(norm_opts) == {"allow", "awaiting_approval", "block"}:
            is_destructive = any(p.search(state) for p in self.DESTRUCTIVE_COMMAND_PATTERNS)
            if is_destructive:
                # Destructive commands require approval or block
                dist = {"allow": 0.01, "awaiting_approval": 0.94, "block": 0.05}
                return "awaiting_approval", dist, 0.94

            is_safe = any(p.search(state) for p in self.SAFE_READONLY_PATTERNS)
            if is_safe:
                dist = {"allow": 0.95, "awaiting_approval": 0.04, "block": 0.01}
                return "allow", dist, 0.95

            # Ambiguous action -> awaiting approval
            dist = {"allow": 0.20, "awaiting_approval": 0.75, "block": 0.05}
            return "awaiting_approval", dist, 0.75

        # DAG Node routing
        scores: dict[str, float] = {}
        for opt in options:
            opt_clean = opt.lower()
            weight = 1.0
            if opt_clean in state:
                weight += 5.0
            for word in opt_clean.replace("-", "_").split("_"):
                if not word:
                    continue
                # direct match
                if re.search(r"\b" + re.escape(word), state):
                    weight += 3.0
                # synonym match
                for syn in self.SEMANTIC_SYNONYMS.get(word, []):
                    if re.search(r"\b" + re.escape(syn), state):
                        weight += 2.5
            scores[opt] = weight

        total = sum(scores.values())
        dist = {opt: round(s / total, 4) for opt, s in scores.items()}
        best_opt = max(dist.items(), key=lambda x: x[1])[0]
        return best_opt, dist, dist[best_opt]

    def _evaluate_score(self, state: str, question: Score) -> tuple[float, dict[int, float], float]:
        inst = question.instructions.lower()
        levels = question.levels

        is_destructive = any(p.search(state) for p in self.DESTRUCTIVE_COMMAND_PATTERNS)
        is_safe = any(p.search(state) for p in self.SAFE_READONLY_PATTERNS)

        dist: dict[int, float] = {}
        if "risk" in inst or "danger" in inst or "severity" in inst:
            if is_destructive:
                # heavily weighted on highest levels
                for i in range(1, levels + 1):
                    dist[i] = 0.02
                dist[levels] = 0.85
                if levels > 1:
                    dist[levels - 1] = 0.15 - (0.02 * (levels - 2))
            elif is_safe:
                # heavily weighted on level 1
                dist[1] = 0.92
                for i in range(2, levels + 1):
                    dist[i] = round(0.08 / (levels - 1), 4)
            else:
                # moderate risk
                mid = (levels + 1) // 2
                dist = {i: round(1.0 / levels, 4) for i in range(1, levels + 1)}
                dist[mid] += 0.2
        else:
            # Uniform with slight peak
            dist = {i: round(1.0 / levels, 4) for i in range(1, levels + 1)}

        # Normalize
        total = sum(dist.values())
        dist = {k: round(v / total, 4) for k, v in dist.items()}
        exp = round(sum(k * v for k, v in dist.items()), 3)
        conf = max(dist.values())
        return exp, dist, conf
