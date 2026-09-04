# Local restart and recovery operations

This document prepares unattended local DEMO recovery without changing the
operator's Windows or Docker Desktop settings.

## Durable state rule

The SQLite file must be stored on an explicit host path or Docker volume. Never
store it only in a disposable container layer. Tenant state, memberships,
commands, approvals, audit events, outbox leases, fencing tokens, attempts, and
workflow checkpoints are authoritative only after their database transaction
commits.

After a process restart, a worker opens the same database, verifies the schema
version and database integrity, and claims only pending work or leases that have
expired. A completed checkpoint is never inferred from memory. If an external
result is unknown, the worker reconciles using the original idempotency key
before another simulated write.

## Future Docker Compose requirements

No long-running service image exists in Phase 2, so Compose is intentionally not
added yet. When API and worker entry points exist, their Compose definitions
must include:

- an explicit persistent database volume;
- `restart: unless-stopped` or a justified bounded equivalent;
- a healthcheck that validates expected migration version, `PRAGMA
  integrity_check`, and worker readiness;
- health-based dependency conditions rather than startup order alone;
- a shutdown grace period that stops new claims and never marks unfinished work
  complete.

The service must remain unhealthy and stop dispatch if the database is corrupt
or newer than the supported migration version. It must not silently replace the
database with an empty file.

## Docker Desktop startup boundary

Starting Docker Desktop automatically when the user signs in is an operator
choice. Codex, application code, installers, and tests must not change Windows
startup apps, registry keys, Task Scheduler, services, or Docker Desktop
preferences.

If the operator wants unattended recovery, they can enable Docker Desktop's
login-start option manually and verify it after a planned reboot. The future
runbook will record pre/post database digest, migration version, outbox counts,
checkpoint versions, and audit-chain verification. Automation must never reboot
the PC.
