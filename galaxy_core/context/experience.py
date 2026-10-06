"""Evidence-linked, model-independent experience shared across agent executions.

Episodes are observations. A suggested lesson is never promoted to a verified
project fact merely because a model proposed it. Access is checked before ranking.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]{3,}", value.casefold(), re.UNICODE))


class ExperienceStore:
    def __init__(self, root: str | Path, project_root: str | Path | None = None):
        # Isolate two repositories with the same user-facing project name.
        repository = Path(project_root or root).resolve()
        scope = hashlib.sha256(str(repository).encode()).hexdigest()[:20]
        self.path = Path(root) / "data" / "runtime" / "experience" / f"{scope}.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL,
                role TEXT NOT NULL, agent_id TEXT NOT NULL, run_id TEXT NOT NULL,
                node_id TEXT NOT NULL, outcome TEXT NOT NULL, summary TEXT NOT NULL,
                lesson TEXT NOT NULL, evidence_json TEXT NOT NULL,
                visibility TEXT NOT NULL, owner_id TEXT, team_id TEXT,
                confidence REAL NOT NULL, verified INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id,node_id)
            );
            CREATE INDEX IF NOT EXISTS idx_experience_project ON episodes(project,status,visibility);
            CREATE TABLE IF NOT EXISTS episode_terms (
                episode_id TEXT NOT NULL REFERENCES episodes(id), term TEXT NOT NULL,
                PRIMARY KEY(episode_id,term)
            );
            CREATE INDEX IF NOT EXISTS idx_episode_terms_term ON episode_terms(term,episode_id);
            CREATE TABLE IF NOT EXISTS ledger_state (
                key TEXT PRIMARY KEY, revision INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO ledger_state(key,revision) VALUES ('episodes',0);
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

    def record(self, *, project: str, task: str, role: str, agent_id: str,
               run_id: str, node_id: str, outcome: str, summary: str, lesson: str = "",
               evidence: list[str] | None = None, visibility: str = "project",
               owner_id: str | None = None, team_id: str | None = None,
               confidence: float = .5, verified: bool = False) -> str:
        if outcome not in {"success", "failure", "blocked"}:
            raise ValueError("outcome must be success, failure or blocked")
        if visibility not in {"project", "private", "team"}:
            raise ValueError("invalid visibility")
        if (visibility == "private" and not owner_id) or (visibility == "team" and not team_id):
            raise ValueError("private/team experience requires an owner/team")
        if not all(x.strip() for x in (project, task, role, agent_id, run_id, node_id, summary)):
            raise ValueError("experience requires project, task, role, identity, run, node and summary")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        evidence = [str(x).strip()[:500] for x in (evidence or []) if str(x).strip()][:12]
        # Verification requires an inspectable source; a claimed success is insufficient.
        verified = bool(verified and outcome == "success" and evidence)
        eid = "EXP-" + hashlib.sha256(f"{run_id}\0{node_id}".encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            cur = db.execute("""INSERT OR IGNORE INTO episodes VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, project, task, role, agent_id, run_id, node_id, outcome,
                 summary[:2000], lesson[:1200], json.dumps(evidence, ensure_ascii=False),
                 visibility, owner_id, team_id, confidence, int(verified), "active", now, now))
            if cur.rowcount:
                terms = _terms(" ".join((task, summary, lesson)))
                db.executemany("INSERT INTO episode_terms(episode_id,term) VALUES (?,?)",
                               [(eid, term) for term in terms])
                db.execute("UPDATE ledger_state SET revision=revision+1 WHERE key='episodes'")
        return eid

    def retract(self, eid: str) -> None:
        with self._db() as db:
            cur = db.execute("UPDATE episodes SET status='retracted',updated_at=? WHERE id=? AND status='active'",
                             (datetime.now(timezone.utc).isoformat(), eid))
            if cur.rowcount:
                db.execute("UPDATE ledger_state SET revision=revision+1 WHERE key='episodes'")
            elif not db.execute("SELECT 1 FROM episodes WHERE id=?", (eid,)).fetchone():
                raise KeyError(eid)

    def signature(self) -> str:
        with self._db() as db:
            return str(db.execute("SELECT revision FROM ledger_state WHERE key='episodes'").fetchone()[0])

    def relevant(self, task: str, *, project: str, role: str = "",
                 owner_id: str | None = None, team_id: str | None = None,
                 limit: int = 5, budget_tokens: int = 650) -> list[dict]:
        # The SQL predicate is the authority boundary; unauthorized rows never enter ranking.
        query = _terms(task)
        if not query:
            return []
        terms = sorted(query)[:32]
        placeholders = ",".join("?" for _ in terms)
        with self._db() as db:
            rows = db.execute(f"""SELECT DISTINCT e.* FROM episodes e
                JOIN episode_terms t ON t.episode_id=e.id
                WHERE t.term IN ({placeholders}) AND e.project=? AND e.status='active'
                AND (e.visibility='project' OR (e.visibility='private' AND e.owner_id=?)
                     OR (e.visibility='team' AND e.team_id=?))""",
                (*terms, project, owner_id, team_id)).fetchall()
        ranked = []
        for row in rows:
            item = dict(row)
            overlap = len(query & _terms(" ".join((item["task"], item["lesson"], item["summary"]))))
            if not overlap:
                continue
            score = overlap / max(1, len(query)) + .12 * (item["role"].casefold() == role.casefold())
            score += .08 * item["verified"] + .04 * item["confidence"]
            ranked.append((score, item))
        ranked.sort(key=lambda x: (-x[0], x[1]["id"]))
        selected, used = [], 0
        for score, row in ranked:
            item = {k: row[k] for k in ("id", "role", "agent_id", "outcome", "summary", "lesson",
                                              "run_id", "node_id", "visibility", "confidence", "verified")}
            item["evidence"] = json.loads(row["evidence_json"])[:3]
            item["score"] = round(score, 4)
            cost = max(1, len(json.dumps(item, ensure_ascii=False)) // 4)
            if used + cost > budget_tokens:
                continue
            selected.append(item)
            used += cost
            if len(selected) >= limit:
                break
        return selected

    def patterns(self, *, project: str, owner_id: str | None = None,
                 team_id: str | None = None, minimum_evidence: int = 2) -> list[dict]:
        """Propose reusable procedures only from independent verified successes.

        Return candidates with citations; no autonomous fact promotion or memory edits.
        A matching failure blocks promotion until a human resolves the conflict.
        """
        with self._db() as db:
            rows = db.execute("""SELECT * FROM episodes WHERE project=? AND status='active'
                AND (visibility='project' OR (visibility='private' AND owner_id=?)
                     OR (visibility='team' AND team_id=?))""",
                (project, owner_id, team_id)).fetchall()
        groups: dict[str, list] = {}
        for row in rows:
            key = " ".join(row["lesson"].casefold().split())
            if key:
                groups.setdefault(key, []).append(row)
        out = []
        for group in groups.values():
            good = [r for r in group if r["verified"] and r["outcome"] == "success"]
            runs = {r["run_id"] for r in good}
            failures = [r["id"] for r in group if r["outcome"] == "failure"]
            if len(runs) >= minimum_evidence and not failures:
                out.append({"lesson": good[0]["lesson"], "evidence_count": len(runs),
                            "episode_ids": [r["id"] for r in good],
                            "source_runs": sorted(runs), "status": "candidate"})
        return sorted(out, key=lambda x: (-x["evidence_count"], x["lesson"]))
