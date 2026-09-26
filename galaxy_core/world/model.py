from __future__ import annotations

import json
from pathlib import Path
from collections import deque
from typing import Any

from .models import WorldSnapshot
from .scanner import ProjectScanner
from .drift import WorldDriftReport, compare_snapshots

class ProjectWorldModel:
    """Persistent, queryable representation of a software repository."""
    def __init__(self, galaxy_root: str | Path, project_root: str | Path | None = None, project: str = "default"):
        self.galaxy_root = Path(galaxy_root).resolve()
        self.project_root = Path(project_root or galaxy_root).resolve()
        self.project = project
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project) or "default"
        self.path = self.galaxy_root / "data" / "runtime" / "world" / f"{safe}.json"
        self.drift_path = self.galaxy_root / "data" / "runtime" / "world" / f"{safe}.drift.json"

    def sync(self) -> WorldSnapshot:
        snapshot = ProjectScanner(self.project_root, self.project).scan()
        self._persist(snapshot)
        return snapshot

    def _persist(self, snapshot: WorldSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def drift(self) -> WorldDriftReport:
        before = self.load(sync_if_missing=False) if self.path.exists() else None
        observed = ProjectScanner(self.project_root, self.project).scan()
        return compare_snapshots(before, observed)

    def sync_with_drift(self) -> tuple[WorldSnapshot, WorldDriftReport]:
        before = self.load(sync_if_missing=False) if self.path.exists() else None
        observed = ProjectScanner(self.project_root, self.project).scan()
        report = compare_snapshots(before, observed)
        self._persist(observed)
        self.drift_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return observed, report

    def future(self, scenario: str, *, seeds: list[str] | None = None, depth: int = 3, max_nodes: int = 50):
        from .future import FutureGraph
        return FutureGraph(self.load()).simulate(scenario, seeds=seeds, depth=depth, max_nodes=max_nodes)

    def load(self, *, sync_if_missing: bool = True) -> WorldSnapshot:
        if not self.path.exists():
            if not sync_if_missing:
                raise FileNotFoundError(self.path)
            return self.sync()
        return WorldSnapshot.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def status(self) -> dict[str, Any]:
        snap = self.load()
        out = {"project": snap.project, "root": snap.root, "generated_at": snap.generated_at, **snap.stats, "snapshot": str(self.path)}
        if self.drift_path.exists():
            try:
                last = json.loads(self.drift_path.read_text(encoding="utf-8"))
                out["last_drift"] = {"drift_score": last.get("drift_score", 0.0), "has_drift": last.get("has_drift", False), "summary": last.get("summary", {})}
            except (OSError, json.JSONDecodeError):
                pass
        return out

    def related(self, seed: str, *, depth: int = 2, limit: int = 30) -> list[dict[str, Any]]:
        snap = self.load()
        by_id = {n.id: n for n in snap.nodes}
        if seed not in by_id:
            # fuzzy path/symbol fallback
            q = seed.lower()
            matches = [n.id for n in snap.nodes if q in n.path.lower() or any(q in s.lower() for s in n.symbols)]
            if not matches:
                return []
            seed = matches[0]
        adj: dict[str, list[tuple[str, str]]] = {}
        for e in snap.edges:
            adj.setdefault(e.source, []).append((e.target, e.relation))
            adj.setdefault(e.target, []).append((e.source, "reverse_" + e.relation))
        out = []
        seen = {seed}
        queue = deque([(seed, 0, "seed")])
        while queue and len(out) < limit:
            node_id, d, rel = queue.popleft()
            node = by_id.get(node_id)
            if node:
                out.append({**node.to_dict(), "distance": d, "via": rel})
            if d >= depth:
                continue
            for nxt, relation in adj.get(node_id, []):
                if nxt not in seen:
                    seen.add(nxt); queue.append((nxt, d + 1, relation))
        return out
