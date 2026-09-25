"""Local DEMO notification fallback simulator and incident acknowledgement."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .domain import Capability, DemoIncidentAcknowledgement, DemoNotificationDelivery, DemoNotificationPreference, OutboxEvent, OutboxState, UrgentNotificationCategory
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


# --- Queryable notification traceability (M2.4) ---


def query_notifications(
    service: StoreControlPlane,
    tenant_id: str,
    incident_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[DemoNotificationDelivery]:
    """Query notification delivery records for a tenant.

    Returns notifications matching the given filters, newest first.
    Used by the dashboard to show notification traceability.
    """
    records = list(service.repo.notification_deliveries_for(tenant_id))
    if incident_id is not None:
        records = [r for r in records if r.notification_key == incident_id]
    if state is not None:
        records = [r for r in records if r.state == state]
    records.sort(key=lambda r: r.sent_at, reverse=True)
    return records[:limit]


def notify_urgent_demo(service: Any, context: Any, incident_key: str, category: str,
                       payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
    """Send urgent notification immediately via primary channel (app_push).

    Category must be one of UrgentNotificationCategory values. The notification is sent
    immediately to app_push; if unacknowledged after 5 minutes, check_and_escalate_incidents()
    will escalate to email. Multiple senders of the same incident_key are deduplicated;
    acknowledgement by any tenant member blocks further escalations.
    """
    service.require(context, Capability.TENANT_ADMIN)
    incident_key, idempotency_key = _opaque(incident_key, "incident_key"), _opaque(idempotency_key, "idempotency_key")

    if category not in (c.value for c in UrgentNotificationCategory):
        raise ConflictError(f"invalid urgent notification category: {category}")
    if not isinstance(payload, Mapping):
        raise ConflictError("notification payload must be a dict")

    payload_with_category = dict(payload)
    payload_with_category["category"] = category
    try:
        encoded = json.dumps(payload_with_category, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConflictError("notification payload must be finite JSON") from exc
    if len(encoded.encode()) > 16 * 1024:
        raise ConflictError("notification payload too large")

    with service.repo.transaction():
        existing_deliveries = [d for d in service.repo.notification_deliveries_for(context.tenant_id) if d.notification_key == incident_key]
        if existing_deliveries:
            first = min(existing_deliveries, key=lambda d: d.created_at)
            return {"state": first.state, "channel": first.channel, "deliveries": [first], "replayed": True}

        pref = service.repo.get_notification_preference(context.tenant_id, incident_key)
        if pref is None:
            pref = DemoNotificationPreference(context.tenant_id, incident_key, ("app_push", "email"), False)
            service.repo.save_notification_preference(pref)

        delivery = DemoNotificationDelivery(
            str(uuid4()), context.tenant_id, incident_key, "app_push", encoded,
            "DELIVERED", 1, None, f"{idempotency_key}:app_push", service._clock()
        )
        delivery, replay = service.repo.save_notification_delivery(delivery)

        if not replay:
            service._audit(
                context.tenant_id, context.user_id, "notification.urgent_sent",
                incident_key, "succeeded", {"category": category, "channel": "app_push"}
            )

        return {"state": delivery.state, "channel": delivery.channel, "deliveries": [delivery], "replayed": replay}


def check_and_escalate_incidents(service: Any, tenant_id: str, current_time: Any) -> dict[str, Any]:
    """Escalate unacknowledged urgent notifications to secondary channel (email) after 5 minutes.

    For each unacknowledged urgent incident created more than 5 minutes ago, send via email.
    This is typically called by a background scheduler. Returns count of escalations.
    """
    with service.repo.transaction():
        all_deliveries = list(service.repo.notification_deliveries_for(tenant_id))

        escalated_count = 0
        escalation_cutoff = current_time - timedelta(minutes=5)

        for delivery in all_deliveries:
            if delivery.state != "DELIVERED" or delivery.channel != "app_push":
                continue

            try:
                payload = json.loads(delivery.payload_json)
            except (json.JSONDecodeError, ValueError):
                continue

            category = payload.get("category")
            if category not in (c.value for c in UrgentNotificationCategory):
                continue

            if delivery.created_at > escalation_cutoff:
                continue

            acks = list(service.repo.acknowledgements_for(tenant_id, delivery.notification_key))
            if acks:
                continue

            escalated_deliveries = list(service.repo.notification_deliveries_for(tenant_id))
            email_sent = any(
                d.notification_key == delivery.notification_key and d.channel == "email"
                for d in escalated_deliveries
            )
            if email_sent:
                continue

            escalation_delivery = DemoNotificationDelivery(
                str(uuid4()), tenant_id, delivery.notification_key, "email",
                delivery.payload_json, "DELIVERED", 2, "app_push",
                f"{delivery.idempotency_key}:email", current_time
            )
            escalation_delivery, _ = service.repo.save_notification_delivery(escalation_delivery)
            escalated_count += 1

        return {"escalated_count": escalated_count, "cutoff_time": escalation_cutoff}
