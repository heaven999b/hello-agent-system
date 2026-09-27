"""紧急开关（kill switch）：出事时几秒内止血，不用发布、不用重启（第 16 课 问题 4）。

开关存在"配置中心"里（这里用一个 dict 模拟），KillSwitch 每次都实时读取：

    {
        "agent_disabled": False,               # 整个 Agent 停用 → 直接转人工
        "disabled_tools": ["refund"],          # 全局停用某些工具
        "read_only": False,                    # 全局只读：停用所有 write / dangerous 工具
        "tenants": {"acme": {"disabled_tools": [...], "read_only": True, "agent_disabled": False}},
    }

和 PermissionPolicy(deny_tools=...) 的区别：deny_tools 在构造 Agent 时就固定了，改它要重新部署；
而事故发生时，"改配置 → 生效"必须以秒计。
"""

from __future__ import annotations

from typing import Callable, Iterable

from agentkit.hooks import Hook, StopRun
from agentkit.tools import Tool

WRITE_RISKS = {"write", "dangerous"}


def blocked_reason(flags: dict, tool_name: str, tool_risk: str, tenant_id: str | None) -> str | None:
    """返回拒绝理由（会作为观察反馈给模型）；放行返回 None。任何缺失的键都视为"没有限制"。"""
    tenant_flags = {}
    if tenant_id is not None:
        tenant_flags = (flags.get("tenants") or {}).get(tenant_id) or {}
    if tool_name in set(flags.get("disabled_tools") or ()):
        return f"工具 {tool_name} 已被紧急停用（全局）。请告诉用户该功能暂时不可用，不要尝试用其他工具绕过。"
    if tool_name in set(tenant_flags.get("disabled_tools") or ()):
        return f"工具 {tool_name} 已对租户 {tenant_id} 紧急停用。请告诉用户该功能暂时不可用，不要尝试用其他工具绕过。"
    if (flags.get("read_only") or tenant_flags.get("read_only")) and tool_risk in WRITE_RISKS:
        return f"系统当前处于只读模式，暂时不能执行 {tool_name} 这类修改操作。请告诉用户稍后再试，或转人工处理。"
    return None


class KillSwitch(Hook):
    """三个生效点：on_run_start（整个 Agent 停用）、visible_tools（不再展示）、before_tool（真正的拦截）。"""

    def __init__(self, flags_source: Callable[[], dict], tools: Iterable[Tool] = ()):
        self.flags_source = flags_source
        self.risks = {t.name: t.risk for t in tools}

    def _flags(self) -> dict:
        return self.flags_source() or {}

    def on_run_start(self, state, user_input):
        flags = self._flags()
        tenant = state.metadata.get("tenant_id")
        if flags.get("agent_disabled") or (flags.get("tenants") or {}).get(tenant, {}).get("agent_disabled"):
            raise StopRun("kill_switch", "智能助手正在维护，已为您转接人工客服。")
        return None

    def visible_tools(self, state, names):
        flags, tenant = self._flags(), state.metadata.get("tenant_id")
        return [n for n in names if blocked_reason(flags, n, self.risks.get(n, "read"), tenant) is None]

    def before_tool(self, state, call, tool):
        risk = tool.risk if tool is not None else self.risks.get(call.name, "read")
        return blocked_reason(self._flags(), call.name, risk, state.metadata.get("tenant_id"))
