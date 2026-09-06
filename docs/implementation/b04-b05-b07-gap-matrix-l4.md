# B04/B05/B07 acceptance gap matrix

Audit of the current local DEMO evidence against D-11 DoD. No LIVE authority
or external side effect is implied.

| Slice | Evidence present | Gap / dependency | Safe next action |
|---|---|---|---|
| B04 inventory/price | durable observations/projections, 10% guard, offer gate, freshness/reapproval follow-up | no external sell-state authority; freshness window remains local default (24h) | retain local DEMO gate and require policy review before any external offer write |
| B05 order/PO | routing split, approval revalidation, cancel race, line tracking, restart and rollback follow-up | legacy direct order mutation paths still rely on their local contracts; no LIVE behavior is authorized | retain gateway/DEMO boundary and official adapter review as a future condition |
| B07 claims/settlement | strict import, match exceptions, projected/realized separation, multi-PO aggregation, restart/claim independence/rollback follow-up | no external accounting/payment authority; official finance retention remains a decision gate | retain local-only reconciliation and require explicit finance/retention decision for future work |

Dependencies remain B04→B05→B07 for the end-to-end bounded path. This packet
selects the B04 persistence/guard slice first because it is read/compute-only,
uses existing catalog products, and requires no external adapter. B04/B05/B07
remain `in_progress` until their listed acceptance evidence is implemented and
verified.
