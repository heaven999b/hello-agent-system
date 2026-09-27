"""第 25 课：主动式 Agent 的最小工具箱（零外部依赖：只用标准库 + agentkit）。

被动式 Agent 等用户开口；主动式 Agent 自己发现"用户现在可能需要帮助"，然后决定**要不要开口、什么时候开口、说什么**。
本文件把这件事拆成四个零件：

  1. 事件流模拟器   simulate_day()        一天里的日历、邮件、工单、告警，以及"用户在专注 / 开会"这类状态事件
  2. 用户模型       UserModel             带置信度和证据的推断；贝叶斯式更新（update_belief）、用户纠正、遗忘、时间衰减
  3. 打扰决策器     should_interrupt()    期望收益 × 置信度 − 打扰成本；勿扰时段、专注状态、频率上限、紧急事件越权
  4. 建议卡片       make_card()           LLM + complete_json 生成结构化建议（只建议，不执行）

run_day() 把它们串起来跑完一天，并用一个"模拟用户"（知道每个事件的真实需求）给策略打分。

练习 (a)(b)(c) 要你重写的 update_belief / should_interrupt / forget / explain，这里都有完整版（demo 用它们）。
先别看，自己写一遍；卡住了再回来对照。
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Literal

from pydantic import BaseModel, Field

from agentkit.llm import LLM, LLMResponse, ScriptedLLM
from agentkit.workflows import complete_json

DAY = 24 * 60  # 一天的分钟数。时间统一用"从第一天 00:00 起的分钟数"表示，≥ 1440 就是第二天


def hhmm(minute: int) -> str:
    """分钟数 → "09:05"；第二天的时间加"次日"前缀。"""
    day, m = divmod(int(minute), DAY)
    return f"{'次日 ' if day else ''}{m // 60:02d}:{m % 60:02d}"


def at(text: str) -> int:
    """"09:05" → 545；"次日 08:30" → 1950。"""
    nxt = text.startswith("次日")
    h, m = text.replace("次日", "").strip().split(":")
    return (DAY if nxt else 0) + int(h) * 60 + int(m)


# =====================================================================
# 1. 事件流模拟器
# =====================================================================


@dataclass(frozen=True)
class Event:
    """Agent 能看到的一条事件。注意：这里**没有**"用户是否真的需要"的信息，那在 Truth 里。"""

    id: str
    t: int  # 分钟数
    source: str  # calendar / email / ticket / alert / activity
    kind: str  # 事件类型：决定用哪条用户模型推断来估计收益（见 KIND_PROFILE）；activity 的 kind 是状态变化
    title: str
    urgent: bool = False  # 由事件源标记（如监控系统的 SEV 级别），不是模型猜的


@dataclass(frozen=True)
class Truth:
    """模拟用户心里的真实需求 —— Agent 看不到，只用于打分和讲解。"""

    value: float  # 在截止时间前帮到用户，用户得到多少价值；0 = 用户根本不需要
    deadline: int | None = None  # 过了这个时刻再提醒就没用了
    need: str = ""


# 状态类事件：它们不需要建议，只改变"用户现在忙不忙"。其中一部分是"空档"，攒着的摘要在空档时推送。
ACTIVITY_KINDS = {"day_start", "focus_start", "focus_end", "meeting_start", "meeting_end", "lunch", "day_end"}
BREAK_KINDS = {"day_start", "focus_end", "meeting_end", "lunch", "day_end"}

# (id, 时间, 来源, 类型, 标题, 是否紧急, 真实价值, 截止时间, 真实需求)
_DAY_SCRIPT: list[tuple] = [
    ("e01", "07:40", "email", "fyi", "海外同事：昨晚 API 变更的说明（FYI）", False, 1, None, "上班后看一眼摘要就行"),
    ("a01", "08:30", "activity", "day_start", "到公司，打开电脑", False, 0, None, ""),
    ("e02", "08:35", "calendar", "meeting_prep", "10:00 支付服务架构评审（你是评审人）", False, 3, "10:00", "会前提醒读设计文档"),
    ("a02", "08:50", "activity", "focus_start", "打开 IDE，进入专注模式", False, 0, None, ""),
    ("e03", "09:05", "email", "manager_request", "主管：周五前把 Q3 延迟分析报告发我", False, 3, None, "记成待办"),
    ("e04", "09:15", "email", "newsletter", "技术周刊 #128", False, 0, None, ""),
    ("e05", "09:30", "alert", "staging_alert", "staging 磁盘使用率 85%", False, 0, None, "运维值班会处理，不需要你"),
    ("a03", "09:55", "activity", "focus_end", "专注结束", False, 0, None, ""),
    ("a04", "10:00", "activity", "meeting_start", "架构评审会开始", False, 0, None, ""),
    ("e06", "10:20", "email", "admin", "HR：年度体检预约通道开放", False, 1, None, "有空时看到就行"),
    ("e07", "10:40", "ticket", "ticket", "P3 工单分配给你：报表导出 CSV 乱码", False, 2, None, "一句话摘要 + 建议优先级"),
    ("a05", "11:00", "activity", "meeting_end", "评审会结束", False, 0, None, ""),
    ("e08", "11:10", "alert", "staging_alert", "staging 服务 CPU 持续 90%", False, 0, None, "运维值班会处理，不需要你"),
    ("e09", "11:30", "calendar", "calendar_conflict", "15:00 的 1:1 与新加入的跨部门同步会冲突", False, 4, "15:00", "给出改期方案"),
    ("a06", "12:00", "activity", "lunch", "午饭", False, 0, None, ""),
    ("e10", "13:10", "alert", "staging_alert", "staging 证书将在 30 天后过期", False, 0, None, "运维值班会处理，不需要你"),
    ("a07", "13:30", "activity", "focus_start", "进入专注模式", False, 0, None, ""),
    ("e11", "13:50", "alert", "prod_alert", "【SEV1】生产 API p99 延迟 3.2s，错误率 5%", True, 8, "14:05", "立刻知道，并拿到 runbook"),
    ("e12", "14:20", "email", "review_request", "同事：有空帮我 review 一下 PR #482 吗？不急", False, 2, None, "专注结束后提醒"),
    ("a08", "14:55", "activity", "focus_end", "专注结束", False, 0, None, ""),
    ("e13", "15:40", "alert", "staging_alert", "staging 队列积压 1200 条", False, 0, None, "运维值班会处理，不需要你"),
    ("e14", "16:30", "email", "travel", "机票确认：周四 08:05 北京→上海", False, 2, None, "加进日历"),
    ("e15", "16:35", "calendar", "meeting_prep", "17:00 临时加开：今天 SEV1 的事故复盘会", False, 3, "17:00", "会前整理事故时间线"),
    ("e16", "16:40", "email", "manager_request", "主管：复盘会前请补充一下事故时间线", False, 3, "17:00", "会前提醒"),
    ("e17", "16:50", "ticket", "ticket", "P2 工单：SEV1 的后续修复任务", False, 2, None, "一句话摘要 + 建议优先级"),
    ("a09", "17:00", "activity", "meeting_start", "事故复盘会", False, 0, None, ""),
    ("e18", "17:20", "alert", "staging_alert", "staging 磁盘使用率 90%", False, 0, None, "运维值班会处理，不需要你"),
    ("a10", "18:00", "activity", "meeting_end", "复盘会结束", False, 0, None, ""),
    ("e19", "18:10", "email", "promo", "云厂商：年中促销最后一天", False, 0, None, ""),
    ("a11", "18:30", "activity", "day_end", "下班", False, 0, None, ""),
    ("e20", "22:40", "email", "schedule_change", "同事：明早站会改到 10:00", False, 2, "次日 09:30", "明早上班时提醒"),
    ("e21", "23:20", "alert", "prod_alert", "【SEV2】夜间批处理失败，明早的报表会缺数据", True, 6, "次日 00:30", "值班的你需要马上处理"),
    ("e22", "23:50", "email", "social", "同事：周六一起去爬山吗？", False, 1, None, "明天有空再看"),
    ("a12", "次日 08:30", "activity", "day_start", "第二天到公司", False, 0, None, ""),
]


def simulate_day() -> tuple[list[Event], dict[str, Truth]]:
    """返回 (按时间排序的事件流, 每个事件的真实需求)。完全确定，便于复现和测试。"""
    events, truth = [], {}
    for eid, t, source, kind, title, urgent, value, deadline, need in _DAY_SCRIPT:
        events.append(Event(eid, at(t), source, kind, title, urgent))
        truth[eid] = Truth(value, at(deadline) if deadline else None, need)
    events.sort(key=lambda e: e.t)
    return events, truth


# =====================================================================
# 2. 用户模型：带置信度和证据的推断
# =====================================================================

CONF_FLOOR, CONF_CEIL = 0.001, 0.999  # 置信度永远不到 0 或 1：到了就再也改不回来了（见 update_belief）
USER_CONFIRMED = 0.97  # 用户亲口确认后的置信度（否认则是 0.03）


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    """数值稳定的 sigmoid：x 很负时 math.exp(-x) 会溢出，所以分两支写。"""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def update_belief(prior: float, evidence_weight: float, supports: bool) -> float:
    """用一条证据更新置信度（对数几率形式的贝叶斯更新）。练习 (a) 的参考实现。

    贝叶斯公式的几率形式：后验几率 = 先验几率 × 似然比。两边取对数就变成了加法：
        logit(后验) = logit(先验) ± evidence_weight
    evidence_weight 就是 ln(似然比)：1.0 ≈ "这条证据在推断为真时出现的可能性是为假时的 e ≈ 2.7 倍"。
    """
    if math.isnan(prior) or math.isnan(evidence_weight):
        raise ValueError("prior / evidence_weight 不能是 NaN")
    if evidence_weight < 0:
        raise ValueError("evidence_weight 必须 ≥ 0；反证请用 supports=False 表达")
    p = min(max(prior, CONF_FLOOR), CONF_CEIL)  # 先夹住：logit(0) = -∞、logit(1) = +∞
    x = _logit(p) + (evidence_weight if supports else -evidence_weight)
    return min(max(_sigmoid(x), CONF_FLOOR), CONF_CEIL)


@dataclass
class Evidence:
    t: int
    source: str  # calendar / email / feedback / user_correction ...
    note: str  # 派生出来的信号描述，而不是原始内容（数据最小化：不存邮件正文）
    weight: float
    supports: bool


@dataclass
class Belief:
    key: str
    statement: str  # 自然语言命题，给人看、也给模型看（GUM 的 proposition）
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    pinned: bool = False  # 用户亲口纠正过：被动观察不再改变它
    half_life_days: float | None = None  # 多少天没有新证据，把握减半（在对数几率上）；None = 稳定偏好，不衰减
    sensitive: bool = False  # 来自私人数据的推断：只在用户明确同意的场景使用


class UserModel:
    """一个用户的推断集合。三条设计原则：
    1. 每条推断都带置信度和证据 —— 能解释"你凭什么这么认为"；
    2. 用户说了算 —— 纠正（correct）直接锁定，遗忘（forget）删掉并拉黑；
    3. 默认会过期 —— 临时状态随时间衰减（decay），不会永远挂在那里。
    """

    def __init__(self, user_id: str = "lin"):
        self.user_id = user_id
        self.beliefs: dict[str, Belief] = {}
        self.blocked: set[str] = set()  # 用户要求遗忘、且不许再推断的 key（"墓碑"）

    def add(
        self, key: str, statement: str, prior: float = 0.5, *, half_life_days: float | None = None, sensitive: bool = False
    ) -> Belief | None:
        if key in self.blocked:
            return None
        belief = Belief(key, statement, min(max(prior, CONF_FLOOR), CONF_CEIL), half_life_days=half_life_days, sensitive=sensitive)
        self.beliefs[key] = belief
        return belief

    def observe(
        self, key: str, *, weight: float, supports: bool, t: int, source: str, note: str, statement: str | None = None
    ) -> float | None:
        """记录一条证据并更新置信度。返回新的置信度；被拉黑 / 不认识的 key 返回 None。"""
        if key in self.blocked:
            return None  # 用户说过"别再推断这个"：连证据都不记
        belief = self.beliefs.get(key)
        if belief is None:
            if statement is None:
                return None
            belief = self.add(key, statement)
        if belief.pinned:
            return belief.confidence  # 用户亲口说过的，被动信号不能推翻
        belief.evidence.append(Evidence(t, source, note, weight, supports))
        belief.confidence = update_belief(belief.confidence, weight, supports)
        return belief.confidence

    def correct(self, key: str, value: bool, *, t: int, note: str = "用户亲口纠正") -> Belief:
        """用户纠正：不走贝叶斯更新，直接设为 0.97 / 0.03 并锁定。显式反馈比一百次隐式信号都可靠。"""
        belief = self.beliefs[key]
        belief.confidence = USER_CONFIRMED if value else 1 - USER_CONFIRMED
        belief.pinned = True
        belief.evidence.append(Evidence(t, "user_correction", note, 0.0, value))
        return belief

    def confidence(self, key: str, default: float = 0.5) -> float:
        b = self.beliefs.get(key)
        return b.confidence if b else default

    def decay(self, days: float) -> None:
        """过了 days 天、没有新证据：把置信度往 0.5（"不知道"）拉回去。锁定的和稳定偏好不衰减。"""
        for b in self.beliefs.values():
            if b.pinned or not b.half_life_days:
                continue
            b.confidence = _sigmoid(_logit(b.confidence) * 0.5 ** (days / b.half_life_days))

    def context_for(self, keys: list[str], *, allow_sensitive: bool = False) -> list[dict]:
        """只取和当前事件相关的推断发给模型（数据最小化）；敏感推断默认不发。"""
        out = []
        for k in keys:
            b = self.beliefs.get(k)
            if b is None or (b.sensitive and not allow_sensitive):
                continue
            out.append({"key": b.key, "statement": b.statement, "confidence": round(b.confidence, 2)})
        return out

    def copy(self) -> "UserModel":
        m = UserModel(self.user_id)
        m.beliefs = {k: replace(b, evidence=[replace(e) for e in b.evidence]) for k, b in self.beliefs.items()}
        m.blocked = set(self.blocked)
        return m


def forget(model: UserModel, key: str) -> bool:
    """遗忘一条推断：删掉推断和它的全部证据，并加入 blocked，之后不再推断它。练习 (c) 的参考实现。

    返回 True 表示确实删掉了一条推断；本来就没有则返回 False（但仍然拉黑）。
    """
    existed = model.beliefs.pop(key, None) is not None
    model.blocked.add(key)
    return existed


def explain(model: UserModel, key: str) -> dict:
    """解释一条推断："我有多大把握、凭什么"。练习 (c) 的参考实现。不存在（或已遗忘）时抛 KeyError。"""
    belief = model.beliefs.get(key)
    if belief is None:
        raise KeyError(key)
    evidence = [asdict(e) for e in sorted(belief.evidence, key=lambda e: e.t)]  # asdict 是深拷贝：调用方改了也不影响模型
    n_support = sum(1 for e in evidence if e["supports"])
    n_refute = len(evidence) - n_support
    pct = round(belief.confidence * 100)
    basis = f"{n_support} 条支持、{n_refute} 条反对" if evidence else "没有任何证据，只是先验"
    if belief.pinned:
        basis += "（已被你亲口纠正并锁定）"
    return {
        "key": belief.key,
        "statement": belief.statement,
        "confidence": round(belief.confidence, 3),
        "pinned": belief.pinned,
        "n_support": n_support,
        "n_refute": n_refute,
        "evidence": evidence,
        "summary": f"我有 {pct}% 的把握认为：{belief.statement}。依据：{basis}。",
    }


# 事件类型 → (用哪条推断估计"用户想不想要"，帮上忙时的收益估计)
# GUMBO 让 LLM 估计收益和成本；这里用一张表：确定、可测试、零调用成本（取舍见 README 2.4）。
KIND_PROFILE: dict[str, tuple[str, float]] = {
    "fyi": ("reads_fyi", 2.0),
    "meeting_prep": ("wants_meeting_prep", 3.0),
    "manager_request": ("track_manager_requests", 3.0),
    "newsletter": ("reads_newsletters", 1.0),
    "staging_alert": ("cares_staging_alerts", 3.0),
    "admin": ("cares_admin_notices", 1.5),
    "ticket": ("wants_ticket_triage", 3.0),
    "calendar_conflict": ("wants_calendar_help", 4.0),
    "prod_alert": ("is_oncall", 8.0),
    "review_request": ("does_code_review", 3.0),
    "travel": ("wants_travel_help", 3.0),
    "promo": ("reads_promos", 1.0),
    "schedule_change": ("wants_calendar_help", 3.0),
    "social": ("enjoys_social_invites", 2.0),
    "listing": ("house_hunting", 2.0),
}


def initial_user_model() -> UserModel:
    """小林（后端工程师，本周值班）的用户模型：由"上周的观察"一条条累积出来，而不是直接写死置信度。

    其中 cares_staging_alerts 是一条**错误推断**：上周是发版周，小林才频繁看 staging；平时 staging 归运维管。
    """
    m = UserModel("lin")
    week = -7 * DAY  # 上周的证据时间记为负数（相对今天 00:00）

    def learn(key, statement, evidence, *, half_life=None, sensitive=False):
        m.add(key, statement, half_life_days=half_life, sensitive=sensitive)
        for dt, source, note, w, sup in evidence:
            m.observe(key, weight=w, supports=sup, t=week + dt, source=source, note=note)

    learn("is_oncall", "本周你是值班（on-call）工程师", [(0, "calendar", "值班表：本周值班人是你", 3.0, True)], half_life=7)
    learn("wants_meeting_prep", "重要会议前，你希望收到读材料的提醒", [
        (1 * DAY + 590, "activity", "上周二评审会开始前 10 分钟你才打开设计文档", 0.8, True),
        (3 * DAY + 830, "feedback", "上周四你接受了一次会前提醒", 0.8, True),
    ], half_life=60)
    learn("track_manager_requests", "主管提出的请求，你希望记成待办", [
        (2 * DAY + 600, "email", "上周三你给主管的 3 封邮件加了星标", 1.0, True),
        (4 * DAY + 700, "feedback", "上周五你把一封主管邮件手动加进了待办", 0.7, True),
    ], half_life=60)
    learn("reads_newsletters", "你会读技术周刊类邮件", [
        (0, "email", "技术周刊连续 6 期未打开", 0.8, False),
        (4 * DAY, "email", "上周五你把一期周刊直接归档", 0.6, False),
    ], half_life=60)
    learn("cares_staging_alerts", "你关心 staging 环境的告警", [
        (1 * DAY + 845, "activity", "上周二 14:05 你打开了 staging 监控面板", 0.8, True),
        (2 * DAY + 630, "activity", "上周三 10:30 你在 staging 告警群里回复了消息", 0.7, True),
        (4 * DAY + 970, "activity", "上周五 16:10 你把 staging 告警群设成了免打扰", 0.3, False),
    ], half_life=14)
    learn("wants_ticket_triage", "新分配的工单，你希望先看到一句话摘要和建议优先级", [
        (2 * DAY + 900, "feedback", "上周三你采纳了一次工单摘要", 0.9, True),
    ], half_life=60)
    learn("wants_calendar_help", "日程冲突或变动时，你希望助手给出处理建议", [
        (1 * DAY + 560, "feedback", "上周二你采纳了一次改期建议", 1.0, True),
        (3 * DAY + 610, "activity", "上周四你手动处理了 2 次日程冲突，各花了约 10 分钟", 0.8, True),
    ], half_life=60)
    learn("does_code_review", "同事请你 review 时，你愿意被提醒", [
        (2 * DAY + 800, "activity", "上周你在 24 小时内完成了 4 个 review 请求", 1.2, True),
    ], half_life=60)
    learn("wants_travel_help", "出差行程确认后，你希望自动加进日历", [
        (3 * DAY + 900, "activity", "上周四你手动把一张机票抄进了日历", 0.4, True),
    ], half_life=60)
    learn("cares_admin_notices", "你关心行政 / HR 通知", [], half_life=60)
    learn("reads_promos", "你会看营销邮件", [(0, "email", "过去 30 天的营销邮件打开率为 0", 2.0, False)], half_life=60)
    learn("enjoys_social_invites", "同事的非工作邀约，你愿意看到", [
        (4 * DAY + 1100, "email", "上周五你回复了同事的聚餐邀请", 0.5, True),
    ], half_life=30)
    learn("reads_fyi", "海外同事的 FYI 邮件，你会在第二天早上集中看", [
        (2 * DAY + 520, "activity", "上周三早上你集中处理了 5 封 FYI 邮件", 0.4, True),
    ], half_life=30)
    learn("house_hunting", "你最近在找房子", [
        (5 * DAY + 1300, "email", "私人邮箱收到租房平台的'看房预约成功'通知", 0.8, True),
    ], half_life=10, sensitive=True)
    return m


def estimate(event: Event, model: UserModel) -> tuple[float, float, str]:
    """估计 (帮上忙时的收益, 用户想要的置信度, 依据的推断 key)。"""
    key, benefit = KIND_PROFILE[event.kind]
    return benefit, model.confidence(key), key


# =====================================================================
# 3. 打扰决策器
# =====================================================================


@dataclass(frozen=True)
class Context:
    now: int  # 分钟数（≥ 1440 表示第二天）
    focus: bool = False  # 用户在专注（深度工作）
    in_meeting: bool = False
    urgent: bool = False  # 事件源标记的紧急事件（SEV1、安全事件……）


@dataclass(frozen=True)
class Limits:
    threshold: float = 1.0  # 净收益要严格大于它才值得打扰
    base_cost: float = 0.6  # 一次打扰的基础成本（和收益同一个单位）
    quiet_start: int = 22 * 60  # 勿扰时段 22:00–08:00（可以跨午夜）
    quiet_end: int = 8 * 60
    max_per_hour: int = 3  # 最近 60 分钟最多打扰几次
    focus_multiplier: float = 3.0  # 打断专注的成本是平时的 3 倍
    meeting_multiplier: float = 5.0
    urgent_min_confidence: float = 0.5  # 紧急事件也要有起码的把握，否则就是"狼来了"


@dataclass(frozen=True)
class Decision:
    action: Literal["interrupt", "defer", "drop"]
    reason: str  # worth_it / urgent_override / quiet_hours / busy / rate_limited / not_worth_it / urgent_low_confidence
    score: float  # 考虑当前情境后的净收益


def in_quiet_hours(now: int, limits: Limits) -> bool:
    m = now % DAY
    if limits.quiet_start == limits.quiet_end:
        return False
    if limits.quiet_start < limits.quiet_end:
        return limits.quiet_start <= m < limits.quiet_end
    return m >= limits.quiet_start or m < limits.quiet_end  # 跨午夜


def should_interrupt(
    benefit: float, confidence: float, cost: float, context: Context, recent_interrupts: list[int], limits: Limits
) -> Decision:
    """该不该现在打扰用户？练习 (b) 的参考实现。

    interrupt = 现在就说；defer = 攒进摘要，等用户有空（专注结束、会议结束、第二天早上）；drop = 不值得说。
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence 必须在 [0, 1] 内，收到 {confidence}")
    if benefit < 0 or cost < 0:
        raise ValueError("benefit 和 cost 不能为负")

    base = benefit * confidence - cost  # 不考虑情境时的净收益
    if context.urgent:
        # 紧急事件：越过勿扰、专注和频率上限 —— 但把握太低时不能越权（否则就是凌晨三点的误报）
        if confidence < limits.urgent_min_confidence:
            return Decision("defer", "urgent_low_confidence", base)
        if base > limits.threshold:
            return Decision("interrupt", "urgent_override", base)
        return Decision("drop", "not_worth_it", base)

    if base <= limits.threshold:
        return Decision("drop", "not_worth_it", base)  # 什么时候说都不值得
    if in_quiet_hours(context.now, limits):
        return Decision("defer", "quiet_hours", base)

    multiplier = 1.0
    if context.focus:
        multiplier = max(multiplier, limits.focus_multiplier)
    if context.in_meeting:
        multiplier = max(multiplier, limits.meeting_multiplier)
    score = benefit * confidence - cost * multiplier
    if score <= limits.threshold:
        return Decision("defer", "busy", score)  # 值得说，但不是现在：等用户空下来

    recent = [t for t in recent_interrupts if context.now - 60 < t <= context.now]
    if len(recent) >= limits.max_per_hour:
        return Decision("defer", "rate_limited", score)
    return Decision("interrupt", "worth_it", score)


