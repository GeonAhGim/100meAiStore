"""Server-side browser sessions and one-use approval confirmation nonces.

This boundary never verifies email/MFA itself. A trusted identity adapter must
issue the opaque assertion reference; neither tokens nor assertion material are
written to logs or durable storage in plaintext.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .domain import (APPROVAL_CAPABILITY, ApprovalConfirmationNonce, ApprovalState,
                     BrowserSession, TenantContext)
from .errors import AuthorizationError, ConflictError

_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_ASSERTION_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")


def _digest(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise AuthorizationError("invalid browser credential")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class IssuedBrowserSession:
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class IssuedApprovalNonce:
    token: str = field(repr=False)
    expires_at: datetime


def browser_session_cookie(token: str, *, max_age_seconds: int, secure: bool = True) -> str:
    """Cookie contract for the future trusted identity callback."""
    _digest(token)
    if type(max_age_seconds) is not int or not 60 <= max_age_seconds <= 86_400 or type(secure) is not bool:
        raise ConflictError("invalid browser session cookie lifetime")
    flags = "; Path=/; HttpOnly; SameSite=Strict"
    if secure:
        flags += "; Secure"
    return f"store_session={token}; Max-Age={max_age_seconds}{flags}"


def issue_browser_session(service: Any, context: TenantContext, *,
                          identity_assertion_ref: str,
                          ttl: timedelta = timedelta(hours=8)) -> IssuedBrowserSession:
    """Trusted-server hook called only after an external identity check."""
    if (not isinstance(identity_assertion_ref, str)
            or not _ASSERTION_REF.fullmatch(identity_assertion_ref)
            or not isinstance(ttl, timedelta)
            or not timedelta(minutes=1) <= ttl <= timedelta(hours=24)):
        raise ConflictError("invalid verified identity assertion")
    membership = service._membership(context)
    now = service._clock()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ConflictError("timezone-aware service clock required")
    token = secrets.token_urlsafe(32)
    expires = now + ttl
    with service.repo.transaction():
        service._membership(context)
        service.repo.save_browser_session(BrowserSession(
            _digest(token), context.tenant_id, context.user_id, membership.version,
            identity_assertion_ref, now, expires))
    return IssuedBrowserSession(token, expires)


def authenticate_browser_session(service: Any, token: str) -> TenantContext:
    digest = _digest(token)
    session = service.repo.get_browser_session(digest)
    if session is None or service._clock() >= session.expires_at:
        raise AuthorizationError("browser session expired or unknown")
    context = TenantContext(session.tenant_id, session.user_id, session.membership_version)
    service._membership(context)
    return context


def issue_approval_confirmation_nonce(service: Any, session_token: str,
                                      approval_id: str,
                                      ttl: timedelta = timedelta(minutes=5)) -> IssuedApprovalNonce:
    if (not isinstance(approval_id, str) or not _ASSERTION_REF.fullmatch(approval_id)
            or not isinstance(ttl, timedelta)
            or not timedelta(seconds=30) <= ttl <= timedelta(minutes=10)):
        raise ConflictError("invalid approval confirmation request")
    context = authenticate_browser_session(service, session_token)
    with service.repo.transaction():
        approval = service.repo.get_approval(context.tenant_id, approval_id)
        service.require(context, APPROVAL_CAPABILITY[approval.kind])
        command = service.repo.get_command(context.tenant_id, approval.command_id)
        now = service._clock()
        if approval.state != ApprovalState.PENDING or now >= approval.expires_at:
            raise ConflictError("approval is not pending")
        token = secrets.token_urlsafe(32)
        expires = min(now + ttl, approval.expires_at)
        service.repo.save_approval_confirmation_nonce(ApprovalConfirmationNonce(
            _digest(token), _digest(session_token), context.tenant_id, approval.id,
            command.id, now, expires))
    return IssuedApprovalNonce(token, expires)


def decide_approval_authenticated(service: Any, session_token: str, approval_id: str,
                                  approve: bool, reason: str, nonce_token: str) -> Any:
    if type(approve) is not bool or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ConflictError("invalid approval decision")
    context = authenticate_browser_session(service, session_token)
    with service.repo.transaction():
        approval = service.repo.get_approval(context.tenant_id, approval_id)
        service.require(context, APPROVAL_CAPABILITY[approval.kind])
        service.repo.consume_approval_confirmation_nonce(
            _digest(nonce_token), _digest(session_token), context.tenant_id,
            approval.id, approval.command_id, service._clock())
    # A consumed nonce stays consumed if authoritative revalidation rejects the
    # decision; callers must re-authenticate the material and request a new one.
    return service.decide(context, approval.command_id, approve, reason)
