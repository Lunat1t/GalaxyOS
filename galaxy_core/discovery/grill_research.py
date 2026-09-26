"""Read-only research and provenance adapters for Galaxy 3.1 grill-v1.

Provides safe, local-first inspection of repository code, documentation,
Second Brain and local Vault notes with verified provenance retention and
automatic secret redaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from galaxy_core.discovery.grill_models import GrillFact, utcnow
from galaxy_core.discovery.grill_privacy import redact_secrets


IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".pytest_cache", ".venv", "venv", ".idea", ".vscode", "dist", "build",
}

TEXT_EXTENSIONS = {
    ".py", ".md", ".json", ".yaml", ".yml", ".txt", ".sh", ".toml",
    ".rst", ".cfg", ".ini", ".sql", ".html", ".css", ".js", ".ts",
}


@dataclass
class ResearchFinding:
    fact: str
    source_type: str
    source_ref: str
    confidence: float = 1.0
    snippet: str = ""

    def to_grill_fact(self, session_id: str, node_id: str | None = None) -> GrillFact:
        return GrillFact(
            session_id=session_id,
            fact=redact_secrets(self.fact),
            node_id=node_id,
            source_type=self.source_type,
            source_ref=redact_secrets(self.source_ref),
            confidence=self.confidence,
            verified_at=utcnow(),
            actor="research_adapter",
        )


class LocalResearchAdapter:
    """Read-only research adapter for local repository, Second Brain, and local Vault."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def search_local_repository(
        self, query: str, max_results: int = 5
    ) -> list[ResearchFinding]:
        """Search local repository text files for relevant keywords read-only."""
        if not query or not query.strip():
            return []

        words = [re.escape(w.lower()) for w in re.findall(r"[\w-]+", query) if len(w) > 2]
        if not words:
            return []

        pattern = re.compile(r"(" + "|".join(words) + r")", flags=re.IGNORECASE)
        findings: list[ResearchFinding] = []

        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in IGNORE_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            lines = content.splitlines()
            for line_no, line in enumerate(lines, 1):
                if pattern.search(line):
                    rel_path = str(path.relative_to(self.root))
                    snippet = line.strip()[:200]
                    fact_text = f"В файле {rel_path}:{line_no} найдено: {snippet}"
                    findings.append(
                        ResearchFinding(
                            fact=fact_text,
                            source_type="local_repo",
                            source_ref=f"{rel_path}:{line_no}",
                            confidence=1.0,
                            snippet=snippet,
                        )
                    )
                    if len(findings) >= max_results:
                        return findings

        return findings

    def search_second_brain(
        self, query: str, project: str | None = None, limit: int = 5
    ) -> list[ResearchFinding]:
        """Search Galaxy Second Brain memories read-only."""
        findings: list[ResearchFinding] = []
        try:
            from galaxy_core.brain.store import BrainStore

            brain = BrainStore(self.root)
            memories = brain.search(query, project=project, limit=limit, include_global=True)
            for mem in memories:
                summary = mem.get("summary") or mem.get("title") or ""
                uid = mem.get("id") or mem.get("uid") or "unknown"
                fact_text = f"Память Brain [{mem.get('kind', 'memory')}]: {summary}"
                findings.append(
                    ResearchFinding(
                        fact=fact_text,
                        source_type="second_brain",
                        source_ref=f"brain:{uid}",
                        confidence=float(mem.get("confidence", 0.9)),
                        snippet=summary[:200],
                    )
                )
        except Exception:
            # Brain store might be uninitialized or empty
            pass

        return findings

    def search_vault(
        self, query: str, max_results: int = 5
    ) -> list[ResearchFinding]:
        """Search local Vault notes read-only."""
        findings: list[ResearchFinding] = []
        vault_candidates = [
            self.root / "data" / "vault",
            self.root / "vault",
        ]

        words = [re.escape(w.lower()) for w in re.findall(r"[\w-]+", query) if len(w) > 2]
        if not words:
            return []
        pattern = re.compile(r"(" + "|".join(words) + r")", flags=re.IGNORECASE)

        for vault_dir in vault_candidates:
            if not vault_dir.is_dir():
                continue
            for md_path in vault_dir.rglob("*.md"):
                if not md_path.is_file():
                    continue
                try:
                    content = md_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                for line_no, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        snippet = line.strip()[:200]
                        rel_path = str(md_path.relative_to(vault_dir))
                        fact_text = f"Заметка Vault [{rel_path}:{line_no}]: {snippet}"
                        findings.append(
                            ResearchFinding(
                                fact=fact_text,
                                source_type="vault",
                                source_ref=f"vault:{rel_path}:{line_no}",
                                confidence=0.95,
                                snippet=snippet,
                            )
                        )
                        if len(findings) >= max_results:
                            return findings

        return findings

    def research_topic(
        self,
        query: str,
        session_id: str,
        node_id: str | None = None,
        project: str | None = None,
    ) -> list[GrillFact]:
        """Collect verified facts from all read-only local sources with provenance."""
        findings: list[ResearchFinding] = []
        findings.extend(self.search_local_repository(query, max_results=3))
        findings.extend(self.search_second_brain(query, project=project, limit=3))
        findings.extend(self.search_vault(query, max_results=3))

        return [f.to_grill_fact(session_id, node_id) for f in findings]
