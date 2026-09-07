# P2-09 L4: production platform readiness, local evidence only

Status: implementing partial local slice. No production deployment/migration.

## Observed gaps

- Core persistence is SQLite; PostgreSQL repository implementation and full
  data migration are absent. Existing local Docker has `postgres:16` image;
  an isolated network-disabled temporary database can verify a minimal RLS
  contract without downloads or touching the commerce database.
- D-07 PWA shell is not implemented as a production app. Approval resources
  contain placeholder before/profit/risk values; mobile authentication,
  secure secret storage and complete operational UI remain unfinished.
- GCP/host selection, actual quote, RPO/RTO, key backend, identity service and
  production-complete dependency inventory require G3/G5. Do not invent costs.

## First bounded proof

Create a separate fixture SQL schema with composite tenant/order identities,
orders and outbox, enabled/forced RLS and non-superuser/non-bypass application
role. Missing tenant context sees no rows; tenant A cannot read/update/insert
tenant B data; same external ID is independent across tenants. Order/outbox
transaction rollback must leave neither committed. No production migration
runner uses this script.

Runner uses only the existing local postgres image, no network and no exposed
ports/host-data mounts. Its database uses an ephemeral memory filesystem. It
creates one uniquely named/labelled disposable container, reads
back its identity/label before cleanup and removes only that owned fixture
container. Never execute against an existing database or pull an image.

Source behavior: [PostgreSQL 16 row security](https://www.postgresql.org/docs/16/ddl-rowsecurity.html).
Owners/superusers can bypass ordinary RLS, so tests must use a non-bypass role;
FORCE ROW LEVEL SECURITY is included but is not a substitute for that role.
Tenant context must be set by the future authenticated service, not untrusted
client SQL. Full application roles, migrations, pool reset and API access remain
separate acceptance items, so P2-09 cannot be marked complete by this proof.

## Local evidence, 2026-09-07

`scripts/test-postgres-fixture.ps1` executed successfully with the already-cached
image `sha256:80f4c7a5e91618546dce5b4fe60cf03b14c0f9efa7e40157278d122772ced8d2`.
Server reported PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2). The SQL emitted
`OFFLINE_RLS_AND_ATOMICITY_PASS`: missing tenant denial, cross-tenant read/write
denial, composite tenant identities, order/outbox rollback, no TRUNCATE grant,
and transaction-local tenant-context reset passed as the non-bypass role.
The uniquely identified fixture container was stopped and auto-removed; its
memory-only synthetic data is intentionally unrecoverable. Operational data
was never mounted or changed. This is a focused SQL proof, not a repository
migration, application connection-pool test, backup recovery proof or PWA.

Verification: 165 Python tests passed; compileall and diff whitespace checks
passed; forbidden local-data filename and common secret-pattern scans found
no matches. The fixture runner was rerun after adding explicit post-cleanup
container-absence verification and passed again.

## Delegated approval resource correction

Review found B06 inbox/detail/decision wrappers require master administration,
so the existing FUNDS/CATALOG_CS domain approval permissions cannot be exercised
through the mobile service resources. Replace the blanket requirement with
current membership and approval-kind capabilities. Lists expose only authorized
approval kinds; auditor read access has no decision actions. Detail/decision
deny another role's kind before exposing payload. Keep transaction-local
membership validation and the authoritative core single-decider transition.
Also verify a direct expired decision commits expiry audit/outbox before raising;
an enclosing wrapper transaction currently rolls back that evidence.

Acceptance uses SQLite with master, funds and catalog users; approval by the
delegated account, wrong role/cross-tenant/revoked session denial, auditor read-only
actions, restart persistence and durable expiry. This fixes local service
wiring, not production login or PWA implementation.

Evidence: the three new regression scenarios failed before the fix (delegated
users/auditor blocked; direct expiry remained pending) and pass after it.
Fifteen targeted approval tests and 208 full Python tests passed. compileall,
diff whitespace, common secret-pattern and forbidden filename gates passed.
SQLite restart preserves each delegated decider and a valid audit hash chain.
Foreign approval IDs fail without exposing the resource. A-007 is closed for
the local service boundary; A-002 production identity/UI work stays open.

## Readiness evidence correction

The local readiness evaluator defaults five absent checks to true and coerces
arbitrary values through bool(), allowing missing evidence or a string such as
"false" to satisfy stage gates. Require explicit Boolean true for every check
and exact Boolean storage readiness. Missing/malformed evidence stays false;
the dependency chain blocks later stages. This remains a synthetic evaluator,
not a signature verifier or an authority to promote LIVE. Regression tests cover
each absent/false/invalid gate and explicit complete evidence.

Evidence: the prior implementation failed six regression assertions. Three
targeted tests and 220 full tests pass after the correction, plus compileall,
diff whitespace, common secret-pattern and forbidden filename checks. A-008
is closed locally; submitted booleans still require external evidence review.

## Operations dashboard text rendering

Dynamic task/status/error fields flow into HTML templates. The current esc
helper only converts to string, so a hostile status can create elements and
event attributes. Encode HTML metacharacters before all dynamic template
interpolation. Verify by executing the actual embedded rendering script in a
local Node sandbox, then parsing produced markup: hostile phase/task/blocker/
commit text must produce no injected image/SVG/event attribute. This is a
rendering regression test, not full-browser or production-auth evidence.

Evidence: the rendering regression produced injected image/SVG elements before
the fix and passes afterward. Three focused dashboard tests and 221 full Python
tests passed, with Node executing the real embedded script locally (not skipped).
compileall, diff whitespace, common secret-pattern and forbidden filename gates
passed. A-009 is closed for this operations dashboard template.
