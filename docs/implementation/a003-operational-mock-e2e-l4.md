# A-003 operational mock E2E worker/outbox wiring — L4 packet

Status: design and failing-acceptance-test plan only. Core implementation is
intentionally deferred while A-024 and D-10 are in flight. This packet permits
local synthetic providers and SQLite only; it grants no LIVE supplier, channel,
payment, messaging, or fulfillment authority.

## Gap and completion boundary

The repository can ingest strict Naver/Coupang fixtures, route them into durable
purchase-order proposals, collect purchase approval, checkpoint a PO, reconcile
a fixture response, ingest tracking, and reconcile settlement. The generic
typed-command worker separately proves durable dispatch and UNKNOWN recovery.
The missing A-003 link is a single durable orchestration path that binds those
pieces to the same tenant, immutable approval intent, PO, provider operation,
outbox lineage, channel order lines, tracking evidence, and profit record.

A-003 closes only when an approved PO can produce an exact
`create_purchase_order` typed command, survive a timeout after the provider
effect and a process restart, reconcile that operation without a second effect,
and apply a verified PO ACK before downstream tracking and settlement are
accepted. A local demo is not evidence that real APIs, credentials, payments,
shipping, tax, or legal seller readiness work.

## Required invariant chain

1. A strict fixture order is ingested and routed to one or more supplier POs.
   Durable channel-line identity must survive restart and no line may disappear
   through SKU aggregation.
2. Every PO remains `APPROVAL_PENDING` until a user with
   `APPROVE_PURCHASE` approves it. A tenant may have up to three users, but one
   valid approval is sufficient; the creator/other users gain no implicit
   approval authority.
3. Approval produces no supplier effect. The orchestrator reloads PO, order,
   lines, policy, expiry, stop state, and approval intent immediately before it
   builds the typed command. It then calls `submit_demo_tool` with tool
   `create_purchase_order`, target bound to the exact PO, a canonical input
   containing supplier and line identities/costs, the original policy version,
   and the PO approval ID. No untyped `submit_demo_po` shortcut may be the
   external-effect authority.
4. Accepted command and `tool.command` outbox event commit atomically. The
   command/approval/intent/PO IDs and canonical digest must be cross-checkable;
   replay with the same idempotency key returns the same command and changed
   content conflicts.
5. `DemoToolCommandWorker` claims the tenant-scoped event and fenced attempt.
   With `DurableSyntheticProvider(mode="timeout_after")`, the provider records
   exactly one effect and the attempt becomes `UNKNOWN`. The outbox item remains
   recoverable and must not be reported as successful.
6. After closing and reopening both SQLite stores, another worker performs
   read-only lookup by the original operation key. `FOUND_SUCCESS` moves the
   attempt to `VERIFIED_SUCCESS`; no second provider effect occurs. Absence or
   inconclusive evidence follows CORE-03 and never blindly resends.
7. Only a verified-success attempt whose result binding matches the same typed
   command and PO can drive PO response reconciliation. A strict fixture
   readback supplies an opaque provider reference and aware observation time.
   The PO becomes `ACKNOWLEDGED` and its response digest is immutable. An ACK
   supplied before verification, for another PO/tenant, or with changed content
   fails closed.
8. Tracking is accepted only for durable order-line identities reachable from
   the acknowledged PO. Duplicate identical observations replay; contradictory,
   stale-version, cross-tenant, or unrelated-line observations do not advance
   fulfillment. No customer notification is implied.
9. Settlement ingestion remains channel/order/currency scoped. Realized profit
   is created only for a completely matched sale and persisted PO cost. Missing
   ACK, missing cost, unmatched order, currency mismatch, duplicate/conflicting
   source rows, or refund/fee ambiguity yields `EXCEPTION` or a null projected
   amount—never a positive success claim.
10. Every state mutation, audit row, and corresponding outbox row commits in
    one local transaction. A forced outbox failure rolls the mutation back.
    Restarts preserve pending/UNKNOWN state and completed outbox checkpoints.

## Proposed implementation seam

Add a narrow DEMO-only coordinator rather than teaching generic repositories
business policy. Suggested service operations:

* `enqueue_demo_po_execution(context, po_id, idempotency_key)` reloads and
  validates the approved PO, derives canonical typed input, and delegates to
  `submit_demo_tool`. It does not call a provider.
