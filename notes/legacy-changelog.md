# Changelog

## 2026-10-06 — alpha.19 second-brain scope

- Remove repository-context modules, commands, MCP dependency and dedicated tests.
- Retain managed memory, vault, goals, experience, verification and memory tests.
- Move ExperienceStore into brain without changing storage paths.
- Require explicit provenance for external memory; identify manual, legacy and run sources.
- Preserve historical context documentation and legacy research; record retirement boundaries.


## 3.2.1-alpha.19-evidence-integrity

- Require passing, hash-matched command logs for `require_evidence`; ignore model-authored evidence strings as proof.
- Re-audit succeeded QA nodes before `DONE` and when a terminal run is reopened; revoke completion on damaged evidence.
- Recheck active plan rules when a previously created run begins execution.
- Mark learned outcomes verified only against trusted verification ledger entries.
- Add tampering, forged-proof and rule-activation regression cases; document remaining trust boundaries.

## 3.2.1-alpha.18-verification

- Add explicit task type and verification contract to agent context.
- Persist deterministic command outcomes, logs and output hashes; carry failed-check evidence into task episodes.
- Propose rules from three independent evidenced failure runs with an explicit lesson and check.
- Replay labeled examples, require attributed promotion and allow immediate disable.
- Gate plans on active downstream command rules and QA completion on active evidence rules.

## 3.2.1-alpha.17-continuity

- Record idempotent, source-linked successful and failed task episodes by project and repository.
- Route bounded relevant prior experience to the next agent by task and role; refresh cached packets on change or retraction.
- Add private/team/project visibility filters for trusted callers and conservative candidate procedure consolidation.
- Stop labeling every passed DAG result as verified; deterministic verification and evidence are required.
- Add sequential offline continuity retrieval evaluation, CLI operations, tests and an end-to-end evaluation protocol.

## 3.2.1-alpha.16-graph-retrieval

- Persist uniquely resolved AST class inheritance in the World Model and maintain it incrementally.
- Add an opt-in directed one-hop import/inheritance expansion from the current Top-15 seeds.
- Add a fixed three-task Django ContextBench graph ablation at 15 files and 20,000 tokens.
- Add a RepoBench-R snippet candidate adapter reporting Accuracy@1/5/10, MRR and retrieval timing.
- Exclude RepoBench-R `next_line` from retrieval queries; keep benchmark data external.

## 3.2.1-alpha.15-ann-compare

- Added `contextbench-compare` for paired exact/ANN runs with shared task slice and budgets.
- Report task alignment, file-set Jaccard, changed selections, retrieval time and scored-vector totals.
- Optionally invoke the independent ContextBench evaluator for both prediction files.
- Keep retrieval overlap explicitly separate from gold-label recall and precision.

## 3.2.1-alpha.14-context-cache

- Added a bounded persistent per-project cache for compiled Context Packets.
- Reuse packets only when task, project, World snapshot, stable memory state and budget match.
- Verify source and memory freshness before serving cached context; safely fall back on cache errors.
- Expose packet cache hit/miss, elapsed time and storage status in retrieval traces.
- Measured about 4.8x faster repeated packet assembly on a local 152-file project; this is not an end-to-end agent result.

## 3.2.1-alpha.13-adaptive-ann

- Added adaptive semantic candidate sizing by repository size, task breadth and retrieval pool.
- Added explicit `auto`, `exact` and `ann` semantic modes to Attention and ContextBench commands.
- Added semantic timing, candidate limit and scored-vector telemetry to retrieval traces.
- Added aggregate semantic work statistics to ContextBench export summaries without reading gold labels.

## 3.2.1-alpha.12-ann

- Added persistent inverted vector-coordinate buckets for approximate neighbor candidates.
- Exact-rerank only a bounded semantic candidate set on large repositories.
- Preserve exhaustive semantic scoring for small indexes and candidate sets covering the index.
- Update and remove ANN postings transactionally with their document vectors.
- Expose semantic search mode and scored/total vector counts in Attention traces.

## 3.2.1-alpha.11-mcp

- Added `pyproject.toml` with a `galaxy` console entry point and MCP v2 dependency.
- Added `galaxy mcp-server`, exposing only `get_context` over stdio.
- Kept MCP as a thin adapter over Context Compiler with fresh World Model sync.
- Added writable `GALAXY_HOME` state isolation for the installed MCP server.
- Documented the remaining Galaxy Kernel 4.0 roadmap and current client setup.

## 3.2.1-alpha.10-kernel-watch

- Added `kernel-watch` as a foreground polling loop with configurable interval and debounce.
- Run the projector on startup and after a quiet period following repository changes.
- Ignore Galaxy runtime files, retry transient projection failures, and stop cleanly on Ctrl-C.

## 3.2.1-alpha.9-kernel-projector

- Added an idempotent World Event consumer that updates the Attention index and affected component maps.
- Persisted a consumer cursor only after both derived views and source freshness checks succeed.
- Added `kernel-observe` for explicit preparation before agent requests.
- Optimized capsule edge grouping for bulk precomputation.

## 3.2.1-alpha.8-world-events

- Added ordered, cursor-readable World Model baseline and change events.
- Persisted a pending event before updating the snapshot so interrupted appends can be recovered once.
- Scoped World Model runtime files by repository path and project identity.
- Added `world-events` CLI and isolation/recovery tests.

## 3.2.1-alpha.7-snapshots

- Added stable World Snapshot IDs to context packets and retrieval traces.
- Verified snapshot freshness before and after context compilation, with one retry on concurrent source changes.
- Reject uncached source bytes that disagree with the snapshot content hash.
- Pruned ignored directories during repository traversal to reduce freshness-check overhead.

