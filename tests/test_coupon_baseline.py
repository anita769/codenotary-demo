"""Baseline tests — cover redemption mechanics, never the expiry boundary."""
from datetime import datetime

from src.billing.coupon import Coupon, redeem
from src.billing.order import settle_order


def test_redeem_before_expiry_naive_clock():
    # 老测试环境：naive 时钟（与 expires_at 同侧，混比问题不暴露）
    coupon = Coupon("PROMO11")
    result = redeem(coupon, now=datetime(2026, 11, 9, 12, 0))
    assert result["ok"] is True
    assert result["code"] == "PROMO11"


def test_redeem_after_expiry_naive_clock():
    coupon = Coupon("PROMO11")
    result = redeem(coupon, now=datetime(2026, 11, 11, 12, 0))
    assert result["ok"] is False
    assert result["reason"] == "expired"


def test_settle_order_applies_discount():
    coupon = Coupon("PROMO11")
    out = settle_order("ORD-1001", coupon, 200.0,
                       now=datetime(2026, 11, 9, 12, 0))
    assert out["settled"] is True
    assert out["discounted"] == 180.0
