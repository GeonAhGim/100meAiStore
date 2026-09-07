# P2-13 L4: whole-product audit and credential-free minimum demo

Status: registered, not passed. Added 2026-09-07 after the user-directed final
audit requirement. This is additional work, not a reinterpretation of completed
DEMO evidence. Tracked denominator increases from 23 to 24, with the existing
17 completed packages unchanged. No 100% claim is allowed before this gate.

## Scope and evidence protocol

Read all requirements v1, architecture D-01–D-11/ADRs/DEC decisions, implementation
evidence and subsequent user decisions. Map each atomic requirement to actual
service, repository, API/UI, worker/outbox, recovery path and acceptance test.
An isolated function or passing unit test is not end-to-end implementation.
Record missing wiring, DI, migrations, unused modules and missing endpoints.

Use Security/Performance/Correctness/Maintainability review dimensions and
Unit/Integration/Contract/E2E/Infrastructure test layers. Check tenant/RBAC,
approval and revocation, secrets/PII, UNKNOWN/idempotency/restart/concurrency,
mobile UX, observability, installation, runbooks, cost ceilings and recovery.
Every gap needs severity, file/line or test evidence, owner, scope, acceptance
test and status. Fix only local/no-external-effect gaps with a bounded L4
packet; rerun regression/integration/E2E evidence and repeat the audit.

The final minimum demo uses fresh local state, three synthetic users,
mock SmartStore/Coupang/supplier/settlement flow and restart recovery. Prove
clean-environment bootstrap without secret inputs, external requests or paid
infrastructure. No operational database deletion or real PII is permitted.

## Initial known gaps (not a completed audit)

| Gap | Severity | Evidence | Owner | Acceptance evidence | State |
|---|---|---|---|---|---|
| A-001 production PostgreSQL repository absent | high | P2-09 L4; only SQLite core and isolated RLS SQL proof | platform | full repository parity, tenant roles, migrations, rollback/restart/pool reset integration tests | open |
| A-002 production PWA/auth/secret integration unfinished | high | P2-09 L4; approval resources use placeholder before/profit/risk | app/security | three-user browser login/revocation/approval, true proposal preview, no client secrets | open |
| A-003 contract fixtures not wired into operational flow | high | offline_* and channel_*_contracts are no-I/O fixture modules | connector/core | synthetic adapters through services→durable store→approval→worker→readback→settlement E2E | open |
| A-004 P2-08 coverage incomplete | high | write-contract L4: listing/claims and full split/batch coverage open | connector | bounded official-schema fixture plans and unknown/partial/replay tests | open |
| A-005 P2-04 source discrepancies unresolved | medium | auth-retry L4: public dummy bcrypt salt invalid; requested-by spelling differs | connector | authoritative resolution or explicit unsupported-case boundary; no guessed live auth | closed locally |
| A-006 real-account/legal/hosting decisions missing | high | DEC-01/02/04/06/07/08 and G1–G5 | product/legal/ops | dated scope-specific owner decisions plus authorized evidence, never fabricated by fixture tests | approval gate |
| A-007 delegated users cannot use mobile approval resources | high | reproduced master-only wrapper and rolled-back direct expiry; fixed approvals.py, see P2-09 L4 | app/security | 15 focused tests / 208 full tests, scoped three-user decisions, restart audit, wrong-role/cross-tenant/revocation and expiry | closed locally |
| A-008 readiness infers missing safety evidence | high | reproduced missing/false-string defaults; corrected readiness.py, P2-09 L4 | core/release | three focused tests / 220 full tests, missing/invalid checks and storage block dependent gates | closed locally |
| A-009 operations dashboard interpolates unescaped HTML | high | reproduced image/SVG injection; encoded dynamic template text in dashboard.py | app/security | real embedded JS rendering regression, three focused / 221 full tests | closed locally |
| A-010 malformed synthetic absence setting authorizes resend | high | constructor coerced string false to true in synthetic_provider.py | core/recovery | provider type-boundary and durable UNKNOWN/manual-review regression; 223 full tests | closed locally |
| A-011 mobile purchase decision leaves PO pending; submission misses revalidation | high | mobile decision and changed-target/expiry regressions in test_order_routing.py | core/orders | three-role mobile/direct decisions, restart, rollback injection, revoked/changed-target denial; 228 full tests | closed locally |
| A-012 durable stops do not reach dispatch or PO submission | high | three failing dispatch/absence-retry/PO regressions | core/safety | scoped denial, explicit resume, reconciliation with one existing effect; 231 full tests | closed locally |
| A-013 core approval interprets string false as consent | high | malformed direct decision regression | core/approval | exact Boolean and bounded reason; no invalid-input mutation; 232 full tests | closed locally |

