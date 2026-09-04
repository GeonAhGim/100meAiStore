# 100meAiStore architecture package

Architecture-only package for `100meAiStore` requirements v1. It defines implementation boundaries and contracts; it does not authorize application-code changes.

## Source and scope

- Normative source: `docs/100meAiStore-requirements-v1.md` (seven requirement blocks; deliverables `D-01`–`D-11`).
- Informative source: `docs/report-source.md` (`S-01`–`S-15` in [traceability](12-traceability.md)).
- In scope: Korean MVP operations for Naver Smart Store and Coupang, domestic direct-ship consignment, supplier API/CSV/Excel/XML/manual adapters, and DEMO/LIVE operation.
- Out of scope: storefront checkout, automatic payment/transfer, high-risk regulated/luxury goods, and international execution. International support is a comparison pipeline only.

## Deliverables

먼저 읽을 한국어 요약: [00-summary-ko.md](00-summary-ko.md)

| ID | Document | Covers |
|---|---|---|
| D-01 | [context-and-deployment](01-context-and-deployment.md) | context, containers, components, topology |
| D-02 | [data-model](02-data-model.md) | tenant model, ownership, retention |
| D-03 | [rbac-and-approval](03-rbac-and-approval.md) | RBAC, approval policy matrix |
| D-04 | [state-machines](04-state-machines.md) | catalog, offer, order, procurement, fulfillment, claim, settlement, compensation |
| D-05 | [adapter-contracts](05-adapter-contracts.md) | channel, supplier, finance, notification, ChatGPT, n8n contracts |
| D-06 | [agent-and-tool-gateway](06-agent-and-tool-gateway.md) | agent boundary, typed tools, BYOK, budget, questions |
| D-07 | [pwa-ux-contract](07-pwa-ux-contract.md) | Android-first PWA IA and approval-screen contract |
| D-08 | [security-privacy-audit](08-security-privacy-audit.md) | security, privacy, audit, threat model, stop/recovery |
| D-09 | [gcp-cost-observability-dr](09-gcp-cost-observability-dr.md) | GCP envelope, telemetry, backup, DR |
| D-10 | [validation-and-release](10-validation-and-release.md) | DEMO/LIVE, scenarios, SLOs, gates |
| D-11 | [adr-risks-backlog](11-adr-risks-backlog.md) | ADRs, risks, backlog, dependencies, acceptance criteria |
| — | [traceability](12-traceability.md) | requirement/source-to-artifact coverage |

## Architecture invariants

1. `Tenant` is the isolation boundary. Every tenant-owned record, secret reference, job, event, budget, and cost has a tenant key; authorization checks it before business policy.
2. `SupplierProduct` (source), `CanonicalProduct` (normalized), and `ChannelOffer` (projection) are separate and lineage-linked. Source snapshots are immutable.
3. `ChannelOrder` and `SupplierPurchaseOrder` are separate state machines. Partial routing, partial cancellation, multiple shipments, claims, and settlement lines are first-class.
4. PostgreSQL is the transaction source of truth. State changes write the current projection and append-only event in one transaction; outbox publishes after commit.
5. Every external write is typed, authorized, policy-checked, idempotent, bounded, audited, and followed by verification/reconciliation.
6. AI proposes and interprets; deterministic policy, approval, and domain commands decide whether a side effect is allowed. AI never receives direct database or channel credentials.
7. Automatic payment/transfer is disabled. A user records supplier payment evidence after approval.
8. Stop is fail-closed for new registration, change, and purchase; in-flight shipment/CS continues. Recovery requires inspection and one authorized approval.

## Normative v1 defaults

These values come from the requirements and are configurable only through the master policy; they are not inferred by an agent.

| Area | Default |
|---|---|
| domestic MVP | Naver Smart Store and Coupang; register approved offers on both |
| product funnel | about 300 candidates; about 50 approved registrations; profit quality beats quota |
| supplier comparison | 4–10 suppliers total, diversified by category; missing business/return information is a hard exclusion |
| schedules | agent 09:00/13:00/17:00/21:00; product approvals 09:00/13:00/17:00; purchase approvals 09:00/12:00/15:00/18:00; configurable and separate even when times overlap |
| approvals | all PO, new product, supplier replacement, refund/compensation, non-routine CS, sell-stop, and content changes; 24h expiry; immediate decision still requires re-check |
| safety | margin below 10% blocks execution; stockout/negative margin/risk triggers temporary pause |
| cash controls | initial cash KRW 500,000; working capital reference KRW 3,000,000; daily PO cost KRW 2,000,000; single-order warning KRW 100,000; safety balance KRW 500,000 |
| profit | projected net profit ≥KRW 3,000/item; margin ex-ad ≥15%; margin with ad ≥10%; weekly KRW 5,000,000 is a KPI, not a guarantee; initial ad experiment ≤KRW 100,000 |
| content/CS | preserve originals; transform resize/background/color/text; generated background optional and non-deceptive; candidate video retained 10 days; routine CS templates may auto-answer, promises/claims/compensation require approval |
| regulated product fields | feed/treat: manufacturer, expiry (default ≥6 months), ingredients, origin, storage, allergens; efficacy wording warns rather than silently deleting |
| operations | no automatic card payment or transfer; finance is manual ledger + CSV/Excel reconciliation; read-only finance connector boundary only |
| international | compare market/API/logistics/cost/returns/onboarding/margin after domestic simulation; no international execution in MVP |

Schedules are evaluated in the tenant timezone but persisted in UTC with the schedule-policy version.

## Explicitly unresolved decisions

The package does not invent the decisions listed below. Each is a release blocker until resolved by the named gate in [D-11](11-adr-risks-backlog.md).

| Key | Missing decision | Safe interim behavior |
|---|---|---|
| DEC-01 | first supplier and official access terms | DEMO fixture/manual adapter only |
| DEC-02 | SKU/order volume, sync interval, stale windows | no LIVE bounded automation |
| DEC-03 | storefront checkout vs seller operations only | resolved: seller-operations only |
| DEC-04 | automatic price-change rate cap | no automatic price change until master configures a cap; purchase/refund always require approval |
| DEC-05 | ChatGPT exposure | resolved for v1: authenticated tenant members only; customer-facing exposure excluded |
| DEC-06 | repository license | code is public but no license is granted until the owner selects one; SBOM/license scan required |
| DEC-07 | legal retention periods and processor/subprocessor terms | legal hold; pseudonymize only after approval |
| DEC-08 | physical PostgreSQL hosting inside KRW 30,000 cap | compare Cloud SQL trial/actual quote with an external serverless PostgreSQL option before deployment |

## Validation of this package

- All `D-01`–`D-11` are linked above and mapped in [traceability](12-traceability.md).
- Every side-effect contract in D-05/D-06 has tenant, authorization, idempotency, timeout/retry, audit, and verification requirements.
- Numeric defaults are copied from requirements; unresolved limits are marked `TBD` and fail LIVE gates.
- Mermaid diagrams are design notation; implementation must preserve the named states, transitions, guards, and events.
