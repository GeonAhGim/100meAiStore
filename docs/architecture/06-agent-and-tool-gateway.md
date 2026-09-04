# D-06. Agents, tool gateway, BYOK, token budgets

## Boundaries

`Operations Agent` observes operational facts, proposes structured decisions, invokes only Tool Gateway commands, and verifies outcomes. `Engineering Agent` works on connector code/tests in an isolated development runtime with a separate identity and allowlist. Neither agent can directly access the ledger database, secret material, or the other agent's credentials.

Loop: `OBSERVE → DECIDE → ACT → VERIFY → DONE | INCIDENT | QUESTION`. A run stores goal, tenant, policy version, model, prompt version, input digest, structured decision, confidence, tool calls, reviewer, cost, and outcome. Routine CS templates may answer order receipt, delivery lookup, business hours, return process/address, stockout expectation, and receipt confirmation; refund promises, liability admission, compensation, efficacy, and delivery-date guarantees always produce approval-required output.

## Tool Gateway contract

```json
{
  "command_id":"uuid",
  "tenant_id":"uuid",
  "actor":{"type":"user|agent|workflow","id":"opaque"},
  "tool":"publish_offer|update_stock|update_price|create_purchase_order|claim_action|reconcile|pause_scope|resume_scope",
  "target":{"type":"offer|order|supplier|channel|tenant|product","id":"opaque"},
  "input":{},
  "idempotency_key":"string",
  "requested_policy_version":"string",
  "approval_id":"uuid|null",
  "mode":"DEMO|SHADOW|LIVE"
}
```

Gateway sequence: authenticate → resolve tenant/membership → authorize capability → validate schema → check stop state → evaluate deterministic policy/budget/freshness → require or validate approval → persist command/audit → enqueue adapter write → verify/reconcile → emit result.

Minimum tool result:

```json
{"command_id":"uuid","state":"accepted|approval_required|blocked|executing|succeeded|failed|unknown","external_refs":[],"policy_decision":{},"verification":{},"next_action":"string|null"}
```

## BYOK and AI budget

Master submits an OpenAI API key through a server-side TLS endpoint. The gateway stores only a secret reference backed by KMS/secret storage; it never re-displays or decrypts for administrators. Loss recovery: email verification → destroy stored key → master registers a new key. The key is never sent to the PWA, ChatGPT, logs, prompts, or engineering runtime.

Per tenant: daily/monthly AI budgets, model tier (`economy|balanced|quality`), generation count, agent-run count, max tokens, max tool calls, and escalation rule. Record estimated and charged cost in `CostLedger`; on key error or budget exhaustion, stop AI research/content/price/purchase judgment while deterministic order/inventory collection, approvals, safety actions, and alerts continue.

Token economy: stable prompt prefix, digest/change-only context, structured summaries/cache, cheap classifier then escalation, bounded tool calls, and per-task turn limits. Never use the AI budget as authorization for a business side effect.

## Question protocol

When information is insufficient or ambiguous, return—not infer:

```json
{
  "question_id":"uuid",
  "tenant_id":"uuid",
  "missing_information":["string"],
  "impact":"string",
  "options":[{"id":"A","label":"string","effect":"string"}],
  "recommendation":"A|null",
  "safe_default":"pause|no_write|retain_current|manual_review",
  "expires_at":"timestamp"
}
```

No response by expiry triggers the safe default and an audit event. Examples: unclear supplier identity → no replacement; unclear rights → warning/approval; unclear refund responsibility → no promise and escalate; insufficient stock freshness → pause offer.
