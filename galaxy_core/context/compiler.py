from __future__ import annotations

from dataclasses import dataclass, asdict, field
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from galaxy_core.brain.store import BrainStore
from galaxy_core.brain.reconcile import MemoryReconciler
from galaxy_core.attention import AttentionEngine
from galaxy_core.attention.index import StaleWorldSnapshot
from galaxy_core.world import FutureGraph, ProjectWorldModel
from .capsules import CapsuleStore, scope_for
from .experience import ExperienceStore


class ContextPacketCache:
    """Bounded per-project persistent cache for serialized Context Packets."""

    VERSION = 2
    MAX_ENTRIES = 128

    def __init__(self, galaxy_root: Path, project_root: Path, project: str):
        scope = hashlib.sha256(f"{project_root.resolve()}\0{project}".encode()).hexdigest()[:20]
        self.path = galaxy_root / "data" / "runtime" / "context-packets" / f"{scope}.sqlite3"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path, timeout=10) as db:
                row = db.execute("SELECT packet_json FROM packets WHERE cache_key = ?", (key,)).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE packets SET last_accessed = ? WHERE cache_key = ?",
                           (time.time(), key))
                value = json.loads(row[0])
                return value if isinstance(value, dict) else None
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return None

    def put(self, key: str, packet: dict[str, Any]) -> bool:
        encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=10) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS packets ("
                           "cache_key TEXT PRIMARY KEY, packet_json TEXT NOT NULL, "
                           "snapshot_id TEXT NOT NULL, created_at REAL NOT NULL, last_accessed REAL NOT NULL)")
                now = time.time()
                db.execute("INSERT OR REPLACE INTO packets VALUES (?, ?, ?, ?, ?)",
                           (key, encoded, str(packet.get("world_snapshot_id") or ""), now, now))
                db.execute("DELETE FROM packets WHERE cache_key IN ("
                           "SELECT cache_key FROM packets ORDER BY last_accessed DESC LIMIT -1 OFFSET ?)",
                           (self.MAX_ENTRIES,))
            return True
        except (sqlite3.Error, OSError):
            return False



