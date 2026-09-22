# User Operations & Scale Development Guide

Status: implementation guidance  
Audience: Codex, implementation workers, reviewers, product/UX developers  
Scope: operator-facing product development, vertical-slice sequencing, and scale-safe implementation

> This document supplements the normative architecture under `docs/architecture/` and the confirmed requirements in `docs/100meAiStore-requirements-v1.md`. It does **not** relax tenant isolation, approval, policy, idempotency, audit, payment, stop/recovery, DEMO/LIVE, or security invariants.

## 1. Product intent

100meAiStore should be developed as an **AI commerce operations system**, not as a bulk product-listing macro or a conventional seller ERP.

The target user experience is:

> The user supervises an AI commerce operating team. The system performs routine work autonomously within policy; the user spends attention only on decisions, exceptions, capital, risk, and strategy.

Normal-day UX should therefore optimize for **low human attention per unit of commerce volume**. A useful design target is that a small operator can understand the business and clear material decisions in roughly 10-20 minutes per day when there are no exceptional incidents. This is a UX target, not an operational guarantee.

The internal architecture may remain sophisticated, but ordinary users must not need to understand workers, queues, leases, outbox records, state-machine internals, model routing, or reconciliation mechanics to operate the business safely.

## 2. The existing "300 candidates / ~50 approved listings" requirement is a bootstrap target, not a system cap

The confirmed requirement that the initial funnel evaluates roughly 300 candidates and approves roughly 50 listings exists to validate product selection quality and early operations. It MUST NOT become a hard-coded platform limit.

Implementation rules:

- No domain rule may assume `50` is the maximum number of products, listings, offers, or active SKUs.
- No UI may depend on rendering the complete catalog in one request or one screen.
- No worker may require a full-catalog scan in one transaction.
- No approval flow may require the user to inspect every routine item individually.
- Product, listing, inventory, price, order, settlement, and audit APIs MUST support pagination/cursors and bounded query windows.
- Bulk work MUST be decomposable into restart-safe batches with idempotent checkpoints.
- Limits that exist for safety, channel policy, supplier policy, cash, cost, or rate limits MUST be explicit configuration/policy, not accidental technical ceilings.

The implementation should be able to grow through scale bands without changing the core domain model:

| Scale band | Active channel offers per tenant | Intent |
| --- | ---: | --- |
| S0 | ~50 | initial DEMO / SHADOW validation |
| S1 | ~500 | small real operator portfolio |
| S2 | ~5,000 | multi-category / multi-supplier operations |
| S3 | ~50,000 | architecture-readiness target; requires measured infrastructure and cost validation |

These are **scale bands, not promised capacity or launch quotas**. Candidate pools may be substantially larger than active offers. Real capacity must be established by load tests, channel quotas, supplier behavior, database measurements, and cost gates.

The system should preserve the same contracts and domain semantics from S0 to S3. Infrastructure may scale, partition, or specialize later without rewriting user workflows or core transaction meanings.

## 3. User-facing information architecture

The primary operator experience should converge around five top-level areas.

### 3.1 Today

The default landing surface is an operating brief, not a generic analytics dashboard.

Show, in priority order:

- realized / expected contribution profit
- available purchasing cash and protected safety cash
- approvals requiring action
- exceptions requiring action
- channel/supplier risk or outage summary
- orders progressing normally
- AI/operator actions completed today
- important changes since the last visit

The home surface should answer one question quickly: **"What needs my attention now?"**

Technical health data can exist, but detailed worker/queue diagnostics belong in an operator diagnostics or engineering surface, not the primary business UI.

### 3.2 Approvals

Approval is the central human-control surface.

The UI should aggregate decisions by material effect, while preserving item-level traceability. It should show:

- what will change
- why the system recommends it
- expected profit/cash/risk impact
- policy reason
- what happens if the user does nothing
- whether approval expires or requires re-check
- affected channel/supplier/products/orders

Actions should remain simple: approve, reject, hold, inspect details, or modify when the domain contract allows modification.

At higher scale, the user should approve **decision groups**, not mechanically click hundreds or thousands of items. Group approval is allowed only when every member shares compatible policy, action semantics, risk bounds, and material inputs. The backend must still emit traceable per-entity commands/events/results.

### 3.3 Products

The product surface represents an AI-managed portfolio, not a manual product-entry grid.

The system should support:

`source discovery -> supplier product -> normalization -> canonical product -> margin/risk evaluation -> recommendation -> approval -> channel offer -> observation -> repricing/stop/expand decision`

Each recommendation should explain the business case, including where available:

- expected selling price
- supplier cost
- shipping
- channel/payment fees
- advertising assumption
- expected return/claim cost
- expected AI/content cost where material
- expected contribution profit
- margin
- supplier confidence
- competition/price context
- policy warnings
- data freshness

Users define operating rules and portfolio intent; the system performs the repetitive catalog work.

### 3.4 Orders

The default order UX should be **exception-oriented**.

Do not force the operator to review every healthy order. Present operational states such as:

- normal / progressing
- attention required
- incident / blocked

