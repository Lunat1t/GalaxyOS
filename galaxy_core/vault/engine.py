"""Unified High-Level Vault Engine for Galaxy Knowledge File System."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from galaxy_core.vault.models import Note
from galaxy_core.vault.parser import parse_note
from galaxy_core.vault.store import VaultStore


IGNORE_DIRS = {".git", ".obsidian", ".galaxy", "node_modules", ".venv", "__pycache__"}


class VaultEngine:
    """Master controller for knowledge vault: incremental sync, file I/O, graph and search."""

    def __init__(self, vault_root: str | Path, db_path: str | Path | None = None):
        self.vault_root = Path(vault_root).resolve()
        self.vault_root.mkdir(parents=True, exist_ok=True)

        if db_path:
            self.db_path = Path(db_path).resolve()
        else:
            runtime_dir = self.vault_root.parent / "runtime"
            if runtime_dir.exists() or self.vault_root.name == "vault":
                runtime_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = runtime_dir / "vault.db"
            else:
                self.db_path = self.vault_root / ".galaxy" / "vault.db"

        self.store = VaultStore(self.db_path)

    def _normalize_id(self, path_or_id: str) -> tuple[str, Path]:
        clean = path_or_id.replace("\\", "/").strip().lstrip("/")
        if clean.endswith(".md"):
            rel_path = clean
            note_id = clean[:-3]
        else:
            note_id = clean
            rel_path = f"{clean}.md"
        abs_path = (self.vault_root / rel_path).resolve()
        return note_id, abs_path

    def sync(self, force: bool = False) -> dict[str, int]:
        """Perform fast incremental synchronization between Markdown files on disk and SQLite."""
        indexed = 0
        deleted = 0
        skipped = 0

        existing_disk_ids: set[str] = set()

        for root, dirs, files in os.walk(self.vault_root):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
            for file in files:
                if not file.endswith(".md"):
                    continue

                abs_file = Path(root) / file
                rel_path = abs_file.relative_to(self.vault_root).as_posix()
                note_id = rel_path[:-3]
                existing_disk_ids.add(note_id)

                # Check if file changed
                meta = self.store.get_note_meta(note_id)
                mtime = abs_file.stat().st_mtime

                if not force and meta and abs(meta["mtime"] - mtime) < 0.001:
                    skipped += 1
                    continue

                # Parse and upsert
                note = parse_note(abs_file, self.vault_root)
                self.store.upsert_note(note)
                indexed += 1

        # Remove deleted files from store
        stored_notes = self.store.list_notes()
        for stored in stored_notes:
            if stored["id"] not in existing_disk_ids:
                self.store.delete_note(stored["id"])
                deleted += 1

        return {
            "indexed": indexed,
            "skipped": skipped,
            "deleted": deleted,
            "total_active": len(existing_disk_ids),
        }

    def read_note(self, path_or_id: str) -> dict[str, Any] | None:
        """Read a note from disk and enrich it with computed backlinks and graph metadata."""
        note_id, abs_path = self._normalize_id(path_or_id)
        if not abs_path.is_file():
            return None

        note = parse_note(abs_path, self.vault_root)
        backlinks = self.store.get_backlinks(note.id)

        data = note.to_dict(include_content=True)
        data["backlinks"] = [bl.to_dict() for bl in backlinks]
        return data

    def write_note(
        self,
        path_or_id: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Atomically write a note to disk, update frontmatter, and refresh index."""
        note_id, abs_path = self._normalize_id(path_or_id)
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        fm = dict(frontmatter or {})
        if title:
            fm["title"] = title

        # Build clean Markdown output
        file_lines: list[str] = []
        if fm:
            file_lines.append("---")
            for k, v in fm.items():
                if isinstance(v, list):
                    file_lines.append(f"{k}: [{', '.join(str(item) for item in v)}]")
                else:
                    file_lines.append(f"{k}: {v}")
            file_lines.append("---")
            file_lines.append("")

        file_lines.append(content.strip())
        full_text = "\n".join(file_lines) + "\n"

        # Atomic write
        tmp_path = abs_path.with_suffix(".tmp")
        tmp_path.write_text(full_text, encoding="utf-8")
        tmp_path.replace(abs_path)

        # Update index immediately
        note = parse_note(abs_path, self.vault_root)
        self.store.upsert_note(note)

        backlinks = self.store.get_backlinks(note.id)
        result = note.to_dict(include_content=True)
        result["backlinks"] = [bl.to_dict() for bl in backlinks]
        return result

    def delete_note(self, path_or_id: str) -> bool:
        """Safely delete a note from disk and remove all its connections from index."""
        note_id, abs_path = self._normalize_id(path_or_id)
        if not abs_path.is_file():
            return False

        abs_path.unlink()
        self.store.delete_note(note_id)
        return True

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search across full text and tags."""
        results = self.store.search_fts(query, limit=limit)
        return [r.to_dict() for r in results]

    def get_graph(self) -> dict[str, Any]:
        """Return interactive knowledge graph (nodes & edges)."""
        return self.store.get_graph().to_dict()

    def list_notes(self, tag: str | None = None) -> list[dict[str, Any]]:
        """List summary of notes, optionally filtered by tag."""
        return self.store.list_notes(tag=tag)

    def status(self) -> dict[str, Any]:
        """Return vault health and statistics."""
        stats = self.store.get_stats()
        stats["vault_root"] = str(self.vault_root)
        return stats
