# Evidence integrity and acceptance status — alpha 19

## Fixed in this release

- A `NodeResult.evidence` string authored by an agent cannot satisfy an active
  `require_evidence` rule. At least one verification command must have exited 0
  and its saved log must still match Galaxy's SHA-256 ledger entry.
- Before marking an execution `DONE`, every succeeded QA node with commands is
  audited. Missing or damaged logs make that node and the run fail. Reopening
  a `DONE` run repeats the audit and revokes completion if evidence changed.
- Active command rules are checked at run creation and again before execution.
- The learning hook marks an episode verified only after the ledger's passing
  log check, not from a model's `PASS` or evidence string.

## Reproduction and scope

On this build, `python -X dev -W always run_tests.py` reported 146 Python tests,
0 failures, 2 skips, and no SQLite ResourceWarning in the current runtime.
The skipped cases are live LLM inference (no endpoint) and log filtering
without generated runs. A separate report citing 141 tests and SQLite warnings
may come from a different copy or Python runtime. Without its actual log and
archive hash, those warnings cannot be located or claimed fixed here.

## Acceptance still open

- The full 15-scenario acceptance matrix has not been executed in a real
  deployment. This release adds targeted forged, damaged, and changed-rule
  cases, not a complete adversarial audit.
- The SQLite ledger and project files are trusted local state. A process with
  permission to modify both the database and logs can forge them; a model
  executor with unrestricted host access is outside this integrity boundary.
- CLI owner/team/reviewer strings are scope selectors, not authenticated
  identities. Multi-user isolation requires a real authorization service.
- A live end-to-end agent run, repeated-error comparison, escaped-defect
  review, model token counts and time to verified completion remain unmeasured.
- A post-completion audit marks damaged evidence invalid when the run is
  reopened; it cannot retract already exported external actions or previously
  copied knowledge without a separate reconciliation process.