Routine deterministic processing should continue without human inspection when policy allows it. Supplier stock loss, material cost movement, address or identity ambiguity, split shipment, missed cutoff, cancellation conflict, claim, and reconciliation mismatch should surface as exceptions.

### 3.5 Money

Cash and realized profit are first-class product features.

The money surface should make the following visible:

- available cash
- protected safety cash
- approved/pending purchase obligations
- expected near-term purchase requirements
- settlement receivables
- expected settlement dates
- refunds/claims exposure
- realized contribution profit
- expected contribution profit
- profit leakage by fee, ad, shipping, return, claim, content/AI, and supplier cost

Revenue alone is not an adequate operating metric.

## 4. AI interaction model: "operations lead", not unrestricted chatbot

Conversational AI may provide a natural operator interface, but all side effects remain behind the existing deterministic safety boundary.

Example user intent:

`마진 15% 아래 상품은 전부 내려.`

Expected product behavior:

1. AI interprets intent.
2. System resolves the affected tenant/channel/offers using deterministic queries.
3. Policy engine evaluates permissions, bounds, current data freshness, stop state, and material effects.
4. System produces a typed change plan.
5. UX summarizes impact, e.g. affected offers, estimated revenue decrease, expected profit/risk improvement.
6. Required approval is obtained.
7. Typed commands execute through the authorized gateway.
8. External readback/reconciliation verifies the result.
9. Audit/activity explains what happened.

The user should experience this as one coherent decision, even though the safety architecture remains explicit internally.

AI MUST NOT bypass typed commands, authorization, policy, approval, idempotency, audit, readback, or reconciliation.

## 5. Operating policy should look like company rules

Do not expose implementation knobs as the main configuration model. The user should configure business policy such as:

- minimum expected contribution profit
- minimum pre-ad / post-ad margin
- daily purchase-cost limit
- protected cash floor
- single-order warning threshold
- maximum automatic price movement
- new-product approval requirement
- refund/compensation approval threshold
- preferred/excluded categories
- supplier risk rules
- inventory freshness/staleness bounds
- channel-specific operating windows
- AI budget/model quality mode

These settings should map to versioned policy objects and deterministic enforcement. Changes to high-impact policy remain privileged and audited.

## 6. Scale-safe UX and API rules

### 6.1 Never use "show everything" as an operating primitive

Catalogs, orders, approvals, events, settlements, and audit records must use bounded retrieval. Prefer opaque cursors for high-churn datasets where offset pagination would produce unstable results.

The UI should support saved filters, search, server-side sort, server-side aggregation, and virtualized/incremental rendering for large collections.

### 6.2 Bulk actions are plans, not giant transactions

A request such as "stop 8,000 low-margin offers" should create a bounded, reviewable operation plan.

The plan should contain:

- selection snapshot / query definition
- policy version
- reason
- expected affected count
- impact summary
- approval reference where required
- per-item typed operation identity
- progress counters
- retry/dead-letter state
- reconciliation result

Execution should fan out into bounded idempotent units. Never hold one database transaction open across a large remote marketplace operation.

### 6.3 Prioritize work by business criticality

At scale, queues must distinguish at least:

1. safety/emergency stop and security
2. order/inventory/fulfillment correctness
3. approval/expiry and customer-impacting exceptions
4. price/listing maintenance
5. settlement/profit reconciliation
6. discovery/content/growth work

Optional AI/discovery work must not starve order, inventory, safety, or approval processing.

### 6.4 External quotas are a first-class constraint

Marketplace and supplier adapters must expose rate-limit/quota/backoff information where available. Workers should pace work per tenant/channel/supplier and avoid synchronized full-catalog bursts.

Prefer incremental change feeds, cursors, watermarks, fingerprints, ETags/version markers, or delta polling where the provider supports them.

### 6.5 Database growth must be anticipated

Codex should preserve partitioning/sharding options without prematurely introducing distributed complexity.

At minimum:

- tenant keys remain present on tenant-owned rows
- high-volume query paths are explicitly indexed and benchmarked
- event/audit/outbox growth is measured
- retention/archival policy is separated from transaction correctness
- IDs do not depend on a single small catalog namespace
- full-table scans are rejected from hot operational paths
- read models/aggregates may be introduced without changing the authoritative transaction model

PostgreSQL remains the production source of truth according to the normative architecture. SQLite remains a local DEMO boundary, not the production scaling strategy.

## 7. Human attention must scale sublinearly

A platform with 5,000 offers cannot create 100x more operator clicks than a platform with 50 offers.

Therefore:

- routine healthy entities should collapse into summaries
- exceptions should be ranked by urgency and economic impact
- similar decisions should be grouped when policy-safe
- repeated low-value notifications should deduplicate/coalesce
- approvals should include recommended defaults and material deltas
- the system should remember unresolved work and resume reliably after restart
- dashboards should use aggregates plus drill-down rather than raw exhaustive lists

A key product metric should be **human interventions per 100 orders / per 1,000 active offers**, segmented by reason. The long-term direction is to reduce this ratio without weakening safety policy.

## 8. Activity and explainability

Users need confidence that automation is controlled.

Provide an activity timeline that can explain, for example:

