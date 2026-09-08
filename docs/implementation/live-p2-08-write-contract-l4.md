# P2-08 L4: non-executable channel change contracts

Status: partial implementation; bounded offer/stock/price/tracking/return and
batch/two-item split fixtures exist. Broader variants and operational connector
wiring remain open. P2-04 source ambiguities still prevent a transport. These
pure fixtures depend on P2-03 snapshots and never use signing.

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

## Single-item listing fixture plan

[Coupang creation](https://developers.coupang.com/ko/api/products/product-creation)
requires shipping/return location, item, content and category fields. Bound the
local slice to one new domestic item, ordinary/free shipping, explicit category,
no automatic approval or pricing, and supplied synthetic category requirements.
Use documented numeric/Boolean types; conflicting example strings are rejected.
Validate required attributes/notices against the fixture metadata, bind full
payload/supplier/metadata/rights digests, enforce fresh stock/cost and 10% margin,
and reject extra unsupported fields. Keep contact/address/content only in the
caller input; the resulting immutable review retains digests and scoped IDs.
SUCCESS with warning details or item errors requires reconciliation; even a
clean creation receipt proves neither vendor approval nor sale availability.
No URLs are fetched and no request/credential/transport is created. Category
entitlement, content rights, image validity, vendor address ownership and actual
approval are unverified; synthetic evidence has no G1–G4 authority.

Listing slice evidence: six targeted tests and 200 full Python tests passed;
compileall, diff whitespace, common secret-pattern and forbidden filename gates
passed. A socket-denied synthetic roundtrip validates review binding and treats
creation ID as pending readback. This is a deliberately restricted fixture:
the reserved `.invalid` image URL and synthetic category requirements cannot be
used as vendor-compliant listing inputs. Product readback and batch/split/claim
execution contracts remain open, as does application/worker wiring.

## Single-receipt return-review fixture

[Official return approval reference](https://developers.coupang.com/en/api/returns/approve-request-for-return)
requires vendor/receipt identity and matching total quantity after warehouse
confirmation. Duplicate/pre-refunded/completed cases require status review.
Bound this fixture to one receipt line, RETURN, warehouse-confirmed, no observed
pre-refund, fresh claim source and explicit synthetic withdrawal-review digest.
Bind tenant, channel, vendor, exact receipt/order/item IDs, quantity, synthetic
amount/cap and all evidence/time fields into review. Response 200 alone requires
exact-ID completed-state fixture readback, not proof of bank refund. Unknown,
partial, changed-count or foreign readback requires reconciliation with no resend.
The current reference lists v4 while the official internationalization notice
lists v5; no transport/method/version is inferred or executed by this fixture.

Return-review evidence: five focused tests and 205 full tests passed;
compileall, diff whitespace, common secret-pattern and forbidden filename
checks passed. Socket-denied roundtrip verifies synthetic review and exact-ID
completed observation while all real-write/refund/resend flags remain false.
Batch receipts, raw withdrawal feed normalization and production approval/
worker integration remain open. Review digest is not an operator signature.

## Listing exact-ID readback

[Official product query](https://developers.coupang.com/en/api/products/querying-product)
returns sellerProductId, vendorId, statusName, items and vendorItemId; the latter
is null for a saved draft and assigned after approval. Require the original
reviewed payload digest, a clean creation ID, fresh readback and exact vendor,
SKU, count and submitted-field matches. Draft and approved observations are
distinct; partial/unknown/denied status, automatic pricing, extra options or
changed payload require reconciliation. Returned IDs remain fixture observations
and cannot activate LIVE or authorize a price/stock write.

Readback evidence: eight listing tests and 210 full tests passed; compileall,
diff whitespace and common secret-pattern checks passed. Both draft and approved
synthetic states are exercised without sockets; vendor-generated ID values are
preserved exactly, while mismatched field/type/option/status/time requires review.

## Tracking batch correlation

Compose existing checked single-item reviews into one immutable scoped batch.
Naver allows at most 30 product-order IDs; the Coupang fixture uses a local cap
of 30 distinct shipment IDs, without claiming this is the vendor maximum.
Require one provider/tenant/connection, nonduplicate exact result identities and
all component reviews unexpired. Batch review digest includes every component.
Naver unordered success coverage must equal the requested set with no failures;
Coupang aggregate and each shipment result must all be successful. Missing,
duplicate, foreign, partial or retry-marked results require whole-batch
reconciliation; no batch or sub-item receives automatic resend authority.

Batch evidence: four focused tests and 214 full tests passed; compileall, diff
whitespace, common secret-pattern and forbidden filename gates passed. The
30-item Naver and two-shipment Coupang fixture results run without sockets.

## Tracking batch to durable DEMO execution wiring

The bounded, same-provider tracking batch now composes into one immutable
`dispatch_shipment` PURCHASE approval.  Its payload binds the batch digest,
component review digests and exact opaque tracking identities; the synthetic
target is derived from the provider and batch digest.  After an authorized
decision, exactly that projection enters the existing fenced local outbox and
durable synthetic worker as one DEMO effect.  A pending approval, changed
component/batch, or cross-tenant submit is blocked and emits no executable
outbox item.

This does not submit a batch to Coupang or Naver, confirm any shipment, or
grant resend authority.  Result correlation remains the pure offline contract;
real transport, credential use and vendor readback remain gated.  Focused
gateway tests and the 265-test local Python suite passed on 2026-09-08.

## Two-item split shipment fixture

[Official split example](https://developers.coupang.com/en/faq/i-want-split-a-shipment)
first sends both vendor items: one invoice and one estimated date, with
splitShipping true and preSplitShipped false. The shipped item's shipment ID
changes; the deferred item retains its original ID. Later submission uses the
deferred item's invoice with preSplitShipped true. Bound this fixture to exactly
two items with no partial quantity, claims or direct delivery; require complete,
fresh order/claim observations and post-preparation evidence. Follow-up needs
fresh exact-order/item/quantity observations proving the first item moved to a
different shipment and dispatch state while the deferred item remains prepared.
No aggregate OK response substitutes for this mapping. Every step remains
non-executable; real invoice tracking, delivery and legal authority are unproven.

Split evidence: four focused tests and 218 full tests passed; compileall, diff
whitespace, common secret-pattern and forbidden filename gates passed. The
socket-denied two-step roundtrip includes identity remapping, deferred date,
claim/freshness denial, wrong/missing mapping, altered review and expiry checks.
More-than-two item splits and vendor response-to-invoice readback remain
unsupported and must not be inferred from this bounded fixture.

## Listing review to durable DEMO execution wiring

The single-item Coupang listing fixture now has an explicit application bridge.
It revalidates fixture scope and expiry, projects only the immutable review
digest plus SKU/price/quantity into a PRODUCT approval, and submits the exact
same payload as `publish_offer`. Pending approval and any changed review are
blocked by the gateway. An accepted command flows through the fenced outbox and
durable synthetic worker added in A-016, with one verified local effect.

This is not a Coupang write adapter: the worker accepts only the exact synthetic
provider type and no listing HTTP request, credential, marketplace ID or sale
state is produced. The focused acceptance test denies implicit pre-approval,
rejects a changed quantity/review digest and proves the exact reviewed command
finishes locally. Other P2-08 change types and real transport remain open.

## Tracking review to durable DEMO execution wiring

The two existing single-item tracking review contracts now have an explicit
application bridge. Coupang shipment and Naver product-order identities are
projected into a typed `dispatch_shipment` PURCHASE approval and the exact
review payload is consumed by the fenced local synthetic worker. Pending,
changed and cross-tenant reviews fail closed with no executable outbox item.
The bridge does not send an invoice or prove shipment confirmation; batch and
split tracking, vendor readback and all real transport remain unverified.

The bounded initial two-item split review also composes into the same
`dispatch_shipment` DEMO command path. Its exact item/shipment/invoice mapping
is approval-bound and reaches one fenced synthetic effect; the shipment
`order_ref` is intentionally distinct from the linked-PO payload field. This
does not authorize a real split submission or vendor readback.

The listing application bridge also exposes the existing exact-ID creation
readback contract under the authenticated tenant/connection scope. It returns
only fixture reconciliation status and never turns a vendor ID into listing
availability or resend authority; real vendor readback remains unverified.
