"""Unified SunInterview facade supporting both legacy interviews and grill-v1."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from galaxy_core.discovery.grill_models import (
    InterviewMode,
    UNKNOWN_ANSWERS,
    VAGUE_ANSWERS,
    utcnow,
)
from galaxy_core.discovery.interview_pkg.grill_bridge import GrillInterview
from galaxy_core.discovery.interview_pkg.legacy_bank import (
    DEPTH_LIMITS,
    DEPTH_RANK,
    QUESTION_BANK,
    InterviewQuestion,
)
from galaxy_core.discovery.interview_pkg.legacy_store import InterviewStore


class SunInterview:
    """Unified Sun Interview facade.

    Delegates to legacy SunInterview implementation for legacy sessions/tests,
    and supports grill-v1 when engine="grill-v1" is selected.
    """

    def __init__(self, root: str | Path, store: InterviewStore | None = None, engine_type: str = "legacy"):
        self.root = Path(root)
        self.store = store or InterviewStore(root)
        self.engine_type = engine_type
        self.grill = GrillInterview(root)

    def start(
        self,
        idea: str,
        project: str = "default",
        depth: str = "deep",
        max_questions: int | None = None,
        engine: str | None = None,
        profile: str = "auto",
        mode: str = InterviewMode.RELENTLESS,
    ) -> dict[str, Any]:
        """Start a new discovery interview.

        If engine='grill-v1' or configured, uses grill-v1.
        Otherwise falls back to the deterministic legacy interview for backward compatibility.
        """
        chosen_engine = engine or self.engine_type
        if chosen_engine == "grill-v1":
            limit = int(max_questions or 30)
            return self.grill.start(
                idea=idea,
                project=project,
                profile=profile,
                mode=mode,
                depth=depth,
                budget_limit=limit,
            )

        # Legacy start
        if not idea or not idea.strip():
            raise ValueError("initial idea is required")
        if depth not in DEPTH_LIMITS:
            raise ValueError("depth must be quick, deep or exhaustive")
        limit = int(max_questions or DEPTH_LIMITS[depth])
        if limit < 4 or limit > 30:
            raise ValueError("max_questions must be between 4 and 30")

        session_id = self.store.create(idea.strip(), project.strip() or "default", depth, limit)
        self.next_question(session_id)
        return self.require(session_id)

    def require(self, session_id: str) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.require(session_id)
        session = self.store.get(session_id)
        if not session:
            raise ValueError(f"interview session not found: {session_id}")
        return session

    def next_question(self, session_id: str) -> dict[str, Any] | None:
        if session_id.startswith("GRILL-"):
            return self.grill.next_question(session_id)

        session = self.require(session_id)
        if session["status"] == "PAUSED":
            session["status"] = "DISCOVERY"
            self.store.save(session)
        if session["status"] != "DISCOVERY":
            return None
        if session.get("pending"):
            return session["pending"]
        if session["questions_asked"] >= session["max_questions"]:
            self._finish_draft(session)
            return None
        for question in QUESTION_BANK:
            if DEPTH_RANK[question.min_depth] > DEPTH_RANK[session["depth"]]:
                continue
            answer = session["answers"].get(question.category)
            if answer and answer.get("status") == "complete":
                continue
            session["pending"] = question.to_dict()
            session["questions_asked"] += 1
            self.store.save(session)
            return session["pending"]
        self._finish_draft(session)
        return None

    def answer(self, session_id: str, answer: str) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.answer(session_id, answer)

        session = self.require(session_id)
        if session["status"] != "DISCOVERY" or not session.get("pending"):
            raise ValueError("interview is not waiting for an answer")
        answer = answer.strip()
        if not answer:
            raise ValueError("answer cannot be empty; use 'не знаю' if uncertain")
        question = session["pending"]
        quality = self._quality(answer)
        self.store.add_turn(session_id, question, answer, quality)
        category = question["category"]
        previous = session["answers"].get(category)
        if question.get("followup_of"):
            combined = (previous.get("answer", "") + "\nУточнение: " + answer).strip() if previous else answer
            unknown = quality in {"unknown", "vague"}
            session["answers"][category] = {
                "answer": combined, "quality": quality, "unknown": unknown, "status": "complete",
            }
            if unknown:
                session["assumptions"].append(
                    f"{category}: точное решение не определено; Sun должен предложить безопасный вариант и показать его пользователю до реализации.")
            session["pending"] = None
        elif quality in {"unknown", "vague"} and session["questions_asked"] < session["max_questions"]:
            session["answers"][category] = {
                "answer": answer, "quality": quality, "unknown": quality == "unknown",
                "status": "needs_followup",
            }
            session["pending"] = self._followup(question).to_dict()
            session["questions_asked"] += 1
        else:
            session["answers"][category] = {
                "answer": answer, "quality": quality, "unknown": quality == "unknown",
                "status": "complete",
            }
            if quality == "unknown":
                session["assumptions"].append(
                    f"{category}: пользователь пока не определился; решение требует явного предположения в плане.")
            session["pending"] = None
        self.store.save(session)
        if not session.get("pending"):
            self.next_question(session_id)
        return self.require(session_id)

    def pause(self, session_id: str) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.pause(session_id)
        session = self.require(session_id)
        if session["status"] == "DISCOVERY":
            session["status"] = "PAUSED"
            self.store.save(session)
        return self.require(session_id)

    def resume(self, session_id: str, additional_budget: int = 0) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.resume(session_id, additional_budget=additional_budget)
        session = self.require(session_id)
        if session["status"] == "PAUSED":
            session["status"] = "DISCOVERY"
            self.store.save(session)
        self.next_question(session_id)
        return self.require(session_id)

    def finish(self, session_id: str) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.finish(session_id)
        session = self.require(session_id)
        if session["status"] == "CONFIRMED":
            return session
        self._finish_draft(session)
        return self.require(session_id)

    def confirm(self, session_id: str) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.confirm(session_id)
        session = self.require(session_id)
        if session["status"] != "DRAFT_READY" or not session.get("brief"):
            raise ValueError("interview has no draft brief to confirm")
        session["status"] = "CONFIRMED"
        session["brief"]["confirmed_at"] = utcnow()
        session["brief"]["confirmed_by"] = "user"
        self.store.save(session)
        return self.require(session_id)

    def revise(self, session_id: str, category: str) -> dict[str, Any]:
        if session_id.startswith("GRILL-"):
            return self.grill.revise(session_id, category)
        session = self.require(session_id)
        known = {q.category for q in QUESTION_BANK}
        if category not in known:
            raise ValueError(f"unknown category: {category}; choose one of {', '.join(sorted(known))}")
        session["answers"].pop(category, None)
        session["assumptions"] = [x for x in session["assumptions"] if not x.startswith(category + ":")]
        session["status"] = "DISCOVERY"
        session["brief"] = None
        session["pending"] = next(q.to_dict() for q in QUESTION_BANK if q.category == category)
        session["questions_asked"] += 1
        self.store.save(session)
        return self.require(session_id)

    def planning_prompt(self, session_id: str, confirmed_only: bool = True) -> str:
        if session_id.startswith("GRILL-"):
            return self.grill.planning_prompt(session_id, confirmed_only=confirmed_only)
        session = self.require(session_id)
        if confirmed_only and session["status"] != "CONFIRMED":
            raise ValueError("the user must confirm the Goal Brief before planning")
        if not session.get("brief"):
            raise ValueError("Goal Brief is not ready")
        return session["brief"]["planning_prompt"]

    def markdown(self, session_id: str) -> str:
        if session_id.startswith("GRILL-"):
            return self.grill.markdown(session_id)
        session = self.require(session_id)
        brief = session.get("brief")
        if not brief:
            answered = len([x for x in session["answers"].values() if x.get("status") == "complete"])
            return (f"# Sun Interview {session_id}\n\nСтатус: {session['status']}\n\n"
                    f"Идея: {session['initial_idea']}\n\nОтвечено категорий: {answered}\n")
        labels = {q.category: q.text for q in QUESTION_BANK}
        lines = [f"# Goal Brief — {brief['title']}", "", f"Проект: `{brief['project']}`",
                 f"Статус: **{session['status']}**", "", "## Исходная идея", "", brief["initial_idea"], ""]
        for category, data in brief["requirements"].items():
            lines += [f"## {category}: {labels.get(category, category)}", "", data["answer"], ""]
        lines += ["## Предположения и открытые вопросы", ""]
        lines += [f"- {item}" for item in brief["assumptions"]] or ["- Нет зафиксированных предположений."]
        lines += ["", "## Правило планирования", "",
                  "Sun обязан сохранить ответы пользователя, явно пометить новые предположения и не расширять scope без подтверждения.", ""]
        return "\n".join(lines)

    @staticmethod
    def _quality(answer: str) -> str:
        normalized = re.sub(r"[.!?,;:]+$", "", answer.strip().lower())
        if normalized in UNKNOWN_ANSWERS:
            return "unknown"
        if normalized in VAGUE_ANSWERS:
            return "vague"
        words = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
        if len(words) < 4:
            return "vague"
        if len(words) < 10:
            return "adequate"
        return "detailed"

    @staticmethod
    def _followup(question: dict[str, Any]) -> InterviewQuestion:
        examples = tuple(question.get("examples") or ())
        example_text = "; ".join(examples)
        text = question.get("rescue") or f"Уточните ответ на вопрос: {question['text']}"
        if example_text:
            text += f" Варианты для ориентира: {example_text}."
        return InterviewQuestion(
            id=question["id"] + "F", category=question["category"], text=text,
            why="Первый ответ был слишком общим или неопределённым. Sun просит только одно дополнительное уточнение.",
            examples=examples, min_depth=question.get("min_depth", "quick"), followup_of=question["id"],
        )

    def _finish_draft(self, session: dict[str, Any]) -> None:
        requirements = {key: value for key, value in session["answers"].items()
                        if value.get("status") == "complete"}
        unanswered = [q.category for q in QUESTION_BANK
                      if DEPTH_RANK[q.min_depth] <= DEPTH_RANK[session["depth"]]
                      and q.category not in requirements]
        assumptions = list(dict.fromkeys([
            *session["assumptions"],
            *[f"{category}: вопрос не был задан или не получил полного ответа из-за лимита интервью."
              for category in unanswered],
        ]))
        title = session["initial_idea"].strip().splitlines()[0][:100]
        brief = {
            "schema_version": "2.0",
            "interview_id": session["id"],
            "title": title,
            "project": session["project"],
            "initial_idea": session["initial_idea"],
            "depth": session["depth"],
            "requirements": requirements,
            "assumptions": assumptions,
            "created_at": utcnow(),
        }
        brief["planning_prompt"] = self._brief_prompt(brief)
        session["assumptions"] = assumptions
        session["brief"] = brief
        session["status"] = "DRAFT_READY"
        session["pending"] = None
        self.store.save(session)

    @staticmethod
    def _brief_prompt(brief: dict[str, Any]) -> str:
        requirements = "\n".join(
            f"- {category}: {data['answer']}" for category, data in brief["requirements"].items())
        assumptions = "\n".join(f"- {item}" for item in brief["assumptions"]) or "- none"
        return f"""Implement the confirmed user goal below.

Initial idea: {brief['initial_idea']}
Project: {brief['project']}

Confirmed discovery answers:
{requirements}

Explicit assumptions or unknowns:
{assumptions}

Do not silently invent missing requirements. Put unresolved assumptions into the DAG as
read-only research or approval checkpoints. Preserve the user's out-of-scope boundary,
success criteria, constraints and autonomy preferences.
"""
