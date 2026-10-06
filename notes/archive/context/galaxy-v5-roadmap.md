> Исторический документ из legacy Amnesia. Не является действующим планом Galaxy. См. [решение об отказе от контекстного ядра](../../context-retirement.md).

# Galaxy 5.0 — узкая основа и проверка пользы

**Аудитория:** разработчики, которые работают с Codex и другими coding agents.  
**Главная цель:** проверить, может ли Galaxy запомнить подтверждённый опыт одной задачи и улучшить следующую.

В этой версии Galaxy не строит отдельную большую Core/OS платформу. Scope ограничен пятью механизмами: **Context, Memory, Experience, Skills/Learning и Evidence**. Codex, Claude, OpenCode и другие модели остаются внешними исполнителями за адаптерами.

## Точка А

В репозитории уже есть локальный Python-пакет, CLI, read-only MCP `get_context`, legacy ContextCompiler/retrieval path, SQLite memory/experience stores и benchmark/evidence tooling. Наличие компонентов не доказывает продуктовую пользу. Реальные связи и ограничения описаны в [текущей карте архитектуры](architecture/current-state.md), scope — в [пяти основах](architecture/foundation.md), claims о коде — в [внутренней source note](sources/galaxy-superapp/01-current-galaxy.md).

## Текущий объём

1. Сохранить legacy retrieval и rendering, чтобы OFF/ON сравнения оставались сопоставимыми.
2. Представлять передачу контекста явным package с task/version/items/source/reason/confidence/token estimate/rank/hash.
3. Записывать context inventory и связывать элементы с наблюдаемыми действиями агента. Path observed не считать доказательством semantic usefulness.
4. Сохранять provenance и evidence для Memory и компактного Experience; raw trajectory оставлять в run archive.
5. Подготовить изолированный experiment **Task A → Task B**: одинаковый агент без Experience и с выбранным прошлым Experience.
6. Выявлять точные повторяющиеся lessons из независимых evidence-backed runs и создавать только `SkillCandidate`.
7. Не активировать candidate автоматически. Для проверки требуются отдельные held-out задачи, benchmark evidence и явный review.

## Порядок работ

### Шаг 1. Наблюдаемость и совместимость

- Убедиться, что MCP, SWE-bench OFF/ON и paired suite сохраняют один и тот же OFF baseline и прежний rendered packet.
- Для ON сохранять package JSON, inventory, hashes, raw trace, evaluator output и compact experience record.
- Не выводить семантическое использование из открытия или редактирования файла.

### Шаг 2. Experience reuse experiment

- Выбрать связанные Task A и Task B с детерминированной проверкой.
- Выполнить Task A и сохранить минимальный Experience с source run, outcome и evidence.
- Зафиксировать Task B и evaluation до запуска; сравнить isolated arms без прошлого опыта и с Experience A.
- Считать verified success, trajectory tokens, searches, edits, tool calls, repeated mistakes, duration, retrieved experience и отдельно наблюдаемое совпадение действий.
- Хранить manifest, версии, traces, evaluator logs и хеши; публиковать null/неопределённые поля как null.

### Шаг 3. Learning candidate calibration

- Создавать candidates только из повторений на независимых run IDs с evidence.
- Различать verified-success strategies и failure hypotheses; смешанный исход не сливать в единое правило.
- Проверять candidate на held-out задачах; до этого не передавать его агенту как validated skill.

## Acceptance criteria

- Текущая архитектура и legacy/experimental границы описаны по реальным imports/calls.
- ContextPackage имеет структуру и точный rendered text; ON artifacts позволяют восстановить, что Galaxy передала.
- OFF остаётся настоящим baseline без Galaxy context.
- Memory/Experience/Skill records имеют прослеживаемое происхождение/evidence.
- Наблюдаемое path/action использование отдельно от semantic usefulness.
- Task A → Task B сравнивает изолированные arms и проверяет outcome независимо от слов агента.
- Внешние модели остаются исполнителями; нет большого Core/OS/runtime refactor.

## Заморожено

Не развивать без конкретных измерений: Future Graph, сложную World Model, personalities/identities, автономный orchestrator, собственный coding runtime, swarm, десятки агентов, сложный UI, SaaS и mobile. Существующий код не удалять без необходимости; сохранять совместимые import paths и benchmark evidence.

## Следующий эксперимент с наибольшей информационной ценностью

Сначала повторить прямой парный OFF/ON тест текущего Context пути на большем
зафиксированном наборе разнообразных accepted coding-задач и нескольких
повторениях на задачу. Уже выполненный [прямой пилот](galaxy-direct-context-eval-v1.md)
дал по пяти задачам 3/5 проверенных успехов в обеих руках, без смены исхода;
ON потратил на 51.6% больше суммарных токенов. Это ранний сигнал, а не
стабильная оценка. Основная метрика следующего запуска — verified success;
токены, время, стоимость, ошибки retrieval и исправления — дополнительные.
План и критерии остановки фиксируются до запуска, результаты всех рук
проверяются официальным evaluator.

После прямого измерения Context перейти к [real-repository Experience
challenge](real-experience-transfer-v1.md): независимые maintenance-задачи,
intervening task, hidden checks и control/relevant/irrelevant/obsolete условия.
Forced exposure проверяет transfer и вред совета; отдельный native retrieval
replay проверяет eligible/retrieved/delivered и исключение устаревшего опыта.
Для obsolete требуется подтверждённая смена поведения, а не только возраст
записи. Сначала получить реальные source episodes с passing evaluator
evidence, затем зафиксировать их hashes и порядок условий до Task B. Не
запускать цепочку A→B как замену базовой проверке OFF/ON Context.

Внешние исследования и общие продуктовые гипотезы собраны в [карте источников](sources/galaxy-superapp/00-source-map.md). Они не заменяют измерение на задачах.

## Операция Амнезия — исследовательский мандат (2026-10-03)

Цель инициативы — максимально улучшить контекст и память coding-агентов и найти отличительную пользу Galaxy. Исследовательский поиск намеренно шире текущего объёма реализации: допускаются любые гипотезы и теории о памяти, поиске, забывании, обновлении знаний, provenance, персонализации, использовании инструментов и долговременной работе агента, включая идеи из смежных дисциплин и продуктов.

Для новых идей фиксировать тип опоры (внутреннее свидетельство, внешний источник или гипотеза), источник/дату/ограничения и короткий эксперимент с метриками. Теории можно добавлять в [исследовательские зацепки](sources/galaxy-superapp/06-research-leads.md), даже если их применимость пока неизвестна. Широкий поиск сам по себе не меняет scope реализации 5.0: сначала выбрать конкретный вопрос по Context или Memory, найти минимальный эксперимент и сравнить на coding-задачах качество, время, токены, стоимость, свежесть, ошибки retrieval и provenance. Изменение roadmap или реализация новых крупных механизмов требует отдельного подтверждения источниками и результатами.

Недельный [радар источников](sources/galaxy-superapp/08-operation-amnesia-radar.md) действует как исследователь, а не как product decision-maker: может самостоятельно искать, сопоставлять и добавлять гипотезы/источники в research notes; не активирует навыки, не меняет автоматически архитектуру и не внедряет новые runtime или продуктовые функции.
