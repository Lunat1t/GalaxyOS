"""Galaxy 2.2 Unified DecisionEngine.

Coordinates local System One adapters and cloud Jev endpoints, provides
speculative fan-out, LRU caching, and fail-safe fallback.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import logging
import os
import time
from typing import Any

from .primitives import (
    Choice,
    DecisionRequest,
    DecisionResponse,
    Noul,
    Score,
)
from .adapters import (
    BaseDecisionAdapter,
    LocalLLMDecisionAdapter,
    TypeSafeJevAdapter,
)

logger = logging.getLogger("galaxy.decisions")


class DecisionEngine:
    """High-throughput, fail-safe decision orchestrator for Galaxy Context OS."""

    def __init__(
        self,
        cloud_adapter: BaseDecisionAdapter | None = None,
        local_adapter: BaseDecisionAdapter | None = None,
        prefer_cloud: bool = False,
        cache_size: int = 500,
    ):
        self.cloud_adapter = cloud_adapter or TypeSafeJevAdapter()
        self.local_adapter = local_adapter or LocalLLMDecisionAdapter()
        self.prefer_cloud = prefer_cloud
        self.cache_size = cache_size
        self._cache: OrderedDict[str, DecisionResponse] = OrderedDict()
        self.metrics: dict[str, int] = {
            "total_requests": 0,
            "cache_hits": 0,
            "cloud_calls": 0,
            "local_calls": 0,
            "fallbacks": 0,
        }

    def _cache_key(self, request: DecisionRequest) -> str:
        data = request.to_dict()
        serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def decide(self, request: DecisionRequest, use_cache: bool = True) -> DecisionResponse:
        """Execute decision request with speculative fan-out and fallback."""
        self.metrics["total_requests"] += 1

        key = self._cache_key(request)
        if use_cache and key in self._cache:
            self.metrics["cache_hits"] += 1
            # Move to end (LRU)
            self._cache.move_to_end(key)
            return self._cache[key]

        response: DecisionResponse | None = None

        # Try cloud if preferred and configured
        if self.prefer_cloud and isinstance(self.cloud_adapter, TypeSafeJevAdapter) and self.cloud_adapter.is_available():
            try:
                self.metrics["cloud_calls"] += 1
                response = self.cloud_adapter.decide(request)
            except Exception as e:
                logger.warning("Cloud decision adapter failed, falling back to local: %s", e)
                self.metrics["fallbacks"] += 1

        # Fallback to local
        if response is None:
            self.metrics["local_calls"] += 1
            response = self.local_adapter.decide(request)

        # Store in LRU cache
        if use_cache:
            if len(self._cache) >= self.cache_size:
                self._cache.popitem(last=False)
            self._cache[key] = response

        return response

    def ask_noul(self, state: str, instructions: str) -> tuple[bool, float]:
        """Convenience method for a single binary question."""
        req = DecisionRequest(
            state=state,
            questions={"q": Noul(instructions=instructions)},
        )
        res = self.decide(req)
        val = bool(res.answers.get("q"))
        prob = float(res.confidences.get("q", 0.5))
        return val, prob

    def ask_choice(self, state: str, instructions: str, options: list[str]) -> tuple[str, dict[str, float], float]:
        """Convenience method for a single choice question."""
        req = DecisionRequest(
            state=state,
            questions={"q": Choice(instructions=instructions, options=options)},
        )
        res = self.decide(req)
        val = str(res.answers.get("q", options[0]))
        dist = dict(res.distributions.get("q", {}))
        conf = float(res.confidences.get("q", 0.5))
        return val, dist, conf

    def ask_score(self, state: str, instructions: str, levels: int = 5) -> tuple[float, dict[int, float], float]:
        """Convenience method for a single ordinal score."""
        req = DecisionRequest(
            state=state,
            questions={"q": Score(instructions=instructions, levels=levels)},
        )
        res = self.decide(req)
        exp = float(res.answers.get("q", 1.0))
        dist = dict(res.distributions.get("q", {}))
        conf = float(res.confidences.get("q", 0.5))
        return exp, dist, conf
