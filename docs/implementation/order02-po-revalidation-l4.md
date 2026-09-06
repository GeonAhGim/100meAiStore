# ORDER-02 local DEMO PO revalidation and response reconciliation

## Boundary

This packet starts from ORDER-01 `APPROVAL_PENDING` purchase-order proposals.
It adds approval-time deterministic revalidation and a local DEMO PO state
machine. No supplier API, network request, real order, payment, credential, or
external write is permitted.

## Contract

`approve_demo_po(context, purchase_order_id, approve, reason)` reloads the PO,
channel order, purchase lines, and linked purchase approval in one transaction.
It requires the PO to be `APPROVAL_PENDING`, the order to remain `PO_PENDING`,
all referenced lines to remain routed to the same supplier, and each line's
positive quantity/cost to match the approval payload. It then uses the existing
approval CAS/expiry/capability/digest checks. Approval moves the PO to
`APPROVED`; rejection moves it to `CANCELLED`. Any failed revalidation leaves
PO and approval state unchanged.

`submit_demo_po(context, purchase_order_id)` is a local checkpoint only. It
requires a current approved, unexpired purchase approval and moves
`APPROVED → SUBMITTED`, emitting an outbox event. It does not call an adapter.

`reconcile_demo_po(context, purchase_order_id, response)` accepts only a strict
fixture response (`status` `ACKNOWLEDGED` or `REJECTED`, opaque optional
`provider_reference`, and an aware `observed_at`). It stores only the canonical
response digest/reference, never the raw body. `ACKNOWLEDGED` moves
`SUBMITTED → ACKNOWLEDGED`; `REJECTED` moves to `EXCEPTION`. Identical response
replays are no-ops; a changed response for the same submitted operation is a
conflict. Unknown results remain `SUBMITTED` for later reconciliation.

All transitions use tenant-scoped optimistic version CAS and commit PO state,
audit, and outbox atomically. Restart reloads the PO checkpoint and response
evidence. There is no automatic payment, supplier retry, or PO rollback caused
by another supplier's failure.

## Acceptance evidence

ORDER-02 tests cover approval-time stale/order/line revalidation, approval
expiry and rejection, approved→submitted→acknowledged/rejected transitions,
response idempotency/conflict, restart recovery, tenant isolation, and the
absence of external adapter calls. Full prior suite remains required.

Verification: `python -B -m pytest -q -p no:cacheprovider` — 88 passed on
2026-09-06; compileall, `git diff --check`, and repository secret-pattern scan
also passed.
