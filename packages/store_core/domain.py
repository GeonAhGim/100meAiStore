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
