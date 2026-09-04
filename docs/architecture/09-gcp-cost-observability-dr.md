# D-09. GCP cost envelope, observability, backup/DR

## Cost design

Google Cloud Seoul is preferred. Use request-based, scale-to-zero Cloud Run for API/worker, object storage, Secret Manager/KMS, scheduler/queue/outbox, FCM/email, and vendor-neutral OpenTelemetry. PostgreSQL remains the logical system of record, but its physical host is a deployment gate: do not assume always-on Cloud SQL fits the KRW 30,000 cap. Compare the current Cloud SQL trial/quote with a low-cost external serverless PostgreSQL provider and record the selection in DEC-08. Firestore may support notifications/read projections, but it must not replace the relational transaction ledger without a new ADR. Exact products and prices must be revalidated immediately before deployment; this is an envelope, not a price promise.

| Threshold | System behavior |
|---|---|
| target: KRW 3,000/month | optimize for scale-to-zero and low-volume MVP |
| warning: KRW 24,000/month | notify master; suspend non-essential growth |
| hard cap: KRW 30,000/month | stop non-essential image/video/research; retain orders, safety, approval, alerts |
| OpenAI | BYOK; tracked separately in tenant AI budget |

Cost ledger dimensions: tenant, provider, service, environment, category (`compute|db|storage|egress|notification|ai`), units, estimated/actual amount, period. A cap decision is made before starting optional work. If provider billing telemetry is delayed, use conservative estimates and disable optional work at the warning boundary.

## Observability contract

All requests/jobs/events carry `correlation_id`, `tenant_id` (masked in public logs), `aggregate_ref`, and `workflow_id`. Emit structured logs, metrics, and traces through OpenTelemetry-compatible interfaces.

Key metrics: order ingestion lag/duplicates, stock freshness, publish success/verification, PO deadline risk, unknown external writes, reconciliation exceptions, approval age/expiry, queue age, API error/rate-limit rate, stop state, AI tokens/cost, monthly cost envelope, backup age/restore result.

Alerts: urgent requirement categories (account suspension/security, mass stockout, negative margin, PO deadline, privacy risk, emergency stop) → FCM immediately → email and ChatGPT after five minutes if unacknowledged. Dedupe by incident key; acknowledgement is shared tenant-wide.

## Backup and DR

PostgreSQL: automated encrypted backups and point-in-time recovery; object storage: versioning/retention for raw snapshots, receipts, and registered media. Outbox and workflow state must be durable. Restore into an isolated environment, run integrity/hash-chain checks, replay outbox safely by idempotency key, then approve cutover.

RPO/RTO values are `TBD` until volume, business hours, and budget are decided (DEC-02). Before LIVE, the team must document numeric RPO/RTO, backup retention, regional recovery decision, restore owner, and last successful restore timestamp. A database backup without a tested restore is not DR evidence.
