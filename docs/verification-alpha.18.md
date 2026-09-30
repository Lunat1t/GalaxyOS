# Galaxy alpha 18 — from experience to executable verification

## Runtime behavior

The plan carries an explicit `task_type` (`general`, `bugfix`, or `feature`).
Before a run starts, active project rules are checked against its DAG. A
`require_command` rule requires a downstream QA/verification node covering
every writer, with a verification command containing the rule's literal
fragment. The existing plan validator still requires deterministic verification
for any write plan. Rules add a more specific constraint.

The agent context includes its acceptance criteria, planned commands and
active rules. Each command executes in the isolated node workspace. Galaxy
stores exit code, status, relative log path and SHA-256 of the captured output
in the verification ledger. A failed command makes the node fail even if the
agent claimed `PASS`; its log becomes experience evidence. Successful command
logs become evidence on the QA result. An active `require_evidence` rule rejects
QA completion when no evidence is present.

## From repeated failure to rule

`rule-suggest` requires at least three different runs with the same explicit
lesson, project-visible failure outcome, and nonempty evidence. The caller
specifies a task type and a supported executable check. This is deliberately
conservative: Galaxy does not transform arbitrary failure prose into shell
commands or silently activate a rule.

```bash
python galaxy.py rule-suggest --project demo \
  --lesson 'Run regression verification' --task-type bugfix \
  --check require_command --match 'pytest' --minimum 3
```

The returned rule starts as `candidate`. To replay it, create a JSON list of
examples, each with a valid serialized `ExecutionPlan`, a reviewed
`should_block` boolean, and optionally `result_evidence` for an evidence rule:

```json
[
  {"plan": {"goal":"Fix A","project":"demo","task_type":"bugfix","nodes":[...]}, "should_block":true},
  {"plan": {"goal":"Fix B","project":"demo","task_type":"bugfix","nodes":[...]}, "should_block":false}
]
```

Run `python galaxy.py rule-eval VR-... cases.json`. A named reviewer can run
`python galaxy.py rule-promote VR-... --by reviewer` only when the replay has
both allowed and blocked examples and no classification errors. Run
`python galaxy.py rule-disable VR-...` to roll back. CLI names identify who
made a decision but are not authentication.

## What is measured and what is not

The verification ledger supports inspection of check failures and evidence.
The tests cover pass, fail, gate, replay, promotion and rollback. They do not
measure the *Galaxy Effect*. Repeated Error Rate requires labeled error
categories across real tasks, and Escaped Defects requires independent
post-completion review. A later A/B experiment should hold model, harness,
task series and budget fixed, then compare those outcomes against verified
completion time, tokens, interventions and false rule blocks.

## Current boundaries

- Rule matching uses an explicit task type and literal command fragment. A
  command's name does not prove that its test reproduces the original bug.
- Replay labels are supplied by a reviewer; replaying two cases is a gate for
  a prototype, not statistical proof of generalization.
- Rules apply only to Galaxy's own DAG runner. External Codex/Claude/OpenHands
  adapters, app UI checks, browser simulations and CI rule generation are not
  implemented.
- Promotion is a trusted-local CLI operation. Multi-user authorization is
  required before offering it through a shared service.