@dataclass
class ContextPacket:
    task: str
    project: str
    project_overview: dict[str, Any]
    files: list[dict[str, Any]]
    graph: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    instructions: list[dict[str, Any]]
    uncertainty: list[str]
    estimated_tokens: int
    budget_tokens: int
    impact: dict[str, Any] = field(default_factory=dict)
    knowledge_health: dict[str, Any] = field(default_factory=dict)
    attention: dict[str, Any] = field(default_factory=dict)
    capsules: list[dict[str, Any]] = field(default_factory=list)
    world_snapshot_id: str = ""
    experiences: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [f"# Dynamic Agent Context — {self.project}", "", "## Task", self.task, "", "## Project overview"]
        lines += [f"- files: {self.project_overview.get('files', 0)}", f"- dependency edges: {self.project_overview.get('edges', 0)}"]
        if self.world_snapshot_id: lines.append(f"- world snapshot: `{self.world_snapshot_id}`")
        comps = self.project_overview.get("components", {})
        if comps: lines.append("- components: " + ", ".join(f"{k} ({v})" for k, v in list(comps.items())[:12]))
        if self.capsules:
            lines += ["", "## Component maps"]
            for capsule in self.capsules:
                lines.append(f"### {capsule['scope']} ({capsule['file_count']} files)")
                for item in capsule["files"]:
                    symbols = ", ".join(item["symbols"])
                    lines.append(f"- `{item['path']}`: {item['summary']}" + (f" [{symbols}]" if symbols else ""))
                for edge in capsule["relationships"]:
                    lines.append(f"- `{edge['source']}` --{edge['relation']}--> `{edge['target']}`")
        lines += ["", "## Relevant files"]
        for f in self.files:
            lines.append(f"- `{f['path']}` — score {f['score']:.2f}; {f['reason']}")
            if f.get("symbols"): lines.append("  symbols: " + ", ".join(f["symbols"][:12]))
            if f.get("excerpt"): lines.append("  " + f["excerpt"].replace("\n", " ")[:420])
        lines += ["", "## Relevant project knowledge"]
        for m in self.memories:
            lines.append(f"- **{m.get('kind')} · {m.get('title')}** [{m.get('uid')}] confidence={float(m.get('confidence') or .5):.2f}")
            if m.get("summary"): lines.append("  " + " ".join(str(m["summary"]).split())[:420])
        if self.experiences:
            lines += ["", "## Relevant prior experience (observations, verify before reuse)"]
            for item in self.experiences:
                lines.append(f"- [{item['id']}] {item['outcome']} · {item['role']} · run {item['run_id']}/{item['node_id']}: {item['lesson'] or item['summary']}")
                lines.append(f"  verified={bool(item['verified'])}; evidence: {', '.join(item['evidence']) or 'none'}")
        if self.instructions:
            lines += ["", "## Project instructions"]
            for i in self.instructions:
                lines.append(f"### {i['path']}")
                lines.append(i["content"][:2500])
        if self.graph:
            lines += ["", "## Relevant relationships"]
            for e in self.graph[:40]: lines.append(f"- `{e['source']}` --{e['relation']}--> `{e['target']}`")
        if self.attention:
            lines += ["", "## Attention Engine"]
            lines.append(f"- strategy: {self.attention.get('strategy', 'unknown')}")
            budget = self.attention.get("budget") or {}
            lines.append(f"- budget profile: {budget.get('profile', 'unknown')} · effective={budget.get('effective_tokens', self.budget_tokens)} tokens")
            quality = self.attention.get("quality") or {}
            lines.append(
                "- retrieval signals: "
                f"coverage={float(quality.get('coverage_signal') or 0):.2f}; "
                f"noise={float(quality.get('noise_signal') or 0):.2f}; "
                f"graph={float(quality.get('graph_coverage_signal') or 0):.2f}; "
                f"selection_confidence={float(quality.get('selection_confidence') or 0):.2f}"
            )
            lines.append("- note: these are retrieval heuristics, not measured answer accuracy")
        if self.impact:
            lines += ["", "## Predicted structural impact"]
            lines.append(f"- structural risk: {float(self.impact.get('structural_risk') or 0):.2f}")
            comps = self.impact.get("affected_components") or []
            if comps: lines.append("- affected components: " + ", ".join(comps[:12]))
            for node in (self.impact.get("impacted") or [])[:12]:
                lines.append(f"- `{node.get('path')}` — {node.get('impact')} d={node.get('distance')} confidence={float(node.get('confidence') or 0):.2f}")
            for note in (self.impact.get("uncertainty") or [])[:4]:
                lines.append("- uncertainty: " + str(note))
        if self.knowledge_health and self.knowledge_health.get("counts", {}).get("total"):
            lines += ["", "## Knowledge health"]
            counts = self.knowledge_health.get("counts", {})
            lines.append(f"- detected findings: {counts.get('total', 0)}; stale-source candidates: {counts.get('source_missing', 0) + counts.get('source_changed', 0)}; competing assertions: {counts.get('competing_assertion', 0)}")
            for finding in (self.knowledge_health.get("findings") or [])[:5]:
                lines.append(f"- `{finding.get('uid')}` {finding.get('type')}: {finding.get('evidence')}")
        if self.uncertainty:
            lines += ["", "## Uncertainty"] + [f"- {x}" for x in self.uncertainty]
        lines += ["", f"Estimated context: ~{self.estimated_tokens} tokens / budget {self.budget_tokens}"]
        return "\n".join(lines) + "\n"