* `apply_verified_demo_po_result(context, po_id, command_id, attempt_id,
  readback)` verifies tenant and full lineage plus `VERIFIED_SUCCESS`, then
  delegates the strict ACK/REJECTED payload to `reconcile_demo_po`.
* A bounded outbox runner selects only `tool.command`; downstream business
  events remain independently checkpointed. It must not mark an UNKNOWN event
  completed.

Reuse `DemoExecutionControlPlane`, `DemoToolCommandWorker`,
`DurableSyntheticProvider`, and current order/finance services. Do not add
transport interfaces, secrets, or a generic adapter registry. The typed input
schema should be explicit and versioned; at minimum it binds `purchase_order_id`,
`channel_order_id`, `supplier_id`, ordered line identities, quantity,
unit-cost/currency, and approval intent digest. Provider readback must return
the operation key/result digest needed to prove correlation, not merely a free
text ACK.

## Failing acceptance tests to add first

Place the orchestration suite in a new
`tests/store_core/test_a003_operational_e2e.py` so it does not overlap current
A-024/D-10 tests. Tests must start red because the coordinator seam is absent,
not because of placeholders or disabled assertions.

| ID | Scenario | Required assertion |
|---|---|---|
| A003-E01 | approved PO → typed command → timeout-after → restart → reconcile | attempt is UNKNOWN before restart, VERIFIED_SUCCESS after; provider effect count is exactly 1; outbox completes only after verification |
| A003-E02 | full Naver fixture flow through ACK, tracking, settlement | exact channel-line IDs persist; PO ACK precedes tracking; matched settlement records projected/realized profit and all evidence survives restart |
| A003-E03 | same full Coupang fixture flow | same invariants with Coupang source identities and an empty strict claim page |
| A003-E04 | three users and delegated approval | only the user holding `APPROVE_PURCHASE` can approve; one valid decision is sufficient; other users cannot execute/approve through context substitution |
| A003-E05 | tenant swap at command, event, attempt, ACK, tracking, settlement | every boundary raises tenant/not-found authorization error and neither tenant mutates |
| A003-E06 | changed PO/line/cost/policy/expired approval/active stop after approval | no accepted command, no provider effect, approval renewal required where applicable |
| A003-E07 | idempotent replay and conflicting replay | same key+digest returns one command/effect; same key+changed input conflicts |
| A003-E08 | forged/mismatched ACK before verified attempt | PO remains SUBMITTED/APPROVED as appropriate; tracking and profit cannot establish success |
| A003-E09 | UNKNOWN lookup absent/inconclusive and stale fencing token | no resend unless CORE-03 authoritative-absence rules allow it; stale worker cannot checkpoint; manual review/backoff is durable |
| A003-E10 | crash boundaries around event claim, DISPATCHING, UNKNOWN, ACK, tracking, settlement | reopening stores produces one legal continuation with no lost/duplicated business effect |
| A003-E11 | injected audit/outbox/repository failure at every new mutation | full local transaction rolls back; retry is deterministic |
| A003-E12 | cancellation race before dispatch/while UNKNOWN/after ACK | pre-effect cancellation blocks dispatch; UNKNOWN preserves uncertainty; post-ACK creates a compensating/manual path, never deletes evidence |
| A003-E13 | settlement mismatch/missing PO cost/currency conflict/refund-only | batch/profit fail closed (`EXCEPTION` or null projected/realized values); dashboard cannot count realized profit |
| A003-E14 | concurrent workers claim one outbox event | fencing/CAS permits one effective worker, one provider effect, and one terminal checkpoint |

The main happy-path test must deny sockets and patch common subprocess/network
entry points so success proves local execution only. It should close/reopen the
application database and provider database at least twice: once while UNKNOWN
and once after downstream evidence is committed.

## File map and collision plan

Current reusable paths are `packages/store_core/gateway.py` (typed command and
approval binding), `tool_worker.py`, `execution.py`, `synthetic_provider.py`,
`order02.py`, `order03.py`, `finance01.py`, `service.py`, and
`sqlite_repository.py`. Existing focused evidence lives in
`test_demo_tool_worker.py`, `test_contract_demo_bridge.py`,
`test_b05_restart_rollback.py`, and `test_b07_split_restart_rollback.py`.

