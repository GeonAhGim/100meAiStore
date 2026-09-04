# Phase 2 durable SQLite UoW/outbox test plan

## 1. Purpose and scope

Phase 2 proves the local DEMO persistence boundary before any marketplace,
supplier, payment, email, or AI write is enabled. The subject is a durable
SQLite Unit of Work (UoW), transactional outbox, worker lease/fencing, retry,
restart recovery, tenant isolation, and audit integrity.

This plan is an independent acceptance contract. It does not promote SQLite to
the production ledger: PostgreSQL plus row-level security remains the LIVE
target. Tests use synthetic data, deterministic clocks, controlled failpoints,
temporary database files, and fake adapters only. Claude and external services
are out of scope.

## 2. Required storage and worker contract

The Phase 2 implementation must expose behavior equivalent to the following,
without requiring these exact names:

- `unit_of_work(tenant_context)`: one explicit transaction that commits or
  rolls back domain projection, command/approval transition, domain event,
  outbox row, and audit event together.
- `enqueue(...)`: inserts an outbox item in the same transaction as the business
  transition; standalone enqueue must still be transactional.
- `claim(worker_id, now, lease_duration)`: atomically changes one eligible item
  to `running`, increments `attempts` and a monotonic `fencing_token`, and
  returns the committed token.
- `complete(job_id, worker_id, fencing_token, result_digest)`: succeeds only for
  the current owner/token while the item is `running`; completion and its audit
  evidence commit atomically.
- `fail(job_id, worker_id, fencing_token, retry_policy, sanitized_error)`: uses
  the same ownership check and atomically schedules a retry or moves the item to
  `dead`.
- `recover(now)`: no destructive reset. Expired `running` items become
  claimable through the normal claim predicate; committed terminal items never
  re-enter the queue.

Outbox payloads contain `tenant_id`, correlation/command identifiers, operation,
aggregate reference and logical version, deterministic idempotency key, schema
version, and a payload digest. They contain no plaintext credentials and no
unnecessary PII. All persisted timestamps are UTC. Time comparisons use an
injected clock in tests.

## 3. Non-negotiable invariants

### Transaction and durability invariants

1. A committed business transition has exactly one matching event and outbox
   item for its deterministic idempotency key; a rolled-back transition has
   none of them.
2. A process crash can leave either the complete pre-transaction state or the
   complete post-transaction state, never a partially visible combination.
3. A successful API return occurs only after SQLite commit. A failed/ambiguous
   commit is treated as unknown and reconciled by idempotency lookup, not blindly
   repeated with a new key.
4. Reopening the database, restarting the worker, restarting containers, or
   rebooting the PC cannot erase committed commands, approvals, checkpoints,
   attempts, outbox states, or audit evidence.
5. Every schema migration is atomic, versioned, repeatable, and fails closed on
   an unsupported newer schema.

### Concurrency, lease, and delivery invariants

6. At most one unexpired lease is current for an outbox item. Concurrent claims
   return one winner and zero or more clean losers; no database-lock exception is
   exposed as a successful claim.
7. Every successful re-claim increments a monotonic fencing token. Worker ID
   alone is not a fence.
8. An expired or superseded worker cannot complete, fail, extend, or otherwise
   mutate the item. A stale operation is rejected and audited/metriced without
   changing the current lease or terminal state.
9. Lease expiry makes unfinished work eligible again; it does not imply that an
   external side effect did not occur. Unknown external results go to
   verification/reconciliation before another write.
10. Delivery is at-least-once internally and effectively-once at the business
    boundary through a stable idempotency key and adapter verification. The
    system does not claim exactly-once network delivery.
11. Retry count, backoff, jitter bounds, maximum attempts, and dead-letter
    transition are deterministic under an injected policy. Terminal `done` and
    `dead` items cannot be claimed.

### Security and evidence invariants

12. Every tenant-owned row and every repository operation requires an explicit
    tenant context. Missing context fails closed. Tenant A cannot read, claim,
    complete, fail, enumerate, or infer Tenant B's item—even with a valid B job
    ID, idempotency key, or fencing token.
13. Uniqueness is tenant-scoped where business identity is tenant-scoped, for
    example `(tenant_id, idempotency_key)`. The same external key may exist in
    two tenants without collision.
14. Audit is append-only and hash-chained per tenant. Concurrent appends serialize
    the tenant chain head; rollback leaves neither an orphan audit row nor a
    moved chain head. Chain verification still passes after restart.
15. Errors, payloads, logs, audit metadata, and dead-letter inspection redact
    secrets and sensitive customer data. Raw adapter responses are not stored in
    the outbox.
16. SQL identifiers and predicates are implementation-owned; payload values use
    parameters. Malformed JSON/schema versions are quarantined or dead-lettered,
    never interpolated into SQL or dispatched.

## 4. Crash-window matrix

Each failpoint terminates or force-closes the process/connection at the named
boundary, reopens the same file, runs integrity checks, and then starts a fresh
worker. Assertions cover both rows and observable dispatch counts.

