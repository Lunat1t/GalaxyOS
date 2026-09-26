"""First counterfactual / Future Graph implementation.

This is a structural impact simulator. It predicts blast radius from the repository graph;
it does not claim runtime certainty. The output therefore carries confidence and evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections import deque
import re
from typing import Any

from .models import WorldSnapshot

WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)


@dataclass(frozen=True)
class ImpactNode:
    path: str
    distance: int
    impact: str  # changed | dependent | dependency_review
    via: str
    confidence: float
    kind: str
    component: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FutureScenario:
    scenario: str
    seeds: list[str]
    impacted: list[ImpactNode] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    affected_tests: list[str] = field(default_factory=list)
    affected_docs: list[str] = field(default_factory=list)
    structural_risk: float = 0.0
    uncertainty: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "seeds": self.seeds,
            "impacted": [x.to_dict() for x in self.impacted],
            "affected_components": self.affected_components,
            "affected_tests": self.affected_tests,
            "affected_docs": self.affected_docs,
            "structural_risk": self.structural_risk,
            "uncertainty": self.uncertainty,
            "summary": {
                "nodes": len(self.impacted),
                "components": len(self.affected_components),
                "tests": len(self.affected_tests),
                "docs": len(self.affected_docs),
            },
        }


class FutureGraph:
    def __init__(self, snapshot: WorldSnapshot):
        self.snapshot = snapshot
        self.nodes = {n.path: n for n in snapshot.nodes}

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {x.lower() for x in WORD.findall(text or "")}

    def resolve_seeds(self, scenario: str, explicit: list[str] | None = None, *, limit: int = 4) -> list[str]:
        found: list[str] = []
        for raw in explicit or []:
            if raw in self.nodes:
                found.append(raw)
                continue
            q = raw.lower()
            matches = [n.path for n in self.snapshot.nodes if q in n.path.lower() or any(q in s.lower() for s in n.symbols)]
            if matches:
                found.append(matches[0])
        if found:
            return list(dict.fromkeys(found))[:limit]

        q = self._terms(scenario)
        ranked: list[tuple[float, str]] = []
        for n in self.snapshot.nodes:
            p = self._terms(n.path.replace("/", " ").replace("_", " "))
            s = self._terms(" ".join(n.symbols))
            sm = self._terms(n.summary)
            score = 3.2 * len(q & p) + 2.4 * len(q & s) + 0.8 * len(q & sm)
            if score:
                ranked.append((score, n.path))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return [p for _, p in ranked[:limit]]

    def simulate(self, scenario: str, *, seeds: list[str] | None = None, depth: int = 3, max_nodes: int = 50) -> FutureScenario:
        seed_paths = self.resolve_seeds(scenario, seeds)
        if not seed_paths:
            return FutureScenario(
                scenario=scenario,
                seeds=[],
                uncertainty=["No repository nodes could be resolved from the scenario; provide --seed for a bounded simulation."],
            )

        dependents: dict[str, list[tuple[str, str, float]]] = {}
        dependencies: dict[str, list[tuple[str, str, float]]] = {}
        for e in self.snapshot.edges:
            dependencies.setdefault(e.source, []).append((e.target, e.relation, e.confidence))
            dependents.setdefault(e.target, []).append((e.source, e.relation, e.confidence))

        impacts: dict[str, ImpactNode] = {}
        queue = deque()
        for seed in seed_paths:
            n = self.nodes[seed]
            impacts[seed] = ImpactNode(seed, 0, "changed", "scenario seed", 1.0, n.kind, n.component)
            queue.append((seed, 0, 1.0))

        # Reverse edges represent code/files likely to be affected by a changed dependency.
        while queue and len(impacts) < max_nodes:
            current, d, conf = queue.popleft()
            if d >= depth:
                continue
            for nxt, relation, edge_conf in dependents.get(current, []):
                next_conf = conf * edge_conf * 0.82
                old = impacts.get(nxt)
                if old and old.distance <= d + 1 and old.confidence >= next_conf:
                    continue
                n = self.nodes.get(nxt)
                if not n:
                    continue
                impacts[nxt] = ImpactNode(nxt, d + 1, "dependent", f"reverse_{relation}:{current}", round(next_conf, 4), n.kind, n.component)
                queue.append((nxt, d + 1, next_conf))
                if len(impacts) >= max_nodes:
                    break

        # Direct dependencies are not predicted to break automatically, but deserve review.
        for seed in seed_paths:
            if len(impacts) >= max_nodes:
                break
            for dep, relation, edge_conf in dependencies.get(seed, []):
                if dep in impacts:
                    continue
                n = self.nodes.get(dep)
                if not n:
                    continue
                impacts[dep] = ImpactNode(dep, 1, "dependency_review", f"{relation}:{seed}", round(edge_conf * 0.55, 4), n.kind, n.component)

        ordered = sorted(impacts.values(), key=lambda x: (x.distance, -x.confidence, x.path))[:max_nodes]
        components = sorted({x.component for x in ordered if x.component})
        tests = sorted(x.path for x in ordered if x.kind == "test")
        docs = sorted(x.path for x in ordered if x.kind == "documentation")
        code_count = sum(1 for x in ordered if x.kind not in {"test", "documentation"})
        direct_dependents = sum(1 for x in ordered if x.impact == "dependent" and x.distance == 1)
        risk = min(1.0, 0.08 + 0.045 * code_count + 0.075 * direct_dependents + 0.055 * max(0, len(components) - 1))
        uncertainty = [
            "Future Graph alpha is a structural counterfactual: dynamic/runtime dependencies may be missing."
        ]
        if not tests:
            risk = min(1.0, risk + 0.12)
            uncertainty.append("No structurally connected tests were found for the predicted blast radius.")
        return FutureScenario(
            scenario=scenario,
            seeds=seed_paths,
            impacted=ordered,
            affected_components=components,
            affected_tests=tests,
            affected_docs=docs,
            structural_risk=round(risk, 4),
            uncertainty=uncertainty,
        )
