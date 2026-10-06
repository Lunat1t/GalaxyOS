# Galaxy Context OS — 3.2.1 Alpha 17

Galaxy is a **local-first context operating system for AI software development**.

Galaxy does not try to replace Codex, Claude, GPT or future coding models. Its job is to maintain project state and decide **what a model actually needs to see for the current task**.

Version: **3.2.1-alpha.19-evidence-integrity**

## Alpha 19: evidence integrity fixes

`require_evidence` now checks Galaxy's own ledger of passing verification
commands and verifies the saved log bytes against their recorded hashes.
Agent-supplied evidence strings alone cannot satisfy the rule. Completed runs
are audited again when reopened: a missing or altered verification log revokes
`DONE`. A rule activated between run creation and execution is checked before
execution. Learning marks an episode as verified only when its command logs
still pass the ledger check. See `docs/evidence-integrity-alpha.19.md`.

## Alpha 18: evidence and verification contracts

An execution plan can set `task_type` to `bugfix`, `feature`, or `general`.
Each agent receives a `verification_contract` containing its acceptance criteria,
deterministic commands, and active project rules. Verification command results
are recorded with exit code, log path and output hash. A passing command adds
its summary log to the node's evidence; a failed command adds its failure log
to the episode. A `PASS` claim cannot override a failing command.

Repeated evidenced failures can produce a **candidate** rule with explicit
`task_type`, check type and cited episode IDs. `rule-eval` replays labeled
examples; `rule-promote` requires an error-free replay and a named reviewer.
Active `require_command` rules reject plans lacking a downstream verifier with
the required command fragment. Active `require_evidence` rules prevent a QA
node with no recorded evidence from succeeding. `rule-disable` reverses a rule.

This is a bounded first implementation. It does not infer safe executable
checks from prose, authenticate CLI reviewers, generate CI configurations, or
prove that agents make fewer repeated mistakes. See `docs/verification-alpha.18.md`.

## Alpha 17: experience continuity

Completed DAG nodes are recorded as sourced episodes. A later temporary agent
can receive relevant episodes from another role or model through `ContextCompiler`.
The packet reserves at most 10% of its effective budget (capped at 900 tokens)
for experience and includes outcome, source run/node, confidence and evidence.
New episodes or retractions invalidate the packet cache. No model is required
for storage or retrieval.

```bash
python galaxy.py experience-record "fix token rotation" "auth tests passed" \
  --project demo --role Earth --agent-id earth-1 --run-id run-1 --node-id auth \
  --outcome success --lesson "Test token rotation before deployment" \
  --evidence "pytest tests/test_auth.py passed"
python galaxy.py experience-search "verify token rotation" --project demo --role Moon
python galaxy.py context-build "verify token rotation" --project demo --role Moon
```

Use the same `--root /path/to/repository` for all three commands when the
repository is elsewhere. `experience-retract EXP-...` removes a wrong episode
from future context. Private and team scope predicates are available for
trusted local callers, but CLI identity arguments are not authentication;
do not expose them as an untrusted multi-user service.

Two independent, evidence-backed successful runs with the same lesson produce
an inspectable **candidate** procedure. A conflicting failure prevents that
candidate. No automatic promotion into confirmed project knowledge occurs.

`experience-eval ordered-tasks.json --repo /path/to/repository` replays
annotated connected tasks in isolation and reports prior-episode retrieval,
packet token estimates and assembly time against a no-experience baseline.
This measures retrieval only. For the intended Codex vs Codex + Galaxy claim,
run both agents on a held-out series of tasks and measure verified task success,
total model input/output tokens, wall time and repeated mistakes. See
`docs/continuity-alpha.17.md` for the schema and protocol.

> Headless build: no TUI, Orbit/web server, SaaS dashboard or Obsidian UI integration.

## Kernel work, part 14: graph expansion experiment and RepoBench-R

