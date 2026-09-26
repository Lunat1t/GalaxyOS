"""Localization (Russian and English) and friendly rendering for Galaxy 3.1 grill-v1.

Formats questions, options, marked recommendations, custom answer cues,
decision DAG trees, and Goal Briefs in clear, non-technical language.
"""
from __future__ import annotations

from typing import Any

from galaxy_core.discovery.grill_models import GrillQuestion, GrillRound, NodeStatus


STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "round_header": "\n=== Раунд {round_no} интервью Sun (вопросов на фронтире: {count}) ===",
        "question_index": "\nВопрос {idx}/{total} [{category}]",
        "why_matters": "Почему это важно:",
        "recommendation_marker": "★ Рекомендация: [{rec_id}] — {rec_text}",
        "recommendation_reason": "Обоснование: {reason}",
        "custom_path": "Или введите свой вариант ответа (свободным текстом или 'не знаю')",
        "controls_hint": (
            "Управление: 'A', 'B' (вариант) | 'rec' (принять рекомендации) | "
            "'не знаю' | '/finish' (завершить) | '/pause' (пауза) | '/tree' (дерево) | '/revise <раздел>' (исправить)"
        ),
        "paused_msg": "Интервью приостановлено. Сессия сохранена: {session_id}",
        "resumed_msg": "Интервью возобновлено: {session_id}",
        "budget_exhausted_msg": (
            "Достигнут лимит вопросов ({limit}). Интервью безопасно приостановлено.\n"
            "Вы можете увеличить бюджет или завершить интервью."
        ),
        "draft_ready_title": "=== Черновик Goal Brief готов к проверке ===",
        "confirm_prompt": "Sun правильно понял задачу? [да / изменить <раздел> / пауза]: ",
        "confirmed_msg": "Goal Brief успешно подтверждён пользователем! Сессия: {session_id}",
        "tree_title": "=== Дерево решений (Decision DAG) [{session_id}] ===",
        "tree_status": "Статус: {status} | Режим: {mode} | Профиль: {profile}",
        "status_answered": "✓ Решено",
        "status_frontier": "● На фронтире",
        "status_blocked": "○ Ожидает решения предпосылок",
        "status_assumption": "▲ Принято допущение",
        "status_conflict": "✕ Конфликт решений",
        "status_research": "🔍 Требует исследования",
        "conflicts_header": "Конфликты и противоречия:",
        "assumptions_header": "Зафиксированные допущения:",
        "facts_header": "Проверенные факты:",
    },
    "en": {
        "round_header": "\n=== Sun Interview Round {round_no} (Frontier questions: {count}) ===",
        "question_index": "\nQuestion {idx}/{total} [{category}]",
        "why_matters": "Why this matters:",
        "recommendation_marker": "★ Recommendation: [{rec_id}] — {rec_text}",
        "recommendation_reason": "Rationale: {reason}",
        "custom_path": "Or enter your custom answer (free-form text or 'unknown')",
        "controls_hint": (
            "Controls: 'A', 'B' (option) | 'rec' (accept recommendations) | "
            "'unknown' | '/finish' | '/pause' | '/tree' | '/revise <category>'"
        ),
        "paused_msg": "Interview paused. Session saved: {session_id}",
        "resumed_msg": "Interview resumed: {session_id}",
        "budget_exhausted_msg": (
            "Question budget limit ({limit}) reached. Interview safely paused.\n"
            "You can increase the budget or finalize the interview."
        ),
        "draft_ready_title": "=== Draft Goal Brief Ready for Review ===",
        "confirm_prompt": "Did Sun understand the task correctly? [yes / edit <category> / pause]: ",
        "confirmed_msg": "Goal Brief confirmed by user! Session: {session_id}",
        "tree_title": "=== Decision DAG Tree [{session_id}] ===",
        "tree_status": "Status: {status} | Mode: {mode} | Profile: {profile}",
        "status_answered": "✓ Answered",
        "status_frontier": "● On frontier",
        "status_blocked": "○ Blocked by dependencies",
        "status_assumption": "▲ Assumption accepted",
        "status_conflict": "✕ Conflict detected",
        "status_research": "🔍 Needs research",
        "conflicts_header": "Conflicts and contradictions:",
        "assumptions_header": "Recorded assumptions:",
        "facts_header": "Verified facts:",
    },
}


def t(key: str, lang: str = "ru", **kwargs: Any) -> str:
    """Retrieve localized string with keyword formatting."""
    lang_dict = STRINGS.get(lang, STRINGS["ru"])
    template = lang_dict.get(key, STRINGS["ru"].get(key, key))
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def format_question(
    question: GrillQuestion,
    lang: str = "ru",
    index: int | None = None,
    total: int | None = None,
) -> str:
    """Format a GrillQuestion with explanation, alternatives, recommendation, and custom path."""
    lines: list[str] = []

    if index is not None and total is not None:
        lines.append(t("question_index", lang, idx=index, total=total, category=question.category))
    else:
        lines.append(f"\n[{question.category.upper()}]")

    lines.append(f"  {question.text}")
    lines.append(f"  {t('why_matters', lang)} {question.why}")
    lines.append("")

    # Display alternatives
    for alt in question.alternatives:
        desc = f" ({alt.description})" if alt.description else ""
        lines.append(f"    [{alt.id}] {alt.text}{desc}")

    lines.append("")

    # Find recommended alternative text
    rec_text = ""
    for alt in question.alternatives:
        if alt.id.lower() == question.recommendation.lower() or alt.text.lower() == question.recommendation.lower():
            rec_text = alt.text
            break
    if not rec_text:
        rec_text = question.recommendation

    lines.append(
        "  " + t("recommendation_marker", lang, rec_id=question.recommendation, rec_text=rec_text)
    )
    if question.recommendation_reason:
        lines.append("  " + t("recommendation_reason", lang, reason=question.recommendation_reason))

    lines.append(f"  • {t('custom_path', lang)}")
    return "\n".join(lines)


