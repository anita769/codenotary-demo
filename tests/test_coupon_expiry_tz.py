"""PR #1 新增：时区修复的回归测试。"""
import unittest
from datetime import datetime, timedelta, timezone

from src.billing.coupon import Coupon, redeem

SH = timezone(timedelta(hours=8))


class TestCouponExpiryTimezone(unittest.TestCase):
    def test_timezone_conversion_in_utc(self):
        # aware 上海时间与 UTC 存储统一换算，不再抛 TypeError
        coupon = Coupon("PROMO11")
        self.assertFalse(
            coupon.is_expired(datetime(2026, 11, 9, 20, 0, tzinfo=SH)))

    def test_valid_on_nov_9(self):
        result = redeem(Coupon("PROMO11"),
                        now=datetime(2026, 11, 9, 23, 59, tzinfo=SH))
        self.assertTrue(result["ok"])

    def test_invalid_on_nov_11(self):
        result = redeem(Coupon("PROMO11"),
                        now=datetime(2026, 11, 11, 0, 1, tzinfo=SH))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "expired")


if __name__ == "__main__":
    unittest.main()
