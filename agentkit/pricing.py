"""成本估算。

⚠️ 下面的价格是**示例占位值**（美元 / 百万 token），请按你实际使用的模型和合同价修改。
企业里成本要能按 租户 / 用户 / 功能 / 模型 维度归因，这是 FinOps 和定价的基础。
"""

from __future__ import annotations

from .types import Usage

# model -> (输入价格, 输出价格)，单位：美元 / 1M tokens
PRICES: dict[str, tuple[float, float]] = {
    "default": (1.25, 10.0),
    "scripted": (1.0, 4.0),
}


def estimate_cost(usage: Usage, model: str) -> float:
    price_in, price_out = PRICES.get(model, PRICES["default"])
    return usage.input_tokens / 1e6 * price_in + usage.output_tokens / 1e6 * price_out
