# D-10. DEMO/LIVE validation, scenarios, SLOs, release gates

## Environments and progression

`DEMO → SHADOW → ASSISTED LIVE → BOUNDED AUTOMATION`. Promotion is per tenant/channel/supplier capability, not a global switch. DEMO has synthetic fixtures; SHADOW reads only; ASSISTED requires user approval for writes; BOUNDED allows only explicit policy bounds.

## Required scenarios

| ID | Scenario | Expected invariant |
|---|---|---|
| T01 | duplicate and out-of-order order delivery | one internal order; event history preserved |
| T02 | supplier stockout | temporary offer pause; no blind replacement |
| T03 | cost spike / negative margin | margin guard blocks <10%; incident emitted |
| T04 | multi-supplier split order | separate POs and shipments; line-level routing |
| T05 | delayed shipment/tracking | customer state and supplier state remain distinct |
| T06 | refund/compensation | approval and claim/settlement linkage; no duplicate refund |
| T07 | channel/supplier API timeout | unknown result reconciled before retry |
| T08 | webhook replay/signature failure | rejected or deduplicated; no mutation |
| T09 | worker/API restart | durable state resumes; outbox idempotent |
| T10 | key error or AI budget exhaustion | AI judgment stops; deterministic safety/order collection continues |
| T11 | emergency stop/recovery | scoped new writes stop; in-flight shipment/CS continue |
| T12 | cross-tenant access attempt | denied and audited |
| T13 | source round trip | required fields preserved; external IDs traceable |
| T14 | deletion/restore/PII purge | cooling-off restore works; legal records remain |

## Gates

| Gate | Required evidence | Pass condition |
|---|---|---|
| Discovery | one real Smart Store channel + one supplier sample; 100 products, orders/cancels, settlement | zero required-field loss, zero duplicate orders, all external IDs traceable |
| Shadow | read-only comparison against existing operation | freshness/order SLO met; differences explained; exception queue operable |
| Assisted | approved product/price/PO writes | idempotency errors 0; complete approval-to-execution audit |
| Bounded | limits by money/SKU/supplier/time/confidence | circuit breaker on anomaly/stale/error spike; recovery tested |
| LIVE readiness | security, restore, cost, legal, adapter, runbook evidence | all blocking decisions resolved; owner sign-off |

## Initial SLOs (measurement contracts)

These are implementation-start defaults; DEC-02 may revise them only with an accepted decision record.

| SLO | Measure |
|---|---|
| order ingestion | ≥99% of available external orders imported within configured polling window |
| idempotency | zero duplicate internal order/PO/refund side effects in replay tests |
| safety | 100% of detected <10% margin candidates blocked from execution |
| audit | 100% of side-effect commands have actor, policy, approval decision, idempotency, verification |
| alerting | urgent push attempted immediately; fallback attempted at +5 min |
| recovery | restart and restore tests meet DEC-02 RPO/RTO once set |
| cost | hard-cap behavior verified at KRW 30,000 envelope |
