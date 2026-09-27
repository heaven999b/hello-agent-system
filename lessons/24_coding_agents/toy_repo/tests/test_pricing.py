"""订单计价的验收测试。这些测试就是需求：请修改 pricing.py，而不是修改这里。"""

import pytest

from pricing import Coupon, Item, apply_coupon, apply_member_discount, order_total, shipping_fee, subtotal

COFFEE = Item("C01", "咖啡豆", unit_price=4900, qty=2)
MUG = Item("M01", "马克杯", unit_price=3500, qty=1)


def test_subtotal_multiplies_quantity():
    assert subtotal([COFFEE, MUG]) == 13300  # 49.00 × 2 + 35.00 × 1


def test_subtotal_empty_cart():
    assert subtotal([]) == 0


def test_subtotal_rejects_zero_quantity():
    with pytest.raises(ValueError):
        subtotal([Item("C01", "咖啡豆", unit_price=4900, qty=0)])


def test_member_discount_rounds_to_nearest_cent():
    assert apply_member_discount(1111, "gold") == 1000  # 11.11 × 0.9 = 9.999 → 10.00


def test_member_discount_half_cent_rounds_up():
    assert apply_member_discount(1990, "silver") == 1891  # 19.90 × 0.95 = 18.905 → 18.91（0.5 分进位）


def test_unknown_member_level_gets_no_discount():
    assert apply_member_discount(5000, "diamond") == 5000


def test_coupon_applies_exactly_at_threshold():
    assert apply_coupon(10000, Coupon(threshold=10000, off=2000)) == 8000  # 正好满 100 元，可以用券


def test_coupon_not_applied_below_threshold():
    assert apply_coupon(9999, Coupon(threshold=10000, off=2000)) == 9999


def test_coupon_never_makes_amount_negative():
    assert apply_coupon(500, Coupon(threshold=0, off=800)) == 0


def test_free_shipping_threshold():
    assert shipping_fee(9900) == 0
    assert shipping_fee(9899) == 800


def test_order_total_end_to_end():
    # 小计 133.00 → 金卡 9 折 119.70 → 满 100 减 20 → 99.70 → 满 99 包邮
    assert order_total([COFFEE, MUG], level="gold", coupon=Coupon(threshold=10000, off=2000)) == 9970
