# Galaxy source recovery audit — 2026-09-30

## Evidence and scope

GitHub `Lunat1t/GalaxyOS` was inspected at main commit
`1759d9720d568585d31e3736173cfb973b43d6cc` (2026-09-26).
At inspection time the repository had one branch, `main`, and no pull requests.
Its runtime version was `3.2.1-alpha.2-contextbench`.

The attached `galaxy-3.2.1-alpha.1-benchmark.zip` predates that commit.
105 files match GitHub exactly. Its extra retrieval runner, collector, models
and test are from the previous benchmark; they are not ExperienceBench.
Importing this archive over main would regress the ContextBench integration.

The later saved `galaxy-3.2.1-alpha.18-verification (1).zip` was recovered.
It contains 162 files: 95 match main, 41 are absent from main and 26 differ.
This recovery imports 65 source/config/test/document changes, excluding all
README files. The existing Codex bridge, repository instructions, legacy
entry points, Obsidian files and existing vault contents remain in the remote
base tree. No private vault files from archives or generated runtime state
are imported. The current GitHub ignore rules and changelog are retained,
with alpha.18 exclusions and release history added.

## Recovered work

| Area | Sources |
| --- | --- |
| Attention index, exact/ANN selection | `galaxy_core/attention/index.py`, `engine.py` |
| Context capsules and packet cache | `galaxy_core/context/capsules.py`, `compiler.py` |
| Incremental World Model, snapshots, events | `galaxy_core/world/` |
| Kernel projector and watcher | `galaxy_core/kernel/` |
| Extended ContextBench and RepoBench-R | `galaxy_core/benchmark/contextbench.py`, `repobench_r.py` |
| Project-scoped experience and offline continuity evaluation | `galaxy_core/context/experience.py`, `galaxy_core/benchmark/continuity.py` |
| Verification ledger, rule replay and promotion | `galaxy_core/engine/verification.py`, `autonomy_pkg/` |
| MCP context adapter and packaging resources | `galaxy_core/mcp_server.py`, `pyproject.toml`, `galaxy_core/resources/` |

## Still missing: latest ExperienceBench work

Neither inspected archive nor the GitHub tree contains the reported
ExperienceBench v0.1 runner or its 12-scenario candidate dataset.
In particular, these reported paths are absent:

- `examples/experience-bench/experiencebench-v0.1-candidate.json`
- `docs/experience-bench-v0.1.md`

The reported baseline/raw/compiled runner, repeat isolation, temporal
filtering/retirement, semantic/provenance judge, pair effects, funnel,
opportunity labels and calibration changes cannot be recovered from a text
report. Alpha.18 experience continuity is an earlier feature and must not be
presented as the new ExperienceBench implementation.

The reported directories `/tmp/opencode/experiencebench-calibration-v01b/`
and `/tmp/opencode/experiencebench-full-calibration-2026-09-30/` are on the
agent's original machine/session; they are not available in this workspace.
The 108-run completion status, telemetry and judge audit are unverified here.
No benchmark dataset, rubric, prompt, evaluator or result has been rebuilt or
changed in this recovery.

## Validation and limits

54 targeted tests passed, covering the recovered context/index/cache,
capsules, snapshots/events, kernel, continuity, verification, benchmark
adapters, MCP context function and existing Codex bridge. The calculator
fixture passed 7 assertions. Whitespace checks passed.

Full discovery was attempted but did not complete cleanly:

- The unchanged legacy chat test requires `tasks/task-002.md`, which is
  absent in the remote source tree. Its failure was reproduced separately.
- The unchanged provider test cannot bind a localhost HTTP socket in this
  sandbox (`PermissionError`).
- Discovery stalled in the legacy artifact-successor test; a broader core
  run also stalled in the existing corrupt-provider test. Both unfinished
  test processes were stopped; their root causes remain unresolved.

Do not describe the full suite as passing. Live LLM calibration, installed
MCP server startup and packaging installation were not validated.
The archived package version is `3.2.1a16` while the restored runtime reports
alpha.18; this existing archive inconsistency is retained for review.

## Finish recovery from the original working copy

On the machine where the agent implemented ExperienceBench, inspect the
actual working copy before pulling or replacing files:

```bash
git status --short
git diff --stat
git log --oneline --all -12
git remote -v
git ls-files --others --exclude-standard
```

Preserve a copy of the full working source and the calibration artifact
directories, including untracked dataset/fixture files. Commit the actual
changed source, tests, protocol and non-secret fixtures on a recovery branch,
then push that branch. Review ignored runtime files before adding anything;
do not include credentials or private memory stores. If local source is
unavailable, export its current project archive and calibration artifacts
from the original agent session. The alpha.18 recovery is partial until those
newer sources are obtained and compared.
