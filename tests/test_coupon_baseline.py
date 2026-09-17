"""Baseline tests — cover redemption mechanics, never the expiry boundary."""
import unittest
from datetime import datetime

from src.billing.coupon import Coupon, redeem
from src.billing.order import settle_order


class TestCouponBaseline(unittest.TestCase):
    def test_redeem_before_expiry_naive_clock(self):
        # 老测试环境：naive 时钟（与 expires_at 同侧，混比问题不暴露）
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=datetime(2026, 11, 9, 12, 0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "PROMO11")

    def test_redeem_after_expiry_naive_clock(self):
        coupon = Coupon("PROMO11")
        result = redeem(coupon, now=datetime(2026, 11, 11, 12, 0))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")

    def test_settle_order_applies_discount(self):
        coupon = Coupon("PROMO11")
        out = settle_order("ORD-1001", coupon, 200.0,
                           now=datetime(2026, 11, 9, 12, 0))
        self.assertTrue(out["settled"])
        self.assertEqual(out["discounted"], 180.0)


if __name__ == "__main__":
    unittest.main()
