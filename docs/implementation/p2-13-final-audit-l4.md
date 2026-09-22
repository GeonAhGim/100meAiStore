# P2-13 whole-product traceability and gap audit (offline) — L4 packet

Status: implementation packet. Offline audit only. It reads repository files
and a throwaway DEMO database; it holds no G1..G5 authority and performs no
external read or write. LIVE remains unapproved.

## Contract

- Traceability: for every item in `docs/implementation/development-progress.json`
  the check resolves the evidence document, extracts acceptance IDs from its
  `| ID | ... |` table rows (IDs of the form `LETTERS-DIGITS` or
  `TASK-NN`), and searches `tests/**/*.py` for each ID. An item is
  `traced` when the document exists and every extracted ID is referenced by
  at least one test. Items whose document has no ID table are reported as
  `no_acceptance_table`, never as traced.
- Gap classes per item: `evidence_missing`, `no_acceptance_table`,
  `untested_ids` (with the IDs), `status_overclaims` (status is `completed`
  but the item is not traced). An item can carry several classes.
- Bootstrap and restart: the check creates a fresh SQLite repository in a
  temporary directory, bootstraps one tenant, closes it, reopens the same
  file and asserts that no outbox event is in the `leased` state and that the
  tenant is still readable. The result records event count and leased count.
- Readiness: gate modes are read from `docs/implementation/approvals/`. The
  report's `live_approved` is true only when G1..G5 all exist with mode
  `LIVE`; otherwise `live_approved=false` with the list of gates that are
  DEMO or missing. The report is fail-closed: any exception during the audit
  yields `ready=false` with the error class, never a partial pass.
- Output: a frozen `AuditReport` with a canonical JSON payload and SHA-256
  digest; `ready` is true only when there are no gaps, the restart check
  passed, and the readiness section is present.

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| P2-13-FINAL-AUDIT-01 | A traceability check maps every progress item to its evidence document and to tests referencing its acceptance IDs and reports gaps |
| P2-13-FINAL-AUDIT-02 | Clean DEMO bootstrap and worker restart leave no leased outbox events and the check proves it |
| P2-13-FINAL-AUDIT-03 | The readiness report is fail-closed and states that LIVE is not approved while any gate record is DEMO or missing |

## Evidence

| Commit | Tests | Fix rounds | Verified by |
|---|---|---|---|
| `d479314` | 323 ran, OK | 0 | worker `9df03896-3cc2-40aa-b069-658ca9f9ea8b` |
