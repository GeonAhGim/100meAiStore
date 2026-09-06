# B04/B05/B07 acceptance gap matrix

Audit of the current local DEMO evidence against D-11 DoD. No LIVE authority
or external side effect is implied.

| Slice | Evidence present | Gap / dependency | Safe next action |
|---|---|---|---|
| B04 inventory/price | pure validation and deterministic 10% calculation | observations and calculated projections are not durable or connected to offer projection; freshness and restart evidence absent | persist tenant-scoped snapshots/calculations and enforce optional guard at local offer projection |
| B05 order/PO | routing split, approval revalidation, cancel race, line tracking, restart and rollback follow-up | legacy direct order mutation paths still rely on their local contracts; no LIVE behavior is authorized | retain gateway/DEMO boundary and official adapter review as a future condition |
| B07 claims/settlement | strict import, match exceptions, realized/projection separation | realized contribution does not yet bind a durable projected-cost snapshot; split-order and restart/rollback evidence incomplete | add immutable projected-cost reference and reconciliation evidence before claiming completion |

Dependencies remain B04→B05→B07 for the end-to-end bounded path. This packet
selects the B04 persistence/guard slice first because it is read/compute-only,
uses existing catalog products, and requires no external adapter. B04/B05/B07
remain `in_progress` until their listed acceptance evidence is implemented and
verified.
