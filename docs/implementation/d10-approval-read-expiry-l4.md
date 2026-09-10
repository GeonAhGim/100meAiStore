# D-10 approval read and durable expiry — L4 packet

Status: implementation packet. Scope is local control-plane state only; it has
no vendor, payment, notification, or other external effect.

## Contract

- `approval_inbox` and `approval_detail` are pure reads. A GET never changes an
  approval, command, audit chain, outbox, or nonce. A due row is omitted from
  the actionable inbox; detail computes `expired` with no actions until the
  worker persists the same fail-closed state.
- A tenant-scoped expiry operation selects approvals where `state=pending` and
  `expires_at <= now`, then changes the linked approval and command to
  `expired`, appends one audit event and one idempotent outbox event in the same
  transaction. The operation may be rerun after a crash or restart.
- Expiry is fail-closed and tenant isolated. Work for one tenant cannot inspect
  or mutate another tenant. Concurrent runs produce one terminal transition.
- Authenticated `GET /api/approvals` and `GET /api/approvals/{opaque-id}` use the
  server-side cookie session, membership capabilities, safe previews, and
  `Cache-Control: no-store`. Query identity overrides are rejected.
- Existing same-origin JSON nonce and decision POST contracts are unchanged.

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| D10-01 | Reading a due approval exposes effective `expired`/no actions without any database mutation. |
| D10-02 | One expiry run atomically expires due approval/command and emits exactly one audit/outbox record. |
| D10-03 | Repeat and post-restart runs are no-ops; concurrent runs cannot duplicate evidence. |
| D10-04 | Expiry selects only the requested tenant and respects a bounded batch size. |
| D10-05 | Inbox/detail GET require a valid cookie session and enforce role and tenant boundaries. |
| D10-06 | GET responses are `no-store`, reject query overrides, and redact sensitive preview fields. |
| D10-07 | Existing POST nonce/decision regression tests remain green. |

## Failure and recovery

Any exception rolls back approval, command, audit, and outbox together. The next
worker tick safely repeats the same tenant operation. A due approval remains
non-executable because the decision path independently checks wall-clock expiry.
The worker reports a count and IDs for local observability; it does not dispatch
the outbox or contact users.
