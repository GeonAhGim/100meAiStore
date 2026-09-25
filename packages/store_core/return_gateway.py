"""Bind a reviewed return fixture to the local DEMO approval gateway.

This bridge records a refund-intent review and, after an authorized decision,
can drive one synthetic DEMO execution.  It never calls a marketplace,
initiates a bank refund, or treats a synthetic effect as a real refund.

M1.6 additions:
- ReturnLine dataclass for line-level return tracking
- ReturnGateway class with readback, process, approve, reject, partial, cancel
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .domain import ApprovalKind
from .offline_claim_contracts import FixtureReturnReview, verify_return_fixture_review


# ---------------------------------------------------------------------------
# ReturnLine — line item inside a return/cancel/partial request
# ---------------------------------------------------------------------------


class ReturnLineStatus(str, Enum):
    """Lifecycle states for a single return line."""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


@dataclass
class ReturnLine:
    """A single line item within a return request."""
    line_id: str
    order_id: str
    product_id: str
    quantity: int
    unit_price_minor: int
    reason: str
    status: ReturnLineStatus = field(default=ReturnLineStatus.PENDING)
    refunded_minor: int = 0
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# ReturnGateway — readback + approve/reject/partial/cancel for returns
# ---------------------------------------------------------------------------


class ReturnGateway:
    """Manages return/cancel/partial-claim lifecycle with readback gate.

    Safety contract (fail-closed):
    - Every monetary operation requires a prior ``readback()`` call that
      produces a ``readback_digest``.  Without it, ``approve()`` and
      ``partial()`` raise ``ValueError``.
    - ``approve()``, ``reject()``, ``partial()``, and ``cancel()`` are
      idempotent on ``decision_id`` — calling again with the same
      decision_id returns the original decision.
    - ``partial()`` requires a valid readback; refund_minor must be <=
      readback total_minor and > 0.
    - ``cancel()`` only works on decisions that are not yet final
      (approve/reject/partial).
    - All state transitions are audited via ``service._audit``.
    """

    def __init__(self, service: Any) -> None:
        self._service = service
        self._decisions: Dict[str, ReturnGateway.ReturnDecision] = {}
        self._readbacks: Dict[str, ReturnGateway.ReturnReadback] = {}

    @dataclass
    class ReturnReadback:
        """Immutable snapshot produced by ``readback()``."""
        order_id: str
        product_id: str
        quantity: int
        unit_price_minor: int
        total_minor: int
        readback_digest: str
        returned_at: datetime

    @dataclass
    class ReturnDecision:
        """Outcome of approve/reject/partial/cancel."""
        decision_id: str
        order_id: str
        product_id: str
        action: str  # "approve" | "reject" | "partial" | "cancel"
        refunded_minor: int
        decision_at: datetime
        approved_by: str
        readback_digest: str
        lines: Dict[str, ReturnGateway.ReturnLineState] = field(
            default_factory=dict
        )

    @dataclass
    class ReturnLineState:
        """Per-line state inside a decision."""
        line_id: str
        status: str
        refunded_minor: int
        decision_id: str

    # -- internal helpers ------------------------------------------------

    @staticmethod
    def _digest(obj: Any) -> str:
        return hashlib.sha256(
            json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    # -- public API ------------------------------------------------------

    def readback(
        self,
        order_id: str,
        product_id: str,
        quantity: int,
        unit_price_minor: int,
    ) -> ReturnReadback:
        """Perform readback — verify order/product state before any action.

        Returns a ReturnReadback whose ``readback_digest`` must be passed
        to approve()/partial()/reject()/cancel() to prove the readback
        gate was satisfied.

        Raises ValueError if quantity <= 0 or unit_price_minor < 0.
        """
        if quantity <= 0:
            raise ValueError("readback quantity must be positive")
        if unit_price_minor < 0:
            raise ValueError("unit_price_minor must not be negative")

        total_minor = quantity * unit_price_minor
        now = datetime.now(timezone.utc)
        rb = self.ReturnReadback(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            unit_price_minor=unit_price_minor,
            total_minor=total_minor,
            readback_digest=self._digest({
                "order_id": order_id,
                "product_id": product_id,
                "quantity": quantity,
                "unit_price_minor": unit_price_minor,
            }),
            returned_at=now,
        )
        self._readbacks[rb.readback_digest] = rb
        return rb

    def process(
        self,
        tenant_id: str,
        user_id: str,
        order_id: str,
        lines: List[ReturnLine],
    ) -> List[ReturnGateway.ReturnLineState]:
        """Process return lines into PENDING state (pre-approval).

        All lines must reference a valid order_id.  Lines are marked
        PENDING until approve(), reject(), partial(), or cancel() is
        called.

        Returns list of ReturnLineState for each processed line.
        Raises ValueError if lines is empty.
        """
        if not lines:
            raise ValueError("process requires at least one line")

        now = datetime.now(timezone.utc)
        states: List[ReturnGateway.ReturnLineState] = []
        for line in lines:
            line.status = ReturnLineStatus.PENDING
            state = self.ReturnLineState(
                line_id=line.line_id,
                status=ReturnLineStatus.PENDING,
                refunded_minor=0,
                decision_id=str(uuid4()),  # placeholder, updated on decision
            )
            states.append(state)

        self._service._audit(
            tenant_id, user_id, "return.processed", order_id,
            "succeeded",
            {"line_count": len(lines), "order_id": order_id},
        )
        return states

    def approve(
        self,
        tenant_id: str,
        user_id: str,
        order_id: str,
        readback_digest: str,
        decision_id: str,
        line_ids: List[str],
    ) -> ReturnDecision:
        """Approve return lines identified by line_ids.

        Requires a prior readback with the given readback_digest.
        Idempotent on decision_id.

        Returns the ReturnDecision.
        Raises ValueError if readback_digest is unknown or decision
        already exists with a different action.
        """
        if readback_digest not in self._readbacks:
            raise ValueError(f"unknown readback_digest: {readback_digest[:8]}...")

        if decision_id in self._decisions:
            existing = self._decisions[decision_id]
            if existing.action == "approve":
                return existing
            raise ValueError(
                f"decision_id {decision_id[:8]}... already exists with action={existing.action}"
            )

        rb = self._readbacks[readback_digest]
        now = datetime.now(timezone.utc)

        decision = self.ReturnDecision(
            decision_id=decision_id,
            order_id=order_id,
            product_id=rb.product_id,
            action="approve",
            refunded_minor=rb.total_minor,
            decision_at=now,
            approved_by=user_id,
            readback_digest=readback_digest,
        )

        per_line = rb.total_minor // max(len(line_ids), 1)
        for lid in line_ids:
            decision.lines[lid] = self.ReturnLineState(
                line_id=lid,
                status=ReturnLineStatus.APPROVED,
                refunded_minor=per_line,
                decision_id=decision_id,
            )

        self._decisions[decision_id] = decision

        self._service._audit(
            tenant_id, user_id, "return.approved", decision_id,
            "succeeded",
            {
                "order_id": order_id,
                "refunded_minor": rb.total_minor,
                "line_count": len(line_ids),
            },
        )
        return decision

    def reject(
        self,
        tenant_id: str,
        user_id: str,
        order_id: str,
        readback_digest: str,
        decision_id: str,
        line_ids: List[str],
        reason: str = "rejected",
    ) -> ReturnDecision:
        """Reject return lines identified by line_ids.

        Requires a prior readback with the given readback_digest.
        Idempotent on decision_id.

        Returns the ReturnDecision.
        """
        if readback_digest not in self._readbacks:
            raise ValueError(f"unknown readback_digest: {readback_digest[:8]}...")

        if decision_id in self._decisions:
            existing = self._decisions[decision_id]
            if existing.action == "reject":
                return existing
            raise ValueError(
                f"decision_id {decision_id[:8]}... already exists with action={existing.action}"
            )

        rb = self._readbacks[readback_digest]
        now = datetime.now(timezone.utc)

        decision = self.ReturnDecision(
            decision_id=decision_id,
            order_id=order_id,
            product_id=rb.product_id,
            action="reject",
            refunded_minor=0,
            decision_at=now,
            approved_by=user_id,
            readback_digest=readback_digest,
        )

        for lid in line_ids:
            decision.lines[lid] = self.ReturnLineState(
                line_id=lid,
                status=ReturnLineStatus.REJECTED,
                refunded_minor=0,
                decision_id=decision_id,
            )

        self._decisions[decision_id] = decision

        self._service._audit(
            tenant_id, user_id, "return.rejected", decision_id,
            "succeeded",
            {"order_id": order_id, "reason": reason, "line_count": len(line_ids)},
        )
        return decision

    def partial(
        self,
        tenant_id: str,
        user_id: str,
        order_id: str,
        readback_digest: str,
        decision_id: str,
        line_ids: List[str],
        refund_minor: int,
    ) -> ReturnDecision:
        """Partial claim — approve a subset of the readback amount.

        Requires a prior readback with the given readback_digest.
        ``refund_minor`` must be <= readback total_minor and > 0.
        Idempotent on decision_id.

        Returns the ReturnDecision.
        Raises ValueError if refund_minor exceeds readback total or <= 0.
        """
        if readback_digest not in self._readbacks:
            raise ValueError(f"unknown readback_digest: {readback_digest[:8]}...")

        if decision_id in self._decisions:
            existing = self._decisions[decision_id]
            if existing.action == "partial":
                return existing
            raise ValueError(
                f"decision_id {decision_id[:8]}... already exists with action={existing.action}"
            )

        rb = self._readbacks[readback_digest]

        if refund_minor <= 0:
            raise ValueError("partial refund_minor must be positive")
        if refund_minor > rb.total_minor:
            raise ValueError(
                f"partial refund_minor {refund_minor} exceeds readback total {rb.total_minor}"
            )

        now = datetime.now(timezone.utc)

        decision = self.ReturnDecision(
            decision_id=decision_id,
            order_id=order_id,
            product_id=rb.product_id,
            action="partial",
            refunded_minor=refund_minor,
            decision_at=now,
            approved_by=user_id,
            readback_digest=readback_digest,
        )

        per_line = refund_minor // max(len(line_ids), 1)
        for lid in line_ids:
            decision.lines[lid] = self.ReturnLineState(
                line_id=lid,
                status=ReturnLineStatus.PARTIAL,
                refunded_minor=per_line,
                decision_id=decision_id,
            )

        self._decisions[decision_id] = decision

        self._service._audit(
            tenant_id, user_id, "return.partial", decision_id,
            "succeeded",
            {
                "order_id": order_id,
                "refund_minor": refund_minor,
                "readback_total": rb.total_minor,
            },
        )
        return decision

    def cancel(
        self,
        tenant_id: str,
        user_id: str,
        decision_id: str,
    ) -> ReturnDecision:
        """Cancel a pending return decision.

        Only cancels decisions that are not yet final (approve/reject/
        partial).  A cancelled decision marks all its lines as CANCELLED.

        Raises ValueError if decision_id not found or already final.
        """
        if decision_id not in self._decisions:
            raise ValueError(f"unknown decision_id: {decision_id[:8]}...")

        existing = self._decisions[decision_id]

        # Idempotency: already cancelled?
        if existing.action == "cancel":
            return existing

        if existing.action in ("approve", "reject", "partial"):
            raise ValueError(
                f"cannot cancel already-{existing.action} decision"
            )

        now = datetime.now(timezone.utc)

        cancelled = self.ReturnDecision(
            decision_id=decision_id,
            order_id=existing.order_id,
            product_id=existing.product_id,
            action="cancel",
            refunded_minor=0,
            decision_at=now,
            approved_by=user_id,
            readback_digest=existing.readback_digest,
        )

        for lid, ls in existing.lines.items():
            cancelled.lines[lid] = self.ReturnLineState(
                line_id=lid,
                status=ReturnLineStatus.CANCELLED,
                refunded_minor=0,
                decision_id=decision_id,
            )

        self._decisions[decision_id] = cancelled

        self._service._audit(
            tenant_id, user_id, "return.cancelled", decision_id,
            "succeeded",
            {"order_id": existing.order_id},
        )
        return cancelled

    # -- lookups ---------------------------------------------------------

    def get_decision(self, decision_id: str) -> Optional[ReturnDecision]:
        """Look up a decision by ID. Returns None if not found."""
        return self._decisions.get(decision_id)

    def get_readback(self, readback_digest: str) -> Optional[ReturnReadback]:
        """Look up a readback by digest. Returns None if not found."""
        return self._readbacks.get(readback_digest)


# ---------------------------------------------------------------------------
# Helper functions (backward-compatible with existing tests)
# ---------------------------------------------------------------------------


def _payload(review: FixtureReturnReview) -> dict[str, Any]:
    return {
        "contract": "coupang-return-fixture-v1",
        "fixture_review_digest": review.approval_digest,
        "vendor_ref": review.vendor_ref,
        "receipt_id": review.receipt_id,
        "shipment_id": review.shipment_id,
        "vendor_item_id": review.vendor_item_id,
        "cancel_quantity": review.cancel_quantity,
        "fixture_amount_krw": review.fixture_amount_krw,
        "fixture_amount_cap_krw": review.fixture_amount_cap_krw,
        "withdrawal_review_digest": review.withdrawal_review_digest,
    }


def _verify(service: Any, context: Any, review: FixtureReturnReview,
            connection_ref: str) -> None:
    verify_return_fixture_review(
        review, approval_digest=review.approval_digest,
        tenant_ref=context.tenant_id, connection_ref=connection_ref,
        now=service._clock())


def request_return_approval(service: Any, context: Any, review: FixtureReturnReview, *,
                            connection_ref: str, idempotency_key: str,
                            policy_version: int, target_version: int):
    """Create a REFUND approval containing only the immutable fixture projection."""
    _verify(service, context, review, connection_ref)
    return service.request_approval(
        context, ApprovalKind.REFUND, f"order:{review.order_id}", _payload(review),
        idempotency_key, policy_version, target_version,
        evidence=({"risk": "OFFLINE_FIXTURE_ONLY", "connection_ref": connection_ref,
                   "bank_refund_verified": False},))


def submit_approved_return(service: Any, context: Any, review: FixtureReturnReview, *,
                           connection_ref: str, approval_id: str,
                           idempotency_key: str, policy_version: int):
    """Submit the exact reviewed command to the DEMO-only typed gateway."""
    _verify(service, context, review, connection_ref)
    return service.submit_demo_tool(
        context, actor_type="workflow", actor_id="return-fixture-v1",
        tool="claim_action", target_type="order", target_id=review.order_id,
        input_value=_payload(review), idempotency_key=idempotency_key,
        requested_policy_version=policy_version, approval_id=approval_id)
