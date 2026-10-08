"""Persistent local Run journal and background Codex process owner."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from galaxy_core.projects import ProjectRegistry
from galaxy_core.runs.codex_cli import CodexRunProvider, ProviderEvent, RunRequest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_message(value: str) -> str:
    value = re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b(\s*[:=]\s*)[^\s,;]+",
                   r"\1\2[скрыто]", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [скрыто]", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
                   "[скрыто]", value)
    value = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                   "[закрытый ключ удалён]", value, flags=re.DOTALL)
    return value[:50000]


def _redact_payload(value):
    if isinstance(value, str):
        return _redact_message(value)
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


class RunManager:
    def __init__(self, registry: ProjectRegistry, provider: CodexRunProvider | None = None):
        self.registry = registry
        self.path = registry.data_dir / "runs.sqlite3"
        self.provider = provider or CodexRunProvider()
        self._lock = threading.RLock()
        self._active: dict[str, object] = {}
        self._cancel_requested: set[str] = set()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence)
                );
            """)
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        with self._connect() as db:
            rows = db.execute("SELECT id, task_id FROM runs WHERE status='running'").fetchall()
        for row in rows:
            self._append(row["id"], "run.failed", {"code": "service_restarted"})
            with self._connect() as db:
                db.execute("UPDATE runs SET status='failed', updated_at=? WHERE id=?", (_now(), row["id"]))
            try:
                self.registry.update_task(row["task_id"], "failed", row["id"])
            except KeyError:
                pass

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, project_id: str, task_id: str) -> dict:
        with self._lock:
            project = self.registry.get(project_id)
            selected_provider = project.settings.get("default_provider", "codex")
            if selected_provider != "codex":
                raise ValueError("Для этого этапа запуск доступен только через Codex.")
            task = self.registry.get_task(task_id)
            if task.project_id != project_id:
                raise ValueError("Задача не относится к выбранному проекту.")
            if task.status == "running":
                raise ValueError("Эта задача уже выполняется.")
            run_id = str(uuid.uuid4())
            timestamp = _now()
            with self._connect() as db:
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                           (run_id, project_id, task_id, "running", timestamp, timestamp))
            self.registry.update_task(task_id, "running", run_id)
            self._append(run_id, "run.started", {"project_id": project_id, "task_id": task_id})
            thread = threading.Thread(target=self._execute, args=(run_id, project.path, task.request), daemon=True)
            thread.start()
            return self.get(run_id)

    def _execute(self, run_id: str, project_path: str, prompt: str) -> None:
        process = None
        try:
            with self._lock:
                if run_id in self._cancel_requested:
                    self._cancel_requested.discard(run_id)
                    self._finish(run_id, "cancelled")
                    return
            if not self.provider.available():
                raise RuntimeError("Codex CLI не установлен или не найден.")
            self._append(run_id, "provider.started", {"provider": "codex"})
            process = self.provider.start(RunRequest(prompt=prompt, workspace=Path(project_path)))
            with self._lock:
                self._active[run_id] = process
                cancel_now = run_id in self._cancel_requested
            if cancel_now:
                process.cancel()
            status = "completed"
            final_payload = {}
            for event in process.events():
                self._record_provider_event(run_id, event)
                if event.kind == "error":
                    status = "failed"
                    final_payload = {"code": event.error_code or "provider_failed"}
                    break
                if event.kind == "cancelled":
                    status = "cancelled"
                    break
            self._finish(run_id, status, final_payload)
        except Exception as exc:
            if process is not None:
                process.cancel()
            message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "Не удалось выполнить задачу."
            self._finish(run_id, "failed", {"message": _redact_message(message)})
        finally:
            with self._lock:
                self._active.pop(run_id, None)
                self._cancel_requested.discard(run_id)

    def _record_provider_event(self, run_id: str, event: ProviderEvent) -> None:
        if event.kind == "message":
            self._append(run_id, "provider.message", {"text": _redact_message(event.message or "")})
        elif event.kind == "result":
            self._append(run_id, "provider.result", {
                "result": _redact_payload(event.result or {}), "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
            })
        elif event.kind == "error":
            self._append(run_id, "provider.error", {"code": event.error_code or "provider_failed"})

    def cancel(self, run_id: str) -> dict:
        run = self.get(run_id)
        if run["status"] != "running":
            raise ValueError("Выполнение уже завершено.")
        with self._lock:
            process = self._active.get(run_id)
        if process is None:
            with self._lock:
                self._cancel_requested.add(run_id)
            return {"run_id": run_id, "status": "cancelling"}
        process.cancel()
        return {"run_id": run_id, "status": "cancelling"}

    def _append(self, run_id: str, event_type: str, payload: dict) -> None:
        with self._connect() as db:
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM run_events WHERE run_id=?",
                                  (run_id,)).fetchone()[0]
            db.execute("INSERT INTO run_events VALUES(?,?,?,?,?)", (
                run_id, sequence, _now(), event_type,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ))
            db.execute("UPDATE runs SET updated_at=? WHERE id=?", (_now(), run_id))

    def _finish(self, run_id: str, status: str, payload: dict | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, _now(), run_id))
        run = self.get(run_id)
        self.registry.update_task(run["task_id"], status, run_id)
        self._append(run_id, f"run.{status}", payload or {})

    def get(self, run_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return dict(row)

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError(run_id)
            rows = db.execute("SELECT * FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence",
                              (run_id, after)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]
