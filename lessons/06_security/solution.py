"""第 06 课练习 —— 参考答案。

先自己做 exercise.py，卡住超过 15 分钟再来看。对照时重点看注释里的"为什么"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal

from agentkit.guardrails import redact_pii
from agentkit.hooks import Hook, PauseRun
from agentkit.tools import Tool

Decision = Literal["allow", "ask", "deny"]
RISKS = ("read", "write", "dangerous")

# =====================================================================
# 练习 (a)：PolicyEngine —— 权限决策矩阵
# =====================================================================

# 套餐 × 风险等级 → 决策。"策略即数据"：改策略只改这张表，不改代码。
PLAN_MATRIX: dict[str, dict[str, Decision]] = {
    "free": {"read": "allow", "write": "ask", "dangerous": "deny"},
    "pro": {"read": "allow", "write": "allow", "dangerous": "ask"},
    "enterprise": {"read": "allow", "write": "allow", "dangerous": "ask"},
}


@dataclass
class PolicyEngine:
    """权限决策点（Policy Decision Point）：只负责"判"，不负责"执行"。

    role_tools: 角色 → 允许使用的工具名集合（RBAC 白名单），"*" 表示全部工具。
    deny_tools: 全局禁用的工具（紧急开关：比如某个工具刚发现漏洞）。
    role_deny:  角色 → 显式禁止的工具集合，"*" 表示全部（比如 "suspended" 账号被冻结）。
    plan_matrix: 租户套餐 × 风险等级 的决策表。
    """

    role_tools: dict[str, set[str]]
    deny_tools: set[str] = field(default_factory=set)
    role_deny: dict[str, set[str]] = field(default_factory=dict)
    plan_matrix: dict[str, dict[str, Decision]] = field(default_factory=lambda: dict(PLAN_MATRIX))

    def decide(self, roles: Iterable[str], tool_name: str, risk: str, tenant_plan: str) -> Decision:
        roles = list(roles or [])
        # 规则 1：全局显式拒绝优先于一切（包括管理员的 "*"）
        if tool_name in self.deny_tools:
            return "deny"
        # 规则 2：任何一个角色显式拒绝 → 拒绝。deny 永远压过 allow，否则多一个角色就能"洗白"权限
        for role in roles:
            banned = self.role_deny.get(role, set())
            if "*" in banned or tool_name in banned:
                return "deny"
        # 规则 3：不认识的风险等级 → 拒绝（fail closed：有疑问时默认拒绝）
        if risk not in RISKS:
            return "deny"
        # 规则 4：RBAC 白名单，所有角色的授权取并集；没有角色 / 未知角色 = 没有权限
        allowed: set[str] = set()
        for role in roles:
            allowed |= set(self.role_tools.get(role, set()))
        if "*" not in allowed and tool_name not in allowed:
            return "deny"
        # 规则 5：查"套餐 × 风险"表；不认识的套餐按最严格的 free 处理（同样是 fail closed）
        row = self.plan_matrix.get(tenant_plan) or self.plan_matrix["free"]
        return row[risk]


class PolicyHook(Hook):
    """权限执行点（Policy Enforcement Point）：把 PolicyEngine 的决策接入 Agent 主循环。

    - visible_tools：决策为 deny 的工具根本不展示给模型（看不见就不会想去调用）；
    - before_tool：deny → 返回拒绝理由；ask → 暂停等待人工审批；allow → 放行。
    两处都检查是"双保险"：模型可能凭记忆或被注入诱导，去调用一个它"看不见"的工具。

    角色和套餐来自 state.metadata（由服务端根据登录身份填写，绝不来自用户输入或模型）。
    """

    def __init__(self, engine: PolicyEngine, tools: Iterable[Tool]):
        self.engine = engine
        self.risks = {t.name: t.risk for t in tools}

    def _decide(self, state, tool_name: str) -> Decision:
        return self.engine.decide(
            state.metadata.get("roles", []),
            tool_name,
            self.risks.get(tool_name, "unknown"),
            state.metadata.get("tenant_plan", "free"),
        )

    def visible_tools(self, state, names: list[str]) -> list[str]:
        return [n for n in names if self._decide(state, n) != "deny"]

    def before_tool(self, state, call, tool) -> str | None:
        if tool is None:
            return None  # 不存在的工具交给 registry 报错
        decision = self._decide(state, call.name)
        if decision == "deny":
            return f"拒绝：根据权限策略，当前用户不能使用工具 {call.name}。"
        if decision == "ask":
            approved = state.approvals.get(call.id)
            if approved is None:
                raise PauseRun(call, f"操作 {call.name}({call.arguments}) 需要人工审批。")
            if not approved:
                return f"拒绝：审批人没有批准 {call.name} 操作。请告知用户该操作未获批准。"
        return None


# =====================================================================
# 练习 (b)：redact —— 扩展 PII 脱敏
# =====================================================================

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4_PATTERN = rf"(?<![A-Za-z0-9.]){_OCTET}(?:\.{_OCTET}){{3}}(?![A-Za-z0-9]|\.\d)"

PROVINCES = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
PLATE_PATTERN = (
    rf"(?<![A-Za-z0-9])[{PROVINCES}][A-HJ-NP-Z][·• ]?"  # 省份简称 + 发证机关字母（无 I、O）+ 可选分隔符
    r"(?=[A-HJ-NP-Z0-9]*\d)[A-HJ-NP-Z0-9]{5,6}(?![A-Za-z0-9])"  # 5 位（普通）或 6 位（新能源），至少含 1 个数字
)

NAME_LABELS = ("姓名", "联系人", "收件人", "收货人", "持卡人", "患者")
NAME_PATTERN = (
    rf"((?:{'|'.join(NAME_LABELS)})[ \t]*[：:][ \t]*)"  # 第 1 组：标签 + 冒号（保留）
    r"[一-鿿]{2,4}(?:[·•][一-鿿]{1,10})*"  # 2-4 个汉字，可带"·"分隔的少数民族/外文译名
)


def redact(text: str) -> str:
    """在 agentkit 的 redact_pii 基础上，额外脱敏 IPv4 地址、中国车牌号、标签后的姓名。"""
    text = redact_pii(text or "")
    text = re.sub(IPV4_PATTERN, "[IP已脱敏]", text)
    text = re.sub(PLATE_PATTERN, "[车牌号已脱敏]", text)
    text = re.sub(NAME_PATTERN, r"\1[姓名已脱敏]", text)
    return text


# =====================================================================
# 练习 (c)：lethal_trifecta —— 致命三要素检测
# =====================================================================

READS_PRIVATE_DATA = "reads_private_data"
INGESTS_UNTRUSTED_CONTENT = "ingests_untrusted_content"
CAN_EXFILTRATE = "can_exfiltrate"
CAPABILITIES = (READS_PRIVATE_DATA, INGESTS_UNTRUSTED_CONTENT, CAN_EXFILTRATE)
CAPABILITY_NAMES = {
    READS_PRIVATE_DATA: "私有数据访问",
    INGESTS_UNTRUSTED_CONTENT: "不可信内容",
    CAN_EXFILTRATE: "对外通信",
}


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的能力标签。labels 只能取 CAPABILITIES 里的值，拼错直接报错（安全配置不能静默出错）。"""

    name: str
    labels: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "labels", frozenset(self.labels))
        unknown = self.labels - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"工具 {self.name} 有未知的能力标签：{sorted(unknown)}，可选值：{CAPABILITIES}")


def lethal_trifecta(tools: list[ToolSpec]) -> list[str]:
    """检测 Agent 的工具组合是否同时具备"致命三要素"，返回风险说明（格式见 exercise.py 的 docstring）。"""
    providers = {cap: list(dict.fromkeys(t.name for t in tools if cap in t.labels)) for cap in CAPABILITIES}
    if not all(providers.values()):
        return []

    lines = ["⚠️ 致命三要素齐全：藏在不可信内容里的指令，可以诱导 Agent 读取私有数据并发送出去"]
    for cap in CAPABILITIES:
        lines.append(f"{CAPABILITY_NAMES[cap]}（{cap}）：{', '.join(providers[cap])}")

    # 关键工具：某项能力的唯一提供者。拿掉它，这项能力就没了，三要素随之被打破
    critical = list(dict.fromkeys(names[0] for names in providers.values() if len(names) == 1))
    order = list(dict.fromkeys(t.name for t in tools))
    critical.sort(key=order.index)
    if critical:
        lines.append(f"建议：移除以下任一工具即可打破三要素：{', '.join(critical)}")
    else:
        lines.append("建议：没有单个工具能打破三要素，请拆分 Agent，或对所有对外通信工具加人工审批")
    return lines