def format_round(
    round_obj: GrillRound,
    lang: str = "ru",
    budget_used: int = 0,
    budget_limit: int = 30,
) -> str:
    """Format an entire interview round."""
    lines: list[str] = []
    lines.append(t("round_header", lang, round_no=round_obj.round_no, count=len(round_obj.questions)))
    lines.append(f"Бюджет вопросов: {budget_used}/{budget_limit}" if lang == "ru" else f"Question budget: {budget_used}/{budget_limit}")

    for idx, q in enumerate(round_obj.questions, 1):
        lines.append(format_question(q, lang, index=idx, total=len(round_obj.questions)))

    lines.append("\n" + t("controls_hint", lang))
    return "\n".join(lines)


def format_tree(tree_data: dict[str, Any], lang: str = "ru") -> str:
    """Format the decision DAG into an understandable visual tree for CLI."""
    lines: list[str] = []
    session_id = tree_data.get("interview_id", "")
    status = tree_data.get("status", "")
    mode = tree_data.get("mode", "")
    profile = tree_data.get("profile", "")

    lines.append(t("tree_title", lang, session_id=session_id))
    lines.append(t("tree_status", lang, status=status, mode=mode, profile=profile))
    lines.append("-" * 60)

    nodes = tree_data.get("nodes") or {}
    deps = tree_data.get("dependencies") or []

    # Map children and parents
    parents_map: dict[str, list[str]] = {}
    for parent, child in deps:
        parents_map.setdefault(child, []).append(parent)

    status_labels = {
        NodeStatus.ANSWERED: t("status_answered", lang),
        NodeStatus.FRONTIER: t("status_frontier", lang),
        NodeStatus.BLOCKED: t("status_blocked", lang),
        NodeStatus.ASSUMPTION: t("status_assumption", lang),
        NodeStatus.CONFLICT: t("status_conflict", lang),
        NodeStatus.RESEARCH: t("status_research", lang),
    }

    for nid, node in sorted(nodes.items()):
        cat = node.get("category", nid)
        n_status = node.get("status", "blocked")
        st_label = status_labels.get(n_status, n_status)
        prereqs = parents_map.get(nid, [])
        prereqs_str = f" [зависит от: {', '.join(prereqs)}]" if prereqs else ""

        line = f"  {st_label} {cat.upper()}{prereqs_str}"
        lines.append(line)

        ans = node.get("answer")
        if ans:
            opt = f" [{node.get('answer_option_id')}]" if node.get("answer_option_id") else ""
            lines.append(f"      Ответ: {ans}{opt}")

        asump = node.get("assumption_text")
        if asump:
            lines.append(f"      Допущение: {asump}")

    # Add conflicts if any
    conflicts = tree_data.get("conflicts") or []
    if conflicts:
        lines.append("\n" + t("conflicts_header", lang))
        for c in conflicts:
            lines.append(f"  ✕ [{c.get('status')}] {c.get('description')} (узлы: {c.get('node_ids')})")

    # Add assumptions
    assumptions = tree_data.get("assumptions") or []
    if assumptions:
        lines.append("\n" + t("assumptions_header", lang))
        for a in assumptions:
            lines.append(f"  ▲ {a.get('category')}: {a.get('assumption_text')}")

    # Add facts
    facts = tree_data.get("facts") or []
    if facts:
        lines.append("\n" + t("facts_header", lang))
        for f in facts:
            lines.append(f"  ✓ [{f.get('source_type')}] {f.get('fact')}")

    lines.append("-" * 60)
    return "\n".join(lines)


def format_brief_markdown(brief: dict[str, Any], lang: str = "ru") -> str:
    """Format confirmed Goal Brief into clear Markdown."""
    title = brief.get("title", "Goal Brief")
    project = brief.get("project", "default")
    idea = brief.get("initial_idea", "")
    profile = brief.get("profile", "general")
    mode = brief.get("mode", "relentless")

    lines = [
        f"# Goal Brief — {title}",
        "",
        f"- **Проект**: `{project}`",
        f"- **Профиль**: `{profile}`",
        f"- **Режим**: `{mode}`",
        f"- **Схема**: `grill-v1`",
        "",
        "## Исходная цель",
        "",
        idea,
        "",
        "## Согласованные требования",
        "",
    ]

    for cat, data in (brief.get("requirements") or {}).items():
        ans = data.get("answer", "") if isinstance(data, dict) else str(data)
        lines.append(f"### {cat.replace('_', ' ').title()}")
        lines.append("")
        lines.append(ans)
        lines.append("")

    lines.append("## Зафиксированные допущения и открытые вопросы")
    lines.append("")
    assumptions = brief.get("assumptions") or []
    if assumptions:
        for item in assumptions:
            lines.append(f"- {item}")
    else:
        lines.append("- Нет открытых допущений.")
    lines.append("")

    lines.append("## Правило планирования Galaxy 3.1")
    lines.append("")
    lines.append(
        "Sun и планировщик обязаны сохранить все зафиксированные ответы пользователя, "
        "явно пометить непроверенные допущения как задачи исследования и строго соблюдать "
        "границы Human Approval Gate перед любой модификацией кода."
    )
    lines.append("")

    return "\n".join(lines)
