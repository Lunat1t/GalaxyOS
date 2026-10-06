"""Canonical persistent roles for the Galaxy 3.1 engine."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    purpose: str
    produces: str
    may_write: bool = False


ROLES = {
    "Sun": Role("Sun", "question the user, expose ambiguity and form a confirmed goal", "Goal Brief and DAG"),
    "Venera": Role("Venera", "turn intent into explicit, testable requirements", "specification"),
    "Ceres": Role("Ceres", "collect project facts and evidence without guessing", "research notes"),
    "Mars": Role("Mars", "design boundaries, architecture and risk controls", "technical design"),
    "Earth": Role("Earth", "implement the approved change inside narrow scopes", "working artifacts", True),
    "Neptun": Role("Neptun", "review correctness and repair verified defects", "review or fixes", True),
    "Moon": Role("Moon", "run independent deterministic verification", "QA evidence"),
    "Mercury": Role("Mercury", "explain verified results and remaining limits", "final report"),
}


def role(name: str) -> Role:
    try:
        return ROLES[name]
    except KeyError as exc:
        raise ValueError(f"unknown Galaxy role: {name}") from exc
