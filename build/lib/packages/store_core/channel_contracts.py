"""Public-document contract scaffolding; data plans only, no network executor.

Sources and unresolved LIVE requirements: docs/implementation/live-phase2-backlog.md.
These offline plans do not declare an available channel capability.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping
from urllib.parse import urlencode


COUPANG_DAY_PAGE_SOURCE = "https://developers.coupang.com/ko/api/shipments/po-list-query-paging-by-day"
NAVER_AUTH_SOURCE = "https://apicenter.commerce.naver.com/docs/auth"
NAVER_LIMIT_SOURCE = "https://apicenter.commerce.naver.com/docs/restriction"
COUPANG_STATES = frozenset({"ACCEPT", "INSTRUCT", "DEPARTURE", "DELIVERING",
                            "FINAL_DELIVERY", "NONE_TRACKING"})
_LOCAL_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True)
class OfflineReadPlan:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    path: str = field(repr=False)
    query: tuple[tuple[str, str], ...] = field(repr=False)
    source: str
    method: str = field(default="GET", init=False)
    mode: str = field(default="OFFLINE_CONTRACT", init=False)
    live_authorized: bool = field(default=False, init=False)

    @property
    def encoded_query(self) -> str:
        """Encode exactly once; no '?' prefix. Never log opaque cursor values."""
        return urlencode(self.query)


def coupang_day_page_plan(*, tenant_ref: str, connection_ref: str, vendor_id: str,
                          start: date, end: date, status: str, max_per_page: int = 50,
                          next_token: str | None = None) -> OfflineReadPlan:
    """Construct a KR request fixture from C3, without credentials or transport.

    Local limits: ASCII path references, <=31 inclusive calendar dates and
    <=4096-character opaque cursor. These are conservative client constraints;
    the reference grammar is not a claim about all vendor-issued identifiers.
    """
    for ref in (tenant_ref, connection_ref, vendor_id):
        if not isinstance(ref, str) or not _LOCAL_REFERENCE.fullmatch(ref):
            raise ValueError("invalid local reference")
    # datetime is a date subclass but would silently change this endpoint's format.
    if type(start) is not date or type(end) is not date:
        raise ValueError("calendar dates required")
    if not 0 <= (end - start).days <= 30:
        raise ValueError("date window must contain 1 to 31 calendar dates")
    if not isinstance(status, str) or status not in COUPANG_STATES:
        raise ValueError("unsupported ordersheet state")
    if type(max_per_page) is not int or not 1 <= max_per_page <= 50:
        raise ValueError("page size must be 1 to 50")
    if next_token is not None and (
        not isinstance(next_token, str) or not 1 <= len(next_token) <= 4096
        or not next_token.isprintable()
    ):
        raise ValueError("invalid opaque page cursor")
    query = [("createdAtFrom", start.isoformat() + "+09:00"),
             ("createdAtTo", end.isoformat() + "+09:00"),
             ("maxPerPage", str(max_per_page)), ("status", status)]
    if next_token is not None:
        query.append(("nextToken", next_token))
    return OfflineReadPlan(tenant_ref, connection_ref,
                           f"/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/ordersheets",
                           tuple(query), COUPANG_DAY_PAGE_SOURCE)


@dataclass(frozen=True)
class ReadErrorDecision:
    action: str
    reason: str
    source: str | None = None
    automatic_retry: bool = field(default=False, init=False)


def classify_naver_read_error(http_status: int, body: Any) -> ReadErrorDecision:
    """N2/N3 error routing only; never executes refresh, wait or retry.

    Raw vendor messages may contain PII/secrets and are not retained in output.
    All unknown errors require review. Retry budgets/headers belong to P2-04.
    """
    if type(http_status) is not int or not 400 <= http_status <= 599:
        raise ValueError("HTTP error status required")
    code = body.get("code") if isinstance(body, Mapping) else None
    if http_status == 401 and code == "GW.AUTHN":
        return ReadErrorDecision("REFRESH_REQUIRED", "documented authentication failure", NAVER_AUTH_SOURCE)
    if http_status == 429 and code == "GW.RATE_LIMIT":
        return ReadErrorDecision("RATE_LIMIT_REVIEW", "inspect rate limit headers and retry budget", NAVER_LIMIT_SOURCE)
    if http_status == 429 and code == "GW.QUOTA_LIMIT":
        return ReadErrorDecision("QUOTA_REVIEW", "inspect quota period and remaining allowance", NAVER_LIMIT_SOURCE)
    return ReadErrorDecision("MANUAL_REVIEW", "unclassified provider error")
