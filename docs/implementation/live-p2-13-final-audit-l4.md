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
| A-005 P2-04 source discrepancies unresolved | medium | auth-retry L4: public dummy bcrypt salt invalid; requested-by spelling differs | connector | authoritative resolution or explicit unsupported-case boundary; no guessed live auth | open |
| A-006 real-account/legal/hosting decisions missing | high | DEC-01/02/04/06/07/08 and G1–G5 | product/legal/ops | dated scope-specific owner decisions plus authorized evidence, never fabricated by fixture tests | approval gate |
| A-007 delegated users cannot use mobile approval resources | high | reproduced master-only wrapper and rolled-back direct expiry; fixed approvals.py, see P2-09 L4 | app/security | 15 focused tests / 208 full tests, scoped three-user decisions, restart audit, wrong-role/cross-tenant/revocation and expiry | closed locally |
| A-008 readiness infers missing safety evidence | high | reproduced missing/false-string defaults; corrected readiness.py, P2-09 L4 | core/release | three focused tests / 220 full tests, missing/invalid checks and storage block dependent gates | closed locally |
| A-009 operations dashboard interpolates unescaped HTML | high | dashboard.py esc only stringifies before innerHTML rendering of status/task/error data | app/security | hostile task/status values remain literal text with no injected elements/attributes | open |

This ledger is a seed for the exhaustive matrix, not a claim that there are only
six gaps. Approval-gated high risks remain visible; they must not be relabelled
low severity or treated as passed to manufacture a zero-gap report.

## Required final verdict

Answer fail closed: would adding only a real account/API key/business/channel
approval make this usable? If missing code, wiring, bootstrap, recovery or tests
remain, answer **no**, link the gaps and continue safe implementation. If only
external/legal/hosting decisions remain, list them exactly and do not activate
LIVE. A complete offline audit never substitutes for G1–G5 or real Discovery.
