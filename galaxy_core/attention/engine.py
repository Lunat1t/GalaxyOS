"""Galaxy 3.2 Attention Engine.

A dependency-free hybrid retrieval and context-selection pipeline for software repositories:
wide lexical/BM25 retrieval -> portable semantic scoring -> graph/Future-Graph expansion ->
confidence-aware gatekeeper -> evidence extraction -> adaptive token packing.

The engine is intentionally not a factual oracle. Its quality fields are heuristic signals that
make selection auditable; they are not claims of model accuracy.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import math
from pathlib import Path
import re
from typing import Any, Iterable

from galaxy_core.brain.semantic import PortableEmbedder
from galaxy_core.world import FutureGraph, WorldSnapshot

from .models import AttentionBudget, AttentionCandidate, AttentionQuality, AttentionResult

TOKEN_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
HIGH_COMPLEXITY = {
    "architecture", "architect", "refactor", "migration", "migrate", "distributed", "security",
    "authentication", "authorization", "database", "schema", "performance", "concurrency", "protocol",
    "архитект", "рефактор", "миграц", "безопас", "авторизац", "баз", "схем", "производ", "протокол",
}
LOW_COMPLEXITY = {"typo", "rename", "readme", "comment", "format", "text", "опечат", "переимен", "комментар"}
TEST_TERMS = {"test", "tests", "bug", "fix", "verify", "ошиб", "тест", "проверк", "исправ"}
DOC_TERMS = {"doc", "docs", "readme", "documentation", "докум"}
CODE_ACTION_TERMS = {"change", "implement", "refactor", "fix", "modify", "rewrite", "add", "remove", "migrate", "измен", "рефактор", "исправ", "добав", "удал", "перепис", "миграц"}


def _tokens(text: str) -> list[str]:
    return [x.casefold() for x in TOKEN_RE.findall(text or "")]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


class AdaptiveBudgeter:
    """Reserve context budget by role instead of letting files consume everything."""

    @staticmethod
    def recommend(task: str, stats: dict[str, Any], requested_tokens: int | None = None) -> AttentionBudget:
        toks = set(_tokens(task))
        files = int(stats.get("files") or 0)
        high = bool(toks & HIGH_COMPLEXITY) or len(toks) >= 24 or files >= 2500
        low = bool(toks & LOW_COMPLEXITY) and len(toks) <= 12 and not high
        if requested_tokens and requested_tokens > 0:
            effective = max(1200, int(requested_tokens))
            profile = "explicit"
            reason = "caller supplied a hard context budget"
        elif high:
            effective = 12000
            profile = "high"
            reason = "broad/architectural task or very large repository"
        elif low:
            effective = 2800
            profile = "low"
            reason = "small localized task"
        else:
            effective = 6500
            profile = "medium"
            reason = "default engineering task"
        # Roles are deliberately explicit so later stages cannot silently starve memory/instructions.
        file_tokens = int(effective * 0.56)
        memory_tokens = int(effective * 0.13)
        graph_tokens = int(effective * 0.12)
        instruction_tokens = int(effective * 0.10)
        reserve_tokens = max(0, effective - file_tokens - memory_tokens - graph_tokens - instruction_tokens)
        return AttentionBudget(profile, int(requested_tokens or 0), effective, file_tokens, memory_tokens,
                               graph_tokens, instruction_tokens, reserve_tokens, reason)


class EvidenceExtractor:
    """Extract query-bearing line windows instead of blindly taking file prefixes."""

    def extract(self, task: str, path: str, text: str, symbols: Iterable[str], *, max_chars: int = 2200) -> list[str]:
        if not text:
            return []
        query = set(_tokens(task))
        symbol_terms = set(_tokens(" ".join(symbols)))
        path_terms = set(_tokens(path.replace("/", " ").replace("_", " ")))
        lines = text.splitlines()
        scores: list[tuple[float, int]] = []
        for i, line in enumerate(lines):
            lt = set(_tokens(line))
            score = len(query & lt) * 3.0 + len(symbol_terms & lt) * 1.4 + len(path_terms & lt) * 0.35
            if any(term in line.casefold() for term in query if len(term) >= 4):
                score += 0.5
            if score > 0:
                scores.append((score, i))
        if not scores:
            compact = text[:min(max_chars, 900)].strip()
            return [compact] if compact else []
        scores.sort(key=lambda x: (-x[0], x[1]))
        windows: list[str] = []
        covered: set[int] = set()
        used = 0
        for _score, idx in scores[:10]:
            start, end = max(0, idx - 2), min(len(lines), idx + 4)
            if any(j in covered for j in range(start, end)):
                continue
            chunk = "\n".join(lines[start:end]).strip()
            if not chunk:
                continue
            remaining = max_chars - used
            if remaining <= 120:
                break
            chunk = chunk[:remaining]
            windows.append(chunk)
            used += len(chunk) + 2
            covered.update(range(start, end))
            if used >= max_chars:
                break
        return windows


class AttentionGatekeeper:
    """Bounded gatekeeper. Optional decision fabric can arbitrate only the uncertain middle."""

    def __init__(self, decision_fabric: Any | None = None):
        self.fabric = decision_fabric

    def gate(self, candidate: AttentionCandidate, *, max_score: float) -> AttentionCandidate:
        normalized = candidate.score / max(max_score, 1e-9)
        # Future-impact candidates and direct tests get a modest safety boost, not automatic inclusion.
        effective = min(1.0, normalized + min(0.16, candidate.impact_score * 0.12))
        if effective >= 0.58:
            candidate.gate = "include"
            candidate.gate_confidence = min(0.99, 0.62 + (effective - 0.58) * 0.85)
        elif effective >= 0.24:
            candidate.gate = "consider"
            candidate.gate_confidence = max(0.50, 0.78 - abs(effective - 0.41))
        else:
            candidate.gate = "drop"
            candidate.gate_confidence = min(0.99, 0.66 + (0.24 - effective))

        # A configured probabilistic decision layer may refine only the ambiguous middle. If its
        # confidence is low, Galaxy keeps the candidate as `consider` rather than silently dropping it.
        if self.fabric is not None and candidate.gate == "consider":
            state = (
                f"path={candidate.path}; kind={candidate.kind}; combined={effective:.3f}; "
                f"lexical={candidate.lexical_score:.3f}; semantic={candidate.semantic_score:.3f}; "
                f"graph={candidate.graph_score:.3f}; impact={candidate.impact_score:.3f}"
            )
            try:
                decision = self.fabric.choose(state, "Should this bounded repository candidate be included in task context?", ["include", "drop"])
                if decision.action == "auto" and decision.confidence >= 0.92:
                    candidate.gate = str(decision.value)
                    candidate.gate_confidence = float(decision.confidence)
                    candidate.reasons.append(f"probabilistic gatekeeper:{decision.provider}")
                else:
                    candidate.reasons.append("probabilistic gatekeeper escalated")
            except Exception:
                candidate.reasons.append("probabilistic gatekeeper unavailable; deterministic gate retained")
        return candidate


class AttentionEngine:
    def __init__(self, project_root: str | Path, project: str = "default", *, decision_fabric: Any | None = None):
        self.root = Path(project_root).resolve()
        self.project = project
        self.embedder = PortableEmbedder(dims=192)
        self.extractor = EvidenceExtractor()
        self.gatekeeper = AttentionGatekeeper(decision_fabric)

    def build(self, task: str, snapshot: WorldSnapshot, *, budget_tokens: int | None = None,
              max_candidates: int = 72, max_files: int = 16) -> AttentionResult:
        budget = AdaptiveBudgeter.recommend(task, snapshot.stats, budget_tokens)
        nodes = {n.path: n for n in snapshot.nodes}
        if not nodes:
            return AttentionResult(task, self.project, "empty", budget, 0, [], 0, AttentionQuality(), {})

        total_source_tokens = sum(max(1, int(n.size_bytes) // 4) for n in snapshot.nodes)
        small_full_context = len(nodes) <= min(max_files, 20) and total_source_tokens <= int(budget.file_tokens * 0.82)
        docs = self._load_documents(snapshot.nodes)
        bm25 = self._bm25(task, docs)
        lexical = self._lexical(task, snapshot)
        semantic = self._semantic(task, snapshot)

        combined: dict[str, AttentionCandidate] = {}
        bm_max = max(bm25.values(), default=1.0) or 1.0
        lex_max = max(lexical.values(), default=1.0) or 1.0
        task_terms = set(_tokens(task))
        wants_tests = bool(task_terms & TEST_TERMS)
        wants_docs = bool(task_terms & DOC_TERMS)
        code_task = bool(task_terms & (CODE_ACTION_TERMS | HIGH_COMPLEXITY)) and not wants_docs
        for path, node in nodes.items():
            b = bm25.get(path, 0.0) / bm_max
            l = lexical.get(path, 0.0) / lex_max
            s = max(0.0, semantic.get(path, 0.0))
            # Documentation/test files often repeat subsystem names and can dominate BM25. For code
            # modification tasks they remain evidence, but source/configuration gets the primary prior.
            kind_factor = 1.0
            if code_task and node.kind == "documentation":
                kind_factor = 0.48
            elif code_task and node.kind == "test" and not wants_tests:
                kind_factor = 0.66
            score = kind_factor * (0.40 * b + 0.24 * l + 0.36 * s)
            reasons = []
            if b >= 0.12: reasons.append("BM25 content/path match")
            if l >= 0.12: reasons.append("path/symbol lexical match")
            if s >= 0.30: reasons.append("portable semantic similarity")
            path_overlap = len(task_terms & set(_tokens(node.path.replace("/", " ").replace("_", " "))))
            if code_task and node.kind == "source":
                score += 0.08 + min(0.16, path_overlap * 0.06)
                reasons.append("source prior for code-change task")
            if node.kind == "test" and wants_tests:
                score += 0.07; reasons.append("test-sensitive task")
            if node.kind == "documentation" and wants_docs:
                score += 0.07; reasons.append("documentation-sensitive task")
            if score > 0.035 or small_full_context:
                combined[path] = AttentionCandidate(
                    path=path, kind=node.kind, language=node.language, component=node.component,
                    symbols=list(node.symbols), lexical_score=round(l, 4), bm25_score=round(b, 4),
                    semantic_score=round(s, 4), score=score, reasons=reasons,
                )

        # Wide retrieval first, then structural expansion.
        ranked_initial = sorted(combined.values(), key=lambda c: (-c.score, c.path))[:max_candidates]
        # Structural propagation should start from implementation candidates when the task is about
        # changing code; otherwise docs/tests that merely mention the subsystem can distort blast radius.
        source_seeds = [x.path for x in ranked_initial if x.kind in {"source", "configuration"}]
        if code_task and source_seeds:
            seed_paths = (source_seeds[:4] + [x.path for x in ranked_initial if x.path not in source_seeds][:1])[:5]
        else:
            seed_paths = [x.path for x in ranked_initial[:8]]
        graph_scores = self._graph_expand(snapshot, seed_paths, depth=2)
        future = FutureGraph(snapshot).simulate(task, seeds=seed_paths[:3] or None, depth=2, max_nodes=max(24, max_candidates // 2))
        impact_by_path: dict[str, float] = {}
        for item in future.impacted:
            impact_by_path[item.path] = max(impact_by_path.get(item.path, 0.0), float(item.confidence))

        for path in set(graph_scores) | set(impact_by_path):
            node = nodes.get(path)
            if not node:
                continue
            c = combined.setdefault(path, AttentionCandidate(path, node.kind, node.language, node.component, list(node.symbols)))
            c.graph_score = round(graph_scores.get(path, 0.0), 4)
            c.impact_score = round(impact_by_path.get(path, 0.0), 4)
            c.score += 0.24 * c.graph_score + 0.18 * c.impact_score
            if c.graph_score >= 0.20: c.reasons.append("dependency graph expansion")
            if c.impact_score >= 0.20: c.reasons.append("Future Graph blast-radius candidate")

        candidates = sorted(combined.values(), key=lambda c: (-c.score, c.path))[:max_candidates]
        max_score = max((c.score for c in candidates), default=1.0)
        for c in candidates:
            self.gatekeeper.gate(c, max_score=max_score)
            if c.path in seed_paths[:3]:
                c.role = "primary"
            elif c.impact_score >= 0.50:
                c.role = "affected"
            elif c.kind == "test":
                c.role = "test"
            elif c.kind == "documentation":
                c.role = "documentation"
            elif c.graph_score > 0:
                c.role = "dependency"
            else:
                c.role = "supporting"
            text = docs.get(c.path, "")
            per_file_chars = 3200 if c.role == "primary" else 1900
            c.evidence = self.extractor.extract(task, c.path, text, c.symbols, max_chars=per_file_chars)
            c.excerpt = "\n\n…\n\n".join(c.evidence)
            c.estimated_tokens = _estimate_tokens(c.excerpt)

        if small_full_context:
            strategy = "small-project-full-context"
            for c in candidates:
                c.gate = "include"
                c.gate_confidence = max(c.gate_confidence, 0.95)
                c.reasons.append("project fits bounded full-context strategy")
                raw = docs.get(c.path, "")
                c.excerpt = raw[:min(len(raw), 5000)]
                c.evidence = [c.excerpt] if c.excerpt else []
                c.estimated_tokens = _estimate_tokens(c.excerpt)
        else:
            strategy = "hybrid-attention"

        selected = self._pack(candidates, budget.file_tokens, max_files)
        selected_paths = {x.path for x in selected}
        selected_score = sum(max(0.0, c.score) for c in selected)
        candidate_score = sum(max(0.0, c.score) for c in candidates if c.gate != "drop") or 1.0
        future_paths = {x.path for x in future.impacted if x.confidence >= 0.35}
        graph_cov = len(future_paths & selected_paths) / max(1, len(future_paths)) if future_paths else 1.0
        considered_selected = sum(1 for c in selected if c.gate == "consider")
        used_tokens = sum(c.estimated_tokens for c in selected)
        confidences = [c.gate_confidence for c in selected] or [0.0]
        quality = AttentionQuality(
            coverage_signal=round(min(1.0, selected_score / candidate_score), 4),
            noise_signal=round(considered_selected / max(1, len(selected)), 4),
            graph_coverage_signal=round(min(1.0, graph_cov), 4),
            token_utilization=round(min(1.0, used_tokens / max(1, budget.file_tokens)), 4),
            selection_confidence=round(sum(confidences) / len(confidences), 4),
        )
        trace = {
            "wide_pool": len(candidates),
            "seed_paths": seed_paths[:8],
            "future_graph_candidates": len(future.impacted),
            "future_structural_risk": future.structural_risk,
            "file_budget_tokens": budget.file_tokens,
            "selected_file_tokens": used_tokens,
            "gate_counts": {g: sum(1 for c in candidates if c.gate == g) for g in ("include", "consider", "drop")},
            # Raw score order is exposed for offline retrieval benchmarks. Context packing can reorder
            # primary/include candidates and is measured separately as budget_recall.
            "ranked_paths": [c.path for c in candidates],
            "quality_note": "Signals are retrieval heuristics, not measured answer accuracy.",
        }
        return AttentionResult(task, self.project, strategy, budget, len(candidates), selected,
                               max(0, len(candidates) - len(selected)), quality, trace)

    def _load_documents(self, nodes: Iterable[Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for node in nodes:
            path = self.root / node.path
            try:
                # Bounded read keeps retrieval predictable on generated/minified files.
                text = path.read_text(encoding="utf-8", errors="replace")[:28_000]
            except OSError:
                text = ""
            prefix = f"{node.path}\n{' '.join(node.symbols)}\n{node.summary}\n"
            out[node.path] = prefix + text
        return out

    def _lexical(self, task: str, snapshot: WorldSnapshot) -> dict[str, float]:
        q = set(_tokens(task))
        out: dict[str, float] = {}
        for n in snapshot.nodes:
            pt = set(_tokens(n.path.replace("/", " ").replace("_", " ")))
            st = set(_tokens(" ".join(n.symbols)))
            sm = set(_tokens(n.summary))
            score = len(q & pt) * 3.4 + len(q & st) * 2.8 + len(q & sm) * 1.0
            if score:
                out[n.path] = score
        return out

    def _semantic(self, task: str, snapshot: WorldSnapshot) -> dict[str, float]:
        qv = self.embedder.encode(task)
        out: dict[str, float] = {}
        for n in snapshot.nodes:
            doc = f"{n.path} {' '.join(n.symbols)} {n.summary} {n.component} {n.kind}"
            out[n.path] = _cosine(qv, self.embedder.encode(doc))
        return out

    def _bm25(self, task: str, docs: dict[str, str]) -> dict[str, float]:
        query = list(dict.fromkeys(_tokens(task)))
        if not query or not docs:
            return {}
        toks = {p: _tokens(text) for p, text in docs.items()}
        lengths = {p: len(ts) for p, ts in toks.items()}
        avgdl = sum(lengths.values()) / max(1, len(lengths))
        df: Counter[str] = Counter()
        for ts in toks.values():
            present = set(ts)
            for term in query:
                if term in present:
                    df[term] += 1
        n_docs = len(toks)
        k1, b = 1.5, 0.75
        out: dict[str, float] = {}
        for path, ts in toks.items():
            tf = Counter(ts)
            dl = max(1, lengths[path])
            score = 0.0
            for term in query:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                denom = freq + k1 * (1.0 - b + b * dl / max(1.0, avgdl))
                score += idf * (freq * (k1 + 1.0) / denom)
            if score > 0:
                out[path] = score
        return out

    def _graph_expand(self, snapshot: WorldSnapshot, seeds: list[str], depth: int = 2) -> dict[str, float]:
        adj: dict[str, list[str]] = defaultdict(list)
        for e in snapshot.edges:
            adj[e.source].append(e.target)
            adj[e.target].append(e.source)
        scores: dict[str, float] = {}
        queue = deque((s, 0) for s in seeds)
        seen = set(seeds)
        while queue:
            path, dist = queue.popleft()
            if dist >= depth:
                continue
            for nxt in adj.get(path, []):
                nd = dist + 1
                scores[nxt] = max(scores.get(nxt, 0.0), 1.0 / (1.0 + nd))
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, nd))
        return scores

    @staticmethod
    def _pack(candidates: list[AttentionCandidate], file_budget_tokens: int, max_files: int) -> list[AttentionCandidate]:
        # Required primary/include items come first, then uncertain candidates only while budget remains.
        ordered = sorted(candidates, key=lambda c: (
            0 if c.role == "primary" else 1 if c.gate == "include" else 2 if c.gate == "consider" else 3,
            -c.score, c.path,
        ))
        out: list[AttentionCandidate] = []
        used = 0
        for c in ordered:
            if len(out) >= max_files or c.gate == "drop":
                continue
            cost = max(12, c.estimated_tokens)
            if out and used + cost > file_budget_tokens:
                continue
            if not out and cost > file_budget_tokens:
                # Keep one primary candidate but crop it so a too-large file cannot starve the packet.
                target_chars = max(400, file_budget_tokens * 4)
                c.excerpt = c.excerpt[:target_chars]
                c.evidence = [c.excerpt] if c.excerpt else []
                c.estimated_tokens = _estimate_tokens(c.excerpt)
                cost = c.estimated_tokens
            out.append(c)
            used += cost
        return out