The World Model now records conservative AST class-inheritance edges when a
base class resolves uniquely inside the repository. Existing retrieval remains
the default. An opt-in one-hop mode starts from the current top 15 files and
adds their directly imported modules and parent-class files; final output stays
at 15 files and the experiment holds the total context budget at 20,000 tokens.
Run the paired ablation on the three fixed Django ContextBench tasks:

```bash
galaxy contextbench-graph-compare data/contextbench_verified.parquet \
  --repo /path/to/django --evaluate \
  --out-dir benchmarks/django-one-hop-graph
```

The runner writes baseline and one-hop predictions, selection deltas, token
estimates and, with `--evaluate`, separate upstream score files. It does not
label additional selected files as recovered gold until the evaluator scores
them.

Galaxy also has a RepoBench-R adapter for snippet-ranking evaluation. Export a
RepoBench-R split to JSONL with `repo_name`, `file_path`, `context` (candidate
snippets), `import_statement`, `code`, and `gold_snippet_index`, then run:

```bash
galaxy repobench-r-run data/repobench-r-python-cff-test-easy.jsonl \
  --semantic-mode exact --out-dir benchmarks/repobench-r-python-cff-test-easy
```

It reports Accuracy@1/5/10, MRR, and build/semantic latency. The `next_line`
field is never used as a query. This benchmark isolates snippet retrieval from
completion and complements ContextBench's file-level issue retrieval. The
RepoBench-R data is not bundled; obtain the official split and honor its
published license.

## Kernel work, part 13: paired ANN validation

`contextbench-compare` runs exact and ANN retrieval over the same task slice,
with identical token and file limits. It checks task ID alignment and reports
file-set Jaccard, changed selections, semantic time and vector work. This makes
the two runs directly comparable without implying that file overlap is recall.
Recall and precision are still calculated only by the independent ContextBench
evaluator. Install its runtime and `pyarrow`, then use the verified split and
local repository mirrors:

```bash
galaxy contextbench-compare data/contextbench_verified.parquet \
  --repos-dir /path/to/repos --limit 50 --evaluate \
  --out-dir benchmarks/contextbench-ann-verified-50
```

The report and exact/ANN predictions are written to the output directory;
`--evaluate` also writes the upstream scores for each mode. Full external
quality measurements are pending until the official dataset, repository
snapshots and evaluator are available together.

## Kernel work, part 12: persistent Context Packet cache

Galaxy now stores compiled Context Packets in a bounded, per-project SQLite
cache. Repeated identical requests can skip Future Graph analysis, memory
reconciliation and retrieval compilation. A cache key includes the task,
repository/project identity, World snapshot, stable memory signature, token
budget and file limit. Galaxy checks repository and memory freshness before
returning a hit; edits or changed memories trigger a new compilation. Each
project cache keeps at most 128 entries, and retrieval traces expose hit/miss,
elapsed time and whether the result was stored.

On a local 152-file project, seven repeated-request pairs measured a median
100.7 ms for a miss and 20.8 ms for a hit (about 4.8x faster). This measures
Context Packet assembly locally; it is not a Codex end-to-end result. The cache
contains project context excerpts and is stored in Galaxy's local runtime data.


## Kernel work, part 11: adaptive ANN and independent comparison

Semantic candidate size now adapts to repository and task size. `auto` keeps exact
scoring through 512 vectors, then grows the ANN pool sublinearly with the index;
long, broad tasks receive 35% more candidates. The pool is capped at 8,192 vectors
to keep query cost bounded. The requested/effective mode, candidate limit, elapsed
semantic time and scored/total vector counts are recorded in the retrieval trace.

Inspect or force a mode directly:

```bash
galaxy attention-build "fix authentication" --root /path/to/repo --semantic-mode auto
galaxy attention-build "fix authentication" --root /path/to/repo --semantic-mode exact
galaxy attention-build "fix authentication" --root /path/to/repo --semantic-mode ann
```

ContextBench accepts the same switch and reports aggregate semantic work without
reading gold labels. Generate two prediction files, then score both with the same
upstream evaluator:

