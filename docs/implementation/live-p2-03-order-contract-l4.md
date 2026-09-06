# P2-03 L4: offline order snapshots and paging evidence

Status: offline scope completed 2026-09-06. No channel capability is registered.

## Source decisions

- Naver [official schema](https://apicenter.commerce.naver.com/docs/commerce-api/current/schemas/%EC%83%81%ED%92%88-%EC%A3%BC%EB%AC%B8-%EC%A0%95%EB%B3%B4-%EA%B5%AC%EC%A1%B0%EC%B2%B4)
  inspected via public HTML after the text browser timed out. Use current
  initial/remain quantity and payment fields, not legacy quantity aliases.
- Naver [detail envelope](https://apicenter.commerce.naver.com/llms/post-v1-pay-order-seller-product-orders-query.md)
  and [change feed](https://apicenter.commerce.naver.com/llms/get-v1-pay-order-seller-product-orders-last-changed-statuses.md)
  were read as public Markdown. Detail response data is an array of order and
  productOrder objects; requested product-order IDs must be accounted for.
  Continuation preserves both moreFrom and moreSequence, with count validation.
- Coupang [day page](https://developers.coupang.com/ko/api/shipments/po-list-query-paging-by-day)
  separates orderId, shipmentBoxId, sequenceNo and vendorItemId. Remaining
  quantity subtracts both cancellation and pending-refund quantities. Preserve
  shipping/unit/order/discount amounts separately; do not infer net profit.

## Contract

Pure parsers produce immutable, PII-minimized snapshots with source hashes and
external IDs. They do not convert into the existing simplified DEMO order
schema: that schema requires unit*quantity totals and cannot represent all
discount/partial-claim semantics safely. Every snapshot remains non-executable.
Known states stay vendor-specific. Unknown states, incomplete requested IDs,
negative/contradictory quantities, non-integral KRW amounts and malformed paging
are quarantinable errors with fixed diagnostic codes and no raw messages.
Claim-bearing Naver snapshots are marked for review; no claim status implies
permission to submit a PO. Non-KRW Coupang money is unsupported by this slice.

Bounded synthetic page journal: only canonical allowlisted snapshot data and
hashes; page identity/replay checks and versioned checkpoint update in one local
SQLite transaction. Replay cannot rewind the cursor. Restart and cross-scope
tests use temporary databases; this is not the production ledger or migration.

Acceptance: synthetic Naver partial claims/discounts/missing IDs, same-time
continuation, Coupang split shipments/cancellation counts/large numeric IDs,
malformed/unknown quarantine, PII exclusion, duplicate+restart+CAS rollback.
Full suite, compileall, diff check and pattern scan precede local commit. Keep
P2-03 in progress until those checks and evidence pass. Publishing stays blocked
by the recorded approval decision; do not attempt a push.

## Completion evidence

`channel_order_contracts.py` and `offline_contract_journal.py` implement the
bounded contract. Ten targeted tests pass; full suite 136 passed. compileall,
diff check and repository key-pattern/forbidden-filename checks pass. Fixture
payloads are authored synthetic values, not copies of vendor sample PII.

Nullable Coupang remote/discount amounts remain null, not zero. Shipment-level
charges are labeled as such on each line; consumers must not sum them per line.
Naver initial/remain money is retained independently of unit price. Legacy
quantity-only payloads fail closed. Unknown source fields remain represented
only by the source hash; this deliberately minimized snapshot is insufficient
for LIVE fulfillment, address delivery or complete financial reconciliation.

The journal persists page evidence/checkpoints only, not operational orders or
vendor credentials. Tests prove reopen/replay, same-time continuation, CAS
failure, atomic rollback and separate tenant scopes. It refuses unrelated
existing SQLite files. No migration of the commerce database occurred.

Next independent offline slice: P2-05 supplier CSV/manual contract. Remaining
real sample/roundtrip checks belong to P2-10 and remain approval-gated.
