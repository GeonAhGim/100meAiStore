# B02 local DEMO catalog source/canonical/projection — L4 packet

Catalog fixtures are normalized through three durable layers: immutable source
snapshot, canonical product, and channel offer projection. Each layer retains
tenant ownership and source hash/lineage; no source payload is overwritten.

`ingest_demo_catalog(context, supplier_id, rows, idempotency_key)` accepts only
strict non-PII rows (`external_key`, `sku`, `title`, `category`, `price_minor`,
`currency`, `attributes`). Unknown fields, missing values, negative prices,
unsupported currency, and duplicate source keys are rejected before writes.
Identical source digest replay is idempotent; changed content under the same
key conflicts. `project_demo_offer` creates a local channel projection linked
to the canonical product and source lineage; it does not publish externally.

Acceptance evidence (local DEMO only): SQLite schema 14 stores immutable source
snapshots, canonical products, one-to-one source lineage, and channel offers;
the InMemory repository provides the same tenant-scoped contract. Import and
projection each emit an internal audit event and durable outbox intent only;
there is no adapter/network invocation. Tests cover replay, changed-content
conflict, strict rejection before writes, lineage preservation, offer replay,
and restart persistence.
