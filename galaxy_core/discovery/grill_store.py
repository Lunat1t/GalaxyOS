"""SQLite v3 additive persistence and recovery for Galaxy 3.1 grill-v1.

Preserves legacy sessions and tables (interview_sessions, interview_turns)
while additively introducing grill-v1 DAG tables with BEGIN IMMEDIATE transactions.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any

from galaxy_core.discovery.grill_dag import GrillDAG
from galaxy_core.discovery.grill_models import (
    GrillAlternative,
    GrillAnswerHistory,
    GrillConflict,
    GrillDependency,
    GrillFact,
    GrillNode,
    GrillQuestion,
    GrillRound,
    GrillSessionMeta,
    NodeStatus,
    SessionNotFoundError,
    SessionStatus,
    utcnow,
)


class GrillStore:
    """Additive SQLite v3 storage with atomic transactions and crash recovery."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "data" / "runtime" / "interviews.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create additive v3 tables if they do not already exist."""
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS interview_sessions(
              id TEXT PRIMARY KEY, project TEXT NOT NULL, initial_idea TEXT NOT NULL,
              depth TEXT NOT NULL, status TEXT NOT NULL, answers_json TEXT NOT NULL,
              assumptions_json TEXT NOT NULL, pending_json TEXT, brief_json TEXT,
              max_questions INTEGER NOT NULL, questions_asked INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS interview_turns(
              session_id TEXT NOT NULL, turn_no INTEGER NOT NULL,
              question_json TEXT NOT NULL, answer TEXT NOT NULL,
              quality TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(session_id,turn_no));

            CREATE TABLE IF NOT EXISTS grill_session_meta(
              session_id TEXT PRIMARY KEY,
              project TEXT NOT NULL,
              initial_idea TEXT NOT NULL,
              engine TEXT NOT NULL DEFAULT 'grill-v1',
              profile TEXT NOT NULL,
              mode TEXT NOT NULL,
              depth TEXT NOT NULL,
              status TEXT NOT NULL,
              budget_limit INTEGER NOT NULL,
              budget_used INTEGER NOT NULL DEFAULT 0,
              pause_reason TEXT,
              brief_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS grill_nodes(
              session_id TEXT NOT NULL,
              node_id TEXT NOT NULL,
              category TEXT NOT NULL,
              title TEXT NOT NULL,
              description TEXT,
              status TEXT NOT NULL,
              question_json TEXT,
              answer TEXT,
              answer_option_id TEXT,
              answer_quality TEXT,
              is_custom_answer INTEGER NOT NULL DEFAULT 0,
              assumption_text TEXT,
              assumption_verification TEXT,
              research_task TEXT,
              research_result TEXT,
              dependencies_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              answered_at TEXT,
              PRIMARY KEY(session_id, node_id));

            CREATE TABLE IF NOT EXISTS grill_dependencies(
              session_id TEXT NOT NULL,
              parent_id TEXT NOT NULL,
              child_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(session_id, parent_id, child_id));

            CREATE TABLE IF NOT EXISTS grill_rounds(
              session_id TEXT NOT NULL,
              round_no INTEGER NOT NULL,
              status TEXT NOT NULL,
              questions_json TEXT NOT NULL,
              answers_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              completed_at TEXT,
              PRIMARY KEY(session_id, round_no));

            CREATE TABLE IF NOT EXISTS grill_facts(
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              node_id TEXT,
              fact TEXT NOT NULL,
              source_type TEXT NOT NULL,
              source_ref TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 1.0,
              verified_at TEXT NOT NULL,
              actor TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS grill_conflicts(
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              node_ids_json TEXT NOT NULL,
              description TEXT NOT NULL,
              status TEXT NOT NULL,
              resolution TEXT,
              accepted_rationale TEXT,
              detected_at TEXT NOT NULL,
              resolved_at TEXT);

            CREATE TABLE IF NOT EXISTS grill_answer_history(
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              node_id TEXT NOT NULL,
              answer TEXT NOT NULL,
              quality TEXT,
              superseded_at TEXT NOT NULL,
              actor TEXT NOT NULL,
              rationale TEXT);
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

    @contextmanager
    def transaction(self, db: sqlite3.Connection):
        """Execute operations inside an atomic BEGIN IMMEDIATE transaction."""
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def is_legacy_session(self, session_id: str) -> bool:
        """Check if session is a legacy v2.0 session lacking grill metadata."""
        with self.connect() as db:
            has_meta = db.execute(
                "SELECT 1 FROM grill_session_meta WHERE session_id=?", (session_id,)
            ).fetchone()
            if has_meta:
                return False
            has_legacy = db.execute(
                "SELECT 1 FROM interview_sessions WHERE id=?", (session_id,)
            ).fetchone()
            return has_legacy is not None

    def get_legacy_session(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve legacy session data from interview_sessions and interview_turns."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM interview_sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                return None
            turns = [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM interview_turns WHERE session_id=? ORDER BY turn_no",
                    (session_id,),
                )
            ]
        result = dict(row)
        for source, target, fallback in (
            ("answers_json", "answers", {}),
            ("assumptions_json", "assumptions", []),
            ("pending_json", "pending", None),
            ("brief_json", "brief", None),
        ):
            raw = result.pop(source)
            result[target] = json.loads(raw) if raw else fallback
        for turn in turns:
            turn["question"] = json.loads(turn.pop("question_json"))
        result["turns"] = turns
        return result

    def create_session(
        self,
        meta: GrillSessionMeta,
        dag: GrillDAG,
        initial_round: GrillRound | None = None,
    ) -> None:
        """Atomically persist a newly created grill-v1 session."""
        meta.validate()
        dag.check_cycles()
        for node in dag.nodes.values():
            node.validate()
        for dep in dag.dependencies:
            dep.validate()
        if initial_round:
            initial_round.validate()

        with self.connect() as db:
            with self.transaction(db):
                # 1. Insert session meta
                brief_json = json.dumps(meta.brief, ensure_ascii=False) if meta.brief else None
                db.execute(
                    """INSERT INTO grill_session_meta VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        meta.session_id,
                        meta.project,
                        meta.initial_idea,
                        meta.engine,
                        meta.profile,
                        meta.mode,
                        meta.depth,
                        meta.status,
                        meta.budget_limit,
                        meta.budget_used,
                        meta.pause_reason,
                        brief_json,
                        meta.created_at,
                        meta.updated_at,
                    ),
                )

                # 2. Insert DAG nodes
                for node in dag.nodes.values():
                    q_json = (
                        json.dumps(node.question.to_dict(), ensure_ascii=False)
                        if node.question
                        else None
                    )
                    db.execute(
                        """INSERT INTO grill_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            meta.session_id,
                            node.id,
                            node.category,
                            node.title,
                            node.description,
                            node.status,
                            q_json,
                            node.answer,
                            node.answer_option_id,
                            node.answer_quality,
                            1 if node.is_custom_answer else 0,
                            node.assumption_text,
                            node.assumption_verification,
                            node.research_task,
                            node.research_result,
                            json.dumps(node.dependencies, ensure_ascii=False),
                            node.created_at,
                            node.updated_at,
                            node.answered_at,
                        ),
                    )

                # 3. Insert dependencies
                for dep in dag.dependencies:
                    db.execute(
                        """INSERT OR IGNORE INTO grill_dependencies VALUES(?,?,?,?)""",
                        (meta.session_id, dep.parent_id, dep.child_id, dep.created_at),
                    )

                # 4. Insert initial round if present
                if initial_round:
                    q_list = [q.to_dict() for q in initial_round.questions]
                    db.execute(
                        """INSERT INTO grill_rounds VALUES(?,?,?,?,?,?,?)""",
                        (
                            meta.session_id,
                            initial_round.round_no,
                            initial_round.status,
                            json.dumps(q_list, ensure_ascii=False),
                            json.dumps(initial_round.answers, ensure_ascii=False),
                            initial_round.created_at,
                            initial_round.completed_at,
                        ),
                    )

                # 5. Insert facts if present
                for fact in dag.facts.values():
                    db.execute(
                        """INSERT INTO grill_facts VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            fact.id,
                            meta.session_id,
                            fact.node_id,
                            fact.fact,
                            fact.source_type,
                            fact.source_ref,
                            fact.confidence,
                            fact.verified_at,
                            fact.actor,
                        ),
                    )

                # 6. Insert conflicts if present
                for conflict in dag.conflicts.values():
                    db.execute(
                        """INSERT INTO grill_conflicts VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            conflict.id,
                            meta.session_id,
                            json.dumps(conflict.node_ids, ensure_ascii=False),
                            conflict.description,
                            conflict.status,
                            conflict.resolution,
                            conflict.accepted_rationale,
                            conflict.detected_at,
                            conflict.resolved_at,
                        ),
                    )

    def save_session(
        self,
        meta: GrillSessionMeta,
        dag: GrillDAG,
        current_round: GrillRound | None = None,
        history: list[GrillAnswerHistory] | None = None,
    ) -> None:
        """Atomically persist session state, DAG nodes, dependencies, round, and history."""
        meta.validate()
        dag.check_cycles()
        for node in dag.nodes.values():
            node.validate()
        for dep in dag.dependencies:
            dep.validate()
        if current_round:
            current_round.validate()
        if history:
            for h in history:
                h.validate()

        with self.connect() as db:
            with self.transaction(db):
                now = utcnow()
                meta.updated_at = now
                brief_json = json.dumps(meta.brief, ensure_ascii=False) if meta.brief else None

                # 1. Update session meta
                db.execute(
                    """UPDATE grill_session_meta SET
                        project=?,
                        initial_idea=?,
                        engine=?,
                        profile=?,
                        mode=?,
                        depth=?,
                        status=?,
                        budget_limit=?,
                        budget_used=?,
                        pause_reason=?,
                        brief_json=?,
                        updated_at=?
                        WHERE session_id=?""",
                    (
                        meta.project,
                        meta.initial_idea,
                        meta.engine,
                        meta.profile,
                        meta.mode,
                        meta.depth,
                        meta.status,
                        meta.budget_limit,
                        meta.budget_used,
                        meta.pause_reason,
                        brief_json,
                        meta.updated_at,
                        meta.session_id,
                    ),
                )

                # 2. Upsert DAG nodes
                for node in dag.nodes.values():
                    q_json = (
                        json.dumps(node.question.to_dict(), ensure_ascii=False)
                        if node.question
                        else None
                    )
                    db.execute(
                        """INSERT INTO grill_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(session_id, node_id) DO UPDATE SET
                          category=excluded.category,
                          title=excluded.title,
                          description=excluded.description,
                          status=excluded.status,
                          question_json=excluded.question_json,
                          answer=excluded.answer,
                          answer_option_id=excluded.answer_option_id,
                          answer_quality=excluded.answer_quality,
                          is_custom_answer=excluded.is_custom_answer,
                          assumption_text=excluded.assumption_text,
                          assumption_verification=excluded.assumption_verification,
                          research_task=excluded.research_task,
                          research_result=excluded.research_result,
                          dependencies_json=excluded.dependencies_json,
                          updated_at=excluded.updated_at,
                          answered_at=excluded.answered_at""",
                        (
                            meta.session_id,
                            node.id,
                            node.category,
                            node.title,
                            node.description,
                            node.status,
                            q_json,
                            node.answer,
                            node.answer_option_id,
                            node.answer_quality,
                            1 if node.is_custom_answer else 0,
                            node.assumption_text,
                            node.assumption_verification,
                            node.research_task,
                            node.research_result,
                            json.dumps(node.dependencies, ensure_ascii=False),
                            node.created_at,
                            node.updated_at,
                            node.answered_at,
                        ),
                    )

                # 3. Upsert dependencies
                for dep in dag.dependencies:
                    db.execute(
                        """INSERT OR IGNORE INTO grill_dependencies VALUES(?,?,?,?)""",
                        (meta.session_id, dep.parent_id, dep.child_id, dep.created_at),
                    )

                # 4. Upsert current round if present
                if current_round:
                    q_list = [q.to_dict() for q in current_round.questions]
                    db.execute(
                        """INSERT INTO grill_rounds VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(session_id, round_no) DO UPDATE SET
                          status=excluded.status,
                          questions_json=excluded.questions_json,
                          answers_json=excluded.answers_json,
                          completed_at=excluded.completed_at""",
                        (
                            meta.session_id,
                            current_round.round_no,
                            current_round.status,
                            json.dumps(q_list, ensure_ascii=False),
                            json.dumps(current_round.answers, ensure_ascii=False),
                            current_round.created_at,
                            current_round.completed_at,
                        ),
                    )

                # 5. Insert history records
                if history:
                    for h in history:
                        db.execute(
                            """INSERT OR IGNORE INTO grill_answer_history VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                h.id,
                                meta.session_id,
                                h.node_id,
                                h.answer,
                                h.quality,
                                h.superseded_at,
                                h.actor,
                                h.rationale,
                            ),
                        )

                # 6. Upsert facts
                for fact in dag.facts.values():
                    db.execute(
                        """INSERT INTO grill_facts VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          fact=excluded.fact,
                          confidence=excluded.confidence,
                          verified_at=excluded.verified_at,
                          actor=excluded.actor""",
                        (
                            fact.id,
                            meta.session_id,
                            fact.node_id,
                            fact.fact,
                            fact.source_type,
                            fact.source_ref,
                            fact.confidence,
                            fact.verified_at,
                            fact.actor,
                        ),
                    )

                # 7. Upsert conflicts
                for conflict in dag.conflicts.values():
                    db.execute(
                        """INSERT INTO grill_conflicts VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          status=excluded.status,
                          resolution=excluded.resolution,
                          accepted_rationale=excluded.accepted_rationale,
                          resolved_at=excluded.resolved_at""",
                        (
                            conflict.id,
                            meta.session_id,
                            json.dumps(conflict.node_ids, ensure_ascii=False),
                            conflict.description,
                            conflict.status,
                            conflict.resolution,
                            conflict.accepted_rationale,
                            conflict.detected_at,
                            conflict.resolved_at,
                        ),
                    )

    def load_session(
        self, session_id: str
    ) -> tuple[GrillSessionMeta, GrillDAG, list[GrillRound]] | None:
        """Load session, DAG, and rounds from SQLite, executing deterministic recovery if needed."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM grill_session_meta WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row:
                return None

            meta_dict = dict(row)
            brief_raw = meta_dict.pop("brief_json", None)
            meta_dict["brief"] = json.loads(brief_raw) if brief_raw else None
            meta = GrillSessionMeta.from_dict(meta_dict)

            # Load nodes
            nodes_rows = db.execute(
                "SELECT * FROM grill_nodes WHERE session_id=?", (session_id,)
            ).fetchall()
            dag = GrillDAG()
            for r in nodes_rows:
                nd = dict(r)
                q_raw = nd.pop("question_json", None)
                nd["question"] = json.loads(q_raw) if q_raw else None
                deps_raw = nd.pop("dependencies_json", "[]")
                nd["dependencies"] = json.loads(deps_raw) if deps_raw else []
                node = GrillNode.from_dict(nd)
                dag.add_node(node)

            # Load dependencies
            deps_rows = db.execute(
                "SELECT * FROM grill_dependencies WHERE session_id=?", (session_id,)
            ).fetchall()
            for r in deps_rows:
                dep = GrillDependency(
                    parent_id=r["parent_id"],
                    child_id=r["child_id"],
                    created_at=r["created_at"],
                )
                dag.dependencies.append(dep)
                if dep.child_id in dag.nodes:
                    child = dag.nodes[dep.child_id]
                    if dep.parent_id not in child.dependencies:
                        child.dependencies.append(dep.parent_id)

            # Load facts
            fact_rows = db.execute(
                "SELECT * FROM grill_facts WHERE session_id=?", (session_id,)
            ).fetchall()
            for r in fact_rows:
                fact = GrillFact.from_dict(dict(r))
                dag.facts[fact.id] = fact

            # Load conflicts
            conflict_rows = db.execute(
                "SELECT * FROM grill_conflicts WHERE session_id=?", (session_id,)
            ).fetchall()
            for r in conflict_rows:
                cd = dict(r)
                nids_raw = cd.pop("node_ids_json", "[]")
                cd["node_ids"] = json.loads(nids_raw) if nids_raw else []
                conflict = GrillConflict.from_dict(cd)
                dag.conflicts[conflict.id] = conflict

            # Load rounds
            rounds_rows = db.execute(
                "SELECT * FROM grill_rounds WHERE session_id=? ORDER BY round_no",
                (session_id,),
            ).fetchall()
            rounds: list[GrillRound] = []
            for r in rounds_rows:
                rd = dict(r)
                qs_raw = rd.pop("questions_json", "[]")
                rd["questions"] = json.loads(qs_raw) if qs_raw else []
                ans_raw = rd.pop("answers_json", "{}")
                rd["answers"] = json.loads(ans_raw) if ans_raw else {}
                rounds.append(GrillRound.from_dict(rd))

        # Crash recovery protocol:
        needs_recovery_save = False

        # 1. If meta.status is DRAFT_READY, verify that all 5 completion invariants actually hold.
        #    If not (e.g. an uncompleted revision occurred before crash), reset to DISCOVERY.
        if meta.status == SessionStatus.DRAFT_READY:
            is_ready, _ = dag.is_draft_ready(mode=meta.mode)
            if not is_ready:
                meta.status = SessionStatus.DISCOVERY
                meta.brief = None
                needs_recovery_save = True

        # 2. Check if the latest round was interrupted (status is active).
        #    If active, ensure nodes waiting for answers in this round remain at consistent
        #    frontier/blocked status and do not retain partial unconfirmed state.
        if rounds and rounds[-1].status == "active":
            latest_round = rounds[-1]
            for q in latest_round.questions:
                node = dag.get_node(q.node_id)
                if node and node.status in NodeStatus.RESOLVED and not any(
                    r.status == "completed" and (q.id in r.answers or q.node_id in r.answers)
                    for r in rounds[:-1]
                ):
                    node.status = NodeStatus.BLOCKED
                    node.answer = None
                    node.answer_option_id = None
                    node.answer_quality = None
                    node.is_custom_answer = False
                    node.answered_at = None
                    needs_recovery_save = True

        # 3. Always recalculate frontier to ensure full consistency
        dag.recalculate_frontier()

        if needs_recovery_save:
            self.save_session(meta, dag)

        return meta, dag, rounds

    def save_round(self, round_obj: GrillRound) -> None:
        """Atomically persist or update a round."""
        round_obj.validate()
        with self.connect() as db:
            with self.transaction(db):
                q_list = [q.to_dict() for q in round_obj.questions]
                db.execute(
                    """INSERT INTO grill_rounds VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(session_id, round_no) DO UPDATE SET
                      status=excluded.status,
                      questions_json=excluded.questions_json,
                      answers_json=excluded.answers_json,
                      completed_at=excluded.completed_at""",
                    (
                        round_obj.session_id,
                        round_obj.round_no,
                        round_obj.status,
                        json.dumps(q_list, ensure_ascii=False),
                        json.dumps(round_obj.answers, ensure_ascii=False),
                        round_obj.created_at,
                        round_obj.completed_at,
                    ),
                )

    def save_fact(self, fact: GrillFact) -> None:
        """Atomically persist or update a verified fact."""
        fact.validate()
        with self.connect() as db:
            with self.transaction(db):
                db.execute(
                    """INSERT INTO grill_facts VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      fact=excluded.fact,
                      confidence=excluded.confidence,
                      verified_at=excluded.verified_at,
                      actor=excluded.actor""",
                    (
                        fact.id,
                        fact.session_id,
                        fact.node_id,
                        fact.fact,
                        fact.source_type,
                        fact.source_ref,
                        fact.confidence,
                        fact.verified_at,
                        fact.actor,
                    ),
                )

    def save_conflict(self, conflict: GrillConflict) -> None:
        """Atomically persist or update a conflict."""
        conflict.validate()
        with self.connect() as db:
            with self.transaction(db):
                db.execute(
                    """INSERT INTO grill_conflicts VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      status=excluded.status,
                      resolution=excluded.resolution,
                      accepted_rationale=excluded.accepted_rationale,
                      resolved_at=excluded.resolved_at""",
                    (
                        conflict.id,
                        conflict.session_id,
                        json.dumps(conflict.node_ids, ensure_ascii=False),
                        conflict.description,
                        conflict.status,
                        conflict.resolution,
                        conflict.accepted_rationale,
                        conflict.detected_at,
                        conflict.resolved_at,
                    ),
                )

    def get_latest_round(self, session_id: str) -> GrillRound | None:
        """Return the latest round for session_id."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM grill_rounds WHERE session_id=? ORDER BY round_no DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            rd = dict(row)
            qs_raw = rd.pop("questions_json", "[]")
            rd["questions"] = json.loads(qs_raw) if qs_raw else []
            ans_raw = rd.pop("answers_json", "{}")
            rd["answers"] = json.loads(ans_raw) if ans_raw else {}
            return GrillRound.from_dict(rd)

    def next_round_no(self, session_id: str) -> int:
        """Return the next sequential round number for session_id."""
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(round_no), 0) + 1 FROM grill_rounds WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return int(row[0]) if row else 1

    def get_answer_history(self, session_id: str) -> list[GrillAnswerHistory]:
        """Return immutable answer revision history for session_id."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM grill_answer_history WHERE session_id=? ORDER BY superseded_at, id",
                (session_id,),
            ).fetchall()
            return [GrillAnswerHistory.from_dict(dict(r)) for r in rows]

    def delete_session(self, session_id: str) -> None:
        """Controlled cascading deletion of session data across all grill tables."""
        with self.connect() as db:
            with self.transaction(db):
                db.execute("DELETE FROM grill_answer_history WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM grill_conflicts WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM grill_facts WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM grill_rounds WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM grill_dependencies WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM grill_nodes WHERE session_id=?", (session_id,))
                db.execute("DELETE FROM grill_session_meta WHERE session_id=?", (session_id,))

    def export_session(self, session_id: str) -> dict[str, Any] | None:
        """Export complete sanitized session payload."""
        result = self.load_session(session_id)
        if not result:
            return None
        meta, dag, rounds = result
        history = self.get_answer_history(session_id)
        return {
            "session": meta.to_dict(),
            "dag": dag.to_dict(),
            "rounds": [r.to_dict() for r in rounds],
            "history": [h.to_dict() for h in history],
        }
