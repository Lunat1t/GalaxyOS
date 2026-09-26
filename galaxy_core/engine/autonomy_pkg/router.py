"""Model routing logic for autonomous DAG execution."""

from __future__ import annotations

from typing import Any

from galaxy_core.engine.autonomy_pkg.models import (
    BudgetExceeded,
    ModelProfile,
    ModelRoute,
    WorkNode,
)


class AdaptiveModelRouter:
    """Capability-first routing with cost, latency and observed reliability."""

    def __init__(self, profiles: list[ModelProfile], metrics: dict[str, dict[str, float]] | None = None):
        if not profiles:
            raise ValueError("at least one model profile is required")
        for profile in profiles:
            profile.validate()
        self.profiles = profiles
        self.metrics = metrics or {}

    def select(self, node: WorkNode, remaining_cost: float) -> ModelRoute:
        candidates: list[ModelRoute] = []
        for profile in self.profiles:
            if not profile.enabled:
                continue
            if node.capability not in profile.capabilities and "general" not in profile.capabilities:
                continue
            cost = profile.estimated_cost(node)
            if cost > remaining_cost:
                continue
            stat = self.metrics.get(profile.name, {})
            reliability = float(stat.get("success_rate", .7))
            observed_latency = float(stat.get("latency_score", profile.latency_score))
            cost_penalty = min(1.0, cost / max(remaining_cost, .000001))
            score = .50 * profile.quality + .30 * reliability + .15 * observed_latency - .05 * cost_penalty
            candidates.append(ModelRoute(profile.name, profile.provider, profile.model, cost, score))
        if not candidates:
            raise BudgetExceeded(f"no enabled model can serve capability {node.capability!r} within budget")
        return max(candidates, key=lambda x: (x.score, -x.estimated_cost_usd, x.profile))
