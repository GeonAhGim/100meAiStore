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
    "normalize_demo_order",
    "validate_demo_page",
]
