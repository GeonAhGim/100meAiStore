# CORE-02 local execution preparation evidence

Design baseline: `31bcff3`, `core02-approval-intent-l4.md`. Implementation adds immutable ApprovalIntent/ExecutionPreparation, SQLite migration 6, repository methods and StoreControlPlane request_approval/prepare_execution. Existing decide rechecks any attached intent before decision. Legacy commands without an intent cannot prepare.

The intent binds payload, evidence, target, kind, expiry, policy version and target version using strict finite JSON serialization. Preparation requires both caller and original approver to retain current capability and requires a current unexpired approved command. Unique tenant/command keys, transaction and immutable SQLite triggers preserve one preparation. Audit and outbox commit with it. Tests use synthetic data and local databases only.

PREPARED is a local durable checkpoint, not external execution authority. Executor-side current state checks and any network dispatch remain CORE-03 or later. No real order, payment or supplier write was performed.

Acceptance coverage in `test_approval_intent.py`: AP-01/02 success and idempotency; AP-03 payload/evidence/version change and invalid JSON rejection; AP-04 missing intent/pending/rejected/expired/superseded; AP-05/06 changed caller/approver authority and tenant boundary; AP-07 audit/outbox rollback on both repositories; AP-08 eight independent-connection preparations; AP-09 process death before/after commit; AP-10 v5-to-v6 membership preservation and immutable intent SQL rejection, plus existing migration regressions.

Existing migration test hardcoded latest version 5 and failed when v6 was added. Expected installed versions now come from MIGRATIONS; failure-injection migration uses LATEST_SCHEMA_VERSION + 1. Cleanup is in finally so a failed assertion cannot leak a SQLite handle. Existing data assertions remain intact.

Final verification on 2026-09-05: `python -B -m pytest -q -p no:cacheprovider` — **49 passed in 5.53s**. Parent independently confirmed 49 passing tests and a secret-pattern scan with no matches. Schema migrations 1–5 remain unchanged. No deployment or live execution occurred.
