# Galaxy Kernel 4.0 roadmap

Galaxy 4.0 is the point where the existing context components become one fast,
shared runtime used by real coding agents. The priorities are context quality,
token cost, latency and safe sharing between people and agents.

## Completed foundation

- Persistent incremental Attention index and BM25 postings.
- Source text separated from semantic vectors for selective loading.
- Incremental World Model sync with stable snapshot identifiers.
- Cached structural component maps (capsules).
- Durable World Model event feed and idempotent cache projector.
- Foreground polling watcher with debounce.
- Installable Python package and one-tool MCP adapter for real clients.
- Bounded approximate semantic retrieval with exact candidate reranking.
- Adaptive ANN candidate sizing and exact/ANN ContextBench export modes.
- Persistent Context Packet cache with source freshness checks and memory-sensitive invalidation.
- Paired exact/ANN ContextBench comparison command with optional upstream scoring.
- AST inheritance edges and opt-in Top-15 one-hop graph ablation for three fixed Django tasks.
- RepoBench-R JSONL snippet-ranking adapter with gold-index metrics.

## Remaining before 4.0

### P0 — prove the context runtime

- Validate ANN recall loss and latency on ContextBench and tune candidate limits
  against repository size and task type.
- Run the fixed Django graph ablation with official task snapshots/evaluator and keep the 15-file, 20k-token limits unchanged.
- Run RepoBench-R test splits; report Python cff/cfr separately and preserve the benchmark's data terms.
- Extend packet cache scope when Team Model adds private-user and per-agent
  identities; current cache is isolated by repository and project.
- Run 10–30 real Codex tasks with and without Galaxy. Measure completion rate,
  total input tokens, time to first useful edit, repeated file reads and stale
  context failures.
- Set and validate release targets: warm P95 context assembly below 500 ms,
  typical packets of 5–12k tokens and more than 70% reuse on repeated work.

### P1 — shared Team Model

- Separate shared project truth, private user memory and temporary agent working
  memory. Every item needs owner, visibility, evidence and version.
- Add per-user and per-agent cursors over the shared event feed.
- Add copy-on-write snapshots so several agents can work from one base without
  mutating each other's active context.
- Consolidate completed agent episodes into decisions, failures and reusable
  evidence; keep raw sessions bounded.
- Resolve duplicate and competing memories with trust scores, provenance and an
  explicit reconciliation history.

### P1 — runtime reliability

- Replace the foreground polling loop with an optional background service and
  native filesystem events, while retaining polling as a portable fallback.
- Coordinate concurrent writers and make cache/event updates transactional.
- Add health/status telemetry for snapshot age, cache reuse, event lag, packet
  size and retrieval confidence.
- Produce reproducible wheels, lock dependencies, test Windows installation and
  publish migration rules for `GALAXY_HOME`.

### P2 — product surface

- Add a thin VS Code view for task context, sources, uncertainty and refresh.
- Add Project Multiverse links between repositories only after single-project
  team isolation is measured and stable.
- Expose more MCP tools only from observed demand. Likely candidates are a fast
  component overview and explicit memory feedback; neither is part of the first
  adapter.

## 4.0 definition of done

Version 4.0 should be released only after Galaxy improves real agent work, keeps
team/private context isolated, survives concurrent activity and meets measured
latency/token targets. Feature count alone is not a release criterion.
