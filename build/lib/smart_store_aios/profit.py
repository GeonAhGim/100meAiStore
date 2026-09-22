from __future__ import annotations

from dataclasses import dataclass

from .config import ProfitPolicy


@dataclass(frozen=True)
class UnitEconomics:
    selling_price: int
    supply_cost: int
    outbound_shipping: int = 0
    other_variable_cost: int = 0

    def contribution(self, policy: ProfitPolicy) -> int:
        rate_cost = self.selling_price * (
            policy.marketplace_fee_rate + policy.ad_cost_rate + policy.return_reserve_rate
        )
        return round(
            self.selling_price
            - self.supply_cost
            - self.outbound_shipping
            - self.other_variable_cost
            - rate_cost
        )

    def margin_rate(self, policy: ProfitPolicy) -> float:
        if self.selling_price <= 0:
            return -1.0
        return self.contribution(policy) / self.selling_price


def weekly_orders_required(policy: ProfitPolicy, contribution_per_order: int) -> int | None:
    if contribution_per_order <= 0:
        return None
    return (policy.weekly_target_krw + contribution_per_order - 1) // contribution_per_order

