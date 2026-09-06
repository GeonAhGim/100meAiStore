from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .domain import (
    Approval, ApprovalKind, ApprovalState, AuditEvent, Command, CommandState,
    AdapterCapability, AdapterCapabilityManifest, AgentState, AgentStatusSnapshot, InboxMessage, InboxState,
    Membership, OutboxEvent, OutboxState, Role, Tenant, User,
    ApprovalIntent, ExecutionPreparation,
    DemoExecutionControl, ExecutionAttempt, AttemptObservation, AttemptState,
    NormalizedInboundPayload, AdapterPollCheckpoint,
    ChannelOrder, OrderLine, RoutingDecision, SupplierPurchaseOrder, PurchaseLine,
    ChannelOrderState, RoutingState, PurchaseOrderState,
    TrackingObservation,
    DemoClaim, ClaimStatusObservation, ClaimStatus,
    DemoSettlementBatch, DemoSettlementLine, DemoRealizedProfit, SettlementStatus,
    DemoCatalogImport, DemoCatalogSnapshot, DemoCanonicalProduct, DemoProductLineage, DemoChannelOffer,
    DemoToolCommand, DemoAgentRun, DemoByokReference, DemoBudgetPolicy, DemoBudgetLedgerEntry,
    DemoNotificationPreference, DemoNotificationDelivery, DemoIncidentAcknowledgement,
)
from .errors import ConflictError, NotFoundError, TenantBoundaryError


