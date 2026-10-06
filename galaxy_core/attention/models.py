from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AttentionCandidate:
    path: str
    kind: str
    language: str
    component: str
    symbols: list[str] = field(default_factory=list)
    lexical_score: float = 0.0
    bm25_score: float = 0.0
    semantic_score: float = 0.0
    graph_score: float = 0.0
    impact_score: float = 0.0
    score: float = 0.0
    gate: str = "consider"  # include | consider | drop
    gate_confidence: float = 0.5
    role: str = "supporting"
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    excerpt: str = ""
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AttentionBudget:
    profile: str
    requested_tokens: int
    effective_tokens: int
    file_tokens: int
    memory_tokens: int
    graph_tokens: int
    instruction_tokens: int
    reserve_tokens: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AttentionQuality:
    coverage_signal: float = 0.0
    noise_signal: float = 0.0
    graph_coverage_signal: float = 0.0
    token_utilization: float = 0.0
    selection_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AttentionResult:
    task: str
    project: str
    strategy: str
    budget: AttentionBudget
    candidates_considered: int
    selected: list[AttentionCandidate]
    dropped: int
    quality: AttentionQuality
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "project": self.project,
            "strategy": self.strategy,
            "budget": self.budget.to_dict(),
            "candidates_considered": self.candidates_considered,
            "selected": [x.to_dict() for x in self.selected],
            "dropped": self.dropped,
            "quality": self.quality.to_dict(),
            "trace": dict(self.trace),
        }
