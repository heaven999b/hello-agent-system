"""成本估算。

⚠️ 下面的价格是**示例占位值**（美元 / 百万 token），请按你实际使用的模型和合同价修改。
企业里成本要能按 租户 / 用户 / 功能 / 模型 维度归因，这是 FinOps 和定价的基础。
"""

from __future__ import annotations

from .types import Usage

# model -> (输入价格, 输出价格[, 缓存命中的输入价格])，单位：美元 / 1M tokens
# 第三项可选：命中提示词缓存的输入通常有折扣（折扣幅度因厂商和模型而异，请查官方价格页后填写）；
# 不填则按普通输入价计算（偏保守）。
PRICES: dict[str, tuple[float, ...]] = {
    "default": (1.25, 10.0),
    "scripted": (1.0, 4.0),
}


def estimate_cost(usage: Usage, model: str) -> float:
    price = PRICES.get(model, PRICES["default"])
    price_in, price_out = price[0], price[1]
    price_cached = price[2] if len(price) > 2 else price_in
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    return ((usage.input_tokens - cached) * price_in + cached * price_cached + usage.output_tokens * price_out) / 1e6