This ledger is a seed for the exhaustive matrix, not a claim that there are only
six gaps. Approval-gated high risks remain visible; they must not be relabelled
low severity or treated as passed to manufacture a zero-gap report.

A-003 partial progress: `contract-demo-bridge-l4.md` now records Naver/Coupang
fixture parsing through durable DEMO ingestion, routing, delegated purchase
approval, supplier readback, settlement and restart. The 236-test suite passes.
The first bridge did not close production channel-line/write-adapter/worker
wiring gaps. A-003a now closes the durable channel-line identity sub-gap; live
transport and write-worker wiring remain open.

### A-003a bounded correction: durable channel-line identity

The bridge includes exact channel line IDs in its fixture digest but drops them
from the normalized payload and generated `order_lines`. After restart there is
therefore no durable way to correlate a core line with Naver `productOrderId` or
Coupang `vendorItemId`. Severity: high; owner: connector/core. Add an optional
`source_line_key` to normalized schema v1 and durable order lines so existing
generic DEMO fixtures remain compatible. Contract-backed fixtures must supply
it; duplicate keys within an order must fail closed. Acceptance: both channel
fixtures retain exact keys through poll, ingest and SQLite restart; malformed or
duplicate keys do not mutate durable state; the schema migration preserves old
rows with a null key. This does not implement a live transport or channel write.

Correction evidence: the regression failed before the new field existed. Both
channel fixtures now retain their exact source keys across poll, ingestion and
SQLite restart. A v18 database with an existing order line upgrades to v19 with
a null compatibility value. Duplicate or malformed source keys fail before any
durable write. Five bridge tests, eleven adapter-ingestion tests and the complete
238-test suite pass; compileall, diff whitespace and common secret-pattern scans
pass.

### A-005 bounded correction: explicit unsupported-source boundary

The two official-example discrepancies remain unresolved facts, but no typed
runtime boundary previously prevented later transport work from guessing past
them. Severity: medium; owner: connector. `offline_auth_source_boundary()` now
reports the Naver public bcrypt vector incompatible, leaves the disputed Coupang
header name absent, lists both stable reasons and always denies live transport.
Fixture evidence cannot mutate these fields. Acceptance: an offline contract
test checks every fail-closed field and confirms no guessed header is exposed.
Authoritative Discovery may replace this boundary later under G1; it does not
make P2-04 or LIVE complete.

### A-002a bounded correction: safe material approval preview

The mobile approval resource currently returns arbitrary command payload and
evidence values verbatim while `before`, profit and risk fields are placeholders.
An approval command containing an API key, authorization header or recipient
contact could therefore expose it to every authorized approval reader, including
the auditor role. Severity: high; owner: app/security. Build a display-only,
bounded recursive projection that redacts secret and direct-contact fields from
payload and evidence without changing the immutable approval intent. Populate
explicit before/after and profit fields only from supplied evidence, and add
risk badges when redaction occurred or profit evidence is missing. Acceptance:
master, delegated approver and auditor receive the same safe material preview;
raw sensitive values never occur in its representation; decisions still verify
the unchanged authoritative intent and survive restart. This is a service/UI
resource correction, not production login, MFA or a browser PWA.

Correction evidence: the regression exposed all four synthetic private values
before the change. The focused seven-test approval suite and complete 240-test
suite pass afterward; compileall, diff whitespace and common secret-pattern
scans pass. The delegated catalog decision verifies the unchanged intent and
the original authoritative payload survives SQLite restart.

### A-002b newly confirmed: unauthenticated browser identity and weak nonce

