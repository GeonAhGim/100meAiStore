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
]
