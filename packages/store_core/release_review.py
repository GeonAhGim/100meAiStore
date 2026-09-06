"""Deletion-free fixture retention review. No operational policy is authorized."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .channel_order_contracts import ContractQuarantine, _identifier, _object, _rows, _stamp
from .offline_auth_contracts import _aware


DATA_CLASSES = frozenset({"channel_raw", "supplier_raw", "orders", "finance", "audit", "sessions", "content_candidates"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RetentionReview:
    record_ref: str
    data_class: str
    action: str
    reason: str
    deletion_authorized: bool = field(default=False, init=False)


def masked_fixture_metadata(rows: Any) -> tuple[dict[str, Any], ...]:
    """Export only this explicit review metadata, dropping every extra field."""
    results = []
    seen = set()
    for raw in _rows(rows, 1000):
        raw = _object(raw)
        ref = _identifier(raw.get("record_ref"))
        if ref in seen:
            raise ContractQuarantine("duplicate_record_reference")
        seen.add(ref)
        data_class = raw.get("data_class")
        if not isinstance(data_class, str) or data_class not in DATA_CLASSES:
            raise ContractQuarantine("unknown_retention_class")
        if type(raw.get("legal_hold")) is not bool:
            raise ContractQuarantine("legal_hold_flag_required")
        results.append({"record_ref": ref, "data_class": data_class,
                        "created_at": _stamp(raw.get("created_at")), "legal_hold": raw["legal_hold"]})
    return tuple(results)


def plan_fixture_retention(rows: Any, policies: dict[str, dict], *, as_of: datetime) -> tuple[RetentionReview, ...]:
    now = _aware(as_of)
    if not isinstance(policies, dict):
        raise ContractQuarantine("policy_map_required")
    results = []
    for row in masked_fixture_metadata(rows):
        created = _aware(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")))
        if created > now:
            raise ContractQuarantine("future_record_timestamp")
        ref, data_class = row["record_ref"], row["data_class"]
        if row["legal_hold"]:
            results.append(RetentionReview(ref, data_class, "RETAIN", "legal_hold"))
            continue
        rule = policies.get(data_class)
        if not isinstance(rule, dict) or set(rule) != {"retention_days", "fixture_approval_digest"}:
            results.append(RetentionReview(ref, data_class, "REVIEW_POLICY", "policy_unresolved"))
            continue
        days, approval = rule["retention_days"], rule["fixture_approval_digest"]
        if (type(days) is not int or not 1 <= days <= 36500 or not isinstance(approval, str)
                or not _SHA256.fullmatch(approval)):
            results.append(RetentionReview(ref, data_class, "REVIEW_POLICY", "policy_unresolved"))
            continue
        # Duration is a synthetic test policy, not a legal calendar calculation.
        if now - created >= timedelta(days=days):
            results.append(RetentionReview(ref, data_class, "ELIGIBLE_FOR_REVIEW", "fixture_retention_elapsed"))
        else:
            results.append(RetentionReview(ref, data_class, "RETAIN", "fixture_retention_active"))
    return tuple(results)
