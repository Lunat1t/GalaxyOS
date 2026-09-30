"""Small, deterministic component maps derived from a WorldSnapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from galaxy_core.world.models import WorldSnapshot


def scope_for(path: str) -> str:
    parts = Path(path).parts
    if len(parts) == 1:
        return "root"
    if parts[0] == "galaxy_core" and len(parts) >= 3:
        return "/".join(parts[:2])
    return parts[0]


class CapsuleStore:
    VERSION = 1

    def __init__(self, galaxy_root: Path, project_root: Path, project: str):
        key = hashlib.sha256(f"{project_root.resolve()}\0{project}".encode()).hexdigest()[:20]
        self.path = galaxy_root.resolve() / "data" / "runtime" / "capsules" / f"{key}.json"

    def select(self, snapshot: WorldSnapshot, selected_paths: list[str], *, limit: int = 2
               ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        nodes: dict[str, list[Any]] = {}
        for node in snapshot.nodes:
            nodes.setdefault(scope_for(node.path), []).append(node)
        edges_by_scope: dict[str, list[tuple[str, str, str]]] = {}
        for edge in snapshot.edges:
            item = (edge.source, edge.target, edge.relation)
            source_scope = scope_for(edge.source)
            target_scope = scope_for(edge.target)
            edges_by_scope.setdefault(source_scope, []).append(item)
            if target_scope != source_scope:
                edges_by_scope.setdefault(target_scope, []).append(item)
        wanted = list(dict.fromkeys(scope_for(p) for p in selected_paths if scope_for(p) in nodes))[:limit]
        try:
            cached = json.loads(self.path.read_text(encoding="utf-8"))
            entries = cached.get("entries", {}) if cached.get("version") == self.VERSION else {}
            if not isinstance(entries, dict):
                entries = {}
        except (OSError, ValueError, TypeError):
            entries = {}
        old_count = len(entries)
        entries = {k: v for k, v in entries.items() if k in nodes}
        capsules: list[dict[str, Any]] = []
        hits = misses = 0
        for scope in wanted:
            group = sorted(nodes[scope], key=lambda n: n.path)
            edges = sorted(edges_by_scope.get(scope, []))
            source = [(n.path, n.content_hash, n.kind, n.symbols, n.summary) for n in group]
            digest = hashlib.sha256(json.dumps([self.VERSION, source, edges], ensure_ascii=False).encode()).hexdigest()[:20]
            entry = entries.get(scope)
            if isinstance(entry, dict) and entry.get("fingerprint") == digest:
                hits += 1
            else:
                entry = self._build(scope, group, edges, digest)
                entries[scope] = entry
                misses += 1
            capsules.append(entry)
        if misses or len(entries) != old_count or not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": self.VERSION, "entries": entries}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        return capsules, {"hits": hits, "built": misses}

    @staticmethod
    def _build(scope: str, group: list[Any], edges: list[tuple[str, str, str]], digest: str) -> dict[str, Any]:
        # A map of observed files, not an LLM summary or a claim about behavior.
        lead = sorted(group, key=lambda n: (n.kind != "source", n.path))[:5]
        files = [{"path": n.path, "summary": n.summary[:150], "symbols": list(n.symbols[:5])} for n in lead]
        links = [{"source": a, "target": b, "relation": relation} for a, b, relation in edges[:6]]
        return {"scope": scope, "fingerprint": digest, "file_count": len(group),
                "files": files, "relationships": links}
