"""第 12 课练习：生产架构里的两个"守门员"。

    (a) TokenBucket / TenantRateLimiter   多租户限流：谁都不能把系统打爆，也不能挤占别人
    (b) choose_model                      模型路由：按任务需要选"够用且最便宜"的模型

要写的地方都标了 TODO（raise NotImplementedError），其余代码已经写好，读懂即可：
    TokenBucket._refill / try_acquire / retry_after
    TenantRateLimiter._bucket
    choose_model

验证：make lesson N=12   （或 .venv/bin/python -m pytest lessons/12_production_architecture）
"""

from __future__ import annotations

import math  # noqa: F401  提示：math.inf 表示"永远等不到"
import time
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------- (a) 令牌桶


class TokenBucket:
    """令牌桶限流器。

    想象一个桶：最多装 capacity 个令牌，每秒匀速滴入 refill_rate 个，满了就溢出。
    每个请求要拿走 tokens 个令牌才能通过；桶里不够就拒绝（HTTP 429）。

    - capacity 决定**突发**：空闲一阵之后，最多能连续放行多少请求；
    - refill_rate 决定**长期平均速率**：持续压测时，每秒最多放行多少。

    实现方式是"惰性补充"：不开后台线程定时加令牌，而是每次访问时根据距上次更新
    过了多久，一次性算出应该补多少。O(1)、没有定时器，也方便放进 Redis 用原子脚本实现。

    clock 可注入：生产用 time.monotonic（不受系统改时间影响），测试用假时钟，结果完全确定。
    """

    def __init__(self, capacity: float, refill_rate: float, clock: Callable[[], float] = time.monotonic):
        if capacity <= 0:
            raise ValueError(f"capacity 必须 > 0，收到 {capacity}")
        if refill_rate < 0:
            raise ValueError(f"refill_rate 不能为负，收到 {refill_rate}")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.clock = clock
        self.tokens = float(capacity)  # 初始是满的：新租户一上来就能用满突发额度
        self.updated_at = clock()

    def _refill(self) -> None:
        """按流逝的时间补充令牌，最多补到 capacity。

        边界：时钟如果没走或倒退（now <= updated_at），什么都不做：既不补充，也**不要**把 updated_at
        往回拨 —— 否则时钟走回来时，倒退的那段时间会被重复计算，凭空多出令牌。

        提示：now = self.clock()；如果 now <= self.updated_at 直接 return；
        否则 新令牌数 = min(capacity, 旧令牌数 + (now - updated_at) × refill_rate)，再把 updated_at 更新为 now。
        """
        raise NotImplementedError("TODO: 按流逝时间补充令牌（封顶 capacity）")

    @property
    def available(self) -> float:
        """当前可用的令牌数（会先触发一次补充）。"""
        self._refill()
        return self.tokens

    def try_acquire(self, tokens: float = 1) -> bool:
        """尝试拿走 tokens 个令牌：够就扣掉并返回 True；不够返回 False，且**不扣任何令牌**。

        tokens 可以大于 1：按"成本"限流时，一个请求按预计消耗的 LLM token 数来扣。
        边界：tokens <= 0 → ValueError；tokens > capacity → 永远不可能满足，直接 False。

        提示：先 _refill()，再判断够不够。
        """
        raise NotImplementedError("TODO: 先补充，再判断并扣减令牌")

    def retry_after(self, tokens: float = 1) -> float:
        """还要等多少秒才能拿到 tokens 个令牌（用来填 HTTP 429 响应的 Retry-After 头）。

        - 现在就够 → 0.0
        - tokens > capacity，或 refill_rate == 0 且现在不够 → math.inf（永远等不到）
        - 否则 → (tokens - 当前令牌数) / refill_rate
        边界：tokens <= 0 → ValueError。这个方法只查询，不扣令牌。
        """
        raise NotImplementedError("TODO: 计算还要等多少秒")


@dataclass(frozen=True)
class Plan:
    """套餐：决定一个租户的限流参数。"""

    name: str
    capacity: float  # 突发上限（桶容量）
    refill_rate: float  # 每秒补充的令牌数（长期平均速率）


