# P2-06 L4: settlement and ledger fixture mapping

Status: implementing. Sources: [Coupang revenue history](https://developers.coupang.com/ko/api/settlement/sales-detail-query),
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
