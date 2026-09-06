# Phase 2: LIVE readiness backlog

Research date: 2026-09-06 (Asia/Seoul). Scope: public official documentation and
synthetic local verification. B01–B11 completed the **DEMO foundation**, not the
production product. This roadmap is distinct from the older local SQLite
`phase2-test-plan.md`. No channel account, credential, supplier contract, real
sample, legal decision, deployment, or LIVE capability has been verified.

## Official evidence register

| Ref | Official source | Verified fact and implementation consequence |
|---|---|---|
| N1 | [Introduction](https://apicenter.commerce.naver.com/docs/introduction) | API-center registration, application registration and permission acquisition precede use. Access is unverified here. |
| N2 | [Authentication](https://apicenter.commerce.naver.com/docs/auth) | OAuth2 client credentials; bearer requests; client ID + millisecond timestamp signed using bcrypt with client secret and Base64. Only HTTP 401 + GW.AUTHN is documented as the token-refresh fallback. No token is requested. |
| N3 | [Restrictions](https://apicenter.commerce.naver.com/docs/restriction) | TLS >=1.2; API-group permissions; variable per-API/application limits. 429 GW.RATE_LIMIT and GW.QUOTA_LIMIT differ. Observe published rate/quota headers; do not invent fixed vendor QPS. |
| N4 | [Changed orders](https://apicenter.commerce.naver.com/docs/commerce-api/current/seller-get-last-changed-status-pay-order-seller) | Observed version 2.87.0 (2026-09-01). GET last-changed-statuses; default 24h window, maximum 300 results; continuation uses moreFrom plus moreSequence. Timestamp-only checkpoints can lose same-time events. Dynamic field schemas were not fully exposed by text extraction; complete schema inspection remains P2-03. |
| N5 | [Settlement by case](https://apicenter.commerce.naver.com/docs/commerce-api/current/find-settle-by-case-pay-settle), [VAT category](https://apicenter.commerce.naver.com/docs/commerce-api/current/%EB%B6%80%EA%B0%80%EC%84%B8-%EB%82%B4%EC%97%AD) | Separate settlement, fee and VAT resources exist. Existence is not proof of seller entitlement, cash receipt, accounting treatment or tax filing readiness. |
| C1 | [Key issuance, updated 2026-09-02](https://developers.coupang.com/ko/getting-started/issue-open-api-keynew) | Business-verified WING seller required; self-development registration includes URL/IP. Guide explicitly says no separate test environment. Actual API access therefore needs user authorization even for a trial read; do not perform the guide's key deletion/reissue steps. |
| C2 | [Official test guide](https://developers.coupang.com/ko/getting-started/open-api-test-guide) | HMAC-SHA256 input concatenates signed timestamp, HTTP method, path and encoded query without '?'. Guide contains inconsistent requested-by header spelling; verify before transport implementation. No Postman requests are executed. |
| C3 | [Day-paged ordersheets](https://developers.coupang.com/ko/api/shipments/po-list-query-paging-by-day) | GET vendor ordersheets; date +09:00, up to 31 days, <=50 rows/page and opaque nextToken. Six documented order states; returned orders require a separate return/cancel feed. Shipping addresses can change before preparation, so re-read before dispatch. This KR-only scaffold does not implement TW. |
| C4 | [Sales detail](https://developers.coupang.com/ko/api/settlement/sales-detail-query) | Official settlement resource identified; financial field semantics and seller samples remain P2-06. Never treat a settlement response as transfer authority. |
| F1 | [RFC 4180](https://www.rfc-editor.org/info/rfc4180/), [W3C XML 1.0](https://www.w3.org/TR/xml/) | File syntax references only. They do not define any supplier's columns, commercial terms, availability or fulfillment guarantees. |
| L1 | [Personal Information Protection Act](https://law.go.kr/LSW/lsInfoP.do?lsiSeq=270351) | Official legal review starting point. Search exposed Article 26 outsourcing requirements and a later 2026-09-11 amendment; direct page rendered an older effective-date header. Applicable consolidated text as of research date is NOT resolved. Obtain dated legal review before real PII processing. |
| L2 | [E-commerce decree amendment](https://www.law.go.kr/lsInfoP.do?lsiSeq=288143&viewCls=lsRvsDocInfoR), [earlier Article 6 text](https://law.go.kr/LSW/lsInfoP.do?ancYnChk=0&chrClsCd=010202&efYd=20250214&lsiSeq=269055&urlMode=lsInfoP) | Earlier text gives differentiated transaction/complaint retention; a 2026 amendment exists. This research does not approve retention periods or automate deletion. Effective-date reconciliation, seller role and tax records need owner/legal review. |

Supplier identity is undecided. API onboarding must obtain official provider
documentation and a permitted masked sample; CSV/Excel/XML need a signed-off
column/type/encoding/unit map, business/return-address verification, inventory
freshness SLA, cancellation rules and purchase confirmation evidence. Manual
submission creates an outstanding human task, never a successful external PO.
Excel formulas/macros and XML external entities must not execute. Import size,
row limits and raw hashes are local safeguards, not vendor promises.

## Bounded work packages

Each row is one tracked deliverable, not an equal estimate of engineering hours.
Completion requires linked evidence and the applicable tests. Splitting a row
later must record the denominator change. Approval-gated rows remain incomplete.

| ID | Deliverable and acceptance criteria | Dependencies / sources | User gate |
|---|---|---|---|
| P2-01 | Official evidence register, unknowns, dependency backlog and separate DEMO/overall dashboard counts | B11; N1–N5, C1–C4, F1, L1–L2 | None for public research |
| P2-02 | Offline KR day-page request plan and Naver error decisions; synthetic encoding, boundary, malformed-input tests; no transport or credentials | P2-01; C3, N2–N3 | None |
| P2-03 | Naver/Coupang order fixture normalization: inspect complete current schemas; preserve line/external IDs, money and cancellation quantities; unknown states quarantine; replay/same-time paging and restart evidence | P2-02; N4, C3 | None with synthetic fixtures; real samples require G1 |
| P2-04 | Offline auth/cursor/retry harness: synthetic HMAC/bcrypt vectors, no-secret diagnostics, bounded retry decisions, 401/403/429/unknown results; resolve C2 header ambiguity | P2-02; N2–N4, C2 | Real key storage/rotation and token requests require G1 |
| P2-05 | Supplier file/manual import harness with versioned local schema, immutable source hash, row errors, duplicate/restart tests; manual task needs explicit human confirmation; XML/Excel limitations documented | P2-01; B02/B05, F1 | Supplier identity/contract and real files require G2; purchase remains G4 |
| P2-06 | Synthetic channel settlement/ledger mapping: fees/refunds/tax/currency/date and expected vs received separated; unmatched/duplicate/restart fixtures; no transfer/payment interface | P2-03; N5, C4, B07 | Real ledger and accounting classification require G2/G3 |
| P2-07 | Privacy/license release packet: data-flow inventory, processors, retention matrix with unresolved decisions, masked export and legal-hold/purge dry-run tests, SBOM/NOTICE | P2-01; L1–L2, ADR-011/012 | G3 to accept legal basis, retention, license; no real deletion |
| P2-08 | Offline offer/stock/price/tracking/claim contract plans with current required fields, exact-ID checks, approval digest, stale-data stop and unknown-result reconciliation; synthetic end-to-end tests | P2-03/P2-04/P2-05; B04–B06 | Real listing/change/refund/PO requires G4 |
| P2-09 | Production persistence/identity/PWA readiness: PostgreSQL isolation/migration plan, local tests where runtime exists, session/secret boundaries and mobile approval flows; deployment/restore/cost packet with unresolved quote and SLOs | P2-07; ADR-003/009/011, B06/B10 | Hosting, paid runtime and infrastructure changes require G5 |
| P2-10 | Authorized Discovery: one selected channel/supplier; required sample coverage, external-ID roundtrip, permission/field-loss report; masked evidence only | P2-03–P2-07; D-10 Discovery | G1/G2/G3 required; no external writes |
| P2-11 | Authorized Shadow: read-only operational comparison, bounded polling, measured freshness, discrepancies and operator exceptions; agreed exit/rollback evidence | P2-09/P2-10; D-10 Shadow | G1/G3/G5 for actual environment |
| P2-12 | Assisted/Bounded release: scope-specific operator approval, monetary/SKU/time caps, audit and stop/restore verification; sign-off required per capability | P2-08/P2-11; D-10 Assisted/Bounded | G4/G5; bounded policy separately approved |

## Approval packets (prepare before requesting approval)

- G1 channel access: selected seller/channel, documented API groups, exact read
  endpoints and interval, IP/hosting, data fields, expiry and masked evidence
  location. Keys enter a secret backend only, never chat/repo. Scope limited to
  approved reads; no registration or credential creation by this task.
- G2 supplier/finance: chosen business/contract, authorized sample field map,
  data rights, return/stock/cost terms and manual confirmation responsibilities.
- G3 legal/product: accountable owner verifies dated law, controller/processor
  roles, lawful purpose, retention/legal hold, cross-border flows and license.
  An unresolved policy fails closed; no legal conclusion is inferred from tests.
- G4 writes: exact channel/supplier capability, proposed payload digest,
  money/SKU/count/time limits, approval expiry, verification and compensation.
  No transfer/payment is permitted by the finance contract.
- G5 operations: selected deployment, quoted monthly cost, RPO/RTO, identities,
  restore evidence and rollback; user explicitly approves paid/external changes.

Gates constrain dependent operations only. Continue independent offline work
while account/business choices remain unresolved. Do not ask for blanket LIVE
approval, fabricate access, or treat a completed fixture test as channel readiness.

## Progress semantics

Manifest schema 2 groups B01–B11 as `demo_foundation` and P2-01–P2-12 as
`live_readiness`. Overall = completed tracked packages / 23; DEMO = 11/11.
This is backlog completion, not operational availability, elapsed effort or
certification. All real channel availability remains unverified and LIVE is
not authorized. Existing DEMO readiness evaluator remains unchanged.

P2-01 verification (2026-09-06): full suite 121 passed; compileall and diff check
passed. Repository key-pattern scan found no matches; tracked-file check found
only the permitted `.env.example`. Dashboard collector reports 12/23 (52%),
DEMO 11/11 (100%) and Phase 2 1/12 (8%) before P2-02 completion. The phase test
proves blocked/pending release work does not inherit DEMO's completion.
