"""订单计价模块。

计价顺序：商品小计 → 会员折扣 → 满减券 → 运费。
所有金额都用"分"（整数）表示，避免 0.1 + 0.2 != 0.3 这类浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass

# 会员折扣（百分比）：普通会员不打折，银卡 95 折，金卡 9 折
MEMBER_DISCOUNT_PERCENT = {"regular": 0, "silver": 5, "gold": 10}

FREE_SHIPPING_THRESHOLD = 9900  # 满 99 元包邮（按折扣、用券之后的金额判断）
SHIPPING_FEE = 800  # 不满包邮门槛时收 8 元运费


@dataclass(frozen=True)
class Item:
    """购物车里的一行商品。"""

    sku: str
    name: str
    unit_price: int  # 单价，单位：分
    qty: int = 1


@dataclass(frozen=True)
class Coupon:
    """满减券："满 threshold 减 off"（单位：分）。"""

    threshold: int
    off: int


def validate_items(items: list[Item]) -> None:
    """检查购物车：数量必须是正整数，单价不能为负。不合法时抛 ValueError。"""
    for item in items:
        if item.qty <= 0:
            raise ValueError(f"{item.sku} 的数量必须大于 0，收到 {item.qty}")
        if item.unit_price < 0:
            raise ValueError(f"{item.sku} 的单价不能为负，收到 {item.unit_price}")


def subtotal(items: list[Item]) -> int:
    """商品小计 = Σ 单价 × 数量。"""
    validate_items(items)
    total = 0
    for item in items:
        total += item.unit_price
    return total


def apply_member_discount(amount: int, level: str) -> int:
    """按会员等级打折，结果四舍五入到分（0.5 分进位）。未知等级按普通会员处理（不打折）。"""
    percent = MEMBER_DISCOUNT_PERCENT.get(level, 0)
    discounted = amount * (100 - percent) / 100
    return int(discounted)


def apply_coupon(amount: int, coupon: Coupon | None) -> int:
    """使用满减券：金额达到门槛（大于等于 threshold）才能用；减完不低于 0。"""
    if coupon is None:
        return amount
    if amount > coupon.threshold:
        return max(0, amount - coupon.off)
    return amount


def shipping_fee(amount: int) -> int:
    """运费：满包邮门槛免运费，否则收固定运费。"""
    if amount >= FREE_SHIPPING_THRESHOLD:
        return 0
    return SHIPPING_FEE


def order_total(items: list[Item], level: str = "regular", coupon: Coupon | None = None) -> int:
    """订单应付总额（分）：小计 → 会员折扣 → 满减券 → 运费。"""
    amount = subtotal(items)
    amount = apply_member_discount(amount, level)
    amount = apply_coupon(amount, coupon)
    return amount + shipping_fee(amount)


def describe_order(items: list[Item], level: str = "regular", coupon: Coupon | None = None) -> dict:
    """返回订单明细，方便前端展示每一步的金额。"""
    amount = subtotal(items)
    after_discount = apply_member_discount(amount, level)
    after_coupon = apply_coupon(after_discount, coupon)
    fee = shipping_fee(after_coupon)
    return {
        "subtotal": amount,
        "after_discount": after_discount,
        "after_coupon": after_coupon,
        "shipping": fee,
        "total": after_coupon + fee,
    }
