# B06 local DEMO approval inbox and mobile contract — L4 packet

The approval resource reuses the existing CORE-02 `ApprovalIntent` digest and
`prepare_execution` checks. `approval_inbox` and `approval_detail` are
tenant-scoped, read-only-shaped responses with `{items,next_cursor,as_of,
stale,permissions}` and the D-07 evidence/expiry/action fields. They expose
no secrets and do not send push, email, ChatGPT, or vendor requests.

`decide_approval` accepts an approval id, decision, reason, and opaque
confirmation nonce. It resolves the command server-side, expires pending
approvals at `now >= expires_at`, and delegates the state transition to the
existing capability, tenant, single-decider, and immutable-intent checks.
Approved DEMO purchase orders still require `submit_demo_po` to revalidate
current order state, expiry, approval state, and intent digest immediately
before the local submission checkpoint.

Acceptance: pending approvals list with detail evidence; approve/reject with
nonce validation; 24-hour expiry and durable expiry evidence; changed intent,
stale session, missing capability, cross-tenant id, and duplicate decision
fail closed; restart persistence; no external notification or write.
