# B11 DEMO evidence package — Discovery → Shadow → Assisted → Bounded

This packet records readiness evidence without enabling LIVE execution. The
repository currently proves local tenant isolation, immutable normalized input,
catalog lineage, deterministic inventory/price guards, order/PO idempotency and
reconciliation, approval revalidation, claims/settlement separation, typed
gateway/budget stops, notification fallback, stop controls, and temporary
backup integrity.

## Stage gates

| Stage | Entry evidence | Allowed effect | Current status |
|---|---|---|---|
| Discovery | adapter manifests, source hashes, tenant/audit boundaries | read-only DEMO fixtures | evidenced by ADAPTER-01/B02 |
| Shadow | deterministic proposals, cost/freshness/threat checks, no vendor write | local projections and simulated outbox | evidenced by B04/B05/B08/B10 |
| Assisted | human approval intent, mobile evidence, expiry and submit re-check | local DEMO command checkpoint only | evidenced by B06/B08/B09 |
| Bounded | approved policy/cost/stop limits plus recovery/backup drills and explicit operator exit | not enabled by this repository | pending completion of B04/B05/B07 and DEC gates |

## Acceptance evidence

| ID | Acceptance criterion |
|---|---|
| B11-01 | Stage evaluator enforces fail-closed mode; requires explicit Boolean true evidence; missing, false or non-Boolean evidence blocks dependent stages |
| B11-02 | Storage readiness is verified as Boolean true before stages are permitted |
| B11-03 | Promotion requires external-contract review, legal/retention decisions, operator exit record; absence is safe hold |

Each stage exits only after its listed evidence is durable and replayable:
Discovery exits on normalized source/audit proof; Shadow exits on deterministic
proposal and no-write replay; Assisted exits on one-decider approval,
expiry, and submit-time revalidation; Bounded exits only after recovery drill,
cost/stop limits, contract/legal review, and an explicit operator exit record.
Rollback is the prior stage with all new writes held: stop controls remain
active, pending approvals expire safely, and backup restore targets a new
temporary path without deleting the source.

`evaluate_demo_readiness(repository, evidence=...)` is the automated evaluator.
It forces `mode=DEMO` and `live_authorized=false`, requires storage readiness and
the earlier checks, and keeps Bounded false until external-contract review and
operator-exit evidence are explicitly supplied. Passing a local boolean never
creates external authority.

2026-09-07 A-008 correction: every check now requires explicitly supplied
Boolean true; absent, false or non-Boolean evidence blocks dependent stages.
Storage readiness must also be Boolean true. These inputs report evidence only;
the evaluator cannot independently authenticate an operator or verify a report.

No stage authorizes channel, supplier, payment, refund, network, cloud, or
paid-service calls. Promotion requires fresh official-contract review, legal/
retention decisions, operational volume/cost evidence, and an explicit master
decision; absence of those inputs is a safe hold, not an inferred approval.