# =====================================================================
# 4. 建议卡片：LLM + complete_json
# =====================================================================


class SuggestionCard(BaseModel):
    title: str = Field(description="卡片标题，不超过 20 个字")
    message: str = Field(description="1-2 句话：发生了什么、建议你做什么")
    why_now: str = Field(description="为什么现在提醒：结合事件和用户模型给出的依据，一句话")
    proposed_action: str = Field(description="建议的下一步。只是建议：用户点确认后才会执行")
    reversible: bool = Field(description="这个下一步执行后能否撤销")
    uses_beliefs: list[str] = Field(default_factory=list, description="引用到的用户模型条目的 key（只能从给出的列表里选）")


CARD_SYSTEM = (
    "你是一个主动式工作助理，负责把事件写成给用户看的'建议卡片'。规则：\n"
    "1. 只建议，不执行：绝不能声称你已经做了任何操作；\n"
    "2. 只使用给出的事件和用户模型信息，不编造事件里没有的细节（如文档链接、人名、数字）；\n"
    "3. why_now 要说清为什么值得现在打扰，并在 uses_beliefs 里列出你引用的用户模型 key；\n"
    "4. proposed_action 从'可选的下一步'里选一个（可以补充细节），不要自创不可撤销的动作（如'直接发送''直接删除'）；\n"
    "5. 简短：用户正在忙，标题一眼能看懂。"
)

