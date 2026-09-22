class DomainError(Exception):
    """Base class for expected domain failures."""


class AuthorizationError(DomainError):
    pass


class TenantBoundaryError(AuthorizationError):
    pass


class ConflictError(DomainError):
    pass


class NotFoundError(DomainError):
    pass
