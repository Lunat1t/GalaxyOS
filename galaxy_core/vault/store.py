"""SQLite Storage Engine and FTS5 Search for Galaxy Open Vault."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from galaxy_core.vault.models import GraphData, Link, Note, SearchResult


class VaultStore:
    """High-performance SQLite engine for notes, links, backlinks and FTS5 search."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    from contextlib import contextmanager

    @contextmanager
    def session(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    path TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    frontmatter_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    indexed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    alias TEXT,
                    heading TEXT,
                    context_snippet TEXT,
                    FOREIGN KEY(source_id) REFERENCES notes(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_id);
                CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_id);

                -- FTS5 Full-Text Search Virtual Table
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    id UNINDEXED,
                    title,
                    content,
                    tags,
                    tokenize = 'porter unicode61'
                );
                """
            )

    def upsert_note(self, note: Note) -> None:
        """Insert or update a note, its links, and FTS5 search index."""
        with self.session() as conn:
            now = time.time()
            # 1. Update notes table
            conn.execute(
                """
                INSERT INTO notes (id, path, title, frontmatter_json, tags_json, content_hash, mtime, size_bytes, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path = excluded.path,
                    title = excluded.title,
                    frontmatter_json = excluded.frontmatter_json,
                    tags_json = excluded.tags_json,
                    content_hash = excluded.content_hash,
                    mtime = excluded.mtime,
                    size_bytes = excluded.size_bytes,
                    indexed_at = excluded.indexed_at
                """,
                (
                    note.id,
                    note.path,
                    note.title,
                    json.dumps(note.frontmatter, ensure_ascii=False, default=str),
                    json.dumps(note.tags, ensure_ascii=False, default=str),
                    note.content_hash,
                    note.mtime,
                    note.size_bytes,
                    now,
                ),
            )

            # 2. Update links (remove old, insert new)
            conn.execute("DELETE FROM links WHERE source_id = ?", (note.id,))
            for link in note.outgoing_links:
                conn.execute(
                    """
                    INSERT INTO links (source_id, target_id, link_type, alias, heading, context_snippet)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        link.source_id,
                        link.target_id,
                        link.link_type,
                        link.alias,
                        link.heading,
                        link.context_snippet,
                    ),
                )

            # 3. Update FTS5 index
            conn.execute("DELETE FROM notes_fts WHERE id = ?", (note.id,))
            tags_str = " ".join(f"#{t}" for t in note.tags)
            conn.execute(
                """
                INSERT INTO notes_fts (id, title, content, tags)
                VALUES (?, ?, ?, ?)
                """,
                (note.id, note.title, note.content, tags_str),
            )

    def delete_note(self, note_id: str) -> None:
        """Remove note, its outgoing links, and FTS5 record."""
        with self.session() as conn:
            conn.execute("DELETE FROM links WHERE source_id = ?", (note_id,))
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))

    def get_note_meta(self, note_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "path": row["path"],
                "title": row["title"],
                "frontmatter": json.loads(row["frontmatter_json"]),
                "tags": json.loads(row["tags_json"]),
                "content_hash": row["content_hash"],
                "mtime": row["mtime"],
                "size_bytes": row["size_bytes"],
            }

    def get_backlinks(self, note_id: str) -> list[Link]:
        """Find all notes that link to this note_id via [[note_id]]."""
        with self.session() as conn:
            # Match exact or with subpath
            cursor = conn.execute(
                """
                SELECT source_id, target_id, link_type, alias, heading, context_snippet
                FROM links
                WHERE target_id = ? OR target_id LIKE ?
                """,
                (note_id, f"{note_id}/%"),
            )
            links: list[Link] = []
            for row in cursor.fetchall():
                links.append(
                    Link(
                        source_id=row["source_id"],
                        target_id=row["target_id"],
                        link_type=row["link_type"],
                        alias=row["alias"],
                        heading=row["heading"],
                        context_snippet=row["context_snippet"],
                    )
                )
            return links

    def get_graph(self) -> GraphData:
        """Construct knowledge graph topology: all notes as nodes and links as edges."""
        with self.session() as conn:
            nodes_cursor = conn.execute("SELECT id, title, tags_json, size_bytes FROM notes")
            nodes: list[dict[str, Any]] = []
            known_ids: set[str] = set()

            for row in nodes_cursor.fetchall():
                known_ids.add(row["id"])
                nodes.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "tags": json.loads(row["tags_json"]),
                        "size": row["size_bytes"],
                    }
                )

            links_cursor = conn.execute("SELECT source_id, target_id, link_type FROM links")
            edges: list[dict[str, Any]] = []

            for row in links_cursor.fetchall():
                target = row["target_id"]
                # Resolve partial target if needed (e.g. note name vs folder/note)
                edges.append(
                    {
                        "source": row["source_id"],
                        "target": target,
                        "type": row["link_type"],
                        "target_exists": target in known_ids,
                    }
                )

            return GraphData(nodes=nodes, edges=edges)

    def search_fts(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Full-text search using SQLite FTS5 with BM25 ranking."""
        clean_query = query.strip()
        if not clean_query:
            return []

        # Sanitize query for FTS5 (escape special chars, match prefix)
        escaped_words = []
        for word in clean_query.split():
            clean_word = "".join(c for c in word if c.isalnum() or c in "_-")
            if clean_word:
                escaped_words.append(f'"{clean_word}"*')

        if not escaped_words:
            return []

        fts_query = " AND ".join(escaped_words)

        with self.session() as conn:
            try:
                cursor = conn.execute(
                    """
                    SELECT
                        notes.id,
                        notes.path,
                        notes.title,
                        notes.tags_json,
                        snippet(notes_fts, 2, '<b>', '</b>', '...', 15) as snippet,
                        bm25(notes_fts) as rank
                    FROM notes_fts
                    JOIN notes ON notes.id = notes_fts.id
                    WHERE notes_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                )
            except sqlite3.OperationalError:
                # Fallback if FTS5 syntax error
                return []

            results: list[SearchResult] = []
            for row in cursor.fetchall():
                results.append(
                    SearchResult(
                        note_id=row["id"],
                        path=row["path"],
                        title=row["title"],
                        snippet=row["snippet"] or "",
                        score=round(float(-row["rank"]), 4),
                        tags=json.loads(row["tags_json"]),
                    )
                )
            return results

    def list_notes(self, tag: str | None = None) -> list[dict[str, Any]]:
        with self.session() as conn:
            if tag:
                clean_tag = tag.lstrip("#")
                cursor = conn.execute(
                    """
                    SELECT id, path, title, frontmatter_json, tags_json, size_bytes, mtime
                    FROM notes
                    WHERE tags_json LIKE ?
                    ORDER BY path ASC
                    """,
                    (f'%"{clean_tag}"%',),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, path, title, frontmatter_json, tags_json, size_bytes, mtime FROM notes ORDER BY path ASC"
                )

            notes: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                notes.append(
                    {
                        "id": row["id"],
                        "path": row["path"],
                        "title": row["title"],
                        "frontmatter": json.loads(row["frontmatter_json"]),
                        "tags": json.loads(row["tags_json"]),
                        "size_bytes": row["size_bytes"],
                        "mtime": row["mtime"],
                    }
                )
            return notes

    def get_stats(self) -> dict[str, Any]:
        with self.session() as conn:
            notes_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            return {
                "total_notes": notes_count,
                "total_links": links_count,
                "db_size_bytes": db_size,
                "db_path": str(self.db_path),
            }
