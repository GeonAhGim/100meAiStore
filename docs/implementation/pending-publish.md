# Publication evidence and pending local commits

Destination observed in git configuration:
`https://github.com/GeonAhGim/100meAiStore.git`, branch `main`.

Automatic approval review rejected a push of P2-01 because authorization for
the external GitHub destination/default-branch payload was not established.
Root confirmed no retries or alternate publishing paths. Keep local work moving.

2026-09-08: the user explicitly authorized verified small commits to origin/main.
Root restored GitHub CLI authentication and confirmed successful publication
through `ac44237`; the local origin/main reference matches. Historical entries
through that commit are now published. Later entries remain pending until a
successful push is recorded; unrelated uncommitted launcher changes are excluded.

| Commit | Local deliverable | Verification |
|---|---|---|
| 5cc5985 | Phase 2 official-source backlog and phase-aware dashboard | 121 tests |
| 2e84b2c | Offline channel request/error scaffold | 126 tests |
| 665a4d3 | Order snapshots and isolated fixture journal | 136 tests |
| 3622ad2 | Local CSV/manual supplier fixture contract | 144 tests |
| 6b3bbde | Synthetic signing and bounded retry plans; official-example issues open | 151 tests |
| 457e8d7 | Correct named-field mapping and safe malformed-kind rejection | 153 tests |
| 7543441 | Offline channel settlement/ledger mapping | 160 tests |
| 176437b | Privacy/license inventory and deletion-free review tests | 165 tests |
| 8a6b266 | Isolated PostgreSQL tenant RLS and atomicity proof | PostgreSQL 16.15 proof; 165 Python tests |
| 3c6b9a3 | Non-executable single-item Coupang tracking review | 172 Python tests |
| caed35e | Confirmed-order, dated, scoped dispatch review fixture | 178 Python tests |
| b0b1558 | Reject unrounded margins below 10% | 179 Python tests |
| 05b3cb9 | Supplier-bound price/quantity review and fixture readback | 186 Python tests |
| c9424ae | Separate Coupang claim-feed fixture and dispatch freshness binding | 194 Python tests; compileall/diff/pattern/filename checks |
| 45f4be3 | Bounded synthetic listing review and warning-aware creation receipt | 200 Python tests; compileall/diff/pattern/filename checks |
| ecc0129 | Single-receipt synthetic return review and exact-ID completion reconciliation | 205 Python tests; compileall/diff/pattern/filename checks |
| a5bff2e | A-007 delegated approval resources and durable direct-decision expiry | 208 Python tests; compileall/diff/pattern/filename checks |
| d111ce0 | Exact listing readback with separate draft/approved fixture observations | 210 Python tests; compileall/diff/pattern/filename checks |
| ed7be11 | Scoped Naver/Coupang tracking batch composition and correlation | 214 Python tests; compileall/diff/pattern/filename checks |
| 500acf4 | Two-item split fixture with deferred-item and remapped-shipment follow-up | 218 Python tests; compileall/diff/pattern/filename checks |
| a658043 | A-008 explicit Boolean readiness evidence, fail-closed storage/inputs | 220 Python tests; compileall/diff/pattern/filename checks |
| 23b79a6 | A-009 operations-dashboard HTML injection correction | 221 Python tests; embedded JS rendering; compileall/diff/pattern/filename checks |
| f87e369 | A-010 strict synthetic absence authority prevents malformed-config resend | 223 Python tests; durable UNKNOWN/manual-review regression; compileall/diff/pattern checks |
| ac44237 | A-011 linked purchase decision and submission revalidation | 228 Python tests; three-user/mobile/restart/atomicity regressions; compileall/diff/pattern checks |
| ac656ba | A-012 durable stop checks at dispatch, retry and PO submission | 231 Python tests; scope/resume/no-effect/readback regressions; compileall/diff/pattern checks |
| 8afcc0e | A-013 exact Boolean core decisions and bounded reasons | 232 Python tests; invalid-input no-mutation regression; compileall/diff/pattern checks |
| 93085d4 | A-003 bounded channel-schema fixture to durable DEMO flow | 236 Python tests; socket-denied two-channel approval/readback/settlement/restart flow; compileall/diff/pattern checks |
| ff37089 | A-003a durable exact channel-line identity and v18→v19 migration | 238 Python tests; two-channel/restart/legacy/duplicate regressions; compileall/diff/pattern checks |
| 7a49cc0 | A-005 explicit unsupported auth-source boundary | 239 Python tests; unresolved-vector/header fail-closed contract; compileall/diff/pattern checks |
| 7c0a861 | A-002a safe material approval preview | 240 Python tests; master/delegated/auditor redaction and restart; compileall/diff/pattern checks |
| 10d8ada | A-002a generic contact/credential redaction hardening | 240 Python tests; naming-variant regression; compileall/diff/pattern checks |
| 70fa067 | A-002b server sessions and one-use approval confirmation | 247 Python tests; HTTP/raw-ID/session/restart/replay tests; compileall/diff/pattern checks |
| fba6258 | A-014 exact approval-intent binding at tool gateway | 249 Python tests; mismatch/revocation/single-use/restart/cross-tenant tests; compileall/diff/pattern checks |
| e47cd3b | A-015 normalized nested secret-key rejection | 249 Python tests; spelling variants/no-write/opaque-ref regression; compileall/diff/pattern checks |
| f0cb0a5 | A-016 durable approved-tool synthetic consumer | 251 Python tests; success/restart/timeout-after-effect reconciliation; compileall/diff/pattern checks |
| 78fefa6 | A-004a exact listing review-to-DEMO execution bridge | 252 Python tests; pre-approval/change denial and one synthetic verified effect; compileall/diff/pattern checks |
| 439819d | A-017 single-receipt return review-to-DEMO execution bridge | 254 Python tests; refund approval binding, changed/cross-tenant denial and one synthetic verified effect; compileall/diff/pattern checks |
| ffef791 | A-018 single-item tracking review-to-DEMO execution bridge | 257 Python tests; Coupang/Naver typed shipment target, approval binding, changed/cross-tenant denial and one synthetic verified effect; compileall/diff/pattern checks |
| c57feb9 | A-019 split tracking review-to-DEMO execution bridge | 258 Python tests; two-item identity/remap review, PO-transition isolation and one synthetic verified effect; compileall/diff/pattern checks |
| Next local commit (pending) | A-020 clean-machine CLI bootstrap from empty cwd | 260 Python tests; subprocess init creates dry-run config and local SQLite state without secrets or external calls; compileall/diff/pattern checks |
| Next local commit (pending) | A-021 scoped listing exact-ID readback wrapper | 261 Python tests; tenant/connection/expiry scope plus draft/approved fixture readback with no resend authority; compileall/diff/pattern checks |

Later commits extend this list; only a successful authorized publishing step
marks them published.

A-012 was initially rejected by automatic approval review in the child audit
context; it was subsequently published from the authorized root context.
