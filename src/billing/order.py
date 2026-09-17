"""Order settlement — downstream consumer of coupon redemption."""
from __future__ import annotations

from datetime import datetime

from .coupon import Coupon, redeem


def settle_order(order_id: str, coupon: Coupon, amount: float,
                 now: datetime | None = None) -> dict:
    """订单结算：核销成功才抵扣。"""
    result = redeem(coupon, now)
    if not result["ok"]:
        return {"order": order_id, "settled": False,
                "reason": f"coupon {result['reason']}"}
    discounted = round(amount * 0.9, 2)
    return {"order": order_id, "settled": True,
            "discounted": discounted, "coupon": coupon.code}
