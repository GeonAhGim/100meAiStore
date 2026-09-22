"""Two-item synthetic split review and later-shipment identity reconciliation."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from .channel_claim_contracts import OfflineClaimPage
from .channel_order_contracts import ContractQuarantine, OfflineOrderPage
from .offline_tracking_contracts import _aware, _digest, _hash_ref, _ref


@dataclass(frozen=True)
class FixtureSplitItem:
    vendor_item_id: str = field(repr=False)
    shipment_id: str = field(repr=False)
    quantity: int
    invoice_number: str = field(repr=False)
    estimated_shipping_date: str
    split_shipping: bool = field(default=True, init=False)
    pre_split_shipped: bool = False
    carrier: str = field(default="KDEXP", init=False)


@dataclass(frozen=True)
class FixtureSplitReview:
    tenant_ref: str = field(repr=False)
    connection_ref: str = field(repr=False)
    order_id: str = field(repr=False)
    phase: str
    items: tuple[FixtureSplitItem, ...] = field(repr=False)
    source_digest: str = field(repr=False)
    claim_digest: str = field(repr=False)
    preparation_digest: str = field(repr=False)
    prior_review_digest: str | None = field(repr=False)
    observed_at: str
    claims_observed_at: str
    created_at: str
    expires_at: str
    external_write_authorized: bool = field(default=False, init=False)

    @property
    def approval_digest(self) -> str:
        return _digest(asdict(self))


def _invoice(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value):
        raise ContractQuarantine("invalid_fixture_invoice")
    return value


def _sources(page, claims, order_id, observed_at, claims_observed_at, now, expires_at, max_age_seconds):
    if not isinstance(page, OfflineOrderPage) or page.provider != "coupang" or page.next_cursor is not None:
        raise ContractQuarantine("complete_split_order_fixture_required")
    if not isinstance(claims, OfflineClaimPage) or claims.next_cursor is not None:
        raise ContractQuarantine("complete_claim_fixture_required")
    if any(line.order_id == order_id for line in claims.lines):
        raise ContractQuarantine("separate_claim_feed_requires_review")
    observed_at, claims_observed_at, now, expires_at = map(_aware, (observed_at, claims_observed_at, now, expires_at))
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
        raise ContractQuarantine("invalid_freshness_policy")
    for observed in (observed_at, claims_observed_at):
        if not timedelta(0) <= now - observed < timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("stale_or_future_observation")
        if not now < expires_at <= observed + timedelta(seconds=max_age_seconds):
            raise ContractQuarantine("invalid_review_expiry")
    lines = tuple(line for line in page.snapshots if line.order_id == order_id)
    if (len(lines) != 2 or len({line.product_id for line in lines}) != 2
            or any(line.claim_review_required or line.remaining_quantity <= 0
                   or line.initial_quantity != line.remaining_quantity for line in lines)):
        raise ContractQuarantine("two_complete_unclaimed_items_required")
    return lines


def build_initial_split_review(page: OfflineOrderPage, claims: OfflineClaimPage, *,
                               tenant_ref: str, connection_ref: str, order_id: str,
                               shipment_id: str, shipping_item_id: str, invoice_number: str,
                               deferred_date: str, preparation_digest: str,
                               post_preparation_reviewed: bool, observed_at: datetime,
                               claims_observed_at: datetime, now: datetime, expires_at: datetime,
                               max_age_seconds: int = 300) -> FixtureSplitReview:
    tenant_ref, connection_ref, order_id, shipment_id, shipping_item_id = map(
        _ref, (tenant_ref, connection_ref, order_id, shipment_id, shipping_item_id))
    lines = _sources(page, claims, order_id, observed_at, claims_observed_at, now, expires_at, max_age_seconds)
    if post_preparation_reviewed is not True:
        raise ContractQuarantine("post_preparation_review_required")
    if any(line.shipment_id != shipment_id or line.status != "INSTRUCT" for line in lines):
        raise ContractQuarantine("initial_split_state_mismatch")
    if shipping_item_id not in {line.product_id for line in lines}:
        raise ContractQuarantine("split_item_identity_mismatch")
    invoice_number = _invoice(invoice_number)
    try:
        deferred = date.fromisoformat(deferred_date)
        if deferred.isoformat() != deferred_date:
            raise ValueError
    except (TypeError, ValueError):
        raise ContractQuarantine("invalid_deferred_date") from None
    # Seven days is this fixture's review cap, not a vendor SLA.
    today = now.astimezone(timezone(timedelta(hours=9))).date()
    if not today <= deferred <= today + timedelta(days=7):
        raise ContractQuarantine("deferred_date_outside_fixture_window")
    items = tuple(FixtureSplitItem(line.product_id, shipment_id, line.remaining_quantity,
                                   invoice_number if line.product_id == shipping_item_id else "",
                                   "" if line.product_id == shipping_item_id else deferred_date)
                  for line in sorted(lines, key=lambda value: value.product_id))
    return FixtureSplitReview(tenant_ref, connection_ref, order_id, "INITIAL", items,
        _hash_ref(page.source_digest), _hash_ref(claims.source_digest), _hash_ref(preparation_digest), None,
        observed_at.isoformat(), claims_observed_at.isoformat(), now.isoformat(), expires_at.isoformat())


def build_followup_split_review(prior: FixtureSplitReview, page: OfflineOrderPage, claims: OfflineClaimPage, *,
                                prior_approval_digest: str, tenant_ref: str, connection_ref: str,
                                invoice_number: str, preparation_digest: str, post_preparation_reviewed: bool,
                                observed_at: datetime, claims_observed_at: datetime, now: datetime,
                                expires_at: datetime, max_age_seconds: int = 300) -> FixtureSplitReview:
    if not isinstance(prior, FixtureSplitReview) or prior.phase != "INITIAL" or len(prior.items) != 2:
        raise ContractQuarantine("initial_split_review_required")
    if (tenant_ref, connection_ref) != (prior.tenant_ref, prior.connection_ref):
        raise ContractQuarantine("approval_scope_mismatch")
    if _hash_ref(prior_approval_digest) != prior.approval_digest:
        raise ContractQuarantine("approval_digest_mismatch")
    lines = _sources(page, claims, prior.order_id, observed_at, claims_observed_at, now, expires_at, max_age_seconds)
    if post_preparation_reviewed is not True or observed_at < datetime.fromisoformat(prior.created_at):
        raise ContractQuarantine("fresh_post_split_review_required")
    sent = tuple(item for item in prior.items if item.invoice_number)
    deferred = tuple(item for item in prior.items if item.estimated_shipping_date)
    if len(sent) != 1 or len(deferred) != 1:
        raise ContractQuarantine("initial_split_review_required")
    sent, deferred = sent[0], deferred[0]
    by_item = {line.product_id: line for line in lines}
    if set(by_item) != {sent.vendor_item_id, deferred.vendor_item_id}:
        raise ContractQuarantine("split_item_identity_mismatch")
    shipped, waiting = by_item[sent.vendor_item_id], by_item[deferred.vendor_item_id]
    if (shipped.shipment_id == sent.shipment_id or shipped.status not in {"DEPARTURE", "DELIVERING", "FINAL_DELIVERY"}
            or shipped.remaining_quantity != sent.quantity or waiting.shipment_id != deferred.shipment_id
            or waiting.status != "INSTRUCT" or waiting.remaining_quantity != deferred.quantity):
        raise ContractQuarantine("split_remapping_reconciliation_required")
    invoice_number = _invoice(invoice_number)
    if invoice_number == sent.invoice_number:
        raise ContractQuarantine("split_invoice_reuse_requires_review")
    item = FixtureSplitItem(waiting.product_id, waiting.shipment_id, waiting.remaining_quantity, invoice_number, "", True)
    return FixtureSplitReview(tenant_ref, connection_ref, prior.order_id, "FOLLOWUP", (item,),
        _hash_ref(page.source_digest), _hash_ref(claims.source_digest), _hash_ref(preparation_digest), prior.approval_digest,
        observed_at.isoformat(), claims_observed_at.isoformat(), now.isoformat(), expires_at.isoformat())


def verify_split_review(plan: FixtureSplitReview, *, approval_digest: str, tenant_ref: str,
                         connection_ref: str, now: datetime) -> str:
    if not isinstance(plan, FixtureSplitReview):
        raise ContractQuarantine("split_review_required")
    if (tenant_ref, connection_ref) != (plan.tenant_ref, plan.connection_ref):
        raise ContractQuarantine("approval_scope_mismatch")
    if _hash_ref(approval_digest) != plan.approval_digest:
        raise ContractQuarantine("approval_digest_mismatch")
    if not datetime.fromisoformat(plan.created_at) <= _aware(now) < datetime.fromisoformat(plan.expires_at):
        raise ContractQuarantine("approval_time_invalid")
    return "FIXTURE_REVIEW_ONLY"
