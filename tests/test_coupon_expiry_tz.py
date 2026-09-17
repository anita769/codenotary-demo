"""PR #1 新增：时区修复的回归测试。"""
from datetime import datetime, timedelta, timezone

from src.billing.coupon import Coupon, redeem

SH = timezone(timedelta(hours=8))


def test_timezone_conversion_in_utc():
    # aware 上海时间与 UTC 存储统一换算，不再抛 TypeError
    coupon = Coupon("PROMO11")
    assert coupon.is_expired(datetime(2026, 11, 9, 20, 0, tzinfo=SH)) is False


def test_valid_on_nov_9():
    result = redeem(Coupon("PROMO11"),
                    now=datetime(2026, 11, 9, 23, 59, tzinfo=SH))
    assert result["ok"] is True


def test_invalid_on_nov_11():
    result = redeem(Coupon("PROMO11"),
                    now=datetime(2026, 11, 11, 0, 1, tzinfo=SH))
    assert result["ok"] is False
    assert result["reason"] == "expired"
