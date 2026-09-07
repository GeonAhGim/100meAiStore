"""Bounded channel-schema fixtures feeding the local DEMO order service.

No transport, signing, credentials or marketplace execution exists here. The
fixture-prefixed provider identity cannot represent a live channel connection.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .channel_claim_contracts import parse_coupang_claim_page
from .channel_order_contracts import (
    ContractQuarantine, _hash, canonical_json, parse_coupang_day_page, parse_naver_details,
)
from .domain import DemoAdapterDescription, DemoPage
from .ingestion import normalize_demo_order


class ContractFixtureReadAdapter:
    adapter_version = "contract-fixture-orders-v1"

    def __init__(self, provider, body, *, sku_bindings, observed_at,
                 requested_ids=(), claim_body=None, clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._observed_at = observed_at
        self._fresh()
        if provider not in ("naver", "coupang") or not isinstance(sku_bindings, dict):
            raise ContractQuarantine("unsupported_fixture_provider_or_mapping")
        self.provider = "fixture-" + provider
        claims_digest = None
        if provider == "naver":
            page = parse_naver_details(body, requested_ids=requested_ids)
        else:
            page = parse_coupang_day_page(body)
            claims = parse_coupang_claim_page(claim_body)
            if claims.next_cursor is not None or claims.lines:
                raise ContractQuarantine("fixture_claim_reconciliation_required")
            claims_digest = claims.source_digest
        if page.next_cursor is not None or not page.snapshots:
            raise ContractQuarantine("complete_nonempty_fixture_page_required")
        grouped = {}
        for row in page.snapshots:
            if (row.status != ("PAYED" if provider == "naver" else "INSTRUCT")
                    or row.claim_review_required or row.initial_quantity != row.remaining_quantity):
                raise ContractQuarantine("unsupported_fixture_order_state")
            money = dict(row.amounts_krw)
            unit, quantity = money["unit_price"], row.remaining_quantity
            if provider == "naver":
                if money["initial_payment"] != unit * quantity or money["remaining_payment"] != unit * quantity:
                    raise ContractQuarantine("fixture_discount_or_partial_payment_unsupported")
            elif any(money[key] != 0 for key in ("discount", "shipment_shipping", "shipment_remote")):
                raise ContractQuarantine("fixture_extra_or_unknown_amount_unsupported")
            sku = sku_bindings.get(row.product_id)
            if not isinstance(sku, str):
                raise ContractQuarantine("explicit_fixture_sku_binding_required")
            grouped.setdefault(row.order_id, []).append((row, sku, unit, quantity))
        items = []
        for order_id, rows in sorted(grouped.items()):
            # Reject duplicate mapped SKUs: core order lines currently have no
            # durable channel-line identity and must not silently merge them.
            if len({sku for _, sku, _, _ in rows}) != len(rows):
                raise ContractQuarantine("duplicate_fixture_sku_mapping")
            digest = _hash({"provider": provider, "claims": claims_digest,
                            "lines": sorted((row.line_id, row.source_digest, sku) for row, sku, _, _ in rows)})
            item = {"external_order_id": order_id, "event_id": "fixture-" + digest,
                    "revision": 1, "currency": "KRW",
                    "total_minor": sum(unit * quantity for _, _, unit, quantity in rows),
                    "lines": [{"sku": sku, "quantity": quantity, "unit_minor": unit,
                               "source_line_key": row.line_id}
                              for row, sku, unit, quantity in sorted(rows, key=lambda value: value[0].line_id)],
                    "source_digest": digest}
            normalize_demo_order(item)
            items.append(item)
        if len(items) > 100:
            raise ContractQuarantine("fixture_order_count_exceeded")
        self._items_json = canonical_json(items)

    def _fresh(self):
        now = self._clock()
        if (not isinstance(self._observed_at, datetime) or self._observed_at.utcoffset() is None
                or not isinstance(now, datetime) or now.utcoffset() is None
                or not 0 <= (now - self._observed_at).total_seconds() <= 900):
            raise ContractQuarantine("fresh_fixture_observation_required")

    def describe(self):
        return DemoAdapterDescription(self.provider, self.adapter_version)

    def list_changes(self, cursor, overlap_from=None):
        self._fresh()
        if cursor is not None or overlap_from is not None:
            raise ContractQuarantine("fixture_continuation_unsupported")
        return DemoPage(tuple(json.loads(self._items_json)), None, False, self._observed_at)
