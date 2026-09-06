"""Local DEMO notification fallback simulator and incident acknowledgement."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .domain import Capability, DemoIncidentAcknowledgement, DemoNotificationDelivery, DemoNotificationPreference, OutboxEvent, OutboxState
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_CHANNELS = ("app_push", "email", "chatgpt")


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value): raise ConflictError(f"invalid {label}")
    return value


def set_demo_notification_preference(service: Any, context: Any, notification_key: str,
                                     channels: Sequence[str] = _CHANNELS, muted: bool = False) -> DemoNotificationPreference:
    service.require(context, Capability.TENANT_ADMIN)
    notification_key = _opaque(notification_key, "notification_key")
    if not isinstance(channels, (list, tuple)) or not channels or len(set(channels)) != len(channels) or any(channel not in _CHANNELS for channel in channels) or type(muted) is not bool:
        raise ConflictError("invalid notification preference")
    with service.repo.transaction():
        prior = service.repo.get_notification_preference(context.tenant_id, notification_key)
        value = service.repo.save_notification_preference(DemoNotificationPreference(context.tenant_id, notification_key, tuple(channels), muted, prior.version + 1 if prior else 1))
        service._audit(context.tenant_id, context.user_id, "notification.preference_changed", notification_key, "succeeded", {"muted": muted})
        return value


def notify_demo(service: Any, context: Any, notification_key: str, payload: Mapping[str, Any],
                idempotency_key: str, failed_channels: Sequence[str] = ()) -> dict[str, Any]:
    service.require(context, Capability.TENANT_ADMIN)
    notification_key, idempotency_key = _opaque(notification_key, "notification_key"), _opaque(idempotency_key, "idempotency_key")
    if not isinstance(payload, Mapping) or not isinstance(failed_channels, (list, tuple)) or any(channel not in _CHANNELS for channel in failed_channels): raise ConflictError("invalid DEMO notification")
    try: encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc: raise ConflictError("notification payload must be finite JSON") from exc
    if len(encoded.encode()) > 16 * 1024: raise ConflictError("notification payload too large")
    with service.repo.transaction():
        pref = service.repo.get_notification_preference(context.tenant_id, notification_key) or DemoNotificationPreference(context.tenant_id, notification_key, _CHANNELS, False)
        if pref.muted:
            delivery = DemoNotificationDelivery(str(uuid4()), context.tenant_id, notification_key, "none", encoded, "MUTED", 1, None, idempotency_key, service._clock())
            delivery, replay = service.repo.save_notification_delivery(delivery)
            return {"state": delivery.state, "channel": delivery.channel, "deliveries": [delivery], "replayed": replay}
        deliveries = []
        previous = None
        replayed = True
        for attempt, channel in enumerate(pref.channels, 1):
            state = "FAILED" if channel in failed_channels else "DELIVERED"
            delivery = DemoNotificationDelivery(str(uuid4()), context.tenant_id, notification_key, channel, encoded, state, attempt, previous, f"{idempotency_key}:{channel}", service._clock())
            delivery, replay = service.repo.save_notification_delivery(delivery)
            replayed = replayed and replay
            deliveries.append(delivery)
            if state == "DELIVERED": break
            previous = channel
        final = deliveries[-1]
        if replayed:
            return {"state": final.state, "channel": final.channel, "deliveries": deliveries, "replayed": True}
        service._audit(context.tenant_id, context.user_id, "notification.simulated", notification_key, "succeeded" if final.state == "DELIVERED" else "blocked", {"channel": final.channel, "state": final.state})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "notification.simulated", notification_key, {"notification_key": notification_key, "state": final.state, "channel": final.channel}, f"notification:{idempotency_key}:simulated", OutboxState.PENDING, final.created_at))
        return {"state": final.state, "channel": final.channel, "deliveries": deliveries, "replayed": replayed}


def acknowledge_demo_incident(service: Any, context: Any, incident_id: str, note: str, idempotency_key: str) -> tuple[DemoIncidentAcknowledgement, bool]:
    service.require(context, Capability.TENANT_ADMIN)
    incident_id, idempotency_key = _opaque(incident_id, "incident_id"), _opaque(idempotency_key, "idempotency_key")
    if not isinstance(note, str) or not note.strip() or len(note) > 1000: raise ConflictError("acknowledgement note is required")
    with service.repo.transaction():
        value, replay = service.repo.save_incident_acknowledgement(DemoIncidentAcknowledgement(str(uuid4()), context.tenant_id, incident_id, context.user_id, note.strip(), idempotency_key, service._clock()))
        if not replay:
            service._audit(context.tenant_id, context.user_id, "incident.acknowledged", incident_id, "succeeded", {})
        return value, replay
