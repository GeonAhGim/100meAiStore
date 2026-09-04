# D-02. Multi-tenant data model, ownership, retention

## Conventions

All IDs are opaque UUIDs; timestamps are UTC with display timezone configured per tenant; money is integer minor units plus ISO currency; quantities are decimal with unit. Every tenant-owned table has `tenant_id`, `created_at`, `updated_at`, and `version`. Mutable aggregates use optimistic concurrency. PII is classified per field, encrypted where stored, and excluded from AI context by default.

## Core schema (logical)

```text
Tenant(id, legal_name, status, mode, locale, timezone, deleted_at, purge_after)
TenantTransfer(id, original_tenant_id, destination_tenant_id, kind, approved_by, audit_ref)
User(id, email, auth_status, mfa_method, revoked_at)
Membership(tenant_id, user_id, role_set, status, created_at)
Policy(tenant_id, version, thresholds_json, schedule_json, effective_at)
SecretRef(id, tenant_id, provider, key_alias, kms_key_ref, status, rotated_at)

Supplier(id, tenant_id, name, verification_status, trust_grade, return_address_ref)
SupplierConnection(id, tenant_id, supplier_id, kind, secret_ref_id, cursor, freshness_policy)
SupplierProduct(id, tenant_id, supplier_id, external_key, source_hash, latest_snapshot_id, status)
SupplierSnapshot(id, tenant_id, supplier_product_id, captured_at, payload_uri, payload_hash, rights_status)
CanonicalProduct(id, tenant_id, identity_key, category, attributes_json, compliance_status,
  content_policy_version, food_label_json)
ProductLineage(id, tenant_id, source_snapshot_id, canonical_product_id, transform_version)
ContentAsset(id, tenant_id, canonical_product_id, kind, rights_status, storage_uri, version, status, expires_at)
ContentCandidate(id, tenant_id, canonical_product_id, asset_id, generated_at, expires_at, review_status)
Channel(id, tenant_id, kind, auth_status, secret_ref_id, status)
ChannelOffer(id, tenant_id, canonical_product_id, channel_id, external_key, content_version,
  price_minor, stock, status, policy_version, source_observed_at)

InventoryObservation(id, tenant_id, product_id, supplier_id, quantity, observed_at,
  expected_refresh_seconds, confidence, safety_stock)
PriceCalculation(id, tenant_id, offer_id, cost_snapshot_id, fee_inputs_json,
  projected_profit_minor, projected_margin, calculated_at)

ChannelOrder(id, tenant_id, channel_id, external_order_key, status, customer_ref,
  totals_json, received_at, idempotency_key)
OrderLine(id, tenant_id, channel_order_id, offer_id, quantity, routed_status)
RoutingDecision(id, tenant_id, order_line_id, supplier_id, reason, confidence, status)
SupplierPurchaseOrder(id, tenant_id, supplier_id, status, payment_ref, approved_at)
PurchaseLine(id, tenant_id, purchase_order_id, order_line_id, quantity, unit_cost_minor)
Shipment(id, tenant_id, order_line_id, supplier_id, tracking_key, status)

Claim(id, tenant_id, channel_order_id, type, consumer_status, channel_status,
  supplier_status, amount_minor, approval_id)
Settlement(id, tenant_id, channel_id, period, source_ref, status)
SettlementLine(id, tenant_id, settlement_id, order_line_id, kind, amount_minor, source_row_ref)
ProfitSnapshot(id, tenant_id, order_line_id, projected_json, realized_json, calculated_at)

Approval(id, tenant_id, command_id, kind, state, expires_at, decided_by, evidence_hash)
Command(id, tenant_id, type, target_ref, payload_json, idempotency_key, state, policy_version)
AuditEvent(id, tenant_id, actor_type, actor_ref, action, target_ref, outcome,
  correlation_id, occurred_at, metadata_json, prev_hash, event_hash)
DomainEvent(id, tenant_id, aggregate_type, aggregate_id, event_type, sequence,
  payload_json, occurred_at)
Outbox(id, tenant_id, event_type, aggregate_ref, payload_json, status, attempts, next_attempt_at)
CostLedger(id, tenant_id, provider, category, units, amount_minor, period)
```

## Ownership and isolation

| Data | Owner | Access rule |
|---|---|---|
| users/memberships/policies/secrets refs | tenant control plane | master for administration; members according to role |
| source/product/offer/inventory | tenant Store Core | catalog/product permissions |
| orders/PO/shipments/claims/settlements | tenant Store Core | finance/CS/product permissions as applicable |
| audit/events/outbox | platform on behalf of tenant | append-only; read by authorized tenant users |
| secret material | KMS/secret store | gateway service identity only; never returned |
| PII | tenant | least privilege; masked views; retention/purge workflow |

Database defense in depth: application tenant context is mandatory; repository queries require tenant predicates; production should add row-level security or an equivalent tested isolation mechanism. Cross-tenant exports are forbidden by default.

## Retention and deletion

- Business transfer/split preserves order, settlement, and audit history under the original tenant ownership; a legal/operational transfer record links the destination view.
- Tenant deletion is `requested → cooling-off → purging → purged`; restore is allowed during cooling-off. The cooling-off duration and legal retention duration are `TBD` (DEC-07).
- During retention, remove or tokenize direct identifiers while preserving financial/audit referential integrity. Do not delete immutable event evidence needed for a legal hold.
- Source snapshots, content candidates, receipts, and audit evidence use separate retention classes. Candidate content expires after 10 days unless registered/archived; registered/archive versions and lineage are retained per DEC-07.
- `food_label_json` is required for feed/treat products: manufacturer, expiry, ingredients, origin, storage, and allergens. Efficacy wording is retained with a warning/review result rather than silently erased.
