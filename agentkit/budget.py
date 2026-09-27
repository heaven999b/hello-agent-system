"""预算护栏：Agent 是"会自己花钱的程序"，必须给它设上限。

真实事故：一个 Agent 在两个工具之间来回调用了 3000 次，一晚上烧掉几千美元。
所以每次运行都要限制：步数（Agent.max_steps）、token 数、金额、工具调用次数、墙钟时间。
超限时优雅地停下（StopRun），而不是抛异常让用户看到 500。
"""

from __future__ import annotations

import time

from .hooks import Hook, StopRun


class BudgetHook(Hook):
    def __init__(
        self,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_tool_calls: int | None = None,
        max_seconds: float | None = None,
    ):
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.max_tool_calls = max_tool_calls
        self.max_seconds = max_seconds

    def before_llm(self, state, messages) -> None:
        # 只统计"实际执行"的时间：暂停等待人工审批的几个小时不算，否则审批一恢复就会被判超时
        elapsed = state.active_seconds + (time.time() - state.segment_started_at)
        if self.max_seconds is not None and elapsed > self.max_seconds:
            raise StopRun("budget_exceeded", f"已超过时长预算 {self.max_seconds}s，任务中止。")

    def after_llm(self, state, response) -> None:
        if self.max_tokens is not None and state.usage.total > self.max_tokens:
            raise StopRun("budget_exceeded", f"已超过 token 预算（{state.usage.total} > {self.max_tokens}），任务中止。")
        if self.max_cost_usd is not None and state.cost_usd > self.max_cost_usd:
            raise StopRun("budget_exceeded", f"已超过金额预算（${state.cost_usd:.4f} > ${self.max_cost_usd}），任务中止。")

    def before_tool(self, state, call, tool) -> str | None:
        if self.max_tool_calls is not None and state.tool_calls_count >= self.max_tool_calls:
            raise StopRun("budget_exceeded", f"工具调用次数已达上限 {self.max_tool_calls}，任务中止。")
        return None
