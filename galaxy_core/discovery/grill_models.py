"""Data models and validation for Galaxy 3.1 grill-v1 interrogation engine.

Provides dataclasses and validators for decision DAG nodes, dependencies,
question options, rounds, facts, conflicts, assumptions, and session metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import re
import uuid
from typing import Any


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class GrillError(Exception):
    """Base exception for grill-v1 interrogation engine."""


class CycleDetectedError(GrillError):
    """Raised when a proposed dependency introduces a cycle in the decision DAG."""


class InvalidProposalError(GrillError):
    """Raised when an untrusted provider proposal fails deterministic validation."""


class SessionNotFoundError(GrillError):
    """Raised when an interview session cannot be found."""


class InvalidStateError(GrillError):
    """Raised when an operation is attempted in an invalid state."""


class GateViolationError(GrillError, ValueError):
    """Raised when an approval gate is violated."""


class NodeStatus:
    BLOCKED = "blocked"
    FRONTIER = "frontier"
    ANSWERED = "answered"
    ASSUMPTION = "assumption"
    RESEARCH = "research"
    CONFLICT = "conflict"

    ALL = {BLOCKED, FRONTIER, ANSWERED, ASSUMPTION, RESEARCH, CONFLICT}
    RESOLVED = {ANSWERED, ASSUMPTION, RESEARCH}


class SessionStatus:
    DISCOVERY = "DISCOVERY"
    DRAFT_READY = "DRAFT_READY"
    CONFIRMED = "CONFIRMED"
    PAUSED = "PAUSED"

    ALL = {DISCOVERY, DRAFT_READY, CONFIRMED, PAUSED}


class ConflictStatus:
    ACTIVE = "active"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"

    ALL = {ACTIVE, RESOLVED, ACCEPTED}


class PauseReason:
    USER_REQUESTED = "USER_REQUESTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

    ALL = {USER_REQUESTED, BUDGET_EXHAUSTED, PROVIDER_UNAVAILABLE}


class InterviewMode:
    RELENTLESS = "relentless"
    NORMAL = "normal"

    ALL = {RELENTLESS, NORMAL}


class InterviewDepth:
    QUICK = "quick"
    DEEP = "deep"
    EXHAUSTIVE = "exhaustive"

    DEPTH_LIMITS = {"quick": 8, "deep": 16, "exhaustive": 24}
    DEPTH_RANK = {"quick": 0, "deep": 1, "exhaustive": 2}


UNKNOWN_ANSWERS: set[str] = {
    "не знаю", "не уверен", "не уверена", "без понятия", "пока не знаю",
    "не решил", "не решила", "не определился", "не определилась", "хз",
    "idk", "unknown", "skip", "пропустить", "i don't know", "dont know", "not sure",
}

VAGUE_ANSWERS: set[str] = {
    "сделай хорошо", "чтобы было хорошо", "как обычно", "как лучше",
    "на твое усмотрение", "на твоё усмотрение", "все", "всё", "любой",
    "нормально", "удобно", "красиво", "быстро", "качественно",
    "make it good", "whatever", "default", "standard", "as usual",
}


@dataclass
class GrillAlternative:
    id: str
    text: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> GrillAlternative:
        if isinstance(data, str):
            return cls(id=data[:10], text=data)
        return cls(
            id=str(data.get("id", "")),
            text=str(data.get("text", "")),
            description=str(data.get("description", "")),
        )

    def validate(self) -> None:
        if not self.id or not str(self.id).strip():
            raise InvalidProposalError("Alternative id is required and cannot be empty")
        if not self.text or not str(self.text).strip():
            raise InvalidProposalError("Alternative text is required and cannot be empty")


@dataclass
class GrillQuestion:
    id: str
    node_id: str
    category: str
    text: str
    why: str
    alternatives: list[GrillAlternative]
    recommendation: str
    recommendation_reason: str = ""
    allow_custom: bool = True
    status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node_id": self.node_id,
            "category": self.category,
            "text": self.text,
            "why": self.why,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
            "recommendation": self.recommendation,
            "recommendation_reason": self.recommendation_reason,
            "allow_custom": self.allow_custom,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillQuestion:
        raw_alts = data.get("alternatives") or []
        alts = [GrillAlternative.from_dict(a) for a in raw_alts]
        return cls(
            id=str(data.get("id", "")),
            node_id=str(data.get("node_id", "")),
            category=str(data.get("category", "")),
            text=str(data.get("text", "")),
            why=str(data.get("why", "")),
            alternatives=alts,
            recommendation=str(data.get("recommendation", "")),
            recommendation_reason=str(data.get("recommendation_reason", "")),
            allow_custom=bool(data.get("allow_custom", True)),
            status=str(data.get("status", "open")),
        )

    def validate(self) -> None:
        if not self.id or not self.id.strip():
            raise InvalidProposalError("Question id is required and cannot be empty")
        if not self.node_id or not self.node_id.strip():
            raise InvalidProposalError("Question node_id is required and cannot be empty")
        if not self.category or not self.category.strip():
            raise InvalidProposalError("Question category is required and cannot be empty")
        if not self.text or not self.text.strip():
            raise InvalidProposalError("Question text is required and cannot be empty")
        if not self.why or not self.why.strip():
            raise InvalidProposalError("Question 'why' explanation is required and cannot be empty")
        if not (2 <= len(self.alternatives) <= 4):
            raise InvalidProposalError(
                f"Question must have between 2 and 4 alternatives, got {len(self.alternatives)}"
            )
        if not self.allow_custom:
            raise InvalidProposalError("Question must allow custom answers (allow_custom=True)")

        alt_ids: list[str] = []
        for alt in self.alternatives:
            alt.validate()
            alt_id_clean = alt.id.strip().lower()
            if alt_id_clean in alt_ids:
                raise InvalidProposalError(f"Question alternatives must have unique IDs, duplicate: '{alt.id}'")
            alt_ids.append(alt_id_clean)

        rec = self.recommendation.strip().lower()
        matched = False
        for alt in self.alternatives:
            if alt.id.strip().lower() == rec or alt.text.strip().lower() == rec:
                matched = True
                break
        if not matched:
            raise InvalidProposalError(
                f"Recommendation '{self.recommendation}' does not match any alternative id or text"
            )


@dataclass
class GrillNode:
    id: str
    category: str
    title: str
    description: str = ""
    status: str = NodeStatus.BLOCKED
    question: GrillQuestion | None = None
    answer: str | None = None
    answer_option_id: str | None = None
    answer_quality: str | None = None
    is_custom_answer: bool = False
    assumption_text: str | None = None
    assumption_verification: str | None = None
    research_task: str | None = None
    research_result: str | None = None
    dependencies: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    answered_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "question": self.question.to_dict() if self.question else None,
            "answer": self.answer,
            "answer_option_id": self.answer_option_id,
            "answer_quality": self.answer_quality,
            "is_custom_answer": self.is_custom_answer,
            "assumption_text": self.assumption_text,
            "assumption_verification": self.assumption_verification,
            "research_task": self.research_task,
            "research_result": self.research_result,
            "dependencies": list(self.dependencies),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "answered_at": self.answered_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillNode:
        q_raw = data.get("question")
        q = GrillQuestion.from_dict(q_raw) if q_raw else None
        return cls(
            id=str(data.get("id") or data.get("node_id") or ""),
            category=str(data.get("category", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", NodeStatus.BLOCKED)),
            question=q,
            answer=data.get("answer"),
            answer_option_id=data.get("answer_option_id"),
            answer_quality=data.get("answer_quality"),
            is_custom_answer=bool(data.get("is_custom_answer", False)),
            assumption_text=data.get("assumption_text"),
            assumption_verification=data.get("assumption_verification"),
            research_task=data.get("research_task"),
            research_result=data.get("research_result"),
            dependencies=list(data.get("dependencies") or []),
            created_at=str(data.get("created_at", utcnow())),
            updated_at=str(data.get("updated_at", utcnow())),
            answered_at=data.get("answered_at"),
        )

    def validate(self) -> None:
        if not self.id or not self.id.strip():
            raise InvalidProposalError("Node id is required and cannot be empty")
        if not self.category or not self.category.strip():
            raise InvalidProposalError("Node category is required and cannot be empty")
        if self.status not in NodeStatus.ALL:
            raise InvalidProposalError(
                f"Invalid node status '{self.status}', must be one of {sorted(NodeStatus.ALL)}"
            )
        if self.question:
            self.question.validate()


@dataclass
class GrillDependency:
    parent_id: str
    child_id: str
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillDependency:
        return cls(
            parent_id=str(data.get("parent_id", "")),
            child_id=str(data.get("child_id", "")),
            created_at=str(data.get("created_at", utcnow())),
        )

    def validate(self) -> None:
        if not self.parent_id or not self.parent_id.strip():
            raise InvalidProposalError("Dependency parent_id cannot be empty")
        if not self.child_id or not self.child_id.strip():
            raise InvalidProposalError("Dependency child_id cannot be empty")
        if self.parent_id.strip() == self.child_id.strip():
            raise CycleDetectedError(f"Self-dependency detected for node '{self.parent_id}'")


@dataclass
class GrillRound:
    session_id: str
    round_no: int
    status: str = "active"
    questions: list[GrillQuestion] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "round_no": self.round_no,
            "status": self.status,
            "questions": [q.to_dict() for q in self.questions],
            "answers": dict(self.answers),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillRound:
        raw_questions = data.get("questions") or []
        questions = [GrillQuestion.from_dict(q) for q in raw_questions]
        return cls(
            session_id=str(data.get("session_id", "")),
            round_no=int(data.get("round_no", 1)),
            status=str(data.get("status", "active")),
            questions=questions,
            answers=dict(data.get("answers") or {}),
            created_at=str(data.get("created_at", utcnow())),
            completed_at=data.get("completed_at"),
        )

    def validate(self) -> None:
        if not self.session_id or not self.session_id.strip():
            raise InvalidProposalError("Round session_id cannot be empty")
        if self.round_no < 1:
            raise InvalidProposalError("Round round_no must be at least 1")
        for q in self.questions:
            q.validate()


@dataclass
class GrillFact:
    session_id: str
    fact: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    node_id: str | None = None
    source_type: str = "user"
    source_ref: str = ""
    confidence: float = 1.0
    verified_at: str = field(default_factory=utcnow)
    actor: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillFact:
        return cls(
            session_id=str(data.get("session_id", "")),
            fact=str(data.get("fact", "")),
            id=str(data.get("id", uuid.uuid4().hex[:12])),
            node_id=data.get("node_id"),
            source_type=str(data.get("source_type", "user")),
            source_ref=str(data.get("source_ref", "")),
            confidence=float(data.get("confidence", 1.0)),
            verified_at=str(data.get("verified_at", utcnow())),
            actor=str(data.get("actor", "user")),
        )

    def validate(self) -> None:
        if not self.session_id or not self.session_id.strip():
            raise InvalidProposalError("Fact session_id cannot be empty")
        if not self.fact or not self.fact.strip():
            raise InvalidProposalError("Fact text cannot be empty")
        if not (0.0 <= self.confidence <= 1.0):
            raise InvalidProposalError(f"Fact confidence must be between 0.0 and 1.0, got {self.confidence}")


@dataclass
class GrillConflict:
    session_id: str
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    node_ids: list[str] = field(default_factory=list)
    status: str = ConflictStatus.ACTIVE
    resolution: str | None = None
    accepted_rationale: str | None = None
    detected_at: str = field(default_factory=utcnow)
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillConflict:
        return cls(
            session_id=str(data.get("session_id", "")),
            description=str(data.get("description", "")),
            id=str(data.get("id", uuid.uuid4().hex[:12])),
            node_ids=list(data.get("node_ids") or []),
            status=str(data.get("status", ConflictStatus.ACTIVE)),
            resolution=data.get("resolution"),
            accepted_rationale=data.get("accepted_rationale"),
            detected_at=str(data.get("detected_at", utcnow())),
            resolved_at=data.get("resolved_at"),
        )

    def validate(self) -> None:
        if not self.session_id or not self.session_id.strip():
            raise InvalidProposalError("Conflict session_id cannot be empty")
        if not self.description or not self.description.strip():
            raise InvalidProposalError("Conflict description cannot be empty")
        if self.status not in ConflictStatus.ALL:
            raise InvalidProposalError(
                f"Invalid conflict status '{self.status}', must be one of {sorted(ConflictStatus.ALL)}"
            )
        if not self.node_ids:
            raise InvalidProposalError("Conflict must reference at least one node_id")
        if self.status == ConflictStatus.RESOLVED and (not self.resolution or not self.resolution.strip()):
            raise InvalidProposalError("Resolved conflict must include an explicit resolution")
        if self.status == ConflictStatus.ACCEPTED and (not self.accepted_rationale or not self.accepted_rationale.strip()):
            raise InvalidProposalError("Accepted conflict must include an explicit accepted_rationale")


@dataclass
class GrillAnswerHistory:
    session_id: str
    node_id: str
    answer: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    quality: str = ""
    superseded_at: str = field(default_factory=utcnow)
    actor: str = "user"
    rationale: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillAnswerHistory:
        return cls(
            session_id=str(data.get("session_id", "")),
            node_id=str(data.get("node_id", "")),
            answer=str(data.get("answer", "")),
            id=str(data.get("id", uuid.uuid4().hex[:12])),
            quality=str(data.get("quality", "")),
            superseded_at=str(data.get("superseded_at", utcnow())),
            actor=str(data.get("actor", "user")),
            rationale=data.get("rationale"),
        )

    def validate(self) -> None:
        if not self.session_id or not self.session_id.strip():
            raise InvalidProposalError("AnswerHistory session_id cannot be empty")
        if not self.node_id or not self.node_id.strip():
            raise InvalidProposalError("AnswerHistory node_id cannot be empty")


@dataclass
class GrillSessionMeta:
    session_id: str
    project: str = "default"
    initial_idea: str = ""
    engine: str = "grill-v1"
    profile: str = "general"
    mode: str = InterviewMode.RELENTLESS
    depth: str = InterviewDepth.DEEP
    status: str = SessionStatus.DISCOVERY
    budget_limit: int = 30
    budget_used: int = 0
    pause_reason: str | None = None
    brief: dict[str, Any] | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillSessionMeta:
        return cls(
            session_id=str(data.get("session_id", "")),
            project=str(data.get("project", "default")),
            initial_idea=str(data.get("initial_idea", "")),
            engine=str(data.get("engine", "grill-v1")),
            profile=str(data.get("profile", "general")),
            mode=str(data.get("mode", InterviewMode.RELENTLESS)),
            depth=str(data.get("depth", InterviewDepth.DEEP)),
            status=str(data.get("status", SessionStatus.DISCOVERY)),
            budget_limit=int(data.get("budget_limit", 30)),
            budget_used=int(data.get("budget_used", 0)),
            pause_reason=data.get("pause_reason"),
            brief=data.get("brief"),
            created_at=str(data.get("created_at", utcnow())),
            updated_at=str(data.get("updated_at", utcnow())),
        )

    def validate(self) -> None:
        if not self.session_id or not self.session_id.strip():
            raise InvalidProposalError("Session session_id cannot be empty")
        if self.status not in SessionStatus.ALL:
            raise InvalidProposalError(
                f"Invalid session status '{self.status}', must be one of {sorted(SessionStatus.ALL)}"
            )
        if self.mode not in InterviewMode.ALL:
            raise InvalidProposalError(
                f"Invalid interview mode '{self.mode}', must be one of {sorted(InterviewMode.ALL)}"
            )
        if self.depth not in InterviewDepth.DEPTH_LIMITS:
            raise InvalidProposalError(
                f"Invalid interview depth '{self.depth}', must be one of {sorted(InterviewDepth.DEPTH_LIMITS)}"
            )
        if self.budget_limit < 1:
            raise InvalidProposalError("Session budget_limit must be at least 1")


PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "node_id",
                    "category",
                    "text",
                    "why",
                    "alternatives",
                    "recommendation",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "node_id": {"type": "string"},
                    "category": {"type": "string"},
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                    "alternatives": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "required": ["id", "text"],
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "recommendation": {"type": "string"},
                    "recommendation_reason": {"type": "string"},
                    "allow_custom": {"type": "boolean"},
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["fact"],
                "properties": {
                    "fact": {"type": "string"},
                    "node_id": {"type": "string"},
                    "source_type": {"type": "string"},
                    "source_ref": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["description", "node_ids"],
                "properties": {
                    "description": {"type": "string"},
                    "node_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def validate_proposal(
    proposal: dict[str, Any], session_id: str = ""
) -> tuple[list[GrillQuestion], list[GrillFact], list[GrillConflict], list[tuple[str, str]]]:
    """Deterministically validate an untrusted provider proposal against PROPOSAL_SCHEMA.

    Returns validated questions, facts, conflicts, and proposed dependencies.
    Raises InvalidProposalError if validation fails.
    """
    if not isinstance(proposal, dict):
        raise InvalidProposalError("Proposal must be a JSON object")

    raw_questions = proposal.get("questions")
    if not isinstance(raw_questions, list):
        raise InvalidProposalError("Proposal must contain a 'questions' list")

    questions: list[GrillQuestion] = []
    dependencies: list[tuple[str, str]] = []
    seen_q_ids: set[str] = set()

    for idx, raw_q in enumerate(raw_questions):
        if not isinstance(raw_q, dict):
            raise InvalidProposalError(f"Question at index {idx} must be an object")

        q = GrillQuestion.from_dict(raw_q)
        q.validate()

        q_id_clean = q.id.strip().lower()
        if q_id_clean in seen_q_ids:
            raise InvalidProposalError(f"Duplicate question id '{q.id}' in proposal")
        seen_q_ids.add(q_id_clean)
        questions.append(q)

        # Extract proposed dependencies for this question
        for parent_id in raw_q.get("dependencies") or []:
            if parent_id and str(parent_id).strip():
                dependencies.append((str(parent_id).strip(), q.node_id))

    facts: list[GrillFact] = []
    for raw_f in proposal.get("facts") or []:
        if isinstance(raw_f, dict):
            raw_f["session_id"] = session_id or raw_f.get("session_id", "")
            fact = GrillFact.from_dict(raw_f)
            fact.validate()
            facts.append(fact)

    conflicts: list[GrillConflict] = []
    for raw_c in proposal.get("conflicts") or []:
        if isinstance(raw_c, dict):
            raw_c["session_id"] = session_id or raw_c.get("session_id", "")
            conflict = GrillConflict.from_dict(raw_c)
            conflict.validate()
            conflicts.append(conflict)

    return questions, facts, conflicts, dependencies
