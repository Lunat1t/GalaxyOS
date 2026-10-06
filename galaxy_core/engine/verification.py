"""Evidence-backed verification contracts and explicitly promoted learned rules."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationStore:
    CHECKS = {"require_command", "require_evidence"}

    def __init__(self, root: str | Path):
        self.path = Path(root) / "data" / "runtime" / "verification.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, task_type TEXT NOT NULL,
                check_kind TEXT NOT NULL, match_text TEXT NOT NULL,
                status TEXT NOT NULL, episode_ids_json TEXT NOT NULL,
                evaluation_json TEXT, promoted_by TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rules_active ON rules(project,task_type,status);
            CREATE TABLE IF NOT EXISTS checks (
                run_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                check_index INTEGER NOT NULL, command TEXT NOT NULL, exit_code INTEGER,
                status TEXT NOT NULL, log_path TEXT NOT NULL, log_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id,node_id,attempt,check_index)
            );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def propose(self, *, project: str, task_type: str, check_kind: str,
                match_text: str = "", episode_ids: list[str] | None = None) -> str:
        if task_type not in {"bugfix", "feature", "general"} or check_kind not in self.CHECKS:
            raise ValueError("unsupported task type or check")
        if check_kind == "require_command" and not match_text.strip():
            raise ValueError("require_command needs a literal command fragment")
        if not project.strip():
            raise ValueError("project is required")
        evidence = sorted(set(episode_ids or []))
        rid = "VR-" + hashlib.sha256(json.dumps(
            [project, task_type, check_kind, match_text.casefold().strip(), evidence]).encode()).hexdigest()[:16]
        now = _now()
        with self._db() as db:
            db.execute("""INSERT OR IGNORE INTO rules VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (rid, project, task_type, check_kind, match_text.strip(), "candidate",
                        json.dumps(evidence), None, None, now, now))
        return rid

    def suggest_from_failures(self, experience, *, project: str, lesson: str,
                              task_type: str, check_kind: str, match_text: str = "",
                              minimum: int = 3) -> str:
        # Explicit lesson and executable check are required: text similarity cannot invent a safe rule.
        if minimum < 2:
            raise ValueError("minimum must be at least 2")
        with experience._db() as db:
            rows = db.execute("""SELECT id,run_id,evidence_json FROM episodes
                WHERE project=? AND status='active' AND outcome='failure'
                AND visibility='project' AND lower(trim(lesson))=?""",
                (project, lesson.strip().casefold())).fetchall()
        cited = [r["id"] for r in rows if json.loads(r["evidence_json"])]
        if len({r["run_id"] for r in rows if r["id"] in cited}) < minimum:
            raise ValueError("insufficient independent evidenced failure runs")
        return self.propose(project=project, task_type=task_type, check_kind=check_kind,
                            match_text=match_text, episode_ids=cited)

    def get(self, rid: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
        if not row:
            raise KeyError(rid)
        item = dict(row)
        item["episode_ids"] = json.loads(item.pop("episode_ids_json"))
        item["evaluation"] = json.loads(item.pop("evaluation_json")) if item["evaluation_json"] else None
        return item

    def active(self, project: str, task_type: str) -> list[dict[str, Any]]:
        with self._db() as db:
            ids = [r["id"] for r in db.execute("""SELECT id FROM rules WHERE project=?
                AND task_type IN (?, 'general') AND status='active' ORDER BY id""",
                (project, task_type)).fetchall()]
        return [self.get(rid) for rid in ids]

    @staticmethod
    def check_plan(rule: dict, plan) -> bool:
        if rule["check_kind"] == "require_evidence":
            return True  # Evaluated against actual NodeResult after execution.
        match = rule["match_text"].casefold()
        writers = {n.id for n in plan.nodes if n.risk == "write"}
        if not writers:
            return True
        by_id = {n.id: n for n in plan.nodes}
        for node in plan.nodes:
            if node.capability not in {"qa", "verification"}:
                continue
            ancestors, pending = set(), list(node.dependencies)
            while pending:
                dep = pending.pop()
                if dep not in ancestors:
                    ancestors.add(dep)
                    pending.extend(by_id[dep].dependencies)
            if writers <= ancestors and any(match in cmd.casefold() for cmd in node.verification_commands):
                return True
        return False

    def evaluate(self, rid: str, cases: list[tuple[Any, bool, list[str]]]) -> dict:
        """Replay labeled plans. `should_block` must come from independent review."""
        rule = self.get(rid)
        if rule["status"] != "candidate":
            raise ValueError("only candidates can be evaluated")
        if len(cases) < 2 or {label for _, label, _ in cases} != {True, False}:
            raise ValueError("evaluation requires allowed and blocked examples")
        outcomes = []
        for plan, should_block, result_evidence in cases:
            plan.validate()
            if plan.project != rule["project"]:
                raise ValueError("evaluation project mismatch")
            predicted = (not self.check_plan(rule, plan)) if rule["check_kind"] == "require_command" else not bool(result_evidence)
            outcomes.append((predicted, should_block))
        report = {"examples": len(cases), "false_blocks": sum(a and not b for a, b in outcomes),
                  "missed_blocks": sum(not a and b for a, b in outcomes),
                  "correct": sum(a == b for a, b in outcomes)}
        with self._db() as db:
            db.execute("UPDATE rules SET evaluation_json=?,updated_at=? WHERE id=?",
                       (json.dumps(report), _now(), rid))
        return report

    def promote(self, rid: str, *, by: str) -> None:
        rule = self.get(rid)
        report = rule["evaluation"]
        if (not by.strip() or rule["status"] != "candidate" or not report
                or report["false_blocks"] or report["missed_blocks"]):
            raise ValueError("human attribution and replay without classification errors are required")
        with self._db() as db:
            db.execute("UPDATE rules SET status='active',promoted_by=?,updated_at=? WHERE id=?",
                       (by, _now(), rid))

    def disable(self, rid: str) -> None:
        self.get(rid)
        with self._db() as db:
            db.execute("UPDATE rules SET status='disabled',updated_at=? WHERE id=?", (_now(), rid))

    def record_check(self, *, run_id: str, node_id: str, attempt: int, index: int,
                     command: str, exit_code: int | None, status: str,
                     log_path: str, output: bytes) -> None:
        with self._db() as db:
            db.execute("""INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (run_id, node_id, attempt, index, command, exit_code, status,
                        log_path, hashlib.sha256(output).hexdigest(), _now()))

    def trusted_evidence(self, root: str | Path, run_id: str, node_id: str, attempt: int) -> bool:
        """Require actual passing checks whose log bytes still match the ledger."""
        with self._db() as db:
            rows = db.execute("""SELECT * FROM checks WHERE run_id=? AND node_id=?
                AND attempt=? ORDER BY check_index""", (run_id, node_id, attempt)).fetchall()
        if not rows:
            return False
        base = Path(root).resolve()
        for row in rows:
            path = (base / row["log_path"]).resolve()
            if (not path.is_relative_to(base / "data" / "runs" / run_id / node_id)
                    or not path.is_file() or row["status"] != "passed" or row["exit_code"] != 0):
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["log_sha256"]:
                return False
        return True
