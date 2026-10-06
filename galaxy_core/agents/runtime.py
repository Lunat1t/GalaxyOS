"""Galaxy 3.1 persistent-agent runtime.

The DAG remains the authority boundary. This module gives every role durable
identity and memory, and lets a planet create a tightly scoped Moon without
granting it the parent's full authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any, Iterable

from ..engine.roles import ROLES


APPROVAL_ONLY_TOOLS = frozenset({
    "delete", "deploy", "email.send", "external.write", "git.push",
    "payment", "production.write", "secrets.write",
})
BLOCK_SCOPES = {"global", "project", "agent", "archival"}
BLOCK_PERMISSIONS = {"r", "rw"}
MOON_KINDS = {"temporary", "persistent"}
MOON_STATUSES = {"RUNNING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"}


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("identifier cannot be empty")
    return slug


@dataclass(frozen=True)
class MoonBudget:
    max_cost_usd: float = 1.0
    max_input_tokens: int = 30_000
    max_output_tokens: int = 10_000
    max_calls: int = 8

    def validate(self) -> None:
        if min(self.max_cost_usd, self.max_input_tokens, self.max_output_tokens,
               self.max_calls) < 0:
            raise ValueError("Moon budget cannot be negative")

    def fits(self, parent: "MoonBudget") -> bool:
        return (self.max_cost_usd <= parent.max_cost_usd and
                self.max_input_tokens <= parent.max_input_tokens and
                self.max_output_tokens <= parent.max_output_tokens and
                self.max_calls <= parent.max_calls)


@dataclass(frozen=True)
class MemoryBlock:
    id: str
    label: str
    scope: str
    value: str
    project: str | None = None
    owner_agent_id: str | None = None
    version: int = 1
    updated_at: str = field(default_factory=utcnow)


@dataclass
class MoonResult:
    status: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    memory_candidates: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float = .5
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MoonResult":
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in known}
        result = cls(**filtered)
        result.validate()
        return result

    def validate(self) -> None:
        if self.status not in {"success", "failure", "blocked"}:
            raise ValueError("Moon result status must be success, failure or blocked")
        if not self.summary.strip():
            raise ValueError("Moon result summary is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if min(self.input_tokens, self.output_tokens, self.cost_usd) < 0:
            raise ValueError("usage cannot be negative")


DEFAULT_AGENTS: dict[str, dict[str, Any]] = {
    "sun": {
        "tools": ["brain.read", "brain.write", "delegate", "plan", "approve.request"],
        "blocks": ["user", "system_rules", "current_goals"], "max_parallel": 7,
    },
    "earth": {
        "tools": ["code.read", "code.write", "files.read", "files.write", "terminal", "tests"],
        "blocks": ["project_architecture", "current_goals"], "max_parallel": 4,
    },
    "venera": {
        "tools": ["brain.read", "files.read", "requirements.validate"],
        "blocks": ["user", "current_goals", "project_architecture"], "max_parallel": 3,
    },
    "ceres": {
        "tools": ["brain.read", "docs.read", "files.read", "web.search"],
        "blocks": ["user", "current_goals", "project_architecture"], "max_parallel": 4,
    },
    "mercury": {
        "tools": ["brain.read", "docs.read", "web.search"],
        "blocks": ["user", "current_goals"], "max_parallel": 4,
    },
    "mars": {
        "tools": ["code.read", "dependency.audit", "security.scan"],
        "blocks": ["project_architecture", "system_rules"], "max_parallel": 3,
    },
    "neptun": {
        "tools": ["code.read", "code.write", "files.read", "tests"],
        "blocks": ["project_architecture", "system_rules"], "max_parallel": 3,
    },
    "moon": {
        "tools": ["code.read", "files.read", "tests"],
        "blocks": ["project_architecture"], "max_parallel": 1,
    },
}


class AgentRegistry:
    """SQLite-backed identities, memory blocks, experience and Moon state."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "data" / "runtime" / "agents.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def session(self):
        """Commit or roll back and always close the SQLite connection."""
        db = self.connect()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _migrate(self) -> None:
        with self.session() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS agents(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
              mission TEXT NOT NULL, parent_id TEXT REFERENCES agents(id),
              kind TEXT NOT NULL DEFAULT 'persistent', depth INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'ACTIVE', model_profile TEXT,
              tools_json TEXT NOT NULL, memory_labels_json TEXT NOT NULL,
              max_parallel INTEGER NOT NULL DEFAULT 1, max_depth INTEGER NOT NULL DEFAULT 2,
              budget_json TEXT NOT NULL, successful_tasks INTEGER NOT NULL DEFAULT 0,
              failed_tasks INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_blocks(
              id TEXT PRIMARY KEY, label TEXT NOT NULL, scope TEXT NOT NULL,
              project TEXT, owner_agent_id TEXT REFERENCES agents(id), value TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
              UNIQUE(label, scope, project, owner_agent_id)
            );
            CREATE TABLE IF NOT EXISTS block_bindings(
              agent_id TEXT NOT NULL REFERENCES agents(id),
              block_id TEXT NOT NULL REFERENCES memory_blocks(id),
              permission TEXT NOT NULL, attached_at TEXT NOT NULL,
              PRIMARY KEY(agent_id, block_id)
            );
            CREATE TABLE IF NOT EXISTS moon_runs(
              id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
              parent_id TEXT NOT NULL REFERENCES agents(id), run_id TEXT,
              mission TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
              budget_json TEXT NOT NULL, usage_json TEXT NOT NULL,
              result_json TEXT, created_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS experiences(
              id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL REFERENCES agents(id),
              project TEXT, run_id TEXT, outcome TEXT NOT NULL, lesson TEXT NOT NULL,
              lesson_key TEXT NOT NULL, confidence REAL NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_moons_parent_status ON moon_runs(parent_id,status);
            CREATE INDEX IF NOT EXISTS idx_experience_key ON experiences(agent_id,lesson_key);
            CREATE TABLE IF NOT EXISTS team_runs(
              id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES agents(id),
              autonomy_run_id TEXT, node_id TEXT, project TEXT, objective TEXT NOT NULL,
              status TEXT NOT NULL, decision_score REAL NOT NULL,
              reason TEXT NOT NULL, plan_json TEXT NOT NULL, result_json TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS team_tasks(
              team_id TEXT NOT NULL REFERENCES team_runs(id), task_id TEXT NOT NULL,
              moon_run_id TEXT, status TEXT NOT NULL, task_json TEXT NOT NULL,
              result_json TEXT, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
              started_at TEXT, finished_at TEXT,
              PRIMARY KEY(team_id,task_id)
            );
            CREATE INDEX IF NOT EXISTS idx_team_status ON team_runs(status,updated_at);
            CREATE INDEX IF NOT EXISTS idx_team_tasks_status ON team_tasks(team_id,status);
            """)

    def bootstrap_defaults(self) -> None:
        for role_name, role_spec in ROLES.items():
            agent_id = role_name.lower()
            defaults = DEFAULT_AGENTS.get(agent_id, {
                "tools": ["brain.read", "files.read"],
                "blocks": ["current_goals"], "max_parallel": 2,
            })
            self.ensure_agent(
                agent_id, role_name, role_name, role_spec.purpose,
                parent_id=None if agent_id == "sun" else "sun",
                tools=defaults["tools"], memory_labels=defaults["blocks"],
                max_parallel=defaults["max_parallel"], max_depth=2,
                budget=MoonBudget(3.0, 80_000, 25_000, 20),
            )
        for label, value in {
            "user": "User-confirmed identity, preferences and communication rules.",
            "system_rules": "Galaxy authority, approval and verification rules.",
            "current_goals": "Active long-term goals selected from Galaxy Brain.",
            "project_architecture": "Confirmed architecture and current project decisions.",
        }.items():
            block = self.ensure_block(label, "global", value)
            for agent in self.list_agents():
                if label in agent["memory_labels"]:
                    self.attach_block(agent["id"], block.id, "rw" if agent["id"] == "sun" else "r")

    def ensure_agent(self, agent_id: str, name: str, role: str, mission: str, *,
                     parent_id: str | None = None, kind: str = "persistent",
                     tools: Iterable[str] = (), memory_labels: Iterable[str] = (),
                     max_parallel: int = 1, max_depth: int = 2,
                     budget: MoonBudget | None = None, model_profile: str | None = None) -> dict[str, Any]:
        agent_id = _slug(agent_id)
        if not mission.strip() or max_parallel < 1 or max_depth < 0:
            raise ValueError("agent requires mission and valid limits")
        budget = budget or MoonBudget()
        budget.validate()
        parent = self.get_agent(parent_id) if parent_id else None
        if parent_id and not parent:
            raise KeyError(f"parent agent not found: {parent_id}")
        depth = int(parent["depth"]) + 1 if parent else 0
        now = utcnow()
        with self.session() as db:
            db.execute("""INSERT OR IGNORE INTO agents
              (id,name,role,mission,parent_id,kind,depth,status,model_profile,tools_json,
               memory_labels_json,max_parallel,max_depth,budget_json,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?,?)""",
              (agent_id, name, role, mission, parent_id, kind, depth, model_profile,
               _json(sorted(set(tools))), _json(sorted(set(memory_labels))), max_parallel,
               max_depth, _json(asdict(budget)), now, now))
        return self.get_agent(agent_id) or {}

    def get_agent(self, agent_id: str | None) -> dict[str, Any] | None:
        if not agent_id:
            return None
        with self.session() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (_slug(agent_id),)).fetchone()
        return self._agent(row) if row else None

    def list_agents(self, *, include_completed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agents" + ("" if include_completed else " WHERE status='ACTIVE'") + " ORDER BY depth,name,id"
        with self.session() as db:
            return [self._agent(row) for row in db.execute(sql)]

    @staticmethod
    def _agent(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tools"] = _load(item.pop("tools_json"), [])
        item["memory_labels"] = _load(item.pop("memory_labels_json"), [])
        item["budget"] = _load(item.pop("budget_json"), {})
        return item

    def ensure_block(self, label: str, scope: str, value: str, *, project: str | None = None,
                     owner_agent_id: str | None = None) -> MemoryBlock:
        if scope not in BLOCK_SCOPES:
            raise ValueError(f"invalid memory scope: {scope}")
        if scope == "project" and not project:
            raise ValueError("project blocks require project")
        if scope == "agent" and not owner_agent_id:
            raise ValueError("agent blocks require owner_agent_id")
        block_id = "BLK-" + uuid.uuid4().hex[:12].upper()
        now = utcnow()
        with self.session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM memory_blocks WHERE label=? AND scope=?
              AND project IS ? AND owner_agent_id IS ?""",
              (_slug(label), scope, project, owner_agent_id)).fetchone()
            if not row:
                db.execute("""INSERT INTO memory_blocks
                  (id,label,scope,project,owner_agent_id,value,version,updated_at)
                  VALUES(?,?,?,?,?,?,1,?)""",
                  (block_id, _slug(label), scope, project, owner_agent_id, value, now))
                row = db.execute("SELECT * FROM memory_blocks WHERE id=?", (block_id,)).fetchone()
        return MemoryBlock(**dict(row))

    def attach_block(self, agent_id: str, block_id: str, permission: str = "r") -> None:
        if permission not in BLOCK_PERMISSIONS:
            raise ValueError("permission must be r or rw")
        if not self.get_agent(agent_id):
            raise KeyError(agent_id)
        with self.session() as db:
            if not db.execute("SELECT 1 FROM memory_blocks WHERE id=?", (block_id,)).fetchone():
                raise KeyError(block_id)
            db.execute("""INSERT INTO block_bindings(agent_id,block_id,permission,attached_at)
              VALUES(?,?,?,?) ON CONFLICT(agent_id,block_id) DO UPDATE SET permission=excluded.permission""",
              (_slug(agent_id), block_id, permission, utcnow()))

    def write_block(self, agent_id: str, block_id: str, value: str, *, expected_version: int) -> MemoryBlock:
        with self.session() as db:
            binding = db.execute("SELECT permission FROM block_bindings WHERE agent_id=? AND block_id=?",
                                 (_slug(agent_id), block_id)).fetchone()
            if not binding or binding["permission"] != "rw":
                raise PermissionError(f"{agent_id} has no write access to {block_id}")
            changed = db.execute("""UPDATE memory_blocks SET value=?,version=version+1,updated_at=?
              WHERE id=? AND version=?""", (value, utcnow(), block_id, expected_version)).rowcount
            if not changed:
                raise RuntimeError("memory block version conflict")
            row = db.execute("SELECT * FROM memory_blocks WHERE id=?", (block_id,)).fetchone()
        return MemoryBlock(**dict(row))

    def context(self, agent_id: str, *, project: str | None = None,
                max_chars: int = 12_000) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        if not agent:
            raise KeyError(agent_id)
        with self.session() as db:
            rows = db.execute("""SELECT b.*,x.permission FROM memory_blocks b
              JOIN block_bindings x ON x.block_id=b.id WHERE x.agent_id=?
              AND (b.project IS NULL OR b.project=?) ORDER BY b.scope,b.label""",
              (_slug(agent_id), project)).fetchall()
        blocks, used = [], 0
        for row in rows:
            item = dict(row)
            size = len(item["value"])
            if blocks and used + size > max_chars:
                continue
            used += size
            blocks.append(item)
        return {
            "identity": {key: agent[key] for key in ("id", "name", "role", "mission", "parent_id", "depth")},
            "tools": agent["tools"], "memory_blocks": blocks,
        }

    def record_experience(self, agent_id: str, outcome: str, lesson: str, *,
                          project: str | None = None, run_id: str | None = None,
                          confidence: float = .7) -> None:
        if not self.get_agent(agent_id):
            raise KeyError(agent_id)
        if not lesson.strip() or not 0 <= confidence <= 1:
            raise ValueError("experience requires a lesson and valid confidence")
        lesson_key = _slug(" ".join(lesson.lower().split())[:96])
        with self.session() as db:
            db.execute("""INSERT INTO experiences
              (agent_id,project,run_id,outcome,lesson,lesson_key,confidence,created_at)
              VALUES(?,?,?,?,?,?,?,?)""", (_slug(agent_id), project, run_id, outcome,
                                             lesson, lesson_key, confidence, utcnow()))

    def consolidate_experience(self, agent_id: str, *, minimum_evidence: int = 3) -> list[dict[str, Any]]:
        with self.session() as db:
            rows = db.execute("""SELECT lesson_key,MAX(lesson) lesson,COUNT(*) evidence,
              AVG(confidence) confidence FROM experiences WHERE agent_id=?
              GROUP BY lesson_key HAVING COUNT(*)>=? ORDER BY evidence DESC""",
              (_slug(agent_id), minimum_evidence)).fetchall()
        return [dict(row) for row in rows]

    def tree(self) -> list[dict[str, Any]]:
        agents = self.list_agents(include_completed=True)
        active_counts: dict[str, int] = {}
        with self.session() as db:
            for row in db.execute("SELECT parent_id,COUNT(*) count FROM moon_runs WHERE status='RUNNING' GROUP BY parent_id"):
                active_counts[row["parent_id"]] = row["count"]
        return [{**agent, "active_moons": active_counts.get(agent["id"], 0)} for agent in agents]


class SubAgentManager:
    """Create and close bounded child agents; never bypass the approval engine."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def spawn(self, parent_id: str, mission: str, *, name: str | None = None,
              kind: str = "temporary", tools: Iterable[str] | None = None,
              budget: MoonBudget | None = None, run_id: str | None = None,
              memory_block_ids: Iterable[str] = ()) -> dict[str, Any]:
        parent = self.registry.get_agent(parent_id)
        if not parent:
            raise KeyError(parent_id)
        if kind not in MOON_KINDS:
            raise ValueError("Moon kind must be temporary or persistent")
        if parent["depth"] >= parent["max_depth"]:
            raise PermissionError("maximum subagent depth reached")
        with self.registry.session() as db:
            running = db.execute("SELECT COUNT(*) count FROM moon_runs WHERE parent_id=? AND status='RUNNING'",
                                 (parent["id"],)).fetchone()["count"]
            active = db.execute("SELECT budget_json FROM moon_runs WHERE parent_id=? AND status='RUNNING'",
                                (parent["id"],)).fetchall()
        if running >= parent["max_parallel"]:
            raise RuntimeError("parent max_parallel limit reached")
        requested = set(tools if tools is not None else parent["tools"])
        unknown = requested - set(parent["tools"])
        if unknown:
            raise PermissionError(f"Moon cannot escalate tools: {sorted(unknown)}")
        forbidden = requested & APPROVAL_ONLY_TOOLS
        if forbidden:
            raise PermissionError(f"approval-only tools cannot be delegated: {sorted(forbidden)}")
        parent_budget = MoonBudget(**parent["budget"])
        budget = budget or MoonBudget(
            min(1.0, parent_budget.max_cost_usd),
            min(30_000, parent_budget.max_input_tokens),
            min(10_000, parent_budget.max_output_tokens),
            min(8, parent_budget.max_calls),
        )
        budget.validate()
        if not budget.fits(parent_budget):
            raise ValueError("Moon budget exceeds parent budget")
        allocated = [MoonBudget(**_load(row["budget_json"], {})) for row in active]
        if (sum(x.max_cost_usd for x in allocated) + budget.max_cost_usd > parent_budget.max_cost_usd or
            sum(x.max_input_tokens for x in allocated) + budget.max_input_tokens > parent_budget.max_input_tokens or
            sum(x.max_output_tokens for x in allocated) + budget.max_output_tokens > parent_budget.max_output_tokens or
            sum(x.max_calls for x in allocated) + budget.max_calls > parent_budget.max_calls):
            raise RuntimeError("active Moon allocations exceed parent budget")
        moon_id = f"{parent['id']}.moon.{uuid.uuid4().hex[:8]}"
        moon = self.registry.ensure_agent(
            moon_id, name or f"{parent['name']} Moon", "Moon", mission,
            parent_id=parent["id"], kind=kind, tools=sorted(requested),
            memory_labels=parent["memory_labels"], max_parallel=1,
            max_depth=parent["max_depth"], budget=budget,
            model_profile=parent["model_profile"],
        )
        with self.registry.session() as db:
            for block_id in memory_block_ids:
                binding = db.execute("SELECT permission FROM block_bindings WHERE agent_id=? AND block_id=?",
                                     (parent["id"], block_id)).fetchone()
                if not binding:
                    raise PermissionError(f"parent has no access to memory block {block_id}")
                db.execute("""INSERT INTO block_bindings(agent_id,block_id,permission,attached_at)
                  VALUES(?,?,?,?) ON CONFLICT(agent_id,block_id) DO UPDATE SET permission='r'""",
                  (moon_id, block_id, "r", utcnow()))
            moon_run_id = "MOON-" + uuid.uuid4().hex[:12].upper()
            db.execute("""INSERT INTO moon_runs
              (id,agent_id,parent_id,run_id,mission,kind,status,budget_json,usage_json,created_at)
              VALUES(?,?,?,?,?,?,'RUNNING',?,?,?)""",
              (moon_run_id, moon_id, parent["id"], run_id, mission, kind,
               _json(asdict(budget)), _json(asdict(MoonBudget(0, 0, 0, 0))), utcnow()))
        return self.get(moon_run_id) or {"agent": moon}

    def get(self, moon_run_id: str) -> dict[str, Any] | None:
        with self.registry.session() as db:
            row = db.execute("SELECT * FROM moon_runs WHERE id=?", (moon_run_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["budget"] = _load(item.pop("budget_json"), {})
        item["usage"] = _load(item.pop("usage_json"), {})
        item["result"] = _load(item.pop("result_json"), None)
        return item

    def list(self, parent_id: str | None = None) -> list[dict[str, Any]]:
        with self.registry.session() as db:
            rows = db.execute("SELECT id FROM moon_runs" + (" WHERE parent_id=?" if parent_id else "") +
                              " ORDER BY created_at DESC", ((_slug(parent_id),) if parent_id else ())).fetchall()
        found = [self.get(row["id"]) for row in rows]
        return [item for item in found if item]

    def complete(self, moon_run_id: str, result: MoonResult | dict[str, Any]) -> dict[str, Any]:
        if isinstance(result, dict):
            result = MoonResult.from_dict(result)
        result.validate()
        run = self.get(moon_run_id)
        if not run or run["status"] != "RUNNING":
            raise ValueError("Moon is not running")
        budget = MoonBudget(**run["budget"])
        if (result.cost_usd > budget.max_cost_usd or result.input_tokens > budget.max_input_tokens or
            result.output_tokens > budget.max_output_tokens):
            raise ValueError("reported Moon usage exceeds reserved budget")
        status = {"success": "SUCCEEDED", "failure": "FAILED", "blocked": "BLOCKED"}[result.status]
        usage = {"max_cost_usd": result.cost_usd, "max_input_tokens": result.input_tokens,
                 "max_output_tokens": result.output_tokens, "max_calls": 1}
        with self.registry.session() as db:
            db.execute("""UPDATE moon_runs SET status=?,usage_json=?,result_json=?,finished_at=? WHERE id=?""",
                       (status, _json(usage), _json(asdict(result)), utcnow(), moon_run_id))
            success_delta = 1 if status == "SUCCEEDED" else 0
            failure_delta = 0 if status == "SUCCEEDED" else 1
            db.execute("""UPDATE agents SET successful_tasks=successful_tasks+?,failed_tasks=failed_tasks+?,
              status=CASE WHEN kind='temporary' THEN 'COMPLETED' ELSE status END,updated_at=? WHERE id=?""",
              (success_delta, failure_delta, utcnow(), run["agent_id"]))
        for lesson in result.memory_candidates:
            self.registry.record_experience(run["parent_id"], result.status, lesson,
                                            run_id=run.get("run_id"), confidence=result.confidence)
        return self.get(moon_run_id) or {}

    def cancel(self, moon_run_id: str, reason: str = "cancelled by parent") -> dict[str, Any]:
        run = self.get(moon_run_id)
        if not run:
            raise KeyError(moon_run_id)
        if run["status"] != "RUNNING":
            return run
        result = MoonResult("blocked", reason, evidence=["runtime cancelled before verified completion"],
                            confidence=0.0)
        with self.registry.session() as db:
            db.execute("""UPDATE moon_runs SET status='CANCELLED',result_json=?,finished_at=? WHERE id=?""",
                       (_json(asdict(result)), utcnow(), moon_run_id))
            db.execute("""UPDATE agents SET status=CASE WHEN kind='temporary' THEN 'COMPLETED' ELSE status END,
              failed_tasks=failed_tasks+1,updated_at=? WHERE id=?""",
              (utcnow(), run["agent_id"]))
        return self.get(moon_run_id) or {}
