from __future__ import annotations

import json
import hashlib
import uuid
from pathlib import Path
from collections import deque
from typing import Any

from .models import WorldSnapshot
from .scanner import ProjectScanner
from .drift import WorldDriftReport, compare_snapshots
from .events import WorldEventLog

class ProjectWorldModel:
    """Persistent, queryable representation of a software repository."""
    def __init__(self, galaxy_root: str | Path, project_root: str | Path | None = None, project: str = "default"):
        self.galaxy_root = Path(galaxy_root).resolve()
        self.project_root = Path(project_root or galaxy_root).resolve()
        self.project = project
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project) or "default"
        identity = hashlib.sha256(f"{self.project_root}\0{project}".encode()).hexdigest()[:16]
        stem = f"{safe}-{identity}"
        self.path = self.galaxy_root / "data" / "runtime" / "world" / f"{stem}.json"
        self.drift_path = self.galaxy_root / "data" / "runtime" / "world" / f"{stem}.drift.json"
        self.manifest_path = self.galaxy_root / "data" / "runtime" / "world" / f"{stem}.manifest.json"
        self.pending_path = self.galaxy_root / "data" / "runtime" / "world" / f"{stem}.pending.json"
        self.event_log = WorldEventLog(self.galaxy_root / "data" / "runtime" / "world" / f"{stem}.events.sqlite3")
        self.last_sync: dict[str, Any] = {}

    def sync(self, *, full: bool = False) -> WorldSnapshot:
        self._recover_pending()
        before = self.load(sync_if_missing=False) if self.path.exists() else None
        snapshot, stamps, mode, changed = self._observe(full=full)
        report = compare_snapshots(before, snapshot)
        self._persist(snapshot, stamps, mode, changed, self._event(before, snapshot, report))
        return snapshot

    def _event(self, before: WorldSnapshot | None, after: WorldSnapshot,
               report: WorldDriftReport) -> dict[str, Any] | None:
        if before is not None and before.snapshot_id == after.snapshot_id:
            return None
        changes = ([{"path": n.path, "change": "added", "after_hash": n.content_hash} for n in after.nodes]
                   if before is None else [change.to_dict() for change in report.changes])
        return {"key": uuid.uuid4().hex, "generated_at": after.generated_at,
                "kind": "WORLD_BASELINE" if before is None else "WORLD_CHANGED",
                "payload": {"project": self.project, "root": str(self.project_root),
                            "before_snapshot_id": before.snapshot_id if before else None,
                            "after_snapshot_id": after.snapshot_id,
                            "observed_at": after.generated_at,
                            "changes": changes,
                            "edges_added": ([e.to_dict() for e in after.edges] if before is None else report.edges_added),
                            "edges_removed": [] if before is None else report.edges_removed}}

    def _recover_pending(self) -> None:
        if not self.pending_path.exists():
            return
        try:
            event = json.loads(self.pending_path.read_text(encoding="utf-8"))
            snapshot = self.load(sync_if_missing=False)
            if (snapshot.generated_at == event["generated_at"]
                    and snapshot.snapshot_id == event["payload"]["after_snapshot_id"]):
                self.event_log.append(event["key"], event["kind"], event["payload"])
            self.pending_path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            # A partially written snapshot will be retried on the next access.
            return

    def events(self, *, after_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        self._recover_pending()
        return self.event_log.read(after_id=after_id, limit=limit)

    def _observe(self, *, full: bool = False) -> tuple[WorldSnapshot, dict[str, list[int]], str, int]:
        scanner = ProjectScanner(self.project_root, self.project)
        paths = {p.relative_to(self.project_root).as_posix(): p for p in scanner._files()}
        try:
            stamps = {rel: [s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino]
                      for rel, path in paths.items() for s in [path.stat()]}
        except OSError:
            # A concurrent rename can invalidate enumeration. A full scan is safer.
            return scanner.scan(), {}, "full", len(paths)
        if not full and self.path.exists() and self.manifest_path.exists():
            try:
                previous = self.load(sync_if_missing=False)
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                old = manifest["stamps"]
                if (manifest.get("version") == 1 and manifest.get("snapshot") == previous.generated_at
                        and previous.stats.get("class_graph_version") == 1
                        and previous.project == self.project and previous.root == str(self.project_root)
                        and set(paths) == set(old) == {n.path for n in previous.nodes}):
                    changed = {rel for rel, stamp in stamps.items() if stamp != old[rel]}
                    if len(changed) <= max(1, len(paths) // 4):
                        try:
                            return scanner.scan_delta(previous, changed, paths), stamps, "delta", len(changed)
                        except OSError:
                            pass
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        return scanner.scan(), stamps, "full", len(paths)

    def _persist(self, snapshot: WorldSnapshot, stamps: dict[str, list[int]], mode: str,
                 changed: int, event: dict[str, Any] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if event is not None:
            pending = self.pending_path.with_suffix(".tmp")
            pending.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
            pending.replace(self.pending_path)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        manifest = {"version": 1, "snapshot": snapshot.generated_at, "stamps": stamps,
                    "last_sync": {"mode": mode, "changed_files": changed}}
        tmp_manifest = self.manifest_path.with_suffix(".tmp")
        tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        tmp_manifest.replace(self.manifest_path)
        self.last_sync = manifest["last_sync"]
        if event is not None:
            self.event_log.append(event["key"], event["kind"], event["payload"])
            self.pending_path.unlink(missing_ok=True)

    def drift(self) -> WorldDriftReport:
        before = self.load(sync_if_missing=False) if self.path.exists() else None
        observed = ProjectScanner(self.project_root, self.project).scan()
        return compare_snapshots(before, observed)

    def sync_with_drift(self, *, full: bool = False) -> tuple[WorldSnapshot, WorldDriftReport]:
        self._recover_pending()
        before = self.load(sync_if_missing=False) if self.path.exists() else None
        observed, stamps, mode, changed = self._observe(full=full)
        report = compare_snapshots(before, observed)
        self._persist(observed, stamps, mode, changed, self._event(before, observed, report))
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

    def is_current(self, snapshot: WorldSnapshot) -> bool:
        """Check source metadata before issuing a context packet from a snapshot."""
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            stamps = manifest["stamps"]
            if (manifest.get("version") != 1 or manifest.get("snapshot") != snapshot.generated_at
                    or snapshot.root != str(self.project_root) or snapshot.project != self.project):
                return False
            paths = {p.relative_to(self.project_root).as_posix(): p
                     for p in ProjectScanner(self.project_root, self.project)._files()}
            if set(paths) != set(stamps) or set(paths) != {n.path for n in snapshot.nodes}:
                return False
            for rel, path in paths.items():
                s = path.stat()
                if stamps[rel] != [s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino]:
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def current(self) -> WorldSnapshot:
        """Reuse a verified snapshot; sync deltas only when source files changed."""
        self._recover_pending()
        if self.path.exists():
            try:
                snapshot = self.load(sync_if_missing=False)
                if self.is_current(snapshot):
                    return snapshot
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return self.sync()

    def status(self) -> dict[str, Any]:
        snap = self.load()
        out = {"project": snap.project, "root": snap.root, "generated_at": snap.generated_at, **snap.stats, "snapshot": str(self.path)}
        if self.manifest_path.exists():
            try:
                out["last_sync"] = json.loads(self.manifest_path.read_text(encoding="utf-8")).get("last_sync", {})
            except (OSError, json.JSONDecodeError):
                pass
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
