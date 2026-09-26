# 3.2.1-alpha.2-contextbench — Codex context integration

- Import the Context OS core, Attention Engine, living memory, ContextBench adapter, configuration and regression tests from the supplied release archive.
- Add `codex_context.py` for fresh context packets and durable, project-scoped chat notes, corrections and retractions.
- Make Galaxy the default external context source through `AGENTS.md`; keep Codex as executor and distinguish retrieved evidence from user instructions.
- Keep chat memory and generated state local and ignored by Git. Preserve legacy entry points for compatibility.
- README files are unchanged; personal vault content from the archive is not imported.

# 1.7.1 — Interactive Core Chat

- Интерактивный диалоговый интерфейс REPL (`chat.py` и `solar chat`) для прямого общения с ядром Galaxy и его 8 планетными агентами через провайдеры `codex` (`codex exec`) и `antigravity` (`agy -p`).
- Поддержка динамического переключения ролей (`/role Sun`, `/role Earth` и т.д.) с автоматической загрузкой системных промптов из `system/agents/<role>.md` и спецификаций `AGENT_SPECS`.
- Загрузка контекста задачи (`/task TASK-XXX`): подтягивание спецификации, frontmatter и последних рантайм-запусков из `tasks/<task>/` прямо в контекст диалога.
- Интеграция с локальной памятью `MemoryStore` (`/memory <query>`): автоматическое обогащение промпта релевантными знаниями из SQLite базы `memory/index.db`.
- Удобное сохранение сессий в Markdown (`/save`), очистка истории (`/clear`) и обработка прерываний.

# 1.7.0 — Verified Flow

- Строгий JSON-контракт; status модели отделён от exit code, stderr — от результата.
- Содержательные выводы и сохранённые артефакты передаются следующим агентам.
- Основной agent-run исполняется через WorkerMesh; события из старых запусков не смешиваются.
- Heartbeat, claim token, проверка владельца ACK/NACK, дедупликация, закрытие SQLite-соединений.
- Атомарный checkpoint + ACK + следующий шаг, run_id, workspace lock, безопасное возобновление.
- Детерминированная QA до Mercury, проверка hash/HEAD, защита существующих входов верификации.
- Mercury в копии; отдельная явная финализация Git с проверкой кандидата и новых файлов.
- Память содержательных результатов с происхождением, индексом слов и фильтром проекта.
- MCP JSON-lines, ролевые права, symlink-проверки, opt-in терминал без неявного shell.
- Хеширование содержимого вместо сравнения git status; ограничения действуют и без Git.
- Согласованные версии, актуальный README, старые описания перенесены в docs.
- Регрессионные тесты используют реальные SQLite/Git/Node и подставные модели; живой LLM не проверялся.

## Что не входит

OS sandbox, автоматическое параллельное ветвление плана, Supabase, embeddings, сетевые воркеры,
распределённый lock, exactly-once внешние действия, автоматический повтор неопределённого шага,
безопасный запуск недоверенных shell/CLI-программ и автоматическая отправка коммитов.

## Совместимость API

`claim` возвращает `(id, event, token)`; `ack`/`nack` требуют worker_id и token.
`receive` больше не подтверждает сообщение автоматически.
`terminal.run` принимает массив argv. Mercury больше не имеет git.commit.
`GalaxyOrchestrator.run` возвращает DONE только после реальной команды проверки.
Новые run state находятся в SQLite и tasks/<task>/runs/<run_id>/state.json.
