"""Repository drift detection for the Galaxy Project World Model.

The detector compares two structural snapshots. It is intentionally deterministic:
it reports evidence, but does not decide whether a semantic architectural change is good or bad.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import WorldSnapshot


@dataclass(frozen=True)
class FileChange:
    path: str
    change: str  # added | removed | modified
    before_hash: str | None = None
    after_hash: str | None = None
    symbols_added: tuple[str, ...] = ()
    symbols_removed: tuple[str, ...] = ()
    component_before: str | None = None
    component_after: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorldDriftReport:
    project: str
    baseline_generated_at: str | None
    observed_generated_at: str
    changes: list[FileChange] = field(default_factory=list)
    edges_added: list[dict[str, Any]] = field(default_factory=list)
    edges_removed: list[dict[str, Any]] = field(default_factory=list)
    components_touched: list[str] = field(default_factory=list)
    drift_score: float = 0.0
    has_drift: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "baseline_generated_at": self.baseline_generated_at,
            "observed_generated_at": self.observed_generated_at,
            "changes": [x.to_dict() for x in self.changes],
            "edges_added": self.edges_added,
            "edges_removed": self.edges_removed,
            "components_touched": list(self.components_touched),
            "drift_score": self.drift_score,
            "has_drift": self.has_drift,
            "summary": {
                "files_added": sum(1 for x in self.changes if x.change == "added"),
                "files_removed": sum(1 for x in self.changes if x.change == "removed"),
                "files_modified": sum(1 for x in self.changes if x.change == "modified"),
                "edges_added": len(self.edges_added),
                "edges_removed": len(self.edges_removed),
            },
        }


def compare_snapshots(before: WorldSnapshot | None, after: WorldSnapshot) -> WorldDriftReport:
    if before is None:
        # First observation is a baseline, not a drift event.
        return WorldDriftReport(
            project=after.project,
            baseline_generated_at=None,
            observed_generated_at=after.generated_at,
            drift_score=0.0,
            has_drift=False,
        )

    left = {n.path: n for n in before.nodes}
    right = {n.path: n for n in after.nodes}
    changes: list[FileChange] = []
    touched: set[str] = set()

    for path in sorted(right.keys() - left.keys()):
        node = right[path]
        touched.add(node.component)
        changes.append(FileChange(path, "added", after_hash=node.content_hash, component_after=node.component,
                                  symbols_added=tuple(node.symbols)))
    for path in sorted(left.keys() - right.keys()):
        node = left[path]
        touched.add(node.component)
        changes.append(FileChange(path, "removed", before_hash=node.content_hash, component_before=node.component,
                                  symbols_removed=tuple(node.symbols)))
    for path in sorted(left.keys() & right.keys()):
        a, b = left[path], right[path]
        if a.content_hash == b.content_hash and a.symbols == b.symbols and a.component == b.component:
            continue
        touched.update([a.component, b.component])
        changes.append(FileChange(
            path, "modified", before_hash=a.content_hash, after_hash=b.content_hash,
            symbols_added=tuple(sorted(set(b.symbols) - set(a.symbols))),
            symbols_removed=tuple(sorted(set(a.symbols) - set(b.symbols))),
            component_before=a.component, component_after=b.component,
        ))

    def edge_key(e):
        return (e.source, e.target, e.relation)

    old_edges = {edge_key(e): e for e in before.edges}
    new_edges = {edge_key(e): e for e in after.edges}
    edges_added = [new_edges[k].to_dict() for k in sorted(new_edges.keys() - old_edges.keys())]
    edges_removed = [old_edges[k].to_dict() for k in sorted(old_edges.keys() - new_edges.keys())]
    for e in edges_added + edges_removed:
        for p in (e["source"], e["target"]):
            node = right.get(p) or left.get(p)
            if node:
                touched.add(node.component)

    # Structural drift score is a bounded signal, not a quality rating.
    weight = (
        sum(1.0 if c.change == "modified" else 1.35 for c in changes)
        + 0.45 * (len(edges_added) + len(edges_removed))
        + 0.35 * sum(len(c.symbols_added) + len(c.symbols_removed) for c in changes)
    )
    denom = max(6.0, len(before.nodes) * 0.18 + len(before.edges) * 0.08)
    score = round(min(1.0, weight / denom), 4)
    return WorldDriftReport(
        project=after.project,
        baseline_generated_at=before.generated_at,
        observed_generated_at=after.generated_at,
        changes=changes,
        edges_added=edges_added,
        edges_removed=edges_removed,
        components_touched=sorted(x for x in touched if x),
        drift_score=score,
        has_drift=bool(changes or edges_added or edges_removed),
    )
