"""Synthetic-only signing and retry plans. No credentials or transport."""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from .channel_contracts import OfflineReadPlan, classify_naver_read_error
from .channel_order_contracts import ContractQuarantine


@dataclass(frozen=True)
class FixtureSignature:
    provider: str
    digest: str = field(repr=False)
    signed_at: str | None = None
    usable_for_live_auth: bool = field(default=False, init=False)


@dataclass(frozen=True)
class OfflineAuthSourceBoundary:
    """Fail-closed record of unresolved official-example discrepancies."""

    naver_public_bcrypt_compatible: bool = field(default=False, init=False)
    coupang_requested_by_header: str | None = field(default=None, init=False)
    live_transport_authorized: bool = field(default=False, init=False)
    unresolved_reasons: tuple[str, ...] = field(default=(
        "naver_public_fixture_bcrypt_incompatible",
        "coupang_requested_by_header_unresolved",
    ), init=False)


def offline_auth_source_boundary() -> OfflineAuthSourceBoundary:
    """Return the static offline boundary; fixture evidence cannot authorize LIVE."""
    return OfflineAuthSourceBoundary()


def naver_public_fixture_signature(hashpw: Callable[[bytes, bytes], bytes]) -> FixtureSignature:
    """Naver's published dummy vector; cannot accept a real client ID/secret."""
    try:
        hashed = hashpw(b"aaaabbbbcccc_1643961623299", b"$2a$10$abcdefghijklmnopqrstuv")
    except (ValueError, TypeError):
        raise ContractQuarantine("public_fixture_bcrypt_incompatible") from None
    if not isinstance(hashed, bytes) or len(hashed) != 60 or not hashed.startswith(b"$2a$10$"):
        raise ContractQuarantine("invalid_fixture_bcrypt_result")
    return FixtureSignature("naver", base64.b64encode(hashed).decode("ascii"))


def naver_canonical_fixture_signature(hashpw: Callable[[bytes, bytes], bytes]) -> FixtureSignature:
    """Our separate valid test vector, never a substitute for a real app secret."""
    try:
        hashed = hashpw(b"offline-fixture_1643961623299", b"$2b$04$abcdefghijklmnopqrstuu")
    except (ValueError, TypeError):
        raise ContractQuarantine("synthetic_bcrypt_incompatible") from None
    if not isinstance(hashed, bytes) or len(hashed) != 60 or not hashed.startswith(b"$2b$04$"):
        raise ContractQuarantine("invalid_fixture_bcrypt_result")
    return FixtureSignature("naver", base64.b64encode(hashed).decode("ascii"))


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractQuarantine("timezone_aware_clock_required")
    return value.astimezone(timezone.utc)


def coupang_fixture_signature(plan: OfflineReadPlan, *, at: datetime) -> FixtureSignature:
    if not isinstance(plan, OfflineReadPlan) or plan.method != "GET" or plan.live_authorized:
        raise ContractQuarantine("offline_get_plan_required")
    stamp = _aware(at).strftime("%y%m%dT%H%M%SZ")
    message = stamp + plan.method + plan.path + plan.encoded_query
    digest = hmac.new(b"offline-fixture-key-not-a-vendor-key", message.encode("utf-8"), hashlib.sha256).hexdigest()
    return FixtureSignature("coupang", digest, stamp)


@dataclass(frozen=True)
class OfflineRetryPlan:
    action: str
    next_attempt: int
    delay_seconds: int | None = None
    executes_network: bool = field(default=False, init=False)


def _limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result = {}
    if not isinstance(headers, Mapping):
        raise ContractQuarantine("invalid_headers")
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ContractQuarantine("invalid_headers")
        lower = name.lower()
        if lower in result:
            raise ContractQuarantine("duplicate_header")
        # Only limit metadata is retained; Authorization/messages never enter output.
        if lower.startswith("gncp-gw-"):
            result[lower] = value
    return result


def _header_integer(headers: dict[str, str], name: str, *, positive: bool = False) -> int:
    value = headers.get(name)
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 7:
        raise ContractQuarantine("invalid_limit_header")
    number = int(value)
    if not (1 if positive else 0) <= number <= 1_000_000:
        raise ContractQuarantine("invalid_limit_header")
    return number


def plan_naver_fixture_retry(*, method: str, http_status: int, body: object,
                             headers: Mapping[str, str], attempts_used: int,
                             fixture_refresh_used: bool, now: datetime,
                             deadline: datetime, max_attempts: int = 3) -> OfflineRetryPlan:
    """Plan next fixture step only. Delays are local policy, not vendor promises."""
    current, expires = _aware(now), _aware(deadline)
    if (type(attempts_used) is not int or attempts_used < 1 or type(max_attempts) is not int
            or not 1 <= max_attempts <= 10 or type(fixture_refresh_used) is not bool):
        raise ContractQuarantine("invalid_retry_budget")
    stop = OfflineRetryPlan("STOP", attempts_used)
    if method != "GET" or attempts_used >= max_attempts or current >= expires:
        return stop
    decision = classify_naver_read_error(http_status, body)
    if decision.action == "REFRESH_REQUIRED":
        return stop if fixture_refresh_used else OfflineRetryPlan("REFRESH_FIXTURE_ONCE", attempts_used + 1)
    if decision.action not in {"RATE_LIMIT_REVIEW", "QUOTA_REVIEW"}:
        return OfflineRetryPlan("MANUAL_REVIEW", attempts_used)
    try:
        limits = _limit_headers(headers)
        if decision.action == "RATE_LIMIT_REVIEW":
            _header_integer(limits, "gncp-gw-ratelimit-replenish-rate", positive=True)
            _header_integer(limits, "gncp-gw-ratelimit-remaining")
        else:
            period = limits.get("gncp-gw-quota-period")
            _header_integer(limits, "gncp-gw-quota-limit", positive=True)
            _header_integer(limits, "gncp-gw-quota-remaining")
            if period != "SECONDS":
                return OfflineRetryPlan("MANUAL_REVIEW", attempts_used)
    except ContractQuarantine:
        return OfflineRetryPlan("MANUAL_REVIEW", attempts_used)
    delay = min(2 ** (attempts_used - 1), 30)
    if current + timedelta(seconds=delay) >= expires:
        return stop
    return OfflineRetryPlan("RETRY_FIXTURE_AFTER_DELAY", attempts_used + 1, delay)
