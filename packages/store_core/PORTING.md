# PostgreSQL porting boundary

`InMemoryRepository` is a DEMO-only adapter. Domain decisions live in
`StoreControlPlane`; a production adapter replaces storage without changing those
rules.

The PostgreSQL implementation must:

- run command, approval, audit event and outbox writes in one transaction;
- require `tenant_id` in every repository method and SQL predicate;
- enable row-level security using a transaction-local tenant setting;
- enforce unique `(tenant_id, idempotency_key)` and one approval per command;
- lock the per-tenant audit-chain head before appending an event;
- use optimistic aggregate versions and reject stale updates;
- store timestamps as `timestamptz`, JSON as `jsonb`, and opaque IDs as UUID;
- expose append-only audit permissions to the application role;
- add an outbox before any external adapter can be enabled.

This slice intentionally has no HTTP, database, marketplace, payment, email, or
AI integration. It cannot perform a real purchase or publish a product.