# 每类事件的"下一步菜单"。第一版提示词只写了"优先可撤销的下一步"，结果真实模型 3 次都把 SEV1 的下一步写成了
# "加入待办" —— 安全了，但没用（见 README 2.5）。把动作做成按事件类型给出的白名单，比一条笼统的规则可靠。
KIND_ACTIONS: dict[str, list[str]] = {
    "meeting_prep": ["在会前插入一个读材料的日程块", "打开会议关联的文档", "草拟评审问题清单"],
    "manager_request": ["加入待办并设截止前一天的提醒", "草拟一封确认收到的回复"],
    "staging_alert": ["标记为已读", "转给运维值班", "打开监控面板"],
    "prod_alert": ["打开对应的 runbook", "在事故频道草拟'我已接手'的消息（你确认后才发送）", "打开监控面板"],
    "ticket": ["生成一句话摘要并建议优先级", "加入待办"],
    "calendar_conflict": ["草拟改期方案", "草拟给对方的改期消息（你确认后才发送）"],
    "schedule_change": ["更新日程（你确认后才生效）", "加入明早提醒"],
    "review_request": ["加入待办", "草拟回复约定 review 时间"],
    "travel": ["把行程加进日历（你确认后才生效）"],
}
DEFAULT_ACTIONS = ["标记为已读", "加入待办"]

