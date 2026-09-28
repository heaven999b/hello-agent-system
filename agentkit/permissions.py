"""权限与人工审批：安全的真正底线。

原则：**假设模型一定会被骗**，然后问"它被骗后最多能造成多大伤害？"
答案取决于它手里有什么工具、这些工具能对谁做什么。所以：

1. 最小权限（RBAC）：按角色只暴露必要的工具 —— 既不给看（visible_tools），也不让调（before_tool），双重保险；
2. 风险分级：read 直接放行；write 视情况；dangerous（转账、删库、重置密码、群发邮件）必须人工审批；
3. 审批要能异步：审批人可能一小时后才看到。所以不是阻塞等待，而是 PauseRun →
   状态落盘 → 审批系统通知审批人 → 审批后 agent.resume() 从断点继续。
"""

from __future__ import annotations

import inspect
from typing import Callable

from .hooks import Hook, PauseRun
from .state import RunState
from .types import ToolCall


class PermissionPolicy(Hook):
    def __init__(
        self,
        role_tools: dict[str, set[str]] | None = None,
        ask_risks: set[str] = frozenset({"dangerous"}),
        deny_tools: set[str] = frozenset(),
        approver: Callable[[ToolCall, RunState], bool] | None = None,
    ):
        """
        role_tools: 角色 → 可用工具名集合；集合里放 "*" 表示全部。None 表示不启用 RBAC。
        ask_risks:  哪些风险等级需要人工审批。
        deny_tools: 全局禁用的工具（紧急开关，比如某个工具发现了漏洞）。
        approver:   即时审批函数 approver(call, state) -> bool（命令行里问一句、或调审批服务），
                    可以是普通函数也可以是 async 函数。为 None 时抛 PauseRun：状态落盘，等审批人稍后
                    agent.approve() —— 审批人可能一小时后才处理，这时不能让一个协程一直挂着等。
        """
        self.role_tools = role_tools
        self.ask_risks = set(ask_risks)
        self.deny_tools = set(deny_tools)
        self.approver = approver

    def allowed(self, state: RunState, tool_name: str) -> bool:
        if tool_name in self.deny_tools:
            return False
        if self.role_tools is None:
            return True
        allowed: set[str] = set()
        for role in state.metadata.get("roles", []):
            allowed |= set(self.role_tools.get(role, set()))
        return "*" in allowed or tool_name in allowed

    def visible_tools(self, state, names: list[str]) -> list[str]:
        return [n for n in names if self.allowed(state, n)]

    def before_tool(self, state, call, tool):
        """返回 None 放行、返回字符串拒绝；approver 是 async 函数时返回一个协程（Agent 会 await 它）。"""
        if tool is None:
            return None  # 不存在的工具交给 registry 报错
        if not self.allowed(state, call.name):
            return f"拒绝：当前用户（角色 {state.metadata.get('roles', [])}）无权使用工具 {call.name}。"
        if tool.risk in self.ask_risks:
            if tool.parse_arguments(call.arguments)[1] is not None:
                return None  # 参数不合法：不打扰审批人，交给 registry 返回校验错误让模型自己改
            decision = state.approvals.get(call.id)
            if decision is None and self.approver is not None:
                raw = self.approver(call, state)
                if inspect.isawaitable(raw):
                    # 千万不能 bool(raw)：bool(协程) 恒为 True，会把高危操作静默批准
                    return self._decide_later(raw, state, call, tool)
                decision = bool(raw)
                state.approvals[call.id] = decision
            return self._apply_decision(decision, call, tool)
        return None

    async def _decide_later(self, pending, state, call, tool) -> str | None:
        decision = bool(await pending)
        state.approvals[call.id] = decision
        return self._apply_decision(decision, call, tool)

    @staticmethod
    def _apply_decision(decision: bool | None, call, tool) -> str | None:
        if decision is None:
            raise PauseRun(call, f"操作 {call.name}({call.arguments}) 风险等级为 {tool.risk}，已提交人工审批。")
        if not decision:
            return f"拒绝：审批人没有批准 {call.name} 操作。请告知用户该操作未获批准。"
        return None
