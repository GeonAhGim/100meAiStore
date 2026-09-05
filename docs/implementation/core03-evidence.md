# CORE-03 local UNKNOWN reconciliation evidence

Specification baseline: `334238d`; actual implementation uses SQLite schema 7 and the explicit `DemoExecutionControlPlane` subclass.

Delivered: attempt/observation persistence in SQLite and memory; stable operation keys; version CAS and lease fencing; durable DISPATCHING before synthetic effect; expired in-flight lease recovery to UNKNOWN; read-only effect lookup; authoritative absence plus current gates required for retry; bounded unknown backoff/manual review. Each recorded result commits state, observation, audit and outbox atomically. Current master authority, original approval expiry/capability/digest and durable demo policy/target/stop state are checked before dispatch. Provider capability/version is checked against the connection manifest.

The fake provider stores synthetic effects in a separate SQLite ledger. Tests demonstrate effects survive caller process death; this does not certify real marketplace idempotency. Only the exact synthetic provider class is accepted; no network client or credentials are used.

| Acceptance | Tests/evidence |
|---|---|
| EX-01 | valid dispatch, single effect, terminal replay denied; explicit DEMO manifest required |
| EX-02 | timeout after durable effect resolves by lookup without another effect |
| EX-03 | actual subprocess os._exit before and after fake effect; restart recovers UNKNOWN |
| EX-04 | provider effect survives injected observation failure, lookup restores verified state |
| EX-05 | stale lease/token rejected, recovered UNKNOWN cannot dispatch |
| EX-06 | unproven absence leads manual; authoritative absence reuses same key; unavailable lookup bounded to manual |
| EX-07 | stop, policy change, expiry, revoked approver block; stopped authoritative absence does not retry |
| EX-08 | independent SQLite claim contenders have one winner; audit/outbox/observation rollback on both backends; duplicate callback rejected |
| EX-09 | tenant scoped attempt lookup hides foreign rows; arbitrary adapter objects rejected |
| EX-10 | v6-to-v7 preserves prior approval/preparation; full prior regression suite |

Tests include 17 execution scenarios plus 4 standalone synthetic-provider tests. Full verification: `python -B -m pytest -q -p no:cacheprovider` — **71 passed in 6.25s** on 2026-09-06. Diff whitespace check and changed-file secret-pattern scan passed. No LIVE behavior is claimed. The next development packet is ADAPTER-01, not a user approval wait.
