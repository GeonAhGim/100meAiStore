# P2-06 L4: settlement and ledger fixture mapping

Status: offline scope completed 2026-09-06. Sources: [Coupang revenue history](https://developers.coupang.com/ko/api/settlement/sales-detail-query),
[Coupang empty items clarification](https://developers.coupang.com/ko/faq/when-querying-the-sales-detail-query-api-the-items-details-of-settlement-ammount),
[Naver settlement by case](https://apicenter.commerce.naver.com/docs/commerce-api/current/find-settle-by-case-pay-settle).

Coupang revenue rows are SALE/REFUND and have recognition vs scheduled settlement
dates; item service fees/VAT and delivery-fee settlement are distinct. The
official FAQ states some pre-confirmation cancelled/returned items are omitted,
so an empty item array cannot prove zero sales or complete reconciliation.
Neither revenue history nor scheduled settlement proves actual bank receipt.

Plan: keep amounts and their basis explicit, preserve raw source hashes and
stable vendorItemId (not mutable productId), detect repeated/differing rows and
unmatched ledger evidence. No payment/transfer method. Naver's deeper settlement
schema must be verified before mapping fields; do not invent those fields.

Inspection found the existing DEMO settlement importer unpacked `row.values()`
despite accepting arbitrary Mapping order. First safe fix: read fields by name
and prove reordered JSON produces an identical batch/replay. Unknown non-string
kind/currency must fail with the normal safe conflict, not a TypeError. Keep the
DEMO financial semantics otherwise unchanged. Then continue the channel-specific
fixture mapping; P2-06 stays in progress until the full scoped evidence passes.

First fix evidence: both key-order and unhashable-kind regressions failed before
the patch. Explicit field lookup and kind-type validation now pass the full
153-test suite; compileall/diff/secret-pattern/forbidden-filename checks pass.
Reordered input produces the same canonical batch and idempotent replay.

## Channel mapping evidence

The [official Naver Markdown schema](https://apicenter.commerce.naver.com/llms/get-v1-pay-settle-settle-case.md)
was read to confirm element fields, target/settlement enums and pagination.
`channel_finance_contracts.py` now preserves Naver signed settlement/benefit/fee
amounts and distinct dates; Coupang delivery and vendor-item observations retain
fees/VAT/discounts separately. Numeric values must be integral KRW in this local
profile; unsupported fractional values are rejected rather than rounded.

Only allowlisted values and source hashes persist, with no buyer names/bank
details. An explicit synthetic ledger source-reference match can be missing,
matched or amount-mismatched; it never sets cash_receipt_verified. Unmatched,
duplicate and wrong-currency ledger rows fail closed. No source sign is reversed
based on a guessed refund convention, and no tax-reporting conclusion is made.

Seven new tests plus ten existing order-contract tests passed; full suite
160 passed. compileall/diff/secret-pattern/filename checks passed. Journal
reopen/replay/content-conflict tests use separate `*_settlement` scopes, proving
settlement pages cannot advance the corresponding order checkpoint.

Limits: observations are not complete accounting books; real sample coverage,
provider row identity across changed snapshots and financial classification
still require G2/G3/P2-10. Naver response pagination metadata is preserved without
assuming its indexing matches request pageNumber; an operational importer must
verify that roundtrip. Existing journal v1 files remain usable; no commerce DB
migration. Next safe slice: P2-07 privacy/license review packet and dry-run gates.
