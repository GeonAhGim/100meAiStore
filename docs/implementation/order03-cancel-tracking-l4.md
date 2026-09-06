# ORDER-03 local DEMO cancellation race and tracking ingestion — L4 packet

## Boundary

This packet is the next safe extension after ORDER-02. It is limited to local
DEMO fixtures and durable tenant-scoped state. It must not call a channel or
supplier, submit a cancellation, buy postage, send a notification, or move
money.

## Cancellation contract

`request_demo_cancel(context, order_id, reason, expected_order_version)` first
reloads the channel order and all PO checkpoints in one CAS transaction. A
`PO_PENDING` order may transition to `CANCELLED` and its still-pending POs may
transition to `CANCELLED`. A `SUBMITTED` or `ACKNOWLEDGED` PO is never silently
rolled back: the order retains the evidence and receives a durable
`CANCEL_REQUESTED` task/outbox event for later external work. A stale order
version or already-terminal request is a conflict/replay, not a second effect.
Cancellation decisions preserve every prior approval, PO, response digest and
audit event.

## Tracking contract

`ingest_demo_tracking(context, order_line_id, tracking_key, status,
observed_at)` accepts only opaque tracking keys and an allowlisted status
(`LABEL_CREATED`, `IN_TRANSIT`, `DELIVERED`, `CANCELLED`). The same tracking
identity/status is an idempotent replay; a changed status appends a new
immutable observation and advances the line checkpoint by CAS. Tracking is
line-level because one order may have multiple suppliers and shipments.

`DELIVERED` is an observed supplier/channel fact only; it does not imply
settlement, customer notification, or claim closure. Unknown or contradictory
tracking remains an exception for reconciliation and never triggers a blind
external write.

## Acceptance targets

Tests must cover pending-PO cancellation, submitted-PO cancellation race,
stale CAS, duplicate and corrected tracking, multi-supplier line isolation,
restart recovery, tenant isolation, audit/outbox atomic rollback, and no
external adapter invocation. The packet remains local DEMO until official
channel/supplier contracts and approval gates are separately authorized.
