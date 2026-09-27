"""ITBuddy 自定义的治理组件：在 agentkit 现有 Hook 之上补齐"业务相关"的能力。

1. check_reset_permission / ArgumentPolicy —— 参数级授权（ABAC）
   agentkit 的 PermissionPolicy 做的是 RBAC："这个角色能不能用这个工具"。
   但 reset_password 的风险取决于**参数**：重置自己 ≠ 重置同事 ≠ 重置别家公司的人。
   这种"看参数 + 看身份"的判断叫 ABAC（基于属性的访问控制），必须单独做。

2. ITBuddyAuditLog —— 在 AuditLog 基础上补记：安全事件、审批决定（含审批意见）。
   审计的核心问题是"出事后能不能回答：谁、何时、以什么身份、批准了什么"。
   agentkit 的 AuditLog 已经记录了 run_end.pending_approval（在等谁批什么）和 tool_call.approved_by（谁批的）；
   这里再补两类：安全事件单独成条，方便安全团队订阅告警；审批决定在恢复执行之前就落审计（含意见）。

3. CanaryGuard —— 系统提示词泄露检测（金丝雀标记）
   在系统提示词里埋一个随机字符串，一旦它出现在输出里，就说明提示词被套出来了。
"""

from __future__ import annotations

import time
from typing import Callable

from agentkit import AuditLog, Hook
from agentkit.state import RunState

from .backend import Backend

# ---------------------------------------------------------------------------- 参数级授权


def check_reset_permission(
    backend: Backend, tenant_id: str | None, actor_id: str | None, roles: list[str] | tuple[str, ...],
    target_user_id: str | None,
) -> str | None:
    """reset_password 的授权规则。返回 None 表示允许，否则返回拒绝原因（会作为观察反馈给模型）。

    这个函数被调用两次：
    - 在 ArgumentPolicy（Hook）里，**审批之前**调用：无效请求根本不会打扰审批人；
    - 在工具函数内部再调用一次：纵深防御。万一有人装配 Agent 时忘了加 Hook，工具自己也守得住。
    """
    if not tenant_id or not actor_id:
        return "拒绝：当前会话没有经过认证的用户身份。"
    target = target_user_id or actor_id
    if target != actor_id and "it_admin" not in roles:
        return "拒绝：普通员工只能重置自己的密码。如需帮同事重置，请让本人发起，或联系 IT 管理员。"
    if backend.get_employee(tenant_id, target) is None:
        # 注意措辞：不说"该用户属于其他公司"，只说"本公司找不到"。
        # 否则攻击者可以用这个工具探测别的租户里有哪些人（枚举攻击）。
        return f"拒绝：本公司员工目录中找不到用户 {target!r}。"
    return None


ArgRule = Callable[[RunState, dict], "str | None"]


class ArgumentPolicy(Hook):
    """按工具名配置参数级规则：rules = {tool_name: rule(state, args) -> 拒绝原因 | None}。"""

    def __init__(self, rules: dict[str, ArgRule]):
        self.rules = rules

    def before_tool(self, state, call, tool) -> str | None:
        rule = self.rules.get(call.name)
        return rule(state, call.parsed_args()) if rule else None


def reset_password_rule(backend: Backend) -> ArgRule:
    def rule(state: RunState, args: dict) -> str | None:
        m = state.metadata
        return check_reset_permission(backend, m.get("tenant_id"), m.get("user_id"), m.get("roles", []),
                                      args.get("target_user_id"))

    return rule


# ---------------------------------------------------------------------------- 审计


class ITBuddyAuditLog(AuditLog):
    """AuditLog 的增强版。继承而不是修改框架代码：框架给通用能力，业务在外面扩展。"""

    def record(self, event: str, **fields) -> None:
        """公开的写入口：给 app / server 记录 Agent 之外发生的事（如审批决定和审批意见）。"""
        self._write({"ts": time.time(), "event": event, **fields})

    def on_run_end(self, state) -> None:
        base = {"run_id": state.run_id, "tenant_id": state.metadata.get("tenant_id"),
                "user_id": state.metadata.get("user_id")}
        security = {k: state.metadata[k] for k in
                    ("blocked_by", "injection_in_tool_output", "secret_leak_blocked", "canary_leak_blocked")
                    if k in state.metadata}
        if security:
            # 安全事件单独成条，方便 SIEM / 安全团队按 event 字段订阅告警
            self.record("security_event", **base, **security)
        super().on_run_end(state)


# ---------------------------------------------------------------------------- 提示词泄露检测


class CanaryGuard(Hook):
    """最终输出里出现金丝雀标记 → 说明系统提示词被套出来了 → 整条替换并打标记。

    它只能发现"原样泄露"，模型换个说法复述就发现不了。所以它是检测手段，不是防御的全部：
    真正的原则是"系统提示词里不放任何秘密"，泄露了也不应该造成实质损失。
    """

    def __init__(self, canary: str):
        self.canary = canary

    def on_final(self, state, output: str) -> str | None:
        if self.canary in (output or ""):
            state.metadata["canary_leak_blocked"] = True
            return "抱歉，我不能提供内部配置信息。有 IT 问题我很乐意帮忙。"
        return None

