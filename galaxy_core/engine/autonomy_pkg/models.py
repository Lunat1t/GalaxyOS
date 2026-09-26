"""Data models, validation, and budget primitives for Galaxy DAG autonomy."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import datetime as dt
import fnmatch
from pathlib import Path
import shlex
import time
from typing import Any
import uuid

from galaxy_core.engine.roles import ROLES


NODE_STATUSES = {
    "PENDING", "RUNNING", "AWAITING_APPROVAL", "SUCCEEDED", "FAILED",
    "BLOCKED", "REJECTED", "SKIPPED",
}
TERMINAL_NODE_STATUSES = {"SUCCEEDED", "FAILED", "BLOCKED", "REJECTED", "SKIPPED"}
RISK_LEVELS = {"read", "write", "external", "dangerous"}
RUN_TERMINAL = {"DONE", "FAILED", "BLOCKED", "REJECTED", "BUDGET_EXHAUSTED"}
GENERATED_DIRS = {".git", ".obsidian", "__pycache__", "data"}


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_rel(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value!r}")
    if path.parts[0] in GENERATED_DIRS or value.startswith(".env"):
        raise ValueError(f"protected path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class WorkNode:
    id: str
    title: str
    objective: str
    role: str = "Earth"
    capability: str = "implementation"
    dependencies: tuple[str, ...] = ()
    risk: str = "read"
    write_scopes: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    estimated_input_tokens: int = 1200
    estimated_output_tokens: int = 700
    max_attempts: int = 2
    timeout_seconds: int = 900
    priority: int = 50
    idempotency_key: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkNode":
        values = dict(raw)
        for key in ("dependencies", "write_scopes", "acceptance_criteria", "context_refs", "verification_commands"):
            values[key] = tuple(values.get(key) or ())
        return cls(**values)

    def validate(self) -> None:
        if not self.id or not all(c.isalnum() or c in "_-" for c in self.id):
            raise ValueError(f"invalid node id: {self.id!r}")
        if not self.title.strip() or not self.objective.strip():
            raise ValueError(f"{self.id}: title and objective are required")
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"{self.id}: invalid risk {self.risk!r}")
        if self.role not in ROLES:
            raise ValueError(f"{self.id}: unknown role {self.role!r}")
        if self.risk == "write" and not ROLES[self.role].may_write:
            raise ValueError(f"{self.id}: role {self.role} has no write authority")
        if self.risk == "write" and not self.write_scopes:
            raise ValueError(f"{self.id}: write nodes require explicit write_scopes")
        for scope in self.write_scopes:
            _safe_rel(scope.replace("*", "x").replace("?", "x"))
        for ref in self.context_refs:
            _safe_rel(ref)
        for command in self.verification_commands:
            if not command.strip() or "\n" in command or not shlex.split(command):
                raise ValueError(f"{self.id}: invalid verification command")
        if self.risk in {"external", "dangerous"} and not self.idempotency_key:
            raise ValueError(f"{self.id}: {self.risk} nodes require idempotency_key")
        if self.estimated_input_tokens < 0 or self.estimated_output_tokens < 0:
            raise ValueError(f"{self.id}: token estimates cannot be negative")
        if self.max_attempts < 1 or self.max_attempts > 8:
            raise ValueError(f"{self.id}: max_attempts must be 1..8")
        if self.timeout_seconds < 1:
            raise ValueError(f"{self.id}: timeout_seconds must be positive")


@dataclass(frozen=True)
class ExecutionPlan:
    goal: str
    project: str
    nodes: tuple[WorkNode, ...]
    success_criteria: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    plan_id: str = field(default_factory=lambda: "PLAN-" + uuid.uuid4().hex[:12].upper())
    created_at: str = field(default_factory=utcnow)
    schema_version: str = "2.0"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExecutionPlan":
        values = dict(raw)
        values["nodes"] = tuple(WorkNode.from_dict(x) for x in values.get("nodes") or ())
        values["success_criteria"] = tuple(values.get("success_criteria") or ())
        values["constraints"] = tuple(values.get("constraints") or ())
        plan = cls(**values)
        plan.validate()
        return plan

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if not self.goal.strip() or not self.project.strip():
            raise ValueError("goal and project are required")
        if not self.nodes:
            raise ValueError("plan must contain at least one node")
        if len(self.nodes) > 128:
            raise ValueError("plan exceeds 128-node safety limit")
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node ids")
        known = set(ids)
        for node in self.nodes:
            node.validate()
            unknown = set(node.dependencies) - known
            if unknown:
                raise ValueError(f"{node.id}: unknown dependencies: {sorted(unknown)}")
            if node.id in node.dependencies:
                raise ValueError(f"{node.id}: self dependency")
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {node.id: node for node in self.nodes}

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError(f"cycle detected at {node_id}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dep in by_id[node_id].dependencies:
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in ids:
            visit(node_id)
        writers = {n.id for n in self.nodes if n.risk == "write"}
        verifiers = [n for n in self.nodes if n.capability in {"verification", "qa"}]
        if writers and not verifiers:
            raise ValueError("a plan with writes requires a verification/qa node")
        if writers and not any(writers.issubset(_ancestors(v.id, by_id)) and v.verification_commands
                               for v in verifiers):
            raise ValueError("at least one verification node with deterministic commands must depend on every write node")

    def layers(self) -> list[list[str]]:
        remaining = {node.id: set(node.dependencies) for node in self.nodes}
        layers: list[list[str]] = []
        while remaining:
            ready = sorted(node_id for node_id, deps in remaining.items() if not deps)
            if not ready:
                raise ValueError("plan contains a cycle")
            layers.append(ready)
            for node_id in ready:
                remaining.pop(node_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return layers


def _ancestors(node_id: str, by_id: dict[str, WorkNode]) -> set[str]:
    found: set[str] = set()
    stack = list(by_id[node_id].dependencies)
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(by_id[current].dependencies)
    return found


@dataclass(frozen=True)
class BudgetLimits:
    max_cost_usd: float = 10.0
    max_input_tokens: int = 200_000
    max_output_tokens: int = 80_000
    max_calls: int = 80
    max_wall_seconds: int = 7200

    def validate(self) -> None:
        if min(self.max_cost_usd, self.max_input_tokens, self.max_output_tokens,
               self.max_calls, self.max_wall_seconds) < 0:
            raise ValueError("budget limits cannot be negative")


@dataclass
class BudgetUsage:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BudgetExceeded(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, limits: BudgetLimits, usage: BudgetUsage | None = None):
        limits.validate()
        self.limits = limits
        self.usage = usage or BudgetUsage()
        self.started = time.monotonic()
        self._lock = asyncio.Lock()

    async def reserve(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        async with self._lock:
            proposed = BudgetUsage(
                self.usage.cost_usd + max(0.0, cost_usd),
                self.usage.input_tokens + max(0, input_tokens),
                self.usage.output_tokens + max(0, output_tokens),
                self.usage.calls + 1,
            )
            if proposed.cost_usd > self.limits.max_cost_usd:
                raise BudgetExceeded("cost budget exhausted")
            if proposed.input_tokens > self.limits.max_input_tokens:
                raise BudgetExceeded("input-token budget exhausted")
            if proposed.output_tokens > self.limits.max_output_tokens:
                raise BudgetExceeded("output-token budget exhausted")
            if proposed.calls > self.limits.max_calls:
                raise BudgetExceeded("call budget exhausted")
            if time.monotonic() - self.started > self.limits.max_wall_seconds:
                raise BudgetExceeded("wall-clock budget exhausted")
            self.usage = proposed


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str | None = None
    capabilities: tuple[str, ...] = ("general",)
    quality: float = 0.75
    latency_score: float = 0.5
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    max_parallel: int = 2
    enabled: bool = True
    base_url: str | None = None
    api_key_env: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelProfile":
        values = dict(raw)
        values["capabilities"] = tuple(values.get("capabilities") or ("general",))
        return cls(**values)

    def estimated_cost(self, node: WorkNode) -> float:
        return (node.estimated_input_tokens * self.input_cost_per_million +
                node.estimated_output_tokens * self.output_cost_per_million) / 1_000_000

    def validate(self) -> None:
        valid_providers = {
            "codex",
            "antigravity",
            "fake",
            "ollama",
            "openai-compatible",
            "deepseek",
            "openrouter",
            "vllm",
            "groq",
        }
        if not self.name or (self.provider not in valid_providers and not self.provider.startswith("openai")):
            raise ValueError(f"invalid model profile: {self.name!r} (unsupported provider {self.provider!r})")
        if self.max_parallel < 1:
            raise ValueError(f"{self.name}: max_parallel must be positive")
        if not 0 <= self.quality <= 1 or not 0 <= self.latency_score <= 1:
            raise ValueError(f"{self.name}: quality and latency_score must be between 0 and 1")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError(f"{self.name}: model costs cannot be negative")


@dataclass(frozen=True)
class ModelRoute:
    profile: str
    provider: str
    model: str | None
    estimated_cost_usd: float
    score: float


@dataclass
class NodeResult:
    status: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    memory_candidates: list[str] = field(default_factory=list)
    confidence: float = .5
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NodeResult":
        result = cls(**raw)
        result.validate()
        return result

    def validate(self) -> None:
        if self.status not in {"PASS", "FAIL", "BLOCKED"}:
            raise ValueError("node result status must be PASS, FAIL or BLOCKED")
        if not self.summary.strip():
            raise ValueError("node result summary is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("node result confidence must be between 0 and 1")
        for rel in [*self.artifacts, *self.deleted]:
            _safe_rel(rel)
