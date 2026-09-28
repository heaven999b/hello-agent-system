"""钩子（Hook）：横切关注点的插拔点。

安全、权限、预算、审计、脱敏……这些能力和"Agent 怎么思考"无关，却要作用于每一步。
如果全写进 Agent 主循环，循环会变成一团乱麻。企业做法是"中间件 / 拦截器"模式：
主循环只在关键节点调用钩子，每种能力是一个独立的 Hook，按需组合、单独测试。

主循环中的调用时机：

    on_run_start ──► [ before_llm ─► LLM ─► after_llm ─► (before_tool ─► 工具 ─► after_tool)* ]* ──► on_final ──► on_run_end

钩子可以：
- 什么都不做（观察）：返回 None
- 改写数据：on_run_start / after_tool / on_final 返回新值
- 拒绝一次工具调用：before_tool 返回字符串（拒绝原因会作为观察反馈给模型）
- 中止整个运行：抛 StopRun
- 暂停等人工：抛 PauseRun（状态会被保存，之后 await agent.resume() 继续）

每个方法都可以写成普通方法，也可以写成 `async def`（比如要查 Redis / 数据库的限流钩子）：
Agent 会自动 await。纯计算的钩子（权限表、预算、脱敏）写成普通方法就好。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import RunState
    from .tools import Tool, ToolResult
    from .types import LLMResponse, Message, ToolCall


class StopRun(Exception):
    """中止运行，如预算耗尽、输入被安全策略拦截。"""

    trace_as_error = False

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


class PauseRun(Exception):
    """暂停运行，等待外部输入（最常见：人工审批高风险操作）。"""

    trace_as_error = False

    def __init__(self, call: "ToolCall", message: str = ""):
        super().__init__(message)
        self.call = call
        self.message = message


class Hook:
    def on_run_start(self, state: "RunState", user_input: str) -> str | None:
        return None

    def visible_tools(self, state: "RunState", names: list[str]) -> list[str]:
        """决定本次运行向模型"展示"哪些工具（比如按角色过滤）。"""
        return names

    def before_llm(self, state: "RunState", messages: list["Message"]) -> None:
        return None

    def after_llm(self, state: "RunState", response: "LLMResponse") -> None:
        return None

    def before_tool(self, state: "RunState", call: "ToolCall", tool: "Tool | None") -> str | None:
        return None

    def after_tool(self, state: "RunState", call: "ToolCall", result: "ToolResult") -> "ToolResult | None":
        return None

    def on_final(self, state: "RunState", output: str) -> str | None:
        return None

    def on_run_end(self, state: "RunState") -> None:
        return None
