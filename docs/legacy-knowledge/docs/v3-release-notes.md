# Galaxy 3.0.0-alpha.1 — Context OS

This release changes the center of gravity of Galaxy.

The previous core primarily coordinated memory, roles, DAGs and model execution. Version 3.0 alpha 1 introduces a project-intelligence layer before execution:

1. **Project World Model** keeps a persistent structural snapshot of a repository.
2. **Context Compiler** builds a bounded task-specific packet from files, dependency edges, living memory and hierarchical project instructions.
3. **Living Memory** can be marked stale or contradicted without deleting history; inactive knowledge is excluded from normal context.
4. **Decision Fabric** wraps Jev/System-One style narrow decisions with explicit confidence escalation.
5. **Autonomous workers** receive compiled `project_context` separately from managed memory.

## What is intentionally not in this build

- TUI
- Orbit/web server
- SaaS dashboard
- Obsidian REST integration
- automatic contradiction detection
- full semantic/runtime architecture graph
- Future Graph / counterfactual simulation

The last three are product research targets, not silently claimed as complete features.

## Verification

- 90 Python regression tests executed
- 0 failures
- 0 errors
- 2 expected skips (live LLM unavailable; generated run history absent in clean build)
- JavaScript calculator fixture passed
