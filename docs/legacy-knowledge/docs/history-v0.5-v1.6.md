
## Galaxy v1.2 — Closed Loop

Version 1.2 connects the eight-agent runtime to the canonical task workflow. Agent hops now update the task frontmatter (`inbox/spec/dev/qa`), and an agent run cannot make a task `done` merely because Mercury was reached. After the agent route completes, Solar runs the task's deterministic `verify_command`, applies the existing Definition of Done, and only then writes `stage: done`, `status: done`, `assignee: Mercury`. Failed DoD or terminal orchestration failures are reflected as `blocked` in the task itself. The CLI now returns exit code 0 for a genuinely completed agent workflow.

The release name **Closed Loop** refers to closing the previously separate loops: agent orchestration, QA evidence, and official task state are now one pipeline.
# Galaxy

Локальный каркас «второго мозга» с Obsidian-заметками, MCP-мостом и проверяемыми задачами.

## Быстрый запуск

Требуются Python 3 и Node.js 18+. Из каталога `galaxy/`:

```sh
python3 solar.py list
python3 solar.py verify TASK-002
python3 solar.py audit
```

`solar.py` теперь обнаруживает задачи из `tasks/`, а не хранит их список прямо в коде. Задача становится локально воспроизводимой, если во frontmatter указан `verify_command`. Например TASK-002 содержит `verify_command: node calculator/test_calculator.js`.

## Контракт воспроизводимой задачи

Минимум: `id`, `status`, `stage`, `assignee` и `verify_command`. Команда проверки должна работать из корня vault без ручных действий и возвращать exit code 0 только при успехе. Запись `QA Passed` без такой проверки считается журналом, а не доказательством.

## Безопасность

Секреты не хранятся в задачах и конфигурации. Используйте переменные окружения и локальный `.env`; пример находится в `.env.example`, а реальные `.env*` исключены из Git.

## MCP

Конфигурация: `system/mcp/mcp_config.json`. Мост поддерживает чтение заметок, поиск и добавление строк в существующие заметки. Следующий этап развития — связать жизненный цикл SPEC → CODE → QA → DONE с реальными артефактами и Git-коммитами.

## Аудит

`python3 solar.py audit` ищет очевидно встроенные credentials и неразрешимые Obsidian wikilinks. Ненулевой exit code означает, что репозиторий пока не проходит локальный аудит.

## Galaxy v0.6 — workflow engine

`solar.py` теперь хранит машинный след выполнения и поддерживает конечный автомат задачи:
`inbox → spec → dev → qa → done`, с возвратом `qa → dev` при ошибке и переводом в `blocked` после достижения `circuit_breaker_limit`.

Основные команды:

```bash
python3 solar.py list
python3 solar.py transition TASK-002 dev --reason "reopen"
python3 solar.py run TASK-002
python3 solar.py verify TASK-002
python3 solar.py git-snapshot TASK-002
python3 solar.py audit
```

Каждый запуск workflow создаёт доказательства в `tasks/task-XXX/`: `execution.jsonl`, `full_trace.md` и `last_verify.log`. `git-snapshot` дополнительно создаёт `patch.diff` и `git_status.txt`, но только если vault действительно является Git-репозиторием. Solar намеренно не делает commit автоматически: сначала QA должен завершиться с кодом 0.

## Galaxy v0.6 — evidence-first roles

`solar.py run TASK-XXX` now executes deterministic local role gates instead of merely renaming an assignee: Venera validates required specification sections, Earth inventories real local implementation artifacts, Moon executes `verify_command` and writes strict JSON evidence, and Mercury records closure only after exit code 0. Role evidence is stored under `tasks/task-xxx/roles/`. This does **not** claim that local scripts are autonomous LLM agents; future model/connector adapters can plug into these gates.

Audit now distinguishes machine-local absolute references as `EXTERNAL_PATH` and supports `python3 solar.py audit --json`.


