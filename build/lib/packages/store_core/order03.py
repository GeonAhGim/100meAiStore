"""Local DEMO cancellation races and line-level tracking observations."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from .domain import (Capability, ChannelOrderState, OutboxEvent, OutboxState,
                     PurchaseOrderState, TrackingObservation)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
_STATUSES = frozenset({"LABEL_CREATED", "IN_TRANSIT", "DELIVERED", "CANCELLED"})


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def request_demo_cancel(service: Any, context: Any, order_id: str, reason: str,
                        expected_order_version: int):
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(reason, str) or not reason.strip() or type(expected_order_version) is not int:
        raise ConflictError("cancellation reason and version are required")
    with service.repo.transaction():
        order = service.repo.get_channel_order(context.tenant_id, order_id)
        if order.status == ChannelOrderState.CANCELLED:
            return order, True
        if order.version != expected_order_version or order.status != ChannelOrderState.PO_PENDING:
            raise ConflictError("order cancellation version/state conflict")
        pos = service.repo.purchase_orders_for(context.tenant_id, order.id)
        order.status, order.version = ChannelOrderState.CANCELLED, order.version + 1
        service.repo.update_channel_order(order, expected_order_version)
        now = service._clock()
        for po in pos:
            if po.status in {PurchaseOrderState.APPROVAL_PENDING, PurchaseOrderState.APPROVED}:
                po.status, po.version = PurchaseOrderState.CANCELLED, po.version + 1
                service.repo.update_purchase_order(po, po.version - 1)
            elif po.status in {PurchaseOrderState.SUBMITTED, PurchaseOrderState.ACKNOWLEDGED}:
                po.status, po.version = PurchaseOrderState.CANCEL_REQUESTED, po.version + 1
                service.repo.update_purchase_order(po, po.version - 1)
                service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id,
                    "purchase_order.cancel_requested", po.id,
                    {"purchase_order_id": po.id, "reason": reason, "mode": "DEMO"},
                    f"po:{po.id}:cancel-requested", OutboxState.PENDING, now))
        service._audit(context.tenant_id, context.user_id, "order.cancelled", order.id, "succeeded", {"reason": reason})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "order.cancelled", order.id,
            {"order_id": order.id, "mode": "DEMO"}, f"order:{order.id}:cancelled", OutboxState.PENDING, now))
        return order, False


def ingest_demo_tracking(service: Any, context: Any, order_line_id: str, tracking_key: str,
                         status: str, observed_at: datetime | None = None,
                         expected_line_version: int | None = None):
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(tracking_key, str) or not _OPAQUE.fullmatch(tracking_key) or status not in _STATUSES:
        raise ConflictError("invalid DEMO tracking identity/status")
    observed_at = observed_at or service._clock()
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ConflictError("tracking timestamp must be timezone aware")
    digest = _digest({"tracking_key": tracking_key, "status": status})
    with service.repo.transaction():
        line = service.repo.get_order_line(context.tenant_id, order_line_id)
        order = service.repo.get_channel_order(context.tenant_id, line.channel_order_id)
        if line.routed_status != "routed" or order.status not in {ChannelOrderState.PO_PENDING, ChannelOrderState.CANCELLED}:
            raise ConflictError("tracking is not applicable to this order line")
        prior = service.repo.tracking_observation_for(context.tenant_id, line.id, tracking_key, status)
        if prior is not None:
            return line, True
        if expected_line_version is not None and line.version != expected_line_version:
            raise ConflictError("order line version conflict")
        observation = service.repo.save_tracking_observation(TrackingObservation(
            str(uuid4()), context.tenant_id, line.id, tracking_key, status, observed_at, digest))
        expected = line.version
        line.tracking_key, line.tracking_status = tracking_key, status
        line.tracking_version, line.tracking_observed_at = line.tracking_version + 1, observed_at
        line.version += 1
        service.repo.update_order_line(line, expected)
        service._audit(context.tenant_id, context.user_id, "tracking.observed", line.id, "succeeded", {"status": status, "response_digest": observation.response_digest})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "tracking.observed", line.id,
            {"order_line_id": line.id, "tracking_key": tracking_key, "status": status, "response_digest": digest},
            f"tracking:{line.id}:{tracking_key}:{status}", OutboxState.PENDING, observed_at))
        return line, False
