"""Galaxy 2.2 System One Decision Primitives (inspired by Jev / TypeSafe contract).

Contracts:
- Noul: answers yes/no question with probability in [0.0, 1.0].
- Choice: selects one item from a finite set of options with full probability distribution.
- Score: assigns probabilities across discrete levels (2..10) and calculates expected value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Sequence


@dataclass
class Noul:
    """Boolean decision primitive returning probability of True."""
    instructions: str
    value: bool | None = None
    probability: float | None = None

    def validate(self) -> None:
        if not self.instructions.strip():
            raise ValueError("Noul instructions cannot be empty")
        if self.probability is not None and not (0.0 <= self.probability <= 1.0):
            raise ValueError(f"Noul probability must be in [0, 1], got {self.probability}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "noul",
            "instructions": self.instructions,
            "value": self.value,
            "probability": self.probability,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Noul":
        prob = data.get("probability")
        val = data.get("value")
        if val is None and prob is not None:
            val = prob >= 0.5
        inst = data.get("instructions") or ""
        return cls(instructions=inst, value=val, probability=prob)


@dataclass
class Choice:
    """Categorical selection primitive across predefined discrete options."""
    instructions: str
    options: list[str]
    selected: str | None = None
    distribution: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0

    def validate(self) -> None:
        if not self.instructions.strip():
            raise ValueError("Choice instructions cannot be empty")
        if not self.options:
            raise ValueError("Choice must have at least one option")
        if len(set(self.options)) != len(self.options):
            raise ValueError(f"Choice options must be unique: {self.options}")
        if self.distribution:
            for opt, p in self.distribution.items():
                if opt not in self.options:
                    raise ValueError(f"Distribution key {opt!r} not in options {self.options}")
                if not (0.0 <= p <= 1.0):
                    raise ValueError(f"Probability for {opt} must be in [0, 1], got {p}")
            total = sum(self.distribution.values())
            if not math.isclose(total, 1.0, abs_tol=0.01) and total > 0.0:
                # normalize if close
                scale = 1.0 / total
                self.distribution = {k: v * scale for k, v in self.distribution.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "choice",
            "instructions": self.instructions,
            "options": list(self.options),
            "selected": self.selected,
            "distribution": dict(self.distribution),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Choice":
        options = list(data.get("options") or data.get("criteria") or [])
        dist = dict(data.get("distribution") or {})
        selected = data.get("selected") or data.get("value")
        conf = float(data.get("confidence") or 0.0)
        if not selected and dist:
            selected = max(dist.items(), key=lambda item: item[1])[0]
        if not conf and dist and selected in dist:
            conf = dist[selected]
        inst = data.get("instructions")
        if isinstance(inst, dict):
            inst = inst.get("goal") or inst.get("rules") or str(inst)
        return cls(
            instructions=str(inst or ""),
            options=options,
            selected=selected,
            distribution=dist,
            confidence=conf,
        )


@dataclass
class Score:
    """Ordinal rating primitive on a scale of 2..10 levels with expected value calculation."""
    instructions: str
    levels: int = 5
    expected_score: float | None = None
    distribution: dict[int, float] = field(default_factory=dict)
    confidence: float = 0.0

    def validate(self) -> None:
        if not self.instructions.strip():
            raise ValueError("Score instructions cannot be empty")
        if not (2 <= self.levels <= 10):
            raise ValueError(f"Score levels must be between 2 and 10, got {self.levels}")
        if self.distribution:
            for lvl, p in self.distribution.items():
                if not (1 <= lvl <= self.levels):
                    raise ValueError(f"Level {lvl} out of range [1, {self.levels}]")
                if not (0.0 <= p <= 1.0):
                    raise ValueError(f"Probability for level {lvl} must be in [0, 1], got {p}")

    def compute_expected_value(self) -> float:
        if not self.distribution:
            return float(self.expected_score or 0.0)
        exp = sum(lvl * p for lvl, p in self.distribution.items())
        self.expected_score = round(exp, 3)
        return self.expected_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "score",
            "instructions": self.instructions,
            "levels": self.levels,
            "expected_score": self.expected_score,
            "distribution": {str(k): v for k, v in self.distribution.items()},
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Score":
        levels = int(data.get("levels") or 5)
        raw_dist = data.get("distribution") or {}
        dist = {int(k): float(v) for k, v in raw_dist.items()}
        exp = data.get("expected_score")
        if exp is not None:
            exp = float(exp)
        inst = data.get("instructions") or ""
        score = cls(
            instructions=str(inst),
            levels=levels,
            expected_score=exp,
            distribution=dist,
            confidence=float(data.get("confidence") or 0.0),
        )
        if exp is None and dist:
            score.compute_expected_value()
        return score


@dataclass
class DecisionRequest:
    """Request payload containing state context and multiple questions (speculative fan-out)."""
    state: str | dict[str, Any]
    questions: dict[str, Noul | Choice | Score]
    model: str = "jev-latest"

    def validate(self) -> None:
        if not self.questions:
            raise ValueError("DecisionRequest must contain at least one question")
        for q_id, q in self.questions.items():
            if not q_id.strip():
                raise ValueError("Question identifier cannot be empty")
            q.validate()

    def to_dict(self) -> dict[str, Any]:
        q_dict = {}
        for q_id, q in self.questions.items():
            q_dict[q_id] = q.to_dict()
        return {
            "state": self.state if isinstance(self.state, str) else str(self.state),
            "model": self.model,
            "questions": q_dict,
        }


@dataclass
class DecisionResponse:
    """Structured response containing evaluated answers and full distributions."""
    answers: dict[str, Any]
    distributions: dict[str, Any]
    confidences: dict[str, float]
    raw_response: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    provider: str = "local"
