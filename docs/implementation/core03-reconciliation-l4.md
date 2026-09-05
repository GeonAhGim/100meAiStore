# CORE-03 UNKNOWN reconciliation — next implementation packet

Status: local DEMO implementation and acceptance tests recorded in `core03-evidence.md`. Specification started from verified CORE-02 commit `5c9ae06`. Actual external provider writes remain outside the packet.

Implementation refinement: `DemoExecutionControlPlane` is an explicit StoreControlPlane subclass. A master configures durable demo policy/target/stop snapshots using `set_demo_control`; missing controls fail closed. Attempts additionally store provider/connection_id so capability checks bind to the specific manifest. DEMO_EXECUTE and DEMO_LOOKUP are explicit capabilities. Only the concrete DurableSyntheticProvider type is accepted by dispatch/reconciliation, with a distinct SQLite file from the control ledger. This is deliberately not a general production adapter gateway.

## Scope and prerequisites

Build a local execution-attempt ledger and synthetic adapter contract around CORE-02 preparations. No real API credentials, HTTP writes, payments or deployments. Prepared intent alone does not authorize dispatch. A trusted executor must reload tenant stop state, policy/target snapshot, current approval/capabilities and digest immediately before dispatch. Until trusted state providers exist, only an explicit DEMO executor may run synthetic fixtures.

## Storage and invariants

New migration 7, never edit migrations 1–6. ExecutionAttempt: id, tenant_id, command_id, preparation_id, operation_key, intent_digest, adapter_version, state, version, lease_owner, lease_until, fencing_token, provider_reference (opaque), last_observed_at, next_check_at. Unique (tenant_id, operation_key); composite tenant foreign keys. Append-only AttemptObservation: id, tenant_id, attempt_id, observation_kind, response_digest, observed_at, correlation_id. Raw provider bodies/credentials never enter audit or observations.

operation_key is deterministic over tenant, command and logical operation version, survives process retry, and is never regenerated to evade an uncertain result. One logical operation may have many observations but only one stable operation key.

## State and service contracts

`prepare_attempt(context, command_id, policy_version, target_version, adapter_version)` reloads the CORE-02 gates and creates PREPARED plus audit/outbox atomically. Adapter manifest must explicitly support DEMO execution and lookup in the next contract; do not infer this from ORDERS_WRITE alone.

`claim_attempt(tenant_id, attempt_id, worker_id, expected_version, lease_duration)` uses CAS and a strictly increasing fencing token. A nonexpired lease blocks another claim. `begin_dispatch` rechecks the trusted state and records DISPATCHING before any synthetic side effect. If a process dies after this commit, recovery treats it as UNKNOWN, even if the fake adapter was never called.

`record_observation(tenant_id, attempt_id, worker_id, fencing_token, observation)` requires a live matching lease. VERIFIED_SUCCESS/VERIFIED_FAILURE/UNKNOWN commit state, version, observation, audit and outbox together. No network call inside the DB transaction. Stale workers cannot record results.

`reconcile_attempt` performs read-only synthetic lookup by operation_key. Results are FOUND_SUCCESS, FOUND_FAILURE, ABSENT or INCONCLUSIVE. FOUND results produce verified terminals. INCONCLUSIVE stays UNKNOWN and schedules bounded backoff or MANUAL_REVIEW. ABSENT permits another dispatch only when the adapter explicitly supplies an authoritative absence/consistency guarantee and the original approval still passes all gates. Otherwise MANUAL_REVIEW. Cancellation, expiry or changed policy prevents further dispatch but does not erase evidence of an earlier possible effect.

Transitions: PREPARED → DISPATCHING → VERIFIED_SUCCESS / VERIFIED_FAILURE / UNKNOWN; recovered DISPATCHING → UNKNOWN; UNKNOWN → RECONCILING → verified / UNKNOWN / MANUAL_REVIEW. No automatic UNKNOWN → PREPARED based on timeout alone.

An authoritative ABSENT result from RECONCILING may return to PREPARED after rechecking approval/control. It retains the same operation key. Five inconclusive observations lead to MANUAL_REVIEW, with bounded exponential delay between lookups. Duplicate result callbacks after lease release are rejected; they create no extra observation or effect.

## Synthetic adapter

Define deterministic in-process fixtures with a separate durable fake-provider ledger to simulate an effect surviving worker death. Modes: success, known refusal, timeout-before-effect, timeout-after-effect, delayed lookup visibility, lookup unavailable, and stale response. This is contract validation, not proof that Naver/Coupang/supplier APIs offer equivalent idempotency or lookup guarantees.

## Acceptance

| ID | Scenario | Expected evidence |
|---|---|---|
| EX-01 | valid DEMO preparation/dispatch/lookup | one verified attempt and fake effect |
| EX-02 | timeout after effect | UNKNOWN then lookup success; no second effect |
| EX-03 | crash after DISPATCHING before callback | UNKNOWN on restart, no blind retry |
| EX-04 | provider effect then local commit failure | recover using stable key |
| EX-05 | expired lease/stale fencing | result mutation rejected |
| EX-06 | unsupported lookup or inconclusive absence | manual review, no resend |
| EX-07 | approval revoked/expired/changed or stop active | dispatch denied, evidence retained |
| EX-08 | concurrent workers and duplicate observations | one logical operation; atomic observation/audit/outbox |
| EX-09 | cross-tenant attempt/reference | no data leakage or mutation |
| EX-10 | migration and core regression | v6 upgrade preserves inbox/intents/preparations; all prior tests pass |

Done: EX-01–10, kill/restart tests with fake-provider ledger, secret scan, evidence document, scoped commit. The subsequent ADAPTER-01 packet adds read-only DEMO channel ingestion contracts; real provider discovery and LIVE gates remain explicit.
