"""Replay World Model changes into the retrieval index and component maps."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from galaxy_core.attention import AttentionEngine
from galaxy_core.attention.index import StaleWorldSnapshot
from galaxy_core.context.capsules import CapsuleStore, scope_for
from galaxy_core.world import ProjectWorldModel


class KernelProjector:
    def __init__(self, galaxy_root: str | Path, project_root: str | Path | None = None,
                 project: str = "default"):
        self.galaxy_root = Path(galaxy_root).resolve()
        self.project_root = Path(project_root or galaxy_root).resolve()
        self.project = project
        self.world = ProjectWorldModel(self.galaxy_root, self.project_root, project)
        self.index = AttentionEngine(self.project_root, project, storage_root=self.galaxy_root).index
        self.capsules = CapsuleStore(self.galaxy_root, self.project_root, project)
        self.path = self.galaxy_root / "data" / "runtime" / "projectors" / f"{self.world.path.stem}.json"

    def _state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") == 1 and isinstance(raw.get("cursor"), int):
                return raw
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {"version": 1, "cursor": 0, "snapshot_id": ""}

    def observe(self) -> dict[str, Any]:
        """Coalesce outstanding events to the latest snapshot, then commit the cursor."""
        state = self._state()
        for attempt in range(2):
            snapshot = self.world.current()
            cursor = state["cursor"]
            latest = cursor
            latest_snapshot_id = state.get("snapshot_id", "")
            count = 0
            scopes: set[str] = set()
            baseline = False
            while True:
                page = self.world.events(after_id=latest, limit=500)
                if not page:
                    break
                for event in page:
                    count += 1
                    baseline |= event["kind"] == "WORLD_BASELINE"
                    for change in event.get("changes", []):
                        scopes.add(scope_for(change["path"]))
                    for edge in event.get("edges_added", []) + event.get("edges_removed", []):
                        scopes.add(scope_for(edge["source"]))
                        scopes.add(scope_for(edge["target"]))
                latest = page[-1]["id"]
                latest_snapshot_id = page[-1]["after_snapshot_id"]
                if len(page) < 500:
                    break
            repair = (not self.index.path.exists() or not self.capsules.path.exists()
                      or latest_snapshot_id != snapshot.snapshot_id)
            if not count and not repair:
                return {"events_consumed": 0, "cursor": cursor, "snapshot_id": snapshot.snapshot_id,
                        "index": {"updated": 0, "reused": 0, "removed": 0},
                        "capsules": {"hits": 0, "built": 0}}
            if baseline or repair:
                scopes = {scope_for(n.path) for n in snapshot.nodes}
            try:
                index_stats = self.index.prepare(snapshot.nodes)
                representatives = {}
                for node in snapshot.nodes:
                    scope = scope_for(node.path)
                    if scope in scopes:
                        representatives.setdefault(scope, node.path)
                _, capsule_stats = self.capsules.select(snapshot, [representatives[s]
                                                      for s in sorted(representatives)],
                                                        limit=len(representatives))
                if not self.world.is_current(snapshot):
                    raise StaleWorldSnapshot("repository changed during projection")
            except StaleWorldSnapshot:
                if attempt == 0:
                    continue
                raise
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"version": 1, "cursor": latest,
                                        "snapshot_id": snapshot.snapshot_id}), encoding="utf-8")
            temp.replace(self.path)
            return {"events_consumed": count, "cursor": latest, "snapshot_id": snapshot.snapshot_id,
                    "index": index_stats, "capsules": capsule_stats}
        raise StaleWorldSnapshot("repository changed repeatedly during projection")
