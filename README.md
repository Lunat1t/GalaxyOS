# Galaxy v1

**Galaxy is a local-first second brain for people and their AI agents.** It stores useful knowledge, remembers decisions and experience, and lets the user inspect, correct, connect, or retract what it has learned.

Current source version: `1.1.6` (Galaxy v1)

Current functionality: **local second brain and reliable memory**

Planned direction: **Galaxy Code web app on Next.js**; the current CLI/TUI is transitional. See [architecture](notes/core-architecture.md) and [change reports](CHANGELOG.md).

## What Galaxy does

- **Managed memory:** save facts, decisions, references, and lessons with project scope, tags, importance, confirmation state, and history.
- **Memory search:** find relevant notes with lexical and semantic signals; inspect linked memories and their evidence.
- **Memory integrity:** confirm, correct, or retract a memory without silently erasing its history. Reconciliation can flag stale or competing project knowledge.
- **Knowledge vault:** use Markdown files as the human-readable source of truth. Galaxy indexes notes, searches them, and builds links and backlinks from Obsidian-style `[[wikilinks]]`.
- **Goals and experience:** keep long-term goals and record sourced agent episodes so useful lessons can be found again.
- **Agent workspace:** planning and agent runs can consult saved memories; execution has budgets, logs, verification evidence, and approval gates.

Galaxy is a memory layer and workspace around the user. It does not claim that a language model has human-like understanding, and it does not silently turn uncertain guesses into confirmed facts. The user remains able to review and change stored knowledge.

## Quick start

The current Galaxy v1 package is a CLI with a transitional terminal workspace. An initial Next.js web workspace now lives in `web/`; it can list projects and save queued tasks, but does not run agents yet. Install the current CLI once for your user account to use memory commands and the prototype workspace from any directory.

### Web workspace preview

The first Next.js workspace is under development. Start the local API from the repository root:

```bash
python3 galaxy.py serve
```

In another terminal, start the web client:

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:3000`. The web preview reads and saves projects and queued tasks through the local API; it does not run an agent yet. Next.js requires Node.js 20.9 or newer.

### Linux and macOS

```bash
python3 -m pip install --user .
galaxy
```

For development from a checkout, use `python3 -m pip install -e .` instead. If your Python installation does not support user installs, create a virtual environment and add its `bin` directory to `PATH`.

### Windows PowerShell

```powershell
py -m pip install --user .
galaxy
```

Windows installs the terminal support package automatically. If the command is not found after installation, add Python's user `Scripts` directory to `PATH`, or run `py -m galaxy` from the checkout.

To see command help, run `galaxy --help`.

Save, open, and list projects from any directory:

```bash
galaxy project add /path/to/project
galaxy project open /path/to/project
galaxy project list
galaxy project show
galaxy project settings
galaxy project settings --set '{"default_provider":"codex"}'
galaxy project task-add "Review the project setup"
galaxy project tasks
```

The project registry is stored in the user's application data directory. `GALAXY_HOME` can override that location. Run `galaxy` with no arguments to open the project workspace. Tasks are saved as queued requests; the agent runner is still being built. Project settings accept provider, agent/model IDs, Skill/MCP IDs, and `allow`/`ask`/`deny` permission choices; there is no field for credentials. These are saved preferences, not active permissions yet.

For the local API prototype used by the future web client, run:

```bash
galaxy serve
```

It listens only on `127.0.0.1:8765`. The service prints the location of a temporary server token for the future Next.js server; do not expose that token in browser code. Stop the service with `Ctrl+C`.

Add and search a memory:

```bash
python galaxy.py brain-add decision "Use SQLite for the local prototype" \
  "Keep the first version local and easy to back up." --project galaxy --tag architecture --confirm
python galaxy.py brain-search "local database choice" --project galaxy
```

Inspect or correct memory:

```bash
python galaxy.py brain-stats
python galaxy.py brain-show MEM-...
python galaxy.py brain-correct MEM-... "Updated understanding" --reason "Decision changed"
python galaxy.py brain-forget MEM-... --reason "No longer relevant"
```

Work with the Markdown vault:

```bash
python galaxy.py vault sync
python galaxy.py vault search "architecture"
python galaxy.py vault graph
python galaxy.py brain-refresh
```

The default vault directory is `data/vault`. Put Markdown notes there; use frontmatter and `[[note links]]` where helpful. The source notes remain ordinary files that can be opened and edited in Obsidian or any text editor.

Manage goals and learned experience:

```bash
python galaxy.py goal-add "Prepare for the AITU master's program" \
  "Build a steady study plan for English and computer science." --project study --horizon long
python galaxy.py goal-list --project study
python galaxy.py experience-search "verify token rotation" --project demo
```

## How memory is handled

1. **Capture:** the user, a Markdown note, or a completed agent run supplies a candidate memory with provenance.
2. **Retrieve:** search returns related items, with project and visibility boundaries applied.
3. **Review:** memories can be inspected with their source, links, and revision history.
4. **Maintain:** the user can confirm, correct, retract, or mark knowledge stale. Reconciliation surfaces possible conflicts for review.

Memory retrieval is not the same as truth. Galaxy preserves evidence and status so a retrieved item can be weighed instead of treated as automatically correct.

## Project boundaries

The active product is the **second brain: managed memory, a Markdown knowledge vault, goals, and reusable experience**. Repository analysis, coding-task context compilation, watchers, and the context MCP adapter have been removed. Work focuses on memory quality, vault usability, provenance, correction, and visibility boundaries. See [the retirement decision and preserved history](notes/context-retirement.md).

## Data and privacy

Galaxy is designed to keep its state on the local machine. Its database and runtime state are stored under the project data directory by default. Markdown vault notes remain readable outside Galaxy. If configured agent or model providers are used, prompts sent to them may include selected information; review provider settings and memory visibility before using private material.

Execution evidence retains the protections introduced in legacy alpha 19: a passing verification command is checked against Galaxy's saved logs and hashes, and reopening a completed run audits that evidence again. See `notes/evidence-integrity-alpha.19.md` for the implementation details.

## Documentation

- `notes/product-direction.md` — what Galaxy is and what is in scope
- `notes/core-architecture.md` — system architecture
- `notes/evidence-integrity-alpha.19.md` — verification evidence rules
- `notes/legacy-changelog.md` — historical changes, including legacy context-system work

- `notes/context-retirement.md` — decision to remove the context core and constraints for future work

Historical documents, decisions and previous test reports are kept in [`notes/`](notes/README.md). They describe earlier versions and do not define the current release number.
