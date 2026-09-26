# Galaxy 3.2 Context OS — Core Architecture

Galaxy Core is UI-agnostic and model-agnostic. Its job is to maintain project understanding, detect when that understanding drifts, forecast bounded structural consequences, and deliver trustworthy context to execution systems.

## Architectural layers

### 1. Project World Model — `galaxy_core/world/`

A persistent structural representation of the repository. It scans files, extracts symbols, resolves supported dependency edges and stores a snapshot in local runtime data.

Current alpha understands Python imports, relative JS/TS imports and Markdown wikilinks. The contract remains extensible for AST/LSP/runtime edges later.

### 2. Drift Detector — `galaxy_core/world/drift.py`

Compares the stored world snapshot with a fresh observation.

Evidence includes:

- file additions/removals/modifications;
- symbol additions/removals;
- dependency-edge additions/removals;
- components touched;
- bounded structural drift score.

A drift score is not a code-quality score.

### 3. Future Graph — `galaxy_core/world/future.py`

Counterfactual structural impact layer.

```text
hypothetical change
      ↓
seed files/symbols
      ↓
reverse dependency propagation
      ↓
dependent code / tests / docs / components
      ↓
structural risk + uncertainty
```

Future Graph alpha is deterministic and explainable. Dynamic runtime relationships are not yet inferred.

### 4. Living Memory — `galaxy_core/brain/`

Durable project knowledge with provenance, confidence, importance, confirmation, revisions, embeddings and entity links.

Lifecycle states:

`active → stale / contradicted → active` when revalidated, while `superseded` and `retracted` remain terminal historical states.

Stale and contradicted items remain auditable but are excluded from normal agent retrieval.

### 5. Memory Reconciliation — `galaxy_core/brain/reconcile.py`

Evidence-driven knowledge-health detection.

It currently supports deterministic source lifecycle checks and conservative same-slot assertion conflict detection. The default is read-only. Applying mutations requires explicit `--apply` and stronger confidence rules.

### 6. Attention Engine + Context Compiler — `galaxy_core/attention/`, `galaxy_core/context/`

3.2 separates **retrieval/attention** from final context assembly.

```text
wide candidates
   ↓
BM25 + lexical + portable semantic
   ↓
graph + Future Graph expansion
   ↓
confidence gatekeeper
   ↓
evidence extraction
   ↓
adaptive role budget
   ↓
Context Compiler
```

The Attention Engine produces auditable file candidates with per-signal scores, roles, evidence windows and estimated token cost. The compiler then combines selected files with living memory, hierarchical instructions, project relationships, Future Graph impact and knowledge-health findings.

For small repositories that genuinely fit inside the bounded budget, Galaxy may use a full-context strategy instead of retrieval.

Retrieval quality fields are heuristic diagnostics, not accuracy benchmarks.

### 7. Decision Fabric — `galaxy_core/engine/decisions/`

Narrow probabilistic decisions only. Noul/Choice/Score remain Jev-compatible.

```text
narrow question
    ↓
Jev / local System-One
    ↓
probability distribution
    ↓
Galaxy deterministic policy
  ├─ high confidence → auto
  └─ uncertain       → escalate
```

No decision model can redefine Galaxy safety policy.

### 8. Discovery, planning and execution

Sun turns ambiguous intent into a structured goal. The planner converts the goal into a validated DAG. The execution engine remains responsible for dependency validation, bounded concurrency, model routing, budgets, isolated write workspaces, approvals, deterministic verification, durable state and evidence.

Workers receive `project_context` plus managed memory.

## Design principles

1. **Attention before generation.** Retrieve widely, then compile bounded evidence instead of dumping the repository.
2. **State before prompts.** Project understanding lives in machine-readable state.
3. **Detect drift explicitly.** A cached world model must be able to admit that the repository changed.
4. **Forecast with uncertainty.** Future Graph exposes structural assumptions rather than pretending to know runtime truth.
5. **Provenance over memory dumps.** Knowledge has source, confidence and lifecycle.
6. **Detection is not authority.** A conflict detector may surface evidence; policy decides whether state changes.
7. **Cheap decisions before expensive reasoning.** Use narrow decision models only where confidence can be measured.
8. **Policy in code.** Safety, approvals and permissions are deterministic.
9. **Human control for irreversible work.** Critical actions escalate.
10. **Headless core.** UI can evolve independently.

## Current alpha limits

- World Model is structural, not yet a full semantic/runtime architecture graph.
- Drift is structural and does not infer product intent.
- Future Graph does not yet simulate runtime dataflow, infrastructure or external services.
- Memory contradiction detection is conservative and primarily same-slot/source-evidence based.
- Attention retrieval is hybrid but still bounded by current static dependency extraction and portable embeddings.
- BM25 is currently built in-memory per run; AST-block/dataflow evidence extraction is not implemented yet.
- No TUI, web app or SaaS layer.