## v0.5 Definition of Done
A task may be `stage: done` only when it has a `verify_command`, Moon evidence with `status=PASS` and `exit_code=0`, and a persisted verification log. Imported historical `QA Passed` text is not sufficient evidence; such tasks are migrated to `blocked / needs_verification` until reproducible QA is available.

Recovered notes are explicitly marked as recovered/metadata-only and never count as implementation or QA evidence.


## Effectiveness benchmark (v0.6)
Run `python3 benchmark.py` from the vault root. It measures repeated TASK-002 verification latency/repeatability, structural audit, Python compilation, and the retry/circuit-breaker path in an isolated temporary copy. Results are written to `benchmark-result.json`. This benchmark measures workflow reliability and overhead; it does not claim to measure LLM coding quality.

## v0.6.1 — benchmark fix

The circuit-breaker benchmark exposed a real workflow bug: after the first QA failure the task returned to `dev`, but Earth only recognized implementation artifacts whose paths contained `/`. Root-level artifacts such as `solar.py` were therefore rejected and the second QA attempt never ran.

v0.6.1 fixes artifact discovery so any backticked path is accepted only when it resolves to an existing local file inside the Galaxy root. The benchmark now validates the complete failure path: QA failure → dev → QA retry → blocked at the configured retry limit.

Run `python3 benchmark.py 30` for the repeatability/failure-handling benchmark and `python3 solar.py audit` for structural/DoD checks.

## Galaxy v0.7

v0.7 adds an opt-in repair step for Earth. A task may declare `repair_command` together with `verify_command`. Solar executes the declared repair without a shell, records `last_repair.log` and Earth evidence, then sends the task to Moon for independent verification. Solar does not invent repair commands; this is a deterministic executor boundary intended for a future Codex/LLM backend.

Run the effectiveness/autonomy benchmark with `python3 benchmark.py 30`. It checks an already-correct task, a deliberately broken artifact repaired through Earth, and a permanent failure that must trip the circuit breaker.

## Galaxy v0.8 — Codex + Antigravity side by side

Earth can delegate implementation to either `codex` or Google Antigravity (`agy`) through `agent_command` / `agent_prompt` task metadata. The safest pattern is to let only one agent write at a time and use the other as an independent reviewer; `agent_mode: sequential` is the default. `agent_mode: parallel` is intentionally rejected for write operations to avoid two agents racing on the same working tree.

Example task frontmatter:

```yaml
agent_backends: codex,antigravity
agent_mode: sequential
agent_prompt: Fix the implementation to satisfy the task specification. Run relevant tests before finishing.
verify_command: python3 tests.py
```

Check local availability with `python3 solar.py agents`. Run the normal pipeline with `python3 solar.py run TASK-XXX`. Provider stdout/stderr is preserved under the task evidence directory.

# Galaxy 1.0 — Eight-Agent Core

Version 1.0 introduces an executable planet layer in `system/agent_core/`. The eight planets — Sun, Venera, Mars, Ceres, Earth, Neptun, Moon and Mercury — are instances of the same `Agent` runtime with separate contracts, prompts, allowed tools and routing rules. A planet is no longer considered executed merely because its name appears in `assignee`.

Production agent execution requires a real provider. Supported providers are Codex (`codex exec`) and Antigravity (`agy -p`). If the requested provider is absent, Galaxy returns `UNAVAILABLE`/`BLOCKED`; it does not fall back to a fake deterministic LLM result.

Use `python3 solar.py agent-contracts` to validate all eight contracts, `python3 solar.py agents` to discover provider availability, and `python3 solar.py agent-run TASK-002 --provider codex` to execute the full eight-planet route. A shorter explicit route can be supplied with `--route Sun,Earth,Neptun,Moon,Mercury`.

The MCP bridge and planet declarations now share the same nine physical tool names: `vault.read`, `vault.search`, `vault.append`, `filesystem.read`, `filesystem.write`, `terminal.run`, `git.diff`, `git.status`, and `git.commit`. Contract tests fail if a planet requests an unregistered tool.