| ID | Injected crash window | Expected durable state after reopen | Acceptance criteria |
|---|---|---|---|
| C01 | before `BEGIN` | no new business/event/outbox/audit rows | retry creates one complete transaction |
| C02 | after business mutation, before event/outbox insert | entire UoW rolled back | no projection change or orphan record |
| C03 | after outbox insert, before audit insert | entire UoW rolled back | no dispatchable outbox row |
| C04 | immediately before `COMMIT` | pre-state after forced rollback | retry with same key creates exactly one command/outbox |
| C05 | commit succeeds but caller crashes before receiving result | complete post-state | lookup by same key returns existing result; row count stays one |
| C06 | after claim commit, before adapter call | `running` with lease/token | no claim before expiry; one re-claim after expiry with higher token |
| C07 | during adapter call, no external result recorded | `running`; external outcome unknown | expiry routes to verify/reconcile; no blind second write |
| C08 | fake adapter succeeds, process crashes before local completion | local state remains non-terminal | reconciliation finds external success and completes locally without second side effect |
| C09 | completion row update occurs, before completion audit insert | both roll back | current lease remains recoverable; no false `done` |
| C10 | completion transaction commits, process crashes before ack | `done` plus audit evidence | redelivery/old ack is a no-op or explicit already-complete result |
| C11 | retry scheduling transaction interrupted | prior `running` state or complete retry state | item is not lost; expiry remains a recovery path |
| C12 | dead transition interrupted | prior retryable state or atomic `dead` plus audit | never `dead` without evidence; never missing |
| C13 | checkpoint commit then process/PC restart | last committed workflow checkpoint | resumes next incomplete step, never repeats completed side effect |
| C14 | work performed but checkpoint transaction not committed | previous checkpoint | repeated computation is safe; side-effect replay uses same key/reconciliation |

Run C01–C14 with SQLite WAL mode if selected and again with the supported
default journal configuration. After each case, `PRAGMA integrity_check` must
return `ok`, foreign-key checks must return no rows, and audit verification must
pass.

## 5. Lease, fencing, retry, and idempotency matrix

| ID | Scenario | Required assertions |
|---|---|---|
| L01 | 2 workers claim one queued item simultaneously | exactly one receives it; one `running` row; attempts = 1; token = initial value |
| L02 | N workers claim M items | each item has one current owner; no item is lost; ordering contract is deterministic where specified |
| L03 | claim before `available_at` | no claim and no mutation |
| L04 | second claim before lease expiry | no claim; owner/token/attempts unchanged |
| L05 | claim at expiry boundary | one documented comparison rule (`<=` or `<`) applied consistently using DB/injected time |
| L06 | re-claim after expiry | new owner, attempts +1, fencing token strictly greater |
| L07 | stale worker completes after re-claim | affected rows = 0 / typed stale-lease error; new lease unchanged; no completion audit |
| L08 | stale worker calls `fail` or lease extension | rejected with same protections as L07 |
| L09 | current worker completes twice | first commits; second is idempotent already-complete or rejected; one terminal audit/event |
| L10 | current worker fails below maximum attempts | queued/retry state; `available_at` follows backoff bounds; error sanitized/truncated |
| L11 | failure reaches maximum attempts | atomic `dead` state plus audit/incident evidence; no future claim |
| L12 | worker dies repeatedly | attempts increase only on successful claim; reaches dead policy predictably |
| L13 | DB busy/locked during claim | bounded retry or typed transient failure; never reports an uncommitted lease |
| I01 | same tenant/key/same semantic payload submitted concurrently | one command/event/outbox; callers resolve to same durable identity |
| I02 | same tenant/key/different payload or operation | conflict; original data unchanged; rejected attempt audited without leaking payload |
| I03 | two tenants use same key | independent successful rows and chains |
| I04 | duplicate queue delivery before completion | lease excludes concurrent execution; stable adapter key reused |
| I05 | duplicate delivery after completion | no adapter invocation and no second business transition |
| I06 | fake adapter records duplicate request with same key | returns prior outcome; external side-effect counter remains one |
| I07 | external timeout then positive reconciliation | item completes from verified truth; write-call counter remains one |
| I08 | external timeout then verified absence | retry uses original idempotency key; at most one external effect |
| I09 | malformed/unsupported payload | no adapter call; quarantined/dead per policy with redacted evidence |

Concurrency tests use independent SQLite connections and a barrier so claims and
idempotent submissions actually overlap. Repeating a test serially is not
sufficient evidence.

## 6. Tenant and audit adversarial matrix