class ContextCompiler:
    """Compile a minimal, provenance-rich packet instead of dumping an entire repository into an agent."""
    def __init__(self, galaxy_root: str | Path, project_root: str | Path | None = None, project: str = "default"):
        self.galaxy_root = Path(galaxy_root).resolve()
        self.project_root = Path(project_root or galaxy_root).resolve()
        self.project = project
        self.world = ProjectWorldModel(self.galaxy_root, self.project_root, project)
        self.brain = BrainStore(self.galaxy_root)
        self.capsules = CapsuleStore(self.galaxy_root, self.project_root, project)
        self.experience = ExperienceStore(self.galaxy_root, self.project_root)
        self.packet_cache = ContextPacketCache(self.galaxy_root, self.project_root, project)

    def compile(self, task: str, *, budget_tokens: int = 0, max_files: int = 16,
                role: str = "", owner_id: str | None = None, team_id: str | None = None,
                refresh_world: bool = False, _retry: bool = False) -> ContextPacket:
        started = time.perf_counter()
        if max_files < 1:
            raise ValueError("max_files must be positive")
        snap = self.world.sync() if refresh_world else self.world.current()
        memory_signature = self._memory_signature()
        experience_signature = self.experience.signature()
        key_payload = [
            ContextPacketCache.VERSION, task, self.project, str(self.project_root),
            snap.snapshot_id, memory_signature, experience_signature, role, owner_id, team_id,
            max(0, int(budget_tokens)), max(1, int(max_files)),
        ]
        cache_key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False,
                                               separators=(",", ":")).encode()).hexdigest()
        cached = self.packet_cache.get(cache_key) if memory_signature is not None else None
        if cached is not None:
            try:
                packet = ContextPacket(**cached)
            except (TypeError, ValueError):
                packet = None
        else:
            packet = None
        if packet is not None:
            if self.world.is_current(snap) and self._memory_signature() == memory_signature and self.experience.signature() == experience_signature:
                self._mark_packet_cache(packet, "hit", time.perf_counter() - started)
                return packet
            if _retry:
                raise StaleWorldSnapshot("project or memory changed while loading cached context")
            return self.compile(task, budget_tokens=budget_tokens, max_files=max_files, role=role, owner_id=owner_id, team_id=team_id,
                                refresh_world=True, _retry=True)

        packet = self._compile_uncached(task, budget_tokens=budget_tokens, max_files=max_files,
                                        role=role, owner_id=owner_id, team_id=team_id,
                                        refresh_world=False, _retry=_retry, _snapshot=snap)
        if not self.world.is_current(snap):
            if _retry:
                raise StaleWorldSnapshot("source changed twice while caching context")
            return self.compile(task, budget_tokens=budget_tokens, max_files=max_files, role=role, owner_id=owner_id, team_id=team_id,
                                refresh_world=True, _retry=True)
        if memory_signature is not None and (self._memory_signature() != memory_signature or self.experience.signature() != experience_signature):
            if _retry:
                raise RuntimeError("project memory changed repeatedly while compiling context")
            return self.compile(task, budget_tokens=budget_tokens, max_files=max_files, role=role, owner_id=owner_id, team_id=team_id,
                                refresh_world=False, _retry=True)
        self._mark_packet_cache(packet, "miss", time.perf_counter() - started)
        packet.attention["packet_cache"]["stored"] = False
        packet.estimated_tokens = self._estimate(packet)
        if memory_signature is not None and packet.estimated_tokens <= packet.budget_tokens:
            packet.attention["packet_cache"]["stored"] = True
            packet.estimated_tokens = self._estimate(packet)
            packet.attention["packet_cache"]["stored"] = (
                self.packet_cache.put(cache_key, packet.to_dict())
                if packet.estimated_tokens <= packet.budget_tokens else False
            )
            packet.estimated_tokens = self._estimate(packet)
        return packet

    def _mark_packet_cache(self, packet: ContextPacket, status: str, elapsed: float) -> None:
        packet.attention["packet_cache"] = {
            "status": status,
            "elapsed_ms": round(elapsed * 1000, 3),
        }
        self._trim(packet)
        if packet.attention:
            packet.attention["selected_files"] = [item["path"] for item in packet.files]
        packet.estimated_tokens = self._estimate(packet)

    def _memory_signature(self) -> str | None:
        """Hash stable memory inputs that can change the packet; ignore read counters."""
        if not self.brain.db.exists():
            return "empty"
        digest = hashlib.sha256()
        tables = {
            "memories": "id,uid,agent,kind,task,project,title,summary,tags,success,qa_pass,created_at,run_id,source,dedupe_key,status,source_type,source_ref,source_hash,confidence,importance,confirmed_at,confirmed_by,updated_at,valid_from,valid_to,supersedes_uid,metadata",
            "memory_links": "from_uid,to_uid,relation,created_at",
            "goals": "id,uid,project,title,description,status,priority,horizon,success_criteria,review_at,source,created_at,updated_at",
            "goal_memory_links": "goal_uid,memory_uid,relation,created_at",
            "entities": "uid,canonical,entity_type,display_name,created_at",
            "entity_aliases": "entity_uid,alias",
            "memory_entities": "memory_uid,entity_uid,confidence,source",
            "entity_relations": "from_uid,to_uid,relation,weight,evidence,created_at",
        }
        try:
            with sqlite3.connect(f"file:{self.brain.db}?mode=ro", uri=True, timeout=5) as db:
                existing = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for table, columns in tables.items():
                    if table not in existing:
                        continue
                    digest.update(table.encode())
                    for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY {columns}"):
                        digest.update(json.dumps(row, ensure_ascii=False, default=str,
                                                 separators=(",", ":")).encode())
                        digest.update(b"\n")
        except (sqlite3.Error, OSError):
            # A fingerprint failure must disable reuse instead of serving stale memory.
            return None
        # Memory ranking has a day-granularity age boost; expire that component at midnight.
        digest.update(time.strftime("%Y-%m-%d").encode())
        return digest.hexdigest()

    def _compile_uncached(self, task: str, *, budget_tokens: int = 0, max_files: int = 16,
                          role: str = "", owner_id: str | None = None, team_id: str | None = None,
                          refresh_world: bool = False, _retry: bool = False,
                          _snapshot=None) -> ContextPacket:
        snap = _snapshot or (self.world.sync() if refresh_world else self.world.current())
        try:
            attention_result = AttentionEngine(self.project_root, self.project, storage_root=self.galaxy_root).build(
                task, snap, budget_tokens=budget_tokens if budget_tokens > 0 else None, max_files=max_files
            )
        except StaleWorldSnapshot:
            if _retry:
                raise
            return self.compile(task, budget_tokens=budget_tokens, max_files=max_files, role=role, owner_id=owner_id, team_id=team_id,
                                refresh_world=True, _retry=True)
        effective_budget = attention_result.budget.effective_tokens
        files: list[dict[str, Any]] = []
        for c in attention_result.selected:
            files.append({
                "path": c.path, "kind": c.kind, "language": c.language, "component": c.component,
                "symbols": list(c.symbols), "summary": "", "score": round(c.score, 4),
                "reason": "; ".join(dict.fromkeys(c.reasons)) or "attention selection",
                "role": c.role, "gate": c.gate, "gate_confidence": c.gate_confidence,
                "retrieval": {
                    "bm25": c.bm25_score, "lexical": c.lexical_score, "semantic": c.semantic_score,
                    "graph": c.graph_score, "future_impact": c.impact_score,
                },
                "evidence": list(c.evidence), "excerpt": c.excerpt,
            })
        file_ids = {f["path"] for f in files}
        graph = [e.to_dict() for e in snap.edges if e.source in file_ids or e.target in file_ids][:80]
        memories = self.brain.context(task, project=self.project, limit=12)
        experiences = self.experience.relevant(task, project=self.project, role=role,
                                               owner_id=owner_id, team_id=team_id,
                                               budget_tokens=max(200, min(900, effective_budget // 10)))
        instructions = self._instructions([f["path"] for f in files])
        seeds = [f["path"] for f in files if f.get("role") == "primary"][:3] or [f["path"] for f in files[:3]]
        scenario = FutureGraph(snap).simulate(task, seeds=seeds or None, depth=2, max_nodes=24)
        impact = scenario.to_dict()
        health = MemoryReconciler(self.brain, self.project_root).scan(project=self.project, apply=False, limit=1000)
        attention = {
            "strategy": attention_result.strategy,
            "budget": attention_result.budget.to_dict(),
            "quality": attention_result.quality.to_dict(),
            "candidates_considered": attention_result.candidates_considered,
            "selected_files": [x.path for x in attention_result.selected],
            "dropped": attention_result.dropped,
            "trace": attention_result.trace,
        }
        packet = ContextPacket(task, self.project, dict(snap.stats), files, graph, memories, instructions, [], 0,
                               effective_budget, impact, health, attention, world_snapshot_id=snap.snapshot_id,
                               experiences=experiences)
        if health.get("counts", {}).get("total"):
            packet.uncertainty.append("Living memory has unresolved evidence/conflict findings; see knowledge_health before treating all project knowledge as current.")
        if attention_result.quality.selection_confidence < 0.68:
            packet.uncertainty.append("Attention Engine selection confidence is moderate; consider repository exploration before a high-risk change.")
        self._trim(packet)
        if packet.attention:
            before = len(packet.attention.get("selected_files") or [])
            packet.attention["selected_files"] = [f["path"] for f in packet.files]
            packet.attention["selected_after_compile"] = len(packet.files)
            packet.attention["trimmed_by_compiler"] = max(0, before - len(packet.files))
            capsules, cache_stats = self.capsules.select(snap, [f["path"] for f in packet.files])
            included = 0
            for capsule in capsules:
                packet.capsules.append(capsule)
                size = self._estimate(packet)
                if size > packet.budget_tokens:
                    packet.capsules.pop()
                else:
                    packet.estimated_tokens = size
                    included += 1
            packet.attention["capsule_cache"] = {**cache_stats, "included": included}
            packet.estimated_tokens = self._estimate(packet)
            q = packet.attention.setdefault("quality", {})
            q["token_utilization"] = round(min(1.0, packet.estimated_tokens / max(1, packet.budget_tokens)), 4)
        packet.estimated_tokens = self._estimate(packet)
        if not self.world.is_current(snap):
            if _retry:
                raise StaleWorldSnapshot("source changed twice while compiling context")
            return self.compile(task, budget_tokens=budget_tokens, max_files=max_files,
                                refresh_world=True, _retry=True)
        return packet

    def overview(self, task: str, *, refresh_world: bool = False, limit: int = 2,
                 _retry: bool = False) -> dict[str, Any]:
        """Fast L1 component map without running Attention or loading source text."""
        snap = self.world.sync() if refresh_world else self.world.current()
        terms = set(re.findall(r"[^\W_]{2,}", task.casefold(), flags=re.UNICODE))
        ranked: dict[str, tuple[float, str]] = {}
        for node in snap.nodes:
            scope = scope_for(node.path)
            path_terms = set(re.findall(r"[^\W_]{2,}", node.path.replace("/", " ").replace("_", " ").casefold()))
            symbol_terms = set(re.findall(r"[^\W_]{2,}", " ".join(node.symbols).replace("_", " ").casefold()))
            summary_terms = set(re.findall(r"[^\W_]{2,}", node.summary.casefold()))
            score = 3 * len(terms & path_terms) + 2 * len(terms & symbol_terms) + len(terms & summary_terms)
            if score > ranked.get(scope, (-1, ""))[0]:
                ranked[scope] = (score, node.path)
        paths = [item[1] for _, item in sorted(ranked.items(), key=lambda pair: (-pair[1][0], pair[0]))[:max(0, limit)]]
        capsules, stats = self.capsules.select(snap, paths, limit=limit)
        result = {"task": task, "project": self.project, "world_snapshot_id": snap.snapshot_id,
                  "capsules": capsules, "cache": stats}
        result["estimated_tokens"] = max(1, len(json.dumps(result, ensure_ascii=False)) // 4)
        if not self.world.is_current(snap):
            if _retry:
                raise StaleWorldSnapshot("source changed twice while compiling overview")
            return self.overview(task, refresh_world=True, limit=limit, _retry=True)
        return result

    def export(self, task: str, destination: str | Path, **kwargs) -> Path:
        packet = self.compile(task, **kwargs)
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() == ".json":
            dest.write_text(json.dumps(packet.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            dest.write_text(packet.to_markdown(), encoding="utf-8")
        return dest

    def _instructions(self, file_paths: list[str]) -> list[dict[str, Any]]:
        """Collect hierarchical agent instructions relevant to selected files.

        Root instructions are always considered. For each selected file, parent directories are
        walked from project root to the file. `AGENTS.override.md` at a directory supersedes
        `AGENTS.md` for that same directory, while higher-level instructions remain visible.
        """
        found: dict[str, dict[str, Any]] = {}
        directories = {Path(".")}
        for rel in file_paths:
            parent = Path(rel).parent
            chain = [Path(".")]
            current = Path()
            for part in parent.parts:
                current = current / part
                chain.append(current)
            directories.update(chain)
        for directory in sorted(directories, key=lambda p: (len(p.parts), p.as_posix())):
            candidates = [directory / "AGENTS.override.md", directory / "AGENTS.md", directory / "CLAUDE.md"]
            override = candidates[0]
            selected = []
            if (self.project_root / override).is_file():
                selected.append(override)
                # Same-directory AGENTS.md is intentionally shadowed by the override.
                selected += [candidates[2]]
            else:
                selected += candidates[1:]
            for rel_path in selected:
                p = self.project_root / rel_path
                if not p.is_file():
                    continue
                key = rel_path.as_posix()
                found[key] = {"path": key, "content": p.read_text(encoding="utf-8", errors="replace")[:5000]}
        return list(found.values())

    @staticmethod
    def _estimate(packet: ContextPacket) -> int:
        raw = json.dumps(packet.to_dict(), ensure_ascii=False)
        return max(1, len(raw) // 4)

    def _trim(self, packet: ContextPacket) -> None:
        packet.estimated_tokens = self._estimate(packet)
        # Structural maps are optional; they must never displace task evidence.
        while packet.estimated_tokens > packet.budget_tokens and packet.capsules:
            packet.capsules.pop()
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and packet.files:
            packet.files.pop()
            file_ids = {f["path"] for f in packet.files}
            packet.graph = [e for e in packet.graph if e["source"] in file_ids or e["target"] in file_ids]
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and packet.memories:
            packet.memories.pop()
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and packet.experiences:
            packet.experiences.pop()
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and packet.graph:
            packet.graph.pop()
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and (packet.impact.get("impacted") or []):
            packet.impact["impacted"].pop()
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and (packet.knowledge_health.get("findings") or []):
            packet.knowledge_health["findings"].pop()
            packet.estimated_tokens = self._estimate(packet)
        # Hierarchical instructions are valuable, but a hard budget wins. Truncate before dropping them.
        while packet.estimated_tokens > packet.budget_tokens and packet.instructions:
            last = packet.instructions[-1]
            content = last.get("content", "")
            if len(content) > 500:
                last["content"] = content[:max(500, len(content)//2)]
            else:
                packet.instructions.pop()
            packet.estimated_tokens = self._estimate(packet)
        if packet.estimated_tokens > packet.budget_tokens:
            packet.uncertainty.append("Context budget exhausted; only a partial project view fits the requested budget.")
            packet.estimated_tokens = self._estimate(packet)