## 3.2.1-alpha.6-capsules

- Added persistent, dependency-fingerprinted structural maps for selected project areas.
- Added fast `context-overview` for a small first context layer.
- Context Compiler includes cached maps only in spare budget after task evidence.

## 3.2.1-alpha.5-world-delta

- Added a persistent file metadata manifest for incremental World Model sync.
- Reparse modified files and their outgoing graph edges; preserve unchanged nodes.
- Fall back to a full scan for additions, removals, missing manifests and broad changes.
- Added `world-sync --full` and sync mode reporting for explicit verification.

## 3.2.1-alpha.4-selective-context

- Split indexed source text from semantic vectors; score vectors without reading all documents.
- Load document text only for ranked evidence candidates.
- Preserve exact semantic similarity and BM25 scores through the migration.

## 3.2.1-alpha.3-kernel-index

- Added a persistent Attention Engine index keyed by world snapshot content hashes.
- Added incremental updates and deletions, with automatic migration of the earlier derived cache.
- Added SQLite BM25 posting lists while retaining the existing scoring formula.
- Kept the independent ContextBench adapter and its command unchanged.

## 3.2.1-alpha.2-contextbench

- Replaced internal Git-history task collection and scoring with an adapter for the independent ContextBench dataset.
- Export file-level predictions from Galaxy Attention Engine at each external issue’s base commit.
- Delegate grading to the upstream ContextBench evaluator; gold labels are not read during retrieval.
- Added CLI, documentation and adapter integration tests.

## 3.2.1-alpha.1-benchmark

- Added `galaxy_core/benchmark/` historical Git retrieval benchmark framework.
- Added `benchmark-collect` to build leakage-resistant datasets from non-merge Git commits.
- Parent commit is used as the pre-solution snapshot; created files are excluded from retrievable gold.
- Added diff-hunk evidence ranges for evidence-selection evaluation.
- Added `benchmark-run` comparing lexical-only, semantic-only, full Attention Engine and full-context coverage/cost.
- Added Recall@5/10/20, Precision@10, first relevant rank, fixed-budget recall, evidence hit rate and token-cost metrics.
- Git snapshots are materialized through `git archive` into temporary directories; the working tree is never checked out or modified.
- Added JSON and Markdown benchmark reports plus 3.2.1 regression tests.

## 3.2.0-alpha.1-attention-engine

- Added `galaxy_core/attention/` hybrid Attention Engine.
- Added BM25/content, lexical path/symbol and portable semantic retrieval signals.
- Added dependency-graph and Future-Graph candidate expansion.
- Added confidence-aware include/consider/drop gatekeeper with optional probabilistic-decision hook.
- Added evidence-window extraction so useful code can be selected from the middle of long files.
- Added adaptive low/medium/high context budgeting plus caller-supplied hard budgets.
- Added bounded full-context strategy for genuinely small repositories.
- Context Compiler now consumes Attention Engine output and exports retrieval trace/quality signals.
- Autonomous DAG project context now uses adaptive context budgeting.
- Added `attention-build` CLI command and 3.2 regression tests.

## 3.1.0-alpha.1-living-graph

- Added structural World Drift detection for file, symbol, dependency-edge and component changes.
- `world-sync` now reports the drift consumed by the new snapshot; added read-only `world-drift`.
- Added first Future Graph counterfactual impact simulator with reverse-dependency propagation, affected tests/docs/components, risk and uncertainty.
- Added `future-impact` CLI command.
- Added evidence-driven Memory Reconciler for missing/changed sources and competing same-slot assertions.
- Added `brain-reconcile` dry-run command with explicit `--apply` for high-confidence lifecycle updates.
- Context Compiler now includes Future Graph impact and knowledge-health findings in each task packet.
- Added 3.1 regression tests while preserving the headless Context OS boundary.

## 3.0.0-alpha.1-context-os

- Added persistent Project World Model with file/symbol/dependency scanning.
- Added task-specific Context Compiler with bounded context packets.
- Added living-memory lifecycle states: stale and contradicted.
- Added confidence-gated Decision Fabric above the Jev/System-One layer.
- Added project_context injection into autonomous DAG workers.
- Added CLI commands: world-sync, world-status, world-related, context-build, decision-classify, brain-state.
- Added v3 regression tests for graph extraction, context compilation, memory lifecycle and decision escalation.
- Kept the core headless: no TUI, web server or SaaS dashboard.

## 2.1.0-alpha.3-core

- Removed Orbit web/SaaS application and its tests.
- Removed TUI/legacy UI remnants and the `solar.py` compatibility layer.
- Removed Obsidian dashboard generation and bundled plugin/MCP UI integration.
- Removed generated runtime history, benchmark outputs, caches, embedded Git metadata and local `.env`.
- Preserved the Vault, knowledge graph, Brain, agent runtime, decision layer, DAG engine, approvals, provider adapters and core tests.
- Kept existing human-authored Markdown vault content while deleting per-run execution traces.
- Reframed the repository as a UI-agnostic Galaxy Core while the product direction is being reconsidered.
# Product direction update — Second Brain

- Reframed Galaxy as a local-first second brain centered on durable memory, the Markdown/Obsidian vault, goals, and reusable experience.
- Replaced the context-engine-focused README and 4.0 roadmap with memory lifecycle, vault usability, provenance, correction, and safe-sharing priorities.
- Kept repository-context components available for compatibility with existing alpha workflows; they are no longer the active product direction or success criteria.
- Updated package metadata to `galaxy-second-brain` and version `3.2.1a19`.
