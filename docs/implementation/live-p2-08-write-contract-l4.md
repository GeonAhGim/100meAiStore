# P2-08 L4: non-executable channel change contracts

Status: partial implementation; offer/stock/price/claims and Naver dispatch
remain open. P2-04 source ambiguities still prevent a transport. This first
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
