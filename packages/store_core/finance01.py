"""Strict local DEMO settlement import and order subledger matching."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .domain import (Capability, DemoRealizedProfit, DemoSettlementBatch,
                     DemoSettlementLine, OutboxEvent, OutboxState, SettlementStatus)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
_KINDS = {"SALE", "FEE", "REFUND"}
_CURRENCIES = {"KRW", "USD", "JPY", "EUR", "GBP", "CNY"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def import_demo_settlement(service: Any, context: Any, channel_id: str, period: str,
                           rows: Sequence[Mapping[str, Any]], idempotency_key: str):
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(channel_id, str) or not _OPAQUE.fullmatch(channel_id) or not isinstance(period, str) or not _OPAQUE.fullmatch(period) or not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255 or not isinstance(rows, (list, tuple)) or not rows:
        raise ConflictError("invalid settlement import")
    canonical_rows = []
    seen_refs = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"external_order_key", "kind", "amount_minor", "currency", "source_row_ref"}:
            raise ConflictError("invalid settlement row schema")
        external, kind, amount, currency, source = row.values()
        if not isinstance(external, str) or not _OPAQUE.fullmatch(external) or kind not in _KINDS or type(amount) is not int or not isinstance(currency, str) or currency not in _CURRENCIES or not isinstance(source, str) or not _OPAQUE.fullmatch(source) or source in seen_refs:
            raise ConflictError("invalid settlement row")
        seen_refs.add(source)
        canonical_rows.append({"external_order_key": external, "kind": kind, "amount_minor": amount, "currency": currency, "source_row_ref": source})
    source_digest = _digest(canonical_rows)
    now = service._clock()
    with service.repo.transaction():
        batch = DemoSettlementBatch(str(uuid4()), context.tenant_id, channel_id, period, source_digest, SettlementStatus.IMPORTED, idempotency_key, now)
        batch, replay = service.repo.save_settlement_batch(batch)
        if replay: return batch, True
        grouped: dict[str, list[DemoSettlementLine]] = {}
        all_match = True
        for row in canonical_rows:
            order = service.repo.find_channel_order(context.tenant_id, channel_id, row["external_order_key"])
            match = "matched" if order is not None and order.currency == row["currency"] else "exception"
            if order is None or order.currency != row["currency"]: all_match = False
            line = DemoSettlementLine(str(uuid4()), context.tenant_id, batch.id, row["external_order_key"], row["kind"], row["amount_minor"], row["currency"], row["source_row_ref"], order.id if order else None, match)
            service.repo.save_settlement_line(line)
            if order: grouped.setdefault(order.id, []).append(line)
        for order_id, lines in grouped.items():
            order = service.repo.get_channel_order(context.tenant_id, order_id)
            sale_total = sum(line.amount_minor for line in lines if line.kind == "SALE")
            status = "reconciled" if sale_total == order.total_minor and all(line.match_status == "matched" for line in lines) else "exception"
            if status != "reconciled": all_match = False
            realized = sum(line.amount_minor for line in lines) if status == "reconciled" else None
            service.repo.save_realized_profit(DemoRealizedProfit(str(uuid4()), context.tenant_id, batch.id, order_id, None, realized, status, now))
        batch.status = SettlementStatus.RECONCILED if all_match else SettlementStatus.EXCEPTION
        # Batch status is immutable in the logical import, but this local update
        # is part of the same transaction and has no external effect.
        batch.version += 1
        service.repo.update_settlement_batch(batch, 1)
        service._audit(context.tenant_id, context.user_id, "settlement.imported", batch.id, "succeeded", {"status": batch.status.value, "source_digest": source_digest})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "settlement.imported", batch.id,
            {"batch_id": batch.id, "status": batch.status.value}, f"settlement:{batch.id}:imported", OutboxState.PENDING, now))
        return batch, False
