# Phase 1 foundation

## Scope

Phase 1 creates a locally testable foundation for tenant identity, membership,
authorization, approvals, commands, and append-only audit evidence. Existing
`smart_store_aios` code remains a prototype until its useful pieces are moved
behind the new boundaries.

## Target repository layout

```text
apps/                 deployable API, worker, and PWA entry points (later slices)
packages/             reusable domain/application/infrastructure packages
smart_store_aios/     legacy prototype, retained during migration
tests/                cross-package and legacy regression tests
docs/architecture/    normative contracts
docs/implementation/  implementation decisions and evidence
```

The first slice may use a single Python package and SQLite repository for fast,
dependency-light DEMO tests. Domain and application code must depend on a
repository protocol rather than SQLite details so PostgreSQL can replace it
without changing authorization or approval rules.

## Local workflow

Requirements: Python 3.10 or newer. No real credentials are needed.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -v
```

Copy `.env.example` only when local overrides are required. `.env*`, local
databases, credentials, and operational data stay untracked.

## CI baseline

GitHub Actions checks installation, Python compilation, all unit/integration
tests, and forbidden tracked secret/data filenames. CI has read-only repository
permissions and receives no secrets. Later phases add formatting, static typing,
dependency/SBOM, migration, and PostgreSQL service tests when the selected tools
are introduced.

## Service boundaries

- `control-api`: authenticates sessions, resolves tenant context, and shapes API
  responses; no direct vendor calls.
- `store-core`: owns deterministic policy and transaction state.
- `worker`: drains durable outbox/jobs and calls adapters; not a source of truth.
- `pwa`: mobile-first approvals and operations views; contains no secrets.
- `adapters`: translate untrusted vendor/file schemas into versioned contracts.

Only the domain/control-plane slice is implemented in Phase 1. Empty deployable
services are intentionally not scaffolded until a framework choice is justified.

## Exit criteria

- A user can belong to multiple isolated tenants.
- A tenant enforces one master and at most two other active members by default.
- Authorization is capability- and tenant-aware; stale membership sessions fail.
- Approval commands expire after 24 hours and cannot execute across tenants.
- Approval decisions and blocked attempts create verifiable audit evidence.
- Duplicate idempotency keys cannot create duplicate commands.
- Tests demonstrate restart-persistent behavior using a local database.
- No external write, real key, PII, or paid resource is used.
