"""Evidence-driven living-memory reconciliation.

The reconciler detects stale sources and competing project assertions. By default it is
read-only. `apply=True` mutates only findings with a deterministic/sufficiently strong rule.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
import hashlib
from pathlib import Path
import re
from typing import Any

from .store import BrainStore

WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)
ASSERTIVE_KINDS = {"fact", "decision", "constraint", "preference", "procedure"}


@dataclass
class MemoryFinding:
    type: str
    uid: str
    severity: str
    suggested_state: str | None
    evidence: str
    related_uid: str | None = None
    confidence: float = 1.0
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryReconciler:
    def __init__(self, brain: BrainStore, project_root: str | Path | None = None):
        self.brain = brain
        self.project_root = Path(project_root or brain.root).resolve()

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {x.lower() for x in WORD.findall(text or "")}

    @staticmethod
    def _dt(value: str | None) -> dt.datetime:
        try:
            return dt.datetime.fromisoformat(value or "")
        except ValueError:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    def _resolve_source(self, item: dict[str, Any]) -> Path | None:
        ref = str(item.get("source_ref") or "").strip()
        if not ref or ref.startswith(("http://", "https://")):
            return None
        project_root = self.project_root.resolve()
        brain_root = Path(self.brain.root).resolve()
        p = Path(ref)
        candidates = [p.resolve()] if p.is_absolute() else [(project_root / p).resolve(), (brain_root / p).resolve()]
        allowed = [c for c in candidates if c == project_root or c == brain_root or c.is_relative_to(project_root) or c.is_relative_to(brain_root)]
        if not allowed:
            return None
        for candidate in allowed:
            if candidate.exists():
                return candidate
        return allowed[0]

    def scan(self, *, project: str | None = None, apply: bool = False, limit: int = 5000) -> dict[str, Any]:
        memories = [x for x in self.brain.recent(limit=limit, status="active")
                    if project is None or x.get("project") in {project, None, ""}]
        findings: list[MemoryFinding] = []

        # 1) Deterministic source lifecycle checks.
        for item in memories:
            source_type = item.get("source_type") or ""
            ref = item.get("source_ref")
            if not ref or source_type in {"manual", "legacy", "agent_run", "autonomy_run", "correction"}:
                continue
            p = self._resolve_source(item)
            if p is not None and not p.exists():
                f = MemoryFinding("source_missing", item["uid"], "high", "stale", f"source no longer exists: {ref}", confidence=1.0)
                if apply:
                    self.brain.mark_stale(item["uid"], f.evidence); f.applied = True
                findings.append(f)
                continue
            # Full-file content hashes are safe to compare only for repository_file/file sources.
            if p is not None and p.is_file() and source_type in {"repository_file", "file"} and item.get("source_hash"):
                current = hashlib.sha256(p.read_bytes()).hexdigest()
                stored = str(item.get("source_hash") or "")
                # Accept legacy shortened hashes if present.
                same = current == stored or current.startswith(stored) or stored.startswith(current)
                if not same:
                    f = MemoryFinding("source_changed", item["uid"], "high", "stale", f"source content hash changed: {ref}", confidence=1.0)
                    if apply:
                        self.brain.mark_stale(item["uid"], f.evidence); f.applied = True
                    findings.append(f)

        # 2) Competing assertions with the same semantic slot (kind + project + title).
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in memories:
            if item.get("kind") not in ASSERTIVE_KINDS:
                continue
            key = (str(item.get("project") or ""), str(item.get("kind") or ""), " ".join(str(item.get("title") or "").lower().split()))
            groups.setdefault(key, []).append(item)

        for items in groups.values():
            if len(items) < 2:
                continue
            items = sorted(items, key=lambda x: self._dt(x.get("updated_at")), reverse=True)
            winner = items[0]
            wtokens = self._tokens(winner.get("summary") or "")
            for older in items[1:]:
                otokens = self._tokens(older.get("summary") or "")
                union = wtokens | otokens
                similarity = len(wtokens & otokens) / max(1, len(union))
                if similarity >= 0.68:
                    continue
                winner_trust = float(winner.get("confidence") or 0.5) + (0.25 if winner.get("confirmed") or winner.get("verified") else 0.0)
                older_trust = float(older.get("confidence") or 0.5) + (0.25 if older.get("confirmed") or older.get("verified") else 0.0)
                confidence = min(0.98, 0.62 + max(0.0, winner_trust - older_trust) * 0.35 + (1.0 - similarity) * 0.18)
                f = MemoryFinding(
                    "competing_assertion", older["uid"], "medium" if confidence < .85 else "high", "contradicted",
                    f"same {older.get('kind')} slot/title as newer {winner['uid']} but summaries diverge (token similarity={similarity:.2f})",
                    related_uid=winner["uid"], confidence=round(confidence, 4),
                )
                # Applying peer contradictions is intentionally stricter than merely detecting them.
                if apply and confidence >= .88 and winner_trust >= older_trust:
                    self.brain.mark_contradicted(older["uid"], f.evidence, by_uid=winner["uid"]); f.applied = True
                findings.append(f)

        return {
            "project": project,
            "scanned_active_memories": len(memories),
            "findings": [x.to_dict() for x in findings],
            "counts": {
                "total": len(findings),
                "source_missing": sum(x.type == "source_missing" for x in findings),
                "source_changed": sum(x.type == "source_changed" for x in findings),
                "competing_assertion": sum(x.type == "competing_assertion" for x in findings),
                "applied": sum(x.applied for x in findings),
            },
        }
