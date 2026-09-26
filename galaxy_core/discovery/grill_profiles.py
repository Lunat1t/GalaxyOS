"""Declarative interview profiles for Galaxy 3.1 grill-v1.

Loads declarative profiles (general, product, engineering, bug, research)
from config/interview_profiles/*.json, supports automatic profile detection,
user override/correction, and DAG generation with profile-specific questions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from galaxy_core.discovery.grill_dag import GrillDAG
from galaxy_core.discovery.grill_models import (
    GrillAlternative,
    GrillNode,
    GrillQuestion,
    NodeStatus,
    utcnow,
)


KNOWN_PROFILES = ("general", "product", "engineering", "bug", "research")


@dataclass
class GrillProfile:
    name: str
    title: str
    description: str
    keywords: list[str] = field(default_factory=list)
    match_patterns: list[str] = field(default_factory=list)
    default_depth: str = "deep"
    categories: dict[str, list[str]] = field(default_factory=dict)
    dependencies: list[tuple[str, str]] = field(default_factory=list)
    questions: dict[str, GrillQuestion] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "keywords": list(self.keywords),
            "match_patterns": list(self.match_patterns),
            "default_depth": self.default_depth,
            "categories": {k: list(v) for k, v in self.categories.items()},
            "dependencies": [list(d) for d in self.dependencies],
            "questions": {k: q.to_dict() for k, q in self.questions.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillProfile:
        name = str(data.get("name", "general"))
        title = str(data.get("title", name.title()))
        description = str(data.get("description", ""))
        keywords = [str(k).strip() for k in data.get("keywords") or [] if str(k).strip()]
        match_patterns = [str(p).strip() for p in data.get("match_patterns") or [] if str(p).strip()]
        default_depth = str(data.get("default_depth", "deep"))
        categories = {k: list(v) for k, v in (data.get("categories") or {}).items()}

        raw_deps = data.get("dependencies") or []
        dependencies = [(str(d[0]), str(d[1])) for d in raw_deps if len(d) >= 2]

        raw_questions = data.get("questions") or {}
        questions: dict[str, GrillQuestion] = {}
        for cat, qdata in raw_questions.items():
            if isinstance(qdata, dict):
                alts = [
                    GrillAlternative.from_dict(a)
                    for a in (qdata.get("alternatives") or [])
                ]
                q = GrillQuestion(
                    id=str(qdata.get("id", f"Q_{name.upper()}_{cat.upper()}")),
                    node_id=str(qdata.get("node_id", f"node_{cat}")),
                    category=str(qdata.get("category", cat)),
                    text=str(qdata.get("text", "")),
                    why=str(qdata.get("why", "")),
                    alternatives=alts,
                    recommendation=str(qdata.get("recommendation", "A")),
                    recommendation_reason=str(qdata.get("recommendation_reason", "")),
                    allow_custom=bool(qdata.get("allow_custom", True)),
                    status=str(qdata.get("status", "open")),
                )
                q.validate()
                questions[cat] = q

        return cls(
            name=name,
            title=title,
            description=description,
            keywords=keywords,
            match_patterns=match_patterns,
            default_depth=default_depth,
            categories=categories,
            dependencies=dependencies,
            questions=questions,
        )


def _find_config_dir(custom_path: Path | str | None = None) -> Path:
    if custom_path:
        p = Path(custom_path)
        if p.is_dir():
            return p
    # Try relative to this file
    this_dir = Path(__file__).resolve().parent
    root_candidate = this_dir.parent.parent
    cfg = root_candidate / "config" / "interview_profiles"
    if cfg.is_dir():
        return cfg
    # Try current working directory
    cwd_cfg = Path.cwd() / "config" / "interview_profiles"
    if cwd_cfg.is_dir():
        return cwd_cfg
    return cfg


def load_profiles(custom_dir: Path | str | None = None) -> dict[str, GrillProfile]:
    """Load all declarative interview profiles from JSON files."""
    cfg_dir = _find_config_dir(custom_dir)
    profiles: dict[str, GrillProfile] = {}

    if cfg_dir.is_dir():
        for json_path in sorted(cfg_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                profile = GrillProfile.from_dict(data)
                profiles[profile.name] = profile
            except Exception:
                # Log or skip malformed profile
                pass

    # Ensure all standard profiles exist, falling back to minimal defaults if needed
    for name in KNOWN_PROFILES:
        if name not in profiles:
            profiles[name] = _builtin_fallback_profile(name)

    return profiles


def get_profile(name: str, custom_dir: Path | str | None = None) -> GrillProfile:
    """Retrieve a profile by name (case-insensitive). Defaults to 'general' if not found."""
    name_clean = (name or "").strip().lower()
    profiles = load_profiles(custom_dir)
    if name_clean in profiles:
        return profiles[name_clean]
    return profiles.get("general", _builtin_fallback_profile("general"))


def detect_profile(
    text: str,
    profiles: dict[str, GrillProfile] | None = None,
    custom_dir: Path | str | None = None,
) -> tuple[str, float, str]:
    """Deterministically detect the most appropriate profile for an idea/request.

    Returns (profile_name, confidence, reason).
    """
    if not text or not text.strip():
        return "general", 0.5, "Пустой запрос; выбран общий профиль по умолчанию."

    text_clean = text.strip()
    text_lower = text_clean.lower()

    if profiles is None:
        profiles = load_profiles(custom_dir)

    # Check for explicit profile mentions first: e.g. "профиль: bug" or "--profile engineering"
    for name in KNOWN_PROFILES:
        patterns = [
            rf"\bпрофиль\s*[:=]\s*{name}\b",
            rf"\bprofile\s*[:=]\s*{name}\b",
            rf"--profile\s+{name}\b",
        ]
        for pat in patterns:
            if re.search(pat, text_lower):
                return name, 1.0, f"Явно указан профиль '{name}' в запросе."

    scores: dict[str, float] = {name: 0.0 for name in KNOWN_PROFILES}
    reasons: dict[str, list[str]] = {name: [] for name in KNOWN_PROFILES}

    # Evaluate each profile against user text
    for name, prof in profiles.items():
        if name not in scores:
            scores[name] = 0.0
            reasons[name] = []

        # 1. Regex match patterns (strong weight)
        for pattern in prof.match_patterns:
            try:
                if re.search(pattern, text_lower):
                    scores[name] += 0.45
                    reasons[name].append(f"совпадение шаблона '{pattern}'")
                    break
            except re.error:
                pass

        # 2. Keywords
        keyword_hits: list[str] = []
        for kw in prof.keywords:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            # Check whole word match or substring for Russian roots
            if re.search(rf"\b{re.escape(kw_clean)}", text_lower):
                keyword_hits.append(kw_clean)

        if keyword_hits:
            hit_score = min(0.5, len(keyword_hits) * 0.12)
            scores[name] += hit_score
            reasons[name].append(f"ключевые слова: {', '.join(keyword_hits[:3])}")

    # Prioritize specific profiles over 'general' when signals exist
    specific_profiles = [p for p in ("bug", "research", "engineering", "product") if p in scores]
    best_specific = max(specific_profiles, key=lambda p: scores.get(p, 0.0))
    best_specific_score = scores.get(best_specific, 0.0)

    if best_specific_score >= 0.25:
        confidence = min(0.95, round(best_specific_score + 0.35, 2))
        reason_text = f"Обнаружены маркеры профиля '{best_specific}' ({'; '.join(reasons[best_specific])})"
        return best_specific, confidence, reason_text

    # Default to general
    return "general", 0.6, "Общий профиль по умолчанию (специфические маркеры других профилей не превысили порог)."


CATEGORY_MAPPINGS: dict[str, dict[str, str]] = {
    "product": {
        "value_proposition": "outcome",
        "core_features": "must_have",
        "success_metrics": "success",
    },
    "engineering": {
        "system_goal": "outcome",
        "core_components": "must_have",
        "api_contracts": "success",
    },
    "bug": {
        "expected_behavior": "outcome",
        "fix_boundaries": "must_have",
        "verification_check": "success",
    },
    "research": {
        "research_goal": "outcome",
        "deliverable_type": "must_have",
        "evaluation_criteria": "success",
    },
}


def build_dag_for_profile(
    profile: GrillProfile,
    depth: str = "deep",
    now: str | None = None,
) -> GrillDAG:
    """Instantiate a GrillDAG configured with profile categories, questions, and dependencies."""
    now = now or utcnow()
    dag = GrillDAG()

    active_categories = profile.categories.get(
        depth,
        profile.categories.get("deep", ["outcome", "problem", "users", "must_have"]),
    )
    active_set = set(active_categories)
    cat_map = CATEGORY_MAPPINGS.get(profile.name, {})

    for cat in active_categories:
        nid = f"node_{cat}"
        effective_cat = cat_map.get(cat, cat)
        q = profile.questions.get(cat)
        if q:
            # Clone with assigned node_id
            q_clone = GrillQuestion(
                id=q.id,
                node_id=nid,
                category=effective_cat,
                text=q.text,
                why=q.why,
                alternatives=list(q.alternatives),
                recommendation=q.recommendation,
                recommendation_reason=q.recommendation_reason,
                allow_custom=q.allow_custom,
                status="open",
            )
        else:
            # Fallback question
            q_clone = GrillQuestion(
                id=f"Q_{profile.name.upper()}_{cat.upper()}",
                node_id=nid,
                category=effective_cat,
                text=f"Уточните требования для раздела '{cat}' в профиле {profile.name}.",
                why=f"Необходимо для завершения спецификации раздела {cat}.",
                alternatives=[
                    GrillAlternative("A", "Принять стандартное решение", "Стандарт"),
                    GrillAlternative("B", "Сформировать индивидуальное требование", "Кастом"),
                ],
                recommendation="A",
                recommendation_reason="Стандартное решение минимизирует риски.",
                allow_custom=True,
            )

        node = GrillNode(
            id=nid,
            category=effective_cat,
            title=cat.replace("_", " ").title(),
            description=q_clone.text,
            status=NodeStatus.BLOCKED,
            question=q_clone,
            created_at=now,
            updated_at=now,
        )
        dag.add_node(node)

    for parent_cat, child_cat in profile.dependencies:
        if parent_cat in active_set and child_cat in active_set:
            p_nid = f"node_{parent_cat}"
            c_nid = f"node_{child_cat}"
            try:
                dag.add_dependency(p_nid, c_nid)
            except Exception:
                pass

    dag.recalculate_frontier()
    return dag


def list_profiles(profiles: dict[str, GrillProfile] | None = None) -> list[dict[str, Any]]:
    """Return summary of all available profiles for UI/CLI display."""
    if profiles is None:
        profiles = load_profiles()
    result = []
    for name in KNOWN_PROFILES:
        prof = profiles.get(name)
        if prof:
            result.append({
                "name": prof.name,
                "title": prof.title,
                "description": prof.description,
                "default_depth": prof.default_depth,
                "keywords": prof.keywords[:6],
            })
    return result


def _builtin_fallback_profile(name: str) -> GrillProfile:
    """Minimal fallback profile if json files are inaccessible."""
    cats = {
        "quick": ["outcome", "problem", "users", "must_have"],
        "deep": ["outcome", "problem", "users", "must_have", "constraints", "success"],
        "exhaustive": ["outcome", "problem", "users", "must_have", "constraints", "success", "maintenance"],
    }
    deps = [("problem", "outcome"), ("outcome", "must_have")]
    return GrillProfile(
        name=name,
        title=f"{name.title()} Profile",
        description=f"Standard {name} interview profile",
        keywords=[name],
        categories=cats,
        dependencies=deps,
    )
