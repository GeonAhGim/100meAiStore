# Phase 1 slice status

Implemented and tested:

- explicit tenant context and cross-tenant denial;
- one master plus two active members;
- role capability checks and membership-version session invalidation;
- 24-hour approvals, authorization, expiry and rejection;
- idempotent command creation and collision rejection;
- material-change supersession with mandatory reapproval;
- immutable-view, per-tenant SHA-256 hash-chained audit events.

Not yet a production durability claim:

- `InMemoryRepository` loses state at process exit. It is deliberately marked
  DEMO-only and cannot satisfy the restart recovery release gate.
- The next storage slice must implement the contract in `PORTING.md` with
  PostgreSQL (or a temporary SQLite adapter), then prove restart recovery of
  memberships, pending approvals, idempotency keys and audit-chain heads.
- There is no external API, payment, marketplace write, notification or AI tool
  execution in this package.
