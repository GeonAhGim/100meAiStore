# B08 local DEMO typed gateway, agent ledger, BYOK metadata, and budget — L4

Only the allowlisted typed tool names and target kinds from D-06 are accepted.
Every command is tenant scoped, persisted with an idempotency key, and returns
the D-06 result envelope. Mutating tools require an existing approved command;
the gateway never performs an adapter or network write and always returns an
empty `external_refs` list in DEMO mode.

Agent runs persist goal, policy/model/prompt versions, input digest, structured
decision, confidence, tool-call count, cost, and outcome. A tenant budget policy
atomically gates daily/monthly cost, generation count, run count, token and
tool-call limits; exhausted budgets record `BLOCKED_BUDGET` without charging.
BYOK stores only an opaque `secret-ref:` identifier and validation status. Raw
keys are rejected from inputs and never enter the database, audit, or outbox.

Acceptance is local SQLite/InMemory persistence, idempotent commands, approved
tool gate, tenant isolation, budget stop, opaque-reference validation, restart
recovery, and no OpenAI/provider/network invocation.
