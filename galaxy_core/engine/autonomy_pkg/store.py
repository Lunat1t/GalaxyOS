"""SQLite storage for autonomy runs, nodes, attempts, and model metrics."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from galaxy_core.engine.autonomy_pkg.models import (
    NODE_STATUSES,
    BudgetLimits,
    BudgetUsage,
    ExecutionPlan,
    ModelRoute,
    NodeResult,
    utcnow,
)


class AutonomyStore:
    """Normalized durable state for DAG runs, attempts, approvals and model metrics."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "data" / "runtime" / "autonomy.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS autonomy_runs(
              id TEXT PRIMARY KEY, plan_json TEXT NOT NULL, status TEXT NOT NULL,
              approval_mode TEXT NOT NULL, budget_json TEXT NOT NULL, usage_json TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS autonomy_nodes(
              run_id TEXT NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, result_json TEXT, route_json TEXT,
              started_at TEXT, finished_at TEXT, error TEXT,
              PRIMARY KEY(run_id,node_id));
            CREATE TABLE IF NOT EXISTS autonomy_attempts(
              run_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              status TEXT NOT NULL, route_json TEXT, result_json TEXT, error TEXT,
              started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
              PRIMARY KEY(run_id,node_id,attempt));
            CREATE TABLE IF NOT EXISTS autonomy_approvals(
              run_id TEXT NOT NULL, node_id TEXT NOT NULL, decision TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,
              PRIMARY KEY(run_id,node_id));
            CREATE TABLE IF NOT EXISTS model_metrics(
              profile TEXT PRIMARY KEY, successes INTEGER NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0, total_latency REAL NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL);
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

    def create(self, plan: ExecutionPlan, budget: BudgetLimits, approval_mode: str) -> str:
        run_id = "RUN-" + uuid.uuid4().hex[:16].upper()
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO autonomy_runs VALUES(?,?,?,?,?,?,?,?,?)", (
                run_id, json.dumps(plan.to_dict(), ensure_ascii=False), "IN_PROGRESS", approval_mode,
                json.dumps(asdict(budget)), json.dumps(BudgetUsage().to_dict()), "", now, now))
            db.executemany("INSERT INTO autonomy_nodes(run_id,node_id,status) VALUES(?,?,'PENDING')",
                           [(run_id, node.id) for node in plan.nodes])
            db.commit()
        return run_id

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM autonomy_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            nodes = [dict(x) for x in db.execute(
                "SELECT * FROM autonomy_nodes WHERE run_id=? ORDER BY rowid", (run_id,))]
            approvals = [dict(x) for x in db.execute(
                "SELECT * FROM autonomy_approvals WHERE run_id=?", (run_id,))]
        result = dict(row)
        for key in ("plan_json", "budget_json", "usage_json"):
            result[key[:-5]] = json.loads(result.pop(key))
        for node in nodes:
            node["result"] = json.loads(node.pop("result_json")) if node["result_json"] else None
            node["route"] = json.loads(node.pop("route_json")) if node["route_json"] else None
        result["nodes"] = nodes
        result["approvals"] = approvals
        return result

    def set_run(self, run_id: str, status: str, usage: BudgetUsage, reason: str = "") -> None:
        with self.connect() as db:
            db.execute("UPDATE autonomy_runs SET status=?,usage_json=?,reason=?,updated_at=? WHERE id=?",
                       (status, json.dumps(usage.to_dict()), reason, utcnow(), run_id))

    def set_node(self, run_id: str, node_id: str, status: str, *, attempts: int | None = None,
                 result: NodeResult | None = None, route: ModelRoute | None = None,
                 error: str = "", started: bool = False, finished: bool = False) -> None:
        if status not in NODE_STATUSES:
            raise ValueError(status)
        fields = ["status=?", "error=?"]
        values: list[Any] = [status, error]
        if attempts is not None:
            fields.append("attempts=?"); values.append(attempts)
        if result is not None:
            fields.append("result_json=?"); values.append(json.dumps(asdict(result), ensure_ascii=False))
        if route is not None:
            fields.append("route_json=?"); values.append(json.dumps(asdict(route), ensure_ascii=False))
        if started:
            fields.append("started_at=?"); values.append(utcnow())
        if finished:
            fields.append("finished_at=?"); values.append(utcnow())
        values.extend([run_id, node_id])
        with self.connect() as db:
            db.execute(f"UPDATE autonomy_nodes SET {','.join(fields)} WHERE run_id=? AND node_id=?", values)

    def attempt(self, run_id: str, node_id: str, attempt: int, status: str, route: ModelRoute,
                result: NodeResult | None, error: str, started_at: str, finished_at: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO autonomy_attempts VALUES(?,?,?,?,?,?,?,?,?)", (
                run_id, node_id, attempt, status, json.dumps(asdict(route)),
                json.dumps(asdict(result), ensure_ascii=False) if result else None,
                error, started_at, finished_at))

    def approval(self, run_id: str, node_id: str, decision: str, note: str = "", by: str = "user") -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError(decision)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO autonomy_approvals VALUES(?,?,?,?,?,?)",
                       (run_id, node_id, decision, note, by, utcnow()))

    def metrics(self) -> dict[str, dict[str, float]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM model_metrics").fetchall()
        out = {}
        for row in rows:
            total = row["successes"] + row["failures"]
            out[row["profile"]] = {
                "success_rate": row["successes"] / total if total else .7,
                "latency_score": 1 / (1 + row["total_latency"] / total) if total else .5,
            }
        return out

    def record_metric(self, profile: str, success: bool, latency: float) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO model_metrics(profile,successes,failures,total_latency,updated_at)
              VALUES(?,?,?,?,?) ON CONFLICT(profile) DO UPDATE SET
              successes=successes+excluded.successes, failures=failures+excluded.failures,
              total_latency=total_latency+excluded.total_latency, updated_at=excluded.updated_at""",
              (profile, int(success), int(not success), max(0.0, latency), utcnow()))
