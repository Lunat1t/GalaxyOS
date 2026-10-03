"""Offline sequential-task retrieval experiment; does not measure agent success."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import time
from typing import Any

from galaxy_core.context import ContextCompiler


def _episode_id(run_id: str, node_id: str) -> str:
    return "EXP-" + hashlib.sha256(f"{run_id}\0{node_id}".encode()).hexdigest()[:16]


def evaluate_continuity(dataset: dict[str, Any], repo: str | Path, *, budget_tokens: int = 5000,
                        max_files: int = 16) -> dict[str, Any]:
    """Replay ordered tasks with isolated baseline and experience-enabled stores.

    `gold_prior` refers only to earlier observations by (run_id,node_id). Input
    annotations are not passed into retrieval; output is a retrieval diagnostic.
    """
    tasks = dataset.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("dataset requires a nonempty tasks list")
    project = str(dataset.get("project") or "continuity-eval")
    details = []
    with tempfile.TemporaryDirectory(prefix="galaxy-continuity-") as tmp:
        enabled = ContextCompiler(Path(tmp) / "enabled", repo, project)
        baseline = ContextCompiler(Path(tmp) / "baseline", repo, project)
        observed = set()
        for index, case in enumerate(tasks):
            task = case["task"]
            role = case.get("role", "")
            expected = {_episode_id(*pair) for pair in case.get("gold_prior", [])}
            if not expected <= observed:
                raise ValueError(f"task {index} references future or missing observations")
            start = time.perf_counter()
            plain = baseline.compile(task, budget_tokens=budget_tokens, max_files=max_files, role=role)
            baseline_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            packet = enabled.compile(task, budget_tokens=budget_tokens, max_files=max_files, role=role)
            enabled_ms = (time.perf_counter() - start) * 1000
            returned = {e["id"] for e in packet.experiences}
            details.append({"id": case.get("id", str(index)), "gold_prior": len(expected),
                            "recalled_prior": len(returned & expected),
                            "extra_episodes": len(returned - expected),
                            "baseline_tokens": plain.estimated_tokens,
                            "galaxy_tokens": packet.estimated_tokens,
                            "baseline_ms": round(baseline_ms, 3),
                            "galaxy_ms": round(enabled_ms, 3)})
            observation = case.get("observation")
            if observation:
                record = dict(observation)
                record.setdefault("project", project)
                record.setdefault("task", task)
                record.setdefault("role", role or "agent")
                record.setdefault("agent_id", record["role"].lower())
                eid = enabled.experience.record(**record)
                observed.add(eid)
    gold = sum(x["gold_prior"] for x in details)
    return {"project": project, "tasks": len(details), "retrieval_recall":
            round(sum(x["recalled_prior"] for x in details) / gold, 4) if gold else None,
            "baseline_tokens": sum(x["baseline_tokens"] for x in details),
            "galaxy_tokens": sum(x["galaxy_tokens"] for x in details),
            "baseline_ms": round(sum(x["baseline_ms"] for x in details), 3),
            "galaxy_ms": round(sum(x["galaxy_ms"] for x in details), 3),
            "details": details,
            "limitations": "Offline annotated retrieval only; no task success, model cost or behavioral improvement measured."}
