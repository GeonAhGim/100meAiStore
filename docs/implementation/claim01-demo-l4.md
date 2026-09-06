# CLAIM-01 local DEMO claim intake and status evidence — L4 packet

## Boundary

This slice consumes the durable local order projection and records a tenant
scoped claim with independent consumer/channel/supplier statuses. It uses no
customer PII and performs no channel message, refund, supplier action,
payment, network, or model call.

## Contract

`open_demo_claim(context, order_id, claim_type, amount_minor, idempotency_key)`
validates an existing order and nonnegative minor-unit amount, then creates an
`OPEN` claim atomically with audit/outbox. Replaying the key returns the same
claim; reusing it for different order/type/amount is a conflict.

`record_demo_claim_status(context, claim_id, status_kind, status,
expected_version)` updates exactly one of `consumer`, `channel`, or `supplier`
status under CAS. Allowed statuses are `OPEN`, `EVIDENCE_PENDING`, `APPROVED`,
`REJECTED`, `REFUND_PENDING`, `REFUNDED`, and `CLOSED`. A status change is
append-only evidence in the claim event log; identical state replay is a
no-op. No status is inferred from another party's status, and `REFUNDED`
never means a payment was executed in DEMO.

All claim state, event, audit, and outbox changes are atomic and tenant scoped.
The state survives restart; foreign IDs are indistinguishable from missing
IDs. Refund approval/execution and settlement matching remain later slices.

Acceptance targets: duplicate intake, amount/order mismatch, independent
status transitions, stale CAS, restart recovery, tenant isolation, rollback
on audit/outbox failure, and zero external side effects.

Implementation evidence: `tests/store_core/test_claim01.py` covers duplicate
intake, amount bounds, independent statuses, CAS, restart, and tenant
isolation. Full verification is `python -B -m pytest -q -p no:cacheprovider` —
94 passed on 2026-09-06; compileall, `git diff --check`, and repository
secret-pattern scan passed.
