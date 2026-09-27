"""第 06 课练习：安全与治理。

一共三题：
  (a) PolicyEngine.decide  —— 权限决策矩阵：显式拒绝 > 角色白名单 > 套餐 × 风险等级
  (b) redact               —— 在 agentkit 的 redact_pii 基础上，扩展 IPv4、车牌号、姓名脱敏
  (c) lethal_trifecta      —— 检测工具组合是否构成"致命三要素"，并给出拆解建议

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=06
    # 或者：.venv/bin/python -m pytest lessons/06_security -v

卡住了？先重读 README 对应小节，再看 solution.py。
"""

from __future__ import annotations

import re  # noqa: F401  （实现 redact 时会用到）
from dataclasses import dataclass, field
from typing import Iterable, Literal

from agentkit.guardrails import redact_pii  # noqa: F401
from agentkit.hooks import Hook, PauseRun
from agentkit.tools import Tool

Decision = Literal["allow", "ask", "deny"]
RISKS = ("read", "write", "dangerous")

# =====================================================================
# 练习 (a)：PolicyEngine —— 权限决策矩阵
# =====================================================================

# 套餐 × 风险等级 → 决策。"策略即数据"：改策略只改这张表，不改代码。
#   free（免费版）：写操作需要确认，危险操作一律禁止
#   pro / enterprise：写操作放行，危险操作需要人工审批
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
        """返回 "allow"（放行）/ "ask"（需人工审批）/ "deny"（拒绝）。

        规则按顺序检查，先命中先返回：
          1. tool_name 在 deny_tools 里 → "deny"（全局紧急开关，压过一切，包括管理员的 "*"）
          2. roles 中任意一个角色的 role_deny 包含 tool_name 或 "*" → "deny"
             （显式拒绝永远压过允许：否则给用户多加一个角色，就能把被禁止的权限"洗白"）
          3. risk 不是 "read" / "write" / "dangerous" 之一 → "deny"（不认识的一律拒绝：fail closed）
          4. RBAC 白名单：把 roles 中所有角色在 role_tools 里的工具集合取并集，
             并集里既没有 "*" 也没有 tool_name → "deny"
             （没有角色、或者角色不在 role_tools 里，都等于没有任何权限）
          5. 查 plan_matrix[tenant_plan][risk] 作为结果；
             tenant_plan 不在 plan_matrix 里 → 按 "free" 这一行处理（未知套餐按最严格的来）

        例：
          engine = PolicyEngine(role_tools={"employee": {"search_kb"}, "admin": {"*"}})
          engine.decide(["employee"], "search_kb", "read", "free")          == "allow"
          engine.decide(["employee"], "reset_password", "dangerous", "pro") == "deny"   # 不在白名单
          engine.decide(["admin"], "reset_password", "dangerous", "pro")    == "ask"
          engine.decide(["admin"], "reset_password", "dangerous", "free")   == "deny"   # 免费版禁止危险操作
          engine.decide([], "search_kb", "read", "enterprise")              == "deny"   # 没有角色

        边界情况：roles 可能是空列表或 None；一个用户可能同时有多个角色。
        """
        raise NotImplementedError("TODO: 练习 (a) —— 实现权限决策")


class PolicyHook(Hook):
    """权限执行点（Policy Enforcement Point）：把 PolicyEngine 的决策接入 Agent 主循环。（已写好，不用改）

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

# 31 个省级行政区的车牌简称
PROVINCES = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
# 后面紧跟姓名的标签
NAME_LABELS = ("姓名", "联系人", "收件人", "收货人", "持卡人", "患者")


def redact(text: str) -> str:
    """在 agentkit.guardrails.redact_pii 的基础上，额外脱敏三类信息。

    第 0 步：先调用 redact_pii(text)（它负责身份证号、银行卡号、手机号、邮箱）。
    然后依次处理：

    1. IPv4 地址 → "[IP已脱敏]"
       - 4 段，每段是 0-255 的十进制数，不允许前导零（"01" 不算）；
       - 前面不能紧挨 ASCII 字母、数字或点，后面不能紧挨 ASCII 字母、数字，也不能紧跟"点+数字"：
         这样 "v1.2.3.4"（版本号）、"1.2.3.4.5"、"999.1.1.1" 都不会被误判；
       - 但句末的英文句号不影响："访问 8.8.8.8." → "访问 [IP已脱敏]."

    2. 中国车牌号 → "[车牌号已脱敏]"
       - 省份简称（见 PROVINCES）+ 1 个大写字母（发证机关，不含 I、O）
         + 可选的分隔符（"·"、"•" 或一个空格）
         + 5 位（普通车牌）或 6 位（新能源车牌）大写字母或数字（不含 I、O），且其中至少有 1 个数字；
       - 前后都不能紧挨 ASCII 字母或数字；
       - 例："京A12345"、"粤B·6K789"、"沪AD12345"（新能源）都要脱敏；
         "新款iPhone"、"北京A区"、"湘A12345678" 都不是车牌。
       - 不要求覆盖警车、教练车、挂车、港澳车牌等特殊号牌。

    3. 标签后的中文姓名 → 保留标签和冒号，只替换姓名为 "[姓名已脱敏]"
       - 标签见 NAME_LABELS；标签和冒号之间、冒号和姓名之间可以有空格或制表符；中英文冒号都要支持；
       - 姓名 = 2-4 个汉字，后面可以跟若干段"·汉字"（如"买买提·艾力"）；
       - 例："姓名：张三，电话…" → "姓名：[姓名已脱敏]，电话…"
             "联系人: 李四" → "联系人: [姓名已脱敏]"
             "客户姓名：欧阳娜娜" → "客户姓名：[姓名已脱敏]"（"姓名"是标签）
       - 已知取舍：姓名后面直接跟汉字时会多遮几个字（"姓名：张三丰今天来了" 会遮掉"张三丰今"）。
         安全场景里"多遮"比"漏遮"好。

    硬性要求：
      - 不能误伤普通数字和日期："2025-06-16"、"14:30"、"12345.67 元"、"版本 v2.3.1"、"房间 305"、"工号 A12345"
        都必须保持原样；
      - 幂等：redact(redact(x)) == redact(x)（脱敏结果再脱敏一次不能变）。

    提示：先写正则，再用 re.sub；向前 / 向后断言 (?<!...) (?!...) 是控制边界的关键；
    多用 Python REPL 对着上面的例子试。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 实现扩展脱敏")


