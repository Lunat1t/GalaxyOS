# Galaxy Second Brain — architecture

Galaxy is a local-first knowledge and memory system. Markdown is the user-readable knowledge layer; managed memory records add scope, lifecycle, evidence, and history; agents can consult that knowledge during supported planning and execution workflows.

## Main components

### 1. Managed Brain — `galaxy_core/brain/`

Stores memory items and their links, provenance, confidence, lifecycle state, and revision history. `BrainStore` provides capture, search, confirmation, correction, retraction, goal access, vault ingestion, and memory statistics. `MemoryReconciler` can identify stale or competing knowledge for review.

### 2. Knowledge Vault — `galaxy_core/vault/`

Uses Markdown files as durable, human-readable notes. The vault parser reads note metadata and Obsidian-style wikilinks. The vault engine indexes changes, supports full-text search, and exposes a note graph and backlinks. The SQLite index is derived data; Markdown remains the editable source.

### 3. Experience — `galaxy_core/brain/experience.py`

Records sourced episodes from agent work and makes relevant lessons searchable later. Outcomes and evidence are retained so an episode can be assessed instead of accepted as fact. Its product role is durable experience memory; storage paths and data formats are preserved.

### 4. Goals and planning — `galaxy_core/brain/` and `galaxy_core/engine/`

Long-term goals can be stored and surfaced during planning. The execution engine records runs, budgets, approvals, logs, and verification results. Alpha 19 verifies evidence against saved command logs and hashes.

### 5. User interface — CLI and Markdown

The command-line interface provides explicit operations for memory, goals, experience, vault, planning, and agent runs. Users can also edit vault notes directly in Obsidian or any Markdown editor.

## Memory lifecycle

```text
capture with source
      ↓
search and inspect
      ↓
confirm, correct, or retract
      ↓
reuse with provenance
```

Search relevance does not certify truth. Confirmed, inferred, stale, and retracted states remain distinguishable, and reviewable history supports correction.

## Data boundaries

State is local by default. The brain store and vault index are SQLite-backed; the vault's Markdown files remain ordinary files. Provider calls are optional for core storage/search, but configured model providers may receive prompt information during agent tasks. Private and team scopes exist in parts of the current experience system; CLI identity flags are not authentication and must not be exposed as a multi-user security boundary.

## Retired repository-context system

World Model, Attention Engine, Context Compiler, Kernel Projector/Watcher and MCP get_context have been removed. Agent workflows use managed memory and agent state without compiling repository packets. See [the decision and archived research](context-retirement.md).