_REASON_TEXT = {
    "worth_it": "净收益超过阈值，且用户当前不忙",
    "urgent_override": "紧急事件，越过了专注 / 勿扰 / 频率上限",
    "quiet_hours": "处于勿扰时段，攒到明早的摘要里",
    "busy": "用户正在专注或开会，攒到空档时的摘要里",
    "rate_limited": "最近 60 分钟打扰次数已达上限，攒到摘要里",
}


@dataclass
class CardResult:
    card: SuggestionCard
    warnings: list[str]
    prompt: str
    beliefs_sent: list[str]  # 实际发给模型的推断 key（审计用：模型看到了用户的哪些信息）


_CLAIMS_EXECUTION = re.compile(r"已(经)?(为你|帮你|替你|自动)|我已(经)?(把|将|给|为|帮)")


def make_card(llm: LLM, events: list[Event], model: UserModel, decision: Decision | None = None, now: int | None = None) -> CardResult:
    """为一条事件（或一次摘要里的多条事件，按重要性从高到低排好）生成建议卡片，并对模型输出做事后检查。"""
    keys = list(dict.fromkeys(KIND_PROFILE[e.kind][0] for e in events))
    beliefs = model.context_for(keys)  # 只发相关、非敏感的推断
    lines = [f"- 事件 ID：{e.id}｜{hhmm(e.t)}｜来源：{e.source}｜{'【紧急】' if e.urgent else ''}{e.title}" for e in events]
    belief_lines = [f"- {b['key']}（把握 {b['confidence']:.0%}）：{b['statement']}" for b in beliefs] or ["- （无）"]
    # 摘要里有多条事件时，排序由决策器决定（调用方按 score 从高到低传入），下一步只针对第 1 条：
    # 否则模型可能挑一条噪音来给建议（见 README 2.5）
    actions = KIND_ACTIONS.get(events[0].kind, DEFAULT_ACTIONS)
    header = ("下面是一条需要提醒用户的事件" if len(events) == 1 else
              f"下面是攒在一次摘要里的 {len(events)} 条事件（已按重要性从高到低排好），请合成一张卡片，proposed_action 针对第 1 条")
    reason = _REASON_TEXT.get(decision.reason, decision.reason) if decision else "定时摘要"
    prompt = (
        f"{header}（当前时间 {hhmm(now if now is not None else events[-1].t)}）：\n" + "\n".join(lines)
        + f"\n\n决策器为什么现在提醒：{reason}"
        + "\n\n用户模型中相关的推断（带把握度）：\n" + "\n".join(belief_lines)
        + "\n\n可选的下一步：\n" + "\n".join(f"- {a}" for a in actions)
    )
    card = complete_json(llm, prompt, SuggestionCard, system=CARD_SYSTEM)

    warnings = []
    allowed = {b["key"] for b in beliefs}
    unknown = [k for k in card.uses_beliefs if k not in allowed]
    if unknown:
        warnings.append(f"模型引用了不存在或未提供的推断 {unknown}，已删除")
        card.uses_beliefs = [k for k in card.uses_beliefs if k in allowed]
    text = f"{card.title} {card.message} {card.proposed_action}"
    if _CLAIMS_EXECUTION.search(text):
        warnings.append("卡片声称'已经执行'了操作 —— 违反'只建议不执行'，上线前应拦截")
    return CardResult(card, warnings, prompt, sorted(allowed))