The served dashboard accepts `tenant_id` and `user_id` from URL parameters,
stores them in browser localStorage and reconstructs a context from membership
without credential/session proof. `confirmation_nonce` is caller-chosen,
format-checked and discarded rather than server-issued, session-bound and
single-use. Severity: high; owner: app/security. Until a server-side authenticated
session boundary exists, the UI is DEMO-only and must not be exposed as a
production PWA. Acceptance requires anonymous/raw-ID denial, secure cookie
sessions with tenant override rejection, three-role browser authorization,
revocation/rotation, short-lived approval-bound one-time nonces and browser
storage/cache inspection with no identity/session/BYOK material. State: open.

## A-010 bounded correction: explicit synthetic absence authority

Final-review continuation reproduced `authoritative_absence="false"` changing a
timed-out DEMO attempt from UNKNOWN to PREPARED after lookup, permitting resend.
The constructor currently coerces the value with bool(). Mutable assignment also
permits non-Boolean response fields. Severity: high; owner: core/recovery.
Only the literal Boolean True may enable the synthetic absence guarantee, both
at construction and later configuration. All other values must remain false.
Acceptance: invalid-value provider tests plus durable approval/dispatch/lookup
integration showing UNKNOWN -> MANUAL_REVIEW with zero effects and no claim;
the existing explicit-True, stable-key retry acceptance remains valid.
This corrects a DEMO configuration boundary, not any marketplace guarantee.
Both regressions failed before correction. All 223 tests passed after correction,
including existing authoritative retry and crash/restart coverage. compileall,
diff whitespace and common secret-pattern scans passed.

## A-011 bounded correction: purchase approval domain wiring

Four new integration tests reproduce mobile approval leaving the linked PO
pending, FUNDS users blocked by the PO wrapper, expired PO decisions rolling
back their expiry evidence, and changed-target PO submission being accepted.
Severity: high; owner: core/orders. Route linked purchase decisions through one
domain transition shared by generic/mobile/direct entry points, atomically
update approval/PO/audit/outbox, and require APPROVE_PURCHASE. Commit expiry
before reporting conflict. Before DEMO submission, use core execution preparation
to revalidate target version, current approver membership and immutable intent.
Acceptance includes three roles, restart, rejection, expiry and revoked/changed
target denial, with fault injection proving no half-committed approval/PO.
Five new integration tests pass; four original regression cases failed before
the fix. All 228 repository tests, compileall, diff whitespace and common secret
pattern checks passed. This closes local PO decision/submission wiring only;
production identity, supplier transport and contract adapter integration remain.

## A-012 bounded correction: durable stop at dispatch boundary

Three new regressions show B10 durable stops block the tool gateway but not
synthetic dispatch, PO submission or authoritative-absence retry. Severity:
high; owner: core/safety. Check tenant-owned global/tenant/current connection
stops when preparing/beginning an attempt and before PO submission (its channel
ID is the local DEMO connection scope). Recheck stops when absence might enable
retry. Read-only reconciliation remains allowed to preserve existing-effect
evidence. Unrelated connections/tenants are not stopped. Verify explicit resume,
zero effects on denial and restart persistence without changing operational data.
All 231 tests passed, including existing durable-stop backup/restart evidence;
compileall, diff whitespace and common secret-pattern checks passed.

## A-013 bounded correction: strict core decision input

Direct StoreControlPlane.decide accepted the string "false" as approval;
the mobile wrapper checked types but the authoritative core did not. Severity:
high; owner: core/approval. Require exact Boolean decisions and nonempty bounded
text reasons in the core transition. Invalid requests must leave approval,
command, audit and outbox unchanged. Cover all malformed Boolean/reason values
and explicit False rejection; existing True approval tests remain the positive
control. This applies to local service inputs and introduces no external action.
The regression failed before correction; all 232 tests passed after correction.
compileall, diff whitespace and common secret-pattern checks passed.

## Required final verdict

Answer fail closed: would adding only a real account/API key/business/channel
approval make this usable? If missing code, wiring, bootstrap, recovery or tests
remain, answer **no**, link the gaps and continue safe implementation. If only
external/legal/hosting decisions remain, list them exactly and do not activate
LIVE. A complete offline audit never substitutes for G1–G5 or real Discovery.
