from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class Role(str, Enum):
    MASTER = "master"
    FUNDS = "funds"
    CATALOG_CS = "catalog_cs"
    AUDITOR = "auditor"


class Capability(str, Enum):
    TENANT_ADMIN = "tenant_admin"
    FUNDS = "funds"
    CATALOG = "catalog"
    CS = "cs"
    APPROVE_PURCHASE = "approve_purchase"
    APPROVE_REFUND = "approve_refund"
    APPROVE_PRODUCT = "approve_product"
    APPROVE_CS = "approve_cs"
    READ_AUDIT = "read_audit"
    RECOVERY = "recovery"


ROLE_CAPABILITIES: Mapping[Role, frozenset[Capability]] = {
    Role.MASTER: frozenset(Capability),
    Role.FUNDS: frozenset(
        {Capability.FUNDS, Capability.APPROVE_PURCHASE, Capability.APPROVE_REFUND}
    ),
    Role.CATALOG_CS: frozenset(
        {
            Capability.CATALOG,
            Capability.CS,
            Capability.APPROVE_PRODUCT,
            Capability.APPROVE_CS,
        }
    ),
    Role.AUDITOR: frozenset({Capability.READ_AUDIT}),
}


class ApprovalKind(str, Enum):
    PURCHASE = "purchase"
    PRODUCT = "product"
    SUPPLIER_REPLACEMENT = "supplier_replacement"
    REFUND = "refund"
    CS = "cs"
    PAUSE = "pause"


APPROVAL_CAPABILITY: Mapping[ApprovalKind, Capability] = {
    ApprovalKind.PURCHASE: Capability.APPROVE_PURCHASE,
    ApprovalKind.PRODUCT: Capability.APPROVE_PRODUCT,
    ApprovalKind.SUPPLIER_REPLACEMENT: Capability.APPROVE_PRODUCT,
    ApprovalKind.REFUND: Capability.APPROVE_REFUND,
    ApprovalKind.CS: Capability.APPROVE_CS,
    ApprovalKind.PAUSE: Capability.RECOVERY,
}