# 离线剧本：按事件 ID 返回预先写好的卡片 JSON（通过 ScriptedLLM 走完整的 complete_json 流程）
OFFLINE_CARDS: dict[str, dict] = {
    "e02": {
        "title": "10:00 架构评审，先读设计文档",
        "message": "你是 10:00 支付服务架构评审的评审人，离开会还有 85 分钟。建议先安排时间读评审材料。",
        "why_now": "现在你没有在专注或开会，提醒不会打断你；你希望重要会议前收到读材料的提醒（把握 83%）。",
        "proposed_action": "在会前插入一个读材料的日程块",
        "reversible": True,
        "uses_beliefs": ["wants_meeting_prep"],
    },
    "e11": {
        "title": "SEV1：生产 API 延迟 3.2s",
        "message": "生产 API p99 延迟 3.2s、错误率 5%，你是本周值班。建议立刻打开 runbook 开始处置。",
        "why_now": "紧急事件且你在值班，所以越过了专注模式。",
        "proposed_action": "打开对应的 runbook",
        "reversible": True,
        "uses_beliefs": ["is_oncall"],
    },
    "e03+e05": {
        "title": "专注结束：2 件事等你看",
        "message": "① 主管要你周五前交 Q3 延迟分析报告；② 09:30 staging 磁盘使用率 85%。",
        "why_now": "你专注时这两件都不值得打断，现在是空档，一次看完。",
        "proposed_action": "把 Q3 报告加入待办，并设截止前一天（周四）的提醒",
        "reversible": True,
        "uses_beliefs": ["track_manager_requests", "cares_staging_alerts"],
    },
}


