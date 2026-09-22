from __future__ import annotations

from dataclasses import dataclass, field

from .config import ProfitPolicy
from .profit import UnitEconomics


@dataclass(frozen=True)
class CandidateProduct:
    supplier_sku: str
    name: str
    economics: UnitEconomics
    stock: int
    origin: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    image_license_confirmed: bool = False
    prohibited: bool = False


@dataclass(frozen=True)
class Decision:
    approved: bool
    reasons: list[str] = field(default_factory=list)


def evaluate(product: CandidateProduct, policy: ProfitPolicy) -> Decision:
    reasons: list[str] = []
    if product.prohibited:
        reasons.append("판매 금지/제한 상품")
    if product.stock <= 0:
        reasons.append("공급사 재고 없음")
    if not product.origin:
        reasons.append("원산지 누락")
    if not product.manufacturer:
        reasons.append("제조자 누락")
    if not product.image_license_confirmed:
        reasons.append("이미지 사용권 미확인")
    margin = product.economics.margin_rate(policy)
    if margin < policy.minimum_contribution_margin_rate:
        reasons.append(
            f"공헌이익률 {margin:.1%} < 하한 {policy.minimum_contribution_margin_rate:.1%}"
        )
    return Decision(approved=not reasons, reasons=reasons)

