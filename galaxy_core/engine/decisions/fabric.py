"""Confidence-gated micro-decision fabric.

Jev/System-One is used only for narrow choices. Policy remains deterministic code.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any

from .engine import DecisionEngine

@dataclass(frozen=True)
class DecisionPolicy:
    auto_confidence: float = 0.90
    escalate_below: float = 0.90

@dataclass(frozen=True)
class FabricDecision:
    value: Any
    confidence: float
    action: str  # auto | escalate
    distribution: dict[str, float]
    reason: str
    provider: str = "decision-engine"

    def to_dict(self):
        return asdict(self)

class DecisionFabric:
    """Small decision layer with explicit confidence gates and no hidden authority."""
    TASK_TYPES = ["documentation", "frontend", "backend", "database", "testing", "architecture", "bugfix", "security", "research"]

    def __init__(self, engine: DecisionEngine | None = None, policy: DecisionPolicy | None = None):
        self.engine = engine or DecisionEngine()
        self.policy = policy or DecisionPolicy()

    def choose(self, state: str, question: str, options: list[str]) -> FabricDecision:
        value, distribution, confidence = self.engine.ask_choice(state, question, options)
        action = "auto" if confidence >= self.policy.auto_confidence else "escalate"
        reason = "high-confidence narrow decision" if action == "auto" else "confidence below policy threshold"
        return FabricDecision(value, confidence, action, distribution, reason)

    def classify_task(self, task: str) -> FabricDecision:
        return self.choose(task, "Classify this engineering task by its primary work type", self.TASK_TYPES)

    def select_worker(self, state: str, workers: list[str]) -> FabricDecision:
        return self.choose(state, "Select the best worker capability for this bounded task", workers)