def offline_card_llm() -> ScriptedLLM:
    """离线模式的"模型"：从提示词里读出事件 ID，返回对应的剧本卡片。"""
    import json

    def respond(messages: list[dict]) -> LLMResponse:
        ids = re.findall(r"事件 ID：(\w+)", messages[-1]["content"])
        card = OFFLINE_CARDS.get("+".join(ids)) or {
            "title": "有一件事可能需要你看看",
            "message": "；".join(ids),
            "why_now": "离线剧本的兜底卡片",
            "proposed_action": "打开查看",
            "reversible": True,
            "uses_beliefs": [],
        }
        return LLMResponse(content=json.dumps(card, ensure_ascii=False))

    return ScriptedLLM([respond] * 20, model="scripted-cards")


# =====================================================================
# 5. 跑完一天 + 模拟用户打分
# =====================================================================


@dataclass(frozen=True)
class Scoring:
    """模拟用户的"感受"。所有数字都是假设 —— 结论依赖它们，这正是要讲的一点（见 README 3）。"""

    useless_interrupt: float = -1.0  # 打扰了但没用
    focus_penalty: float = -1.5  # 打断专注（有用也要付）
    meeting_penalty: float = -2.0
    quiet_penalty: float = -3.0  # 深夜 / 清晨把人吵醒
    critical_value: float = 5.0  # 真实价值 ≥ 它的紧急事件：被打断也心甘情愿，不扣情境分
    digest_cost: float = -0.3  # 推送一次摘要（轻打扰）
    digest_useless_item: float = -0.2  # 摘要里的无用条目（扫一眼的成本）
    digest_factor: float = 0.9  # 有用条目进摘要：晚了一点，价值打九折
    miss_factor: float = 0.5  # 错过普通需求：损失一半价值
    urgent_miss_factor: float = 1.0  # 错过紧急需求：损失全部价值


