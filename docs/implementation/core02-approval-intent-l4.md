# CORE-02 approval intent and preparation L4

Implementation entry: after CORE-01 `f7d82ac`. This packet prepares an immutable execution intent locally. It does not call a supplier, marketplace, bank, model, or payment API.

## Data and canonical representation

Add SQLite migration 6; migrations 1–5 stay unchanged. `approval_intents` uses `(tenant_id, command_id)` as its primary key and a composite foreign key to commands. Columns: canonical_digest, policy_version, target_version, created_at. The digest covers kind, target_ref, the entire payload, the approval evidence list, expires_at, policy_version and target_version. Amount/currency/supplier fields are therefore bound whenever present in the proposed payload. Validation of per-kind financial payload schema is a later ORDER/FINANCE contract, not implied by digest binding.

`execution_preparations` uses `(tenant_id, command_id)` as its primary key and references the intent with the same tenant. Columns: id (unique), canonical_digest, prepared_by, prepared_at. One logical command has at most one preparation; no actual SENT/SUCCESS status is introduced. CORE-03 will add execution attempts through a new migration and explicit state machine.

Frozen domain objects: ApprovalIntent and ExecutionPreparation. Repositories return detached immutable values. Save intent inserts only; reusing a command with a different intent conflicts. Preparing twice returns the existing preparation only after all current authority and version checks pass.

Canonicalization accepts JSON values only, finite numbers, and string dictionary keys; no fallback object stringification. Invalid payload/evidence fails before any write. Expiry is bound as an ISO timestamp string to prevent silent extension of approval lifetime.

## Service methods

`request_approval(context, kind, target_ref, payload, idempotency_key, policy_version, target_version, evidence=()) -> (Command, Approval)`

Inside one UoW, require existing kind capability, validate positive integer policy/target versions, and use the existing create_command path. Persist the immutable intent before commit. Idempotent requests may reuse the original intent only if its digest and versions match; a conflicting request rolls back without changing existing approval/audit/outbox.

Existing `decide` checks an attached intent against current command/evidence before approving or rejecting. Legacy commands remain non-executable by the new preparation API, preserving existing prototype compatibility without silently inventing an intent.

`prepare_execution(context, command_id, policy_version, target_version) -> (ExecutionPreparation, replayed)`

Inside one UoW: validate current caller membership and kind capability; load tenant-scoped command/approval/intent; require APPROVED command and approval; reject expiry at now >= expires_at; recompute digest; compare current trusted policy and target versions; require the deciding member is still active and still has the relevant approval capability. Insert preparation, append audit and `execution.prepared` outbox atomically. Repeated preparation after these checks returns the same id and emits no extra events.

The version inputs represent trusted service-layer snapshots in this local slice. An HTTP client must never be allowed to assert these versions as a substitute for server-side policy/target lookup. Executor dispatch must recheck immediately before any future network write; PREPARED alone is not execution authorization for an external connector.

## Failure and concurrency rules

Expired/rejected/superseded/changed-intent requests fail closed. Revoked or downgraded approvers invalidate preparation. A transaction prevents simultaneous supersede/decide/prepare from producing conflicting persisted decisions. SQLite BEGIN IMMEDIATE and the unique command binding arbitrate separate connections. InMemory uses snapshot rollback. A crash before commit leaves no preparation/audit/outbox; a crash after commit is recoverable by replay. No external operation occurs in either case.

## Acceptance tests

| ID | Case | Result |
|---|---|---|
| AP-01 | request, approve, prepare | one immutable intent and one preparation; linked outbox |
| AP-02 | repeated request/prepare | same identifiers; no duplicated events |
| AP-03 | changed payload/evidence/policy/target | digest/version conflict; no preparation |
| AP-04 | no intent, pending/rejected/expired/superseded | denied |
| AP-05 | revoked/stale caller or approver downgrade/revoke | denied |
| AP-06 | cross-tenant command | no foreign preparation disclosure/write |
| AP-07 | outbox/audit failure | preparation and events roll back together |
| AP-08 | concurrent independent connections | one preparation and one event |
| AP-09 | process death before/after commit | all-or-none and replay |
| AP-10 | existing core suite and migration | preserved behavior; v6 readiness |

Done means these tests and evidence are committed; existence of this note is not implementation completion.
