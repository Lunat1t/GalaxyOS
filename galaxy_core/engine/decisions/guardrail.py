"""Galaxy 2.2 Mars SecOps Decision Guardrail (pi-warden pattern).

Applies System One speculative fan-out to inspect commands and tool actions
before execution, enforcing fail-safe escalation to Human Approval Gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from .primitives import (
    Choice,
    DecisionRequest,
    Noul,
    Score,
)
from .engine import DecisionEngine


@dataclass(frozen=True)
class GuardrailVerdict:
    action: str  # ALLOW, AWAITING_APPROVAL, BLOCK
    is_destructive: bool
    risk_score: float
    confidence: float
    reason: str
    decision_ms: float
    requires_human_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "is_destructive": self.is_destructive,
            "risk_score": self.risk_score,
            "confidence": self.confidence,
            "reason": self.reason,
            "decision_ms": self.decision_ms,
            "requires_human_approval": self.requires_human_approval,
        }


class MarsDecisionGuardrail:
    """SecOps guardrail protecting the filesystem and execution environment."""

    CONFIDENCE_THRESHOLD = 0.85
    HIGH_RISK_SCORE_THRESHOLD = 3.5

    def __init__(self, engine: DecisionEngine | None = None):
        self.engine = engine or DecisionEngine()

    def inspect_command(self, command: str, context: str = "") -> GuardrailVerdict:
        """Inspect a shell or terminal command via speculative fan-out."""
        start = time.monotonic()
        state = f"Command: {command}\nContext: {context}".strip()

        # Speculative fan-out: 3 typed questions evaluated in a single pass
        request = DecisionRequest(
            state=state,
            questions={
                "is_destructive": Noul(
                    instructions="Does this command perform destructive, irreversible deletions or force mutations?"
                ),
                "risk_score": Score(
                    instructions="Rate the operational and security risk of this command from 1 (read-only safe) to 5 (critical/destructive)",
                    levels=5,
                ),
                "policy_action": Choice(
                    instructions="Determine the security policy action for this command",
                    options=["allow", "awaiting_approval", "block"],
                ),
            },
        )

        res = self.engine.decide(request)

        is_destructive = bool(res.answers.get("is_destructive", False))
        risk_score = float(res.answers.get("risk_score", 1.0))
        raw_action = str(res.answers.get("policy_action", "awaiting_approval")).lower()

        conf_destr = float(res.confidences.get("is_destructive", 0.5))
        conf_risk = float(res.confidences.get("risk_score", 0.5))
        conf_action = float(res.confidences.get("policy_action", 0.5))
        min_conf = min(conf_destr, conf_risk, conf_action)

        # Enforce fail-safe policy:
        # 1. Any destructive command or risk_score >= 3.5 MUST NOT be silently allowed.
        # 2. Low confidence (< 0.85) forces escalation to Human Approval Gate.
        reasons: list[str] = []
        final_action = raw_action.upper()
        requires_approval = False

        if is_destructive:
            reasons.append("Command identified as potentially destructive")
            if final_action == "ALLOW":
                final_action = "AWAITING_APPROVAL"

        if risk_score >= self.HIGH_RISK_SCORE_THRESHOLD:
            reasons.append(f"High risk score ({risk_score:.2f} >= {self.HIGH_RISK_SCORE_THRESHOLD})")
            if final_action == "ALLOW":
                final_action = "AWAITING_APPROVAL"

        if min_conf < self.CONFIDENCE_THRESHOLD:
            reasons.append(f"Confidence below threshold ({min_conf:.2f} < {self.CONFIDENCE_THRESHOLD})")
            if final_action == "ALLOW":
                final_action = "AWAITING_APPROVAL"

        if final_action == "AWAITING_APPROVAL":
            requires_approval = True
            if not reasons:
                reasons.append("SecOps policy requires explicit human confirmation")
        elif final_action == "BLOCK":
            requires_approval = False
            if not reasons:
                reasons.append("Dangerous operation strictly blocked by Mars SecOps")
        else:
            final_action = "ALLOW"
            reasons.append("Command validated as safe read-only or authorized operation")

        elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)

        return GuardrailVerdict(
            action=final_action,
            is_destructive=is_destructive,
            risk_score=risk_score,
            confidence=min_conf,
            reason="; ".join(reasons),
            decision_ms=elapsed_ms,
            requires_human_approval=requires_approval,
        )
