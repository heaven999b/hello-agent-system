"""小票功能的验收测试：每个测试函数对应 features.json 里的一个功能。"""

import pytest

from pricing import Coupon, Item
from receipt import format_line, format_yuan, parse_coupon


def test_format_yuan():  # F1
    assert format_yuan(2350) == "¥23.50"
    assert format_yuan(5) == "¥0.05"
    assert format_yuan(0) == "¥0.00"
    assert format_yuan(-120) == "-¥1.20"


def test_parse_coupon():  # F2
    assert parse_coupon("满100减20") == Coupon(threshold=10000, off=2000)
    assert parse_coupon(" 满 300 减 50 ") == Coupon(threshold=30000, off=5000)
    with pytest.raises(ValueError):
        parse_coupon("打八折")
    with pytest.raises(ValueError):
        parse_coupon("满20减100")  # 减免额大于门槛


def test_format_line():  # F3
    assert format_line(Item("C01", "咖啡豆", unit_price=4900, qty=2)) == "咖啡豆 ×2 ¥98.00"
    assert format_line(Item("M01", "马克杯", unit_price=3500)) == "马克杯 ×1 ¥35.00"
