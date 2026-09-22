# P2-11 DEMO read-only Shadow comparison on synthetic fixtures — L4 packet

Status: implementation packet. Scope is DEMO only under the approval records in
`docs/implementation/approvals/` (G1/G3/G5, mode DEMO). Read-only: the run
compares snapshots and produces a report. No network access, no credentials,
no write to any channel, supplier, or database. LIVE remains unapproved.

## Contract

- Input: the approvals directory, the existing-operation snapshot (a list of
  `OfflineOrderSnapshot` values the operator already holds), the channel page
  parsed from a synthetic fixture, the page's observed time, the comparison
  time, and the bounded polling window in seconds.
- Gate check: G1, G3 and G5 must each have a record whose mode is DEMO,
  otherwise `ShadowBlocked` is raised before any comparison. Modes are read
  through the same reader as P2-10.
- Freshness: `age_seconds = now - observed_at`. A page older than the polling
  window is `stale=true`; the run still compares but every discrepancy from a
  stale page is downgraded to `unverified` and an `exception` of kind
  `stale_page` is emitted. A stale page is never silently accepted.
- Comparison: keyed by `(provider, line_id)`. Classes are `match`,
  `status_drift`, `quantity_drift`, `amount_drift`, `missing_in_channel`,
  `missing_in_operation`. A line can carry more than one drift class.
- Operator exceptions: one per non-match line plus `stale_page`, each with a
  kind, the line key, and a redacted detail (field name and both values; no
  names or addresses). Exceptions are ordered deterministically.
- Exit and rollback evidence: the report records `polling_window_seconds`,
  the number of pages compared (always 1 per run), the source digests of both
  sides and the decision `continue` (no exceptions), `hold` (exceptions only
  from a fresh page) or `exit` (stale page or any missing line). Rollback in
  Shadow is trivial because nothing was written; the report says so
  explicitly with `rollback="none_required"`.
- Output: a frozen `ShadowReport` with a canonical JSON payload and a SHA-256
  digest.

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| P2-11-DEMO-SHADOW-01 | A shadow run compares existing-operation and channel snapshots per external line ID and classifies discrepancies |
| P2-11-DEMO-SHADOW-02 | Freshness is measured against the bounded polling window and a stale page is reported, never silently accepted |
| P2-11-DEMO-SHADOW-03 | Operator exceptions and exit or rollback evidence are produced and the run is fail-closed without DEMO approval records for G1, G3 and G5 |
