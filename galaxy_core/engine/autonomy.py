"""Galaxy 2.0 autonomous DAG runtime.

This module acts as a backward-compatible facade delegating to the modular
`galaxy_core.engine.autonomy_pkg` submodules (models, router, store, workspace, engine).
"""
from __future__ import annotations

from galaxy_core.engine.autonomy_pkg import (
    GENERATED_DIRS,
    NODE_STATUSES,
    RISK_LEVELS,
    RUN_TERMINAL,
    TERMINAL_NODE_STATUSES,
    AdaptiveModelRouter,
    AutonomousEngine,
    AutonomyStore,
    BudgetExceeded,
    BudgetLedger,
    BudgetLimits,
    BudgetUsage,
    ExecutionPlan,
    Executor,
    ModelProfile,
    ModelRoute,
    NodeResult,
    NodeWorkspace,
    WorkNode,
    _ancestors,
    _clone_or_copy,
    _file_hash,
    _safe_rel,
    utcnow,
)

__all__ = [
    "GENERATED_DIRS",
    "NODE_STATUSES",
    "RISK_LEVELS",
    "RUN_TERMINAL",
    "TERMINAL_NODE_STATUSES",
    "BudgetExceeded",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetUsage",
    "ExecutionPlan",
    "ModelProfile",
    "ModelRoute",
    "NodeResult",
    "WorkNode",
    "_ancestors",
    "_safe_rel",
    "utcnow",
    "AdaptiveModelRouter",
    "AutonomyStore",
    "NodeWorkspace",
    "_clone_or_copy",
    "_file_hash",
    "Executor",
    "AutonomousEngine",
]