| ID | Scenario | Acceptance criteria |
|---|---|---|
| S01 | Tenant A lists/gets Tenant B job | indistinguishable not-found/denied response; no B data returned |
| S02 | A claims B job by guessed ID | zero mutation; denied attempt is safely audited for A/platform without revealing B facts |
| S03 | A completes B job with stolen valid token | zero mutation; B item remains claimable by B only |
| S04 | repository method omits tenant context | typed fail-closed error before SQL execution |
| S05 | cross-tenant join/export query through application API | prohibited; results contain only active context tenant |
| S06 | same aggregate/external/idempotency values in A and B | both work independently; separate lease and audit chains |
| A01 | multiple connections append audits for one tenant | one linear, gap-free hash chain; all hashes verify |
| A02 | tenants append concurrently | each chain starts/continues independently; no cross-chain hash reference |
| A03 | transaction rolls back after audit construction | no row and chain head unchanged |
| A04 | attempt to update/delete audit via application role/API | denied; chain unchanged; denial evidence produced where safe |
| A05 | persisted audit row is tampered in test fixture | verification fails at deterministic position and startup/health reports degraded |
| A06 | restart and backup/restore copy | chain verifies; last durable head equals recomputed head |
| A07 | error contains API key/email/order PII | stored/logged/audited representation is redacted |

SQLite lacks production RLS, so tests must prove mandatory tenant predicates and
deny tenant-less repository entry points. Passing these tests does not waive the
PostgreSQL RLS gate for LIVE.

## 7. Restart, checkpoint, and container acceptance

### Process and PC restart

Use a real file on a mounted/persistent path, not `:memory:` or an ephemeral
container filesystem. A scripted acceptance run must:

1. create two tenants and enqueue multiple workflows;
2. finish one workflow, leave one claimed, and commit an intermediate checkpoint
   for another;
3. stop the worker without graceful in-memory cleanup;
4. reopen the same database with a new process identity;
5. wait or advance the injected clock past the abandoned lease;
6. drain and reconcile all work;
7. assert every intended workflow reaches exactly one terminal business outcome,
   completed steps are not re-executed, no committed item is lost, and all audit
   chains verify.

The manual PC reboot drill repeats this flow across an actual Windows reboot and
records pre/post database digest, job counts, checkpoint versions, adapter fake
side-effect counts, and audit verification. Automation must never reboot the
user's PC itself.

### Docker Compose

Compose is accepted only when:

- the database directory is an explicit named volume or bind mount outside the
  disposable container layer;
- the worker has a bounded safe restart policy such as `unless-stopped`, and no
  restart loop can bypass schema/integrity failure;
- healthcheck verifies database open, expected schema version, integrity status,
  and worker readiness; liveness alone is insufficient;
- dependency readiness is health-based rather than startup-order assumptions;
- `docker compose stop` then `start`, container recreation, and Docker daemon
  restart preserve the test fixture and pass the restart scenario above;
- a corrupted database or unsupported migration makes the service unhealthy and
  prevents dispatch, instead of recreating an empty database;
- graceful shutdown stops new claims and either completes within a bounded grace
  period or leaves the lease to expire; it never marks unfinished work done.

Docker Desktop's **Start Docker Desktop when you sign in** option may be
documented as an optional operator prerequisite for unattended local DEMO
recovery. Phase 2 code, tests, CLI commands, installers, and agents must not
change Windows login startup, registry, Task Scheduler, Docker Desktop settings,
or any equivalent user/OS setting. Verification is documentation review plus a
before/after settings snapshot performed by the user if desired.

## 8. Observability and test evidence

Required metrics/log assertions:

- claim wins/losses, expired lease reclaims, stale-token rejections;
- attempts by result, retry scheduled, dead count and oldest eligible age;
- duplicate idempotency hits/conflicts and reconciliation outcomes;
- UoW commit/rollback counts and SQLite busy/lock duration;
- audit verification status per tenant without emitting tenant identity publicly;
- correlation ID joins command, approval, event, outbox, worker attempt, adapter
  verification, and terminal audit evidence.

Tests must not assert secrets or raw PII in snapshots. Failure artifacts contain
synthetic IDs and redacted error summaries only. Each crash test records the
failpoint, pre/post durable rows, fake adapter call ledger, and integrity result.

## 9. Phase 2 release gates

Phase 2 is accepted only when all of the following are true:

- all C01–C14, L01–L13, I01–I09, S01–S06, and A01–A07 scenarios pass repeatedly;
- at least 100 repeated concurrent-claim/idempotency runs show zero double owners,
  zero lost rows, and zero duplicate fake external effects;
- process restart and Compose stop/start/recreate tests pass automatically;
- the manual Windows reboot drill is documented and its evidence template exists;
  performing or configuring OS auto-start is not a CI requirement and is never
  done without the user;
- every external-write simulation demonstrates stable idempotency plus an
  unknown-result reconciliation path;
- database integrity, foreign keys, migration version, checkpoint consistency,
  tenant isolation, and audit chains pass after all crash/restart suites;
- no test requires a credential, network access, paid service, marketplace API,
  Claude, or changes to host startup settings;
- README/operations documentation states that SQLite is DEMO-only and that LIVE
  still requires PostgreSQL concurrency, RLS, backup/restore, and migration
  validation.

Any duplicate purchase/refund/publish effect, stale-worker completion, cross-
tenant disclosure/mutation, orphan outbox row, unverifiable audit chain, or loss
of committed checkpoint is a release-blocking failure. It cannot be waived by a
retry or treated as flaky.
