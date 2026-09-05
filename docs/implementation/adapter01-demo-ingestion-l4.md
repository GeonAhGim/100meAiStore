# ADAPTER-01 read-only DEMO ingestion — next packet

Status: specified, not implemented. Resume after CORE-03 evidence/commit. This is the next authorized local packet, not an approval wait. No live Naver/Coupang credentials, network calls, publishing, supplier ordering or money movement.

## Purpose and boundary

Prove paginated read-only ingestion into durable inbox with a restart-safe cursor and immutable normalized DEMO payloads. Existing receipt digest/ref alone cannot reconstruct an order. Therefore this packet first adds a normalized payload store before asserting replayable domain input. Received payloads are staged fixtures, not completed sales orders or POs.

## Contracts

`DemoReadAdapter.describe()` returns provider, adapter_version, normalized_schema_version, capability=ORDERS_READ, mode=DEMO. `list_changes(cursor, overlap_from)` returns Page(items, next_cursor, has_more, observed_at). All fields are explicit; missing cursor is not silently guessed. Fixtures implement duplicates, reordered records, multiple pages, malformed schema, transient read error, stale version, and empty terminal page. A typed retryable read error may be retried with bounded backoff; no token/credential data exists.

NormalizedDemoOrder schema v1: external_order_id (opaque), event_id (opaque), revision (positive int), currency (allowlisted), total_minor (nonnegative int), lines (nonempty list of sku/quantity/unit_minor). Line quantity is positive integer. Strict JSON and size/item limits apply before storage; unknown keys are rejected in this version. No names, phone numbers, addresses or real customer data are used. Multi-currency conversion is not attempted. Amount discrepancy is a validation error, not auto-corrected.

## Storage

New migration after the current schema, preserving previous migrations. `normalized_inbound_payloads`: tenant_id, immutable_ref, canonical_digest, schema_version, payload_json, source_digest, created_at, composite primary key. Update/delete triggers prohibit mutation in this local slice; retention/lifecycle will be a separately authorized policy migration. `adapter_poll_checkpoints`: tenant_id, provider, connection_id, adapter_version, cursor, overlap_from, version, updated_at. Composite FK to the manifest. Cursor/overlap stored as typed bounded opaque values and timezone-aware timestamps.

Payload canonical_digest is the strict normalized JSON hash used by the inbox receipt. source_digest, when present in fixtures, remains distinct and is not misrepresented as the normalized digest. Payload references are opaque identifiers, never public paths or raw JSON. Access requires an authenticated tenant context; missing and foreign refs produce the same NotFound response.

## Service flow and atomicity

`poll_demo_connection(context, provider, connection_id, expected_checkpoint_version, adapter)` requires master and a matching DEMO ORDERS_READ/INBOUND_EVENTS manifest. Read the current checkpoint; call only the concrete fixture adapter outside the database transaction; validate the complete returned page.

Inside one UoW, reload checkpoint and compare its version. Insert immutable payloads by digest/ref, call receive_inbound for each event and then CAS-advance the cursor. Each inbox receipt already creates audit and process_requested outbox atomically. If any same event identity has conflicting content, the entire page rolls back and the prior cursor remains. Identical delivery is a replay. Never skip a conflicting row and advance past it silently.

The polling call persists at most one bounded page. A deterministic scheduler can enqueue the next page only after commit; startup enumerates checkpoints and pending inbox receipts without an LLM call. Checkpoint exhaustion has an explicit last-success time; lack of new data is not failure. Overlap windows request previously seen changes and rely on inbox identities for deduplication. Provider-specific cursor guarantees remain unverified until official channel contract work.

No domain transition to order ACCEPTED/ROUTING occurs here. Subsequent ORDER-01 loads the immutable payload, verifies digest/schema, and applies its own aggregate version rules. Out-of-order fixture revisions are preserved as input, not silently overwritten.

## Acceptance tests

| ID | Scenario | Required outcome |
|---|---|---|
| AD-01 | two valid pages | all payloads/receipts persist; cursor advances per page |
| AD-02 | duplicate/overlap page | stable receipt IDs; no extra inbox events |
| AD-03 | conflicting same event or malformed row | whole page and cursor rollback |
| AD-04 | crash after fetch/before commit, after commit/before ack | cursor+receipts all-or-none; restart replay |
| AD-05 | concurrent polls from same checkpoint | one CAS winner; loser cannot advance cursor |
| AD-06 | unsupported schema/version/capability | fail before payload writes |
| AD-07 | foreign tenant connection/payload ref | no disclosure or mutation |
| AD-08 | transient read failure/empty last page | bounded retry/no cursor loss; explicit completion |
| AD-09 | out-of-order revision and modified payload | immutable versions preserved; digest detects mutation |
| AD-10 | migration and full prior suite | inbox/approval/attempt history unchanged |

Done: schema/contract tests, subprocess recovery, evidence file, secret scan, small implementation commit. The next packet is catalog feed normalization and order-domain routing design, while all live integration remains gated.
