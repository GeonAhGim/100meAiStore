# P2-12 DEMO scope-bounded release policy on the synthetic adapter — L4 packet

Status: implementation packet. Scope is DEMO only under the approval records in
`docs/implementation/approvals/` (G4/G5, mode DEMO). The check decides whether
a proposed DEMO write may proceed; it performs no write itself. Synthetic
adapter only. No network, no credentials, no real channel or supplier write.
LIVE remains unapproved.

## Contract

- Gate check: G4 and G5 must each have a DEMO record, otherwise
  `BoundedBlocked` is raised before any decision.
- Policy: a frozen, versioned `BoundedPolicy` with `capability`,
  `max_amount_minor`, `max_skus`, `max_writes_per_day`, `max_age_seconds`
  (approval expiry) and `halted`. All caps are positive integers; the money
  cap for DEMO is 0 effective (no money moves) but the field is enforced the
  same way so LIVE reuses the code path.
- Operator approval: a frozen `ScopedApproval` naming `capability`,
  `target_ref`, `payload_digest`, `approved_at` and `approver`. It is
  scope-specific: it admits exactly one capability on exactly one target.
- Proposed write: `capability`, `target_ref`, `payload` (dict),
  `amount_minor`, `sku_count`, `proposed_at`.
- Decision: `admit` only when the approval matches capability and target, the
  canonical digest of `payload` equals `approval.payload_digest`, the approval
  is not older than `max_age_seconds` at `proposed_at`, `amount_minor`,
  `sku_count` and today's write count all fit their caps, and the policy is
  not halted. Otherwise `reject` with an ordered tuple of reason codes.
  Reason codes: `halted`, `scope_mismatch`, `digest_mismatch`,
  `approval_expired`, `amount_cap`, `sku_cap`, `daily_cap`.
- Audit: every decision appends one `AuditRecord` (sequence, decision,
  reasons, capability, target_ref, payload digest, policy version, at) to the
  gate's audit ledger; the ledger is append-only and never contains payloads.
- Stop and restore: `halt()` returns a new policy with `halted=True` and
  `restore()` the reverse. Both append an audit record of kind `halt` or
  `restore`. Decisions made before a halt remain in the ledger; a restored
  policy admits again with the same version and counters.
- Output: a `BoundedGate` whose `ledger` is a tuple and whose `evidence()`
  returns a canonical JSON summary (policy version, halted, counts by
  decision, ledger digest).

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| P2-12-DEMO-BOUNDED-01 | A proposed DEMO write is admitted only when a scope-specific operator approval exists, the payload digest matches and every money, SKU, count and time cap holds |
| P2-12-DEMO-BOUNDED-02 | Every admit or reject decision produces one audit record and a halted policy rejects everything until restored |
| P2-12-DEMO-BOUNDED-03 | Stop and restore evidence is produced and the check is fail-closed without DEMO approval records for G4 and G5 |

## Evidence

| Commit | Tests | Fix rounds | Verified by |
|---|---|---|---|
| `ea2a694` | 320 ran, OK | 0 | worker `2d33643e-66d8-4e93-b099-468c14ed830f` |
