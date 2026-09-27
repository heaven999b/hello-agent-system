"""第 07 课：工程考量的"维度目录" —— 20 个维度，每个维度的通用必查点与情境触发点。

这是本课的**单一事实来源**（single source of truth）：
  - README.md / README.en.md 第 2 节的每一条都能在这里找到同一个 id、同一个级别、同一个标题；
    第 3 节的"场景 × 维度"矩阵与这里的 SCENARIOS 一致（test_exercise.py 里有一致性检查）；
  - demo.py 用它为一个项目画像生成"必须考虑的清单"；
  - exercise.py 的规则引擎读取这里的条件（Condition）。

数据一览
  FACTS         项目画像里的"事实"（相当于一份问卷）：名字、类型、中英文说明
  GROUPS        6 个维度分组
  DIMENSIONS    20 个维度
  CATALOG       全部考量点（Consideration）：通用必查点 when=None；情境触发点 when=条件
  FOCUS_RULES   场景权重规则：画像满足条件时给某些维度加权（练习 b 的 prioritize 用）
  SCENARIOS     9 个典型场景画像（README 第 3 节的矩阵、demo 的预置画像）
  EXAMPLE       第 4 节"完整小例子"的需求原文与人工标注的画像

条件（Condition）就是普通的 dict，所以可以原样放进 JSON / YAML 配置文件：

    {"fact": "can_send_external"}                            布尔事实为 True
    {"fact": "peak_qps", "op": ">=", "value": 50}            数值阈值
    {"fact": "peak_llm_rpm", "op": ">", "value": {"fact": "provider_rpm_limit"}}   和另一个事实比较
    {"all": [c1, c2]}   {"any": [c1, c2]}   {"not": c}         组合

条件的求值（规则引擎）是练习 (a)，本文件只负责"数据"和"把条件翻译成人话"。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# =====================================================================
# 基础类型
# =====================================================================

SEVERITIES = ("P0", "P1", "P2")
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
SEVERITY_ICON = {"P0": "🔴", "P1": "🟠", "P2": "🟢"}

# 比较运算符：条件里的 "op" 只能是这几个
OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}

# 通用必查点（when=None）的触发原因
GENERAL_REASON = "always"

# coverage_report 里视为"占位、还没写"的标记（已经是规范化后的小写形式）
PLACEHOLDER_MARKERS = ("todo", "tbd", "待补充", "待填写")


class UnknownFactError(KeyError):
    """条件里引用了画像中不存在的事实。

    为什么要报错而不是当作 False：画像里"忘了填"不等于"不需要考虑"。
    如果静默当作 False，漏填一个 can_send_external，整组数据外泄的检查就悄无声息地消失了。
    """

    def __init__(self, fact: str):
        super().__init__(fact)
        self.fact = fact

    def __str__(self) -> str:
        return f"画像里没有事实 {self.fact!r}（unknown fact）—— 请补全画像，不要默认当作 False"


@dataclass(frozen=True)
class Fact:
    name: str
    kind: str  # "bool" | "number"
    zh: str
    en: str


@dataclass(frozen=True)
class Group:
    id: str
    zh: str
    en: str


@dataclass(frozen=True)
class Dimension:
    id: str
    group: str
    zh: str
    en: str
    decide_by: str  # "design" 设计期 | "launch" 上线前 | "operate" 运营期 —— 最晚要在什么时候想清楚
    lessons: tuple[str, ...]
    keywords: tuple[str, ...]  # 给 coverage_report 用：设计文档里出现这些词，算"提到了"这个维度


@dataclass(frozen=True)
class Consideration:
    """一个考量点。when=None 是通用必查点；否则是情境触发点。"""

    id: str  # "<维度 id>.<短名>"
    severity: str  # "P0" | "P1" | "P2"
    zh: str
    en: str
    when: dict | None = None
    refs: tuple[str, ...] = ()  # "L09" = 第 09 课；"S3" = 失败模式图鉴里的 S3

    @property
    def dimension(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def kind(self) -> str:
        return "general" if self.when is None else "situational"

    def title(self, lang: str = "zh") -> str:
        return self.zh if lang == "zh" else self.en


@dataclass(frozen=True)
class Triggered:
    """规则引擎的输出：一个适用于本项目的考量点 + 为什么适用。"""

    item: Consideration
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Ranked:
    """prioritize 的输出：排好序的考量点。rank 从 1 开始。"""

    rank: int
    item: Consideration
    reasons: tuple[str, ...]
    weight: float


@dataclass(frozen=True)
class FocusRule:
    """画像满足 when 时，给 weights 里的维度加权。"""

    when: dict
    weights: Mapping[str, float]


@dataclass(frozen=True)
class Scenario:
    id: str
    zh: str
    en: str
    short_zh: str  # 矩阵表头用的短名
    short_en: str
    profile: dict
    focus: tuple[str, ...]  # 重点关注的维度
    defer: tuple[str, ...]  # 可以先放一放的维度（只放情境点与 P2，通用 P0 永远不能放）


@dataclass(frozen=True)
class Requirement:
    """coverage_report 的检查项：一份设计文档应该覆盖的一个维度或考量点。"""

    id: str
    title: str
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    how: str  # "heading" | "keyword"
    line_no: int  # 从 1 开始
    line: str  # 原始行（去掉首尾空白）


@dataclass
class CoverageReport:
    covered: dict[str, Evidence] = field(default_factory=dict)
    missing: list[Requirement] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        total = len(self.covered) + len(self.missing)
        return 1.0 if total == 0 else len(self.covered) / total


# =====================================================================
# 事实（画像问卷）
# =====================================================================

FACTS: tuple[Fact, ...] = (
    # ---- 用户与数据
    Fact("external_users", "bool", "有公司外部的人（客户、合作方、公众）直接使用它", "people outside the company (customers, partners, the public) use it directly"),
    Fact("consumer_facing", "bool", "输出直接展示给 C 端公众（普通消费者）", "its output is shown directly to the general public (consumers)"),
    Fact("minors", "bool", "用户中可能有未成年人", "users may include minors"),
    Fact("handles_pii", "bool", "处理个人信息（姓名、电话、邮箱、工号……）", "it processes personal data (names, phone numbers, emails, employee IDs...)"),
    Fact("sensitive_data", "bool", "处理敏感个人信息或受特别监管的数据（医疗健康、金融账户、生物识别、行踪轨迹……）", "it processes sensitive or specially regulated data (health, financial accounts, biometrics, location...)"),
    Fact("regulated_industry", "bool", "属于强监管行业（金融、保险、医疗、医药、政府……）", "it operates in a heavily regulated industry (finance, insurance, healthcare, pharma, government...)"),
    Fact("automated_decisions", "bool", "输出会决定或显著影响对个人的决定（审批贷款、筛简历、定价、封号）", "its output decides or significantly shapes decisions about individuals (loans, hiring, pricing, bans)"),
    Fact("cross_border", "bool", "数据跨境：用户、模型服务、存储不在同一个法域", "data crosses borders: users, model service and storage are in different jurisdictions"),
    # ---- 能力与风险面
    Fact("accesses_private_data", "bool", "能读取企业内部或用户的私有数据", "it can read internal or users' private data"),
    Fact("reads_untrusted_content", "bool", "会读取不可信内容（网页、外部邮件、用户上传的文件、工单、第三方 API 返回）", "it reads untrusted content (web pages, inbound email, uploaded files, tickets, third-party API responses)"),
    Fact("can_send_external", "bool", "能对外发送信息（邮件、IM、Webhook、任意 HTTP 请求、发布内容）", "it can send information out (email, IM, webhooks, arbitrary HTTP requests, publishing)"),
    Fact("writes_data", "bool", "能执行写操作或有副作用的操作（建单、改数据、发消息）", "it can write data or cause side effects (create tickets, change records, send messages)"),
    Fact("irreversible_actions", "bool", "有不可逆或高影响的操作（付款、删除、改生产配置、发出去就收不回的消息）", "it has irreversible or high-impact actions (payments, deletes, production changes, messages you cannot unsend)"),
    Fact("executes_code", "bool", "能执行代码或 shell 命令", "it can execute code or shell commands"),
    Fact("third_party_tools", "bool", "使用第三方提供的工具、插件或 MCP 服务器", "it uses third-party tools, plugins or MCP servers"),
    # ---- 架构形态
    Fact("uses_rag", "bool", "从企业知识库或文档检索内容来回答（RAG）", "it answers from enterprise knowledge retrieved at run time (RAG)"),
    Fact("long_term_memory", "bool", "有跨会话的长期记忆", "it has long-term memory across sessions"),
    Fact("multi_agent", "bool", "由多个 Agent 协作完成任务", "multiple agents collaborate on a task"),
    Fact("multi_tenant", "bool", "同一套系统服务多个企业客户或相互隔离的组织（租户）", "one deployment serves multiple customer organizations (tenants)"),
    Fact("batch", "bool", "以离线批处理为主，没有用户在实时等待", "it mainly runs offline batch jobs with nobody waiting in real time"),
    Fact("voice", "bool", "实时语音交互", "it is a real-time voice interaction"),
    Fact("multilingual", "bool", "需要支持多种语言", "it must support multiple languages"),
    Fact("self_hosted_model", "bool", "自己部署模型（而不是调用云端 API）", "it runs self-hosted models instead of a cloud API"),
    # ---- 数字
    Fact("peak_qps", "number", "高峰期每秒请求数", "peak requests per second"),
    Fact("llm_calls_per_request", "number", "每个请求平均调用模型的次数", "average model calls per request"),
    Fact("provider_rpm_limit", "number", "单个模型账号每分钟的请求上限（不知道就填 0）", "requests-per-minute limit of one model account (0 = unknown)"),
    Fact("p95_task_seconds", "number", "单次任务的 p95 耗时（秒）", "p95 duration of one task, in seconds"),
    Fact("daily_runs", "number", "每天的运行次数", "runs per day"),
    Fact("context_tokens", "number", "典型的单次上下文长度（token）", "typical context size per call, in tokens"),
    Fact("tool_count", "number", "暴露给 Agent 的工具数量", "number of tools exposed to the agent"),
    Fact("availability_slo", "number", "可用性目标（百分比，如 99.9）", "availability target in percent (e.g. 99.9)"),
    Fact("teams_involved", "number", "需要协作的团队数量", "number of teams that must work together"),
)

# 由其他事实推导出来的事实（derive_facts 计算，不需要用户填写）
DERIVED_FACTS: tuple[Fact, ...] = (
    Fact("peak_llm_rpm", "number", "峰值每分钟模型调用数（= 峰值 QPS × 每请求模型调用次数 × 60）", "peak model calls per minute (= peak QPS × model calls per request × 60)"),
)

FACT_BY_NAME: dict[str, Fact] = {f.name: f for f in FACTS + DERIVED_FACTS}


def derive_facts(profile: Mapping[str, Any]) -> dict[str, Any]:
    """返回补上推导事实后的新画像（不修改传入的 dict）。

    峰值模型调用数才是撞上模型配额的那个数：一个 Agent 请求往往要调用模型好几次。
    """
    out = dict(profile)
    if "peak_qps" in out and "llm_calls_per_request" in out:
        out["peak_llm_rpm"] = out["peak_qps"] * out["llm_calls_per_request"] * 60
    return out


def validate_profile(profile: Mapping[str, Any]) -> list[str]:
    """检查画像是否完整、类型是否正确。返回问题列表（空列表 = 没问题）。"""
    problems = []
    for f in FACTS:
        if f.name not in profile:
            problems.append(f"缺少事实 {f.name}")
            continue
        v = profile[f.name]
        if f.kind == "bool" and not isinstance(v, bool):
            problems.append(f"{f.name} 应该是 True/False，实际是 {v!r}")
        if f.kind == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            problems.append(f"{f.name} 应该是数字，实际是 {v!r}")
    for name in profile:
        if name not in FACT_BY_NAME:
            problems.append(f"未知的事实 {name}")
    return problems


# =====================================================================
# 把条件翻译成人话（给 README、demo 和规则引擎的"触发原因"用）
# =====================================================================

_OP_TEXT = {"==": "=", "!=": "≠", ">": ">", ">=": "≥", "<": "<", "<=": "≤"}


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"fact"}


def render_condition(cond: dict | None, style: str = "code") -> str:
    """把条件渲染成一行文字。style: "code"（README 里的"触发："）| "zh" | "en"。

    >>> render_condition({"all": [{"fact": "batch"}, {"fact": "peak_qps", "op": ">=", "value": 50}]})
    'batch and peak_qps >= 50'
    """
    if cond is None:
        return {"code": GENERAL_REASON, "zh": "任何项目", "en": "every project"}[style]
    words = {
        "code": (" and ", " or ", "not "),
        "zh": (" 且 ", " 或 ", "并非 "),
        "en": (" AND ", " OR ", "NOT "),
    }[style]

    def name(fact: str) -> str:
        if style == "code":
            return fact
        f = FACT_BY_NAME.get(fact)
        return (f.zh if style == "zh" else f.en) if f else fact

    def go(c: dict, top: bool) -> str:
        if "fact" in c:
            if "op" not in c:
                return name(c["fact"])
            value = c["value"]
            rhs = name(value["fact"]) if _is_ref(value) else _fmt_value(value)
            op = c["op"] if style == "code" else _OP_TEXT[c["op"]]
            return f"{name(c['fact'])} {op} {rhs}"
        if "not" in c:
            inner = go(c["not"], top=False)
            return words[2] + inner
        key = "all" if "all" in c else "any"
        joiner = words[0] if key == "all" else words[1]
        text = joiner.join(go(x, top=False) for x in c[key])
        return text if top else f"({text})"

    return go(cond, top=True)


def describe_leaf(cond: dict, profile: Mapping[str, Any]) -> str:
    """一个**成立的**叶子条件的触发原因（带上实际值），供规则引擎使用。

    >>> describe_leaf({"fact": "can_send_external"}, {"can_send_external": True})
    'can_send_external = True'
    >>> describe_leaf({"fact": "peak_qps", "op": ">=", "value": 50}, {"peak_qps": 80})
    'peak_qps = 80 >= 50'
    >>> describe_leaf({"fact": "peak_llm_rpm", "op": ">", "value": {"fact": "provider_rpm_limit"}},
    ...               {"peak_llm_rpm": 12000, "provider_rpm_limit": 5000})
    'peak_llm_rpm = 12000 > provider_rpm_limit = 5000'
    """
    name = cond["fact"]
    actual = _fmt_value(profile[name])
    if "op" not in cond:
        return f"{name} = {actual}"
    value = cond["value"]
    if _is_ref(value):
        ref = value["fact"]
        rhs = f"{ref} = {_fmt_value(profile[ref])}"
    else:
        rhs = _fmt_value(value)
    return f"{name} = {actual} {cond['op']} {rhs}"


def describe_not(child: dict) -> str:
    """一个成立的 {"not": child} 条件的触发原因。"""
    inner = render_condition(child, "code")
    return f"not ({inner})" if (" and " in inner or " or " in inner) else f"not {inner}"


def referenced_facts(cond: dict | None) -> set[str]:
    """条件里引用到的所有事实名（包括 value 里引用的事实）。"""
    if cond is None:
        return set()
    if "fact" in cond:
        out = {cond["fact"]}
        if _is_ref(cond.get("value")):
            out.add(cond["value"]["fact"])
        return out
    if "not" in cond:
        return referenced_facts(cond["not"])
    key = "all" if "all" in cond else "any"
    return set().union(*(referenced_facts(c) for c in cond[key])) if cond[key] else set()


# ---- 写条件的小帮手（让下面的目录读起来像句子）


def is_(fact: str) -> dict:
    return {"fact": fact}


def ge(fact: str, value: Any) -> dict:
    return {"fact": fact, "op": ">=", "value": value}


def gt(fact: str, value: Any) -> dict:
    return {"fact": fact, "op": ">", "value": value}


def eq(fact: str, value: Any) -> dict:
    return {"fact": fact, "op": "==", "value": value}


def ref(fact: str) -> dict:
    return {"fact": fact}


def all_(*conds: dict) -> dict:
    return {"all": list(conds)}


def any_(*conds: dict) -> dict:
    return {"any": list(conds)}


def not_(cond: dict) -> dict:
    return {"not": cond}


# ---- 阈值：写成常量，改一处全局生效（也方便你按公司实际情况调整）
STREAMING_SECONDS = 5  # 超过几秒的交互式任务，要流式输出
LONG_TASK_SECONDS = 60  # 超过 1 分钟：异步任务、检查点
VERY_LONG_TASK_SECONDS = 600  # 超过 10 分钟：里程碑、跨上下文窗口的进度笔记
DURABLE_TASK_SECONDS = 3600  # 超过 1 小时：持久化执行引擎
SCALE_OUT_QPS = 20  # 这个量级基本就是多实例部署了
HIGH_QPS = 50
MANY_RUNS_PER_DAY = 10_000
LONG_CONTEXT_TOKENS = 50_000
HIGH_AVAILABILITY = 99.9
MANY_TOOLS = 20
MANY_TEAMS = 3
REVIEW_HEAVY_RUNS = 1_000

LETHAL_TRIFECTA = all_(is_("accesses_private_data"), is_("reads_untrusted_content"), is_("can_send_external"))
LONG_TASK = ge("p95_task_seconds", LONG_TASK_SECONDS)
QUEUE_BASED = any_(is_("batch"), ge("p95_task_seconds", LONG_TASK_SECONDS))


# =====================================================================
# 分组与维度
# =====================================================================

GROUPS: tuple[Group, ...] = (
    Group("product", "产品与需求", "Product & requirements"),
    Group("intelligence", "智能层", "Intelligence layer"),
    Group("quality", "质量", "Quality"),
    Group("risk", "风险", "Risk"),
    Group("operations", "运营", "Operations"),
    Group("organization", "组织", "Organization"),
)

DIMENSIONS: tuple[Dimension, ...] = (
    # ---- 产品与需求
    Dimension("goals", "product", "目标与成功标准", "Goals & success criteria", "design", ("00", "05", "11"),
              ("成功标准", "上线标准", "非目标", "success criteria", "launch criteria", "non-goals")),
    Dimension("ux", "product", "用户体验与交互", "User experience & interaction", "design", ("02", "12"),
              ("用户体验", "交互形态", "流式输出", "转人工", "user experience", "streaming", "hand-off", "handoff")),
    Dimension("human", "product", "人机协作边界", "Human-AI collaboration", "design", ("06", "08", "09"),
              ("人工审批", "人工复核", "人在回路", "human-in-the-loop", "human approval", "human review")),
    # ---- 智能层
    Dimension("model", "intelligence", "模型与提示词", "Models & prompts", "design", ("01", "11", "14", "16"),
              ("模型选型", "模型版本", "提示词版本", "model selection", "model version", "prompt version")),
    Dimension("tools", "intelligence", "工具与行动", "Tools & actions", "design", ("03",),
              ("工具清单", "工具设计", "工具风险", "tool list", "tool design", "tool risk")),
    Dimension("context", "intelligence", "上下文、记忆与知识", "Context, memory & knowledge", "design", ("04", "15"),
              ("上下文策略", "上下文长度", "长期记忆", "context strategy", "context window", "long-term memory")),
    # ---- 质量
    Dimension("evals", "quality", "正确性与评估", "Correctness & evaluation", "launch", ("11",),
              ("评估集", "评估方案", "评测集", "eval set", "evaluation plan")),
    Dimension("reliability", "quality", "可靠性与容错", "Reliability & fault tolerance", "design", ("08", "13"),
              ("重试", "熔断", "降级", "检查点", "retry", "circuit breaker", "fallback", "checkpoint")),
    Dimension("latency", "quality", "性能与延迟", "Performance & latency", "design", ("14",),
              ("延迟", "p95", "首字时间", "latency", "time-to-first-token")),
    # ---- 风险
    Dimension("security", "risk", "安全", "Security", "design", ("09",),
              ("威胁模型", "提示词注入", "致命三要素", "threat model", "prompt injection", "lethal trifecta")),
    Dimension("privacy", "risk", "隐私与合规", "Privacy & compliance", "design", ("09", "12"),
              ("个人信息", "隐私", "数据保留", "跨境", "personal data", "privacy", "data retention", "cross-border")),
    Dimension("access", "risk", "权限与审计", "Authorization & audit", "design", ("03", "09"),
              ("权限矩阵", "最小权限", "审计日志", "rbac", "least privilege", "audit log")),
    Dimension("safety", "risk", "伦理与内容安全", "Responsible AI & content safety", "launch", ("09", "11"),
              ("内容安全", "内容审核", "公平性", "有害内容", "content safety", "moderation", "fairness", "harmful content")),
    # ---- 运营
    Dimension("cost", "operations", "成本", "Cost", "launch", ("14",),
              ("成本", "预算", "cost", "budget")),
    Dimension("observability", "operations", "可观测性", "Observability", "launch", ("10",),
              ("可观测", "链路追踪", "告警", "observability", "tracing", "alerting")),
    Dimension("ops", "operations", "发布与运维", "Release & operations", "launch", ("16", "12"),
              ("灰度", "回滚", "运行手册", "runbook", "rollout", "rollback", "canary", "on-call")),
    Dimension("scale", "operations", "可扩展性与并发", "Scalability & concurrency", "design", ("12", "13"),
              ("并发", "限流", "扩容", "背压", "concurrency", "rate limit", "scale out", "backpressure")),
    Dimension("tenancy", "operations", "多租户", "Multi-tenancy", "design", ("12", "15"),
              ("多租户", "租户隔离", "multi-tenant", "tenant isolation")),
    # ---- 组织
    Dimension("ownership", "organization", "团队与交接", "Ownership & handover", "operate", ("12", "16"),
              ("架构决策记录", "adr-", "负责团队", "交接", "值班培训", "ownership", "handover", "raci")),
    Dimension("vendor", "organization", "供应商依赖与退出", "Vendor dependency & exit", "operate", ("12", "16"),
              ("供应商锁定", "退出方案", "备用模型", "第二供应商", "vendor lock-in", "exit plan", "fallback model", "second source")),
)

DIMENSION_BY_ID: dict[str, Dimension] = {d.id: d for d in DIMENSIONS}
GROUP_BY_ID: dict[str, Group] = {g.id: g for g in GROUPS}


# =====================================================================
# 考量点目录
# =====================================================================


def _g(id: str, severity: str, zh: str, en: str, refs: str = "") -> Consideration:
    """通用必查点。"""
    return Consideration(id, severity, zh, en, None, tuple(refs.split()))


def _s(id: str, severity: str, zh: str, en: str, when: dict, refs: str = "") -> Consideration:
    """情境触发点。"""
    return Consideration(id, severity, zh, en, when, tuple(refs.split()))


CATALOG: tuple[Consideration, ...] = (
    # ================================================================ 目标与成功标准
    _g("goals.scope", "P0", "写清职责边界与非目标", "Write down scope and non-goals", "L00"),
    _g("goals.why_agent", "P0", "论证为什么非用 Agent 不可", "Justify why it has to be an agent", "L05 L06 O1"),
    _g("goals.launch_criteria", "P0", "定义可量化的上线标准", "Define measurable launch criteria", "L11"),
    _g("goals.baseline", "P1", "先测量现状基线", "Measure today's baseline first"),
    _g("goals.failure_cost", "P1", "分别评估答错、做错、泄露的代价", "Rate the cost of wrong answers, wrong actions and leaks"),
    _g("goals.stakeholders", "P1", "尽早拉齐利益相关方", "Bring every stakeholder in early"),
    _g("goals.unit_value", "P1", "算清一次任务值多少钱", "Know what one task is worth", "L14"),
    _g("goals.kill_criteria", "P2", "事先约定止损条件", "Agree on kill criteria up front"),
    _s("goals.hard_constraints", "P0", "把'绝不能说错的话'写成硬约束", "Turn 'must never get wrong' into hard constraints",
       is_("external_users"), "M5"),
    _s("goals.compliance_requirements", "P0", "把合规要求写成可验收的需求", "Turn compliance rules into testable requirements",
       is_("regulated_industry")),
    _s("goals.contestability", "P1", "把可解释、可申诉列为需求", "Make explanations and appeals part of the spec",
       is_("automated_decisions")),
    _s("goals.tenant_targets", "P1", "区分平台级与租户级目标", "Separate platform-level and tenant-level targets",
       is_("multi_tenant"), "L12"),
    _s("goals.long_task_milestones", "P1", "为长任务定义里程碑和中间验收", "Define milestones and interim acceptance for long tasks",
       ge("p95_task_seconds", VERY_LONG_TASK_SECONDS), "M2"),
    _s("goals.throughput_targets", "P2", "用吞吐量和截止时间定义成功", "Define success by throughput and deadlines",
       is_("batch")),
    _s("goals.unit_economics", "P1", "把单位经济写进上线标准", "Put unit economics into the launch criteria",
       ge("daily_runs", MANY_RUNS_PER_DAY), "L14"),
    # ================================================================ 用户体验与交互
    _g("ux.interaction_mode", "P1", "选定交互形态与等待预期", "Pick the interaction mode and waiting expectations", "L12"),
    _g("ux.capability_disclosure", "P1", "告诉用户能做什么、不能做什么", "Tell users what it can and cannot do"),
    _g("ux.visible_uncertainty", "P1", "让不确定可见：追问、给依据", "Make uncertainty visible: ask, cite, qualify", "M4 M5"),
    _g("ux.graceful_endings", "P0", "每种异常结束都有友好结局", "A friendly ending for every abnormal stop", "L02 L08"),
    _g("ux.feedback", "P1", "提供反馈入口并接入评估", "Collect feedback and wire it into evals", "L11 E1"),
    _g("ux.progress", "P2", "等待时给出进度", "Show progress while users wait"),
    _g("ux.channel_format", "P2", "输出格式适配渠道", "Fit the output format to the channel"),
    _s("ux.async_tasks", "P0", "异步任务、进度反馈与断点续看", "Async tasks, progress updates and resumable views",
       LONG_TASK, "L12 L13 P4"),
    _s("ux.streaming", "P1", "流式输出，缩短首字时间", "Stream output to cut time-to-first-token",
       all_(not_(is_("batch")), ge("p95_task_seconds", STREAMING_SECONDS)), "L14"),
    _s("ux.human_handoff", "P1", "转人工时带上完整上下文", "Hand off to humans with full context",
       is_("external_users")),
    _s("ux.voice_turn_taking", "P0", "打断、话轮与沉默处理", "Barge-in, turn-taking and silences",
       is_("voice")),
    _s("ux.action_preview", "P1", "写操作前先说清要做什么", "Say what it will change before changing it",
       is_("writes_data"), "M1"),
    _s("ux.confirmation_ui", "P1", "高影响操作的确认界面", "Confirmation UI for high-impact actions",
       is_("irreversible_actions"), "L09"),
    _s("ux.undo", "P1", "可撤销与可见的操作记录", "Undo and a visible activity log",
       is_("writes_data")),
    _s("ux.batch_exceptions", "P1", "批处理结果的异常队列", "Exception queues for batch results",
       is_("batch")),
    _s("ux.accessibility", "P2", "无障碍与弱网体验", "Accessibility and poor-network experience",
       is_("consumer_facing")),
    # ================================================================ 人机协作边界
    _g("human.autonomy_levels", "P0", "按风险划分自主等级", "Assign autonomy levels by risk", "L00 S5"),
    _g("human.escalation", "P0", "设计转人工与兜底路径", "Design escalation and fallback paths", "M3 M5"),
    _g("human.clear_responsibility", "P1", "写清人和 Agent 各负责什么", "Write down what the human and the agent are each responsible for"),
    _g("human.stop_button", "P1", "人能随时接管或叫停", "Humans can take over or stop at any time"),
    _g("human.progressive_autonomy", "P1", "先多抽检，再凭证据逐步放权", "Start with heavy human checks; widen autonomy with evidence", "L16"),
    _g("human.automation_bias", "P2", "防止自动化偏见与过度依赖", "Guard against automation bias and over-reliance"),
    _s("human.async_approval", "P0", "审批异步化：暂停、落盘、恢复", "Asynchronous approval: pause, persist, resume",
       is_("irreversible_actions"), "L08 R5"),
    _s("human.separation_of_duties", "P0", "职责分离：审批人不能是发起人", "Separation of duties: approver is not the requester",
       is_("irreversible_actions")),
    _s("human.reviewable_requests", "P1", "审批请求要让人看得懂", "Approval requests a human can judge",
       is_("irreversible_actions"), "L09"),
    _s("human.revalidate", "P1", "批准后、执行前重新校验", "Re-validate after approval, before execution",
       is_("irreversible_actions"), "R5"),
    _s("human.approval_expiry", "P1", "审批的超时、过期与提醒", "Approval timeouts, expiry and reminders",
       any_(is_("irreversible_actions"), is_("regulated_industry")), "R5"),
    _s("human.approval_quality", "P2", "监控审批质量，防橡皮图章", "Monitor approval quality; catch rubber-stamping",
       is_("irreversible_actions"), "L09"),
    _s("human.reviewer_capacity", "P1", "审批人的容量规划", "Plan reviewer capacity",
       all_(is_("irreversible_actions"), ge("daily_runs", REVIEW_HEAVY_RUNS))),
    _s("human.meaningful_review", "P0", "对个人的决定要有实质性人工复核", "Meaningful human review of decisions about people",
       is_("automated_decisions")),
    _s("human.command_policy", "P0", "命令与代码执行的确认策略", "Confirmation policy for commands and code",
       is_("executes_code"), "L09"),
    _s("human.qualified_reviewer", "P1", "由有资质的专业人员把关", "Qualified professionals in the loop",
       is_("regulated_industry")),
    _s("human.batch_sampling", "P1", "批量结果按风险分层抽检", "Risk-stratified sampling of batch output",
       is_("batch")),
    _s("human.delegation_checkpoints", "P2", "多 Agent 交接处设人工检查点", "Human checkpoints at multi-agent hand-offs",
       is_("multi_agent"), "L06"),
    # ================================================================ 模型与提示词
    _g("model.eval_driven_choice", "P1", "用评估数据选模型", "Choose models with eval data", "L01 L11"),
    _g("model.pin_snapshot", "P0", "生产固定模型快照版本", "Pin model snapshots in production", "E5"),
    _g("model.prompt_as_code", "P0", "提示词纳入版本控制与评审", "Treat prompts as code: versioned and reviewed", "L16 E4"),
    _g("model.no_secrets", "P0", "系统提示词里不放秘密", "Keep secrets out of the system prompt", "S1"),
    _g("model.behavior_rules", "P1", "写明信息不足、不确定、超范围时怎么做", "Spell out what to do when unsure or out of scope", "M1 M4 M5"),
    _g("model.structured_output", "P1", "结构化输出：原生能力或校验加修复", "Structured output: native mode or validate-and-repair", "L01 M7"),
    _g("model.sampling_config", "P2", "采样参数是有意识的配置", "Sampling parameters are deliberate config", "L01"),
    _g("model.cache_friendly_prompt", "P2", "提示词稳定部分在前", "Put stable prompt content first", "L14 B2"),
    _s("model.tiering", "P1", "按子任务分层选型", "Tier models per subtask",
       ge("daily_runs", MANY_RUNS_PER_DAY), "L14 B4"),
    _s("model.fallback_evals", "P1", "备用模型也要过同一套评估", "Fallback models pass the same evals",
       ge("availability_slo", HIGH_AVAILABILITY), "L08 R3"),
    _s("model.self_hosting", "P1", "自部署推理的容量与升级", "Capacity and upgrades for self-hosted inference",
       is_("self_hosted_model")),
    _s("model.speech_stack", "P1", "语音架构：端到端还是级联", "Speech architecture: end-to-end or cascaded",
       is_("voice")),
    _s("model.per_language", "P1", "按语言验证模型能力", "Verify model quality per language",
       is_("multilingual")),
    _s("model.tenant_prompts", "P1", "租户自定义提示词的边界", "Boundaries for tenant-customized prompts",
       is_("multi_tenant"), "S1"),
    _s("model.brand_voice", "P2", "语气、品牌与人设一致", "Consistent tone, brand voice and persona",
       is_("consumer_facing")),
    # ================================================================ 工具与行动
    _g("tools.model_facing_docs", "P0", "写给模型看的工具说明", "Tool docs written for the model", "L03 T1"),
    _g("tools.schema_validation", "P0", "参数严格按 Schema 校验", "Strict schema validation of arguments", "L03 M4"),
    _g("tools.identity_injection", "P0", "身份不进参数，由系统注入", "Identity is injected, never an argument", "L03 S4"),
    _g("tools.risk_levels", "P0", "每个工具标注风险等级", "Tag every tool with a risk level", "L03 L09"),
    _g("tools.timeouts", "P0", "每个工具都有超时", "Every tool has a timeout", "T4"),
    _g("tools.output_limits", "P1", "输出有上限，截断要明说", "Cap output and say when it is truncated", "T3"),
    _g("tools.actionable_errors", "P1", "错误信息让模型能据此行动", "Errors the model can act on", "T6"),
    _g("tools.small_toolset", "P1", "控制暴露给模型的工具数量", "Keep the exposed toolset small", "T2"),
    _s("tools.idempotency", "P0", "写工具幂等，键传到下游", "Idempotent writes with keys passed downstream",
       is_("writes_data"), "L08 T5"),
    _s("tools.atomic_operations", "P1", "多步写操作封装成原子工具", "Wrap multi-step writes into atomic tools",
       is_("writes_data"), "T7"),
    _s("tools.sandbox", "P0", "代码在沙箱里执行", "Run code in a sandbox",
       is_("executes_code"), "L09"),
    _s("tools.third_party_vetting", "P1", "第三方工具与 MCP 服务器要审核、锁版本", "Vet and pin third-party tools and MCP servers",
       is_("third_party_tools"), "S6"),
    _s("tools.tool_routing", "P1", "工具路由或按需加载", "Route or lazy-load tools",
       ge("tool_count", MANY_TOOLS), "T2"),
    _s("tools.dry_run", "P1", "预览、dry-run 与补偿动作", "Preview, dry-run and compensating actions",
       is_("irreversible_actions")),
    _s("tools.protect_downstream", "P1", "保护下游系统", "Protect downstream systems",
       ge("peak_qps", HIGH_QPS)),
    _s("tools.submit_and_poll", "P1", "慢操作拆成提交加查询", "Split slow operations into submit and poll",
       LONG_TASK, "T4"),
    # ================================================================ 上下文、记忆与知识
    _g("context.length_strategy", "P0", "有明确的上下文长度策略", "An explicit context-length strategy", "L04 C2"),
    _g("context.pairing", "P0", "截断时保持工具调用成对", "Keep tool calls and results paired when trimming", "C1"),
    _g("context.identity_from_idp", "P0", "身份与权限只从身份系统读", "Identity and roles come only from the IdP", "C6"),
    _g("context.state_outside", "P1", "关键业务状态放在上下文之外", "Keep business state outside the context", "C3 C4"),
    _g("context.untrusted_marking", "P1", "外部内容标记为不可信数据", "Mark external content as untrusted data", "L09 S2"),
    _g("context.summary_contract", "P1", "摘要必须保留目标、约束和已完成操作", "Summaries keep goals, constraints and completed actions", "C3"),
    _g("context.minimal_context", "P2", "只放完成任务需要的信息", "Include only what the task needs"),
    _s("context.acl_prefilter", "P0", "检索时按权限前过滤", "Filter by permissions before retrieval",
       all_(is_("uses_rag"), is_("accesses_private_data")), "L15 C7"),
    _s("context.freshness", "P1", "知识的更新与删除要传播", "Propagate knowledge updates and deletions",
       is_("uses_rag"), "L15 C8"),
    _s("context.citations", "P1", "引用要校验", "Verify citations",
       is_("uses_rag"), "L15 C9"),
    _s("context.memory_governance", "P1", "长期记忆的写入、过期与删除", "Govern long-term memory writes, expiry and deletion",
       is_("long_term_memory"), "L04 C6"),
    _s("context.long_context", "P1", "长上下文不等于有效上下文", "A long context is not an effective context",
       ge("context_tokens", LONG_CONTEXT_TOKENS), "C2"),
    _s("context.progress_notes", "P1", "跨上下文窗口的进度笔记", "Progress notes that survive context windows",
       ge("p95_task_seconds", VERY_LONG_TASK_SECONDS), "M2"),
    _s("context.delegation_brief", "P1", "委派时给出完整任务简报", "Give sub-agents a complete brief",
       is_("multi_agent"), "L06 O2"),
    # ================================================================ 正确性与评估
    _g("evals.dataset", "P0", "建立覆盖关键场景的评估集", "An eval set covering the cases that matter", "L11"),
    _g("evals.ci_gate", "P0", "评估接入 CI 作为发布门禁", "Evals gate every release in CI", "L11 E4"),
    _g("evals.trajectory", "P1", "同时评估结果与轨迹", "Grade both outcome and trajectory", "M1"),
    _g("evals.repeated_runs", "P1", "多次运行，看 pass^k", "Run repeatedly and watch pass^k", "E2"),
    _g("evals.calibrated_judges", "P1", "LLM 评委要有细则并与人工校准", "LLM judges with rubrics, calibrated against humans", "E3"),
    _g("evals.production_feedback", "P1", "线上 bad case 回流评估集", "Feed production failures back into the eval set", "E1"),
    _g("evals.change_triggers", "P1", "模型、提示词、工具任一变化都要全量评估", "Any model, prompt or tool change triggers a full run", "E4 E5"),
    _g("evals.cost_latency_in_reports", "P2", "评估报告同时报成本、延迟和步数", "Report cost, latency and steps alongside accuracy"),
    _s("evals.adversarial_cases", "P0", "对抗性用例进 CI", "Adversarial cases in CI",
       any_(is_("external_users"), is_("reads_untrusted_content")), "L09"),
    _s("evals.expert_labels", "P0", "领域专家标注与签字", "Domain-expert labels and sign-off",
       is_("regulated_industry")),
    _s("evals.fairness_slices", "P1", "按人群切片评估差异", "Slice results by population group",
       is_("automated_decisions")),
    _s("evals.per_language", "P1", "按语言分别评估", "Evaluate each language separately",
       is_("multilingual")),
    _s("evals.tenant_slices", "P1", "按租户切片与租户专属用例", "Per-tenant slices and tenant-specific cases",
       is_("multi_tenant")),
    _s("evals.online", "P1", "线上评估与 A/B 实验", "Online evaluation and A/B tests",
       ge("daily_runs", MANY_RUNS_PER_DAY), "L16"),
    _s("evals.retrieval_vs_generation", "P1", "检索与生成分开评估", "Evaluate retrieval and generation separately",
       is_("uses_rag"), "L15"),
    _s("evals.simulated_env", "P1", "用模拟环境评估有副作用的操作", "Evaluate side effects in a simulated environment",
       is_("writes_data")),
    _s("evals.real_audio", "P1", "用真实音频评估", "Evaluate with real audio",
       is_("voice")),
    # ================================================================ 可靠性与容错
    _g("reliability.retry_policy", "P0", "只重试可重试的错误，退避加抖动", "Retry only retryable errors, with backoff and jitter", "L08 R2"),
    _g("reliability.single_retry_layer", "P0", "重试只在一层发生", "Retry in exactly one layer", "R1"),
    _g("reliability.step_limits", "P0", "设置最大步数与墙钟时限", "Cap steps and wall-clock time", "L02 M3"),
    _g("reliability.explicit_status", "P1", "所有异常结束都收敛为明确状态", "Every abnormal stop maps to an explicit status", "P1"),
    _g("reliability.circuit_breaker", "P1", "下游持续失败时熔断", "Break the circuit when a dependency keeps failing", "R1"),
    _g("reliability.evaluated_fallbacks", "P1", "有降级方案，且降级路径经过评估", "Fallbacks exist and are evaluated", "R3"),
    _g("reliability.cancellation", "P1", "用户离开时取消后台运行", "Cancel runs when the user goes away"),
    _s("reliability.checkpoints", "P0", "每一步写检查点，可恢复", "Checkpoint every step so runs can resume",
       any_(LONG_TASK, is_("irreversible_actions")), "L08 R4"),
    _s("reliability.durable_execution", "P1", "引入持久化执行引擎", "Adopt a durable-execution engine",
       ge("p95_task_seconds", DURABLE_TASK_SECONDS), "L13"),
    _s("reliability.version_skew", "P1", "恢复时的版本错位", "Version skew on resume",
       LONG_TASK, "R6"),
    _s("reliability.multi_provider", "P1", "多供应商或多区域", "Multiple providers or regions",
       ge("availability_slo", HIGH_AVAILABILITY), "L08"),
    _s("reliability.delegation_budget", "P1", "委派深度与整棵调用树共享预算", "Delegation depth limits and tree-wide budgets",
       is_("multi_agent"), "O4"),
    _s("reliability.batch_partial_failure", "P1", "批任务的部分失败与重跑", "Partial failures and re-runs in batch jobs",
       is_("batch")),
    _s("reliability.voice_degradation", "P1", "语音链路的降级", "Graceful degradation of the voice pipeline",
       is_("voice")),
    _s("reliability.chaos_drills", "P2", "定期故障演练", "Regular failure drills",
       ge("availability_slo", HIGH_AVAILABILITY)),
    # ================================================================ 性能与延迟
    _g("latency.slo", "P1", "按交互形态定义延迟目标", "Latency targets per interaction mode", "L14"),
    _g("latency.breakdown", "P1", "把延迟分解到每一步", "Break latency down step by step", "L10"),
    _g("latency.fewer_serial_steps", "P1", "减少串行步骤和模型调用", "Cut serial steps and model calls"),
    _g("latency.aligned_timeouts", "P1", "各层超时对齐", "Align timeouts across layers", "P4"),
    _g("latency.tail_focus", "P1", "盯住 p95/p99，而不是平均值", "Watch p95/p99, not the average", "P4"),
    _g("latency.parallel_reads", "P2", "独立的只读工具并行执行", "Run independent read-only tools in parallel"),
    _g("latency.prompt_caching", "P2", "利用提示词缓存缩短首字时间", "Use prompt caching to cut time-to-first-token", "L14 B2"),
    _s("latency.voice_budget", "P0", "端到端语音延迟预算", "End-to-end voice latency budget",
       is_("voice")),
    _s("latency.first_response", "P1", "先快速给出第一个有用的响应", "Get a useful first response out fast",
       is_("consumer_facing")),
    _s("latency.queueing", "P1", "排队延迟与容量余量", "Queueing delay and capacity headroom",
       ge("peak_qps", HIGH_QPS), "L13"),
    _s("latency.hedging", "P2", "只对幂等只读请求做对冲", "Hedge only idempotent read requests",
       ge("peak_qps", HIGH_QPS), "L14 D10"),
    _s("latency.prefill", "P1", "长输入的预填充时间", "Prefill time of long inputs",
       ge("context_tokens", LONG_CONTEXT_TOKENS)),
    _s("latency.critical_path", "P1", "多 Agent 的关键路径", "The critical path through multiple agents",
       is_("multi_agent"), "L06"),
    _s("latency.cross_region", "P2", "跨区域调用的网络往返", "Network round-trips across regions",
       is_("cross_border")),
    _s("latency.inference_tuning", "P1", "自部署推理的吞吐与延迟权衡", "Throughput vs latency in self-hosted inference",
       is_("self_hosted_model")),
    # ================================================================ 安全
    _g("security.threat_model", "P0", "威胁建模：列出所有不可信来源", "Threat-model every untrusted source", "L09 S2"),
    _g("security.assume_fooled", "P0", "按'模型一定会被骗'来设计", "Design as if the model will be fooled", "L09 S5"),
    _g("security.secrets", "P0", "密钥不进代码、提示词和上下文", "Keep secrets out of code, prompts and context", "S7"),
    _g("security.output_scanning", "P0", "最终输出做敏感信息检测", "Scan final output for sensitive data", "S7"),
    _g("security.input_screening", "P1", "输入层检测与长度上限", "Input screening and length limits", "S1"),
    _g("security.kill_switch", "P1", "分钟级的紧急开关", "A kill switch that works in minutes", "L16"),
    _g("security.owasp_review", "P2", "对照 OWASP 两份 Top 10 复查", "Review against both OWASP Top 10 lists", "L09"),
    _s("security.lethal_trifecta", "P0", "切断致命三要素之一", "Break the lethal trifecta",
       LETHAL_TRIFECTA, "L09 S3"),
    _s("security.outbound_controls", "P0", "对外发送的目的地白名单与确认", "Allowlisted destinations and confirmation for outbound sends",
       is_("can_send_external"), "S3"),
    _s("security.side_effects_after_untrusted", "P0", "读到不可信内容后，副作用要受限", "Restrict side effects after reading untrusted content",
       is_("reads_untrusted_content"), "S2"),
    _s("security.render_allowlist", "P0", "前端不渲染任意外部图片和链接", "No arbitrary external images or links in the UI",
       all_(is_("accesses_private_data"), is_("reads_untrusted_content")), "S3"),
    _s("security.code_egress", "P0", "代码执行环境的网络出口控制", "Network egress control for code execution",
       is_("executes_code"), "L09"),
    _s("security.scoped_credentials", "P1", "第三方工具的凭证最小作用域", "Least-scope credentials for third-party tools",
       is_("third_party_tools"), "S6"),
    _s("security.memory_poisoning", "P1", "防记忆投毒", "Defend against memory poisoning",
       is_("long_term_memory"), "C6"),
    _s("security.abuse", "P1", "滥用、刷量与越狱传播", "Abuse, bots and jailbreak sharing",
       is_("consumer_facing")),
    _s("security.red_team", "P1", "持续红队测试", "Continuous red-teaming",
       any_(is_("external_users"), is_("reads_untrusted_content")), "L09"),
    # ================================================================ 隐私与合规
    _g("privacy.data_flow_map", "P0", "画数据流图（含模型供应商）", "Map data flows, including the model vendor"),
    _g("privacy.vendor_data_terms", "P0", "确认模型供应商的数据条款", "Confirm the model vendor's data terms"),
    _g("privacy.data_classification", "P1", "数据分级：哪类数据能发给哪个模型", "Classify data: which data may go to which model"),
    _g("privacy.minimization", "P1", "数据最小化", "Data minimization"),
    _g("privacy.retention", "P1", "所有存储都有保留期并自动清理", "Retention limits and auto-deletion everywhere"),
    _g("privacy.legal_review", "P1", "让法务确认适用的法规与责任边界", "Have legal confirm applicable rules and liability"),
    _s("privacy.telemetry_pii", "P0", "日志、trace、评估集里的个人信息", "Personal data in logs, traces and eval sets",
       is_("handles_pii"), "L10 S7"),
    _s("privacy.subject_requests", "P1", "查阅、删除请求覆盖所有副本", "Access and deletion requests reach every copy",
       is_("handles_pii")),
    _s("privacy.sensitive_pi", "P0", "敏感个人信息：单独同意与影响评估", "Sensitive personal data: separate consent and impact assessment",
       is_("sensitive_data")),
    _s("privacy.minors_data", "P0", "未成年人个人信息", "Children's personal data",
       is_("minors")),
    _s("privacy.cross_border_transfer", "P0", "跨境传输的合规路径", "A lawful route for cross-border transfers",
       is_("cross_border")),
    _s("privacy.automated_decisions", "P0", "自动化决策的透明度与拒绝权", "Transparency and opt-out for automated decisions",
       is_("automated_decisions")),
    _s("privacy.eu_high_risk", "P1", "判断是否属于欧盟 AI 法的高风险类别", "Check whether it is 'high-risk' under the EU AI Act",
       is_("automated_decisions")),
    _s("privacy.ai_disclosure", "P1", "告知 AI 身份与生成内容标识", "AI disclosure and labeling of generated content",
       is_("external_users")),
    _s("privacy.genai_filing", "P1", "面向中国境内公众时的安全评估与备案", "Security assessment and filing for public services in China",
       is_("consumer_facing")),
    _s("privacy.sector_rules", "P0", "行业规则（如 HIPAA 的 BAA）", "Sector rules (e.g. a HIPAA BAA)",
       all_(is_("regulated_industry"), is_("sensitive_data"))),
    # ================================================================ 权限与审计
    _g("access.on_behalf_of_user", "P0", "以发起用户的身份与权限行事", "Act with the requesting user's identity and permissions", "L15 S4"),
    _g("access.least_privilege", "P0", "按角色最小权限：不展示，也不放行", "Least privilege by role: hidden and blocked", "L09 S5"),
    _g("access.enforced_in_code", "P0", "授权在代码里执行，不靠提示词", "Authorization enforced in code, not prompts", "M6"),
    _g("access.argument_level", "P1", "参数级授权", "Argument-level authorization", "L09"),
    _g("access.audit_trail", "P0", "审计：谁、何时、以谁的身份、做了什么、谁批准", "Audit: who, when, as whom, what, approved by whom", "L09 P5"),
    _g("access.audit_integrity", "P1", "审计日志防篡改、不采样", "Tamper-evident, unsampled audit logs", "P5"),
    _g("access.access_reviews", "P2", "定期复查权限", "Periodic access reviews"),
    _s("access.delegation_intersection", "P0", "委派链上的权限取交集", "Intersect permissions along the delegation chain",
       is_("multi_agent"), "S8"),
    _s("access.service_identity", "P1", "无人值守任务的服务身份", "Service identities for unattended jobs",
       is_("batch")),
    _s("access.scoped_tokens", "P1", "下游调用用短期、限定作用域的令牌", "Short-lived, narrowly scoped tokens downstream",
       is_("third_party_tools")),
    _s("access.regulatory_audit", "P0", "满足监管要求的留存与举证", "Regulator-grade retention and evidence",
       is_("regulated_industry")),
    _s("access.no_ambient_credentials", "P0", "执行环境里没有'顺手可用'的凭证", "No ambient credentials in the execution environment",
       is_("executes_code")),
    _s("access.sensitive_access_log", "P1", "敏感数据的每次访问都留痕", "Log every access to sensitive data",
       is_("sensitive_data")),
    _s("access.break_glass", "P2", "紧急提权流程与事后复核", "Break-glass access with after-the-fact review",
       is_("irreversible_actions")),
    # ================================================================ 伦理与内容安全
    _g("safety.content_policy", "P1", "写下内容政策", "Write down a content policy"),
    _g("safety.grounded_commitments", "P1", "承诺类输出必须有依据", "Commitments must be grounded", "M5"),
    _g("safety.no_pretending", "P1", "不假装：不编造行动，不冒充人类", "No pretending: no phantom actions, no posing as human", "M1"),
    _g("safety.high_risk_topics", "P1", "高风险话题的处置流程", "A protocol for high-risk topics"),
    _g("safety.harm_reporting", "P2", "有害输出的上报与处置", "A path to report and handle harmful output"),
    _g("safety.bias_review", "P2", "检查输出里的刻板印象与歧视性表述", "Check output for stereotypes and discriminatory wording"),
    _s("safety.public_moderation", "P0", "输入输出双向内容审核", "Two-way moderation for public-facing output",
       is_("consumer_facing")),
    _s("safety.minors_protection", "P0", "未成年人保护", "Protections for minors",
       is_("minors")),
    _s("safety.fairness", "P0", "公平性与偏差测试", "Fairness and bias testing",
       is_("automated_decisions")),
    _s("safety.professional_advice", "P0", "专业建议的边界", "Limits on professional advice",
       is_("regulated_industry")),
    _s("safety.published_content", "P1", "以公司名义对外发出的内容", "Content sent out in the company's name",
       is_("can_send_external")),
    _s("safety.sycophancy", "P1", "用户施压时守住规则", "Hold the line under user pressure",
       is_("external_users"), "M6"),
    _s("safety.multilingual_safety", "P1", "各语言的安全能力不一致", "Safety quality differs by language",
       is_("multilingual")),
    _s("safety.synthetic_voice", "P1", "合成语音的披露与防冒充", "Disclose synthetic voices; prevent impersonation",
       is_("voice")),
    # ================================================================ 成本
    _g("cost.estimate", "P1", "上线前做成本估算", "Estimate cost before launch", "L14"),
    _g("cost.run_budget", "P0", "单次运行的 token 与金额预算", "Per-run token and spend budgets", "L08 B1"),
    _g("cost.quotas", "P0", "用户、租户和每日配额", "Per-user, per-tenant and daily quotas", "B1 P3"),
    _g("cost.attribution", "P1", "成本可归因", "Attributable cost", "L14 B3"),
    _g("cost.anomaly_alerts", "P1", "成本异常告警", "Cost anomaly alerts", "L10"),
    _g("cost.cache_hit_rate", "P1", "监控提示词缓存命中率", "Track the prompt-cache hit rate", "B2"),
    _g("cost.hidden_costs", "P2", "算上评估、存储和人工的隐性成本", "Count hidden costs: evals, storage, human review"),
    _s("cost.routing", "P1", "模型路由与级联", "Model routing and cascades",
       ge("daily_runs", MANY_RUNS_PER_DAY), "L14 B4"),
    _s("cost.response_cache", "P1", "精确缓存与语义缓存", "Exact and semantic caching",
       ge("daily_runs", MANY_RUNS_PER_DAY), "L14 D5"),
    _s("cost.batch_api", "P1", "使用批处理接口", "Use batch APIs",
       is_("batch"), "L14"),
    _s("cost.multi_agent_multiplier", "P1", "多 Agent 的 token 倍增", "The multi-agent token multiplier",
       is_("multi_agent"), "L06"),
    _s("cost.long_context", "P1", "长上下文的成本", "The cost of long contexts",
       ge("context_tokens", LONG_CONTEXT_TOKENS), "L14"),
    _s("cost.tenant_metering", "P1", "按租户计量与定价", "Per-tenant metering and pricing",
       is_("multi_tenant"), "B3"),
    _s("cost.denial_of_wallet", "P1", "防'刷钱'攻击", "Defend against denial-of-wallet",
       is_("consumer_facing"), "B1"),
    _s("cost.self_hosting_tco", "P2", "自部署的总拥有成本", "Total cost of self-hosting",
       is_("self_hosted_model")),
    _s("cost.audio_minutes", "P2", "按通话时长建成本模型", "Model cost per call minute",
       is_("voice")),
    # ================================================================ 可观测性
    _g("observability.full_trace", "P0", "每次运行都有完整 trace", "A full trace for every run", "L10 P2"),
    _g("observability.status_metrics", "P0", "运行状态与结束原因是一级指标", "Run status and stop reasons as first-class metrics", "P1"),
    _g("observability.version_stamps", "P1", "记录模型、提示词、工具版本", "Stamp model, prompt and tool versions", "E5"),
    _g("observability.trace_id_surface", "P1", "trace ID 返回给前端与客服", "Surface trace IDs to the UI and support", "P2"),
    _g("observability.alerts", "P1", "关键信号有告警", "Alerts on key signals", "L10"),
    _g("observability.otel_conventions", "P2", "遵循 OpenTelemetry GenAI 约定", "Follow the OpenTelemetry GenAI conventions", "L10"),
    _g("observability.business_dashboard", "P2", "业务级看板", "A business-level dashboard", "P1"),
    _s("observability.sampling", "P1", "采样策略：调试可采样，审计不可", "Sampling: sample debug traces, never audit logs",
       ge("daily_runs", MANY_RUNS_PER_DAY), "L10"),
    _s("observability.cross_agent_propagation", "P1", "跨 Agent、跨服务传播 trace 上下文", "Propagate trace context across agents and services",
       is_("multi_agent"), "L10"),
    _s("observability.tenant_views", "P1", "按租户的指标与看板", "Per-tenant metrics and dashboards",
       is_("multi_tenant")),
    _s("observability.heartbeats", "P1", "长任务的心跳与进度指标", "Heartbeats and progress metrics for long runs",
       LONG_TASK),
    _s("observability.voice_metrics", "P1", "语音专属指标", "Voice-specific metrics",
       is_("voice")),
    _s("observability.security_signals", "P1", "安全信号：注入命中与异常调用", "Security signals: injection hits and unusual calls",
       is_("reads_untrusted_content"), "L09"),
    _s("observability.batch_metrics", "P2", "批任务的完成率、积压与截止", "Batch completion, backlog and deadlines",
       is_("batch"), "D4"),
    # ================================================================ 发布与运维
    _g("ops.release_unit", "P0", "代码、提示词、模型、工具、配置作为一个发布单元", "Code, prompts, model, tools and config ship as one unit", "L16 D11"),
    _g("ops.canary_rollback", "P0", "灰度发布与快速回滚", "Canary releases and fast rollback", "L16"),
    _g("ops.runbooks", "P1", "运行手册", "Runbooks"),
    _g("ops.on_call", "P1", "值班与升级路径", "On-call and escalation"),
    _g("ops.auto_rollback", "P1", "基于指标自动回滚", "Metric-based automatic rollback", "L16"),
    _g("ops.postmortems", "P1", "无责复盘并回流评估集", "Blameless postmortems that feed evals", "L16"),
    _g("ops.dependency_health", "P2", "监控外部依赖健康", "Monitor dependency health"),
    _s("ops.long_run_deploys", "P1", "有长任务时的发布策略", "Deploying while long runs are in flight",
       LONG_TASK, "R6"),
    _s("ops.tenant_rollouts", "P1", "按租户灰度与变更通知", "Tenant-aware rollouts and change notices",
       is_("multi_tenant"), "D6"),
    _s("ops.change_control", "P0", "正式的变更管理与留痕", "Formal change control with a paper trail",
       is_("regulated_industry")),
    _s("ops.shadow_mode", "P1", "先影子模式，再逐步放权", "Shadow mode before granting autonomy",
       is_("irreversible_actions"), "L16"),
    _s("ops.provider_outage", "P1", "模型供应商故障预案", "A playbook for provider outages",
       ge("availability_slo", HIGH_AVAILABILITY), "L08"),
    _s("ops.model_deprecation", "P1", "模型弃用与迁移计划", "A plan for model deprecation and migration",
       not_(is_("self_hosted_model"))),
    _s("ops.data_incident", "P0", "数据泄露事件响应", "Data-breach incident response",
       is_("handles_pii")),
    # ================================================================ 可扩展性与并发
    _g("scale.capacity_estimate", "P1", "估算峰值并发与模型调用量", "Estimate peak concurrency and model calls", "L12"),
    _g("scale.dependency_limits", "P1", "摸清所有依赖的容量上限", "Know the capacity limits of every dependency"),
    _g("scale.stateless_workers", "P1", "worker 无状态，状态外置", "Stateless workers with externalized state", "L12 L13"),
    _g("scale.session_serialization", "P1", "同一会话的并发写要有控制", "Control concurrent writes to the same session", "L13 D1"),
    _g("scale.backpressure", "P1", "背压与准入控制", "Backpressure and admission control", "D4"),
    _g("scale.load_tests", "P2", "压测，包括模型限流场景", "Load tests, including provider throttling"),
    _s("scale.quota_exceeded", "P0", "峰值模型调用超过账号限额", "Peak model calls exceed the account limit",
       all_(gt("provider_rpm_limit", 0), gt("peak_llm_rpm", ref("provider_rpm_limit"))), "L13 D9"),
    _s("scale.quota_unknown", "P1", "模型配额还不清楚：先拿到书面数字", "Model quota unknown: get the number in writing",
       eq("provider_rpm_limit", 0)),
    _s("scale.global_rate_limit", "P0", "多实例下的全局限流", "Global rate limiting across instances",
       any_(ge("peak_qps", SCALE_OUT_QPS), ge("availability_slo", HIGH_AVAILABILITY)), "L13 D9"),
    _s("scale.delivery_semantics", "P0", "至少一次投递与消费端幂等", "At-least-once delivery with idempotent consumers",
       QUEUE_BASED, "L13 D3"),
    _s("scale.leases_fencing", "P1", "租约、心跳与 fencing token", "Leases, heartbeats and fencing tokens",
       QUEUE_BASED, "L13 D2"),
    _s("scale.traffic_isolation", "P1", "批处理与交互流量分开", "Separate batch from interactive traffic",
       is_("batch"), "P3"),
    _s("scale.stampede", "P2", "请求合并与缓存过期抖动", "Request coalescing and jittered expiry",
       ge("peak_qps", HIGH_QPS), "D8"),
    _s("scale.outbox", "P1", "写库加发事件用事务性发件箱", "Transactional outbox for write-then-publish",
       all_(is_("writes_data"), any_(is_("batch"), ge("peak_qps", SCALE_OUT_QPS))), "L13 D7"),
    # ================================================================ 多租户
    _g("tenancy.decide_early", "P1", "尽早确定租户模型", "Decide the tenancy model early", "L12"),
    _g("tenancy.per_user_isolation", "P0", "用户级数据隔离", "Per-user data isolation", "C5"),
    _g("tenancy.scoped_keys", "P1", "存储键和缓存键都带归属范围", "Scope every storage and cache key", "D5"),
    _g("tenancy.isolation_tests", "P1", "自动化越权测试", "Automated cross-boundary tests"),
    _g("tenancy.layered_config", "P2", "配置分层", "Layered configuration"),
    _g("tenancy.scope_deletion", "P2", "能按范围整体删除数据", "Delete everything for a given scope"),
    _s("tenancy.tenant_from_auth", "P0", "租户 ID 来自认证并贯穿全链路", "Tenant ID from auth, carried end to end",
       is_("multi_tenant"), "L12 C5"),
    _s("tenancy.isolation_model", "P0", "选择隔离模式：silo、pool 或 bridge", "Choose an isolation model: silo, pool or bridge",
       is_("multi_tenant"), "L12"),
    _s("tenancy.noisy_neighbor", "P0", "租户级限流、配额与公平调度", "Per-tenant limits, quotas and fair scheduling",
       is_("multi_tenant"), "P3"),
    _s("tenancy.retrieval_isolation", "P0", "索引与记忆按租户隔离", "Per-tenant isolation of indexes and memory",
       all_(is_("multi_tenant"), any_(is_("uses_rag"), is_("long_term_memory"))), "L15 C5"),
    _s("tenancy.tenant_config", "P1", "租户级配置受控、可审计", "Controlled, auditable tenant configuration",
       is_("multi_tenant")),
    _s("tenancy.tenant_offboarding", "P1", "租户注销时完整删除", "Complete deletion when a tenant leaves",
       is_("multi_tenant")),
    _s("tenancy.dedicated_option", "P1", "为高敏感租户提供独立部署", "Dedicated deployments for sensitive tenants",
       all_(is_("multi_tenant"), is_("regulated_industry")), "L12"),
    _s("tenancy.tenant_residency", "P1", "按租户的数据驻留", "Per-tenant data residency",
       all_(is_("multi_tenant"), is_("cross_border"))),
    _s("tenancy.tenant_keys", "P2", "租户级加密密钥", "Per-tenant encryption keys",
       all_(is_("multi_tenant"), is_("sensitive_data"))),
    # ================================================================ 团队与交接
    _g("ownership.named_owners", "P1", "每个部件都有明确的 owner", "A named owner for every part"),
    _g("ownership.architecture_docs", "P0", "架构图与数据流图，标出信任边界", "Architecture and data-flow diagrams with trust boundaries"),
    _g("ownership.adrs", "P1", "用 ADR 记录关键决策", "Record key decisions as ADRs"),
    _g("ownership.eval_playbook", "P1", "评估集的标注规范与扩充流程", "Document how to label and extend the eval set", "L11"),
    _g("ownership.oncall_training", "P1", "值班与客服接受培训", "Train on-call and support staff"),
    _g("ownership.known_limitations", "P2", "维护已知限制清单", "Maintain a known-limitations list"),
    _g("ownership.user_docs", "P2", "面向用户的说明", "User-facing documentation"),
    _s("ownership.interface_contracts", "P1", "跨团队的接口契约", "Interface contracts between teams",
       ge("teams_involved", MANY_TEAMS)),
    _s("ownership.platform_split", "P2", "平台团队与业务团队的分工", "Platform vs product team responsibilities",
       ge("teams_involved", MANY_TEAMS), "L12"),
    _s("ownership.content_owners", "P1", "知识库内容要有 owner 和更新流程", "Owners and an update process for knowledge content",
       is_("uses_rag"), "L15"),
    _s("ownership.compliance_owner", "P0", "合规负责人与模型风险文档", "A compliance owner and model-risk documentation",
       is_("regulated_industry")),
    _s("ownership.customer_support", "P1", "客户支持分层与租户沟通", "Tiered customer support and tenant communication",
       is_("multi_tenant")),
    _s("ownership.approver_training", "P1", "审批人培训", "Train the approvers",
       is_("irreversible_actions")),
    _s("ownership.external_comms", "P1", "对外沟通口径", "An external communications plan",
       is_("external_users")),
    # ================================================================ 供应商依赖与退出
    _g("vendor.gateway_abstraction", "P1", "业务代码只认逻辑模型名", "Application code knows only logical model names", "L12"),
    _g("vendor.own_your_assets", "P1", "提示词、评估集、trace 数据掌握在自己手里", "Keep prompts, eval sets and traces in your hands"),
    _g("vendor.switch_by_evals", "P1", "换供应商靠评估集，不靠感觉", "Switch vendors by eval results, not by feel", "L11"),
    _g("vendor.contract_terms", "P1", "合同条款：SLA、配额、弃用通知、价格", "Contract terms: SLA, quota, deprecation notice, price"),
    _g("vendor.lockin_inventory", "P2", "列出专有能力依赖", "Inventory proprietary dependencies"),
    _g("vendor.exit_plan", "P2", "写一页退出方案", "Write a one-page exit plan"),
    _s("vendor.second_source", "P1", "准备经过评估的第二供应商", "An evaluated second source",
       ge("availability_slo", HIGH_AVAILABILITY), "R3"),
    _s("vendor.framework_lockin", "P2", "编排框架与托管平台的锁定", "Lock-in to orchestration frameworks and managed platforms",
       is_("multi_agent"), "L12"),
    _s("vendor.tool_vendors", "P1", "第三方工具供应商的可用性", "Availability of third-party tool vendors",
       is_("third_party_tools")),
    _s("vendor.regional_availability", "P1", "目标地区能用哪些模型", "Which models are available in your regions",
       is_("cross_border")),
    _s("vendor.weights_license", "P1", "开源权重的许可条款", "License terms of open weights",
       is_("self_hosted_model")),
    _s("vendor.third_party_risk", "P0", "第三方（外包）风险管理", "Third-party (outsourcing) risk management",
       is_("regulated_industry")),
    _s("vendor.price_sensitivity", "P2", "价格变化的敏感性分析", "Sensitivity to price changes",
       ge("daily_runs", MANY_RUNS_PER_DAY)),
)

CATALOG_BY_ID: dict[str, Consideration] = {c.id: c for c in CATALOG}


def items_of(dimension: str, kind: str | None = None) -> list[Consideration]:
    return [c for c in CATALOG if c.dimension == dimension and (kind is None or c.kind == kind)]


def dimension_requirements(lang: str = "zh") -> list[Requirement]:
    """把 20 个维度变成 coverage_report 的检查项：标题 = 维度名，关键词 = 维度关键词。"""
    return [Requirement(d.id, d.zh if lang == "zh" else d.en, d.keywords) for d in DIMENSIONS]


# =====================================================================
# 场景权重（练习 b）
# =====================================================================

FOCUS_RULES: tuple[FocusRule, ...] = (
    FocusRule(is_("external_users"), {"ux": 1, "safety": 1, "security": 1}),
    FocusRule(is_("consumer_facing"), {"safety": 2, "ux": 1, "latency": 1, "scale": 1}),
    FocusRule(is_("minors"), {"safety": 2, "privacy": 1}),
    FocusRule(is_("handles_pii"), {"privacy": 2, "observability": 1}),
    FocusRule(is_("sensitive_data"), {"privacy": 2, "access": 1}),
    FocusRule(is_("regulated_industry"), {"access": 2, "privacy": 1, "evals": 1, "human": 1, "ops": 1, "vendor": 1}),
    FocusRule(is_("automated_decisions"), {"human": 2, "safety": 1, "privacy": 1}),
    FocusRule(is_("cross_border"), {"privacy": 2, "vendor": 1}),
    FocusRule(is_("accesses_private_data"), {"access": 1, "context": 1}),
    FocusRule(LETHAL_TRIFECTA, {"security": 3}),
    FocusRule(is_("reads_untrusted_content"), {"security": 1}),
    FocusRule(is_("can_send_external"), {"security": 1}),
    FocusRule(is_("writes_data"), {"tools": 1, "reliability": 1}),
    FocusRule(is_("irreversible_actions"), {"human": 2, "access": 1, "tools": 1}),
    FocusRule(is_("executes_code"), {"security": 2, "tools": 1, "access": 1}),
    FocusRule(is_("third_party_tools"), {"security": 1, "vendor": 1}),
    FocusRule(is_("uses_rag"), {"context": 2}),
    FocusRule(is_("long_term_memory"), {"context": 1, "privacy": 1}),
    FocusRule(is_("multi_agent"), {"observability": 1, "cost": 1, "reliability": 1}),
    FocusRule(is_("multi_tenant"), {"tenancy": 3, "cost": 1}),
    FocusRule(is_("batch"), {"cost": 1, "reliability": 1}),
    FocusRule(is_("voice"), {"latency": 3, "ux": 2}),
    FocusRule(is_("multilingual"), {"evals": 1}),
    FocusRule(is_("self_hosted_model"), {"model": 1, "ops": 1}),
    FocusRule(ge("peak_qps", HIGH_QPS), {"scale": 2, "cost": 1, "latency": 1}),
    FocusRule(ge("daily_runs", MANY_RUNS_PER_DAY), {"cost": 2}),
    FocusRule(LONG_TASK, {"reliability": 2, "ux": 1}),
    FocusRule(ge("context_tokens", LONG_CONTEXT_TOKENS), {"context": 1, "cost": 1}),
    FocusRule(ge("availability_slo", HIGH_AVAILABILITY), {"reliability": 1, "ops": 1, "vendor": 1}),
    FocusRule(ge("tool_count", MANY_TOOLS), {"tools": 1}),
    FocusRule(ge("teams_involved", MANY_TEAMS), {"ownership": 2}),
)


# =====================================================================
# 场景画像（README 第 3 节）
# =====================================================================

BASE_PROFILE: dict[str, Any] = {
    **{f.name: False for f in FACTS if f.kind == "bool"},
    "peak_qps": 1,
    "llm_calls_per_request": 3,
    "provider_rpm_limit": 1000,
    "p95_task_seconds": 10,
    "daily_runs": 200,
    "context_tokens": 8000,
    "tool_count": 5,
    "availability_slo": 99.0,
    "teams_involved": 1,
}


def profile(**overrides: Any) -> dict[str, Any]:
    """在 BASE_PROFILE 上覆盖若干事实，得到一个完整画像。拼错事实名会直接报错。"""
    unknown = set(overrides) - {f.name for f in FACTS}
    if unknown:
        raise UnknownFactError(sorted(unknown)[0])
    return {**BASE_PROFILE, **overrides}


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "internal_tool", "内部低频工具", "Internal low-traffic tool", "内部工具", "Internal",
        profile(accesses_private_data=True, uses_rag=True, daily_runs=300, tool_count=4),
        focus=("goals", "context", "access", "evals"),
        defer=("latency", "cost", "scale", "tenancy", "vendor"),
    ),
    Scenario(
        "consumer_assistant", "面向 C 端的高并发助手", "High-traffic consumer assistant", "C 端助手", "Consumer",
        profile(external_users=True, consumer_facing=True, minors=True, handles_pii=True, accesses_private_data=True,
                reads_untrusted_content=True, writes_data=True, uses_rag=True, peak_qps=300,
                provider_rpm_limit=30000, p95_task_seconds=8, daily_runs=500_000, tool_count=12,
                availability_slo=99.9, teams_involved=4),
        focus=("safety", "ux", "latency", "cost", "scale", "security"),
        defer=("tenancy",),
    ),
    Scenario(
        "regulated", "强监管行业（金融/医疗）", "Heavily regulated (finance/health)", "强监管", "Regulated",
        profile(external_users=True, consumer_facing=True, handles_pii=True, sensitive_data=True,
                regulated_industry=True, automated_decisions=True, accesses_private_data=True, writes_data=True,
                irreversible_actions=True, uses_rag=True, peak_qps=10, provider_rpm_limit=20000,
                p95_task_seconds=15, daily_runs=8000, tool_count=10, availability_slo=99.95, teams_involved=5),
        focus=("privacy", "access", "human", "evals", "safety", "ops"),
        defer=("latency", "cost", "scale"),
    ),
    Scenario(
        "saas", "多租户 SaaS", "Multi-tenant SaaS", "多租户 SaaS", "SaaS",
        profile(external_users=True, handles_pii=True, accesses_private_data=True, reads_untrusted_content=True,
                writes_data=True, third_party_tools=True, uses_rag=True, multi_tenant=True, peak_qps=80,
                llm_calls_per_request=4, provider_rpm_limit=30000, p95_task_seconds=12, daily_runs=150_000,
                context_tokens=12000, tool_count=15, availability_slo=99.9, teams_involved=4),
        focus=("tenancy", "security", "access", "cost", "observability"),
        defer=("latency", "safety"),
    ),
    Scenario(
        "batch", "离线批处理", "Offline batch processing", "批处理", "Batch",
        profile(batch=True, accesses_private_data=True, handles_pii=True, reads_untrusted_content=True,
                writes_data=True, peak_qps=20, llm_calls_per_request=2, provider_rpm_limit=5000,
                p95_task_seconds=45, daily_runs=50_000, context_tokens=60_000, tool_count=4, teams_involved=2),
        focus=("cost", "reliability", "evals", "scale", "context"),
        defer=("ux", "latency", "safety"),
    ),
    Scenario(
        "voice", "实时语音", "Real-time voice", "实时语音", "Voice",
        profile(voice=True, external_users=True, consumer_facing=True, handles_pii=True, accesses_private_data=True,
                writes_data=True, multilingual=True, peak_qps=30, llm_calls_per_request=2, provider_rpm_limit=10000,
                p95_task_seconds=3, daily_runs=40_000, context_tokens=4000, tool_count=6, availability_slo=99.9,
                teams_involved=3),
        focus=("latency", "ux", "reliability", "safety", "evals"),
        defer=("tenancy", "context"),
    ),
    Scenario(
        "coding_ops", "编码/运维类高权限 Agent", "High-privilege coding / ops agent", "编码运维", "Coding/Ops",
        profile(executes_code=True, writes_data=True, irreversible_actions=True, accesses_private_data=True,
                reads_untrusted_content=True, can_send_external=True, third_party_tools=True, peak_qps=2,
                llm_calls_per_request=20, provider_rpm_limit=2000, p95_task_seconds=900, daily_runs=2000,
                context_tokens=120_000, tool_count=30, teams_involved=2),
        focus=("security", "access", "human", "tools", "reliability"),
        defer=("latency", "tenancy", "safety"),
    ),
    Scenario(
        "research", "研究类长任务 Agent", "Long-running research agent", "研究长任务", "Research",
        profile(multi_agent=True, reads_untrusted_content=True, accesses_private_data=True, can_send_external=True,
                uses_rag=True, long_term_memory=True, peak_qps=1, llm_calls_per_request=60, provider_rpm_limit=2000,
                p95_task_seconds=7200, daily_runs=300, context_tokens=150_000, tool_count=12),
        focus=("context", "reliability", "cost", "evals", "observability", "security"),
        defer=("latency", "scale", "tenancy"),
    ),
    Scenario(
        "internal_workflow", "内部办理类（如 IT 服务台）", "Internal workflow (e.g. IT help desk)", "内部办理", "Workflow",
        profile(accesses_private_data=True, handles_pii=True, reads_untrusted_content=True, writes_data=True,
                irreversible_actions=True, uses_rag=True, multi_tenant=True, peak_qps=5, provider_rpm_limit=1000,
                p95_task_seconds=12, daily_runs=3000, tool_count=6, availability_slo=99.5, teams_involved=3),
        focus=("access", "human", "security", "tools", "tenancy", "evals"),
        defer=("latency", "scale"),
    ),
)

SCENARIO_BY_ID: dict[str, Scenario] = {s.id: s for s in SCENARIOS}


# =====================================================================
# 第 4 节的完整小例子：B2B SaaS 里的"销售邮件助理"
# =====================================================================

EXAMPLE_REQUIREMENT_ZH = """\
我们是一家做 B2B 销售管理 SaaS 的公司，想在产品里加一个"销售邮件助理"。
它会读取销售邮箱里收到的来信（来自客户的客户），结合 CRM 里的联系人、商机和历史沟通记录，起草回复；
销售点一下"发送"，就从销售本人的邮箱发出去。也能按销售的指令，给一组联系人批量发跟进邮件。
目前有 300 家企业客户，最大的客户有 2000 个销售账号，每天大约 20 万次请求，高峰每秒 50 个请求。
每个请求平均调用模型 4 次，典型任务 10 秒内完成，一次要带上邮件线程和 CRM 记录，大约 1.5 万 token；
Agent 大约有 8 个工具。我们的模型账号每分钟限额 5000 次请求。
邮件里经常有联系人的姓名、电话、职位等个人信息。客户主要在中国和欧洲，模型服务部署在美国。
我们对客户承诺 99.9% 的可用性。项目涉及产品、平台、安全、法务四个团队。
"""

EXAMPLE_REQUIREMENT_EN = """\
We sell a B2B sales-management SaaS and want to add a "sales email assistant" to the product.
It reads the emails that arrive in a sales rep's inbox (sent by our customers' customers), combines them with
contacts, deals and past conversations from the CRM, and drafts a reply; when the rep clicks "Send", it goes out
from the rep's own mailbox. It can also send follow-up emails to a group of contacts on the rep's instruction.
We have 300 business customers; the largest has 2,000 sales seats. About 200,000 requests a day, peaking at
50 requests per second. Each request calls the model 4 times on average, a typical task finishes within
10 seconds, and each call carries the email thread plus CRM records, roughly 15,000 tokens.
The agent has about 8 tools. Our model account is limited to 5,000 requests per minute.
Emails routinely contain contacts' names, phone numbers and job titles. Customers are mainly in China and Europe;
the model service is hosted in the US. We promise customers 99.9% availability.
Four teams are involved: product, platform, security and legal.
"""

# 人工标注的画像（demo --offline 用它；真实模式下与模型抽取的画像做对比）
EXAMPLE_PROFILE: dict[str, Any] = profile(
    external_users=True,  # 用户是我们的企业客户（公司外部）
    consumer_facing=False,  # 用户是企业里的销售，不是普通消费者
    handles_pii=True,
    cross_border=True,  # 中国、欧洲的数据发往美国的模型服务
    accesses_private_data=True,  # CRM、邮箱
    reads_untrusted_content=True,  # 任何人都能给销售发邮件
    can_send_external=True,  # 能发邮件
    writes_data=True,
    irreversible_actions=True,  # 发出去的邮件收不回来
    uses_rag=True,  # 检索历史沟通记录
    multi_tenant=True,
    multilingual=True,  # 中文、英文以及欧洲各国语言
    peak_qps=50,
    llm_calls_per_request=4,
    provider_rpm_limit=5000,
    p95_task_seconds=10,
    daily_runs=200_000,
    context_tokens=15_000,
    tool_count=8,
    availability_slo=99.9,
    teams_involved=4,
)


# =====================================================================
# 课程链接（最终编号）
# =====================================================================

LESSONS: dict[str, tuple[str, str, str]] = {
    "00": ("00_overview", "全景图", "The big picture"),
    "01": ("01_llm_essentials", "LLM 与 Agent 开发必备知识", "LLM essentials for agent developers"),
    "02": ("02_agent_loop", "Agent 循环", "The agent loop"),
    "03": ("03_tools", "工具设计", "Tool design"),
    "04": ("04_context_memory", "上下文与记忆", "Context & memory"),
    "05": ("05_agent_architectures", "常见 Agent 架构", "Common agent architectures"),
    "06": ("06_orchestration", "编排模式", "Orchestration patterns"),
    "07": ("07_engineering_perspectives", "工程考量全景", "Engineering perspectives"),
    "08": ("08_reliability", "可靠性", "Reliability"),
    "09": ("09_security", "安全", "Security"),
    "10": ("10_observability", "可观测性", "Observability"),
    "11": ("11_evals", "评估", "Evals"),
    "12": ("12_production_architecture", "生产架构", "Production architecture"),
    "13": ("13_distributed_concurrency", "高并发与分布式", "Concurrency & distribution"),
    "14": ("14_cost_latency", "成本与延迟", "Cost & latency"),
    "15": ("15_enterprise_rag", "权限感知 RAG", "Permission-aware RAG"),
    "16": ("16_release_ops", "发布运维", "Release & operations"),
}


def describe_ref(ref: str, lang: str = "zh") -> str:
    """把 refs 里的缩写翻译成人话：'L09' → '第 09 课'，'S3' → '失败模式 S3'。"""
    if ref.startswith("L") and ref[1:] in LESSONS:
        return f"第 {ref[1:]} 课" if lang == "zh" else f"Lesson {ref[1:]}"
    return f"失败模式 {ref}" if lang == "zh" else f"failure mode {ref}"