MIGRATIONS = ((1, """
CREATE TABLE tenants(id TEXT PRIMARY KEY, legal_name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE memberships(tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, roles_json TEXT NOT NULL,
 active INTEGER NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(tenant_id,user_id));
CREATE TABLE commands(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, target_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, supersedes_id TEXT, UNIQUE(tenant_id,idempotency_key));
CREATE INDEX commands_tenant_id_id ON commands(tenant_id,id);
CREATE TABLE approvals(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
 decided_by TEXT, decision_reason TEXT, UNIQUE(tenant_id,command_id));
CREATE INDEX approvals_tenant_command ON approvals(tenant_id,command_id);
CREATE TABLE audit_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL, actor_ref TEXT NOT NULL, action TEXT NOT NULL, target_ref TEXT NOT NULL, outcome TEXT NOT NULL,
 correlation_id TEXT NOT NULL, metadata_json TEXT NOT NULL, prev_hash TEXT, event_hash TEXT NOT NULL);
CREATE INDEX audit_tenant_sequence ON audit_events(tenant_id,sequence);
CREATE TABLE outbox(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, topic TEXT NOT NULL, aggregate_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 checkpoint_json TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 completed_at TEXT, UNIQUE(tenant_id,idempotency_key));
CREATE INDEX outbox_tenant_state ON outbox(tenant_id,state,created_at);
"""),(2, """
ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN available_at TEXT;
ALTER TABLE outbox ADD COLUMN last_error TEXT;
CREATE INDEX outbox_claimable ON outbox(tenant_id,state,available_at,created_at);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
"""),(3, """
PRAGMA defer_foreign_keys=ON;
CREATE TABLE memberships_v3(
 tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, roles_json TEXT NOT NULL, active INTEGER NOT NULL, version INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,user_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT);
INSERT INTO memberships_v3 SELECT * FROM memberships;

CREATE TABLE commands_v3(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, target_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, supersedes_id TEXT,
 UNIQUE(tenant_id,idempotency_key), UNIQUE(tenant_id,id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,supersedes_id) REFERENCES commands_v3(tenant_id,id) DEFERRABLE INITIALLY DEFERRED);
INSERT INTO commands_v3 SELECT * FROM commands;

CREATE TABLE approvals_v3(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
 decided_by TEXT, decision_reason TEXT, UNIQUE(tenant_id,command_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,command_id) REFERENCES commands_v3(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(decided_by) REFERENCES users(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,decided_by) REFERENCES memberships_v3(tenant_id,user_id) ON DELETE RESTRICT);
INSERT INTO approvals_v3 SELECT * FROM approvals;

CREATE TABLE audit_events_v3(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL, actor_ref TEXT NOT NULL, action TEXT NOT NULL, target_ref TEXT NOT NULL, outcome TEXT NOT NULL,
 correlation_id TEXT NOT NULL, metadata_json TEXT NOT NULL, prev_hash TEXT, event_hash TEXT NOT NULL,
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
INSERT INTO audit_events_v3 SELECT * FROM audit_events;

CREATE TABLE outbox_v3(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, topic TEXT NOT NULL, aggregate_ref TEXT NOT NULL,
 payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
 checkpoint_json TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL DEFAULT 0,
 completed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT, last_error TEXT,
 UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
INSERT INTO outbox_v3(rowid,id,tenant_id,topic,aggregate_ref,payload_json,idempotency_key,state,created_at,
 checkpoint_json,lease_owner,lease_until,fencing_token,completed_at,attempts,available_at,last_error)
 SELECT rowid,id,tenant_id,topic,aggregate_ref,payload_json,idempotency_key,state,created_at,
 checkpoint_json,lease_owner,lease_until,fencing_token,completed_at,attempts,available_at,last_error FROM outbox;

DROP TABLE approvals;
DROP TABLE memberships;
DROP TABLE audit_events;
DROP TABLE outbox;
DROP TABLE commands;
ALTER TABLE commands_v3 RENAME TO commands;
ALTER TABLE memberships_v3 RENAME TO memberships;
ALTER TABLE approvals_v3 RENAME TO approvals;
ALTER TABLE audit_events_v3 RENAME TO audit_events;
ALTER TABLE outbox_v3 RENAME TO outbox;
CREATE INDEX commands_tenant_id_id ON commands(tenant_id,id);
CREATE INDEX approvals_tenant_command ON approvals(tenant_id,command_id);
CREATE INDEX audit_tenant_sequence ON audit_events(tenant_id,sequence);
CREATE INDEX outbox_tenant_state ON outbox(tenant_id,state,created_at);
CREATE INDEX outbox_claimable ON outbox(tenant_id,state,available_at,created_at);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit events are append-only'); END;
"""),(4, """
CREATE TABLE inbox_messages(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider TEXT NOT NULL, connection_id TEXT NOT NULL,
 external_event_id TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version>0), received_at TEXT NOT NULL,
 payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64), raw_payload_ref TEXT, state TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), processed_at TEXT,
 UNIQUE(tenant_id,provider,connection_id,external_event_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE INDEX inbox_tenant_state_received ON inbox_messages(tenant_id,state,received_at,id);
CREATE TABLE adapter_capability_manifests(
 tenant_id TEXT NOT NULL, provider TEXT NOT NULL, connection_id TEXT NOT NULL, adapter_version TEXT NOT NULL,
 capabilities_json TEXT NOT NULL, inbound_schema_versions_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(tenant_id,provider,connection_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
"""), (5, """
CREATE TABLE agent_status_snapshots(
 tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL,
 current_task TEXT, started_at TEXT, last_heartbeat_at TEXT, ended_at TEXT, last_message TEXT,
 last_commit TEXT, test_result TEXT, next_task TEXT, blocker TEXT, usage_limited INTEGER NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(tenant_id,agent_id),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE INDEX agent_status_tenant_updated ON agent_status_snapshots(tenant_id,updated_at);
"""), (6, """
CREATE TABLE approval_intents(
 tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, canonical_digest TEXT NOT NULL,
 policy_version INTEGER NOT NULL CHECK(policy_version>0), target_version INTEGER NOT NULL CHECK(target_version>0),
 created_at TEXT NOT NULL, PRIMARY KEY(tenant_id,command_id),
 FOREIGN KEY(tenant_id,command_id) REFERENCES commands(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE execution_preparations(
 id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, canonical_digest TEXT NOT NULL,
 prepared_by TEXT NOT NULL, prepared_at TEXT NOT NULL, PRIMARY KEY(tenant_id,command_id),
 FOREIGN KEY(tenant_id,command_id) REFERENCES approval_intents(tenant_id,command_id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,prepared_by) REFERENCES memberships(tenant_id,user_id) ON DELETE RESTRICT);
CREATE TRIGGER approval_intents_no_update BEFORE UPDATE ON approval_intents BEGIN SELECT RAISE(ABORT,'approval intents are immutable'); END;
CREATE TRIGGER approval_intents_no_delete BEFORE DELETE ON approval_intents BEGIN SELECT RAISE(ABORT,'approval intents are immutable'); END;
CREATE TRIGGER execution_preparations_no_update BEFORE UPDATE ON execution_preparations BEGIN SELECT RAISE(ABORT,'execution preparations are immutable'); END;
CREATE TRIGGER execution_preparations_no_delete BEFORE DELETE ON execution_preparations BEGIN SELECT RAISE(ABORT,'execution preparations are immutable'); END;
"""), (7, """
CREATE UNIQUE INDEX preparation_tenant_command_id ON execution_preparations(tenant_id,command_id,id);
CREATE TABLE demo_execution_controls(
 tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, policy_version INTEGER NOT NULL CHECK(policy_version>0),
 target_version INTEGER NOT NULL CHECK(target_version>0), stopped INTEGER NOT NULL CHECK(stopped IN (0,1)),
 PRIMARY KEY(tenant_id,command_id), FOREIGN KEY(tenant_id,command_id) REFERENCES commands(tenant_id,id));
CREATE TABLE execution_attempts(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, preparation_id TEXT NOT NULL,
 operation_key TEXT NOT NULL, intent_digest TEXT NOT NULL, adapter_version TEXT NOT NULL,
 provider TEXT NOT NULL, connection_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 lease_owner TEXT, lease_until TEXT, fencing_token INTEGER NOT NULL CHECK(fencing_token>=0),
 provider_reference TEXT, last_observed_at TEXT, next_check_at TEXT,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,operation_key), UNIQUE(tenant_id,command_id),
 FOREIGN KEY(tenant_id,command_id,preparation_id) REFERENCES execution_preparations(tenant_id,command_id,id),
 FOREIGN KEY(tenant_id,provider,connection_id) REFERENCES adapter_capability_manifests(tenant_id,provider,connection_id));
CREATE INDEX attempt_tenant_recovery ON execution_attempts(tenant_id,state,next_check_at,lease_until);
CREATE TABLE attempt_observations(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, attempt_id TEXT NOT NULL, observation_kind TEXT NOT NULL,
 response_digest TEXT NOT NULL, observed_at TEXT NOT NULL, correlation_id TEXT NOT NULL,
 FOREIGN KEY(tenant_id,attempt_id) REFERENCES execution_attempts(tenant_id,id));
CREATE INDEX observations_tenant_attempt ON attempt_observations(tenant_id,attempt_id);
CREATE TRIGGER attempt_observations_no_update BEFORE UPDATE ON attempt_observations BEGIN SELECT RAISE(ABORT,'observations are append-only'); END;
CREATE TRIGGER attempt_observations_no_delete BEFORE DELETE ON attempt_observations BEGIN SELECT RAISE(ABORT,'observations are append-only'); END;
"""), (8, """
CREATE TABLE normalized_inbound_payloads(
 tenant_id TEXT NOT NULL, immutable_ref TEXT NOT NULL, canonical_digest TEXT NOT NULL CHECK(length(canonical_digest)=64),
 schema_version INTEGER NOT NULL CHECK(schema_version>0), payload_json TEXT NOT NULL,
 source_digest TEXT CHECK(source_digest IS NULL OR length(source_digest)=64), created_at TEXT NOT NULL,
 PRIMARY KEY(tenant_id,immutable_ref),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE INDEX normalized_payloads_tenant_digest ON normalized_inbound_payloads(tenant_id,canonical_digest);
CREATE TRIGGER normalized_payloads_no_update BEFORE UPDATE ON normalized_inbound_payloads BEGIN SELECT RAISE(ABORT,'normalized payloads are immutable'); END;
CREATE TRIGGER normalized_payloads_no_delete BEFORE DELETE ON normalized_inbound_payloads BEGIN SELECT RAISE(ABORT,'normalized payloads are immutable'); END;
CREATE TABLE adapter_poll_checkpoints(
 tenant_id TEXT NOT NULL, provider TEXT NOT NULL, connection_id TEXT NOT NULL, adapter_version TEXT NOT NULL,
 cursor TEXT, overlap_from TEXT, version INTEGER NOT NULL CHECK(version>0), updated_at TEXT NOT NULL,
 last_success_at TEXT,
 PRIMARY KEY(tenant_id,provider,connection_id),
 FOREIGN KEY(tenant_id,provider,connection_id) REFERENCES adapter_capability_manifests(tenant_id,provider,connection_id) ON DELETE RESTRICT);
CREATE INDEX poll_checkpoints_tenant_updated ON adapter_poll_checkpoints(tenant_id,updated_at);
"""), (9, """
CREATE TABLE channel_orders(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, external_order_key TEXT NOT NULL,
 payload_ref TEXT NOT NULL, currency TEXT NOT NULL, total_minor INTEGER NOT NULL CHECK(total_minor>=0),
 status TEXT NOT NULL, received_at TEXT NOT NULL, idempotency_key TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,channel_id,external_order_key), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,payload_ref) REFERENCES normalized_inbound_payloads(tenant_id,immutable_ref) ON DELETE RESTRICT);
CREATE INDEX channel_orders_tenant_status ON channel_orders(tenant_id,status,received_at);
CREATE TABLE order_lines(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_order_id TEXT NOT NULL, sku TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_minor INTEGER NOT NULL CHECK(unit_minor>=0),
 routed_status TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), UNIQUE(tenant_id,id),
 FOREIGN KEY(tenant_id,channel_order_id) REFERENCES channel_orders(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE routing_decisions(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_line_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_cost_minor INTEGER NOT NULL CHECK(unit_cost_minor>=0),
 reason TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(tenant_id,order_line_id),
 FOREIGN KEY(tenant_id,order_line_id) REFERENCES order_lines(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE supplier_purchase_orders(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_order_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
 status TEXT NOT NULL, idempotency_key TEXT NOT NULL, approval_command_id TEXT, created_at TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), UNIQUE(tenant_id,id), UNIQUE(tenant_id,channel_order_id,supplier_id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id,channel_order_id) REFERENCES channel_orders(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,approval_command_id) REFERENCES commands(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE purchase_lines(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, purchase_order_id TEXT NOT NULL, order_line_id TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_cost_minor INTEGER NOT NULL CHECK(unit_cost_minor>=0),
 UNIQUE(tenant_id,purchase_order_id,order_line_id),
 FOREIGN KEY(tenant_id,purchase_order_id) REFERENCES supplier_purchase_orders(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,order_line_id) REFERENCES order_lines(tenant_id,id) ON DELETE RESTRICT);
CREATE INDEX purchase_orders_tenant_status ON supplier_purchase_orders(tenant_id,status,created_at);
"""), (10, """
ALTER TABLE supplier_purchase_orders ADD COLUMN provider_reference TEXT;
ALTER TABLE supplier_purchase_orders ADD COLUMN last_response_digest TEXT;
ALTER TABLE supplier_purchase_orders ADD COLUMN last_observed_at TEXT;
"""), (11, """
ALTER TABLE order_lines ADD COLUMN tracking_key TEXT;
ALTER TABLE order_lines ADD COLUMN tracking_status TEXT;
ALTER TABLE order_lines ADD COLUMN tracking_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE order_lines ADD COLUMN tracking_observed_at TEXT;
CREATE TABLE tracking_observations(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_line_id TEXT NOT NULL, tracking_key TEXT NOT NULL,
 status TEXT NOT NULL, observed_at TEXT NOT NULL, response_digest TEXT NOT NULL CHECK(length(response_digest)=64),
 UNIQUE(tenant_id,order_line_id,tracking_key,status),
 FOREIGN KEY(tenant_id,order_line_id) REFERENCES order_lines(tenant_id,id) ON DELETE RESTRICT);
CREATE INDEX tracking_observations_tenant_line ON tracking_observations(tenant_id,order_line_id,observed_at);
CREATE TRIGGER tracking_observations_no_update BEFORE UPDATE ON tracking_observations BEGIN SELECT RAISE(ABORT,'tracking observations are append-only'); END;
CREATE TRIGGER tracking_observations_no_delete BEFORE DELETE ON tracking_observations BEGIN SELECT RAISE(ABORT,'tracking observations are append-only'); END;
"""), (12, """
CREATE TABLE demo_claims(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_order_id TEXT NOT NULL, claim_type TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor>=0), consumer_status TEXT NOT NULL, channel_status TEXT NOT NULL,
 supplier_status TEXT NOT NULL, idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id,channel_order_id) REFERENCES channel_orders(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE claim_status_observations(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, claim_id TEXT NOT NULL, status_kind TEXT NOT NULL,
 status TEXT NOT NULL, observed_at TEXT NOT NULL, response_digest TEXT NOT NULL CHECK(length(response_digest)=64),
 UNIQUE(tenant_id,claim_id,status_kind,status),
 FOREIGN KEY(tenant_id,claim_id) REFERENCES demo_claims(tenant_id,id) ON DELETE RESTRICT);
CREATE INDEX claim_observations_tenant_claim ON claim_status_observations(tenant_id,claim_id,observed_at);
CREATE TRIGGER claim_status_observations_no_update BEFORE UPDATE ON claim_status_observations BEGIN SELECT RAISE(ABORT,'claim status observations are append-only'); END;
CREATE TRIGGER claim_status_observations_no_delete BEFORE DELETE ON claim_status_observations BEGIN SELECT RAISE(ABORT,'claim status observations are append-only'); END;
"""), (13, """
CREATE TABLE demo_settlement_batches(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, period TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest)=64), status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 created_at TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_settlement_lines(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, batch_id TEXT NOT NULL, external_order_key TEXT NOT NULL,
 kind TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, source_row_ref TEXT NOT NULL,
 order_id TEXT, match_status TEXT NOT NULL, UNIQUE(tenant_id,batch_id,source_row_ref),
 FOREIGN KEY(tenant_id,batch_id) REFERENCES demo_settlement_batches(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,order_id) REFERENCES channel_orders(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE demo_realized_profit(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, batch_id TEXT NOT NULL, order_id TEXT NOT NULL,
 projected_minor INTEGER, realized_minor INTEGER, status TEXT NOT NULL, calculated_at TEXT NOT NULL,
 UNIQUE(tenant_id,batch_id,order_id), FOREIGN KEY(tenant_id,batch_id) REFERENCES demo_settlement_batches(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,order_id) REFERENCES channel_orders(tenant_id,id) ON DELETE RESTRICT);
"""), (14, """
CREATE TABLE demo_catalog_imports(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest)=64), idempotency_key TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_catalog_snapshots(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, import_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
 external_key TEXT NOT NULL, source_digest TEXT NOT NULL CHECK(length(source_digest)=64), payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(tenant_id,id), UNIQUE(tenant_id,import_id,external_key),
 FOREIGN KEY(tenant_id,import_id) REFERENCES demo_catalog_imports(tenant_id,id) ON DELETE RESTRICT);
CREATE INDEX demo_catalog_snapshots_tenant_import ON demo_catalog_snapshots(tenant_id,import_id);
CREATE TRIGGER demo_catalog_snapshots_no_update BEFORE UPDATE ON demo_catalog_snapshots BEGIN SELECT RAISE(ABORT,'catalog snapshots are immutable'); END;
CREATE TRIGGER demo_catalog_snapshots_no_delete BEFORE DELETE ON demo_catalog_snapshots BEGIN SELECT RAISE(ABORT,'catalog snapshots are immutable'); END;
CREATE TABLE demo_canonical_products(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, sku TEXT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL,
 price_minor INTEGER NOT NULL CHECK(price_minor>=0), currency TEXT NOT NULL, attributes_json TEXT NOT NULL,
 source_snapshot_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,sku),
 FOREIGN KEY(tenant_id,source_snapshot_id) REFERENCES demo_catalog_snapshots(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE demo_product_lineage(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, source_snapshot_id TEXT NOT NULL, canonical_product_id TEXT NOT NULL,
 transform_version INTEGER NOT NULL CHECK(transform_version>0), created_at TEXT NOT NULL,
 UNIQUE(tenant_id,source_snapshot_id),
 FOREIGN KEY(tenant_id,source_snapshot_id) REFERENCES demo_catalog_snapshots(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,canonical_product_id) REFERENCES demo_canonical_products(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE demo_channel_offers(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, canonical_product_id TEXT NOT NULL,
 source_snapshot_id TEXT NOT NULL, external_key TEXT NOT NULL, price_minor INTEGER NOT NULL CHECK(price_minor>=0),
 currency TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), created_at TEXT NOT NULL,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,channel_id,canonical_product_id),
 FOREIGN KEY(tenant_id,canonical_product_id) REFERENCES demo_canonical_products(tenant_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,source_snapshot_id) REFERENCES demo_catalog_snapshots(tenant_id,id) ON DELETE RESTRICT);
"""), (15, """
CREATE UNIQUE INDEX approval_tenant_id ON approvals(tenant_id,id);
CREATE TABLE demo_tool_commands(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
 tool TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL, input_json TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, requested_policy_version INTEGER NOT NULL CHECK(requested_policy_version>0),
 approval_id TEXT, mode TEXT NOT NULL, state TEXT NOT NULL, blocked_reason TEXT, created_at TEXT NOT NULL,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key), FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT,
 FOREIGN KEY(tenant_id,approval_id) REFERENCES approvals(tenant_id,id) ON DELETE RESTRICT);
CREATE TABLE demo_agent_runs(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL, goal TEXT NOT NULL,
 policy_version INTEGER NOT NULL CHECK(policy_version>0), model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 input_digest TEXT NOT NULL CHECK(length(input_digest)=64), decision_json TEXT NOT NULL, confidence TEXT NOT NULL,
 tool_calls INTEGER NOT NULL CHECK(tool_calls>=0), reviewer TEXT, estimated_cost_minor INTEGER NOT NULL CHECK(estimated_cost_minor>=0),
 charged_cost_minor INTEGER, outcome TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_byok_references(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, provider TEXT NOT NULL, secret_ref TEXT NOT NULL,
 validation_status TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 UNIQUE(tenant_id,provider), FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_budget_policies(
 tenant_id TEXT PRIMARY KEY, daily_limit_minor INTEGER NOT NULL CHECK(daily_limit_minor>=0), monthly_limit_minor INTEGER NOT NULL CHECK(monthly_limit_minor>=0),
 generation_limit INTEGER NOT NULL CHECK(generation_limit>=0), agent_run_limit INTEGER NOT NULL CHECK(agent_run_limit>=0),
 max_tokens INTEGER NOT NULL CHECK(max_tokens>0), max_tool_calls INTEGER NOT NULL CHECK(max_tool_calls>0), model_tier TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_budget_ledger(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>=0),
 occurred_at TEXT NOT NULL, idempotency_key TEXT NOT NULL, UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id,run_id) REFERENCES demo_agent_runs(tenant_id,id) ON DELETE RESTRICT);
CREATE INDEX demo_budget_ledger_tenant_time ON demo_budget_ledger(tenant_id,occurred_at);
"""), (16, """
CREATE TABLE demo_notification_preferences(
 tenant_id TEXT NOT NULL, notification_key TEXT NOT NULL, channels_json TEXT NOT NULL,
 muted INTEGER NOT NULL CHECK(muted IN (0,1)), version INTEGER NOT NULL CHECK(version>0),
 PRIMARY KEY(tenant_id,notification_key), FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_notification_deliveries(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, notification_key TEXT NOT NULL, channel TEXT NOT NULL,
 payload_json TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt>0), fallback_from TEXT,
 idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT);
CREATE TABLE demo_incident_acknowledgements(
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL, acknowledged_by TEXT NOT NULL,
 note TEXT NOT NULL, idempotency_key TEXT NOT NULL, acknowledged_at TEXT NOT NULL,
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key), FOREIGN KEY(tenant_id,acknowledged_by) REFERENCES memberships(tenant_id,user_id) ON DELETE RESTRICT);
CREATE INDEX demo_notification_deliveries_tenant_time ON demo_notification_deliveries(tenant_id,created_at);
"""))
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]

