# D-05. Adapter contracts

## Common adapter envelope

```json
{
  "contract_version": "1.0",
  "tenant_id": "uuid",
  "connection_id": "opaque-id",
  "correlation_id": "uuid",
  "idempotency_key": "string",
  "mode": "DEMO|SHADOW|LIVE",
  "occurred_at": "timestamp",
  "payload": {}
}
```

Every adapter declares `capabilities`, `rate_limit`, `freshness`, `retryable_errors`, `webhook_support`, and `external_id_rules`. Credentials are referenced by `secret_ref`, never embedded in payloads or logs.

## Channel adapter: Naver Smart Store / Coupang

```ts
interface ChannelAdapter {
  discover(ctx): Promise<CapabilityReport>;
  pullOrders(cursor): Page<ExternalOrder>;
  pullSettlements(period): SettlementPage;
  upsertOffer(cmd: PublishOffer): ExternalWriteResult;
  updateOfferStock(cmd: UpdateStock): ExternalWriteResult;
  updateOfferPrice(cmd: UpdatePrice): ExternalWriteResult;
  submitTracking(cmd: SubmitTracking): ExternalWriteResult;
  requestCancelOrRefund(cmd: ClaimCommand): ExternalWriteResult;
  verify(ref): ExternalRecord;
  reconcile(scope): ReconciliationReport;
}
```

The implementation must document the official API/file version, signature/replay rules, pagination/cursor behavior, rate-limit response, idempotency strategy, channel status mapping, and required channel fields before LIVE. Do not infer these from a generic marketplace contract.

## Supplier adapter

Supported kinds: `API`, `CSV`, `EXCEL`, `XML`, `MANUAL`. Read methods: `importCatalog`, `observeInventory`, `observeCost`, `pullOrderStatus`, `pullTracking`. Write methods: `submitPurchaseOrder`, `requestCancel`, `requestReturn`; manual adapter produces a human task and never pretends that a write succeeded. Each import stores immutable raw payload URI/hash and parsing report.

Supplier onboarding must verify business information and return address. Replacement candidates require exact identity match on barcode/model/brand/spec/color/config; otherwise return `AMBIGUOUS` and ask a question.

Supplier discovery returns 4–10 suppliers total diversified across the configured categories when available, with trust grade and warnings for non-blocking uncertainty. The system must not pad the list to meet a quota.

## Finance adapter

```ts
interface FinanceAdapter {
  importLedger(fileOrRows): ImportReport; // CSV/Excel/manual
  readBalances?(): ReadOnlyBalanceReport;
  executeTransfer(...): never; // unavailable by contract
  executePayment(...): never;  // unavailable by contract
}
```

Imported rows need source file hash, row reference, date, amount, currency, counterparty token, and mapping confidence. Reconciliation is deterministic and produces exceptions for human review.

## Notification adapter

`notify({tenant_id, incident_id, severity, channel, dedupe_key, body_ref})` supports FCM push first, then email and ChatGPT re-delivery five minutes later for unacknowledged urgent incidents. Acknowledgement is tenant-wide for that incident; delivery is at-least-once and deduplicated by `dedupe_key`.

## ChatGPT/MCP adapter

Expose the same read and typed command catalog as the PWA. Read tools may return masked data for lookup, product search, re-analysis, and emergency-stop requests. Side-effect tools require explicit approval or return an approval request; ChatGPT cannot approve payment or bypass mobile approval. Surface support and authentication must be revalidated against current provider documentation at implementation time.

## n8n adapter

n8n is optional for schedules, notifications, and external SaaS connections. It receives signed event envelopes and calls the public control API; it cannot write the ledger or own retries/state. If unavailable, native scheduler/outbox/notification paths remain functional. Commercial/customer distribution requires a separate fair-code/license review.

## Adapter acceptance checklist

Contract tests cover duplicate delivery, out-of-order events, timeout/unknown result, rate limiting, malformed payload, signature failure, restart, reconciliation, DEMO fixtures, and no-secret logging. An adapter is LIVE-ready only after a real API/file sample and external-ID round-trip pass the Discovery Gate.
