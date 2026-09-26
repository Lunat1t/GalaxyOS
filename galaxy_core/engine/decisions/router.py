"""Galaxy 2.2 DAG Decision Router.

Uses System One Choice primitive to select the next optimal DAG execution branch
without invoking a heavy generative LLM cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Sequence

from .primitives import Choice, DecisionRequest
from .engine import DecisionEngine


@dataclass(frozen=True)
class BranchRoute:
    selected_node: str
    confidence: float
    distribution: dict[str, float]
    routing_latency_ms: float


class DAGDecisionRouter:
    """Fast, deterministic router for dynamic DAG branching."""

    def __init__(self, engine: DecisionEngine | None = None):
        self.engine = engine or DecisionEngine()

    def route_next_branch(
        self,
        current_state: str,
        candidate_nodes: Sequence[str],
        objective: str = "",
    ) -> BranchRoute:
        """Select next execution node from candidates."""
        if not candidate_nodes:
            raise ValueError("Candidate nodes list cannot be empty")
        if len(candidate_nodes) == 1:
            return BranchRoute(
                selected_node=candidate_nodes[0],
                confidence=1.0,
                distribution={candidate_nodes[0]: 1.0},
                routing_latency_ms=0.1,
            )

        start = time.monotonic()
        prompt = (
            f"Current Objective: {objective}\n"
            f"Execution State: {current_state}\n"
            f"Select the most appropriate next node to execute."
        )

        request = DecisionRequest(
            state=prompt,
            questions={
                "next_node": Choice(
                    instructions="Select the next work node to execute based on progress and dependencies",
                    options=list(candidate_nodes),
                )
            },
        )

        res = self.engine.decide(request)
        selected = str(res.answers.get("next_node", candidate_nodes[0]))
        dist = dict(res.distributions.get("next_node", {}))
        conf = float(res.confidences.get("next_node", 0.5))

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)

        return BranchRoute(
            selected_node=selected,
            confidence=conf,
            distribution=dist,
            routing_latency_ms=elapsed_ms,
        )
