# CORE-01 durable inbox implementation evidence

Baseline: L4 notes commit `d1b2850`; implementation follows section 4 and IN-01 through IN-10.

## Delivered boundary

`StoreControlPlane.register_adapter_manifest`, `receive_inbound`, `get_inbox`, `inbox_for`, and `process_inbound` are local master-only APIs. SQLite and InMemory implement tenant-scoped receipt storage, manifest lookup, digest/schema deduplication, detached receipt reads, and version-checked processing.

Receipt creation, `inbox.received` audit, and `inbox.process_requested` outbox commit together. Processing changes the receipt with CAS and commits `inbound.accepted` plus audit together. Replays return `(receipt, True)` and create no additional audit or outbox rows. Cross-tenant and missing inbox IDs both return NotFoundError; no foreign record existence is disclosed.

PROCESSED means the receipt was accepted for downstream routing. It does **not** mean an order has executed or that payload contents were reconstructed. Only a digest and optional opaque reference are stored. A trusted normalized-payload store and domain router remain separate work.

## Local decision: InMemory atomic rollback

The former InMemory transaction was a no-op. It now uses an RLock and nested dictionary snapshots to roll back failures, so inbox/audit/outbox follow the same failure contract as SQLite. Unchanged existing objects retain their identity for prototype compatibility. Inbox reads return detached copies. This is a DEMO implementation with snapshot memory overhead, not a durable or production store. Existing SQLite migrations v1–v5 were not edited and no schema increment was needed.

## Acceptance evidence

`python -B -m pytest -q -p no:cacheprovider tests/store_core`: **33 passed** on 2026-09-05.

| IDs | Evidence |
|---|---|
| IN-01–03 | receipt/audit/outbox counts; restart replay; changed digest/schema rejected |
| IN-04 | same external identity in two tenants; foreign read/process indistinguishable from missing |
| IN-05 | missing manifest, unsupported capability/schema, URL/path raw references rejected |
| IN-06 | ordinary and revoked member rejected |
| IN-07 | stale CAS rejected; accepted once; processed replay adds nothing |
| IN-08 | audit and outbox exceptions roll back receipt and processing on both repositories; detached reads |
| IN-09 | 8 concurrent requests over independent SQLite connections produce one receipt and one acceptance |
| IN-10 | subprocess `os._exit` before/after receipt commit and processing commit; restart/replay verifies no partial or duplicated effect |

Existing core tests include migration, durable approval, lease/fencing, and recovery regressions and remain green. `git diff --check` passed. First full suite exposed an unrelated pre-existing dirty `tests/test_dev_dashboard.py` expectation (recent_messages expected 1, observed 2); that file is neither changed nor staged by this task. Therefore no full-repository green claim is made here.

The read-only command sandbox initially prevented temporary test databases. Re-running the same test command with automatic escalation succeeded. No external channels, orders, payments, credentials, cloud deployment, or paid services were called.
