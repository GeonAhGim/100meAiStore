"""Local DEMO inventory snapshots and deterministic projected price guard."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
import re

from .errors import ConflictError
from .domain import Capability, DemoInventorySnapshot, DemoPriceProjection
from uuid import uuid4

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


@dataclass(frozen=True)
class DemoInventoryObservation:
    sku: str
    supplier_id: str
    quantity: int
    observed_at: datetime


@dataclass(frozen=True)
class DemoPriceCalculation:
    selling_price_minor: int
    supply_cost_minor: int
    variable_cost_minor: int
    fee_rate: Decimal
    projected_contribution_minor: int
    projected_margin: Decimal
    status: str


def observe_demo_inventory(sku: str, supplier_id: str, quantity: int, observed_at: datetime) -> DemoInventoryObservation:
    if not isinstance(sku, str) or not _OPAQUE.fullmatch(sku) or not isinstance(supplier_id, str) or not _OPAQUE.fullmatch(supplier_id) or type(quantity) is not int or quantity < 0 or not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ConflictError("invalid DEMO inventory observation")
    return DemoInventoryObservation(sku, supplier_id, quantity, observed_at)


def calculate_demo_price(selling_price_minor: int, supply_cost_minor: int, variable_cost_minor: int = 0, fee_rate: Decimal | str = Decimal("0")) -> DemoPriceCalculation:
    if any(type(value) is not int or value < 0 for value in (selling_price_minor, supply_cost_minor, variable_cost_minor)) or type(fee_rate) not in (Decimal, str):
        raise ConflictError("invalid DEMO price inputs")
    try: rate = Decimal(fee_rate)
    except Exception as exc: raise ConflictError("invalid fee rate") from exc
    if not rate.is_finite() or rate < 0 or rate >= 1 or selling_price_minor <= 0: raise ConflictError("invalid fee rate or selling price")
    contribution = Decimal(selling_price_minor) - Decimal(supply_cost_minor) - Decimal(variable_cost_minor) - (Decimal(selling_price_minor) * rate)
    contribution_minor = int(contribution.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    margin = (contribution / Decimal(selling_price_minor)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return DemoPriceCalculation(selling_price_minor, supply_cost_minor, variable_cost_minor, rate, contribution_minor, margin, "READY" if margin >= Decimal("0.10") else "BLOCKED")


def record_demo_inventory(service: Any, context: Any, sku: str, supplier_id: str, quantity: int,
                          observed_at: datetime | None = None) -> DemoInventorySnapshot:
    service.require(context, Capability.TENANT_ADMIN)
    observed_at = observed_at or service._clock()
    value = observe_demo_inventory(sku, supplier_id, quantity, observed_at)
    with service.repo.transaction():
        result = service.repo.save_inventory_snapshot(DemoInventorySnapshot(str(uuid4()), context.tenant_id, value.sku, value.supplier_id, value.quantity, value.observed_at))
        service._audit(context.tenant_id, context.user_id, "inventory.observed_demo", result.id, "succeeded", {"sku": sku, "supplier_id": supplier_id})
        return result


def record_demo_price_projection(service: Any, context: Any, sku: str, selling_price_minor: int,
                                 supply_cost_minor: int, variable_cost_minor: int = 0,
                                 fee_rate: Decimal | str = Decimal("0")) -> DemoPriceProjection:
    service.require(context, Capability.TENANT_ADMIN)
    if not isinstance(sku, str) or not _OPAQUE.fullmatch(sku):
        raise ConflictError("invalid sku")
    result = calculate_demo_price(selling_price_minor, supply_cost_minor, variable_cost_minor, fee_rate)
    with service.repo.transaction():
        value = DemoPriceProjection(str(uuid4()), context.tenant_id, sku, result.selling_price_minor, result.supply_cost_minor, result.variable_cost_minor, str(result.fee_rate), result.projected_contribution_minor, str(result.projected_margin), result.status, service._clock())
        service.repo.save_price_projection(value)
        service._audit(context.tenant_id, context.user_id, "price.projected_demo", value.id, "succeeded" if result.status == "READY" else "blocked", {"sku": sku, "status": result.status})
        return value