Implementation should first add the new test module and a new coordinator
module (suggested `packages/store_core/po_execution.py`), then expose only thin
service delegates. Repository/schema changes are justified only if immutable
lineage cannot be represented by current command, attempt, and PO columns. Do
not edit A-024 traceability/dashboard files or D-10 approval read/expiry files
until those changes land and their public interfaces are re-read.

## Verification and exit evidence

Run the new focused suite, all worker/execution/order/finance/tenant tests, then
the full pytest and unittest suites, compileall, `git diff --check`, tracked-file
secret/name checks, and the GitHub CI matrix. Record the exact test counts and
commit in the final-audit ledger. A-003 stays open if any high/medium case above
is missing, if a provider effect can occur without a fresh exact approval, if
UNKNOWN can be called success, or if settlement can claim profit without ACK,
cost, and matched source evidence.

## 2026-09-11 pre-implementation read-only audit

The failing-first suite is implementable without a schema migration, but four
contract mismatches must be resolved deliberately before the first green test:

1. `orders.propose_routing` creates a PURCHASE command targeted as
   `po:{channel_order_id}:{supplier_id}`; the durable PO ID does not exist until
   after that command is created. `gateway._TARGETS` has no `po` or
   `purchase_order` target. The coordinator therefore cannot truthfully submit
   the existing approval as a command targeted to the exact PO. Recommended
   bounded correction: retain the existing target string for compatibility,
   accept a typed target kind `purchase_order`, and make the coordinator prove
   that the target's order/supplier tuple and payload resolve to exactly one
   tenant-scoped PO before persisting the typed command. A future schema
   migration may bind `purchase_order_id` directly, but must not silently
   invalidate pending approvals.
2. The approval payload is exactly `order_id`, `supplier_id`, and ordered
   `lines[{order_line_id, quantity, unit_cost_minor}]`. Adding PO ID, currency,
   or approval digest to the typed gateway input would currently fail
   `_validated_tool_approval` because it requires JSON equality. Keep the
   approved payload canonical and store extra lineage in command/event metadata
   or a versioned execution binding; never weaken the equality check.
3. `DemoToolCommandWorker` requires `DemoExecutionControlPlane`, while the
   ingestion/routing APIs return `StoreControlPlane` semantics. The E2E fixture
   must instantiate `DemoExecutionControlPlane` from the beginning (it inherits
   the store plane), or the coordinator must reject the wrong concrete service
   before any event is emitted. Do not reconstruct a second service around the
   same open connection mid-transaction.
4. `reconcile_demo_po` presently accepts a caller-supplied ACK based only on PO
   state and response shape. It has no command/attempt/result lineage argument.
   `apply_verified_demo_po_result` must become the sole operational E2E path and
   verify terminal attempt, command target/payload, intent digest, provider
   reference/result digest, and tenant before delegating. The low-level fixture
   helper can remain for existing unit tests but must not be exposed as proof of
   operational ACK authority.

The first red-test order is fixed as E05 (tenant isolation), E06 (fresh
approval/gates), E01 (UNKNOWN/restart/exactly-once), E08 (verified ACK lineage),
E07/E14 (idempotency and concurrency), E11 (transaction rollback), then the two
provider happy paths E02/E03 and downstream E12/E13. This puts safety boundaries
ahead of the demonstrator and prevents a happy-path-only implementation from
being mistaken for closure.

No red scaffold was added during this audit. A module importing the not-yet
implemented coordinator would break the shared full suite while A-024 and D-10
are landing. The next implementation PM should add
`test_a003_operational_e2e.py` and `po_execution.py` together in one bounded
working checkpoint, immediately run the focused red suite, and then implement
only enough surface to make each group green in the order above.

Collision ownership is explicit: A-003 may own only the new coordinator, new
test module, `gateway.py`, `service.py`, and narrowly justified execution/worker
changes. It must not edit current in-flight `approvals.py`, `repository.py`,
`test_d10_approval_expiry.py`, dashboard files, traceability manifests/scripts,
or their tests. If A-003 requires repository/schema changes, defer them until
D-10 is committed and rebase/re-read the repository protocols first.
