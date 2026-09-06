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
    "normalize_demo_order",
    "validate_demo_page",
]
