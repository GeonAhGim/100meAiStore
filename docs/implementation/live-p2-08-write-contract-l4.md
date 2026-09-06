# P2-08 L4: non-executable channel change contracts

Status: partial implementation; offer/stock/price/claims remain open. P2-04
source ambiguities still prevent a transport. This first
pure fixture slice depends only on P2-03 snapshots and never uses signing.

## 2026-09-07 official sources and scope

[Coupang invoice upload](https://developers.coupang.com/ko/api/shipments/uploading-waybills)
documents KR, INSTRUCT-only submission with order/shipment/vendor-item identities,
carrier, invoice and split flags. Results include aggregate and per-shipment
status; retryRequired is not approval to resend. The table calls data an array,
but the example uses an object, and code is documented numeric but shown as a
string. Accept those code representations only, use the demonstrated object
shape and quarantine other shapes. No request is sent to resolve ambiguity.

[Coupang shipment read](https://developers.coupang.com/ko/api/shipments/single-po-query-using-shipmentboxid)
requires receiver recheck after preparation and product-description consistency
before dispatch. Fixture evidence records an explicit review digest, not PII.
This cannot establish that a real address or product was verified.

Bound the first slice to a single-item, whole shipment, no split/direct delivery.
Match exact IDs to a normalized page; stop on claims, zero quantity, stale/future
observation or missing post-preparation review. Build immutable canonical fields
with synthetic approval binding to tenant/connection/source/time/payload digest.
The approval checker grants only fixture-review status, never G4 authority.
Timeout/unknown/partial/mismatched results require reconciliation, never resend.
Tests cover immutability, altered digest/context, expiry, stale source, ID
mismatch, cancellation, partial results, no-network execution and safe repr.

Other carriers, split-shipment identity changes, vendor duplicate-invoice rules,
full batch result correlation and authorized vendor samples remain unverified.
No full P2-08 completion or real shipment acceptance is claimed.

First-slice evidence: seven targeted tracking tests and 172 full Python tests
passed. compileall, diff whitespace, forbidden local-data filename and common
secret-pattern checks passed. Socket creation is denied in the fixture
roundtrip test. Synthetic approval has no operator identity/signature service;
its matching digest is deliberately review-only and cannot authorize G4.

## Naver single-product-order dispatch slice

[Official dispatch reference](https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-dispatch-product-orders-pay-order-seller)
sets a maximum batch of 30. The first fixture intentionally supports one only.
[Official Markdown schema](https://apicenter.commerce.naver.com/llms/post-v1-pay-order-seller-product-orders-dispatch.md)
supplies productOrderId, deliveryMethod, deliveryCompanyCode, trackingNumber,
dispatchDate, and separate success/failure collections. Only DELIVERY/CJGLS
fixture fields are supported, not every documented delivery variant.
[Order schema](https://apicenter.commerce.naver.com/docs/commerce-api/current/schemas/상품-주문-정보-구조체)
distinguishes PAYED from placeOrderStatus OK (confirmed). Require both, plus
placeOrderDate <= dispatchDate <= observation time. Retain no address/name.
Bind complete source hash, scoped IDs, carrier/invoice and times to review digest.
Missing/duplicate/unexpected success IDs or any failure entry require review;
never infer resend authority from HTTP 200 or the documentation's retry prose.

Naver slice verification: tracking suite 12 tests passed, full repository suite
178 tests passed; compileall/diff/common secret-pattern/filename checks passed.

## Price guard boundary correction

While preparing price/stock fixtures, a regression test reproduced existing
B04 behavior that approved 100000 selling price / 90001 cost: true margin is
below 10%, but display rounding produced 0.1000. Eligibility now compares
unrounded contribution to the 10% threshold; display values stay unchanged.
This is a local policy defect correction, not an accounting/tax determination.

Boundary regression and full suite passed (179 tests), along with compileall,
diff whitespace and pattern/filename secret gates.

## Coupang price/quantity fixture slice

[Price changes](https://developers.coupang.com/ko/api/products/changing-price-of-each-item-of-a-product)
require 10-KRW increments. Default change bounds are -50%/+100%; this scaffold
never enables forceSalePriceUpdate or automatic price adjustment options.
[Quantity changes](https://developers.coupang.com/ko/api/products/changing-quantity-of-each-product-item)
target vendorItemId after product approval. No actual product approval exists.
[Inventory read](https://developers.coupang.com/ko/api/products/query-quantitypricestatus-by-product-items)
returns sellerItemId, amountInStock, salePrice, onSale. The fixture checks exact
numeric ID against expected vendorItemId; the differing names remain an explicit
real-sample roundtrip check under G1, not proof of vendor entitlement.

Use one local CSV row and explicit synthetic identity-mapping digest. Reject
unready reports, non-KRW data, stale/future supplier or channel observations,
stopped listings, insufficient quantity after reservations/buffer, caller cap
violations, and margins below 10% using B04. Required fee/variable-cost inputs
are fixture assumptions, never actual channel cost promises. Each plan changes
only price or quantity, binds source hashes/context/expiry, and authorizes no
external write. SUCCESS alone requires a separate exact-ID/value readback;
unknown results never permit retry. Offer creation and claim contracts remain.

Price/quantity slice evidence: seven focused tests and 186 full tests passed;
compileall, diff whitespace, forbidden filenames and common secret-pattern scans
passed. Fixture CSV→cost guard→scoped review→matching readback runs with socket
creation forbidden. This remains separate from the production worker/outbox.

## Coupang separate claim-feed gate

[Official return/cancel feed](https://developers.coupang.com/ko/api/returns/return-cancellation-request-list-query)
requires checking release-stop requests before dispatch. Order-sheet cancellation
quantities alone are insufficient. Parse receipt/order/shipment/vendor-item IDs,
partial cancel/purchase counts, five published receipt states and preRefund as
an observation, not bank-refund proof. Preserve signed KRW returnShippingCharge;
do not retain requester names/addresses or free-text reasons. Unknown state,
duplicate claim line, inconsistent counts or currency quarantines the page.

Tracking review must additionally bind a fresh synthetic claim-page digest.
Any matching claim requires manual reconciliation before this narrow tracking
fixture qualifies. An empty fixture does not prove live feed-window coverage;
real pagination/window/receipt reconciliation remains G1 and integration work.

Claim slice evidence: official feed rechecked on 2026-09-07; six parser tests
and fourteen tracking tests cover signed charges, partial quantities, duplicate
and malformed quarantine, immutable/private observations, replay digests,
dispatch blocking and claim freshness. Claim observation time is bound into
the approval digest. Full suite: 194 tests passed; compileall, diff whitespace,
common secret-pattern and forbidden filename checks passed. No network is
available in the synthetic parsing/dispatch roundtrip tests. Claim execution,
offer creation, complete batch/split contracts and operational wiring remain
open; this slice is not P2-08 completion or real claim/refund verification.
