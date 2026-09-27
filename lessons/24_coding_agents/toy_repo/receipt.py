"""小票格式化 —— harness 演示用的"待开发功能"。

features.json 里的每个功能对应这里的一个函数，验收测试在 tests/test_receipt.py。
"""

from __future__ import annotations

from pricing import Coupon, Item


def format_yuan(cents: int) -> str:
    """把"分"格式化成人民币字符串：2350 → "¥23.50"，5 → "¥0.05"，-120 → "-¥1.20"。"""
    raise NotImplementedError("F1：待实现")


def parse_coupon(text: str) -> Coupon:
    """解析满减券文案："满100减20" → Coupon(threshold=10000, off=2000)。

    金额是整数元，文案前后和数字两侧可以有空格；格式不对、或者减免额大于门槛时抛 ValueError。
    """
    raise NotImplementedError("F2：待实现")


def format_line(item: Item) -> str:
    """一行小票："商品名 ×数量 金额"，如 "咖啡豆 ×2 ¥98.00"（金额 = 单价 × 数量，用 format_yuan 格式化）。"""
    raise NotImplementedError("F3：待实现")
