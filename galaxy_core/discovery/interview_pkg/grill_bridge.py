"""Authoritative grill-v1 coordinator for discovery interviews."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import uuid

from galaxy_core.discovery.grill_engine import GrillEngine
from galaxy_core.discovery.grill_models import (
    GrillRound,
    GrillSessionMeta,
    InterviewDepth,
    InterviewMode,
    NodeStatus,
    PauseReason,
    SessionStatus,
    utcnow,
)
from galaxy_core.discovery.grill_privacy import delete_session_data, export_session_data
from galaxy_core.discovery.grill_profiles import (
    build_dag_for_profile,
    detect_profile,
    get_profile,
)
from galaxy_core.discovery.grill_store import GrillStore
from galaxy_core.discovery.i18n import format_brief_markdown


class GrillInterview:
    """Authoritative grill-v1 coordinator for discovery interviews.

    Features:
      - Declarative profiles (general, product, engineering, bug, research) with auto-selection.
      - Decision DAG state machine with <= 5 questions per round.
      - 2-4 distinct alternatives, explicit recommendation, and custom answer path.
      - Free-form text, option IDs, partial answers, 'не знаю', bulk recommendation acceptance.
      - Pause/resume, tree display, revision reopening, and completion gates.
      - Secret redaction and privacy preservation.
    """

    def __init__(self, root: str | Path, store: GrillStore | None = None, engine: GrillEngine | None = None):
        self.root = Path(root)
        self.store = store or GrillStore(self.root)
        self.engine = engine or GrillEngine(self.root, self.store)

    def start(
        self,
        idea: str,
        project: str = "default",
        profile: str = "auto",
        mode: str = InterviewMode.RELENTLESS,
        depth: str = InterviewDepth.DEEP,
        budget_limit: int = 30,
    ) -> dict[str, Any]:
        """Start a new grill-v1 interview session with declarative profile."""
        if not idea or not idea.strip():
            raise ValueError("initial idea is required")
        if depth not in InterviewDepth.DEPTH_LIMITS:
            raise ValueError("depth must be quick, deep or exhaustive")
        if mode not in InterviewMode.ALL:
            raise ValueError("mode must be relentless or normal")

        # Auto-detect profile if set to 'auto'
        selected_profile = profile.strip().lower() if profile else "auto"
        auto_detected = False
        detection_reason = ""
        if selected_profile == "auto":
            selected_profile, conf, detection_reason = detect_profile(idea)
            auto_detected = True

        prof_obj = get_profile(selected_profile)

        session_id = "GRILL-" + uuid.uuid4().hex[:12].upper()
        now = utcnow()

        meta = GrillSessionMeta(
            session_id=session_id,
            project=project.strip() or "default",
            initial_idea=idea.strip(),
            engine="grill-v1",
            profile=prof_obj.name,
            mode=mode,
            depth=depth,
            status=SessionStatus.DISCOVERY,
            budget_limit=budget_limit,
            budget_used=0,
            created_at=now,
            updated_at=now,
        )

        # Build DAG from declarative profile
        dag = build_dag_for_profile(prof_obj, depth, now=now)

        # Batch first round (at most 5 independent frontier questions)
        batch = dag.get_independent_frontier_batch(max_batch=5)
        initial_round: GrillRound | None = None
        if batch:
            questions = [n.question for n in batch if n.question]
            initial_round = GrillRound(
                session_id=session_id,
                round_no=1,
                status="active",
                questions=questions,
                answers={},
                created_at=now,
            )
            meta.budget_used += 1

        # Atomically persist in SQLite v3
        self.store.create_session(meta, dag, initial_round)

        view = self.get_view(session_id)
        if auto_detected:
            view["auto_detected_profile"] = {
                "profile": selected_profile,
                "reason": detection_reason,
            }
        return view

    def require(self, session_id: str) -> dict[str, Any]:
        """Retrieve view dictionary for session, raising SessionNotFoundError if not found."""
        return self.get_view(session_id)

    def get_view(self, session_id: str) -> dict[str, Any]:
        """Return comprehensive, JSON-serializable session view."""
        meta, dag, rounds = self.engine.require_session(session_id)

        current_round_dict = None
        if rounds and rounds[-1].status == "active":
            current_round_dict = rounds[-1].to_dict()

        answered_categories = [
            node.category
            for node in dag.nodes.values()
            if node.status == NodeStatus.ANSWERED
        ]

        assumptions = [
            node.assumption_text or f"{node.category}: допущение"
            for node in dag.nodes.values()
            if node.status == NodeStatus.ASSUMPTION
        ]

        conflicts = [c.to_dict() for c in dag.conflicts.values()]
        facts = [f.to_dict() for f in dag.facts.values()]

        # Backward-compatible pending question mapping (first question of current round)
        pending_question = None
        if current_round_dict and current_round_dict.get("questions"):
            pending_question = current_round_dict["questions"][0]

        return {
            "id": meta.session_id,
            "interview_id": meta.session_id,
            "engine": meta.engine,
            "project": meta.project,
            "initial_idea": meta.initial_idea,
            "profile": meta.profile,
            "mode": meta.mode,
            "depth": meta.depth,
            "status": meta.status,
            "budget_used": meta.budget_used,
            "budget_limit": meta.budget_limit,
            "questions_asked": meta.budget_used,
            "max_questions": meta.budget_limit,
            "pause_reason": meta.pause_reason,
            "current_round": current_round_dict,
            "pending": pending_question,
            "pending_question": pending_question,
            "answered_categories": answered_categories,
            "answers": {
                node.category: {
                    "answer": node.answer,
                    "quality": node.answer_quality or "adequate",
                    "status": "complete" if node.status in NodeStatus.RESOLVED else "pending",
                    "unknown": node.answer_quality == "unknown",
                }
                for node in dag.nodes.values()
                if node.status in NodeStatus.RESOLVED
            },
            "assumptions": assumptions,
            "conflicts": conflicts,
            "facts": facts,
            "brief_ready": bool(meta.brief),
            "brief": meta.brief,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
        }

    def next_question(self, session_id: str) -> dict[str, Any] | None:
        """Return pending question dict, or advance to next round."""
        meta, dag, rounds = self.engine.require_session(session_id)
        if meta.status == SessionStatus.PAUSED:
            return None
        if meta.status != SessionStatus.DISCOVERY:
            return None

        rnd = self.engine.next_round(session_id)
        if rnd and rnd.questions:
            return rnd.questions[0].to_dict()
        return None

    def answer(self, session_id: str, answer_input: str | dict[str, Any]) -> dict[str, Any]:
        """Submit answer(s) for the current active round."""
        meta, dag, rounds = self.engine.require_session(session_id)
        if meta.status != SessionStatus.DISCOVERY:
            raise ValueError(f"interview is not waiting for an answer (status: {meta.status})")

        active_round = None
        for r in rounds:
            if r.status == "active":
                active_round = r
                break

        if not active_round:
            active_round = self.engine.next_round(session_id)
            if not active_round:
                return self.get_view(session_id)

        # Parse answer input
        accept_recs = False
        answers_dict: dict[str, Any] = {}

        if isinstance(answer_input, dict):
            answers_dict = answer_input
            accept_recs = bool(answer_input.get("accept_recommendations", False))
        else:
            ans_str = str(answer_input).strip()
            ans_lower = ans_str.lower()

            if ans_lower in {"rec", "recommendation", "рекомендация", "принять все", "accept"}:
                accept_recs = True
            elif ans_lower in {"/finish", "finish", "/завершить", "завершить"}:
                return self.finish(session_id)
            elif ans_lower in {"/pause", "pause", "/пауза", "пауза"}:
                return self.pause(session_id)
            elif ans_lower in {"/continue", "continue", "/продолжить", "продолжить"}:
                return self.resume(session_id)
            elif ":" in ans_str:
                # Key-value answers: "1: A, 2: B" or "Q01: text" or single "1: A" or "outcome: A"
                pairs = re.split(r"[,;\n]+", ans_str)
                for pair in pairs:
                    if ":" in pair:
                        k, v = pair.split(":", 1)
                        answers_dict[k.strip()] = v.strip()
            elif "," in ans_str and len(active_round.questions) > 1:
                # Multiple answers separated by commas: "A, B, C" or "A, text, C"
                items = [x.strip() for x in re.split(r"[,;\n]+", ans_str) if x.strip()]
                for idx, item in enumerate(items):
                    if idx < len(active_round.questions):
                        q = active_round.questions[idx]
                        answers_dict[q.id] = item
            else:
                # Single answer applied to the first open question in round
                if active_round.questions:
                    first_q = active_round.questions[0]
                    answers_dict[first_q.id] = ans_str

        self.engine.submit_answers(
            session_id=session_id,
            round_no=active_round.round_no,
            answers=answers_dict,
            accept_recommendations=accept_recs,
        )
        return self.get_view(session_id)

    def finish(self, session_id: str) -> dict[str, Any]:
        """Finalize the interview session immediately into DRAFT_READY status."""
        meta, dag, rounds = self.engine.require_session(session_id)
        if meta.status == SessionStatus.CONFIRMED:
            return self.get_view(session_id)

        now = utcnow()
        for r in rounds:
            if r.status == "active":
                r.status = "completed"
                r.completed_at = now
                self.store.save_round(r)

        for node in dag.nodes.values():
            if node.status not in NodeStatus.RESOLVED:
                node.status = NodeStatus.ASSUMPTION
                if node.question:
                    rec_alt = None
                    for alt in node.question.alternatives:
                        if alt.id.lower() == node.question.recommendation.lower():
                            rec_alt = alt.text
                            break
                    rec_desc = f"по рекомендации '{rec_alt or node.question.recommendation}'"
                else:
                    rec_desc = "по умолчанию"
                node.assumption_text = (
                    f"{node.category}: завершено досрочно по запросу пользователя; "
                    f"принято допущение {rec_desc}."
                )
                node.assumption_verification = (
                    f"Уточнить требования к {node.category} перед реализацией."
                )
                node.updated_at = now

        dag.recalculate_frontier()
        self.engine._finish_draft(meta, dag)
        return self.get_view(session_id)

    def pause(self, session_id: str) -> dict[str, Any]:
        """Pause session execution."""
        self.engine.pause_session(session_id, PauseReason.USER_REQUESTED)
        return self.get_view(session_id)

    def resume(self, session_id: str, additional_budget: int = 0) -> dict[str, Any]:
        """Resume paused session."""
        self.engine.resume_session(session_id, additional_budget=additional_budget)
        return self.get_view(session_id)

    def revise(self, session_id: str, target: str, new_answer: str | None = None) -> dict[str, Any]:
        """Reopen a node and its dependent branches for revision."""
        self.engine.revise_answer(session_id, target, new_answer=new_answer)
        return self.get_view(session_id)

    def confirm(self, session_id: str) -> dict[str, Any]:
        """Confirm generated Goal Brief."""
        self.engine.confirm_brief(session_id)
        return self.get_view(session_id)

    def planning_prompt(self, session_id: str, confirmed_only: bool = True) -> str:
        """Return planning prompt from confirmed Goal Brief."""
        return self.engine.planning_prompt(session_id, confirmed_only=confirmed_only)

    def tree(self, session_id: str) -> dict[str, Any]:
        """Return structured decision tree."""
        return self.engine.get_tree(session_id)

    def markdown(self, session_id: str) -> str:
        """Format Goal Brief or current status into Markdown."""
        view = self.get_view(session_id)
        brief = view.get("brief")
        if brief:
            return format_brief_markdown(brief, lang="ru")

        answered = len(view.get("answered_categories") or [])
        return (
            f"# Sun Interview (grill-v1) {session_id}\n\n"
            f"- **Статус**: `{view['status']}`\n"
            f"- **Профиль**: `{view['profile']}`\n"
            f"- **Режим**: `{view['mode']}`\n"
            f"- **Идея**: {view['initial_idea']}\n"
            f"- **Отвечено разделов**: {answered}\n"
            f"- **Бюджет вопросов**: {view['budget_used']}/{view['budget_limit']}\n"
        )

    def export(self, session_id: str, redact: bool = True) -> dict[str, Any]:
        """Export complete session data with secret redaction."""
        meta, dag, rounds = self.engine.require_session(session_id)
        return export_session_data(meta, dag, rounds, redact=redact)

    def delete(self, session_id: str) -> dict[str, Any]:
        """Safely delete session records from SQLite."""
        return delete_session_data(self.store, session_id)
