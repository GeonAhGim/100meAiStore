# D-07. Android-first PWA UX and approval contracts

## Information architecture

Persistent shell: tenant switcher, DEMO/LIVE badge, urgent incident banner, notification state, and current user capability. Primary navigation:

`Summary / 승인함(상품) / 승인함(발주) / Orders & Procurement / Products / CS / Funds & Settlement / Suppliers / Channels / AI & Automation / Stop & Recovery / Users & Permissions / Audit / Scenario Test`.

Mobile-first rules: one primary action per screen, sticky evidence summary, safe-area support, keyboard-accessible controls, offline read cache only, no cached secrets, and re-authentication for approval/recovery. English and Korean strings are keyed; no hard-coded language assumptions. iOS/native is a later client of the same API.

## Approval screen contract

```json
{
  "approval_id":"uuid",
  "kind":"product|purchase|supplier_replacement|refund|cs|pause",
  "risk_badges":["margin_change","stale_stock","rights_warning"],
  "target":{"label":"string","ref":"opaque-id"},
  "before":{},
  "after":{},
  "profit":{"projected_profit_minor":0,"margin_ex_ad":0.0,"margin_with_ad":0.0,"currency":"KRW"},
  "evidence":[{"label":"string","observed_at":"timestamp","ref":"opaque-id"}],
  "policy":{"version":"string","decision":"allow|deny|approval_required","reasons":[]},
  "rollback":{"available":true,"description":"string"},
  "expires_at":"timestamp",
  "actions":["approve","reject","ask_question"]
}
```

Approval UI must show before/after, source observation time, expected profit, changed fields, risk reasons, rollback/compensation, expiry, and who is authorized. The client submits `approval_id` plus a confirmation nonce; the server re-checks all policy and current facts.

## Incident and stop UX

Urgent incidents are actionable from the banner and incident timeline. Scope selector: `GLOBAL | TENANT | CHANNEL | SUPPLIER | PRODUCT`. Stop confirmation lists what will stop (new registration/change/purchase) and what continues (in-flight shipment/CS). Recovery screen shows health checks, blocked commands, partial/full scope, and a one-master approval. “Resume selected work” is explicit; nothing restarts implicitly.

## API view contracts

Every list returns `{items, next_cursor, as_of, stale, permissions}`. Every mutation returns the D-06 command result. PWA never calls a vendor API or AI provider directly. ChatGPT links users to the same approval resource for side effects.
