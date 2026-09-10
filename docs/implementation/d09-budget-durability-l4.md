# D-09 local platform monthly budget durability

The local control plane enforces a shared KRW monthly budget across all tenants
using its durable SQLite ledger (or the equivalent in-memory test repository).
UTC calendar months define the accounting period. This is one database's total,
not a claim about measured cloud billing or coordination across separate hosts.

At a projected 24,000 KRW the state is WARNING and optional growth is suspended;
at a projected 30,000 KRW the state is HARD_CAP and optional work is blocked.
Essential order, inventory, approval, safety, alerts and outbox flows are outside
the optional agent budget gateway. Tenant policy cannot raise platform limits.
Unavailable or stale telemetry blocks optional requests conservatively.

Schema v22 appends a request ledger recording the canonical digest and run ID
for accepted and blocked requests. Exact replay returns the original run before
evaluating today's policy or month. Changed semantics conflict. Existing v21
accepted keys have no canonical digest and fail closed; blocked legacy requests
never stored a key and cannot be reconstructed. No down migration is provided;
recovery requires a compatible binary or a pre-upgrade backup.

Tests precede implementation: boundary and repository parity, strict replay,
restart, v21 upgrade and migration failure rollback, and two-connection race.
Run, request, reservation and audit writes share one transaction; SQLite uses
BEGIN IMMEDIATE and in-memory uses an RLock and rollback snapshots.

The public agent-run gateway now applies that contract before accepting an
optional run. A canonical digest covers every request field, including the work
class, so both accepted and blocked outcomes replay exactly and changed input
fails closed. The shared projection includes every tenant in the database:
ordinary optional work may reach WARNING, growth work stops at WARNING, and all
optional agent work stops before HARD_CAP. Missing, malformed, future-dated, or
older-than-60-second ledger telemetry records a durable blocked result without a
reservation. Essential deterministic order, inventory, approval, safety, alert,
outbox, and reconciliation paths do not use this optional agent-run entry point.

Local acceptance evidence: `test_d09_budget_gateway.py` covers 23,999/24,000/
29,999/30,000/30,001 boundaries, cross-tenant aggregation, growth suspension,
strict accepted/blocked restart replay, telemetry failure, fault rollback, and
two independent SQLite connections racing for the cap. The full local suite is
283 pytest and unittest tests at this checkpoint. Direct ledger writes and all
persisted policy integers also have identical bounded validation across the
memory and SQLite adapters. This remains DEMO/local evidence, not cloud
billing telemetry or a distributed multi-database cap.
