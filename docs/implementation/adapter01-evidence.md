# ADAPTER-01 local DEMO ingestion evidence

Implementation is limited to local fixture reads. No HTTP client, marketplace
write, supplier, payment, credential, cloud, or model side effect exists in
this packet.

Migration 8 adds `normalized_inbound_payloads` and `adapter_poll_checkpoints`.
Normalized order JSON is strict schema v1, canonicalized and SHA-256 hashed
before storage; unknown fields, malformed lines, amount discrepancies,
unsupported currency, oversized payloads, and invalid cursors are rejected.
Payload rows are immutable through SQLite triggers. Inbox receipts reference
the immutable ref and use the normalized digest, while an optional fixture
`source_digest` remains a separate field.

`StoreControlPlane.poll_demo_connection` validates the DEMO ORDERS_READ and
INBOUND_EVENTS manifest before reading one bounded page. Fetching occurs
outside the database transaction; payload insert, inbox receipt/audit/outbox,
and versioned cursor advance occur in one transaction. A missing checkpoint
starts at version 0. Concurrent callers use a version CAS, and terminal empty
pages record `last_success_at` without losing the prior cursor.

| Acceptance | Evidence |
|---|---|
| AD-01 | two-page fixture persists two immutable payloads/receipts and advances cursor |
| AD-02 | overlap replay returns the same receipt ID and no additional event |
| AD-03 | unknown/malformed row rejects the complete page with no payload, receipt, or cursor |
| AD-04 | close/reopen replay preserves committed checkpoint and receipt identity |
| AD-05 | independent SQLite contenders produce one checkpoint winner |
| AD-06 | unsupported manifest fails before fixture read or payload writes |
| AD-07 | authenticated tenant payload lookup hides foreign refs as NotFound |
| AD-08 | typed transient read failure leaves storage unchanged; terminal empty page records success |
| AD-09 | out-of-order revisions remain immutable; modified event identity conflicts |
| AD-10 | migration readiness is schema 8; update/delete payload triggers reject mutation; prior suite remains green |

Verification: `python -B -m pytest -q -p no:cacheprovider` — 81 passed on
2026-09-06. `git diff --check` and repository secret-pattern scan passed.