`order received -> supplier stock checked -> PO proposed -> user approved -> supplier order recorded -> tracking received -> channel updated -> settlement reconciled -> realized profit posted`

For AI-assisted decisions, record enough structured evidence to answer:

- what input was used
- what policy version applied
- what the recommendation was
- who/what approved it
- what command executed
- what external result/readback was observed
- whether reconciliation succeeded

This is a user trust surface built on top of the audit/event model; it is not a replacement for immutable audit records.

## 9. Mobile-first decision UX

Android/PWA should be treated as a primary approval and exception interface.

A material-change card should be understandable in seconds. Example:

- supplier cost: 18,000 -> 21,500 KRW
- margin: 16.2% -> 8.4%
- recommended action: increase price to 34,900 KRW or stop sale
- affected channels/orders
- data freshness

Then present bounded choices such as price adjustment, stop sale, reject, hold, or inspect details.

Do not require desktop-only workflows for normal approvals, stop actions, or incident acknowledgment.

## 10. Emergency stop must remain obvious at every scale

Expose stop controls at global/tenant/channel/supplier/product scope as already designed.

At high scale, a stop request must not enumerate every entity synchronously before becoming effective. The stop authority/state should become effective first; workers and command authorization then fail closed for newly prohibited work. Follow-up fan-out/reconciliation can proceed asynchronously.

In-flight shipment and required CS behavior continue according to the normative architecture.

## 11. Development sequence: finish one user-visible vertical slice before adding horizontal breadth

The highest-value next sequence is a SHADOW vertical slice that a real operator can understand end-to-end:

`workspace/session`
`-> supplier product ingest`
`-> normalization/canonicalization`
`-> margin + risk evaluation`
`-> AI/product recommendation`
`-> mobile approval`
`-> Naver/Coupang listing SHADOW action`
`-> external-style readback/reconciliation`
`-> order ingest`
`-> supplier PO proposal`
`-> approval`
`-> shipment/tracking`
`-> cancellation/claim path`
`-> settlement`
`-> realized contribution profit`
`-> Today/activity summary`

Start with one product and one order for deterministic acceptance, but **all contracts must be batch/scale compatible from day one**. Then validate the same flow at S0 and progressively larger synthetic volumes.

Do not mistake "one-item vertical slice" for a one-item architecture.

## 12. Codex implementation rules

When Codex or another implementation worker uses this guide:

1. Preserve the normative architecture and existing security invariants.
2. Prefer completing an end-to-end operator journey over adding another isolated backend capability.
3. Do not introduce a `50 product` hard limit anywhere except an explicitly named DEMO/test fixture or configurable bootstrap campaign target.
4. Any list endpoint introduced for operational entities must be bounded/paginated from its first version.
5. Any bulk external write must decompose into bounded idempotent operations with durable progress and reconciliation.
6. Any new AI action that can cause side effects must terminate in a deterministic typed command/gateway path.
7. Any material user decision must show economic/risk impact where the required data is available.
8. Healthy high-volume operations should disappear into summaries; exceptions should receive human attention.
9. Tests must cover restart/retry/idempotency and tenant isolation, not only happy-path UI behavior.
10. Do not prematurely build distributed infrastructure solely to satisfy the S3 label. Preserve scale-safe contracts first, then introduce infrastructure from measured bottlenecks.
11. Do not assume the initial GCP cost target can sustain S2/S3 loads. Capacity, cost, and product scale must be measured independently; cost policy remains a hard operating guard where configured.
12. Maintain DEMO/LIVE/SHADOW distinctions and never treat mock success as external side-effect success.

## 13. Definition of Done for new operator-facing capabilities

A capability is not complete merely because its domain service exists. For operator-facing work, acceptance should include:

- business intent visible in Today/Approvals/Product/Orders/Money where applicable
- authorization and tenant isolation
- policy evaluation
- approval/reapproval rules where applicable
- idempotency
- durable state transition
- audit/event record
- restart/retry behavior
- external verification/reconciliation when an adapter side effect exists
- clear exception state
- mobile-usable decision flow where human action is expected
- bounded behavior under large result sets
- no hidden dependency on the initial 50-product bootstrap size

## 14. Anti-goals

Do not optimize the product toward any of the following:

- a spreadsheet-like UI that requires the user to manually manage every SKU
- mass listing volume without contribution-profit quality
- AI agents with direct database, marketplace-secret, payment, or unrestricted tool authority
- thousands of approvals generated because the backend lacks safe grouping or policy automation
- full-catalog synchronous scans for normal operations
- one huge transaction for remote bulk changes
- revenue-only reporting that hides cash needs and profit leakage
- a UI that exposes engineering internals as the normal operator workflow
- architecture rewrites solely because the catalog grows beyond the initial validation set

## 15. Product north star

The system should make the operator feel:

> "I am not manually running several online stores. I am supervising an AI commerce company whose routine operations are automated, whose exceptions are brought to me with context, and whose money and risk remain under my control."

Future features should be evaluated against that north star and against the requirement that human attention, not just compute, must scale efficiently as catalogs, channels, suppliers, tenants, and order volume grow.
