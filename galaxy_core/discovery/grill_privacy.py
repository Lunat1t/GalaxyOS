"""Privacy, secret redaction, context minimization, export, and deletion for grill-v1.

Ensures sensitive credentials, tokens, and private data are systematically redacted
before any provider call, export, or log output, while providing minimal context
packaging, session export, and controlled deletion.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import re
from typing import Any

from galaxy_core.discovery.grill_dag import GrillDAG
from galaxy_core.discovery.grill_models import (
    GrillRound,
    GrillSessionMeta,
    NodeStatus,
    utcnow,
)
from galaxy_core.discovery.grill_store import GrillStore


SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{20,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36,}\b")),
    ("GITHUB_PAT", re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{50,}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("HUGGINGFACE_TOKEN", re.compile(r"\bhf_[a-zA-Z0-9]{34,}\b")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+([a-zA-Z0-9_\-\.]{20,})\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z0-9_-]*PRIVATE KEY-----")),
    ("URI_PASSWORD", re.compile(r"(://[^:\s]+):([^@\s/]+)@")),
    ("GENERIC_SECRET", re.compile(r"(?i)\b(api_key|secret_key|password|token|auth_token)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{16,})['\"]?")),
]


def redact_secrets(text: str) -> str:
    """Redact sensitive tokens, keys, passwords, and private certificates from a string."""
    if not text or not isinstance(text, str):
        return text

    sanitized = text

    # Handle private keys first
    for name, pattern in SECRET_PATTERNS:
        if name == "PRIVATE_KEY":
            sanitized = pattern.sub("[REDACTED_PRIVATE_KEY]", sanitized)
        elif name == "URI_PASSWORD":
            sanitized = pattern.sub(r"\1:[REDACTED_PASSWORD]@", sanitized)
        elif name == "BEARER_TOKEN":
            sanitized = pattern.sub("Bearer [REDACTED_TOKEN]", sanitized)
        elif name == "GENERIC_SECRET":
            sanitized = pattern.sub(r"\1=[REDACTED_SECRET]", sanitized)
        else:
            sanitized = pattern.sub(f"[REDACTED_{name}]", sanitized)

    return sanitized


def redact_data(obj: Any) -> Any:
    """Recursively redact secrets from nested dictionaries, lists, strings, and dataclasses."""
    if isinstance(obj, str):
        return redact_secrets(obj)
    elif isinstance(obj, dict):
        return {k: redact_data(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [redact_data(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(redact_data(item) for item in obj)
    elif is_dataclass(obj):
        return redact_data(asdict(obj))
    return obj


def minimize_provider_context(
    meta: GrillSessionMeta,
    dag: GrillDAG,
    current_nodes: list[Any] | None = None,
) -> dict[str, Any]:
    """Extract minimal, privacy-redacted context required for provider proposals.

    Passes only the goal, answered requirements, active frontier categories,
    and verified facts, completely omitting internal state or secrets.
    """
    answered_summary: dict[str, str] = {}
    for node in dag.nodes.values():
        if node.status in NodeStatus.RESOLVED and node.answer:
            answered_summary[node.category] = redact_secrets(node.answer)

    facts_summary = [
        {"fact": redact_secrets(f.fact), "confidence": f.confidence, "source": f.source_type}
        for f in dag.facts.values()
    ]

    frontier_categories = [
        node.category for node in (current_nodes or []) if hasattr(node, "category")
    ]

    payload = {
        "project": meta.project,
        "initial_idea": redact_secrets(meta.initial_idea),
        "profile": meta.profile,
        "mode": meta.mode,
        "depth": meta.depth,
        "answered_categories": answered_summary,
        "frontier_categories": frontier_categories,
        "verified_facts": facts_summary,
    }
    return payload


def export_session_data(
    meta: GrillSessionMeta,
    dag: GrillDAG,
    rounds: list[GrillRound],
    redact: bool = True,
) -> dict[str, Any]:
    """Export complete interview session data with optional secret redaction."""
    data = {
        "export_version": "grill-v1",
        "exported_at": utcnow(),
        "session": meta.to_dict(),
        "dag": {
            "nodes": {nid: n.to_dict() for nid, n in dag.nodes.items()},
            "dependencies": [d.to_dict() for d in dag.dependencies],
            "conflicts": [c.to_dict() for c in dag.conflicts.values()],
            "facts": [f.to_dict() for f in dag.facts.values()],
        },
        "rounds": [r.to_dict() for r in rounds],
    }

    if redact:
        return redact_data(data)
    return data


def delete_session_data(store: GrillStore, session_id: str) -> dict[str, Any]:
    """Safely delete session records from SQLite with an audit receipt."""
    counts: dict[str, int] = {}
    now = utcnow()

    with store.connect() as db:
        # Verify session exists
        row = db.execute("SELECT session_id FROM grill_session_meta WHERE session_id=?", (session_id,)).fetchone()
        legacy_row = db.execute("SELECT id FROM interview_sessions WHERE id=?", (session_id,)).fetchone()

        if not row and not legacy_row:
            return {
                "deleted": False,
                "session_id": session_id,
                "reason": "Session not found",
            }

        # Delete from grill-v1 tables
        tables = [
            ("grill_answer_history", "session_id"),
            ("grill_conflicts", "session_id"),
            ("grill_facts", "session_id"),
            ("grill_rounds", "session_id"),
            ("grill_dependencies", "session_id"),
            ("grill_nodes", "session_id"),
            ("grill_session_meta", "session_id"),
            ("interview_turns", "session_id"),
            ("interview_sessions", "id"),
        ]

        for table, col in tables:
            cur = db.execute(f"DELETE FROM {table} WHERE {col}=?", (session_id,))
            counts[table] = cur.rowcount

    return {
        "deleted": True,
        "session_id": session_id,
        "deleted_at": now,
        "record_counts": counts,
    }
