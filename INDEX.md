---
title: Galaxy — Verified Flow
version: 1.7.1
---
# Galaxy 1.7.1

Локальный помощник для проверяемых задач и накопления опыта.

- [[README|Запуск, архитектура и ограничения]]
- [[CHANGELOG|Изменения 1.7]]
- [[system/mcp/README|MCP]]
- [[tasks/task-002|Пример: калькулятор]]

| Роль | Назначение |
| --- | --- |
| [[system/agents/sun|Sun]] | Маршрутизация |
| [[system/agents/venera|Venera]] | Спецификация |
| [[system/agents/ceres|Ceres]] | Контекст и сведения |
| [[system/agents/mars|Mars]] | Архитектура и риски |
| [[system/agents/earth|Earth]] | Реализация |
| [[system/agents/neptun|Neptun]] | Проверка и исправление реализации |
| [[system/agents/moon|Moon]] | QA и возврат на исправление |
| [[system/agents/mercury|Mercury]] | Итог проверенного запуска |

Актуальные стадии: `python solar.py list`. История запуска и доказательства находятся в `tasks/<task>/runs/<run_id>/`.

При установленном Dataview:

```dataview
TABLE stage AS "Стадия", status AS "Статус", assignee AS "Агент"
FROM "tasks"
WHERE id
SORT file.mtime DESC
```