MAX_ERROR_LENGTH = 500
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|token|password|secret)(\s*[:=]\s*)([^\s,;]+)")


def _safe_error(value: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)[:MAX_ERROR_LENGTH]


def _backoff_seconds(attempts: int) -> int:
    return min(3600, 30 * (2 ** max(0, attempts - 1)))


def _required_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255 or any(c in normalized for c in "\r\n\0"):
        raise ConflictError(f"invalid {label}")
    return normalized


def _payload_digest(value: str) -> str:
    normalized = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ConflictError("payload_digest must be a SHA-256 hex digest")
    return normalized


def _raw_ref(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    # Only an opaque identifier is accepted. URLs, JSON and inline content could
    # accidentally persist credentials or customer PII.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", normalized) or "//" in normalized:
        raise ConflictError("raw_payload_ref must be an opaque non-content reference")
    return normalized


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteRepository:
    """Durable DEMO adapter. Every aggregate query includes a tenant predicate.

    One instance/connection is owned by one thread. Concurrency uses independent
    instances; ``_depth`` tracks savepoints only within that connection.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._depth = 0
        try:
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def save_demo_control(self, value: DemoExecutionControl) -> None:
        self.connection.execute('''INSERT INTO demo_execution_controls VALUES (?,?,?,?,?)
            ON CONFLICT(tenant_id,command_id) DO UPDATE SET policy_version=excluded.policy_version,
            target_version=excluded.target_version,stopped=excluded.stopped''',
            (value.tenant_id, value.command_id, value.policy_version, value.target_version, int(value.stopped)))

    def get_demo_control(self, tenant_id: str, command_id: str) -> DemoExecutionControl:
        row = self.connection.execute('SELECT * FROM demo_execution_controls WHERE tenant_id=? AND command_id=?', (tenant_id, command_id)).fetchone()
        if row is None:
            raise NotFoundError('demo execution control missing')
        return DemoExecutionControl(tenant_id, command_id, row['policy_version'], row['target_version'], bool(row['stopped']))

    @staticmethod
    def _attempt(row: sqlite3.Row) -> ExecutionAttempt:
        return ExecutionAttempt(row['id'], row['tenant_id'], row['command_id'], row['preparation_id'], row['operation_key'],
            row['intent_digest'], row['adapter_version'], row['provider'], row['connection_id'], AttemptState(row['state']),
            row['version'], row['lease_owner'], _dt(row['lease_until']), row['fencing_token'], row['provider_reference'],
            _dt(row['last_observed_at']), _dt(row['next_check_at']))

    def get_attempt(self, tenant_id: str, attempt_id: str) -> ExecutionAttempt:
        row = self.connection.execute('SELECT * FROM execution_attempts WHERE tenant_id=? AND id=?', (tenant_id, attempt_id)).fetchone()
        if row is None:
            raise NotFoundError('attempt not found')
        return self._attempt(row)

    def attempt_for_key(self, tenant_id: str, operation_key: str) -> ExecutionAttempt | None:
        row = self.connection.execute('SELECT * FROM execution_attempts WHERE tenant_id=? AND operation_key=?', (tenant_id, operation_key)).fetchone()
        return self._attempt(row) if row else None

    def insert_attempt(self, value: ExecutionAttempt) -> None:
        self.connection.execute('INSERT INTO execution_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (value.id, value.tenant_id, value.command_id, value.preparation_id, value.operation_key, value.intent_digest,
             value.adapter_version, value.provider, value.connection_id, value.state.value, value.version,
             value.lease_owner, value.lease_until.isoformat() if value.lease_until else None, value.fencing_token,
             value.provider_reference, value.last_observed_at.isoformat() if value.last_observed_at else None,
             value.next_check_at.isoformat() if value.next_check_at else None))

    def update_attempt(self, value: ExecutionAttempt, expected_version: int) -> None:
        if value.version != expected_version + 1:
            raise ConflictError('attempt version conflict')
        changed = self.connection.execute('''UPDATE execution_attempts SET state=?,version=?,lease_owner=?,lease_until=?,
            fencing_token=?,provider_reference=?,last_observed_at=?,next_check_at=? WHERE tenant_id=? AND id=? AND version=?''',
            (value.state.value, value.version, value.lease_owner, value.lease_until.isoformat() if value.lease_until else None,
             value.fencing_token, value.provider_reference, value.last_observed_at.isoformat() if value.last_observed_at else None,
             value.next_check_at.isoformat() if value.next_check_at else None, value.tenant_id, value.id, expected_version)).rowcount
        if changed != 1:
            raise ConflictError('attempt version conflict')

    def append_observation(self, value: AttemptObservation) -> None:
        self.connection.execute('INSERT INTO attempt_observations VALUES (?,?,?,?,?,?,?)',
            (value.id, value.tenant_id, value.attempt_id, value.observation_kind, value.response_digest,
             value.observed_at.isoformat(), value.correlation_id))

    def observations_for(self, tenant_id: str, attempt_id: str) -> tuple[AttemptObservation, ...]:
        self.get_attempt(tenant_id, attempt_id)
        return tuple(AttemptObservation(row['id'], tenant_id, attempt_id, row['observation_kind'], row['response_digest'],
            _dt(row['observed_at']), row['correlation_id']) for row in self.connection.execute(
            'SELECT * FROM attempt_observations WHERE tenant_id=? AND attempt_id=? ORDER BY rowid', (tenant_id, attempt_id)))

    def save_approval_intent(self, intent: ApprovalIntent) -> None:
        self.connection.execute('INSERT INTO approval_intents VALUES (?,?,?,?,?,?)',
            (intent.tenant_id, intent.command_id, intent.canonical_digest, intent.policy_version, intent.target_version, intent.created_at.isoformat()))

    def get_approval_intent(self, tenant_id: str, command_id: str) -> ApprovalIntent | None:
        row = self.connection.execute('SELECT * FROM approval_intents WHERE tenant_id=? AND command_id=?', (tenant_id, command_id)).fetchone()
        return ApprovalIntent(tenant_id, command_id, row['canonical_digest'], row['policy_version'], row['target_version'], _dt(row['created_at'])) if row else None

    def save_execution_preparation(self, value: ExecutionPreparation) -> None:
        self.connection.execute('INSERT INTO execution_preparations VALUES (?,?,?,?,?,?)',
            (value.id, value.tenant_id, value.command_id, value.canonical_digest, value.prepared_by, value.prepared_at.isoformat()))

    def get_execution_preparation(self, tenant_id: str, command_id: str) -> ExecutionPreparation | None:
        row = self.connection.execute('SELECT * FROM execution_preparations WHERE tenant_id=? AND command_id=?', (tenant_id, command_id)).fetchone()
        return ExecutionPreparation(row['id'], tenant_id, command_id, row['canonical_digest'], row['prepared_by'], _dt(row['prepared_at'])) if row else None

    def save_adapter_manifest(self, manifest: AdapterCapabilityManifest) -> None:
        self.connection.execute('''INSERT INTO adapter_capability_manifests VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id,provider,connection_id) DO UPDATE SET
            adapter_version=excluded.adapter_version,capabilities_json=excluded.capabilities_json,
            inbound_schema_versions_json=excluded.inbound_schema_versions_json,updated_at=excluded.updated_at''',
            (manifest.tenant_id, manifest.provider, manifest.connection_id, manifest.adapter_version,
             json.dumps(sorted(c.value for c in manifest.capabilities)), json.dumps(sorted(manifest.inbound_schema_versions)), manifest.updated_at.isoformat()))

    def get_adapter_manifest(self, tenant_id: str, provider: str, connection_id: str) -> AdapterCapabilityManifest:
        row = self.connection.execute('SELECT * FROM adapter_capability_manifests WHERE tenant_id=? AND provider=? AND connection_id=?', (tenant_id, provider, connection_id)).fetchone()
        if row is None:
            raise NotFoundError('adapter manifest not found')
        return AdapterCapabilityManifest(tenant_id, provider, connection_id, row['adapter_version'],
            frozenset(AdapterCapability(c) for c in json.loads(row['capabilities_json'])),
            frozenset(json.loads(row['inbound_schema_versions_json'])), _dt(row['updated_at']))

    @staticmethod
    def _inbox(row: sqlite3.Row) -> InboxMessage:
        return InboxMessage(row['id'], row['tenant_id'], row['provider'], row['connection_id'], row['external_event_id'],
            row['schema_version'], _dt(row['received_at']), row['payload_digest'], row['raw_payload_ref'],
            InboxState(row['state']), row['version'], _dt(row['processed_at']))

    def receive_inbox(self, message: InboxMessage) -> tuple[InboxMessage, bool]:
        with self.transaction():
            row = self.connection.execute('SELECT * FROM inbox_messages WHERE tenant_id=? AND provider=? AND connection_id=? AND external_event_id=?',
                (message.tenant_id, message.provider, message.connection_id, message.external_event_id)).fetchone()
            if row:
                prior = self._inbox(row)
                if (prior.payload_digest, prior.schema_version) != (message.payload_digest, message.schema_version):
                    raise ConflictError('inbound identity reused with different content')
                return prior, True
            self.connection.execute('INSERT INTO inbox_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (message.id, message.tenant_id, message.provider, message.connection_id, message.external_event_id,
                 message.schema_version, message.received_at.isoformat(), message.payload_digest, message.raw_payload_ref,
                 message.state.value, message.version, message.processed_at.isoformat() if message.processed_at else None))
            return self.get_inbox(message.tenant_id, message.id), False

    def get_inbox(self, tenant_id: str, inbox_id: str) -> InboxMessage:
        row = self.connection.execute('SELECT * FROM inbox_messages WHERE tenant_id=? AND id=?', (tenant_id, inbox_id)).fetchone()
        if row is None:
            raise NotFoundError('inbox message not found')
        return self._inbox(row)

    def inbox_for(self, tenant_id: str) -> tuple[InboxMessage, ...]:
        return tuple(self._inbox(row) for row in self.connection.execute('SELECT * FROM inbox_messages WHERE tenant_id=? ORDER BY received_at,id', (tenant_id,)))

    def mark_inbox_processed(self, tenant_id: str, inbox_id: str, expected_version: int, processed_at: datetime) -> InboxMessage:
        with self.transaction():
            self.get_inbox(tenant_id, inbox_id)
            changed = self.connection.execute('UPDATE inbox_messages SET state=?,version=version+1,processed_at=? WHERE tenant_id=? AND id=? AND version=? AND state=?',
                (InboxState.PROCESSED.value, processed_at.isoformat(), tenant_id, inbox_id, expected_version, InboxState.RECEIVED.value)).rowcount
            if changed != 1:
                raise ConflictError('inbox state/version conflict')
            return self.get_inbox(tenant_id, inbox_id)

    def save_normalized_payload(self, value: NormalizedInboundPayload) -> NormalizedInboundPayload:
        _payload_digest(value.canonical_digest)
        if value.source_digest is not None:
            _payload_digest(value.source_digest)
        row = self.connection.execute(
            "SELECT * FROM normalized_inbound_payloads WHERE tenant_id=? AND immutable_ref=?",
            (value.tenant_id, value.immutable_ref),
        ).fetchone()
        if row:
            prior = self._normalized_payload(row)
            if (prior.canonical_digest, prior.payload_json, prior.schema_version, prior.source_digest) != (
                value.canonical_digest, value.payload_json, value.schema_version, value.source_digest):
                raise ConflictError("immutable payload reference reused with different content")
            return prior
        try:
            self.connection.execute(
                "INSERT INTO normalized_inbound_payloads VALUES (?,?,?,?,?,?,?)",
                (value.tenant_id, value.immutable_ref, value.canonical_digest, value.schema_version,
                 value.payload_json, value.source_digest, value.created_at.isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("normalized payload could not be stored") from exc
        return value

    @staticmethod
    def _normalized_payload(row: sqlite3.Row) -> NormalizedInboundPayload:
        return NormalizedInboundPayload(row['tenant_id'], row['immutable_ref'], row['canonical_digest'],
            row['schema_version'], row['payload_json'], row['source_digest'], _dt(row['created_at']))

    def get_normalized_payload(self, tenant_id: str, immutable_ref: str) -> NormalizedInboundPayload:
        row = self.connection.execute(
            "SELECT * FROM normalized_inbound_payloads WHERE tenant_id=? AND immutable_ref=?",
            (tenant_id, immutable_ref),
        ).fetchone()
        if row is None:
            raise NotFoundError('normalized payload not found')
        return self._normalized_payload(row)

    def normalized_payloads_for(self, tenant_id: str) -> tuple[NormalizedInboundPayload, ...]:
        return tuple(self._normalized_payload(row) for row in self.connection.execute(
            "SELECT * FROM normalized_inbound_payloads WHERE tenant_id=? ORDER BY created_at,immutable_ref",
            (tenant_id,),
        ))

    def get_poll_checkpoint(self, tenant_id: str, provider: str, connection_id: str) -> AdapterPollCheckpoint | None:
        row = self.connection.execute(
            "SELECT * FROM adapter_poll_checkpoints WHERE tenant_id=? AND provider=? AND connection_id=?",
            (tenant_id, provider, connection_id),
        ).fetchone()
        if row is None:
            return None
        return AdapterPollCheckpoint(tenant_id, provider, connection_id, row['adapter_version'], row['cursor'],
            _dt(row['overlap_from']), row['version'], _dt(row['updated_at']), _dt(row['last_success_at']))

    def insert_or_advance_poll_checkpoint(self, value: AdapterPollCheckpoint, expected_version: int) -> AdapterPollCheckpoint:
        prior = self.get_poll_checkpoint(value.tenant_id, value.provider, value.connection_id)
        actual = prior.version if prior else 0
        if actual != expected_version or value.version != expected_version + 1:
            raise ConflictError('adapter poll checkpoint version conflict')
        if prior is None:
            try:
                self.connection.execute(
                    "INSERT INTO adapter_poll_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
                    (value.tenant_id, value.provider, value.connection_id, value.adapter_version, value.cursor,
                     value.overlap_from.isoformat() if value.overlap_from else None, value.version,
                     value.updated_at.isoformat(), value.last_success_at.isoformat() if value.last_success_at else None),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError('adapter poll checkpoint insert conflict') from exc
        else:
            changed = self.connection.execute(
                "UPDATE adapter_poll_checkpoints SET adapter_version=?,cursor=?,overlap_from=?,version=?,updated_at=?,last_success_at=? "
                "WHERE tenant_id=? AND provider=? AND connection_id=? AND version=?",
                (value.adapter_version, value.cursor, value.overlap_from.isoformat() if value.overlap_from else None,
                 value.version, value.updated_at.isoformat(), value.last_success_at.isoformat() if value.last_success_at else None,
                 value.tenant_id, value.provider, value.connection_id, expected_version),
            ).rowcount
            if changed != 1:
                raise ConflictError('adapter poll checkpoint version conflict')
        return value

    @staticmethod
    def _channel_order(row: sqlite3.Row) -> ChannelOrder:
        return ChannelOrder(row['id'], row['tenant_id'], row['channel_id'], row['external_order_key'], row['payload_ref'],
            row['currency'], row['total_minor'], ChannelOrderState(row['status']), _dt(row['received_at']),
            row['idempotency_key'], row['version'])

    def save_channel_order(self, value: ChannelOrder) -> tuple[ChannelOrder, bool]:
        row = self.connection.execute(
            "SELECT * FROM channel_orders WHERE tenant_id=? AND (id=? OR (channel_id=? AND external_order_key=?))",
            (value.tenant_id, value.id, value.channel_id, value.external_order_key)).fetchone()
        if row:
            prior = self._channel_order(row)
            if (prior.channel_id, prior.external_order_key, prior.payload_ref, prior.total_minor, prior.currency) != (value.channel_id, value.external_order_key, value.payload_ref, value.total_minor, value.currency):
                raise ConflictError('order identity reused with different content')
            return prior, True
        try:
            self.connection.execute("INSERT INTO channel_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (value.id, value.tenant_id, value.channel_id, value.external_order_key, value.payload_ref,
                 value.currency, value.total_minor, value.status.value, value.received_at.isoformat(), value.idempotency_key, value.version))
        except sqlite3.IntegrityError as exc:
            raise ConflictError('order idempotency key already exists') from exc
        return value, False

    def get_channel_order(self, tenant_id: str, order_id: str) -> ChannelOrder:
        row = self.connection.execute("SELECT * FROM channel_orders WHERE tenant_id=? AND id=?", (tenant_id, order_id)).fetchone()
        if row is None: raise NotFoundError('order not found')
        return self._channel_order(row)

    def find_channel_order(self, tenant_id: str, channel_id: str, external_key: str) -> ChannelOrder | None:
        row = self.connection.execute("SELECT * FROM channel_orders WHERE tenant_id=? AND channel_id=? AND external_order_key=?", (tenant_id, channel_id, external_key)).fetchone()
        return self._channel_order(row) if row else None

    def update_channel_order(self, value: ChannelOrder, expected_version: int) -> None:
        if value.version != expected_version + 1: raise ConflictError('order version conflict')
        changed = self.connection.execute("UPDATE channel_orders SET status=?,version=? WHERE tenant_id=? AND id=? AND version=?",
            (value.status.value, value.version, value.tenant_id, value.id, expected_version)).rowcount
        if changed != 1: raise ConflictError('order version conflict')

    def save_order_line(self, value: OrderLine) -> None:
        self.connection.execute("INSERT INTO order_lines (id,tenant_id,channel_order_id,sku,quantity,unit_minor,routed_status,version,tracking_key,tracking_status,tracking_version,tracking_observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (value.id, value.tenant_id, value.channel_order_id, value.sku, value.quantity, value.unit_minor, value.routed_status, value.version,
             value.tracking_key, value.tracking_status, value.tracking_version,
             value.tracking_observed_at.isoformat() if value.tracking_observed_at else None))

    def update_order_line(self, value: OrderLine, expected_version: int) -> None:
        if value.version != expected_version + 1: raise ConflictError('order line version conflict')
        changed = self.connection.execute("UPDATE order_lines SET routed_status=?,version=?,tracking_key=?,tracking_status=?,tracking_version=?,tracking_observed_at=? WHERE tenant_id=? AND id=? AND version=?",
            (value.routed_status, value.version, value.tracking_key, value.tracking_status, value.tracking_version,
             value.tracking_observed_at.isoformat() if value.tracking_observed_at else None,
             value.tenant_id, value.id, expected_version)).rowcount
        if changed != 1: raise ConflictError('order line version conflict')

    @staticmethod
    def _order_line(row: sqlite3.Row) -> OrderLine:
        return OrderLine(row['id'], row['tenant_id'], row['channel_order_id'], row['sku'], row['quantity'], row['unit_minor'], row['routed_status'], row['version'], row['tracking_key'], row['tracking_status'], row['tracking_version'], _dt(row['tracking_observed_at']))

    def order_lines_for(self, tenant_id: str, order_id: str) -> tuple[OrderLine, ...]:
        self.get_channel_order(tenant_id, order_id)
        return tuple(self._order_line(row) for row in self.connection.execute(
            "SELECT * FROM order_lines WHERE tenant_id=? AND channel_order_id=? ORDER BY rowid", (tenant_id, order_id)))

    def get_order_line(self, tenant_id: str, line_id: str) -> OrderLine:
        row = self.connection.execute("SELECT * FROM order_lines WHERE tenant_id=? AND id=?", (tenant_id, line_id)).fetchone()
        if row is None: raise NotFoundError('order line not found')
        return self._order_line(row)

    def save_tracking_observation(self, value: TrackingObservation) -> TrackingObservation:
        row = self.connection.execute("SELECT * FROM tracking_observations WHERE tenant_id=? AND order_line_id=? AND tracking_key=? AND status=?",
            (value.tenant_id, value.order_line_id, value.tracking_key, value.status)).fetchone()
        if row:
            prior = TrackingObservation(row['id'], row['tenant_id'], row['order_line_id'], row['tracking_key'], row['status'], _dt(row['observed_at']), row['response_digest'])
            if prior.response_digest != value.response_digest: raise ConflictError('tracking identity reused with different content')
            return prior
        try:
            self.connection.execute("INSERT INTO tracking_observations VALUES (?,?,?,?,?,?,?)",
                (value.id, value.tenant_id, value.order_line_id, value.tracking_key, value.status, value.observed_at.isoformat(), value.response_digest))
        except sqlite3.IntegrityError as exc: raise ConflictError('tracking observation already exists') from exc
        return value

    def tracking_observation_for(self, tenant_id: str, line_id: str, tracking_key: str, status: str) -> TrackingObservation | None:
        row = self.connection.execute("SELECT * FROM tracking_observations WHERE tenant_id=? AND order_line_id=? AND tracking_key=? AND status=?", (tenant_id, line_id, tracking_key, status)).fetchone()
        return TrackingObservation(row['id'], row['tenant_id'], row['order_line_id'], row['tracking_key'], row['status'], _dt(row['observed_at']), row['response_digest']) if row else None

    def tracking_for(self, tenant_id: str, line_id: str) -> tuple[TrackingObservation, ...]:
        self.get_order_line(tenant_id, line_id)
        return tuple(TrackingObservation(row['id'], row['tenant_id'], row['order_line_id'], row['tracking_key'], row['status'], _dt(row['observed_at']), row['response_digest']) for row in self.connection.execute("SELECT * FROM tracking_observations WHERE tenant_id=? AND order_line_id=? ORDER BY rowid", (tenant_id, line_id)))

    @staticmethod
    def _claim(row: sqlite3.Row) -> DemoClaim:
        return DemoClaim(row['id'], row['tenant_id'], row['channel_order_id'], row['claim_type'], row['amount_minor'], ClaimStatus(row['consumer_status']), ClaimStatus(row['channel_status']), ClaimStatus(row['supplier_status']), row['idempotency_key'], _dt(row['created_at']), row['version'])

    def save_claim(self, value: DemoClaim) -> tuple[DemoClaim, bool]:
        row = self.connection.execute("SELECT * FROM demo_claims WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = self._claim(row)
            if (prior.channel_order_id, prior.claim_type, prior.amount_minor) != (value.channel_order_id, value.claim_type, value.amount_minor): raise ConflictError('claim idempotency key reused')
            return prior, True
        try:
            self.connection.execute("INSERT INTO demo_claims VALUES (?,?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.channel_order_id, value.claim_type, value.amount_minor, value.consumer_status.value, value.channel_status.value, value.supplier_status.value, value.idempotency_key, value.created_at.isoformat(), value.version))
        except sqlite3.IntegrityError as exc: raise ConflictError('claim idempotency key already exists') from exc
        return value, False

    def get_claim(self, tenant_id: str, claim_id: str) -> DemoClaim:
        row = self.connection.execute("SELECT * FROM demo_claims WHERE tenant_id=? AND id=?", (tenant_id, claim_id)).fetchone()
        if row is None: raise NotFoundError('claim not found')
        return self._claim(row)

    def update_claim(self, value: DemoClaim, expected_version: int) -> None:
        if value.version != expected_version + 1: raise ConflictError('claim version conflict')
        changed = self.connection.execute("UPDATE demo_claims SET consumer_status=?,channel_status=?,supplier_status=?,version=? WHERE tenant_id=? AND id=? AND version=?", (value.consumer_status.value, value.channel_status.value, value.supplier_status.value, value.version, value.tenant_id, value.id, expected_version)).rowcount
        if changed != 1: raise ConflictError('claim version conflict')

    def save_claim_observation(self, value: ClaimStatusObservation) -> ClaimStatusObservation:
        row = self.connection.execute("SELECT * FROM claim_status_observations WHERE tenant_id=? AND claim_id=? AND status_kind=? AND status=?", (value.tenant_id, value.claim_id, value.status_kind, value.status.value)).fetchone()
        if row:
            prior = ClaimStatusObservation(row['id'], row['tenant_id'], row['claim_id'], row['status_kind'], ClaimStatus(row['status']), _dt(row['observed_at']), row['response_digest'])
            if prior.response_digest != value.response_digest: raise ConflictError('claim status reused with different content')
            return prior
        try:
            self.connection.execute("INSERT INTO claim_status_observations VALUES (?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.claim_id, value.status_kind, value.status.value, value.observed_at.isoformat(), value.response_digest))
        except sqlite3.IntegrityError as exc: raise ConflictError('claim status observation already exists') from exc
        return value

    def claim_observation_for(self, tenant_id: str, claim_id: str, status_kind: str, status: ClaimStatus) -> ClaimStatusObservation | None:
        row = self.connection.execute("SELECT * FROM claim_status_observations WHERE tenant_id=? AND claim_id=? AND status_kind=? AND status=?", (tenant_id, claim_id, status_kind, status.value)).fetchone()
        return ClaimStatusObservation(row['id'], row['tenant_id'], row['claim_id'], row['status_kind'], ClaimStatus(row['status']), _dt(row['observed_at']), row['response_digest']) if row else None

    def claim_observations_for(self, tenant_id: str, claim_id: str) -> tuple[ClaimStatusObservation, ...]:
        self.get_claim(tenant_id, claim_id)
        return tuple(ClaimStatusObservation(row['id'], row['tenant_id'], row['claim_id'], row['status_kind'], ClaimStatus(row['status']), _dt(row['observed_at']), row['response_digest']) for row in self.connection.execute("SELECT * FROM claim_status_observations WHERE tenant_id=? AND claim_id=? ORDER BY rowid", (tenant_id, claim_id)))

    @staticmethod
    def _catalog_import(row: sqlite3.Row) -> DemoCatalogImport:
        return DemoCatalogImport(row['id'], row['tenant_id'], row['supplier_id'], row['source_digest'], row['idempotency_key'], _dt(row['created_at']))

    def save_catalog_import(self, value: DemoCatalogImport) -> tuple[DemoCatalogImport, bool]:
        row = self.connection.execute("SELECT * FROM demo_catalog_imports WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = self._catalog_import(row)
            if (prior.source_digest, prior.supplier_id) != (value.source_digest, value.supplier_id): raise ConflictError('catalog idempotency key reused')
            return prior, True
        try: self.connection.execute("INSERT INTO demo_catalog_imports VALUES (?,?,?,?,?,?)", (value.id, value.tenant_id, value.supplier_id, value.source_digest, value.idempotency_key, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('catalog import already exists') from exc
        return value, False

    def get_catalog_import(self, tenant_id: str, import_id: str) -> DemoCatalogImport:
        row = self.connection.execute("SELECT * FROM demo_catalog_imports WHERE tenant_id=? AND id=?", (tenant_id, import_id)).fetchone()
        if row is None: raise NotFoundError('catalog import not found')
        return self._catalog_import(row)

    @staticmethod
    def _catalog_snapshot(row: sqlite3.Row) -> DemoCatalogSnapshot:
        return DemoCatalogSnapshot(row['id'], row['tenant_id'], row['import_id'], row['supplier_id'], row['external_key'], row['source_digest'], row['payload_json'], _dt(row['created_at']))

    def save_catalog_snapshot(self, value: DemoCatalogSnapshot) -> DemoCatalogSnapshot:
        try: self.connection.execute("INSERT INTO demo_catalog_snapshots VALUES (?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.import_id, value.supplier_id, value.external_key, value.source_digest, value.payload_json, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc:
            row = self.connection.execute("SELECT * FROM demo_catalog_snapshots WHERE tenant_id=? AND id=?", (value.tenant_id, value.id)).fetchone()
            if row and row['payload_json'] == value.payload_json: return self._catalog_snapshot(row)
            raise ConflictError('catalog snapshot already exists') from exc
        return value

    def catalog_snapshots_for(self, tenant_id: str, import_id: str) -> tuple[DemoCatalogSnapshot, ...]:
        self.get_catalog_import(tenant_id, import_id)
        return tuple(self._catalog_snapshot(row) for row in self.connection.execute("SELECT * FROM demo_catalog_snapshots WHERE tenant_id=? AND import_id=? ORDER BY rowid", (tenant_id, import_id)))

    @staticmethod
    def _canonical_product(row: sqlite3.Row) -> DemoCanonicalProduct:
        return DemoCanonicalProduct(row['id'], row['tenant_id'], row['sku'], row['title'], row['category'], row['price_minor'], row['currency'], row['attributes_json'], row['source_snapshot_id'], row['version'], _dt(row['created_at']))

    def save_canonical_product(self, value: DemoCanonicalProduct) -> DemoCanonicalProduct:
        row = self.connection.execute("SELECT * FROM demo_canonical_products WHERE tenant_id=? AND sku=?", (value.tenant_id, value.sku)).fetchone()
        if row:
            prior = self._canonical_product(row)
            if (prior.title, prior.category, prior.price_minor, prior.currency, prior.attributes_json) != (value.title, value.category, value.price_minor, value.currency, value.attributes_json): raise ConflictError('canonical SKU reused with different content')
            return prior
        try: self.connection.execute("INSERT INTO demo_canonical_products VALUES (?,?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.sku, value.title, value.category, value.price_minor, value.currency, value.attributes_json, value.source_snapshot_id, value.version, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('canonical product already exists') from exc
        return value

    def get_canonical_product(self, tenant_id: str, product_id: str) -> DemoCanonicalProduct:
        row = self.connection.execute("SELECT * FROM demo_canonical_products WHERE tenant_id=? AND id=?", (tenant_id, product_id)).fetchone()
        if row is None: raise NotFoundError('canonical product not found')
        return self._canonical_product(row)

    def save_product_lineage(self, value: DemoProductLineage) -> DemoProductLineage:
        row = self.connection.execute("SELECT * FROM demo_product_lineage WHERE tenant_id=? AND source_snapshot_id=?", (value.tenant_id, value.source_snapshot_id)).fetchone()
        if row:
            prior = DemoProductLineage(row['id'], row['tenant_id'], row['source_snapshot_id'], row['canonical_product_id'], row['transform_version'], _dt(row['created_at']))
            if prior.canonical_product_id != value.canonical_product_id: raise ConflictError('catalog lineage conflict')
            return prior
        try: self.connection.execute("INSERT INTO demo_product_lineage VALUES (?,?,?,?,?,?)", (value.id, value.tenant_id, value.source_snapshot_id, value.canonical_product_id, value.transform_version, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('catalog lineage already exists') from exc
        return value

    def lineage_for(self, tenant_id: str, product_id: str) -> tuple[DemoProductLineage, ...]:
        self.get_canonical_product(tenant_id, product_id)
        return tuple(DemoProductLineage(row['id'], row['tenant_id'], row['source_snapshot_id'], row['canonical_product_id'], row['transform_version'], _dt(row['created_at'])) for row in self.connection.execute("SELECT * FROM demo_product_lineage WHERE tenant_id=? AND canonical_product_id=? ORDER BY rowid", (tenant_id, product_id)))

    @staticmethod
    def _channel_offer(row: sqlite3.Row) -> DemoChannelOffer:
        return DemoChannelOffer(row['id'], row['tenant_id'], row['channel_id'], row['canonical_product_id'], row['source_snapshot_id'], row['external_key'], row['price_minor'], row['currency'], row['version'], _dt(row['created_at']))

    def save_channel_offer(self, value: DemoChannelOffer) -> tuple[DemoChannelOffer, bool]:
        row = self.connection.execute("SELECT * FROM demo_channel_offers WHERE tenant_id=? AND channel_id=? AND canonical_product_id=?", (value.tenant_id, value.channel_id, value.canonical_product_id)).fetchone()
        if row:
            prior = self._channel_offer(row)
            if (prior.price_minor, prior.currency) != (value.price_minor, value.currency): raise ConflictError('channel offer already exists with different content')
            return prior, True
        try: self.connection.execute("INSERT INTO demo_channel_offers VALUES (?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.channel_id, value.canonical_product_id, value.source_snapshot_id, value.external_key, value.price_minor, value.currency, value.version, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('channel offer already exists') from exc
        return value, False

    def get_channel_offer(self, tenant_id: str, offer_id: str) -> DemoChannelOffer:
        row = self.connection.execute("SELECT * FROM demo_channel_offers WHERE tenant_id=? AND id=?", (tenant_id, offer_id)).fetchone()
        if row is None: raise NotFoundError('channel offer not found')
        return self._channel_offer(row)

    def channel_offers_for(self, tenant_id: str, product_id: str) -> tuple[DemoChannelOffer, ...]:
        self.get_canonical_product(tenant_id, product_id)
        return tuple(self._channel_offer(row) for row in self.connection.execute("SELECT * FROM demo_channel_offers WHERE tenant_id=? AND canonical_product_id=? ORDER BY rowid", (tenant_id, product_id)))

    @staticmethod
    def _settlement_batch(row: sqlite3.Row) -> DemoSettlementBatch:
        return DemoSettlementBatch(row['id'], row['tenant_id'], row['channel_id'], row['period'], row['source_digest'], SettlementStatus(row['status']), row['idempotency_key'], _dt(row['created_at']), row['version'])

    def save_settlement_batch(self, value: DemoSettlementBatch) -> tuple[DemoSettlementBatch, bool]:
        row = self.connection.execute("SELECT * FROM demo_settlement_batches WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = self._settlement_batch(row)
            if prior.source_digest != value.source_digest: raise ConflictError('settlement idempotency key reused')
            return prior, True
        try: self.connection.execute("INSERT INTO demo_settlement_batches VALUES (?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.channel_id, value.period, value.source_digest, value.status.value, value.idempotency_key, value.created_at.isoformat(), value.version))
        except sqlite3.IntegrityError as exc: raise ConflictError('settlement batch already exists') from exc
        return value, False

    def get_settlement_batch(self, tenant_id: str, batch_id: str) -> DemoSettlementBatch:
        row = self.connection.execute("SELECT * FROM demo_settlement_batches WHERE tenant_id=? AND id=?", (tenant_id, batch_id)).fetchone()
        if row is None: raise NotFoundError('settlement batch not found')
        return self._settlement_batch(row)

    def update_settlement_batch(self, value: DemoSettlementBatch, expected_version: int) -> None:
        if value.version != expected_version + 1: raise ConflictError('settlement version conflict')
        changed = self.connection.execute("UPDATE demo_settlement_batches SET status=?,version=? WHERE tenant_id=? AND id=? AND version=?", (value.status.value, value.version, value.tenant_id, value.id, expected_version)).rowcount
        if changed != 1: raise ConflictError('settlement version conflict')

    def save_settlement_line(self, value: DemoSettlementLine) -> None:
        self.connection.execute("INSERT INTO demo_settlement_lines VALUES (?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.batch_id, value.external_order_key, value.kind, value.amount_minor, value.currency, value.source_row_ref, value.order_id, value.match_status))

    def settlement_lines_for(self, tenant_id: str, batch_id: str) -> tuple[DemoSettlementLine, ...]:
        self.get_settlement_batch(tenant_id, batch_id)
        return tuple(DemoSettlementLine(row['id'], row['tenant_id'], row['batch_id'], row['external_order_key'], row['kind'], row['amount_minor'], row['currency'], row['source_row_ref'], row['order_id'], row['match_status']) for row in self.connection.execute("SELECT * FROM demo_settlement_lines WHERE tenant_id=? AND batch_id=? ORDER BY rowid", (tenant_id, batch_id)))

    def save_realized_profit(self, value: DemoRealizedProfit) -> None:
        self.connection.execute("INSERT OR REPLACE INTO demo_realized_profit VALUES (?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.batch_id, value.order_id, value.projected_minor, value.realized_minor, value.status, value.calculated_at.isoformat()))

    def realized_profits_for(self, tenant_id: str, batch_id: str) -> tuple[DemoRealizedProfit, ...]:
        self.get_settlement_batch(tenant_id, batch_id)
        return tuple(DemoRealizedProfit(row['id'], row['tenant_id'], row['batch_id'], row['order_id'], row['projected_minor'], row['realized_minor'], row['status'], _dt(row['calculated_at'])) for row in self.connection.execute("SELECT * FROM demo_realized_profit WHERE tenant_id=? AND batch_id=? ORDER BY rowid", (tenant_id, batch_id)))

    def save_routing_decision(self, value: RoutingDecision) -> None:
        try:
            self.connection.execute("INSERT INTO routing_decisions VALUES (?,?,?,?,?,?,?,?)",
                (value.id, value.tenant_id, value.order_line_id, value.supplier_id, value.quantity,
                 value.unit_cost_minor, value.reason, value.status.value))
        except sqlite3.IntegrityError as exc:
            raise ConflictError('routing decision already exists') from exc

    @staticmethod
    def _routing(row: sqlite3.Row) -> RoutingDecision:
        return RoutingDecision(row['id'], row['tenant_id'], row['order_line_id'], row['supplier_id'], row['quantity'], row['unit_cost_minor'], row['reason'], RoutingState(row['status']))

    def routing_for(self, tenant_id: str, order_id: str) -> tuple[RoutingDecision, ...]:
        return tuple(self._routing(row) for row in self.connection.execute(
            "SELECT r.* FROM routing_decisions r JOIN order_lines l ON l.tenant_id=r.tenant_id AND l.id=r.order_line_id WHERE r.tenant_id=? AND l.channel_order_id=? ORDER BY r.rowid", (tenant_id, order_id)))

    @staticmethod
    def _purchase_order(row: sqlite3.Row) -> SupplierPurchaseOrder:
        return SupplierPurchaseOrder(row['id'], row['tenant_id'], row['channel_order_id'], row['supplier_id'], PurchaseOrderState(row['status']), row['idempotency_key'], row['approval_command_id'], _dt(row['created_at']), row['version'], row['provider_reference'], row['last_response_digest'], _dt(row['last_observed_at']))

    def save_purchase_order(self, value: SupplierPurchaseOrder) -> tuple[SupplierPurchaseOrder, bool]:
        row = self.connection.execute("SELECT * FROM supplier_purchase_orders WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = self._purchase_order(row)
            if (prior.channel_order_id, prior.supplier_id) != (value.channel_order_id, value.supplier_id):
                raise ConflictError('purchase idempotency key reused')
            return prior, True
        try:
            self.connection.execute("INSERT INTO supplier_purchase_orders (id,tenant_id,channel_order_id,supplier_id,status,idempotency_key,approval_command_id,created_at,version) VALUES (?,?,?,?,?,?,?,?,?)",
                (value.id, value.tenant_id, value.channel_order_id, value.supplier_id, value.status.value,
                 value.idempotency_key, value.approval_command_id, value.created_at.isoformat(), value.version))
        except sqlite3.IntegrityError as exc:
            raise ConflictError('purchase order already exists') from exc
        return value, False

    def update_purchase_order(self, value: SupplierPurchaseOrder, expected_version: int) -> None:
        if value.version != expected_version + 1: raise ConflictError('purchase order version conflict')
        changed = self.connection.execute("UPDATE supplier_purchase_orders SET status=?,version=?,provider_reference=?,last_response_digest=?,last_observed_at=? WHERE tenant_id=? AND id=? AND version=?",
            (value.status.value, value.version, value.provider_reference, value.last_response_digest,
             value.last_observed_at.isoformat() if value.last_observed_at else None,
             value.tenant_id, value.id, expected_version)).rowcount
        if changed != 1: raise ConflictError('purchase order version conflict')

    def get_purchase_order(self, tenant_id: str, po_id: str) -> SupplierPurchaseOrder:
        row = self.connection.execute("SELECT * FROM supplier_purchase_orders WHERE tenant_id=? AND id=?", (tenant_id, po_id)).fetchone()
        if row is None: raise NotFoundError('purchase order not found')
        return self._purchase_order(row)

    def save_purchase_line(self, value: PurchaseLine) -> None:
        self.connection.execute("INSERT INTO purchase_lines VALUES (?,?,?,?,?,?)",
            (value.id, value.tenant_id, value.purchase_order_id, value.order_line_id, value.quantity, value.unit_cost_minor))

    def purchase_orders_for(self, tenant_id: str, order_id: str) -> tuple[SupplierPurchaseOrder, ...]:
        self.get_channel_order(tenant_id, order_id)
        return tuple(self._purchase_order(row) for row in self.connection.execute(
            "SELECT * FROM supplier_purchase_orders WHERE tenant_id=? AND channel_order_id=? ORDER BY rowid", (tenant_id, order_id)))

    def purchase_lines_for(self, tenant_id: str, po_id: str) -> tuple[PurchaseLine, ...]:
        return tuple(PurchaseLine(row['id'], row['tenant_id'], row['purchase_order_id'], row['order_line_id'], row['quantity'], row['unit_cost_minor']) for row in self.connection.execute(
            "SELECT * FROM purchase_lines WHERE tenant_id=? AND purchase_order_id=? ORDER BY rowid", (tenant_id, po_id)))

    def _migrate(self) -> None:
        versions = [version for version, _ in MIGRATIONS]
        if any(version <= 0 for version in versions) or versions != sorted(set(versions)):
            raise sqlite3.DatabaseError("migration versions must be strictly increasing and unique")
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {r[0] for r in self.connection.execute("SELECT version FROM schema_migrations")}
        if applied and max(applied) > LATEST_SCHEMA_VERSION:
            raise ConflictError(
                f"database schema {max(applied)} is newer than supported {LATEST_SCHEMA_VERSION}"
            )
        for version, sql in MIGRATIONS:
            if version not in applied:
                # executescript commits implicitly unless transaction control is
                # embedded in the script. Keep DDL and its version marker atomic.
                applied_at = datetime.now().astimezone().isoformat().replace("'", "''")
                script = (
                    "BEGIN IMMEDIATE;\n" + sql + "\n"
                    f"INSERT INTO schema_migrations VALUES ({version},'{applied_at}');\n"
                    "COMMIT;"
                )
                try:
                    self.connection.executescript(script)
                except Exception:
                    if self.connection.in_transaction:
                        self.connection.execute("ROLLBACK")
                    raise

    def readiness(self, expected_schema_version: int = LATEST_SCHEMA_VERSION) -> dict[str, object]:
        """Fail-closed storage readiness check with no external side effects."""
        versions = [row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")]
        actual = max(versions, default=0)
        if actual != expected_schema_version or actual > LATEST_SCHEMA_VERSION:
            raise ConflictError(
                f"schema version mismatch: expected {expected_schema_version}, found {actual}"
            )
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ConflictError(f"sqlite integrity check failed: {integrity}")
        foreign_keys = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ConflictError("sqlite foreign key check failed")
        return {"ready": True, "schema_version": actual, "integrity": integrity}

    def save_notification_preference(self, value: DemoNotificationPreference) -> DemoNotificationPreference:
        row = self.connection.execute("SELECT * FROM demo_notification_preferences WHERE tenant_id=? AND notification_key=?", (value.tenant_id, value.notification_key)).fetchone()
        if row and value.version != row['version'] + 1: raise ConflictError('notification preference version conflict')
        self.connection.execute("INSERT INTO demo_notification_preferences VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,notification_key) DO UPDATE SET channels_json=excluded.channels_json,muted=excluded.muted,version=excluded.version", (value.tenant_id, value.notification_key, json.dumps(value.channels), int(value.muted), value.version))
        return value

    def get_notification_preference(self, tenant_id: str, key: str) -> DemoNotificationPreference | None:
        row = self.connection.execute("SELECT * FROM demo_notification_preferences WHERE tenant_id=? AND notification_key=?", (tenant_id, key)).fetchone()
        return DemoNotificationPreference(row['tenant_id'], row['notification_key'], tuple(json.loads(row['channels_json'])), bool(row['muted']), row['version']) if row else None

    @staticmethod
    def _notification_delivery(row: sqlite3.Row) -> DemoNotificationDelivery:
        return DemoNotificationDelivery(row['id'], row['tenant_id'], row['notification_key'], row['channel'], row['payload_json'], row['state'], row['attempt'], row['fallback_from'], row['idempotency_key'], _dt(row['created_at']))

    def save_notification_delivery(self, value: DemoNotificationDelivery) -> tuple[DemoNotificationDelivery, bool]:
        row = self.connection.execute("SELECT * FROM demo_notification_deliveries WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = self._notification_delivery(row)
            if prior.payload_json != value.payload_json: raise ConflictError('notification idempotency key reused')
            return prior, True
        try: self.connection.execute("INSERT INTO demo_notification_deliveries VALUES (?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.notification_key, value.channel, value.payload_json, value.state, value.attempt, value.fallback_from, value.idempotency_key, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('notification delivery already exists') from exc
        return value, False

    def notification_deliveries_for(self, tenant_id: str) -> tuple[DemoNotificationDelivery, ...]:
        return tuple(self._notification_delivery(row) for row in self.connection.execute("SELECT * FROM demo_notification_deliveries WHERE tenant_id=? ORDER BY created_at,id", (tenant_id,)))

    def save_incident_acknowledgement(self, value: DemoIncidentAcknowledgement) -> tuple[DemoIncidentAcknowledgement, bool]:
        row = self.connection.execute("SELECT * FROM demo_incident_acknowledgements WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = DemoIncidentAcknowledgement(row['id'], row['tenant_id'], row['incident_id'], row['acknowledged_by'], row['note'], row['idempotency_key'], _dt(row['acknowledged_at']))
            if prior.incident_id != value.incident_id: raise ConflictError('acknowledgement idempotency key reused')
            return prior, True
        try: self.connection.execute("INSERT INTO demo_incident_acknowledgements VALUES (?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.incident_id, value.acknowledged_by, value.note, value.idempotency_key, value.acknowledged_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('incident acknowledgement already exists') from exc
        return value, False

    def acknowledgements_for(self, tenant_id: str, incident_id: str) -> tuple[DemoIncidentAcknowledgement, ...]:
        return tuple(DemoIncidentAcknowledgement(row['id'], row['tenant_id'], row['incident_id'], row['acknowledged_by'], row['note'], row['idempotency_key'], _dt(row['acknowledged_at'])) for row in self.connection.execute("SELECT * FROM demo_incident_acknowledgements WHERE tenant_id=? AND incident_id=? ORDER BY acknowledged_at,id", (tenant_id, incident_id)))

    @staticmethod
    def _tool_command(row: sqlite3.Row) -> DemoToolCommand:
        return DemoToolCommand(row['id'], row['tenant_id'], row['actor_type'], row['actor_id'], row['tool'], row['target_type'], row['target_id'], row['input_json'], row['idempotency_key'], row['requested_policy_version'], row['approval_id'], row['mode'], row['state'], row['blocked_reason'], _dt(row['created_at']))

    def save_tool_command(self, value: DemoToolCommand) -> tuple[DemoToolCommand, bool]:
        row = self.connection.execute("SELECT * FROM demo_tool_commands WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            prior = self._tool_command(row)
            if prior.input_json != value.input_json or prior.tool != value.tool: raise ConflictError('tool idempotency key reused')
            return prior, True
        try: self.connection.execute("INSERT INTO demo_tool_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.actor_type, value.actor_id, value.tool, value.target_type, value.target_id, value.input_json, value.idempotency_key, value.requested_policy_version, value.approval_id, value.mode, value.state, value.blocked_reason, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc: raise ConflictError('tool command already exists') from exc
        return value, False

    def tool_commands_for(self, tenant_id: str) -> tuple[DemoToolCommand, ...]:
        return tuple(self._tool_command(row) for row in self.connection.execute("SELECT * FROM demo_tool_commands WHERE tenant_id=? ORDER BY created_at,id", (tenant_id,)))

    @staticmethod
    def _agent_run(row: sqlite3.Row) -> DemoAgentRun:
        return DemoAgentRun(row['id'], row['tenant_id'], row['agent_id'], row['goal'], row['policy_version'], row['model'], row['prompt_version'], row['input_digest'], row['decision_json'], row['confidence'], row['tool_calls'], row['reviewer'], row['estimated_cost_minor'], row['charged_cost_minor'], row['outcome'], _dt(row['created_at']))

    def save_agent_run(self, value: DemoAgentRun) -> DemoAgentRun:
        self.connection.execute("INSERT INTO demo_agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.agent_id, value.goal, value.policy_version, value.model, value.prompt_version, value.input_digest, value.decision_json, value.confidence, value.tool_calls, value.reviewer, value.estimated_cost_minor, value.charged_cost_minor, value.outcome, value.created_at.isoformat()))
        return value

    def agent_runs_for(self, tenant_id: str) -> tuple[DemoAgentRun, ...]:
        return tuple(self._agent_run(row) for row in self.connection.execute("SELECT * FROM demo_agent_runs WHERE tenant_id=? ORDER BY created_at,id", (tenant_id,)))

    def get_agent_run(self, tenant_id: str, run_id: str) -> DemoAgentRun:
        row = self.connection.execute("SELECT * FROM demo_agent_runs WHERE tenant_id=? AND id=?", (tenant_id, run_id)).fetchone()
        if not row: raise NotFoundError('agent run not found')
        return self._agent_run(row)

    @staticmethod
    def _byok(row: sqlite3.Row) -> DemoByokReference:
        return DemoByokReference(row['id'], row['tenant_id'], row['provider'], row['secret_ref'], row['validation_status'], _dt(row['created_at']), row['version'])

    def save_byok_reference(self, value: DemoByokReference) -> DemoByokReference:
        row = self.connection.execute("SELECT * FROM demo_byok_references WHERE tenant_id=? AND provider=?", (value.tenant_id, value.provider)).fetchone()
        if row:
            prior = self._byok(row)
            if prior.secret_ref != value.secret_ref: raise ConflictError('BYOK provider reference already exists')
            return prior
        try: self.connection.execute("INSERT INTO demo_byok_references VALUES (?,?,?,?,?,?,?)", (value.id, value.tenant_id, value.provider, value.secret_ref, value.validation_status, value.created_at.isoformat(), value.version))
        except sqlite3.IntegrityError as exc: raise ConflictError('BYOK reference already exists') from exc
        return value

    def byok_references_for(self, tenant_id: str) -> tuple[DemoByokReference, ...]:
        return tuple(self._byok(row) for row in self.connection.execute("SELECT * FROM demo_byok_references WHERE tenant_id=? ORDER BY provider", (tenant_id,)))

    def save_budget_policy(self, value: DemoBudgetPolicy) -> DemoBudgetPolicy:
        self.connection.execute("INSERT INTO demo_budget_policies VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET daily_limit_minor=excluded.daily_limit_minor,monthly_limit_minor=excluded.monthly_limit_minor,generation_limit=excluded.generation_limit,agent_run_limit=excluded.agent_run_limit,max_tokens=excluded.max_tokens,max_tool_calls=excluded.max_tool_calls,model_tier=excluded.model_tier,version=excluded.version", (value.tenant_id, value.daily_limit_minor, value.monthly_limit_minor, value.generation_limit, value.agent_run_limit, value.max_tokens, value.max_tool_calls, value.model_tier, value.version))
        return value

    def get_budget_policy(self, tenant_id: str) -> DemoBudgetPolicy | None:
        row = self.connection.execute("SELECT * FROM demo_budget_policies WHERE tenant_id=?", (tenant_id,)).fetchone()
        return DemoBudgetPolicy(row['tenant_id'], row['daily_limit_minor'], row['monthly_limit_minor'], row['generation_limit'], row['agent_run_limit'], row['max_tokens'], row['max_tool_calls'], row['model_tier'], row['version']) if row else None

    def save_budget_entry(self, value: DemoBudgetLedgerEntry) -> DemoBudgetLedgerEntry:
        row = self.connection.execute("SELECT * FROM demo_budget_ledger WHERE tenant_id=? AND idempotency_key=?", (value.tenant_id, value.idempotency_key)).fetchone()
        if row:
            if row['amount_minor'] != value.amount_minor: raise ConflictError('budget idempotency key reused')
            return DemoBudgetLedgerEntry(row['id'], row['tenant_id'], row['run_id'], row['amount_minor'], _dt(row['occurred_at']), row['idempotency_key'])
        try: self.connection.execute("INSERT INTO demo_budget_ledger VALUES (?,?,?,?,?,?)", (value.id, value.tenant_id, value.run_id, value.amount_minor, value.occurred_at.isoformat(), value.idempotency_key))
        except sqlite3.IntegrityError as exc: raise ConflictError('budget ledger entry already exists') from exc
        return value

    def budget_entries_for(self, tenant_id: str) -> tuple[DemoBudgetLedgerEntry, ...]:
        return tuple(DemoBudgetLedgerEntry(row['id'], row['tenant_id'], row['run_id'], row['amount_minor'], _dt(row['occurred_at']), row['idempotency_key']) for row in self.connection.execute("SELECT * FROM demo_budget_ledger WHERE tenant_id=? ORDER BY occurred_at,id", (tenant_id,)))

    def save_agent_status(self, status: AgentStatusSnapshot) -> None:
        """Persist the latest worker/PM heartbeat as a tenant-owned checkpoint."""
        self.connection.execute(
            """INSERT INTO agent_status_snapshots
            (tenant_id,agent_id,role,state,current_task,started_at,last_heartbeat_at,ended_at,last_message,
             last_commit,test_result,next_task,blocker,usage_limited,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id,agent_id) DO UPDATE SET
             role=excluded.role,state=excluded.state,current_task=excluded.current_task,
             started_at=excluded.started_at,last_heartbeat_at=excluded.last_heartbeat_at,ended_at=excluded.ended_at,
             last_message=excluded.last_message,last_commit=excluded.last_commit,test_result=excluded.test_result,
             next_task=excluded.next_task,blocker=excluded.blocker,usage_limited=excluded.usage_limited,
             updated_at=excluded.updated_at""",
            (status.tenant_id, status.agent_id, status.role, status.state.value, status.current_task,
             status.started_at.isoformat() if status.started_at else None,
             status.last_heartbeat_at.isoformat() if status.last_heartbeat_at else None,
             status.ended_at.isoformat() if status.ended_at else None, status.last_message,
             status.last_commit, status.test_result, status.next_task, status.blocker, int(status.usage_limited),
             status.updated_at.isoformat()),
        )

    @staticmethod
    def _agent_status(row: sqlite3.Row) -> AgentStatusSnapshot:
        return AgentStatusSnapshot(
            tenant_id=row["tenant_id"], agent_id=row["agent_id"], role=row["role"], state=AgentState(row["state"]),
            current_task=row["current_task"], started_at=_dt(row["started_at"]),
            last_heartbeat_at=_dt(row["last_heartbeat_at"]), ended_at=_dt(row["ended_at"]),
            last_message=row["last_message"], last_commit=row["last_commit"], test_result=row["test_result"],
            next_task=row["next_task"], blocker=row["blocker"], usage_limited=bool(row["usage_limited"]),
            updated_at=_dt(row["updated_at"]),  # type: ignore[arg-type]
        )

    def agent_status_for(self, tenant_id: str) -> tuple[AgentStatusSnapshot, ...]:
        return tuple(self._agent_status(row) for row in self.connection.execute(
            "SELECT * FROM agent_status_snapshots WHERE tenant_id=? ORDER BY agent_id", (tenant_id,)
        ))

    @contextmanager
    def transaction(self) -> Iterator[SQLiteRepository]:
        savepoint = f"uow_{self._depth}"
        if self._depth == 0:
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            self.connection.execute(f"SAVEPOINT {savepoint}")
        self._depth += 1
        try:
            yield self
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.connection.execute("ROLLBACK")
            else:
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                self.connection.execute(f"RELEASE {savepoint}")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.connection.execute("COMMIT")
            else:
                self.connection.execute(f"RELEASE {savepoint}")

    def add_tenant(self, value: Tenant) -> None:
        self.connection.execute("INSERT INTO tenants VALUES (?,?,?)", (value.id, value.legal_name, value.created_at.isoformat()))

    def add_user(self, value: User) -> None:
        try:
            self.connection.execute("INSERT INTO users VALUES (?,?,?)", (value.id, value.email, value.created_at.isoformat()))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("email already registered") from exc

    def find_user_by_email(self, email: str) -> User | None:
        row = self.connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return User(row["id"], row["email"], _dt(row["created_at"])) if row else None  # type: ignore[arg-type]

    def get_user(self, user_id: str) -> User:
        row = self.connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row: raise NotFoundError("user not found")
        return User(row["id"], row["email"], _dt(row["created_at"]))  # type: ignore[arg-type]

    def save_membership(self, value: Membership) -> None:
        self.connection.execute("""INSERT INTO memberships VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,user_id) DO UPDATE SET
 roles_json=excluded.roles_json,active=excluded.active,version=excluded.version""",
            (value.tenant_id,value.user_id,json.dumps(sorted(r.value for r in value.roles)),int(value.active),value.version))

    def get_membership(self, tenant_id: str, user_id: str) -> Membership:
        row = self.connection.execute("SELECT * FROM memberships WHERE tenant_id=? AND user_id=?", (tenant_id,user_id)).fetchone()
        if not row: raise NotFoundError("membership not found")
        return Membership(row["tenant_id"],row["user_id"],frozenset(Role(v) for v in json.loads(row["roles_json"])),bool(row["active"]),row["version"])

    def tenant_memberships(self, tenant_id: str):
        rows = self.connection.execute("SELECT user_id FROM memberships WHERE tenant_id=?", (tenant_id,)).fetchall()
        return tuple(self.get_membership(tenant_id,row["user_id"]) for row in rows)

    def save_command(self, c: Command) -> None:
        self.connection.execute("""INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
 state=excluded.state,supersedes_id=excluded.supersedes_id,payload_json=excluded.payload_json,payload_digest=excluded.payload_digest""",
            (c.id,c.tenant_id,c.kind.value,c.target_ref,json.dumps(c.payload,ensure_ascii=False,sort_keys=True),c.payload_digest,c.idempotency_key,c.state.value,c.created_at.isoformat(),c.supersedes_id))

    def get_command(self, tenant_id: str, command_id: str) -> Command:
        row = self.connection.execute("SELECT * FROM commands WHERE tenant_id=? AND id=?",(tenant_id,command_id)).fetchone()
        if not row:
            if self.connection.execute("SELECT 1 FROM commands WHERE id=?",(command_id,)).fetchone(): raise TenantBoundaryError("cross-tenant command access denied")
            raise NotFoundError("command not found")
        return Command(row["id"],row["tenant_id"],ApprovalKind(row["kind"]),row["target_ref"],json.loads(row["payload_json"]),row["payload_digest"],row["idempotency_key"],CommandState(row["state"]),_dt(row["created_at"]),row["supersedes_id"])  # type: ignore[arg-type]

    def command_id_for_key(self, tenant_id: str, key: str) -> str | None:
        row=self.connection.execute("SELECT id FROM commands WHERE tenant_id=? AND idempotency_key=?",(tenant_id,key)).fetchone()
        return row["id"] if row else None

    def bind_command_key(self, tenant_id: str, idempotency_key: str, command_id: str) -> None:
        # The unique binding is stored with the command row.
        if self.command_id_for_key(tenant_id,idempotency_key) != command_id: raise ConflictError("command idempotency binding mismatch")

    def save_approval(self, a: Approval) -> None:
        self.connection.execute("""INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
 state=excluded.state,decided_by=excluded.decided_by,decision_reason=excluded.decision_reason""",
            (a.id,a.tenant_id,a.command_id,a.kind.value,a.state.value,a.requested_at.isoformat(),a.expires_at.isoformat(),json.dumps(a.evidence,ensure_ascii=False,sort_keys=True),a.decided_by,a.decision_reason))

    def get_approval_for_command(self, tenant_id: str, command_id: str) -> Approval:
        self.get_command(tenant_id,command_id)
        r=self.connection.execute("SELECT * FROM approvals WHERE tenant_id=? AND command_id=?",(tenant_id,command_id)).fetchone()
        if not r: raise NotFoundError("approval not found")
        return Approval(r["id"],r["tenant_id"],r["command_id"],ApprovalKind(r["kind"]),ApprovalState(r["state"]),_dt(r["requested_at"]),_dt(r["expires_at"]),tuple(json.loads(r["evidence_json"])),r["decided_by"],r["decision_reason"])  # type: ignore[arg-type]

    def approvals_for(self, tenant_id: str) -> tuple[Approval, ...]:
        return tuple(Approval(r["id"],r["tenant_id"],r["command_id"],ApprovalKind(r["kind"]),ApprovalState(r["state"]),_dt(r["requested_at"]),_dt(r["expires_at"]),tuple(json.loads(r["evidence_json"])),r["decided_by"],r["decision_reason"]) for r in self.connection.execute("SELECT * FROM approvals WHERE tenant_id=? ORDER BY requested_at,id", (tenant_id,)))  # type: ignore[arg-type]

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval:
        r = self.connection.execute("SELECT * FROM approvals WHERE tenant_id=? AND id=?", (tenant_id, approval_id)).fetchone()
        if not r: raise NotFoundError("approval not found")
        return Approval(r["id"],r["tenant_id"],r["command_id"],ApprovalKind(r["kind"]),ApprovalState(r["state"]),_dt(r["requested_at"]),_dt(r["expires_at"]),tuple(json.loads(r["evidence_json"])),r["decided_by"],r["decision_reason"])  # type: ignore[arg-type]

    def append_audit(self, e: AuditEvent) -> None:
        self.connection.execute("INSERT INTO audit_events(id,tenant_id,occurred_at,actor_ref,action,target_ref,outcome,correlation_id,metadata_json,prev_hash,event_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e.id,e.tenant_id,e.occurred_at.isoformat(),e.actor_ref,e.action,e.target_ref,e.outcome,e.correlation_id,json.dumps(e.metadata,ensure_ascii=False,sort_keys=True),e.prev_hash,e.event_hash))

    def audits_for(self, tenant_id: str) -> tuple[AuditEvent,...]:
        rows=self.connection.execute("SELECT * FROM audit_events WHERE tenant_id=? ORDER BY sequence",(tenant_id,)).fetchall()
        return tuple(AuditEvent(r["id"],r["tenant_id"],_dt(r["occurred_at"]),r["actor_ref"],r["action"],r["target_ref"],r["outcome"],r["correlation_id"],json.loads(r["metadata_json"]),r["prev_hash"],r["event_hash"]) for r in rows)  # type: ignore[arg-type]

    def append_outbox(self, e: OutboxEvent) -> None:
        try:
            self.connection.execute("""INSERT INTO outbox
            (id,tenant_id,topic,aggregate_ref,payload_json,idempotency_key,state,created_at,checkpoint_json,
             lease_owner,lease_until,fencing_token,completed_at,attempts,available_at,last_error)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(e.id,e.tenant_id,e.topic,e.aggregate_ref,json.dumps(e.payload,ensure_ascii=False,sort_keys=True),e.idempotency_key,e.state.value,e.created_at.isoformat(),json.dumps(e.checkpoint,ensure_ascii=False,sort_keys=True),e.lease_owner,e.lease_until.isoformat() if e.lease_until else None,e.fencing_token,e.completed_at.isoformat() if e.completed_at else None,e.attempts,(e.available_at or e.created_at).isoformat(),e.last_error))
        except sqlite3.IntegrityError as exc: raise ConflictError("outbox idempotency key already exists") from exc

    def _outbox(self, r: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(r["id"],r["tenant_id"],r["topic"],r["aggregate_ref"],json.loads(r["payload_json"]),r["idempotency_key"],OutboxState(r["state"]),_dt(r["created_at"]),json.loads(r["checkpoint_json"]),r["lease_owner"],_dt(r["lease_until"]),r["fencing_token"],_dt(r["completed_at"]),r["attempts"],_dt(r["available_at"]),r["last_error"])  # type: ignore[arg-type]

    def outbox_for(self, tenant_id: str) -> tuple[OutboxEvent,...]:
        return tuple(self._outbox(r) for r in self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? ORDER BY created_at,id",(tenant_id,)))

    def claim_outbox(self, tenant_id: str, event_id: str, worker_id: str, now: datetime, lease_until: datetime) -> OutboxEvent:
        with self.transaction():
            r=self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? AND id=?",(tenant_id,event_id)).fetchone()
            if not r:
                if self.connection.execute("SELECT 1 FROM outbox WHERE id=?",(event_id,)).fetchone(): raise TenantBoundaryError("cross-tenant outbox access denied")
                raise NotFoundError("outbox event not found")
            e=self._outbox(r)
            if e.state in {OutboxState.COMPLETED,OutboxState.DEAD}: raise ConflictError("outbox event is terminal")
            if e.state==OutboxState.RETRY and e.available_at and e.available_at>now: raise ConflictError("outbox event is not available")
            if e.lease_until and e.lease_until>now: raise ConflictError("outbox event already leased")
            token,attempts=e.fencing_token+1,e.attempts+1
            self.connection.execute("UPDATE outbox SET state=?,lease_owner=?,lease_until=?,fencing_token=?,attempts=?,last_error=NULL WHERE tenant_id=? AND id=?",(OutboxState.LEASED.value,worker_id,lease_until.isoformat(),token,attempts,tenant_id,event_id))
            e.state,e.lease_owner,e.lease_until,e.fencing_token,e.attempts,e.last_error=OutboxState.LEASED,worker_id,lease_until,token,attempts,None
            return e

    def claim_next_outbox(self, tenant_id: str, worker_id: str, now: datetime, lease_until: datetime) -> OutboxEvent | None:
        with self.transaction():
            row=self.connection.execute("""SELECT o.id FROM outbox o WHERE o.tenant_id=? AND
             ((o.state IN (?,?) AND COALESCE(o.available_at,o.created_at)<=?) OR (o.state=? AND o.lease_until<=?))
             AND NOT EXISTS (SELECT 1 FROM outbox prior WHERE prior.tenant_id=o.tenant_id
               AND prior.aggregate_ref=o.aggregate_ref AND prior.rowid<o.rowid AND prior.state NOT IN (?,?))
             ORDER BY o.rowid LIMIT 1""",
             (tenant_id,OutboxState.PENDING.value,OutboxState.RETRY.value,now.isoformat(),OutboxState.LEASED.value,now.isoformat(),OutboxState.COMPLETED.value,OutboxState.DEAD.value)).fetchone()
            return self.claim_outbox(tenant_id,row["id"],worker_id,now,lease_until) if row else None

    def checkpoint_outbox(self, tenant_id: str, event_id: str, worker_id: str, fencing_token: int, checkpoint: dict, now: datetime, completed: bool=False) -> OutboxEvent:
        with self.transaction():
            r=self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? AND id=?",(tenant_id,event_id)).fetchone()
            if not r: raise NotFoundError("outbox event not found")
            e=self._outbox(r)
            if e.state!=OutboxState.LEASED or e.lease_owner!=worker_id or e.fencing_token!=fencing_token or not e.lease_until or e.lease_until<=now: raise ConflictError("stale or expired outbox lease")
            state=OutboxState.COMPLETED if completed else OutboxState.LEASED
            self.connection.execute("UPDATE outbox SET checkpoint_json=?,state=?,completed_at=?,lease_owner=?,lease_until=? WHERE tenant_id=? AND id=?",(json.dumps(checkpoint,sort_keys=True),state.value,now.isoformat() if completed else None,None if completed else worker_id,None if completed else e.lease_until.isoformat(),tenant_id,event_id))
            e.checkpoint,e.state=dict(checkpoint),state
            if completed: e.completed_at,e.lease_owner,e.lease_until=now,None,None
            return e

    def fail_outbox(self, tenant_id: str, event_id: str, worker_id: str, fencing_token: int, error: str, now: datetime, max_attempts: int = 5) -> OutboxEvent:
        if max_attempts < 1: raise ValueError("max_attempts must be positive")
        with self.transaction():
            r=self.connection.execute("SELECT * FROM outbox WHERE tenant_id=? AND id=?",(tenant_id,event_id)).fetchone()
            if not r: raise NotFoundError("outbox event not found")
            e=self._outbox(r)
            if e.state!=OutboxState.LEASED or e.lease_owner!=worker_id or e.fencing_token!=fencing_token or not e.lease_until or e.lease_until<=now: raise ConflictError("stale or expired outbox lease")
            state=OutboxState.DEAD if e.attempts>=max_attempts else OutboxState.RETRY
            from datetime import timedelta
            available=now if state==OutboxState.DEAD else now+timedelta(seconds=_backoff_seconds(e.attempts))
            safe=_safe_error(str(error))
            self.connection.execute("UPDATE outbox SET state=?,available_at=?,last_error=?,lease_owner=NULL,lease_until=NULL WHERE tenant_id=? AND id=?",(state.value,available.isoformat(),safe,tenant_id,event_id))
            e.state,e.available_at,e.last_error,e.lease_owner,e.lease_until=state,available,safe,None,None
            return e
