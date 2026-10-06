"""Durable, ordered change feed for World Model observers."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


class WorldEventLog:
    def __init__(self, path: Path):
        self.path = path

    def append(self, key: str, kind: str, payload: dict[str, Any]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "event_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO events (event_key, kind, payload) VALUES (?, ?, ?)",
                       (key, kind, json.dumps(payload, ensure_ascii=False)))
            return int(db.execute("SELECT id FROM events WHERE event_key = ?", (key,)).fetchone()[0])

    def read(self, *, after_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id, kind, payload FROM events WHERE id > ? ORDER BY id LIMIT ?",
                              (max(0, after_id), max(0, min(limit, 500)))).fetchall()
        return [{"id": event_id, "kind": kind, **json.loads(payload)} for event_id, kind, payload in rows]
