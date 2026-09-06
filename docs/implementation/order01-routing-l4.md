# ORDER-01 local DEMO order routing — L4 contract

## Boundary

ORDER-01 consumes only the immutable normalized payload written by ADAPTER-01
and caller-supplied supplier quote fixtures. It creates tenant-scoped internal
order/line projections, deterministic routing decisions and purchase-order
approval proposals. It does not call a channel, supplier, payment service, or
network and it never claims that a PO was submitted or paid.

## Contracts

`ingest_order(context, channel_id, payload_ref, idempotency_key?)` authenticates
the tenant, reloads and verifies payload schema/digest, and creates an
`ACCEPTED` `ChannelOrder` with immutable source reference and `OrderLine`s.
The default idempotency key is `order:{channel_id}:{external_order_id}`;
replays return the original order and do not append effects. Reusing the same
external key with another payload is rejected.

`propose_routing(context, order_id, supplier_options,
expected_order_version=1)` accepts local quote rows keyed by SKU:
`supplier_id`, `unit_cost_minor`, and `available_quantity`. For each line it
selects the lowest feasible cost, breaking ties by opaque supplier ID. A line
without a feasible quote moves the order to `EXCEPTION` and creates no PO.
Successful routing creates one `SupplierPurchaseOrder` per supplier, line-level
`RoutingDecision`/`PurchaseLine` records, and a purchase approval command in
`APPROVAL_PENDING`. A single channel order may therefore produce multiple POs.

The order aggregate uses an optimistic version CAS. PO proposal keys are
`po:{order_id}:{supplier_id}` and approval keys append `:approval`; retries
return existing records. A successful PO proposal remains independent if
another supplier's proposal later fails. Approval is a durable proposal only;
there is no supplier write, payment, or external status transition in this
packet.

## Storage and restart

Migration 9 adds tenant-scoped `channel_orders`, `order_lines`,
`routing_decisions`, `supplier_purchase_orders`, and `purchase_lines` with
composite foreign keys, unique external/idempotency keys, and version columns.
Order ingestion, lines, audit and outbox commit atomically. Routing, decisions,
POs, purchase lines, approval command/audit/outbox commit atomically. SQLite
restart reloads all rows; stale order versions and duplicate keys fail closed.

## Acceptance evidence

`tests/store_core/test_order_routing.py` covers duplicate order ingestion,
restart persistence, multi-supplier split POs, approval-pending state,
deterministic routing, stale CAS/replay, unavailable-supplier exception, and
tenant isolation. Full prior suite remains a required gate. Later packets must
add human PO decision re-checks, payment evidence, supplier response
reconciliation, cancellation races and tracking; none are claimed here.

Verification: `python -B -m pytest -q -p no:cacheprovider` — 86 passed on
2026-09-06; compileall, `git diff --check`, and repository secret-pattern scan
also passed.