class CommandState(str, Enum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ApprovalIntent:
    tenant_id: str
    command_id: str
    canonical_digest: str
    policy_version: int
    target_version: int
    created_at: datetime


@dataclass(frozen=True)
class ExecutionPreparation:
    id: str
    tenant_id: str
    command_id: str
    canonical_digest: str
    prepared_by: str
    prepared_at: datetime


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str
    membership_version: int


@dataclass
class Tenant:
    id: str
    legal_name: str
    created_at: datetime


@dataclass
class User:
    id: str
    email: str
    created_at: datetime


@dataclass
class Membership:
    tenant_id: str
    user_id: str
    roles: frozenset[Role]
    active: bool = True
    version: int = 1


@dataclass
class Command:
    id: str
    tenant_id: str
    kind: ApprovalKind
    target_ref: str
    payload: Mapping[str, Any]
    payload_digest: str
    idempotency_key: str
    state: CommandState
    created_at: datetime
    supersedes_id: str | None = None


@dataclass
class Approval:
    id: str
    tenant_id: str
    command_id: str
    kind: ApprovalKind
    state: ApprovalState
    requested_at: datetime
    expires_at: datetime
    evidence: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    decided_by: str | None = None
    decision_reason: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    tenant_id: str
    occurred_at: datetime
    actor_ref: str
    action: str
    target_ref: str
    outcome: str
    correlation_id: str
    metadata: Mapping[str, Any]
    prev_hash: str | None
    event_hash: str


class OutboxState(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY = "retry"
    COMPLETED = "completed"
    DEAD = "dead"


@dataclass
class OutboxEvent:
    id: str
    tenant_id: str
    topic: str
    aggregate_ref: str
    payload: Mapping[str, Any]
    idempotency_key: str
    state: OutboxState
    created_at: datetime
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    lease_owner: str | None = None
    lease_until: datetime | None = None
    fencing_token: int = 0
    completed_at: datetime | None = None
    attempts: int = 0
    available_at: datetime | None = None
    last_error: str | None = None


class InboxState(str, Enum):
    RECEIVED = "received"
    PROCESSED = "processed"


@dataclass
class InboxMessage:
    """Durable delivery receipt; PROCESSED means accepted for downstream routing."""

    id: str
    tenant_id: str
    provider: str
    connection_id: str
    external_event_id: str
    schema_version: int
    received_at: datetime
    payload_digest: str
    raw_payload_ref: str | None
    state: InboxState = InboxState.RECEIVED
    version: int = 1
    processed_at: datetime | None = None


class AgentState(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usage_limited"
    UNKNOWN = "unknown"


@dataclass
class AgentStatusSnapshot:
    tenant_id: str
    agent_id: str
    role: str
    state: AgentState
    current_task: str | None
    started_at: datetime | None
    last_heartbeat_at: datetime | None
    ended_at: datetime | None
    last_message: str | None
    last_commit: str | None
    test_result: str | None
    next_task: str | None
    blocker: str | None
    usage_limited: bool
    updated_at: datetime


@dataclass(frozen=True)
class AdapterCapabilityManifest:
    tenant_id: str
    provider: str
    connection_id: str
    adapter_version: str
    capabilities: frozenset[AdapterCapability]
    inbound_schema_versions: frozenset[int]
    updated_at: datetime


class AdapterCapability(str, Enum):
    DEMO_EXECUTE = "demo_execute"
    DEMO_LOOKUP = "demo_lookup"
    INBOUND_EVENTS = "inbound_events"
    ORDERS_READ = "orders_read"
    ORDERS_WRITE = "orders_write"
    PRODUCTS_READ = "products_read"
    PRODUCTS_WRITE = "products_write"
    INVENTORY_READ = "inventory_read"


@dataclass(frozen=True)
class DemoAdapterDescription:
    """The deliberately small, read-only contract used by DEMO ingestion."""

    provider: str
    adapter_version: str
    normalized_schema_version: int = 1
    capability: AdapterCapability = AdapterCapability.ORDERS_READ
    mode: str = "DEMO"


@dataclass(frozen=True)
class NormalizedDemoOrder:
    external_order_id: str
    event_id: str
    revision: int
    currency: str
    total_minor: int
    lines: tuple[Mapping[str, Any], ...]
    source_digest: str | None = None


@dataclass(frozen=True)
class DemoPage:
    items: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    has_more: bool
    observed_at: datetime


@dataclass(frozen=True)
class NormalizedInboundPayload:
    tenant_id: str
    immutable_ref: str
    canonical_digest: str
    schema_version: int
    payload_json: str
    source_digest: str | None
    created_at: datetime


@dataclass(frozen=True)
class AdapterPollCheckpoint:
    tenant_id: str
    provider: str
    connection_id: str
    adapter_version: str
    cursor: str | None
    overlap_from: datetime | None
    version: int
    updated_at: datetime
    last_success_at: datetime | None = None


class ChannelOrderState(str, Enum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    ROUTING = "routing"
    PO_PENDING = "po_pending"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"


class RoutingState(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    EXCEPTION = "exception"


class PurchaseOrderState(str, Enum):
    DRAFT = "draft"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EXCEPTION = "exception"


@dataclass
class ChannelOrder:
    id: str
    tenant_id: str
    channel_id: str
    external_order_key: str
    payload_ref: str
    currency: str
    total_minor: int
    status: ChannelOrderState
    received_at: datetime
    idempotency_key: str
    version: int = 1


@dataclass
class OrderLine:
    id: str
    tenant_id: str
    channel_order_id: str
    sku: str
    quantity: int
    unit_minor: int
    routed_status: str = "unrouted"
    version: int = 1
    tracking_key: str | None = None
    tracking_status: str | None = None
    tracking_version: int = 0
    tracking_observed_at: datetime | None = None


@dataclass(frozen=True)
class TrackingObservation:
    id: str
    tenant_id: str
    order_line_id: str
    tracking_key: str
    status: str
    observed_at: datetime
    response_digest: str


class ClaimStatus(str, Enum):
    OPEN = "OPEN"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    CLOSED = "CLOSED"


@dataclass
class DemoClaim:
    id: str
    tenant_id: str
    channel_order_id: str
    claim_type: str
    amount_minor: int
    consumer_status: ClaimStatus
    channel_status: ClaimStatus
    supplier_status: ClaimStatus
    idempotency_key: str
    created_at: datetime
    version: int = 1


@dataclass(frozen=True)
class ClaimStatusObservation:
    id: str
    tenant_id: str
    claim_id: str
    status_kind: str
    status: ClaimStatus
    observed_at: datetime
    response_digest: str


class SettlementStatus(str, Enum):
    IMPORTED = "IMPORTED"
    RECONCILED = "RECONCILED"
    EXCEPTION = "EXCEPTION"


@dataclass
class DemoSettlementBatch:
    id: str
    tenant_id: str
    channel_id: str
    period: str
    source_digest: str
    status: SettlementStatus
    idempotency_key: str
    created_at: datetime
    version: int = 1


@dataclass
class DemoSettlementLine:
    id: str
    tenant_id: str
    batch_id: str
    external_order_key: str
    kind: str
    amount_minor: int
    currency: str
    source_row_ref: str
    order_id: str | None
    match_status: str


@dataclass
class DemoRealizedProfit:
    id: str
    tenant_id: str
    batch_id: str
    order_id: str
    projected_minor: int | None
    realized_minor: int | None
    status: str
    calculated_at: datetime


@dataclass(frozen=True)
class DemoCatalogImport:
    """Immutable local DEMO catalog batch and its idempotency identity."""

    id: str
    tenant_id: str
    supplier_id: str
    source_digest: str
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True)
class DemoCatalogSnapshot:
    id: str
    tenant_id: str
    import_id: str
    supplier_id: str
    external_key: str
    source_digest: str
    payload_json: str
    created_at: datetime


@dataclass(frozen=True)
class DemoCanonicalProduct:
    id: str
    tenant_id: str
    sku: str
    title: str
    category: str
    price_minor: int
    currency: str
    attributes_json: str
    source_snapshot_id: str
    version: int
    created_at: datetime


@dataclass(frozen=True)
class DemoProductLineage:
    id: str
    tenant_id: str
    source_snapshot_id: str
    canonical_product_id: str
    transform_version: int
    created_at: datetime


@dataclass(frozen=True)
class DemoChannelOffer:
    id: str
    tenant_id: str
    channel_id: str
    canonical_product_id: str
    source_snapshot_id: str
    external_key: str
    price_minor: int
    currency: str
    version: int
    created_at: datetime


@dataclass
class DemoToolCommand:
    id: str
    tenant_id: str
    actor_type: str
    actor_id: str
    tool: str
    target_type: str
    target_id: str
    input_json: str
    idempotency_key: str
    requested_policy_version: int
    approval_id: str | None
    mode: str
    state: str
    blocked_reason: str | None
    created_at: datetime


@dataclass
class DemoAgentRun:
    id: str
    tenant_id: str
    agent_id: str
    goal: str
    policy_version: int
    model: str
    prompt_version: str
    input_digest: str
    decision_json: str
    confidence: str
    tool_calls: int
    reviewer: str | None
    estimated_cost_minor: int
    charged_cost_minor: int | None
    outcome: str
    created_at: datetime


@dataclass
class DemoByokReference:
    id: str
    tenant_id: str
    provider: str
    secret_ref: str
    validation_status: str
    created_at: datetime
    version: int = 1


@dataclass
class DemoBudgetPolicy:
    tenant_id: str
    daily_limit_minor: int
    monthly_limit_minor: int
    generation_limit: int
    agent_run_limit: int
    max_tokens: int
    max_tool_calls: int
    model_tier: str
    version: int = 1


@dataclass(frozen=True)
class DemoBudgetLedgerEntry:
    id: str
    tenant_id: str
    run_id: str
    amount_minor: int
    occurred_at: datetime
    idempotency_key: str


@dataclass
class DemoNotificationPreference:
    tenant_id: str
    notification_key: str
    channels: tuple[str, ...]
    muted: bool
    version: int = 1


@dataclass
class DemoNotificationDelivery:
    id: str
    tenant_id: str
    notification_key: str
    channel: str
    payload_json: str
    state: str
    attempt: int
    fallback_from: str | None
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True)
class DemoIncidentAcknowledgement:
    id: str
    tenant_id: str
    incident_id: str
    acknowledged_by: str
    note: str
    idempotency_key: str
    acknowledged_at: datetime


@dataclass
class DemoStopControl:
    tenant_id: str
    scope_type: str
    scope_ref: str
    stopped: bool
    reason: str
    version: int
    updated_at: datetime


@dataclass(frozen=True)
class DemoBackupManifest:
    id: str
    tenant_id: str
    source_digest: str
    schema_version: int
    created_at: datetime


@dataclass
class RoutingDecision:
    id: str
    tenant_id: str
    order_line_id: str
    supplier_id: str
    quantity: int
    unit_cost_minor: int
    reason: str
    status: RoutingState = RoutingState.PROPOSED


@dataclass
class SupplierPurchaseOrder:
    id: str
    tenant_id: str
    channel_order_id: str
    supplier_id: str
    status: PurchaseOrderState
    idempotency_key: str
    approval_command_id: str | None
    created_at: datetime
    version: int = 1
    provider_reference: str | None = None
    last_response_digest: str | None = None
    last_observed_at: datetime | None = None


@dataclass
class PurchaseLine:
    id: str
    tenant_id: str
    purchase_order_id: str
    order_line_id: str
    quantity: int
    unit_cost_minor: int


class AttemptState(str, Enum):
    PREPARED = 'prepared'
    DISPATCHING = 'dispatching'
    UNKNOWN = 'unknown'
    RECONCILING = 'reconciling'
    VERIFIED_SUCCESS = 'verified_success'
    VERIFIED_FAILURE = 'verified_failure'
    MANUAL_REVIEW = 'manual_review'


@dataclass(frozen=True)
class DemoExecutionControl:
    tenant_id: str
    command_id: str
    policy_version: int
    target_version: int
    stopped: bool


@dataclass
class ExecutionAttempt:
    id: str
    tenant_id: str
    command_id: str
    preparation_id: str
    operation_key: str
    intent_digest: str
    adapter_version: str
    provider: str
    connection_id: str
    state: AttemptState = AttemptState.PREPARED
    version: int = 1
    lease_owner: str | None = None
    lease_until: datetime | None = None
    fencing_token: int = 0
    provider_reference: str | None = None
    last_observed_at: datetime | None = None
    next_check_at: datetime | None = None


@dataclass(frozen=True)
class AttemptObservation:
    id: str
    tenant_id: str
    attempt_id: str
    observation_kind: str
    response_digest: str
    observed_at: datetime
    correlation_id: str
