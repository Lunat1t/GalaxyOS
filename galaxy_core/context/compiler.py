from __future__ import annotations

from dataclasses import dataclass, asdict, field
import json
from pathlib import Path
from typing import Any

from galaxy_core.brain.store import BrainStore
from galaxy_core.brain.reconcile import MemoryReconciler
from galaxy_core.attention import AttentionEngine
from galaxy_core.world import FutureGraph, ProjectWorldModel


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [f"# Dynamic Agent Context — {self.project}", "", "## Task", self.task, "", "## Project overview"]
        lines += [f"- files: {self.project_overview.get('files', 0)}", f"- dependency edges: {self.project_overview.get('edges', 0)}"]
        comps = self.project_overview.get("components", {})
        if comps: lines.append("- components: " + ", ".join(f"{k} ({v})" for k, v in list(comps.items())[:12]))
        lines += ["", "## Relevant files"]
        for f in self.files:
            lines.append(f"- `{f['path']}` — score {f['score']:.2f}; {f['reason']}")
            if f.get("symbols"): lines.append("  symbols: " + ", ".join(f["symbols"][:12]))
            if f.get("excerpt"): lines.append("  " + f["excerpt"].replace("\n", " ")[:420])
        lines += ["", "## Relevant project knowledge"]
        for m in self.memories:
            lines.append(f"- **{m.get('kind')} · {m.get('title')}** [{m.get('uid')}] confidence={float(m.get('confidence') or .5):.2f}")
            if m.get("summary"): lines.append("  " + " ".join(str(m["summary"]).split())[:420])
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

    def compile(self, task: str, *, budget_tokens: int = 0, max_files: int = 16, refresh_world: bool = False) -> ContextPacket:
        snap = self.world.sync() if refresh_world else self.world.load()
        attention_result = AttentionEngine(self.project_root, self.project).build(
            task, snap, budget_tokens=budget_tokens if budget_tokens > 0 else None, max_files=max_files
        )
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
        packet = ContextPacket(task, self.project, dict(snap.stats), files, graph, memories, instructions, [], 0, effective_budget, impact, health, attention)
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
            q = packet.attention.setdefault("quality", {})
            q["token_utilization"] = round(min(1.0, packet.estimated_tokens / max(1, packet.budget_tokens)), 4)
        return packet

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
        while packet.estimated_tokens > packet.budget_tokens and packet.files:
            packet.files.pop()
            file_ids = {f["path"] for f in packet.files}
            packet.graph = [e for e in packet.graph if e["source"] in file_ids or e["target"] in file_ids]
            packet.estimated_tokens = self._estimate(packet)
        while packet.estimated_tokens > packet.budget_tokens and packet.memories:
            packet.memories.pop()
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
