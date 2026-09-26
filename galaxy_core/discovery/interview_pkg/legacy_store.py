"""Legacy SQLite persistence for interview sessions and turns."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from galaxy_core.discovery.grill_models import utcnow


class InterviewStore:
    """Legacy SQLite store preserved for backward compatibility."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "data" / "runtime" / "interviews.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS interview_sessions(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, initial_idea TEXT NOT NULL,
              depth TEXT NOT NULL, status TEXT NOT NULL, answers_json TEXT NOT NULL,
              assumptions_json TEXT NOT NULL, pending_json TEXT, brief_json TEXT,
              max_questions INTEGER NOT NULL, questions_asked INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interview_turns(
              session_id TEXT NOT NULL, turn_no INTEGER NOT NULL,
              question_json TEXT NOT NULL, answer TEXT NOT NULL,
              quality TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(session_id,turn_no));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def create(self, idea: str, project: str, depth: str, max_questions: int) -> str:
        session_id = "INT-" + uuid.uuid4().hex[:14].upper()
        now = utcnow()
        with self.connect() as db:
            db.execute("INSERT INTO interview_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                session_id, project, idea, depth, "DISCOVERY", "{}", "[]", None, None,
                max_questions, 0, now, now))
        return session_id

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM interview_sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                return None
            turns = [dict(x) for x in db.execute(
                "SELECT * FROM interview_turns WHERE session_id=? ORDER BY turn_no", (session_id,))]
        result = dict(row)
        for source, target, fallback in (
            ("answers_json", "answers", {}), ("assumptions_json", "assumptions", []),
            ("pending_json", "pending", None), ("brief_json", "brief", None),
        ):
            raw = result.pop(source)
            result[target] = json.loads(raw) if raw else fallback
        for turn in turns:
            turn["question"] = json.loads(turn.pop("question_json"))
        result["turns"] = turns
        return result

    def save(self, session: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("""UPDATE interview_sessions SET status=?,answers_json=?,assumptions_json=?,
              pending_json=?,brief_json=?,questions_asked=?,updated_at=? WHERE id=?""", (
                session["status"], json.dumps(session["answers"], ensure_ascii=False),
                json.dumps(session["assumptions"], ensure_ascii=False),
                json.dumps(session["pending"], ensure_ascii=False) if session.get("pending") else None,
                json.dumps(session["brief"], ensure_ascii=False) if session.get("brief") else None,
                session["questions_asked"], utcnow(), session["id"]))

    def add_turn(self, session_id: str, question: dict[str, Any], answer: str, quality: str) -> None:
        with self.connect() as db:
            number = db.execute("SELECT COALESCE(MAX(turn_no),0)+1 FROM interview_turns WHERE session_id=?",
                                (session_id,)).fetchone()[0]
            db.execute("INSERT INTO interview_turns VALUES(?,?,?,?,?,?)", (
                session_id, number, json.dumps(question, ensure_ascii=False), answer, quality, utcnow()))
