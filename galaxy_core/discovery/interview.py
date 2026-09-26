"""Discovery interview subsystem for Galaxy 3.1: Sun, grill-v1, and legacy fallback.

Backward-compatible facade forwarding to `galaxy_core.discovery.interview_pkg`.
"""

from __future__ import annotations

from galaxy_core.discovery.interview_pkg import (
    DEPTH_LIMITS,
    DEPTH_RANK,
    INTERVIEW_STATUSES,
    GrillInterview,
    InterviewQuestion,
    InterviewStore,
    QUESTION_BANK,
    SunInterview,
    classify_request,
    is_ambiguous_request,
)

__all__ = [
    "DEPTH_LIMITS",
    "DEPTH_RANK",
    "INTERVIEW_STATUSES",
    "GrillInterview",
    "InterviewQuestion",
    "InterviewStore",
    "QUESTION_BANK",
    "SunInterview",
    "classify_request",
    "is_ambiguous_request",
]
