"""Refund compensation — downstream consumer of coupon validity."""
from __future__ import annotations

from datetime import datetime, timezone

from .coupon import Coupon


def compensation_credit(coupon: Coupon, now: datetime | None = None) -> float:
    """退款补偿：券仍有效则补偿额度翻倍。"""
    now = now or datetime.now(timezone.utc)
    try:
        expired = coupon.is_expired(now)
    except TypeError:
        expired = True  # 保守处理
    compensation = 0.0 if expired else 50.0
    return compensation