# =====================================================================
# 练习 (c)：lethal_trifecta —— 致命三要素检测
# =====================================================================

READS_PRIVATE_DATA = "reads_private_data"  # 能读私有数据：邮箱、CRM、内部文档、数据库……
INGESTS_UNTRUSTED_CONTENT = "ingests_untrusted_content"  # 会读到外人可控的内容：网页、邮件、工单、公共知识库……
CAN_EXFILTRATE = "can_exfiltrate"  # 能把数据送出去：发邮件、发 HTTP 请求、写公开仓库、渲染外链图片……
CAPABILITIES = (READS_PRIVATE_DATA, INGESTS_UNTRUSTED_CONTENT, CAN_EXFILTRATE)
CAPABILITY_NAMES = {
    READS_PRIVATE_DATA: "私有数据访问",
    INGESTS_UNTRUSTED_CONTENT: "不可信内容",
    CAN_EXFILTRATE: "对外通信",
}


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的能力标签。labels 只能取 CAPABILITIES 里的值，拼错直接报错（安全配置不能静默出错）。（已写好）

    例：ToolSpec("read_email", {READS_PRIVATE_DATA, INGESTS_UNTRUSTED_CONTENT})
    """

    name: str
    labels: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "labels", frozenset(self.labels))
        unknown = self.labels - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"工具 {self.name} 有未知的能力标签：{sorted(unknown)}，可选值：{CAPABILITIES}")


def lethal_trifecta(tools: list[ToolSpec]) -> list[str]:
    """检测 Agent 的工具组合是否同时具备 Simon Willison 提出的"致命三要素"。

    三要素：私有数据访问 + 不可信内容 + 对外通信。三者齐全时，攻击者只要在 Agent 会读到的内容里
    埋一段指令，就可能让 Agent 读取私有数据并发送出去。三者可以来自不同工具，也可以来自同一个工具。

    返回值：
      - 三要素不全（包括 tools 为空）→ 返回 []
      - 三要素齐全 → 返回恰好 5 行字符串：
          [0] 总结行，必须包含"致命三要素"四个字；
          [1] f"{CAPABILITY_NAMES[READS_PRIVATE_DATA]}（reads_private_data）：工具1, 工具2"
          [2] f"{CAPABILITY_NAMES[INGESTS_UNTRUSTED_CONTENT]}（ingests_untrusted_content）：..."
          [3] f"{CAPABILITY_NAMES[CAN_EXFILTRATE]}（can_exfiltrate）：..."
              每行列出提供该能力的工具名，按输入顺序、用 ", " 连接、同名只列一次；
          [4] 建议行，以"建议："开头：
              - "关键工具" = 某项能力的唯一提供者（拿掉它，这项能力就没了，三要素随之被打破）。
                如果存在关键工具：f"建议：移除以下任一工具即可打破三要素：{按输入顺序去重后用 ', ' 连接}"
              - 如果不存在（每项能力都至少有两个工具提供）：
                "建议：没有单个工具能打破三要素，请拆分 Agent，或对所有对外通信工具加人工审批"

    例：
      lethal_trifecta([
          ToolSpec("query_crm", {READS_PRIVATE_DATA}),
          ToolSpec("fetch_url", {INGESTS_UNTRUSTED_CONTENT, CAN_EXFILTRATE}),
      ])
      → ["⚠️ 致命三要素齐全：……",
         "私有数据访问（reads_private_data）：query_crm",
         "不可信内容（ingests_untrusted_content）：fetch_url",
         "对外通信（can_exfiltrate）：fetch_url",
         "建议：移除以下任一工具即可打破三要素：query_crm, fetch_url"]
    """
    raise NotImplementedError("TODO: 练习 (c) —— 实现致命三要素检测")
