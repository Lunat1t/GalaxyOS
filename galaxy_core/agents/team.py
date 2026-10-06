"""Automatic bounded team construction and durable Moon execution."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import inspect
import json
from pathlib import Path
import re
import uuid
from typing import Any, Awaitable, Callable, Iterable

from .runtime import (
    APPROVAL_ONLY_TOOLS, AgentRegistry, MoonBudget, MoonResult, SubAgentManager,
    _json, _load, utcnow,
)
from ..engine.autonomy import NodeWorkspace, WorkNode


TEAM_TERMINAL = {"SUCCEEDED", "FAILED", "BLOCKED", "SKIPPED"}
TASK_TERMINAL = {"SUCCEEDED", "FAILED", "BLOCKED", "SKIPPED"}
_COMPLEX_WORDS = {
    "architecture", "audit", "compare", "design", "implement", "integration",
    "investigate", "migration", "optimize", "refactor", "research", "security",
    "test", "verify", "архитект", "аудит", "внедр", "интеграц", "исслед",
    "миграц", "оптимиз", "провер", "рефактор", "сравн", "тест",
}


def _task_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-")
    if not value:
        raise ValueError("team task id is required")
    return value[:64]


@dataclass(frozen=True)
class TeamTask:
    id: str
    title: str
    mission: str
    capability: str = "general"
    tools: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    priority: int = 50
    expected_seconds: int = 300
    budget: MoonBudget = field(default_factory=MoonBudget)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TeamTask":
        data = dict(raw)
        data["id"] = _task_id(data["id"])
        data["tools"] = tuple(data.get("tools") or ())
        data["dependencies"] = tuple(data.get("dependencies") or ())
        if isinstance(data.get("budget"), dict):
            data["budget"] = MoonBudget(**data["budget"])
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TeamPlan:
    objective: str
    tasks: tuple[TeamTask, ...]
    rationale: str
    expected_gain: float = .25

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TeamPlan":
        return cls(
            str(raw.get("objective") or ""),
            tuple(TeamTask.from_dict(item) for item in raw.get("tasks") or ()),
            str(raw.get("rationale") or ""),
            float(raw.get("expected_gain", .25)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self, parent: dict[str, Any], policy: "TeamPolicy") -> None:
        if not self.objective.strip() or not self.rationale.strip():
            raise ValueError("team plan requires objective and rationale")
        if not 2 <= len(self.tasks) <= min(policy.max_tasks, 16):
            raise ValueError("automatic team must contain 2..max_tasks tasks")
        if not 0 <= self.expected_gain <= 1:
            raise ValueError("expected_gain must be between 0 and 1")
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate team task ids")
        known = set(ids)
        parent_tools = set(parent["tools"])
        for task in self.tasks:
            if not task.title.strip() or not task.mission.strip():
                raise ValueError(f"{task.id}: title and mission are required")
            if task.expected_seconds < 1 or not 0 <= task.priority <= 100:
                raise ValueError(f"{task.id}: invalid scheduling metadata")
            unknown = set(task.dependencies) - known
            if unknown or task.id in task.dependencies:
                raise ValueError(f"{task.id}: invalid dependencies {sorted(unknown)}")
            escalated = set(task.tools) - parent_tools
            if escalated:
                raise PermissionError(f"{task.id}: tool escalation {sorted(escalated)}")
            forbidden = set(task.tools) & APPROVAL_ONLY_TOOLS
            if forbidden:
                raise PermissionError(f"{task.id}: approval-only tools {sorted(forbidden)}")
            mutating = {tool for tool in task.tools if any(
                marker in tool for marker in ("write", "delete", "deploy", "push", "send", "payment"))}
            if mutating:
                raise PermissionError(f"{task.id}: automatic Moons are read-only: {sorted(mutating)}")
            task.budget.validate()
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {task.id: task for task in self.tasks}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"team task cycle at {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)
        parent_budget = MoonBudget(**parent["budget"])
        if (sum(task.budget.max_cost_usd for task in self.tasks) > parent_budget.max_cost_usd or
            sum(task.budget.max_input_tokens for task in self.tasks) > parent_budget.max_input_tokens or
            sum(task.budget.max_output_tokens for task in self.tasks) > parent_budget.max_output_tokens or
            sum(task.budget.max_calls for task in self.tasks) > parent_budget.max_calls):
            raise ValueError("team reservation exceeds parent budget")


@dataclass(frozen=True)
class TeamPolicy:
    enabled: bool = True
    minimum_complexity: float = 3.0
    minimum_expected_gain: float = .15
    max_tasks: int = 4
    max_parallel: int = 4
    budget_fraction: float = .8
    fail_fast: bool = True

    def validate(self) -> None:
        if self.minimum_complexity < 0 or not 0 <= self.minimum_expected_gain <= 1:
            raise ValueError("invalid Team Builder thresholds")
        if not 2 <= self.max_tasks <= 16 or self.max_parallel < 1:
            raise ValueError("invalid Team Builder concurrency")
        if not 0 < self.budget_fraction <= 1:
            raise ValueError("budget_fraction must be within (0,1]")


@dataclass
class TeamOutcome:
    delegated: bool
    status: str
    summary: str
    reason: str
    team_id: str | None = None
    tasks: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    memory_candidates: list[str] = field(default_factory=list)
    confidence: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Planner = Callable[..., TeamPlan | dict[str, Any] | Awaitable[TeamPlan | dict[str, Any]]]
Executor = Callable[..., MoonResult | dict[str, Any] | Awaitable[MoonResult | dict[str, Any]]]


class AutomaticTeamBuilder:
    """Decide, plan, execute and join a safe team of read-only Moons."""

    def __init__(self, registry: AgentRegistry, planner: Planner, executor: Executor,
                 policy: TeamPolicy | None = None):
        self.registry = registry
        self.moons = SubAgentManager(registry)
        self.planner = planner
        self.executor = executor
        self.policy = policy or TeamPolicy()
        self.policy.validate()

    @staticmethod
    def complexity(objective: str, criteria: Iterable[str] = ()) -> float:
        criteria = tuple(criteria)
        text = " ".join([objective, *criteria]).lower()
        words = re.findall(r"[\w-]+", text, flags=re.UNICODE)
        score = min(3.0, len(words) / 18)
        score += min(3.0, sum(1 for key in _COMPLEX_WORDS if key in text) * .65)
        score += min(2.0, text.count(",") * .2 + text.count(";") * .35)
        score += min(2.0, max(0, len(criteria) - 1) * .35)
        return round(score, 3)

    def should_delegate(self, objective: str, criteria: Iterable[str] = ()) -> tuple[bool, float, str]:
        if not self.policy.enabled:
            return False, 0.0, "Team Builder disabled by policy"
        score = self.complexity(objective, tuple(criteria))
        if score < self.policy.minimum_complexity:
            return False, score, "single agent is cheaper for this objective"
        return True, score, "objective has enough independent analytical work"

    async def run_async(self, parent_id: str, objective: str, *,
                        criteria: Iterable[str] = (), project: str | None = None,
                        autonomy_run_id: str | None = None, node_id: str | None = None,
                        context: dict[str, Any] | None = None, force: bool = False,
                        resume_team_id: str | None = None) -> TeamOutcome:
        parent = self.registry.get_agent(parent_id)
        if not parent:
            raise KeyError(parent_id)
        if resume_team_id:
            team = self._require_team(resume_team_id)
            if team["parent_id"] != parent["id"]:
                raise PermissionError("team belongs to another parent")
            plan = TeamPlan.from_dict(team["plan"])
            self._recover_interrupted(team["id"])
            return await self._execute(team["id"], parent, plan, context or {}, project)
        decision, score, reason = self.should_delegate(objective, criteria)
        if not decision and not force:
            return TeamOutcome(False, "SKIPPED", "Parent should execute directly", reason,
                               confidence=1.0)
        raw = self.planner(
            parent=parent, objective=objective, criteria=list(criteria),
            context=context or {}, policy=asdict(self.policy),
        )
        if inspect.isawaitable(raw):
            raw = await raw
        plan = raw if isinstance(raw, TeamPlan) else TeamPlan.from_dict(raw)
        plan = self._allocate(plan, parent)
        plan.validate(parent, self.policy)
        if plan.expected_gain < self.policy.minimum_expected_gain and not force:
            return TeamOutcome(False, "SKIPPED", "Parent should execute directly",
                               "planner expected gain is below policy threshold", confidence=1.0)
        team_id = self._create_team(parent["id"], plan, score, reason, project,
                                    autonomy_run_id, node_id)
        return await self._execute(team_id, parent, plan, context or {}, project)

    def run(self, parent_id: str, objective: str, **kwargs: Any) -> TeamOutcome:
        return asyncio.run(self.run_async(parent_id, objective, **kwargs))

    def _allocate(self, plan: TeamPlan, parent: dict[str, Any]) -> TeamPlan:
        count = len(plan.tasks)
        if not count:
            return plan
        total = MoonBudget(**parent["budget"])
        fraction = self.policy.budget_fraction
        each = MoonBudget(
            max_cost_usd=round(total.max_cost_usd * fraction / count, 6),
            max_input_tokens=max(1, int(total.max_input_tokens * fraction / count)),
            max_output_tokens=max(1, int(total.max_output_tokens * fraction / count)),
            max_calls=max(1, int(total.max_calls * fraction / count)),
        )
        tasks = tuple(TeamTask(
            task.id, task.title, task.mission, task.capability, task.tools,
            task.dependencies, task.priority, task.expected_seconds, each,
        ) for task in plan.tasks)
        return TeamPlan(plan.objective, tasks, plan.rationale, plan.expected_gain)

    def _create_team(self, parent_id: str, plan: TeamPlan, score: float, reason: str,
                     project: str | None, autonomy_run_id: str | None,
                     node_id: str | None) -> str:
        team_id = "TEAM-" + uuid.uuid4().hex[:12].upper()
        now = utcnow()
        parent = self.registry.get_agent(parent_id)
        if not parent:
            raise KeyError(parent_id)
        parent_budget = MoonBudget(**parent["budget"])
        requested = self._plan_budget(plan)
        with self.registry.session() as db:
            db.execute("BEGIN IMMEDIATE")
            reserved = MoonBudget(0, 0, 0, 0)
            for row in db.execute("""SELECT plan_json FROM team_runs
              WHERE parent_id=? AND status IN ('PLANNED','RUNNING')""", (parent_id,)):
                active_plan = TeamPlan.from_dict(_load(row["plan_json"], {}))
                reserved = self._add_budget(reserved, self._plan_budget(active_plan))
            active_team_ids = [row["id"] for row in db.execute("""SELECT id FROM team_runs
              WHERE parent_id=? AND status IN ('PLANNED','RUNNING')""", (parent_id,))]
            manual_sql = "SELECT budget_json FROM moon_runs WHERE parent_id=? AND status='RUNNING'"
            params: list[Any] = [parent_id]
            if active_team_ids:
                manual_sql += " AND (run_id IS NULL OR run_id NOT IN (%s))" % ",".join("?" * len(active_team_ids))
                params.extend(active_team_ids)
            for row in db.execute(manual_sql, params):
                reserved = self._add_budget(reserved, MoonBudget(**_load(row["budget_json"], {})))
            combined = self._add_budget(reserved, requested)
            if not combined.fits(parent_budget):
                raise RuntimeError("parent budget is already reserved by active teams or Moons")
            db.execute("""INSERT INTO team_runs
              (id,parent_id,autonomy_run_id,node_id,project,objective,status,decision_score,
               reason,plan_json,created_at,updated_at)
              VALUES(?,?,?,?,?,?,'PLANNED',?,?,?,?,?)""",
              (team_id, parent_id, autonomy_run_id, node_id, project, plan.objective,
               score, reason, _json(plan.to_dict()), now, now))
            db.executemany("""INSERT INTO team_tasks
              (team_id,task_id,status,task_json) VALUES(?,?,'PENDING',?)""",
              [(team_id, task.id, _json(task.to_dict())) for task in plan.tasks])
        return team_id

    @staticmethod
    def _plan_budget(plan: TeamPlan) -> MoonBudget:
        return MoonBudget(
            sum(task.budget.max_cost_usd for task in plan.tasks),
            sum(task.budget.max_input_tokens for task in plan.tasks),
            sum(task.budget.max_output_tokens for task in plan.tasks),
            sum(task.budget.max_calls for task in plan.tasks),
        )

    @staticmethod
    def _add_budget(left: MoonBudget, right: MoonBudget) -> MoonBudget:
        return MoonBudget(
            left.max_cost_usd + right.max_cost_usd,
            left.max_input_tokens + right.max_input_tokens,
            left.max_output_tokens + right.max_output_tokens,
            left.max_calls + right.max_calls,
        )

    async def _execute(self, team_id: str, parent: dict[str, Any], plan: TeamPlan,
                       context: dict[str, Any], project: str | None) -> TeamOutcome:
        concurrency = min(parent["max_parallel"], self.policy.max_parallel)
        while True:
            rows = self._task_rows(team_id)
            statuses = {row["task_id"]: row["status"] for row in rows}
            if all(status in TASK_TERMINAL for status in statuses.values()):
                break
            failed = {task_id for task_id, status in statuses.items()
                      if status in {"FAILED", "BLOCKED"}}
            for task in plan.tasks:
                if statuses[task.id] == "PENDING" and any(dep in failed for dep in task.dependencies):
                    self._set_task(team_id, task.id, "BLOCKED",
                                   error="dependency failed", finished=True)
            rows = self._task_rows(team_id)
            statuses = {row["task_id"]: row["status"] for row in rows}
            ready = [task for task in plan.tasks if statuses[task.id] == "PENDING" and
                     all(statuses[dep] == "SUCCEEDED" for dep in task.dependencies)]
            if not ready:
                if any(status == "RUNNING" for status in statuses.values()):
                    await asyncio.sleep(.02)
                    continue
                break
            batch = sorted(ready, key=lambda item: (-item.priority, item.id))[:concurrency]
            prepared: list[tuple[TeamTask, dict[str, Any]]] = []
            for task in batch:
                try:
                    prepared.append(self._prepare(team_id, parent, task))
                except Exception as exc:
                    self._set_task(team_id, task.id, "FAILED", error=f"Moon spawn failed: {exc}",
                                   attempts_delta=1, started=True, finished=True)
            if prepared:
                await asyncio.gather(*[
                    self._execute_prepared(team_id, parent, task, moon, context, project)
                    for task, moon in prepared
                ])
            if self.policy.fail_fast:
                latest = {row["task_id"]: row["status"] for row in self._task_rows(team_id)}
                if any(status == "FAILED" for status in latest.values()):
                    for task_id, status in latest.items():
                        if status == "PENDING":
                            self._set_task(team_id, task_id, "SKIPPED",
                                           error="team fail-fast", finished=True)
        return self._join(team_id, plan)

    def _prepare(self, team_id: str, parent: dict[str, Any], task: TeamTask) -> tuple[TeamTask, dict[str, Any]]:
        moon = self.moons.spawn(
            parent["id"], task.mission, name=task.title, kind="temporary",
            tools=task.tools or self._safe_default_tools(parent), budget=task.budget,
            run_id=team_id,
        )
        self._set_task(team_id, task.id, "RUNNING", moon_run_id=moon["id"],
                       attempts_delta=1, started=True)
        return task, moon

    @staticmethod
    def _safe_default_tools(parent: dict[str, Any]) -> list[str]:
        tools = [tool for tool in parent["tools"] if tool not in APPROVAL_ONLY_TOOLS and
                 not any(part in tool for part in ("write", "delete", "deploy", "push", "send"))]
        return tools or [tool for tool in parent["tools"] if tool not in APPROVAL_ONLY_TOOLS][:1]

    async def _execute_prepared(self, team_id: str, parent: dict[str, Any], task: TeamTask,
                                moon: dict[str, Any], context: dict[str, Any],
                                project: str | None) -> None:
        evidence = self.registry.root / "data" / "teams" / team_id / task.id
        evidence.mkdir(parents=True, exist_ok=True)
        node = WorkNode(
            id=f"moon-{task.id}", title=task.title, objective=task.mission,
            role="Moon", capability=task.capability, risk="read",
            timeout_seconds=task.expected_seconds,
            estimated_input_tokens=task.budget.max_input_tokens,
            estimated_output_tokens=task.budget.max_output_tokens,
            max_attempts=1,
        )
        result: MoonResult
        error = ""
        try:
            with NodeWorkspace(self.registry.root, node) as workspace:
                moon_context = {
                    "parent": {key: parent[key] for key in ("id", "name", "role", "mission")},
                    "agent": self.registry.context(moon["agent_id"], project=project),
                    "team_context": context,
                    "dependencies": self._dependency_results(team_id, task),
                    "authority": "read-only disposable workspace; no external side effects",
                }
                invoked = self.executor(task=task, context=moon_context,
                                        workdir=workspace.path, evidence=evidence)
                if inspect.isawaitable(invoked):
                    invoked = await asyncio.wait_for(invoked, timeout=task.expected_seconds)
                result = invoked if isinstance(invoked, MoonResult) else MoonResult.from_dict(invoked)
                result.validate()
        except asyncio.TimeoutError:
            error = "Moon deadline exceeded"
            result = MoonResult("failure", error, evidence=[error], confidence=0.0)
        except Exception as exc:
            error = str(exc)
            result = MoonResult("failure", f"Moon execution error: {exc}",
                                evidence=[str(exc)], confidence=0.0)
        try:
            completed = self.moons.complete(moon["id"], result)
            status = completed["status"]
        except Exception as exc:
            error = f"{error}; {exc}".strip("; ")
            self.moons.cancel(moon["id"], error)
            status = "FAILED"
        self._set_task(team_id, task.id, status, result=asdict(result),
                       error=error, finished=True)

    def _dependency_results(self, team_id: str, task: TeamTask) -> dict[str, Any]:
        rows = {row["task_id"]: row for row in self._task_rows(team_id)}
        return {dep: rows[dep]["result"] for dep in task.dependencies}

    def _join(self, team_id: str, plan: TeamPlan) -> TeamOutcome:
        rows = self._task_rows(team_id)
        failed = [row for row in rows if row["status"] in {"FAILED", "BLOCKED"}]
        status = "FAILED" if failed else "SUCCEEDED"
        results = [row["result"] for row in rows if row["result"]]
        findings = self._unique(item for result in results for item in result.get("findings", []))
        artifacts = self._unique(item for result in results for item in result.get("artifacts", []))
        evidence = self._unique(item for result in results for item in result.get("evidence", []))
        memories = self._unique(item for result in results for item in result.get("memory_candidates", []))
        confidences = [float(result.get("confidence", 0)) for result in results]
        summary_lines = [f"{row['task_id']}: {(row['result'] or {}).get('summary', row['error'])}" for row in rows]
        summary = ("Team completed. " if status == "SUCCEEDED" else "Team failed. ") + " | ".join(summary_lines)
        outcome = TeamOutcome(
            True, status, summary,
            "parallel Moon results joined deterministically", team_id, rows,
            findings, artifacts, evidence, memories,
            min(confidences) if confidences else 0.0,
            sum(int(result.get("input_tokens", 0)) for result in results),
            sum(int(result.get("output_tokens", 0)) for result in results),
            round(sum(float(result.get("cost_usd", 0)) for result in results), 8),
        )
        now = utcnow()
        with self.registry.session() as db:
            db.execute("""UPDATE team_runs SET status=?,result_json=?,updated_at=?,finished_at=? WHERE id=?""",
                       (status, _json(outcome.to_dict()), now, now, team_id))
        return outcome

    @staticmethod
    def _unique(items: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(item for item in items if item))

    def _recover_interrupted(self, team_id: str) -> None:
        for row in self._task_rows(team_id):
            if row["status"] == "RUNNING":
                if row["moon_run_id"]:
                    self.moons.cancel(row["moon_run_id"], "recovered after interrupted Team Builder run")
                self._set_task(team_id, row["task_id"], "PENDING",
                               moon_run_id="", error="recovered after interruption")

    def _require_team(self, team_id: str) -> dict[str, Any]:
        with self.registry.session() as db:
            row = db.execute("SELECT * FROM team_runs WHERE id=?", (team_id,)).fetchone()
        if not row:
            raise KeyError(team_id)
        item = dict(row)
        item["plan"] = _load(item.pop("plan_json"), {})
        item["result"] = _load(item.pop("result_json"), None)
        return item

    def status(self, team_id: str) -> dict[str, Any]:
        team = self._require_team(team_id)
        team["tasks"] = self._task_rows(team_id)
        return team

    def list(self, parent_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.registry.session() as db:
            if parent_id:
                rows = db.execute("SELECT id FROM team_runs WHERE parent_id=? ORDER BY created_at DESC LIMIT ?",
                                  (parent_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT id FROM team_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.status(row["id"]) for row in rows]

    def _task_rows(self, team_id: str) -> list[dict[str, Any]]:
        with self.registry.session() as db:
            rows = db.execute("SELECT * FROM team_tasks WHERE team_id=? ORDER BY task_id", (team_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["task"] = _load(item.pop("task_json"), {})
            item["result"] = _load(item.pop("result_json"), None)
            out.append(item)
        return out

    def _set_task(self, team_id: str, task_id: str, status: str, *,
                  moon_run_id: str | None = None, result: dict[str, Any] | None = None,
                  error: str | None = None, attempts_delta: int = 0,
                  started: bool = False, finished: bool = False) -> None:
        updates = ["status=?", "attempts=attempts+?"]
        values: list[Any] = [status, attempts_delta]
        if moon_run_id is not None:
            updates.append("moon_run_id=?"); values.append(moon_run_id or None)
        if result is not None:
            updates.append("result_json=?"); values.append(_json(result))
        if error is not None:
            updates.append("error=?"); values.append(error)
        if started:
            updates.append("started_at=?"); values.append(utcnow())
        if finished:
            updates.append("finished_at=?"); values.append(utcnow())
        values.extend([team_id, task_id])
        with self.registry.session() as db:
            db.execute(f"UPDATE team_tasks SET {','.join(updates)} WHERE team_id=? AND task_id=?", values)
            db.execute("UPDATE team_runs SET status='RUNNING',updated_at=? WHERE id=? AND status='PLANNED'",
                       (utcnow(), team_id))
