"""Autonomous DAG engine executor for Galaxy 2.0+."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
import shlex
import time
from typing import Any, Awaitable, Callable

from galaxy_core.engine.autonomy_pkg.models import (
    RUN_TERMINAL,
    TERMINAL_NODE_STATUSES,
    BudgetExceeded,
    BudgetLedger,
    BudgetLimits,
    BudgetUsage,
    ExecutionPlan,
    ModelProfile,
    ModelRoute,
    NodeResult,
    WorkNode,
    utcnow,
)
from galaxy_core.engine.autonomy_pkg.router import AdaptiveModelRouter
from galaxy_core.engine.autonomy_pkg.store import AutonomyStore
from galaxy_core.engine.autonomy_pkg.workspace import NodeWorkspace
from galaxy_core.engine.storage import atomic_json

Executor = Callable[..., NodeResult | Awaitable[NodeResult]]


class AutonomousEngine:
    """Durable, budgeted and approval-aware executor for validated task graphs."""

    def __init__(self, root: str | Path, profiles: list[ModelProfile], executor: Executor,
                 *, max_parallel: int = 4, store: AutonomyStore | None = None,
                 memory_context: Callable[[WorkNode, ExecutionPlan], list[dict[str, Any]]] | None = None,
                 project_context: Callable[[WorkNode, ExecutionPlan], dict[str, Any]] | None = None,
                 learning_hook: Callable[[str, ExecutionPlan, dict[str, Any]], None] | None = None):
        self.root = Path(root).resolve()
        self.profiles = profiles
        self.executor = executor
        self.max_parallel = max(1, max_parallel)
        self.store = store or AutonomyStore(self.root)
        self.memory_context = memory_context
        self.project_context = project_context
        self.learning_hook = learning_hook
        self._promotion_lock = asyncio.Lock()

    def start(self, plan: ExecutionPlan, budget: BudgetLimits | None = None,
              approval_mode: str = "writes") -> str:
        plan.validate()
        if approval_mode not in {"writes", "once", "off"}:
            raise ValueError("approval_mode must be writes, once or off")
        return self.store.create(plan, budget or BudgetLimits(), approval_mode)

    def approve(self, run_id: str, node_id: str, note: str = "", by: str = "user") -> dict[str, Any]:
        run = self._require_run(run_id)
        node = next((x for x in run["nodes"] if x["node_id"] == node_id), None)
        if not node or node["status"] != "AWAITING_APPROVAL":
            raise ValueError("node is not awaiting approval")
        self.store.approval(run_id, node_id, "approved", note, by)
        self.store.set_node(run_id, node_id, "PENDING")
        self.store.set_run(run_id, "IN_PROGRESS", BudgetUsage(**run["usage"]))
        return self._require_run(run_id)

    def reject(self, run_id: str, node_id: str, note: str = "rejected by user",
               by: str = "user") -> dict[str, Any]:
        run = self._require_run(run_id)
        if not any(x["node_id"] == node_id and x["status"] == "AWAITING_APPROVAL" for x in run["nodes"]):
            raise ValueError("node is not awaiting approval")
        self.store.approval(run_id, node_id, "rejected", note, by)
        self.store.set_node(run_id, node_id, "REJECTED", error=note, finished=True)
        self.store.set_run(run_id, "REJECTED", BudgetUsage(**run["usage"]), note)
        return self._require_run(run_id)

    def run(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        return asyncio.run(self.run_async(run_id, timeout))

    async def run_async(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        run = self._require_run(run_id)
        if run["status"] in RUN_TERMINAL:
            return run
        plan = ExecutionPlan.from_dict(run["plan"])
        limits = BudgetLimits(**run["budget"])
        ledger = BudgetLedger(limits, BudgetUsage(**run["usage"]))
        router = AdaptiveModelRouter(self.profiles, self.store.metrics())
        semaphores = {profile.name: asyncio.Semaphore(profile.max_parallel) for profile in self.profiles}
        deadline = time.monotonic() + (timeout if timeout is not None else limits.max_wall_seconds)
        await self._recover_interrupted(run_id, plan)

        while True:
            if time.monotonic() >= deadline:
                self.store.set_run(run_id, "BLOCKED", ledger.usage, "autonomy deadline exceeded")
                break
            run = self._require_run(run_id)
            statuses = {x["node_id"]: x["status"] for x in run["nodes"]}
            if all(status in TERMINAL_NODE_STATUSES for status in statuses.values()):
                if all(status in {"SUCCEEDED", "SKIPPED"} for status in statuses.values()):
                    self.store.set_run(run_id, "DONE", ledger.usage)
                elif any(status == "REJECTED" for status in statuses.values()):
                    self.store.set_run(run_id, "REJECTED", ledger.usage, "approval rejected")
                else:
                    self.store.set_run(run_id, "FAILED", ledger.usage, "one or more DAG nodes failed")
                break

            self._block_descendants(run_id, plan, statuses)
            run = self._require_run(run_id)
            statuses = {x["node_id"]: x["status"] for x in run["nodes"]}
            if all(status in TERMINAL_NODE_STATUSES for status in statuses.values()):
                continue
            approvals = {(x["node_id"], x["decision"]) for x in run["approvals"]}
            ready = [node for node in plan.nodes if statuses[node.id] == "PENDING" and
                     all(statuses[dep] == "SUCCEEDED" for dep in node.dependencies)]
            executable: list[WorkNode] = []
            for node in ready:
                if self._approval_needed(node, run["approval_mode"], approvals):
                    self.store.set_node(run_id, node.id, "AWAITING_APPROVAL")
                else:
                    executable.append(node)
            if executable:
                ordered = sorted(executable, key=lambda n: (-n.priority, n.id))[:self.max_parallel]
                results = await asyncio.gather(*[
                    self._execute_node(run_id, plan, node, router, ledger, semaphores) for node in ordered
                ], return_exceptions=True)
                fatal = next((x for x in results if isinstance(x, BudgetExceeded)), None)
                if fatal:
                    self.store.set_run(run_id, "BUDGET_EXHAUSTED", ledger.usage, str(fatal))
                    break
                unexpected = next((x for x in results if isinstance(x, BaseException)), None)
                if unexpected:
                    self.store.set_run(run_id, "BLOCKED", ledger.usage,
                                       f"runtime error: {unexpected}")
                    break
                continue
            run = self._require_run(run_id)
            if any(x["status"] == "AWAITING_APPROVAL" for x in run["nodes"]):
                self.store.set_run(run_id, "AWAITING_APPROVAL", ledger.usage,
                                   "one or more nodes require human approval")
                break
            if any(x["status"] == "RUNNING" for x in run["nodes"]):
                await asyncio.sleep(.02)
                continue
            self.store.set_run(run_id, "BLOCKED", ledger.usage, "DAG has no executable frontier")
            break

        final = self._require_run(run_id)
        self._write_projection(final)
        if final["status"] == "DONE" and self.learning_hook:
            self.learning_hook(run_id, plan, final)
        return final

    async def _execute_node(self, run_id: str, plan: ExecutionPlan, node: WorkNode,
                            router: AdaptiveModelRouter, ledger: BudgetLedger,
                            semaphores: dict[str, asyncio.Semaphore]) -> None:
        run = self._require_run(run_id)
        row = next(x for x in run["nodes"] if x["node_id"] == node.id)
        if row["attempts"] >= node.max_attempts:
            self.store.set_node(run_id, node.id, "FAILED",
                                error="attempt limit exhausted", finished=True)
            return
        last_error = ""
        for attempt in range(row["attempts"] + 1, node.max_attempts + 1):
            remaining = ledger.limits.max_cost_usd - ledger.usage.cost_usd
            route = router.select(node, remaining)
            await ledger.reserve(node.estimated_input_tokens, node.estimated_output_tokens,
                                 route.estimated_cost_usd)
            self.store.set_run(run_id, "IN_PROGRESS", ledger.usage)
            self.store.set_node(run_id, node.id, "RUNNING", attempts=attempt,
                                route=route, started=True)
            started_at = utcnow()
            before = time.monotonic()
            result: NodeResult | None = None
            error = ""
            evidence = self.root / "data" / "runs" / run_id / node.id / f"attempt-{attempt}"
            evidence.mkdir(parents=True, exist_ok=True)
            try:
                with NodeWorkspace(self.root, node) as workspace:
                    context = self._context(node, plan, run_id)
                    if getattr(self.executor, "manages_model_concurrency", False):
                        invoked = self.executor(node, context, route, workspace.path, evidence)
                        if inspect.isawaitable(invoked):
                            result = await asyncio.wait_for(invoked, node.timeout_seconds)
                        else:
                            result = invoked
                    else:
                        async with semaphores[route.profile]:
                            invoked = self.executor(node, context, route, workspace.path, evidence)
                            if inspect.isawaitable(invoked):
                                result = await asyncio.wait_for(invoked, node.timeout_seconds)
                            else:
                                result = invoked
                    if isinstance(result, dict):
                        result = NodeResult.from_dict(result)
                    if not isinstance(result, NodeResult):
                        raise TypeError("executor must return NodeResult")
                    result.validate()
                    if result.status == "PASS" and node.verification_commands:
                        await self._verify_commands(node, workspace.path, evidence)
                    if result.status == "PASS" and node.risk == "write":
                        async with self._promotion_lock:
                            workspace.promote(result)
                success = result.status == "PASS"
            except asyncio.TimeoutError:
                error = "node deadline exceeded"; success = False
            except Exception as exc:
                error = str(exc); success = False
            latency = time.monotonic() - before
            self.store.record_metric(route.profile, success, latency)
            finished_at = utcnow()
            attempt_status = "SUCCEEDED" if success else "FAILED"
            self.store.attempt(run_id, node.id, attempt, attempt_status, route, result,
                               error, started_at, finished_at)
            if success:
                self.store.set_node(run_id, node.id, "SUCCEEDED", attempts=attempt,
                                    result=result, route=route, finished=True)
                return
            last_error = error or (result.summary if result else "unknown executor failure")
            if result and result.status == "BLOCKED":
                break
            if node.risk in {"external", "dangerous"}:
                last_error += "; external side effects may be uncertain, automatic retry refused"
                break
        self.store.set_node(run_id, node.id, "FAILED", attempts=min(node.max_attempts, max(1, row["attempts"] + 1)),
                            result=result, route=route, error=last_error, finished=True)

    async def _verify_commands(self, node: WorkNode, workdir: Path,
                               evidence: Path) -> None:
        logs: list[str] = []
        for index, command in enumerate(node.verification_commands, 1):
            args = shlex.split(command)
            process = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), node.timeout_seconds)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
                raise RuntimeError(f"verification command timed out: {command}")
            text = (stdout.decode("utf-8", errors="replace") +
                    stderr.decode("utf-8", errors="replace"))
            (evidence / f"verify-{index}.log").write_text(text, encoding="utf-8")
            logs.append(f"[{process.returncode}] {command}")
            if process.returncode:
                raise RuntimeError(f"verification command failed ({process.returncode}): {command}")
        (evidence / "verification-summary.log").write_text("\n".join(logs) + "\n", encoding="utf-8")

    def _context(self, node: WorkNode, plan: ExecutionPlan, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        results = {x["node_id"]: x["result"] for x in run["nodes"] if x["result"]}
        context: dict[str, Any] = {
            "autonomy_run_id": run_id,
            "node_id": node.id,
            "goal": plan.goal,
            "project": plan.project,
            "success_criteria": list(plan.success_criteria),
            "constraints": list(plan.constraints),
            "dependency_results": {dep: results.get(dep) for dep in node.dependencies},
            "context_refs": list(node.context_refs),
        }
        if self.memory_context:
            context["managed_memories"] = self.memory_context(node, plan)
        if self.project_context:
            context["project_context"] = self.project_context(node, plan)
        return context

    @staticmethod
    def _approval_needed(node: WorkNode, mode: str,
                         approvals: set[tuple[str, str]]) -> bool:
        if (node.id, "approved") in approvals:
            return False
        if mode == "once" and any(decision == "approved" for _, decision in approvals):
            return node.risk in {"external", "dangerous"}
        if node.risk in {"external", "dangerous"}:
            return True
        return node.risk == "write" and mode == "writes"

    def _block_descendants(self, run_id: str, plan: ExecutionPlan,
                           statuses: dict[str, str]) -> None:
        failed = {node_id for node_id, status in statuses.items()
                  if status in {"FAILED", "BLOCKED", "REJECTED"}}
        if not failed:
            return
        changed = True
        while changed:
            changed = False
            for node in plan.nodes:
                if statuses[node.id] == "PENDING" and any(dep in failed for dep in node.dependencies):
                    statuses[node.id] = "BLOCKED"; failed.add(node.id); changed = True
                    self.store.set_node(run_id, node.id, "BLOCKED",
                                        error="dependency failed", finished=True)

    async def _recover_interrupted(self, run_id: str, plan: ExecutionPlan) -> None:
        run = self._require_run(run_id)
        by_id = {node.id: node for node in plan.nodes}
        for row in run["nodes"]:
            if row["status"] != "RUNNING":
                continue
            node = by_id[row["node_id"]]
            if node.risk in {"read", "write"}:
                self.store.set_node(run_id, node.id, "PENDING",
                                    attempts=max(0, row["attempts"] - 1),
                                    error="recovered interrupted isolated attempt")
            else:
                self.store.set_node(run_id, node.id, "BLOCKED",
                                    error="external side effects uncertain after interruption", finished=True)

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if not run:
            raise ValueError(f"autonomy run not found: {run_id}")
        return run

    def _write_projection(self, run: dict[str, Any]) -> None:
        path = self.root / "data" / "runs" / run["id"] / "state.json"
        atomic_json(path, run)
