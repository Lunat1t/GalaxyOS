"""Natural-Language Dispatch & Ambiguity Classifier for Galaxy 3.1."""

from __future__ import annotations

import re
from typing import Any

from galaxy_core.discovery.grill_profiles import detect_profile

INFORMATIONAL_PATTERNS: list[str] = [
    r"^(какой\s+)?статус\b",
    r"^покажи\s+(статус|задачи|рантайм|агентов|дашборд|память|лог)",
    r"^что\s+(в\s+работе|сделано|нового|в\s+бэклоге)",
    r"^(список|покажи)\s+(задач|агентов|целей|памяти)",
    r"^кто\s+ты\b",
    r"^(help|помощь|справка|doctor|версия|version)\b",
    r"^who\s+are\s+you\b",
    r"^(show|list)\s+(status|agents|tasks|goals|runs)\b",
    r"^найди\s+в\s+памяти\b",
    r"^brain\s+search\b",
    r"^(как\s+работает|как\s+устроена?|расскажи\s+про|что\s+такое)\b",
    r"^(where\s+are|how\s+does|what\s+is|tell\s+me\s+about)\b",
    r"^(где\s+(лежат|находятся|хранятся))\b",
    r"^(поясни|объясни)\b",
    r"^(информация|справка|info)\b",
]

PROJECT_FEATURE_PATTERNS: list[str] = [
    r"^(сделай|создай|разработай|напиши|реализуй|запусти|построй)\b",
    r"^(добавь|внедри|подключи|интегрируй)\s+(фичу|возможность|модуль|сервис|api|функцию|поддержку)",
    r"^(исправь|почини|устрани|разберись)\s+(баг|ошибк|дефект|сбой|падение)",
    r"^(исследуй|сравни|проанализируй|изучи|проверь)\s+(гипотезу|технологи|варианты)",
    r"^(перепиши|отрефактори|оптимизируй)\s+(архитектуру|код|модуль|систему|базу)",
    r"^(хочу|нужно|надо|планирую)\s+(сделать|создать|разработать|добавить|исправить)",
    r"^(build|create|make|implement|develop|design|fix|refactor|research)\b",
]


def is_ambiguous_request(text: str) -> bool:
    """Return True if request is a new project, feature, or complex decision needing discovery."""
    res = classify_request(text)
    return res["needs_grill"]


def classify_request(text: str) -> dict[str, Any]:
    """Classify user request to decide whether it automatically enters grill-v1 or bypasses it.

    Simple informational requests bypass grill-v1.
    New ambiguous projects, features, and complex decisions enter grill-v1.
    """
    if not text or not text.strip():
        return {
            "needs_grill": False,
            "action": "bypass",
            "reason": "Пустой запрос.",
            "profile": "general",
            "confidence": 1.0,
        }

    text_clean = text.strip()
    text_lower = text_clean.lower()

    # Check for informational patterns first
    for pat in INFORMATIONAL_PATTERNS:
        if re.search(pat, text_lower):
            return {
                "needs_grill": False,
                "action": "bypass",
                "reason": "Информационный запрос или команда мониторинга; интервью не требуется.",
                "profile": "general",
                "confidence": 0.95,
            }

    # Check for project/feature/bug/research patterns
    for pat in PROJECT_FEATURE_PATTERNS:
        if re.search(pat, text_lower):
            prof, conf, reason = detect_profile(text_clean)
            return {
                "needs_grill": True,
                "action": "grill",
                "reason": f"Обнаружен запрос на разработку/изменение; запуск grill-v1 ({reason}).",
                "profile": prof,
                "confidence": conf,
            }

    # If text is long enough (> 4 words) and not a short factual query, treat as potential goal
    words = re.findall(r"[\w-]+", text_lower)
    if len(words) >= 4:
        prof, conf, reason = detect_profile(text_clean)
        return {
            "needs_grill": True,
            "action": "grill",
            "reason": f"Неоднозначный запрос требует уточнения требований в grill-v1 ({reason}).",
            "profile": prof,
            "confidence": max(0.6, conf),
        }

    return {
        "needs_grill": False,
        "action": "bypass",
        "reason": "Короткий информационный запрос.",
        "profile": "general",
        "confidence": 0.8,
    }