@dataclass
class Row:
    t: int
    event: Event
    action: str  # interrupt / defer / drop / digest / skip
    reason: str
    score: float | None
    outcome: str  # 用户的反应
    delta: float  # 这一步对满意度的影响


@dataclass
class DayReport:
    policy: str
    useful: int = 0  # 在截止前送达、且用户确实需要的建议数
    notifications: int = 0  # 打扰次数：即时打扰 + 摘要推送
    busy_interrupts: int = 0  # 其中打断专注 / 会议的次数（紧急事件除外）
    night_interrupts: int = 0  # 其中勿扰时段的次数（紧急事件除外）
    missed: int = 0  # 用户需要、却没有及时送达的
    satisfaction: float = 0.0  # 满意度（代理指标）= 所有 delta 之和
    rows: list[Row] = field(default_factory=list)
    digests: list[tuple[int, list[Event]]] = field(default_factory=list)


Policy = Literal["never", "always", "decider"]

IMPLICIT_ACCEPT, IMPLICIT_DISMISS, DIGEST_IGNORED = 0.5, 0.5, 0.2  # 隐式反馈的证据权重：都很弱（用户可能只是太忙）


def run_day(
    events: list[Event],
    truth: dict[str, Truth],
    policy: Policy,
    model: UserModel | None = None,
    *,
    limits: Limits = Limits(),
    scoring: Scoring = Scoring(),
    corrections: dict[str, bool] | None = None,
    learn: bool = True,
) -> DayReport:
    """按策略跑完一天。model 会被原地更新（隐式反馈、用户纠正），需要对比时请先 model.copy()。

    corrections：{推断 key: 真实值}。模拟用户在"被这条推断打扰且没用"时，会点开"为什么推荐这个"并纠正它。
    """
    corrections = dict(corrections or {})
    report = DayReport(policy)
    focus = meeting = False
    pending: list[Event] = []
    recent: list[int] = []
    delivered: set[str] = set()

    def useful_now(e: Event, t: int) -> bool:
        tr = truth[e.id]
        return tr.value > 0 and (tr.deadline is None or t <= tr.deadline)

    def add(t, e, action, reason, score, outcome, delta):
        report.rows.append(Row(t, e, action, reason, score, outcome, delta))
        report.satisfaction += delta

    def interrupt(e: Event, reason: str, score: float | None) -> None:
        tr = truth[e.id]
        report.notifications += 1
        recent.append(e.t)
        critical = e.urgent and tr.value >= scoring.critical_value
        delta, notes = 0.0, []
        if useful_now(e, e.t):
            delta += tr.value
            report.useful += 1
            delivered.add(e.id)
            notes.append(f"有用 +{tr.value:g}")
        else:
            delta += scoring.useless_interrupt
            notes.append(f"没用 {scoring.useless_interrupt:g}")
        if not critical:
            if in_quiet_hours(e.t, limits):
                delta += scoring.quiet_penalty
                report.night_interrupts += 1
                notes.append(f"深夜被吵醒 {scoring.quiet_penalty:g}")
            elif meeting:
                delta += scoring.meeting_penalty
                report.busy_interrupts += 1
                notes.append(f"打断会议 {scoring.meeting_penalty:g}")
            elif focus:
                delta += scoring.focus_penalty
                report.busy_interrupts += 1
                notes.append(f"打断专注 {scoring.focus_penalty:g}")
        # 反馈回路：用户点了"有用"或"忽略"，这是隐式证据
        if model is not None and learn and e.kind in KIND_PROFILE:
            key = KIND_PROFILE[e.kind][0]
            ok = useful_now(e, e.t)
            model.observe(key, weight=IMPLICIT_ACCEPT if ok else IMPLICIT_DISMISS, supports=ok, t=e.t,
                          source="feedback", note=f"{hhmm(e.t)} 你{'采纳' if ok else '忽略'}了关于「{e.title}」的提醒")
            if not ok and key in corrections and key in model.beliefs and not model.beliefs[key].pinned:
                model.correct(key, corrections.pop(key), t=e.t, note=f"{hhmm(e.t)} 你点开'为什么推荐这个'后纠正了这条推断")
                notes.append(f"用户纠正了推断 {key}")
        add(e.t, e, "interrupt", reason, score, "，".join(notes), delta)

    def flush_digest(t: int, trigger: Event) -> None:
        if not pending:
            return
        items = list(pending)
        pending.clear()
        report.notifications += 1
        recent.append(t)
        report.digests.append((t, items))
        add(t, trigger, "digest", f"{len(items)} 条", None, f"推送摘要 {scoring.digest_cost:g}", scoring.digest_cost)
        for e in items:
            tr = truth[e.id]
            if useful_now(e, t):
                gain = tr.value * scoring.digest_factor
                report.useful += 1
                delivered.add(e.id)
                add(t, e, "digest_item", "", None, f"有用 +{gain:g}", gain)
                ok = True
            else:
                late = tr.value > 0
                add(t, e, "digest_item", "", None, "已过截止时间" if late else f"没用 {scoring.digest_useless_item:g}",
                    0.0 if late else scoring.digest_useless_item)
                ok = False
            if model is not None and learn and e.kind in KIND_PROFILE:
                key = KIND_PROFILE[e.kind][0]
                model.observe(key, weight=DIGEST_IGNORED, supports=ok, t=t, source="feedback",
                              note=f"{hhmm(t)} 摘要里的「{e.title}」你{'点开了' if ok else '没看'}")

    for e in sorted(events, key=lambda x: x.t):
        if e.kind in ACTIVITY_KINDS:
            focus = {"focus_start": True, "focus_end": False}.get(e.kind, focus if e.kind != "day_end" else False)
            meeting = {"meeting_start": True, "meeting_end": False}.get(e.kind, meeting)
            if e.kind in BREAK_KINDS:
                flush_digest(e.t, e)
            continue

        if policy == "never":
            add(e.t, e, "skip", "从不主动", None, "", 0.0)
            continue
        if policy == "always":
            interrupt(e, "每件事都说", None)
            continue

        if model is None:
            raise ValueError("decider 策略需要用户模型")
        benefit, conf, _key = estimate(e, model)
        ctx = Context(now=e.t, focus=focus, in_meeting=meeting, urgent=e.urgent)
        d = should_interrupt(benefit, conf, limits.base_cost, ctx, recent, limits)
        if d.action == "interrupt":
            interrupt(e, d.reason, d.score)
        elif d.action == "defer":
            pending.append(e)
            add(e.t, e, "defer", d.reason, d.score, "", 0.0)
        else:
            add(e.t, e, "drop", d.reason, d.score, "", 0.0)

    # 一天结束：用户需要、却没有及时送达的，按"错过"扣分
    for e in events:
        tr = truth[e.id]
        if e.kind in ACTIVITY_KINDS or tr.value <= 0 or e.id in delivered:
            continue
        factor = scoring.urgent_miss_factor if e.urgent else scoring.miss_factor
        report.missed += 1
        add(e.t, e, "missed", "", None, f"错过 -{tr.value * factor:g}", -tr.value * factor)
    return report


def policy_table(reports: list[DayReport]) -> list[list[str]]:
    return [
        [r.policy, str(r.useful), str(r.notifications), str(r.busy_interrupts), str(r.night_interrupts), str(r.missed), f"{r.satisfaction:+.1f}"]
        for r in reports
    ]


__all__ = [
    "DAY", "hhmm", "at", "Event", "Truth", "simulate_day", "ACTIVITY_KINDS", "BREAK_KINDS",
    "CONF_FLOOR", "CONF_CEIL", "USER_CONFIRMED", "update_belief", "Evidence", "Belief", "UserModel", "forget", "explain",
    "KIND_PROFILE", "initial_user_model", "estimate",
    "Context", "Limits", "Decision", "in_quiet_hours", "should_interrupt",
    "SuggestionCard", "CardResult", "KIND_ACTIONS", "make_card", "offline_card_llm", "OFFLINE_CARDS",
    "Scoring", "Row", "DayReport", "run_day", "policy_table",
]