The deterministic v0.9 workflow remains available for reproducible local QA and migration compatibility. It is not counted as an AI-agent execution. Real eight-agent traces are written under `tasks/<task>/agent-v1/`.


## v1.1 Enforcement
Normal `agent-run` is now dynamically routed by the validated `next_agent` result. Every hop persists `pipeline_state.json`. Mercury is code-gated behind a successful Moon hop. Git-backed runs detect unauthorized working-tree writes and commits according to each planet contract. Hop and revisit limits stop routing loops. `--route` remains deterministic debug/test mode.

## Galaxy v1.3 — Capability Sandbox

v1.3 moves privilege separation in front of provider execution at the repository boundary. Sun, Venera, Ceres, Mars and Moon run with a disposable repository copy as their working directory; writes made by those provider processes are discarded and cannot alter the canonical Galaxy worktree through normal relative-path operations. Earth and Neptun are the implementation agents and receive the canonical worktree. Mercury receives it only for final Git closure and is still subject to post-execution policy checks.

Every hop records its `sandbox` mode in `agent-v1/pipeline_state.json`, so the trace proves whether the provider ran in `disposable-copy`, `canonical-write`, or `canonical-finalize` mode.

This is deliberately described as **repository isolation**, not a hardened OS security sandbox. A malicious native process that deliberately targets absolute paths outside its cwd is outside this boundary. A future hardened mode can use an OS/container sandbox when available.

Run the regression check with `python3 test_sandbox_enforcement.py`.

## Galaxy v1.6 — Memory Runtime

v1.5 adds a persistent local **Memory Mesh** and a durable **Galaxy Runtime** event bus. Every real agent hop is now written into `memory/index.db`; before an LLM agent runs, Galaxy retrieves relevant memories for that planet plus shared memories and injects them into the agent context. Successful Moon hops are marked as QA-backed memories, so retrieval can prefer proven experience.

The runtime in `system/agent_core/runtime.py` provides per-planet asyncio queues backed by a durable SQLite event store plus an append-only `traces/runtime_events.jsonl` journal. The orchestrator now emits real `task_start`, `agent_result`, and `handoff` events on every hop, so the runtime is connected to the production agent path rather than existing only as a demo module. It is deliberately a small local runtime, not a claim that eight LLM processes are permanently running in the background. Real intelligence still requires an available Codex or Antigravity CLI; missing providers remain BLOCKED/UNAVAILABLE.

Commands:

```bash
python3 solar.py memory-stats
python3 solar.py memory-search "Fabric animation" --agent Earth
python3 solar.py runtime-status
python3 solar.py runtime-history --task TASK-002 --limit 50
python3 test_memory_runtime.py
```

Memory is local SQLite and deterministic lexical retrieval in v1.5. Embeddings/vector search are intentionally deferred until a real embedding provider and migration strategy exist.

## v1.6 — Worker Mesh storage decision

Galaxy stays **local-first**. SQLite in WAL mode is the default durable store for memory and mailboxes because a single-machine second brain should keep working offline with zero account or network dependency. v1.6 adds leased queue claims, ACK/NACK, crash recovery, and independent `PlanetWorker` loops, so different planet mailboxes can execute concurrently.

Supabase is **not rejected**. It is the planned shared/cloud backend when Galaxy needs multiple computers, a web dashboard, remote workers, realtime subscriptions, or pgvector semantic retrieval. The storage boundary is deliberately kept behind `MemoryStore`/`GalaxyRuntime` so a Postgres/Supabase adapter can be introduced without changing agent behavior. For one laptop, making Supabase mandatory would add network latency, credentials, RLS and outage dependency without improving local execution.

Security note: never put a Supabase secret/service key in task Markdown or the repository. Cloud configuration belongs in environment variables and RLS must protect user-facing access.
