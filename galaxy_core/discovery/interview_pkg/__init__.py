"""Discovery interview package for Galaxy 3.1: classifier, legacy bank, stores, grill bridge, and Sun facade."""

from __future__ import annotations

from galaxy_core.discovery.interview_pkg.classifier import (
    INFORMATIONAL_PATTERNS,
    PROJECT_FEATURE_PATTERNS,
    classify_request,
    is_ambiguous_request,
)
from galaxy_core.discovery.interview_pkg.grill_bridge import GrillInterview
from galaxy_core.discovery.interview_pkg.legacy_bank import (
    DEPTH_LIMITS,
    DEPTH_RANK,
    INTERVIEW_STATUSES,
    QUESTION_BANK,
    InterviewQuestion,
)
from galaxy_core.discovery.interview_pkg.legacy_store import InterviewStore
from galaxy_core.discovery.interview_pkg.sun import SunInterview

__all__ = [
    "INFORMATIONAL_PATTERNS",
    "PROJECT_FEATURE_PATTERNS",
    "classify_request",
    "is_ambiguous_request",
    "DEPTH_LIMITS",
    "DEPTH_RANK",
    "INTERVIEW_STATUSES",
    "QUESTION_BANK",
    "InterviewQuestion",
    "InterviewStore",
    "GrillInterview",
    "SunInterview",
]