```bash
galaxy contextbench-run data/contextbench_verified.parquet --repos-dir /path/to/repos \
  --semantic-mode exact --out benchmarks/contextbench-exact.jsonl
galaxy contextbench-run data/contextbench_verified.parquet --repos-dir /path/to/repos \
  --semantic-mode ann --out benchmarks/contextbench-ann.jsonl
```

This makes ANN quality loss measurable independently. Galaxy does not claim a
ContextBench result until those prediction files are evaluated by the upstream tool.

## Kernel work, part 10: bounded approximate semantic retrieval

Large Attention indexes no longer fetch and compare every stored vector for each
query. Each document stores postings for its strongest signed vector coordinates.
A query uses those postings to select a bounded approximate-neighbor pool, then
Galaxy computes exact cosine scores only inside that pool. This keeps final scores
auditable while reducing vector reads. The buckets update and delete alongside the
document index; existing derived indexes rebuild once because the schema version changed.

Repositories with at most 512 indexed documents retain exhaustive vector scoring.
The normal candidate pool is at least 1,024 vectors and grows with the requested
Attention pool. Retrieval traces report `mode`, total vectors and vectors actually
scored. If the requested candidate pool covers the index, Galaxy also uses exact mode.

A local synthetic test with 10,000 documents and eight queries measured a median
23.444 ms for bounded retrieval versus 121.996 ms for exhaustive scoring (5.2x),
with mean Recall@20 0.956 and minimum Recall@20 0.85. The one-time index build took
5.37 seconds. These are development measurements on generated documents, not
ContextBench or end-to-end agent results; independent quality validation remains required.

## Kernel work, part 9: pip package and MCP adapter

Install the project from its checkout:

```bash
python -m pip install -e .
galaxy --version
```

The package exposes `galaxy` on `PATH`. The new `galaxy mcp-server` command runs
an MCP stdio server with one read-only tool: `get_context`. It accepts a task and
local repository path, refreshes the World Model, and returns a bounded Context
Packet plus ranked file names. It calls the existing Context Compiler, so MCP
does not introduce a second retrieval path. Runtime state is written outside the
installed package to `GALAXY_HOME`, `%LOCALAPPDATA%/Galaxy` on Windows, or the
platform user data directory.

Codex configuration (`~/.codex/config.toml`):

```toml
[mcp_servers.galaxy]
command = "galaxy"
args = ["mcp-server"]
```

Or register it with:

```bash
codex mcp add galaxy -- galaxy mcp-server
```

