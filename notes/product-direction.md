# Galaxy product direction

## In one sentence

Galaxy is a local-first second brain that helps people and their AI agents save, find, connect, and maintain useful knowledge over time.

## Who it serves

Independent developers, students, and small teams who want a durable place for project knowledge, decisions, personal notes, goals, and lessons learned by their agents.

## The central problem

Useful knowledge is scattered across notes, decisions, task outcomes, and conversations. It is difficult to find later, easy to repeat mistakes, and risky to trust when its source or freshness is unclear.

## What Galaxy provides

- Durable memory records with project scope, tags, importance, status, links, and history.
- Search over managed memories and Markdown notes.
- An Obsidian-compatible vault with links, backlinks, and a knowledge graph.
- Goals and agent experience that can be revisited during later planning.
- User controls to confirm, correct, mark stale, or retract information.
- Local-first storage and optional agent/model integrations.

## Product principles

1. **Memory should be inspectable.** Show sources, status, and history.
2. **A suggestion is not a fact.** Keep inferred and agent-written information reviewable.
3. **The user owns correction.** Fixing or retracting knowledge must be straightforward and auditable.
4. **Files should stay portable.** Notes should remain readable without Galaxy.
5. **Privacy scopes must be real.** Shared and private knowledge must be enforced by the system before multi-user use.

## Focus boundary

The repository-context runtime has been removed. File ranking, coding-context packets, token-budget optimization, watchers, World Model and a separate Core/OS platform are outside the current scope. Historical research is preserved in [context-retirement.md](context-retirement.md) and its archive. Reopening this scope requires an explicit owner decision.
