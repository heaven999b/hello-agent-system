"""第 27 课 demo 与集成测试共用的场景：电商客服 Agent 的工具、离线"模型"、用于重放演示的 workflow 变体。

为什么离线模型用 responder（按对话内容决定回复）而不是按顺序出牌的剧本？
Temporal 会把 activity 派给任何一个 worker，worker 也可能中途被杀、换一个新的接着跑。
按顺序出牌的剧本在新 worker 上会从头开始出牌；按"已经有几条工具结果"来决定下一步，换谁来演都一样。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Annotated

from pydantic import Field

from agentkit import ToolContext, call_tool, reply, tool
from agentkit.types import Message

# ---------------------------------------------------------------------------------------------
# 工具（进程内的"下游系统"状态：每个 worker 进程各有一份，演示时足够说明问题）
# ---------------------------------------------------------------------------------------------

ORDERS = {
    "A1001": {"order_id": "A1001", "status": "已签收", "amount": 99.0, "item": "蓝牙耳机"},
    "A1002": {"order_id": "A1002", "status": "运输中", "amount": 20.0, "item": "手机壳"},
}
_lock = threading.Lock()
CALLS: dict[str, int] = {}  # 工具名 → 本进程里被真正执行的次数
LEDGER: dict[str, dict] = {}  # 退款流水：幂等键 → 记录（同一个键只记一次）
REPORT_SECONDS = 3.0  # generate_report 的耗时：足够在它执行到一半时杀掉 worker


def _count(name: str) -> int:
    with _lock:
        CALLS[name] = CALLS.get(name, 0) + 1
        return CALLS[name]


@tool
def lookup_order(order_id: Annotated[str, Field(description="订单号，如 A1001")]) -> str:
    """查询订单的状态、金额和商品"""
    _count("lookup_order")
    order = ORDERS.get(order_id)
    return json.dumps(order, ensure_ascii=False) if order else f"订单 {order_id} 不存在"


@tool
def check_inventory(sku: Annotated[str, Field(description="商品 SKU，如 SKU-42")]) -> str:
    """查询某个 SKU 的库存。库存服务不太稳定：每个进程里第一次调用会连接失败"""
    if _count("check_inventory") == 1:
        raise ConnectionError("inventory-service: connection reset by peer")
    return json.dumps({"sku": sku, "available": 7, "warehouse": "上海仓"}, ensure_ascii=False)


@tool(risk="dangerous")
def refund(
    order_id: Annotated[str, Field(description="订单号")],
    amount: Annotated[float, Field(gt=0, description="退款金额（元）")],
    ctx: ToolContext,
) -> str:
    """给订单发起退款（高风险操作，需要人工审批）"""
    _count("refund")
    with _lock:
        # 幂等：同一个 workflow 里的同一次工具调用，无论重试几次，只记一笔
        LEDGER.setdefault(ctx.idempotency_key, {"order_id": order_id, "amount": amount, "by_user": ctx.user_id})
    return f"已为订单 {order_id} 退款 {amount} 元（流水号 {ctx.idempotency_key}）"


@tool(timeout_s=30)
async def generate_report(order_id: Annotated[str, Field(description="订单号")]) -> str:
    """生成订单的对账报表（比较慢，大约需要几秒）"""
    _count("generate_report")
    await asyncio.sleep(REPORT_SECONDS)  # async 工具：activity 被取消时这里会收到 CancelledError
    return f"订单 {order_id} 对账报表：金额一致，无异常"


TOOLS = [lookup_order, check_inventory, refund, generate_report]

SYSTEM_PROMPT = (
    "你是电商客服助手。先用工具查清事实再回答；退款必须先用 lookup_order 核对订单金额，再调用 refund；"
    "需要对账报表时调用 generate_report。工具返回错误时如实告诉用户，不要编造。"
)

# ---------------------------------------------------------------------------------------------
# 离线"模型"：按用户问题和已有的工具结果决定下一步
# ---------------------------------------------------------------------------------------------


def _plan(user_text: str) -> list[tuple[str, dict]]:
    order = "A1002" if "A1002" in user_text else "A1001"
    if "库存" in user_text:
        return [("check_inventory", {"sku": "SKU-42"})]
    if "退款" in user_text:
        amount = 20.0 if order == "A1002" else 99.0
        return [("lookup_order", {"order_id": order}), ("refund", {"order_id": order, "amount": amount})]
    if "报表" in user_text:
        return [("lookup_order", {"order_id": order}), ("generate_report", {"order_id": order})]
    return [("lookup_order", {"order_id": order})]


def offline_brain(messages: list[Message]):
    user_text = next((m["content"] for m in messages if m["role"] == "user"), "")
    plan = _plan(user_text)
    results = [m["content"] for m in messages if m["role"] == "tool"]
    if len(results) < len(plan):
        name, args = plan[len(results)]
        return call_tool(name, **args)
    return reply("处理结果：" + "；".join(r[:80] for r in results))


def offline_llm_factory(latency: float = 0.0):
    """每个 worker 一个 agentkit.ScriptedLLM（responder 模式；latency 用 asyncio.sleep 模拟模型耗时，可被取消）。"""

    def factory():
        from agentkit import ScriptedLLM

        return ScriptedLLM(responder=offline_brain, latency=latency)

    return factory


def real_llm_factory():
    """真实模型：agentkit 的 OpenAICompatLLM（async，httpx 连接池）。max_connections=2 就是这个 worker 对网关的最大并发。

    不在外面套 ResilientLLM：重试已经交给 Temporal 的 RetryPolicy，两层都重试次数会相乘（3 × 5 = 15 次），
    而且内层的重试不进事件历史，外面看不见。
    """
    from agentkit import default_llm

    return default_llm(max_connections=2)


# ---------------------------------------------------------------------------------------------
# 重放演示用的 workflow 变体（需要 temporalio；没装时这一段直接跳过）
# ---------------------------------------------------------------------------------------------

try:
    from temporalio import workflow
except ImportError:  # pragma: no cover - 取决于环境
    workflow = None

if workflow is not None:
    from agentkit.contrib.temporal import AgentInput, AgentResult, AgentWorkflow

    @workflow.defn(name="AgentWorkflow", sandboxed=False)
    class ChangedAgentWorkflow(AgentWorkflow):
        """"新版本"：开始前先等 1 秒（比如为了限流）。直接改代码：旧执行重放时命令序列对不上。

        注意 run 的参数注解必须写成 AgentInput（与父类 @workflow.init 字面相同）。
        """

        @workflow.run
        async def run(self, inp: AgentInput) -> AgentResult:
            await workflow.sleep(1)
            return await super().run(inp)

    @workflow.defn(name="AgentWorkflow", sandboxed=False)
    class PatchedAgentWorkflow(AgentWorkflow):
        """同样的改动，用 workflow.patched 版本化：旧执行（历史里没有这个补丁标记）走旧路径。"""

        @workflow.run
        async def run(self, inp: AgentInput) -> AgentResult:
            if workflow.patched("throttle-before-start"):
                await workflow.sleep(1)
            return await super().run(inp)