Antigravity configuration (`~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "galaxy": {
      "command": "galaxy",
      "args": ["mcp-server"]
    }
  }
}
```

The first call scans the repository and builds derived state. Later calls reuse
the persistent index; running `kernel-watch` alongside the MCP server prepares
changes before the agent asks. The adapter currently exposes one tool on purpose,
so real usage can be measured before widening the protocol surface.

The tracked path to 4.0 is in [`docs/v4-roadmap.md`](docs/v4-roadmap.md).

## Kernel work, part 8: polling watcher

Run the watcher in a foreground terminal while editing a repository:

```bash
python galaxy.py kernel-watch --root /path/to/repo --project default
```

It runs an initial `kernel-observe`, then checks source-file metadata once per
second. After changes have been quiet for 500 ms, it syncs the World Model and
prepares the retrieval index and affected component maps. This batches nearby
writes and moves preparation ahead of the next context request. Each completed
projection prints one JSON line; errors go to stderr and the watcher retries on
the next poll. Stop it with Ctrl-C. Use `--interval-ms` and `--debounce-ms` to
adjust the two timings.

This is a polling process, so it needs a terminal to remain running and has
polling latency and per-poll directory traversal cost. Run one watcher per
repository/project; concurrent writers are not coordinated. As with routine
World Model sync, a modification that preserves size, modification time,
change time and file identity requires `world-sync --full` to verify contents.

## Kernel work, part 7: event consumer for indexes and maps

Run `kernel-observe` after repository changes, such as from a local file-change
hook or before starting an agent session:

```bash
python galaxy.py kernel-observe --root /path/to/repo --project default
```

It syncs the World Model, reads its change feed, incrementally prepares the
Attention index and rebuilds affected component maps. If multiple changes
accumulate, it coalesces them into the latest snapshot. Its cursor advances
only after both caches are ready and the source is still current. An interrupted
run replays outstanding events; derived caches can be rebuilt if deleted.

This moves preparation before the next agent request. The total amount of work
may increase slightly because of observation and durable bookkeeping. Direct context requests still
work without running this command, preparing missing entries on demand.

## Kernel work, part 6: durable World Model change feed

World Model sync now records a `WORLD_BASELINE` event for a new project and a
`WORLD_CHANGED` event when source files or graph edges change. Each event has
before/after snapshot IDs, changed paths and edge changes. No-op syncs create
no event. Consumers can resume after the last numeric cursor:

```bash
python galaxy.py world-events --root /path/to/repo --project default --after 0 --limit 50
```

The event log is stored in SQLite under `data/runtime/world/`. An on-disk pending
record lets the next access finish an interrupted append after the snapshot was
saved, without duplicating the event. World snapshot and event files are scoped
to both the repository path and project name, allowing repositories with the
same project label to coexist. Older unscoped World Model snapshots rebuild on
first access after upgrading; the old files are left untouched.

This is a change feed for future background index and capsule consumers. The
World Model snapshot remains authoritative; the feed alone is not an event-
sourced state engine or a filesystem watcher. Concurrent writes to the same
project still require one external writer/coordinator.

## Kernel work, part 5: consistent snapshot context

Context Compiler now checks the World Model manifest before and after building a
packet, updates changed files automatically, and includes a stable
`world_snapshot_id` in the packet. The ID depends on file and graph state rather
than scan time, so it can identify which project state an agent used. A source
file that changes during index preparation fails its content-hash check; Galaxy
retries the compile once against a refreshed snapshot. Repeated concurrent
changes produce an explicit error instead of a mixed context packet.

The fast check uses file size, modification time, change time and file identity.
Run `world-sync --full` for complete content verification if external tools can
preserve all four attributes while modifying bytes. Checking freshness twice
adds some latency; directory traversal now prunes excluded folders early.

## Kernel work, part 4: cached component maps

Galaxy now builds compact, deterministic maps of project areas such as
`galaxy_core/attention` and `galaxy_core/world` from the World Model snapshot.
Each map names observed files, symbols and a few relationships. A fingerprint
of its source nodes and edges lets unchanged maps survive other project changes.
The maps are structural evidence, not generated claims about how code behaves.

For a quick first look, use:

```bash
python galaxy.py context-overview "fix world model delta" --root /path/to/repo
```

This returns up to two maps without running full Attention retrieval, memory
reconciliation or Future Graph. `context-build` adds maps only after preserving
the selected task evidence and only if the token budget has room. The cache is
derived state under `data/runtime/capsules/`; source changes take effect after
a World Model sync (`--refresh` or `world-sync`).

## Kernel work, part 3: incremental World Model sync

`world-sync` now compares file metadata with the last snapshot and reparses only
modified existing files, including their outgoing import/link edges. An unchanged
repository needs no source content reads. When paths are added or removed, or too
many files change, it falls back to a full scan so imports from untouched files
are resolved correctly. The snapshot and its metadata manifest live together
under `data/runtime/world/`; an absent or mismatched manifest triggers a full
scan. `world-status` shows the last sync mode and changed-file count.

Use `python galaxy.py world-sync --root /path/to/repo --full` to force content
verification of every scannable file. Routine sync compares size, modification
time, change time and file identity; filesystem changes that deliberately preserve
all of those attributes require a forced full scan. This is polling on sync,
and the foreground `kernel-watch` command polls for changes automatically.

## Kernel work, parts 1–2: persistent index and selective context loading

The Attention Engine persists bounded source text, portable semantic vectors and
BM25 term postings under `data/runtime/attention/`. A world snapshot refresh
rebuilds changed entries, removes deleted entries and reuses the rest. SQLite
postings let BM25 score only documents containing query terms. Source text and
vectors are stored separately, so only ranked candidate documents are loaded
for evidence extraction. Earlier index schemas rebuild automatically; this is
a disposable derived cache.

The index removes repeated source reads, tokenization and document-vector encoding
on the warm query path. Large indexes use the bounded semantic candidate search
described above; small indexes retain exhaustive scoring. A world sync (`--refresh`
for context commands) or the foreground watcher observes changed source files. The
independent ContextBench adapter remains available below.

## Independent retrieval evaluation — ContextBench adapter

Galaxy now exports file-level retrieval predictions for the independent [ContextBench](https://github.com/EuniAI/ContextBench) dataset. The previous Git-history collector and its self-defined scoring have been removed. ContextBench provides human-annotated gold contexts and performs the scoring; Galaxy sees only the issue text and the repository at its `base_commit`.

1. Download the official `default` or `contextbench_verified` dataset as Parquet, or export records to JSONL. For Parquet input install `pyarrow` (`pip install pyarrow`).
2. Prepare local Git mirrors at `repos/OWNER/REPO` with the dataset base commits available. For a single-repository subset, use `--repo /path/to/repo` instead of `--repos-dir`.
3. Generate predictions:

```bash
python galaxy.py contextbench-run data/contextbench_verified.parquet \
  --repos-dir /path/to/repos \
  --limit 10 --token-budget 10000 --max-files 20 \
  --out benchmarks/contextbench-predictions.jsonl
```

To run upstream scoring in the same command, install the [official ContextBench package and dependencies](https://github.com/EuniAI/ContextBench) in the environment and append `--evaluate benchmarks/contextbench-scores.jsonl`. The equivalent standalone command is:

```bash
python -m contextbench.evaluate \
  --gold data/contextbench_verified.parquet \
  --pred benchmarks/contextbench-predictions.jsonl \
  --out benchmarks/contextbench-scores.jsonl
```

This adapter evaluates **selected files**. Galaxy's evidence excerpts do not preserve exact source line offsets, so it does not claim symbol, span, edit-location, agent trajectory, or issue-resolution scores. The upstream evaluator reports file coverage, precision and F1; do not compare missing span metrics to full agent runs. The local Git archive is materialized in a temporary directory without changing your working branch. Failed instances appear in the command summary and produce a nonzero exit status; they are absent from the prediction file. The official dataset and package are external dependencies and are not bundled in this archive.

## Core idea

A large context window is not the same thing as useful context. Galaxy 3.2 introduces an explicit Attention Engine between a repository and an executor.

```text
Repository
   │
   ├─ Project World Model
   ├─ Living Memory
   ├─ Future Graph
   └─ project instructions
   │
   ▼
ATTENTION ENGINE
   │
   ├─ BM25/content retrieval
   ├─ path + symbol lexical retrieval
   ├─ portable semantic similarity
   ├─ dependency-graph expansion
   ├─ Future-Graph expansion
   ├─ confidence gatekeeper
   ├─ evidence extraction
   └─ adaptive token budgeting
   │
   ▼
Bounded Context Packet
   │
   ▼
Codex / Claude / GPT / local model / worker
```

The goal is not "retrieve top K files". The goal is **wide recall first, then controlled attention**.

## What is new in 3.2 alpha 1

### 1. Hybrid Attention Engine — `galaxy_core/attention/`

The old single lexical file ranker has been replaced by a staged retrieval pipeline.

Signals currently include:

- BM25 over bounded repository content;
- path/symbol/summary lexical relevance;
- dependency-free portable semantic embeddings;
- dependency graph proximity;
- Future Graph impact confidence;
- task-sensitive test/documentation boosts.

Inspect retrieval directly:

```bash
python galaxy.py attention-build \
  "change refresh token rotation" \
  --root /path/to/repo \
  --project demo
```

The result explains why candidates were selected, exposes individual retrieval scores, gate decisions, evidence and token cost.

### 2. Evidence-first context

Galaxy no longer takes only the beginning of every selected file.

For each candidate it extracts query-bearing line windows around relevant symbols/content. This matters when the useful code is in the middle of a long module.

Selected file entries now contain:

```text
role
retrieval scores
selection reason
gate + confidence
evidence windows
bounded excerpt
```

### 3. Adaptive context budgets

When `--budget-tokens 0` is used, Galaxy classifies context need conservatively:

- `low` — small localized edits;
- `medium` — normal engineering work;
- `high` — architecture/refactor/migration/security/large-repository work.

The budget is split by role instead of allowing source files to consume everything:

```text
files          56%
memory         13%
graph/impact   12%
instructions   10%
reserve         9%
```

An explicit positive `--budget-tokens` remains a hard caller-supplied budget.

### 4. Small-project full-context strategy

Retrieval is not dogma. If a project is genuinely small enough to fit safely inside the bounded file budget, Galaxy can use:

```text
strategy = small-project-full-context
```

Large projects use:

```text
strategy = hybrid-attention
```

This avoids throwing away useful information just because RAG exists.

### 5. Confidence-aware gatekeeper

Candidates become:

- `include`;
- `consider`;
- `drop`.

The default gate is deterministic and auditable. The gatekeeper exposes an optional hook for a bounded probabilistic decision layer, but uncertain model decisions are not allowed to silently redefine Galaxy policy.

### 6. Retrieval quality signals

Every attention run exposes heuristic signals:

- `coverage_signal`;
- `noise_signal`;
- `graph_coverage_signal`;
- `token_utilization`;
- `selection_confidence`.

These are **diagnostics, not measured answer-accuracy claims**.

### 7. Context Compiler now uses Attention Engine

```bash
python galaxy.py context-build \
  "refactor authentication architecture" \
  --root /path/to/repo \
  --project demo \
  --budget-tokens 0
```

The final packet combines:

```text
Task
├─ selected evidence-bearing files
├─ dependency relationships
├─ Living Memory
├─ hierarchical AGENTS/CLAUDE instructions
├─ Future Graph impact
├─ knowledge-health findings
└─ Attention Engine trace + uncertainty
```

Autonomous DAG workers now request adaptive 3.2 context rather than a fixed 4.5K-token packet.

## 3.1 capabilities retained

### Living Graph / World Drift

```bash
python galaxy.py world-sync --root /path/to/repo --project demo
python galaxy.py world-drift --root /path/to/repo --project demo
```

Galaxy persists repository files, symbols and supported dependency edges and can detect structural drift.

### Future Graph

```bash
python galaxy.py future-impact \
  "change authentication session model" \
  --root /path/to/repo --project demo --seed src/auth.py
```

The forecast is structural and explicitly uncertain. It is not runtime proof.

### Living Memory reconciliation

```bash
python galaxy.py brain-reconcile --root /path/to/repo --project demo
python galaxy.py brain-reconcile --root /path/to/repo --project demo --apply
```

Stale/contradicted knowledge remains auditable and is excluded from ordinary active retrieval.

## Existing core retained

- managed memory + embeddings;
- entity graph and goals;
- Markdown Vault filesystem;
- Sun discovery/interview;
- validated DAG execution;
- model routing and budgets;
- persistent roles + temporary bounded workers;
- human approvals;
- deterministic verification;
- provider adapters;
- Jev/System-One decision primitives and Mars guardrails.

## Quick start

```bash
python run_tests.py
python galaxy.py doctor
python galaxy.py world-sync --project galaxy
python galaxy.py attention-build "change context compiler" --project galaxy
python galaxy.py context-build "change context compiler" --project galaxy
```

## Product boundary

Galaxy 3.2 is still **not an IDE UI**. The innovation target is the intelligence layer between software and AI executors:

**World Model → Living Memory → Future Graph → Attention Engine → Context Compiler → Decision Fabric → Executor**.

See `docs/core-architecture.md` and `docs/v3.2-release-notes.md`.
