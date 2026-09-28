"""HR 助手：API 进程（同步接口那条反面教材）和 worker 进程共用的 Agent 定义。

工具通过 ctx 拿到网关注入的 tenant_id / user_id —— 模型没有机会"声明自己是谁"（第 03、09 课）。
离线时用 HRScriptedLLM：按问题内容决定调用哪个工具、每次"调模型"等多久（asyncio.sleep，不阻塞事件循环）。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import Field

from agentkit import Agent, ToolContext, call_tool, default_llm, reply, tool
from agentkit.types import LLMResponse

LEAVE_BALANCE = {("acme", "u-alice"): 7, ("globex", "u-bob"): 12, ("hooli", "u-carl"): 3}

HR_PROMPT = "你是企业 HR 助手。制度问题先用 search_policy 检索；个人数据用工具查询，不要编造。回答不超过 3 句话。"


@tool
def get_leave_balance(ctx: ToolContext) -> str:
    """查询当前员工的剩余年假天数。"""
    days = LEAVE_BALANCE.get((ctx.tenant_id, ctx.user_id))
    return f"剩余年假 {days} 天" if days is not None else "没有找到该员工的假期记录"


@tool
def search_policy(query: Annotated[str, Field(description="检索关键词")]) -> str:
    """检索本公司的 HR 制度。"""
    return "【年假】入职满 1 年 5 天，满 10 年 10 天，满 20 年 15 天；可分次使用，当年未休可顺延至次年 3 月底。"


HR_TOOLS = [get_leave_balance, search_policy]


def is_long_task(text: str) -> bool:
    return "报告" in text or "汇总" in text


class HRScriptedLLM:
    """离线剧本。三类请求：
    - "剩几天"：查余额 → 回答（2 次模型调用）；
    - "报告 / 汇总"（长任务）：检索制度 → 查余额 → 写报告（3 次模型调用，每次 long_latency 秒）；
    - 其他制度问题：检索 → 回答（2 次模型调用）。
    model 是网关路由出来的逻辑模型名（lite / mini / pro ……），写进每次响应，成本按它归因。
    """

    def __init__(self, model: str, latency: float = 0.25, long_latency: float = 1.5):
        self.model, self.latency, self.long_latency = model, latency, long_latency

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        question = next(m["content"] for m in messages if m["role"] == "user")
        tool_results = [m["content"] for m in messages if m["role"] == "tool"]
        long = is_long_task(question)
        await asyncio.sleep(self.long_latency if long else self.latency)
        if long:
            if len(tool_results) == 0:
                r = call_tool("search_policy", query="年假", input_tokens=900, output_tokens=30)
            elif len(tool_results) == 1:
                r = call_tool("get_leave_balance", input_tokens=1400, output_tokens=20)
            else:
                r = reply(f"部门请假汇总报告：制度要点已核对；本人{tool_results[-1]}；建议按季度复盘。", 2200, 400)
        elif "剩几天" in question:
            r = call_tool("get_leave_balance", input_tokens=380, output_tokens=15) if not tool_results else \
                reply(f"你{tool_results[-1]}。", 420, 12)
        else:
            r = call_tool("search_policy", query="年假", input_tokens=380, output_tokens=18) if not tool_results else \
                reply("入职满 1 年 5 天，满 10 年 10 天，满 20 年 15 天。", 470, 30)
        r.model = self.model
        return r


def make_llm(model: str, offline: bool, latency: float = 0.25, long_latency: float = 1.5):
    """离线：剧本模型；在线：本机只配置了一个真实模型，所有逻辑模型都映射到它（生产中由模型网关映射到不同的真实模型）。"""
    return HRScriptedLLM(model, latency, long_latency) if offline else default_llm()


def make_agent(llm, **kwargs) -> Agent:
    return Agent(llm, HR_TOOLS, system_prompt=HR_PROMPT, name="hr-assistant", max_steps=6, **kwargs)
