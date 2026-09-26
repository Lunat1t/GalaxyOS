"""Deterministic interrogation engine coordinator for Galaxy 3.1 grill-v1.

Maintains decision DAG, frontier calculation, round batching (<= 5 questions),
recommendation invariants, revision reopening, pause/resume, budget enforcement,
and completion approval gates.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid
from typing import Any

from galaxy_core.discovery.grill_dag import GrillDAG
from galaxy_core.discovery.grill_models import (
    ConflictStatus,
    GateViolationError,
    GrillAlternative,
    GrillAnswerHistory,
    GrillConflict,
    GrillFact,
    GrillNode,
    GrillQuestion,
    GrillRound,
    GrillSessionMeta,
    InterviewDepth,
    InterviewMode,
    InvalidProposalError,
    InvalidStateError,
    NodeStatus,
    PauseReason,
    SessionNotFoundError,
    SessionStatus,
    UNKNOWN_ANSWERS,
    VAGUE_ANSWERS,
    utcnow,
)
from galaxy_core.discovery.grill_store import GrillStore


DEPTH_CATEGORIES: dict[str, list[str]] = {
    "quick": [
        "outcome", "problem", "users", "current_state",
        "must_have", "out_of_scope", "success", "constraints",
    ],
    "deep": [
        "outcome", "problem", "users", "current_state",
        "must_have", "out_of_scope", "success", "constraints",
        "workflow", "failure_cases", "data_privacy", "integrations",
        "priorities", "time_budget", "autonomy", "deliverables",
    ],
    "exhaustive": [
        "outcome", "problem", "users", "current_state",
        "must_have", "out_of_scope", "success", "constraints",
        "workflow", "failure_cases", "data_privacy", "integrations",
        "priorities", "time_budget", "autonomy", "deliverables",
        "stakeholders", "maintenance",
    ],
}

DEFAULT_DEPENDENCIES: list[tuple[str, str]] = [
    ("problem", "current_state"),
    ("outcome", "must_have"),
    ("users", "must_have"),
    ("must_have", "workflow"),
    ("must_have", "out_of_scope"),
    ("must_have", "success"),
    ("workflow", "failure_cases"),
    ("current_state", "constraints"),
    ("users", "data_privacy"),
    ("constraints", "data_privacy"),
    ("constraints", "integrations"),
    ("must_have", "priorities"),
    ("constraints", "priorities"),
    ("priorities", "time_budget"),
    ("constraints", "autonomy"),
    ("success", "deliverables"),
    ("users", "stakeholders"),
    ("deliverables", "maintenance"),
]


def create_default_question(category: str, node_id: str) -> GrillQuestion:
    """Generate a standard compliant question with 2-4 alternatives, recommendation, and explanation."""
    questions_data: dict[str, dict[str, Any]] = {
        "outcome": {
            "text": "Представьте, что работа уже успешно закончена. Что конкретно должно существовать или измениться?",
            "why": "Определяет конечную цель и форму результата, отличая настоящий результат от общей идеи.",
            "alternatives": [
                GrillAlternative("A", "Работающее приложение или CLI-утилита", "Исполняемая программа с понятным интерфейсом"),
                GrillAlternative("B", "Автоматизированный процесс или скрипт", "Скрипт или пайплайн обработки данных"),
                GrillAlternative("C", "Исследование, спецификация или отчёт", "Документ с архитектурой и выводами"),
                GrillAlternative("D", "Исправленная ошибка и проверочные тесты", "Устранение дефекта с регрессионными тестами"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Работающее приложение или утилита даёт непосредственно проверяемый результат.",
        },
        "problem": {
            "text": "Какую проблему вы хотите решить и почему она важна именно сейчас?",
            "why": "Без чёткого понимания проблемы можно реализовать функцию, которая не принесёт реальной пользы.",
            "alternatives": [
                GrillAlternative("A", "Ручные операции отнимают время и приводят к ошибкам", "Автоматизация рутины"),
                GrillAlternative("B", "Отсутствует единая система или инструмент", "Создание централизованного решения"),
                GrillAlternative("C", "Требуется запустить новый продукт или возможность с нуля", "Новый функционал"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Автоматизация рутинных действий быстрее всего окупает затраты на разработку.",
        },
        "users": {
            "text": "Кто будет пользоваться результатом? Опишите главного пользователя и его уровень подготовки.",
            "why": "Решение для разработчика, администратора и конечного пользователя должно проектироваться по-разному.",
            "alternatives": [
                GrillAlternative("A", "Разработчики и технические специалисты через CLI и API", "Техническая аудитория"),
                GrillAlternative("B", "Конечные пользователи без глубоких технических знаний", "Нетехническая аудитория"),
                GrillAlternative("C", "Внутренняя команда проекта и автоматические агенты", "Системные агенты"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Ориентация на разработчиков обеспечивает простоту автоматизации и интеграции.",
        },
        "current_state": {
            "text": "Как вы решаете эту задачу сейчас? Что уже есть: файлы, код, сервисы или процесс?",
            "why": "Необходимо опираться на существующие наработки и инфраструктуру, а не дублировать код.",
            "alternatives": [
                GrillAlternative("A", "Есть существующий проект и код, требующий расширения", "Доработка имеющегося кода"),
                GrillAlternative("B", "Задача решается вручную или через разрозненные инструменты", "Ручной процесс"),
                GrillAlternative("C", "Проект начинается полностью с нуля", "Чистый старт"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Использование существующего контекста ускоряет разработку и сохраняет совместимость.",
        },
        "must_have": {
            "text": "Назовите функции или свойства, без которых результат будет для вас бесполезен.",
            "why": "Определяет обязательное ядро функциональности, которое нельзя потерять при планировании.",
            "alternatives": [
                GrillAlternative("A", "Базовый детерминированный сценарий и обработка ошибок", "Минимальный рабочий продукт"),
                GrillAlternative("B", "Полная автоматизация процесса с сохранением состояния", "Автономное выполнение"),
                GrillAlternative("C", "Интеграция с существующими компонентами системы", "Совместимость"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Фокус на базовом сценарии гарантирует быстрое получение работающего ядра.",
        },
        "workflow": {
            "text": "Опишите идеальный сценарий пользователя от первого действия до полученного результата.",
            "why": "Последовательность шагов пользователя выявляет пропущенные состояния, данные и проверки.",
            "alternatives": [
                GrillAlternative("A", "Запуск команды -> ввод данных -> автоматическая обработка -> результат", "Линейный CLI поток"),
                GrillAlternative("B", "Интерактивный пошаговый диалог с подтверждением ключевых этапов", "Интерактивный режим"),
                GrillAlternative("C", "Фоновое выполнение по расписанию или системному событию", "Фоновый процесс"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Линейный детерминированный поток легче всего тестировать и автоматизировать.",
        },
        "out_of_scope": {
            "text": "Что точно не нужно делать в первой версии?",
            "why": "Границы проекта защищают реализацию от бесконечного расширения объёма задач.",
            "alternatives": [
                GrillAlternative("A", "Без графического интерфейса (GUI), голоса и мобильных приложений", "Только CLI/API"),
                GrillAlternative("B", "Без внешних облачных зависимостей и сторонних платных API", "Только локально"),
                GrillAlternative("C", "Без многопользовательского режима и совместной работы", "Однопользовательский режим"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Отказ от GUI в первой версии позволяет сосредоточиться на надёжности ядра.",
        },
        "success": {
            "text": "По каким наблюдаемым признакам вы скажете: «Да, задача решена»?",
            "why": "Наблюдаемые признаки успешности становятся критериями приёмки и тестами.",
            "alternatives": [
                GrillAlternative("A", "Все автоматические тесты проходят успешно", "Зелёные автотесты"),
                GrillAlternative("B", "Сценарий выполняется без ошибок за заданное время", "Производительность и стабильность"),
                GrillAlternative("C", "Пользователь подтверждает соответствие результата требованиям", "Ручная приёмка"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Автоматические тесты дают объективное и воспроизводимое подтверждение.",
        },
        "failure_cases": {
            "text": "Какие ошибки, неправильные действия или неприятные ситуации система обязана выдержать?",
            "why": "Система обязана предсказуемо и безопасно обрабатывать сбои и ошибки ввода.",
            "alternatives": [
                GrillAlternative("A", "Сбои сети/провайдера с сохранением состояния и откатом", "Устойчивость к сбоям"),
                GrillAlternative("B", "Ошибки валидации входных данных и понятные сообщения", "Валидация ввода"),
                GrillAlternative("C", "Таймауты и исчерпание лимитов ресурсов", "Ресурсные ограничения"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Сохранение состояния при сбоях предотвращает потерю подтверждённых данных.",
        },
        "constraints": {
            "text": "Какие ограничения уже известны: технологии, устройство, ОС, язык, интернет или код?",
            "why": "Ограничения среды, языка и безопасности влияют на архитектуру с самого начала.",
            "alternatives": [
                GrillAlternative("A", "Стандартный стек Python и локальная SQLite база данных", "Python + SQLite"),
                GrillAlternative("B", "Только локальное исполнение без внешних сетевых запросов", "Полная автономность"),
                GrillAlternative("C", "Кроссплатформенная работа (Linux / macOS)", "Кроссплатформенность"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Стандартная библиотека и SQLite минимизируют внешние зависимости.",
        },
        "data_privacy": {
            "text": "Какие данные будут использоваться? Есть ли личные, платёжные или секретные данные?",
            "why": "Требования к приватности определяют способы хранения данных и маскирования секретов.",
            "alternatives": [
                GrillAlternative("A", "Локальное хранение данных с автоматическим маскированием секретов", "Локально + реджекты"),
                GrillAlternative("B", "Публичные обезличенные данные без ограничений доступа", "Публичные данные"),
                GrillAlternative("C", "Строго изолированное хранилище с контролем доступа", "Изоляция"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Локальное хранение с маскированием защищает конфиденциальные данные.",
        },
        "integrations": {
            "text": "С какими программами, API, устройствами или людьми результат должен взаимодействовать?",
            "why": "Взаимодействие с внешними системами несёт риски несовместимости и отказов.",
            "alternatives": [
                GrillAlternative("A", "Без внешних интеграций, только локальные компоненты системы", "Локальная замкнутость"),
                GrillAlternative("B", "Интеграция с локальными базами знаний и Obsidian", "Obsidian и Second Brain"),
                GrillAlternative("C", "Внешние API по отдельному согласованию", "Внешние сервисы"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Автономная работа без внешних API наиболее надёжна.",
        },
        "priorities": {
            "text": "Расставьте приоритеты: скорость разработки, качество, низкая стоимость, безопасность.",
            "why": "При возникновении компромиссов приоритеты определяют порядок выбора.",
            "alternatives": [
                GrillAlternative("A", "1. Надёжность, 2. Корректность, 3. Совместимость, 4. Скорость", "Качество и стабильность"),
                GrillAlternative("B", "1. Скорость прототипа, 2. Простота реализации", "Быстрый старт"),
                GrillAlternative("C", "1. Безопасность данных, 2. Минимальные ресурсы", "Безопасность"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Приоритет надёжности и совместимости обеспечивает долгосрочную стабильность.",
        },
        "time_budget": {
            "text": "Есть ли срок, этапы или ограничение бюджета? Что должно быть готово первым?",
            "why": "Ограничения времени и ресурсов влияют на декомпозицию и этапы разработки.",
            "alternatives": [
                GrillAlternative("A", "Поэтапная вертикальная реализация без жёсткого дедлайна", "Инкрементальная поставка"),
                GrillAlternative("B", "Быстрый прототип за короткий срок", "Быстрое демо"),
                GrillAlternative("C", "Полная реализация со всеми тестами за один этап", "Монолитный релиз"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Вертикальные инкременты позволяют валидировать каждый шаг.",
        },
        "autonomy": {
            "text": "Что Galaxy может делать самостоятельно, а что обязана каждый раз согласовывать с вами?",
            "why": "Границы автономности определяют, какие действия агент выполняет без подтверждения.",
            "alternatives": [
                GrillAlternative("A", "Автоматический анализ и чтение; запись и модификация только после подтверждения", "Human in the loop"),
                GrillAlternative("B", "Полная автономность во всех действиях", "Автономия"),
                GrillAlternative("C", "Подтверждение каждого отдельного шага", "Строгий контроль"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Чтение read-only безопасно, а контроль записи защищает кодовую базу.",
        },
        "deliverables": {
            "text": "В каком виде вы хотите получить результат и объяснение?",
            "why": "Формат сдачи определяет структуру артефактов и документации.",
            "alternatives": [
                GrillAlternative("A", "Исходный код, автоматические тесты и документация по архитектуре", "Полный комплект"),
                GrillAlternative("B", "Только работающий код без дополнительной документации", "Минимум артефактов"),
                GrillAlternative("C", "Пошаговая инструкция и отчёт о тестировании", "Отчёт и инструкции"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Код вместе с тестами и документацией обеспечивает долгосрочную поддержку.",
        },
        "stakeholders": {
            "text": "Кто ещё влияет на решение или должен принять результат? В чём их интересы?",
            "why": "Влияющие стороны могут выдвинуть дополнительные требования к приёмке.",
            "alternatives": [
                GrillAlternative("A", "Основной разработчик и пользователи системы", "Разработчик и юзеры"),
                GrillAlternative("B", "Команда сопровождения и архитекторы", "Архитекторы"),
                GrillAlternative("C", "Только автор задачи", "Один участник"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Учёт интересов пользователей и разработчиков предотвращает переделки.",
        },
        "maintenance": {
            "text": "Кто будет поддерживать результат через полгода и насколько легко его должно быть изменять?",
            "why": "Определяет, кто и как будет поддерживать и развивать решение в будущем.",
            "alternatives": [
                GrillAlternative("A", "Сопровождающие репозитория Galaxy на основе тестов и спецификации", "Открытая поддержка"),
                GrillAlternative("B", "Автор задачи для личного использования", "Личная поддержка"),
                GrillAlternative("C", "Решение временное, долгосрочная поддержка не планируется", "Временное решение"),
            ],
            "recommendation": "A",
            "recommendation_reason": "Стандартные контракты и тесты позволяют любому сопровождающему поддерживать код.",
        },
    }

    default_info = questions_data.get(category, {
        "text": f"Уточните требования для раздела '{category}'.",
        "why": f"Необходимо для завершения спецификации раздела {category}.",
        "alternatives": [
            GrillAlternative("A", "Принять стандартное решение", "Стандарт"),
            GrillAlternative("B", "Сформировать индивидуальное требование", "Кастом"),
        ],
        "recommendation": "A",
        "recommendation_reason": "Стандартное решение минимизирует риски.",
    })

    q = GrillQuestion(
        id=f"Q_{category.upper()}_01",
        node_id=node_id,
        category=category,
        text=default_info["text"],
        why=default_info["why"],
        alternatives=default_info["alternatives"],
        recommendation=default_info["recommendation"],
        recommendation_reason=default_info.get("recommendation_reason", ""),
        allow_custom=True,
    )
    q.validate()
    return q


class GrillEngine:
    """Authoritative coordinator for Galaxy 3.1 grill-v1 discovery interviews."""

    def __init__(self, root: str | Path, store: GrillStore | None = None):
        self.root = Path(root)
        self.store = store or GrillStore(self.root)

    def start_session(
        self,
        idea: str,
        project: str = "default",
        profile: str = "general",
        mode: str = InterviewMode.RELENTLESS,
        depth: str = InterviewDepth.DEEP,
        budget_limit: int = 30,
    ) -> tuple[GrillSessionMeta, GrillRound | None]:
        """Start a new deterministic grill-v1 interview session."""
        if not idea or not idea.strip():
            raise ValueError("initial idea is required")
        if depth not in InterviewDepth.DEPTH_LIMITS:
            raise ValueError("depth must be quick, deep or exhaustive")
        if mode not in InterviewMode.ALL:
            raise ValueError("mode must be relentless or normal")
        if budget_limit < 4 or budget_limit > 100:
            raise ValueError("budget_limit must be between 4 and 100")

        session_id = "GRILL-" + uuid.uuid4().hex[:12].upper()
        now = utcnow()

        meta = GrillSessionMeta(
            session_id=session_id,
            project=project.strip() or "default",
            initial_idea=idea.strip(),
            engine="grill-v1",
            profile=profile,
            mode=mode,
            depth=depth,
            status=SessionStatus.DISCOVERY,
            budget_limit=budget_limit,
            budget_used=0,
            created_at=now,
            updated_at=now,
        )

        # Build initial DAG from depth categories and dependencies
        dag = GrillDAG()
        active_categories = DEPTH_CATEGORIES.get(depth, DEPTH_CATEGORIES["deep"])
        active_set = set(active_categories)

        for cat in active_categories:
            nid = f"node_{cat}"
            q = create_default_question(cat, nid)
            node = GrillNode(
                id=nid,
                category=cat,
                title=cat.replace("_", " ").title(),
                description=q.text,
                status=NodeStatus.BLOCKED,
                question=q,
                created_at=now,
                updated_at=now,
            )
            dag.add_node(node)

        for parent_cat, child_cat in DEFAULT_DEPENDENCIES:
            if parent_cat in active_set and child_cat in active_set:
                p_nid = f"node_{parent_cat}"
                c_nid = f"node_{child_cat}"
                dag.add_dependency(p_nid, c_nid)

        # Recalculate frontier
        dag.recalculate_frontier()

        # Batch first round: at most 5 independent frontier questions
        batch = dag.get_independent_frontier_batch(max_batch=5)
        initial_round: GrillRound | None = None
        if batch:
            questions = [n.question for n in batch if n.question]
            initial_round = GrillRound(
                session_id=session_id,
                round_no=1,
                status="active",
                questions=questions,
                answers={},
                created_at=now,
            )
            meta.budget_used += 1

        # Atomically persist in SQLite v3
        self.store.create_session(meta, dag, initial_round)
        return meta, initial_round

    def get_session(self, session_id: str) -> tuple[GrillSessionMeta, GrillDAG, list[GrillRound]] | None:
        return self.store.load_session(session_id)

    def require_session(self, session_id: str) -> tuple[GrillSessionMeta, GrillDAG, list[GrillRound]]:
        res = self.get_session(session_id)
        if not res:
            raise SessionNotFoundError(f"Interview session not found: {session_id}")
        return res

    def next_round(self, session_id: str) -> GrillRound | None:
        """Advance or return the current pending round for the session."""
        meta, dag, rounds = self.require_session(session_id)

        # Handle pause state
        if meta.status == SessionStatus.PAUSED:
            return None

        if meta.status != SessionStatus.DISCOVERY:
            return None

        # Check budget limit
        if meta.budget_used >= meta.budget_limit:
            meta.status = SessionStatus.PAUSED
            meta.pause_reason = PauseReason.BUDGET_EXHAUSTED
            self.store.save_session(meta, dag)
            return None

        # If there is already an active round, return it
        if rounds and rounds[-1].status == "active":
            return rounds[-1]

        # Get frontier batch (at most 5 questions, or all if < 3)
        batch = dag.get_independent_frontier_batch(max_batch=5)
        if batch:
            round_no = self.store.next_round_no(session_id)
            questions = [n.question for n in batch if n.question]
            new_round = GrillRound(
                session_id=session_id,
                round_no=round_no,
                status="active",
                questions=questions,
                answers={},
                created_at=utcnow(),
            )
            meta.budget_used += 1
            self.store.save_session(meta, dag, new_round)
            return new_round

        # Frontier is empty: check completion gates
        is_ready, reasons = dag.is_draft_ready(mode=meta.mode)
        if is_ready:
            self._finish_draft(meta, dag)
            return None

        return None

    def submit_answers(
        self,
        session_id: str,
        round_no: int,
        answers: dict[str, Any] | None = None,
        accept_recommendations: bool = False,
    ) -> tuple[GrillSessionMeta, GrillRound | None]:
        """Process answers for the active round and update the decision DAG.

        Supports option IDs, free-form text, 'не знаю', partial answers,
        and current-round recommendation acceptance.
        """
        meta, dag, rounds = self.require_session(session_id)
        if meta.status != SessionStatus.DISCOVERY:
            raise InvalidStateError(f"Cannot submit answers for session in status {meta.status}")

        answers = dict(answers or {})

        current_round = None
        for r in rounds:
            if r.round_no == round_no:
                current_round = r
                break

        if not current_round:
            raise InvalidStateError(f"Round {round_no} not found for session {session_id}")

        if current_round.status == "completed":
            # Check duplicate-answer idempotency for an already completed round
            is_dup = True
            if answers:
                for idx, q in enumerate(current_round.questions):
                    ans = (
                        answers.get(q.id)
                        or answers.get(q.node_id)
                        or answers.get(str(idx + 1))
                        or answers.get(idx + 1)
                        or answers.get(str(idx))
                    )
                    if ans is not None:
                        prev = current_round.answers.get(q.id) or {}
                        prev_text = prev.get("answer") if isinstance(prev, dict) else str(prev)
                        prev_opt = prev.get("option_id") if isinstance(prev, dict) else None
                        ans_clean = str(ans).strip().lower()
                        if ans_clean != (prev_text or "").strip().lower() and (not prev_opt or ans_clean != prev_opt.strip().lower()):
                            is_dup = False
                            break
            if is_dup:
                active_rnd = None
                for r in rounds:
                    if r.status == "active":
                        active_rnd = r
                        break
                if not active_rnd and meta.status == SessionStatus.DISCOVERY:
                    active_rnd = self.next_round(session_id)
                    meta, _, _ = self.require_session(session_id)
                return meta, active_rnd
            raise InvalidStateError(f"Round {round_no} is already completed for session {session_id}")

        if current_round.status != "active":
            raise InvalidStateError(f"Round {round_no} has status '{current_round.status}', not active")

        history: list[GrillAnswerHistory] = []
        now = utcnow()

        # If user bulk-accepted recommendations for this round
        if accept_recommendations:
            for q in current_round.questions:
                if q.id not in answers and q.node_id not in answers:
                    answers[q.id] = q.recommendation

        # Process each question in the round
        for idx, q in enumerate(current_round.questions):
            raw_answer = (
                answers.get(q.id)
                or answers.get(q.node_id)
                or answers.get(str(idx + 1))
                or answers.get(idx + 1)
                or answers.get(str(idx))
            )
            if raw_answer is None:
                # Question skipped/unanswered: remains open on frontier
                continue

            raw_answer = str(raw_answer).strip()
            if not raw_answer:
                continue

            node = dag.get_node(q.node_id)
            if not node:
                continue

            # Support recommendation keywords
            raw_lower = raw_answer.lower()
            rec_keywords = {
                "рекомендация", "рекомендацию", "принять рекомендацию", "рек",
                "rec", "recommendation", "accept",
            }
            if raw_lower in rec_keywords:
                raw_answer = q.recommendation
                raw_lower = raw_answer.lower()

            # Resolve option ID vs custom text
            resolved_answer = raw_answer
            matched_option_id: str | None = None
            is_custom = True

            for alt in q.alternatives:
                if alt.id.lower() == raw_lower or alt.text.lower() == raw_lower:
                    resolved_answer = alt.text
                    matched_option_id = alt.id
                    is_custom = False
                    break

            # Check duplicate-answer idempotency against current node state
            if node.status in NodeStatus.RESOLVED and (node.answer or node.answer_option_id):
                is_duplicate = False
                if matched_option_id and node.answer_option_id == matched_option_id:
                    is_duplicate = True
                elif node.answer and node.answer.strip().lower() == resolved_answer.strip().lower():
                    is_duplicate = True

                if is_duplicate:
                    # Idempotent re-submission
                    current_round.answers[q.id] = {
                        "answer": node.answer,
                        "option_id": node.answer_option_id,
                        "quality": node.answer_quality or "adequate",
                        "is_custom": node.is_custom_answer,
                    }
                    continue
                else:
                    # Answer changed: record history
                    history.append(
                        GrillAnswerHistory(
                            session_id=session_id,
                            node_id=node.id,
                            answer=node.answer or node.assumption_text or "",
                            quality=node.answer_quality or "",
                            actor="user",
                            rationale="Answer revised via re-submission",
                        )
                    )

            if not is_custom:
                words = re.findall(r"[\w-]+", resolved_answer, flags=re.UNICODE)
                quality = "detailed" if len(words) >= 10 else "adequate"
            else:
                quality = self._evaluate_quality(resolved_answer)

            # Handle unknown answers ("не знаю" / "unknown")
            if quality == "unknown":
                node.status = NodeStatus.ASSUMPTION
                node.assumption_text = (
                    f"{node.category}: точное решение не определено пользователем; "
                    f"принято допущение по рекомендации '{q.recommendation}'."
                )
                node.assumption_verification = (
                    f"Проверить допущение по разделу {node.category} перед формированием DAG."
                )
                node.answer = resolved_answer
                node.answer_option_id = matched_option_id
                node.answer_quality = quality
                node.is_custom_answer = is_custom
                node.answered_at = now
            elif meta.mode == InterviewMode.RELENTLESS and quality == "vague":
                # In relentless mode, vague answers keep branch open or convert to assumption
                node.status = NodeStatus.ASSUMPTION
                node.assumption_text = (
                    f"{node.category}: дан общий/неточный ответ '{resolved_answer}'."
                )
                node.assumption_verification = (
                    f"Уточнить требования к {node.category} в процессе реализации."
                )
                node.answer = resolved_answer
                node.answer_option_id = matched_option_id
                node.answer_quality = quality
                node.is_custom_answer = is_custom
                node.answered_at = now
            else:
                node.status = NodeStatus.ANSWERED
                node.answer = resolved_answer
                node.answer_option_id = matched_option_id
                node.answer_quality = quality
                node.is_custom_answer = is_custom
                node.answered_at = now

            node.updated_at = now
            current_round.answers[q.id] = {
                "answer": resolved_answer,
                "option_id": matched_option_id,
                "quality": quality,
                "is_custom": is_custom,
            }

        # Mark round completed
        current_round.status = "completed"
        current_round.completed_at = now

        # Recalculate frontier
        dag.recalculate_frontier()

        # Save state atomically
        self.store.save_session(meta, dag, current_round, history)

        # Prepare next round or finalize draft
        next_rnd = self.next_round(session_id)
        # Reload meta after next_round
        updated_meta, _, _ = self.require_session(session_id)
        return updated_meta, next_rnd

    def revise_answer(
        self,
        session_id: str,
        node_id: str,
        new_answer: str | None = None,
        actor: str = "user",
        rationale: str | None = None,
    ) -> tuple[GrillSessionMeta, GrillRound | None]:
        """Atomically reopen a node and all transitive descendants upon answer revision.

        Preserves previous answers in GrillAnswerHistory.
        """
        meta, dag, rounds = self.require_session(session_id)

        target_node_id = node_id
        if target_node_id not in dag.nodes:
            if f"node_{node_id}" in dag.nodes:
                target_node_id = f"node_{node_id}"
            else:
                for n in dag.nodes.values():
                    if n.category == node_id:
                        target_node_id = n.id
                        break

        # Reopen target and descendants
        history_records = dag.reopen_node(
            node_id=target_node_id,
            session_id=session_id,
            actor=actor,
            rationale=rationale or "User initiated revision",
        )

        # Reset draft status if previously ready
        if meta.status in {SessionStatus.DRAFT_READY, SessionStatus.CONFIRMED}:
            meta.status = SessionStatus.DISCOVERY
            meta.brief = None

        # Apply new answer if provided
        if new_answer and str(new_answer).strip():
            node = dag.get_node(target_node_id)
            if node:
                raw_ans = str(new_answer).strip()
                raw_lower = raw_ans.lower()
                resolved_ans = raw_ans
                opt_id = None
                is_custom = True
                if node.question:
                    for alt in node.question.alternatives:
                        if alt.id.lower() == raw_lower or alt.text.lower() == raw_lower:
                            resolved_ans = alt.text
                            opt_id = alt.id
                            is_custom = False
                            break
                if not is_custom:
                    words = re.findall(r"[\w-]+", resolved_ans, flags=re.UNICODE)
                    quality = "detailed" if len(words) >= 10 else "adequate"
                else:
                    quality = self._evaluate_quality(resolved_ans)

                now = utcnow()
                node.answer = resolved_ans
                node.answer_option_id = opt_id
                node.answer_quality = quality
                node.is_custom_answer = is_custom
                node.answered_at = now
                node.updated_at = now

                if quality == "unknown":
                    node.status = NodeStatus.ASSUMPTION
                    node.assumption_text = (
                        f"{node.category}: точное решение не определено пользователем; "
                        f"принято допущение."
                    )
                    node.assumption_verification = (
                        f"Проверить допущение по разделу {node.category} перед формированием DAG."
                    )
                elif meta.mode == InterviewMode.RELENTLESS and quality == "vague":
                    node.status = NodeStatus.ASSUMPTION
                    node.assumption_text = (
                        f"{node.category}: дан общий/неточный ответ '{resolved_ans}'."
                    )
                    node.assumption_verification = (
                        f"Уточнить требования к {node.category} в процессе реализации."
                    )
                else:
                    node.status = NodeStatus.ANSWERED

                dag.recalculate_frontier()

        # Invalidate any currently active round since the frontier has changed
        superseded_round = None
        for r in rounds:
            if r.status == "active":
                r.status = "superseded"
                r.completed_at = utcnow()
                superseded_round = r
                break

        # Save updated DAG, session meta, and history atomically
        self.store.save_session(meta, dag, current_round=superseded_round, history=history_records)

        # Prepare next round
        next_rnd = self.next_round(session_id)
        updated_meta, _, _ = self.require_session(session_id)
        return updated_meta, next_rnd

    def pause_session(
        self, session_id: str, reason: str = PauseReason.USER_REQUESTED
    ) -> GrillSessionMeta:
        """Pause session execution."""
        meta, dag, _ = self.require_session(session_id)
        if meta.status == SessionStatus.DISCOVERY:
            meta.status = SessionStatus.PAUSED
            meta.pause_reason = reason
            self.store.save_session(meta, dag)
        return meta

    def resume_session(
        self, session_id: str, additional_budget: int = 0
    ) -> tuple[GrillSessionMeta, GrillRound | None]:
        """Resume a paused interview session, optionally increasing the question budget."""
        meta, dag, _ = self.require_session(session_id)
        if meta.status == SessionStatus.PAUSED:
            if additional_budget > 0:
                meta.budget_limit += additional_budget
            meta.status = SessionStatus.DISCOVERY
            meta.pause_reason = None
            self.store.save_session(meta, dag)
        next_rnd = self.next_round(session_id)
        updated_meta, _, _ = self.require_session(session_id)
        return updated_meta, next_rnd

    def increase_budget(self, session_id: str, additional_budget: int) -> GrillSessionMeta:
        """Increase the session question budget and unpause if paused due to budget."""
        if additional_budget <= 0:
            raise ValueError("additional_budget must be positive")
        meta, dag, _ = self.require_session(session_id)
        meta.budget_limit += additional_budget
        if meta.status == SessionStatus.PAUSED and meta.pause_reason == PauseReason.BUDGET_EXHAUSTED:
            meta.status = SessionStatus.DISCOVERY
            meta.pause_reason = None
        self.store.save_session(meta, dag)
        return meta

    def add_fact(
        self,
        session_id: str,
        fact: str,
        node_id: str | None = None,
        source_type: str = "user",
        source_ref: str = "",
        confidence: float = 1.0,
        actor: str = "user",
    ) -> GrillFact:
        """Add and persist a verified fact for the session."""
        meta, dag, _ = self.require_session(session_id)
        fact_obj = GrillFact(
            session_id=session_id,
            fact=fact,
            node_id=node_id,
            source_type=source_type,
            source_ref=source_ref,
            confidence=confidence,
            actor=actor,
        )
        dag.add_fact(fact_obj)
        self.store.save_session(meta, dag)
        return fact_obj

    def add_conflict(
        self,
        session_id: str,
        description: str,
        node_ids: list[str],
    ) -> GrillConflict:
        """Register a conflict/contradiction between nodes."""
        meta, dag, _ = self.require_session(session_id)
        conflict = GrillConflict(
            session_id=session_id,
            description=description,
            node_ids=node_ids,
            status=ConflictStatus.ACTIVE,
        )
        dag.add_conflict(conflict)
        # If session was draft ready, revert to discovery because active conflict exists
        if meta.status in {SessionStatus.DRAFT_READY, SessionStatus.CONFIRMED}:
            meta.status = SessionStatus.DISCOVERY
            meta.brief = None
        self.store.save_session(meta, dag)
        return conflict

    def resolve_conflict(
        self,
        session_id: str,
        conflict_id: str,
        resolution: str,
    ) -> GrillConflict:
        """Resolve a conflict with an explanation."""
        meta, dag, _ = self.require_session(session_id)
        dag.resolve_conflict(conflict_id, resolution)
        self.store.save_session(meta, dag)
        return dag.conflicts[conflict_id]

    def accept_conflict(
        self,
        session_id: str,
        conflict_id: str,
        rationale: str,
    ) -> GrillConflict:
        """Explicitly accept a conflict with documented rationale."""
        meta, dag, _ = self.require_session(session_id)
        dag.accept_conflict(conflict_id, rationale)
        self.store.save_session(meta, dag)
        return dag.conflicts[conflict_id]

    def get_frontier(self, session_id: str) -> list[GrillNode]:
        """Return all nodes currently on the frontier."""
        _, dag, _ = self.require_session(session_id)
        frontier_ids = dag.recalculate_frontier()
        return [dag.nodes[nid] for nid in frontier_ids]

    def confirm_brief(self, session_id: str, confirmed_by: str = "user") -> GrillSessionMeta:
        """Gate 1: User explicitly confirms the draft Goal Brief."""
        meta, dag, _ = self.require_session(session_id)
        if meta.status != SessionStatus.DRAFT_READY or not meta.brief:
            raise GateViolationError("interview has no draft brief to confirm")

        meta.status = SessionStatus.CONFIRMED
        meta.brief["confirmed_at"] = utcnow()
        meta.brief["confirmed_by"] = confirmed_by
        self.store.save_session(meta, dag)
        return meta

    def planning_prompt(self, session_id: str, confirmed_only: bool = True) -> str:
        """Gate 2 Guard: Retrieve planning prompt only after Gate 1 confirmation."""
        meta, _, _ = self.require_session(session_id)
        if confirmed_only and meta.status != SessionStatus.CONFIRMED:
            raise GateViolationError("the user must confirm the Goal Brief before planning")
        if not meta.brief:
            raise ValueError("Goal Brief is not ready")
        return meta.brief.get("planning_prompt", "")

    def authorize_dag_creation(self, session_id: str, authorized_by: str = "user") -> dict[str, Any]:
        """Gate 2: Authorize DAG creation from confirmed Goal Brief."""
        meta, dag, _ = self.require_session(session_id)
        if meta.status != SessionStatus.CONFIRMED or not meta.brief:
            raise GateViolationError("Premature DAG creation rejected: Goal Brief must be explicitly confirmed first")
        now = utcnow()
        meta.brief["dag_authorized"] = True
        meta.brief["dag_authorized_at"] = now
        meta.brief["dag_authorized_by"] = authorized_by
        self.store.save_session(meta, dag)
        return {
            "authorized": True,
            "session_id": session_id,
            "authorized_by": authorized_by,
            "authorized_at": now,
        }

    def authorize_implementation(self, session_id: str, authorized_by: str = "user") -> dict[str, Any]:
        """Gate 3: Authorize Earth code implementation / writes."""
        meta, dag, _ = self.require_session(session_id)
        if meta.status != SessionStatus.CONFIRMED or not meta.brief:
            raise GateViolationError("Premature implementation rejected: Goal Brief must be confirmed first")
        if not meta.brief.get("dag_authorized"):
            raise GateViolationError("Premature implementation rejected: DAG creation must be authorized before implementation")
        now = utcnow()
        meta.brief["implementation_authorized"] = True
        meta.brief["implementation_authorized_at"] = now
        meta.brief["implementation_authorized_by"] = authorized_by
        self.store.save_session(meta, dag)
        return {
            "authorized": True,
            "session_id": session_id,
            "authorized_by": authorized_by,
            "authorized_at": now,
        }

    def get_tree(self, session_id: str) -> dict[str, Any]:
        """Return full structured decision tree for the session."""
        meta, dag, _ = self.require_session(session_id)
        return {
            "interview_id": meta.session_id,
            "status": meta.status,
            "mode": meta.mode,
            "profile": meta.profile,
            "nodes": {nid: n.to_dict() for nid, n in dag.nodes.items()},
            "dependencies": [[d.parent_id, d.child_id] for d in dag.dependencies],
            "conflicts": [c.to_dict() for c in dag.conflicts.values()],
            "assumptions": [
                n.to_dict() for n in dag.nodes.values() if n.status == NodeStatus.ASSUMPTION
            ],
            "facts": [f.to_dict() for f in dag.facts.values()],
        }

    def _finish_draft(self, meta: GrillSessionMeta, dag: GrillDAG) -> None:
        """Build draft Goal Brief when frontier is empty and invariants pass."""
        requirements: dict[str, Any] = {}
        assumptions: list[str] = []

        for node in dag.nodes.values():
            if node.status == NodeStatus.ANSWERED and node.answer:
                requirements[node.category] = {
                    "answer": node.answer,
                    "quality": node.answer_quality or "adequate",
                    "option_id": node.answer_option_id,
                }
            elif node.status == NodeStatus.ASSUMPTION:
                desc = node.assumption_text or f"{node.category}: не определено"
                if node.assumption_verification:
                    desc += f" (проверка: {node.assumption_verification})"
                assumptions.append(desc)

        title = meta.initial_idea.strip().splitlines()[0][:100]
        brief = {
            "schema_version": "3.0",
            "interview_id": meta.session_id,
            "title": title,
            "project": meta.project,
            "initial_idea": meta.initial_idea,
            "depth": meta.depth,
            "mode": meta.mode,
            "profile": meta.profile,
            "requirements": requirements,
            "assumptions": assumptions,
            "conflicts": [c.to_dict() for c in dag.conflicts.values()],
            "created_at": utcnow(),
        }
        brief["planning_prompt"] = self._format_planning_prompt(brief)

        meta.brief = brief
        meta.status = SessionStatus.DRAFT_READY
        self.store.save_session(meta, dag)

    @staticmethod
    def _format_planning_prompt(brief: dict[str, Any]) -> str:
        requirements = "\n".join(
            f"- {cat}: {data['answer']}" for cat, data in brief["requirements"].items()
        )
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

    @staticmethod
    def _evaluate_quality(answer: str) -> str:
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
