"""Persistent registry of local Galaxy projects."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

PROJECT_SETTINGS = {
    "default_agent_id", "default_provider", "default_model", "reasoning_effort",
    "model_profile", "skills", "mcp_servers", "permissions",
}
PROVIDERS = {"codex", "openrouter", "agy", "claude-code"}
PERMISSION_VALUES = {"allow", "ask", "deny"}


def user_data_dir() -> Path:
    """Return the per-user data directory without tying it to the install path."""
    override = os.environ.get("GALAXY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Galaxy"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Galaxy"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base.expanduser() / "galaxy"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: str
    created_at: str
    last_opened_at: str | None
    settings: dict
    is_active: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectTask:
    id: str
    project_id: str
    request: str
    status: str
    created_at: str
    updated_at: str
    latest_run_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ProjectRegistry:
    """Store project locations and recent/active selection in user data."""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else user_data_dir()
        self.data_dir = self.data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "projects.sqlite3"
        self._init_schema()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    settings_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS registry_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    request TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    latest_run_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_project_tasks_recent
                    ON project_tasks(project_id, created_at DESC);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(project_tasks)")}
            if "latest_run_id" not in columns:
                db.execute("ALTER TABLE project_tasks ADD COLUMN latest_run_id TEXT")

    @staticmethod
    def _canonical_path(path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Project directory does not exist: {candidate}") from exc
        if not resolved.is_dir():
            raise ValueError(f"Project path is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _project(row: sqlite3.Row, active_id: str | None = None) -> Project:
        return Project(
            id=row["id"], name=row["name"], path=row["path"],
            created_at=row["created_at"], last_opened_at=row["last_opened_at"],
            settings=json.loads(row["settings_json"]), is_active=row["id"] == active_id,
        )

    def add(self, path: str | Path = ".", name: str | None = None) -> Project:
        directory = self._canonical_path(path)
        timestamp = _now()
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE path=?", (str(directory),)).fetchone()
            if row:
                if name and name.strip() and name.strip() != row["name"]:
                    db.execute("UPDATE projects SET name=? WHERE id=?", (name.strip(), row["id"]))
                    row = db.execute("SELECT * FROM projects WHERE id=?", (row["id"],)).fetchone()
                active = db.execute("SELECT value FROM registry_state WHERE key='active_project'").fetchone()
                return self._project(row, active["value"] if active else None)
            project_id = str(uuid.uuid4())
            project_name = name.strip() if name and name.strip() else directory.name or str(directory)
            db.execute("INSERT INTO projects(id,name,path,created_at) VALUES(?,?,?,?)",
                       (project_id, project_name, str(directory), timestamp))
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            active = db.execute("SELECT value FROM registry_state WHERE key='active_project'").fetchone()
            return self._project(row, active["value"] if active else None)

    def open(self, path_or_id: str | Path = ".") -> Project:
        raw = str(path_or_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (raw,)).fetchone()
            if row:
                directory = Path(row["path"])
                if not directory.is_dir():
                    raise ValueError(f"Saved project directory is unavailable: {directory}")
                project_id = row["id"]
            else:
                directory = self._canonical_path(path_or_id)
                row = db.execute("SELECT * FROM projects WHERE path=?", (str(directory),)).fetchone()
                if not row:
                    project_id = str(uuid.uuid4())
                    db.execute("INSERT INTO projects(id,name,path,created_at) VALUES(?,?,?,?)",
                               (project_id, directory.name or str(directory), str(directory), _now()))
                else:
                    project_id = row["id"]
            now = _now()
            db.execute("UPDATE projects SET last_opened_at=? WHERE id=?", (now, project_id))
            db.execute("INSERT INTO registry_state(key,value) VALUES('active_project',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (project_id,))
            result = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return self._project(result, project_id)

    def list_recent(self, limit: int = 20) -> list[Project]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            active = db.execute("SELECT value FROM registry_state WHERE key='active_project'").fetchone()
            active_id = active["value"] if active else None
            rows = db.execute("""
                SELECT * FROM projects
                ORDER BY (last_opened_at IS NULL), last_opened_at DESC, created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [self._project(row, active_id) for row in rows]

    def current(self) -> Project | None:
        with self._connect() as db:
            active = db.execute("SELECT value FROM registry_state WHERE key='active_project'").fetchone()
            if not active:
                return None
            row = db.execute("SELECT * FROM projects WHERE id=?", (active["value"],)).fetchone()
            return self._project(row, active["value"]) if row else None

    def get(self, project_id_or_path: str | Path) -> Project:
        value = str(project_id_or_path)
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (value,)).fetchone()
            if not row:
                directory = self._canonical_path(project_id_or_path)
                row = db.execute("SELECT * FROM projects WHERE path=?", (str(directory),)).fetchone()
            if not row:
                raise KeyError(value)
            active = db.execute("SELECT value FROM registry_state WHERE key='active_project'").fetchone()
            return self._project(row, active["value"] if active else None)

    def save_settings(self, project_id: str, settings: dict) -> Project:
        self._validate_settings(settings)
        encoded = json.dumps(settings, ensure_ascii=False, sort_keys=True)
        with self._connect() as db:
            cursor = db.execute("UPDATE projects SET settings_json=? WHERE id=?", (encoded, project_id))
            if not cursor.rowcount:
                raise KeyError(project_id)
            active = db.execute("SELECT value FROM registry_state WHERE key='active_project'").fetchone()
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return self._project(row, active["value"] if active else None)

    def update_settings(self, project_id: str, updates: dict) -> Project:
        current = self.get(project_id)
        if not isinstance(updates, dict):
            raise ValueError("settings update must be a JSON object")
        return self.save_settings(project_id, {**current.settings, **updates})

    def create_task(self, project_id: str, request: str) -> ProjectTask:
        request = request.strip()
        if not request:
            raise ValueError("task request cannot be empty")
        task = ProjectTask(str(uuid.uuid4()), project_id, request, "queued", _now(), _now())
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            db.execute("INSERT INTO project_tasks(id,project_id,request,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (
                task.id, task.project_id, task.request, task.status,
                task.created_at, task.updated_at,
            ))
        return task

    def update_task(self, task_id: str, status: str, run_id: str | None = None) -> ProjectTask:
        if status not in {"queued", "running", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported task status")
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE project_tasks SET status=?, updated_at=?, latest_run_id=COALESCE(?,latest_run_id) WHERE id=?",
                (status, _now(), run_id, task_id),
            )
            if not cursor.rowcount:
                raise KeyError(task_id)
            return ProjectTask(**dict(db.execute("SELECT * FROM project_tasks WHERE id=?", (task_id,)).fetchone()))

    def list_tasks(self, project_id: str, limit: int = 20) -> list[ProjectTask]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM project_tasks WHERE project_id=?
                ORDER BY created_at DESC LIMIT ?
            """, (project_id, limit)).fetchall()
            return [ProjectTask(**dict(row)) for row in rows]

    def get_task(self, task_id: str) -> ProjectTask:
        with self._connect() as db:
            row = db.execute("SELECT * FROM project_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return ProjectTask(**dict(row))

    @staticmethod
    def _validate_settings(settings: dict) -> None:
        if not isinstance(settings, dict):
            raise ValueError("project settings must be a JSON object")
        unknown = set(settings) - PROJECT_SETTINGS
        if unknown:
            raise ValueError(f"unsupported project settings: {', '.join(sorted(unknown))}")
        for key in ("default_agent_id", "model_profile"):
            if key in settings and not isinstance(settings[key], str):
                raise ValueError(f"{key} must be a string")
        if "default_model" in settings:
            model = settings["default_model"]
            if not isinstance(model, str) or len(model) > 200 or any(char.isspace() for char in model):
                raise ValueError("default_model must be a model ID without whitespace (up to 200 characters)")
        if "reasoning_effort" in settings:
            effort = settings["reasoning_effort"]
            if not isinstance(effort, str) or effort not in {"low", "medium", "high"}:
                raise ValueError("reasoning_effort must be low, medium, or high")
        if "default_provider" in settings:
            provider = settings["default_provider"]
            if not isinstance(provider, str) or provider not in PROVIDERS:
                raise ValueError(f"default_provider must be one of: {', '.join(sorted(PROVIDERS))}")
        for key in ("skills", "mcp_servers"):
            if key in settings and (not isinstance(settings[key], list) or
                                    any(not isinstance(item, str) or not item.strip()
                                        for item in settings[key])):
                raise ValueError(f"{key} must be a list of non-empty IDs")
        if "permissions" in settings:
            permissions = settings["permissions"]
            if not isinstance(permissions, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str) or value not in PERMISSION_VALUES
                    for key, value in permissions.items()):
                raise ValueError("permissions must map capability IDs to allow, ask, or deny")
