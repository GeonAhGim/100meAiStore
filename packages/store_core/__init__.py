"""Tenant-safe commerce control-plane domain slice."""

from .domain import (
    ApprovalKind,
    ApprovalState,
    Capability,
    CommandState,
    Role,
    TenantContext,
    OutboxState,
)
from .dashboard import DashboardProjection
from .errors import AuthorizationError, ConflictError, NotFoundError, TenantBoundaryError
from .service import StoreControlPlane
from .sqlite_repository import SQLiteRepository
from .ingestion import (
    DemoPollResult, DemoReadAdapter, FixtureDemoReadAdapter, RetryableReadError, Page,
    normalize_demo_order, validate_demo_page,
)
from .domain import AdapterCapability, AdapterCapabilityManifest, DemoPage
from .orders import ingest_order, propose_routing
from .order02 import approve_demo_po, submit_demo_po, reconcile_demo_po
from .order03 import request_demo_cancel, ingest_demo_tracking
from .claim01 import open_demo_claim, record_demo_claim_status
from .finance01 import import_demo_settlement
from .inventory import DemoInventoryObservation, DemoPriceCalculation, calculate_demo_price, observe_demo_inventory, record_demo_inventory, record_demo_price_projection
from .catalog import ingest_demo_catalog, project_demo_offer
from .approvals import approval_inbox, approval_detail, decide_approval
from .gateway import configure_demo_byok, set_demo_budget_policy, record_demo_agent_run, submit_demo_tool
from .notifications import set_demo_notification_preference, notify_demo, acknowledge_demo_incident
from .safety import set_demo_stop, backup_demo_sqlite
from .readiness import evaluate_demo_readiness

__all__ = [
    "ApprovalKind",
    "ApprovalState",
    "AuthorizationError",
    "Capability",
    "CommandState",
    "ConflictError",
    "NotFoundError",
    "Role",
    "StoreControlPlane",
    "TenantBoundaryError",
    "TenantContext",
    "OutboxState",
    "SQLiteRepository",
    "DashboardProjection",
    "DemoPollResult",
    "DemoReadAdapter",
    "FixtureDemoReadAdapter",
    "RetryableReadError",
    "Page",
    "AdapterCapability",
    "AdapterCapabilityManifest",
    "DemoPage",
    "ingest_order",
    "propose_routing",
    "approve_demo_po",
    "submit_demo_po",
    "reconcile_demo_po",
    "request_demo_cancel",
    "ingest_demo_tracking",
    "open_demo_claim",
    "record_demo_claim_status",
    "import_demo_settlement",
    "DemoInventoryObservation",
    "DemoPriceCalculation",
    "calculate_demo_price",
    "observe_demo_inventory",
    "record_demo_inventory",
    "record_demo_price_projection",
    "ingest_demo_catalog",
    "project_demo_offer",
    "approval_inbox",
    "approval_detail",
    "decide_approval",
    "configure_demo_byok",
    "set_demo_budget_policy",
    "record_demo_agent_run",
    "submit_demo_tool",
    "set_demo_notification_preference",
    "notify_demo",
    "acknowledge_demo_incident",
    "set_demo_stop",
    "backup_demo_sqlite",
    "evaluate_demo_readiness",
    "normalize_demo_order",
    "validate_demo_page",
]
