"""Local DEMO claim intake with independent party status checkpoints."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any
from uuid import uuid4

from .domain import (Capability, ClaimStatus, ClaimStatusObservation, DemoClaim,
                     OutboxEvent, OutboxState)
from .errors import ConflictError

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def open_demo_claim(service: Any, context: Any, order_id: str, claim_type: str,
                    amount_minor: int, idempotency_key: str):
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(claim_type, str) or not _OPAQUE.fullmatch(claim_type) or type(amount_minor) is not int or amount_minor < 0 or not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ConflictError("invalid claim intake")
    with service.repo.transaction():
        order = service.repo.get_channel_order(context.tenant_id, order_id)
        if amount_minor > order.total_minor:
            raise ConflictError("claim amount exceeds order total")
        now = service._clock()
        claim = DemoClaim(str(uuid4()), context.tenant_id, order.id, claim_type, amount_minor,
                          ClaimStatus.OPEN, ClaimStatus.OPEN, ClaimStatus.OPEN, idempotency_key, now)
        claim, replay = service.repo.save_claim(claim)
        if replay: return claim, True
        service._audit(context.tenant_id, context.user_id, "claim.opened", claim.id, "succeeded", {"order_id": order.id, "claim_type": claim_type, "amount_minor": amount_minor})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "claim.opened", claim.id,
            {"claim_id": claim.id, "order_id": order.id}, f"claim:{claim.id}:opened", OutboxState.PENDING, now))
        return claim, False


def record_demo_claim_status(service: Any, context: Any, claim_id: str, status_kind: str,
                             status: str, expected_version: int):
    service.require(context, Capability.TENANT_ADMIN)
    if status_kind not in {"consumer", "channel", "supplier"} or status not in {item.value for item in ClaimStatus} or type(expected_version) is not int:
        raise ConflictError("invalid claim status transition")
    target = ClaimStatus(status)
    with service.repo.transaction():
        claim = service.repo.get_claim(context.tenant_id, claim_id)
        prior = service.repo.claim_observation_for(context.tenant_id, claim.id, status_kind, target)
        if prior is not None: return claim, True
        if claim.version != expected_version: raise ConflictError("claim version conflict")
        observed = service._clock()
        digest = _digest({"status_kind": status_kind, "status": status})
        service.repo.save_claim_observation(ClaimStatusObservation(str(uuid4()), context.tenant_id, claim.id, status_kind, target, observed, digest))
        setattr(claim, f"{status_kind}_status", target)
        claim.version += 1
        service.repo.update_claim(claim, expected_version)
        service._audit(context.tenant_id, context.user_id, "claim.status_observed", claim.id, "succeeded", {"status_kind": status_kind, "status": status, "response_digest": digest})
        service.repo.append_outbox(OutboxEvent(str(uuid4()), context.tenant_id, "claim.status_observed", claim.id,
            {"claim_id": claim.id, "status_kind": status_kind, "status": status, "response_digest": digest}, f"claim:{claim.id}:{status_kind}:{status}", OutboxState.PENDING, observed))
        return claim, False
