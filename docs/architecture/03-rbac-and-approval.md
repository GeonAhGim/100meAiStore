# D-03. RBAC and approval policy

## Roles

Permissions are capabilities, not UI labels. A membership may carry both `funds` and `catalog_cs`; the master always has all capabilities.

| Role | Capabilities |
|---|---|
| `master` | tenant administration, membership, channel/supplier deletion, policy/limits, funds, catalog, CS, approvals, stop/recovery |
| `funds` | view/record ledger and settlements; approve purchase, refund/compensation, financial policy where delegated |
| `catalog_cs` | catalog/offer/content, product approval, CS drafts; approve non-financial catalog/CS actions where delegated |
| `auditor` (optional read-only) | dashboards, orders, settlements, audit; no commands |
| `service` | narrow machine capability; no human approval authority |

Membership limit: one master plus two members by default, maximum three; raising the limit is a master-only policy change and remains `TBD` if product policy permits it.

## Authorization matrix

| Action | master | funds | catalog_cs | auditor | approval |
|---|---:|---:|---:|---:|---|
| view tenant operations | ✓ | scoped | scoped | read | none |
| add/remove user, delete tenant/channel | ✓ | — | — | — | master |
| change limits/policies/schedules | ✓ | — | — | — | master |
| approve new product/content/offer | ✓ | — | ✓ | — | one authorized account |
| approve supplier replacement | ✓ | — | ✓ | — | one authorized account; identity guard |
| approve purchase order | ✓ | ✓ | — | — | one authorized account; financial policy |
| approve refund/compensation | ✓ | ✓ | — | — | one authorized account; amount policy TBD |
| approve non-routine CS | ✓ | — | ✓ | — | one authorized account |
| emergency stop/recovery | ✓ | — | — | — | stop immediate; recovery one master |
| record supplier payment evidence | ✓ | ✓ | — | — | prior PO approval required |
| read audit/export | ✓ | scoped | scoped | read | export policy |

## Approval policy matrix

| Command kind | Default | Re-check before execute |
|---|---|---|
| new product, offer, content change | human approval; 24h expiry | source rights, compliance, margin, channel validation |
| purchase order | human approval; 24h expiry | supplier cost, stock, total/day limit, safety balance, stop state |
| supplier replacement | human approval; 24h expiry | exact identity: barcode/model/brand/spec/color/config |
| refund/compensation | human approval; 24h expiry | claim evidence, amount limit, duplicate check |
| non-routine CS | human approval; 24h expiry | answer risk classification and current order state |
| sell pause | immediate temporary safety action; final action approved | risk reason, scope, active stop |
| routine read-only CS | bounded automatic answer allowed | source freshness and answer template |
| price/stock update | automatic only if future DEC-04 bounds pass | cost, stock freshness, margin ≥10%, change-rate cap |

Expired approval is not executable. Any material change in cost, stock, or margin creates a new command and approval. “Approve immediately” skips the queue delay but not authorization, re-check, audit, or policy.

## Schedules

Tenant policy stores separate schedules for `agent_run`, `product_approval`, and `purchase_approval`. v1 defaults are respectively `09:00, 13:00, 17:00, 21:00`; `09:00, 13:00, 17:00`; and `09:00, 12:00, 15:00, 18:00` in tenant local time. The master may change them; equal timestamps remain separate queue instances.

## Approval object contract

```json
{
  "approval_id": "uuid",
  "tenant_id": "uuid",
  "command_id": "uuid",
  "kind": "purchase|product|supplier_replacement|refund|cs|pause",
  "requested_at": "timestamp",
  "expires_at": "timestamp",
  "before": {},
  "after": {},
  "evidence": [{"type":"source_snapshot|price_calc|claim|policy","ref":"opaque-id"}],
  "risk": {"reasons": [], "confidence": 0.0},
  "decision": "pending|approved|rejected|expired|superseded",
  "decided_by": "user-id|null",
  "decision_reason": "string|null"
}
```