class TenantRateLimiter:
    """多租户限流：每个租户一个**独立**的令牌桶，桶的参数由租户的套餐决定。

    为什么不用一个全局桶？一个租户疯狂刷接口（"吵闹的邻居"，noisy neighbor），会把全局桶
    抽干，所有租户一起被 429。每租户一个桶，谁刷爆了谁自己被限，别人不受影响。
    （生产中通常还会在外面再套一个全局桶，保护上游模型的总配额。）

    - plans：套餐名 → Plan
    - tenant_plans：租户 → 套餐名；没登记的租户使用 default_plan
    - 桶**按需创建**（某个租户第一次请求时），所有桶共用同一个 clock
    """

    def __init__(
        self,
        plans: dict[str, Plan],
        tenant_plans: dict[str, str],
        default_plan: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        if default_plan not in plans:
            raise ValueError(f"默认套餐 {default_plan!r} 不在 plans 里")
        unknown = {p for p in tenant_plans.values() if p not in plans}
        if unknown:
            raise ValueError(f"tenant_plans 引用了不存在的套餐：{sorted(unknown)}")
        self.plans = dict(plans)
        self.tenant_plans = dict(tenant_plans)
        self.default_plan = default_plan
        self.clock = clock
        self._buckets: dict[str, TokenBucket] = {}

    def plan_of(self, tenant_id: str) -> Plan:
        """租户对应的套餐；没登记的租户用默认套餐。"""
        return self.plans[self.tenant_plans.get(tenant_id, self.default_plan)]

    def _bucket(self, tenant_id: str) -> TokenBucket:
        """取出租户的桶；第一次访问时按套餐创建（共用 self.clock）。

        提示：self._buckets 是缓存。已有就直接返回；没有就用 plan_of(tenant_id) 的参数新建一个，
        存进缓存再返回。一定要把 self.clock 传给 TokenBucket，否则测试里的假时钟不起作用。
        """
        raise NotImplementedError("TODO: 按需创建并缓存每个租户的令牌桶")

    def try_acquire(self, tenant_id: str, tokens: float = 1) -> bool:
        """该租户的桶里够 tokens 个令牌就放行。"""
        return self._bucket(tenant_id).try_acquire(tokens)

    def retry_after(self, tenant_id: str, tokens: float = 1) -> float:
        return self._bucket(tenant_id).retry_after(tokens)


# ---------------------------------------------------------------- (b) 模型路由


@dataclass(frozen=True)
class ModelSpec:
    """模型网关里登记的一个（逻辑）模型。价格单位：美元 / 百万 token。"""

    name: str
    context_window: int  # 最大上下文 token 数（输入 + 输出）
    supports_tools: bool  # 是否支持工具调用（function calling）
    strong: bool  # 是否属于"强模型"档：复杂推理、长链路规划
    input_price: float
    output_price: float

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens / 1e6 * self.input_price + output_tokens / 1e6 * self.output_price


class NoModelAvailable(Exception):
    """没有任何模型能满足这个任务的硬性要求。调用方应该拒绝请求或让用户缩小任务，而不是随便挑一个。"""


COMPLEXITIES = ("low", "medium", "high")


def choose_model(task: dict, models: list[ModelSpec]) -> str:
    """按规则为任务挑选模型，返回模型名。

    task 字段：
        input_tokens: int                          必填，预计输入 token 数（≥ 0）
        output_tokens: int = 0                     预计输出 token 数（≥ 0）
        needs_tools: bool = False                  是否需要工具调用
        complexity: "low" | "medium" | "high" = "medium"

    规则（按顺序执行，先硬约束、再质量要求、最后才优化成本）：
        1. 校验：input_tokens 缺失或为负、output_tokens 为负、complexity 不在三个取值里 → ValueError
        2. 能力：needs_tools 为 True → 只保留 supports_tools 的模型
        3. 容量：input_tokens + output_tokens > context_window 的模型排除（输出也占上下文！）
        4. 质量：complexity == "high" → 只保留 strong 的模型
        5. 成本：剩下的候选里，选 estimated_cost(input_tokens, output_tokens) 最低的；
           成本相同时选 models 列表里靠前的（列表顺序 = 偏好顺序）
        6. 任何一步之后候选为空 → 抛 NoModelAvailable，错误信息里说明是哪条规则把候选排除光了：
           第 2 步写"工具"，第 3 步写"上下文"，第 4 步写"强模型"。models 本身为空也抛 NoModelAvailable。

    为什么高复杂度找不到强模型时要报错，而不是退而求其次用弱模型？因为"悄悄降级"会让质量问题
    无声无息地发生。要降级也应该是调用方显式决定（并记录到 trace 里），而不是路由器擅自做主。

    提示：用一个 candidates 列表，每条规则过滤一次、检查一次是否为空。
    min(candidates, key=...) 在 key 相同时返回最先出现的元素，正好满足"成本相同选靠前的"。
    """
    raise NotImplementedError("TODO: 实现规则路由：能力 → 容量 → 质量 → 成本")
