# Galaxy 3.2.1 alpha 17 — continuity slice

## What executes today

1. The existing World Model and Attention Engine select repository evidence. Alpha 16's component capsules and packet cache remain in use.
2. Each completed DAG node with a result is stored once by `(run_id, node_id)` in a repository-scoped SQLite experience ledger. It records task, role, model-independent agent identity, outcome, lesson candidate, confidence, evidence, source run and visibility. Failed completed DAGs also invoke the learning hook.
3. A new agent's `ContextCompiler` ranks visible episodes by task overlap and a small role/verification preference. It can reuse another role's experience. An independent experience budget is at most 10% of the packet budget, capped at 900 tokens. The packet still enforces its overall budget.
4. The cache key includes the source snapshot, stable project memory, a constant-cost SQLite experience revision and requesting identity/scope. Recording or retracting experience prevents reuse of stale packets.
5. `experience-search` shows candidate procedures after two *distinct*, evidence-backed verified success runs with the same explicit lesson. A failure with that lesson blocks a candidate. Candidates are inspectable and never silently written into Brain as confirmed facts.

## Relationship to the six research directions

| Direction | Implemented in this slice | Remaining research/work |
| --- | --- | --- |
| ACE, evolving context | Source snapshot, delta World Model and cache invalidation keep packets current; episodes append without rewriting a summary | Evidence-aware semantic consolidation and aging |
| ReasoningBank | Outcome plus lesson and evidence are stored; failures can block candidate procedures | Extract transferable strategies from full tool trajectories |
| G-Memory | Project/agent episodes link to run and node, with existing World/Brain graph context | Explicit insight/query/interaction graph and trajectory traversal |
| RCR-Router | Task/role-aware episode ranking with bounded token allocation | Learned allocator and measured utility feedback |
| Collaborative Memory | Project/private/team predicates, owner and team provenance fields | Authenticated principals, membership service, auditable grants and cross-user security tests |
| Agent KB | Experience is independent of model/executor and reused by later agents | Framework adapters, serialization contract and cross-framework benchmarks |

The labels refer to architectural inspiration. Alpha 17 does not reproduce the papers' algorithms or their published performance claims.

## Sequential offline experiment

Prepare a JSON file with ordered tasks. Each case's `gold_prior` refers only to observations from earlier cases. The evaluator rejects future references.

```json
{
  "project": "demo",
  "tasks": [
    {
      "id": "A", "task": "fix utf8 parser", "role": "Earth",
      "observation": {
        "run_id": "run-a", "node_id": "parser", "outcome": "success",
        "summary": "parser corrected", "lesson": "Test utf8 parser inputs",
        "evidence": ["test_parser passed"], "verified": true
      }
    },
    {
      "id": "B", "task": "test utf8 parser", "role": "Moon",
      "gold_prior": [["run-a", "parser"]]
    }
  ]
}
```

Run `python galaxy.py experience-eval tasks.json --repo /path/to/repo --out report.json`. `retrieval_recall` is an annotation-based episode recall; token counts are packet estimates; assembly times include local filesystem/cache effects. It does **not** establish faster or more successful agents.

## End-to-end A/B protocol

Use several repositories and a held-out series of related tasks, reset each branch to the same state, and alternate run order. Baseline: one agent without Galaxy continuity. Treatment: the same model/harness and task budget with alpha 17 continuity. Compare verified success per task, total model tokens, wall time, redundant file reads, repeated mistakes and transfer when agent/model changes. Preserve full logs and report failures and any private-memory leakage. Only after this experiment should improvement be claimed.

## Boundaries

- Lessons are supplied by the executor or caller; no automatic full-trajectory reflection is implemented.
- A `PASS` result alone is not verification. The DAG's verification commands must succeed and the result must carry evidence before an episode is marked verified.
- CLI `--owner-id` and `--team-id` are trusted-local scope selectors, not authentication. Do not serve the multi-user APIs without an identity and authorization layer.
- Only completed DAGs (`DONE` or `FAILED`) run the hook; interrupted or approval-blocked runs wait for resolution.
