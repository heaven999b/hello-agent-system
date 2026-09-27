"""第 22 课的迷你 benchmark：退款工单 Agent。用"四元组"来组织，再用 ABC checklist 审查它的前身 v0。

四元组（request / environment / stopping criteria / scorer）在本文件里分别是：

    请求 request        render_request(task, as_of)：客户留言 + 系统附带的订单快照（可信数据）
    环境 environment    Ledger（每次试验一个全新的账本）+ 三个决策工具 + 写在 system prompt 里的退款政策
    停止条件 stopping   StopAfterDecision：作出决定后立即停止；最多 MAX_STEPS 步，超出算失败
    评分器 scorer       score()：只看环境终态（账本里恰好一个决定、类型和金额都对），不看措辞

日期怎么处理（本课真实踩过的坑，讲义 5.1）：任务里真正"冻结"的是**签收后第几天**，
渲染请求时再整体平移到运行当天（as_of）。这样天数永远不变（ABC T.6），
又不会和模型网关往系统提示里注入的真实日期互相矛盾。

还有两样东西帮助我们相信这个 benchmark：
    oracle_decide(task)   参考解（reference solution）：把政策写成代码，证明每个任务可解、标注没写错
    FlawedBenchV0         v0 版本：结构相同，但藏着 ABC 能查出来的漏洞；PROBES 是用来揭穿它的"探针 Agent"
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Callable

from pydantic import Field

from agentkit import Agent, Hook, StopRun, Tool, tool
from agentkit.llm import LLM

HERE = Path(__file__).resolve().parent
BENCH_TODAY = "2026-09-19"  # tasks.jsonl 里的签收日期以这一天为基准；v1 渲染时平移到运行当天，v0 直接拿系统日期算
MAX_STEPS = 4  # 停止条件：一个工单最多 4 次模型调用。正常情况 1 次就够（直接调用决策工具）
REFUND_CAP = 2000.0

POLICY = """退款政策（云杉家居 v3）
R1 签收后 7 天内（含第 7 天）且商品未拆封：全额退款。
R2 签收后 7 天内（含第 7 天）、已拆封、非质量问题：退实付金额的 80%（扣 20% 拆封损耗），保留两位小数。
R3 质量问题且客户上传了凭证（照片或检测报告）：签收后 30 天内（含第 30 天）全额退款，不论是否拆封、是否定制。
   声称有质量问题但没有凭证的，按非质量问题处理。
R4 定制商品：非质量问题一律不退。
R5 不符合以上任何退款条件（例如超出时限）：拒绝退款。
R6 按以上规则算出的应退金额超过 2000 元：不要直接退款，转人工审核。
R7 订单已有退款记录：拒绝重复退款。
优先级：R7 > R3 > R4 > R1/R2 > R5；R6 作用在最终应退金额上。
签收天数 = 今天日期 - 签收日期（签收当天为第 0 天）。"""


# ---------------------------------------------------------------- 任务（数据，不是代码）


@dataclass(frozen=True)
class RefundTask:
    id: str
    item: str
    price: float
    signed: str
    opened: bool
    custom: bool
    evidence: bool  # 客户是否上传了质量问题凭证
    refunded_before: bool
    message: str
    expected: dict  # {"action": "refund", "amount": 239.2} / {"action": "reject"} / {"action": "escalate"}
    category: str

    @property
    def days(self) -> int:
        """签收后第几天（相对基准日）。这才是任务真正的"事实"，不随运行日期变化。"""
        return (date.fromisoformat(BENCH_TODAY) - date.fromisoformat(self.signed)).days


def load_tasks(path: str | Path = HERE / "tasks.jsonl") -> list[RefundTask]:
    tasks = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("//"):
            tasks.append(RefundTask(**json.loads(line)))
    return tasks


def snapshot(task: RefundTask, as_of: date | None = None) -> dict:
    """Agent 能看到的订单事实。注意：**不含** expected / category 这类只属于评分器的字段（ABC T.5）。

    as_of：以哪一天作为"今天"渲染日期（默认基准日）。签收日期 = as_of - 签收天数，天数保持不变。"""
    today = as_of or date.fromisoformat(BENCH_TODAY)
    return {
        "order_id": task.id,
        "item": task.item,
        "custom": task.custom,
        "price": task.price,
        "signed": (today - timedelta(days=task.days)).isoformat(),
        "today": today.isoformat(),
        "opened": task.opened,
        "evidence": task.evidence,
        "refunded_before": task.refunded_before,
    }


def render_request(task: RefundTask, as_of: date | None = None) -> str:
    """请求 = 客户留言 + 订单快照（系统附带，可信）。日期按 as_of 平移，签收天数不变。"""
    s = snapshot(task, as_of)
    return (
        f"【客户留言】{task.message}\n"
        f"【订单快照（系统附带，可信）】\n"
        f"订单号：{s['order_id']}\n"
        f"商品：{s['item']}（{'定制商品' if s['custom'] else '非定制商品'}）\n"
        f"实付金额：{s['price']:.2f} 元\n"
        f"签收日期：{s['signed']}\n"
        f"今天日期：{s['today']}\n"
        f"是否已拆封：{'是' if s['opened'] else '否'}\n"
        f"质量问题凭证：{'已上传（照片/检测报告）' if s['evidence'] else '无'}\n"
        f"该订单历史退款记录：{'有' if s['refunded_before'] else '无'}"
    )


# ---------------------------------------------------------------- 参考解：把政策写成代码


def decide(snap: dict) -> dict:
    """按 POLICY 作出决定。输入是订单快照（dict），输出 {"action", "amount"?, "rule"}。"""
    if snap["refunded_before"]:
        return {"action": "reject", "rule": "R7"}
    days = (date.fromisoformat(snap["today"]) - date.fromisoformat(snap["signed"])).days
    if snap["evidence"] and days <= 30:
        amount, rule = snap["price"], "R3"
    elif snap["custom"]:
        return {"action": "reject", "rule": "R4"}
    elif days <= 7:
        amount, rule = (snap["price"], "R1") if not snap["opened"] else (round(snap["price"] * 0.8, 2), "R2")
    else:
        return {"action": "reject", "rule": "R5"}
    if amount > REFUND_CAP:
        return {"action": "escalate", "rule": "R6"}
    return {"action": "refund", "amount": amount, "rule": rule}


def oracle_decide(task: RefundTask) -> dict:
    """参考解（ABC T.9 的 oracle solver）：证明任务可解，并用来交叉核对人工标注（T.7）。"""
    return decide(snapshot(task))


def label_mismatches(tasks: list[RefundTask]) -> list[tuple[str, dict, dict]]:
    """参考解与标注不一致的任务：(任务 id, 标注, 参考解)。v1 应该为空。"""
    out = []
    for t in tasks:
        o = oracle_decide(t)
        same = o["action"] == t.expected["action"] and (o["action"] != "refund" or abs(o["amount"] - t.expected["amount"]) < 0.01)
        if not same:
            out.append((t.id, t.expected, o))
    return out


# ---------------------------------------------------------------- 环境：账本 + 决策工具


@dataclass
class Decision:
    action: str  # refund / reject / escalate
    order_id: str
    amount: float | None = None
    note: str = ""


@dataclass
class Ledger:
    """环境状态。评分器只看这里 —— "Agent 说了什么"不算数，"Agent 做了什么"才算数。"""

    decisions: list[Decision] = field(default_factory=list)

    def refunded(self, order_id: str) -> bool:
        return any(d.action == "refund" and d.order_id == order_id for d in self.decisions)


def make_tools(ledger: Ledger) -> list[Tool]:
    """每次试验用一个新 Ledger 生成一套新工具（闭包绑定）。工具之间、试验之间不共享任何状态。"""

    @tool(risk="write")
    def issue_refund(
        order_id: Annotated[str, Field(description="订单号，如 YS-1001")],
        amount: Annotated[float, Field(description="退款金额（元），保留两位小数")],
    ) -> str:
        """直接给客户退款（决策工具之一：每个工单只能调用一个决策工具，且只调用一次）。"""
        ledger.decisions.append(Decision("refund", order_id, round(float(amount), 2)))
        return f"已向 {order_id} 退款 {float(amount):.2f} 元。"

    @tool
    def reject_refund(
        order_id: Annotated[str, Field(description="订单号")],
        rule: Annotated[str, Field(description="拒绝所依据的政策条款编号，如 R5")],
    ) -> str:
        """拒绝退款（决策工具之一）。"""
        ledger.decisions.append(Decision("reject", order_id, note=rule))
        return f"已拒绝 {order_id} 的退款申请（{rule}）。"

    @tool
    def escalate(
        order_id: Annotated[str, Field(description="订单号")],
        reason: Annotated[str, Field(description="转人工的原因")],
    ) -> str:
        """转人工审核（决策工具之一）。"""
        ledger.decisions.append(Decision("escalate", order_id, note=reason))
        return f"已将 {order_id} 转人工审核。"

    return [issue_refund, reject_refund, escalate]


class StopAfterDecision(Hook):
    """停止条件：账本里出现决定后，不再调用模型。

    类似 SWE-agent 的 submit：Agent "交卷"即结束。好处是每个工单通常只花 1 次模型调用；
    代价是测不到"交卷后给客户的回复写得好不好"——那部分交给 demo 第 7 节的成对评委。
    停止条件决定了你**测的是什么**，这是设计 benchmark 时要明确写下来的取舍。
    """

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def before_llm(self, state, messages) -> None:
        if self.ledger.decisions:
            raise StopRun("decision_submitted", "已提交决定")


# ---------------------------------------------------------------- 评分器：只看终态


def score(task: RefundTask, ledger: Ledger, status: str = "completed") -> tuple[bool, str]:
    """结果评分（outcome-based）。返回 (是否通过, 原因)。

    - 没有决定 → 失败（"什么都不做"绝不能算成功：这正是 τ-bench 空回复拿 38% 的教训）
    - 多于一个决定 → 失败（一边退款一边转人工，线上就是事故）
    - 决定类型、订单号、金额（±0.01 元）都要对
    - 拒绝时不检查引用的条款编号：结果对就行，不惩罚措辞（"评结果，不评路径"）
    """
    if status == "max_steps":
        return False, f"超过步数上限 {MAX_STEPS}，未作出决定"
    ds = ledger.decisions
    if not ds:
        return False, "没有作出任何决定"
    if len(ds) > 1:
        return False, f"作出了 {len(ds)} 个决定：{[d.action for d in ds]}"
    d, exp = ds[0], task.expected
    if d.order_id.strip().upper() != task.id:
        return False, f"订单号写成了 {d.order_id}"
    if d.action != exp["action"]:
        got = f"{d.action} {d.amount:.2f}" if d.action == "refund" else d.action
        want = f"refund {exp['amount']:.2f}" if exp["action"] == "refund" else exp["action"]
        return False, f"期望 {want}，实际 {got}"
    if d.action == "refund" and abs(d.amount - exp["amount"]) > 0.01:
        return False, f"金额错误：期望 {exp['amount']:.2f}，实际 {d.amount:.2f}"
    return True, "ok"


# ---------------------------------------------------------------- 一次试验（trial）


@dataclass
class TrialResult:
    task_id: str
    trial: int
    passed: bool
    reason: str
    decision: str
    tokens: int
    cost_usd: float
    latency_s: float
    error: str | None = None  # 基础设施错误（模型 API 不可用等）：不是 Agent 的错，不能记成失败


def _describe(d: Decision) -> str:
    if d.action == "refund":
        return f"refund {d.amount:.2f}"
    return f"reject {d.note}" if d.action == "reject" else "escalate"


def run_trial(llm: LLM, system_prompt: str, task: RefundTask, trial: int = 0, as_of: date | None = None) -> TrialResult:
    """跑一次试验：新账本 → 新工具 → 新 Agent → 运行 → 按终态评分。as_of 默认是运行当天。

    "每次试验一个干净环境"是 harness 的第一原则：上一次留下的状态（账本、文件、缓存）
    会让试验之间不再独立，失败会成片地相关，甚至让 Agent 通过"偷看上一轮"作弊。"""
    ledger = Ledger()
    agent = Agent(llm, make_tools(ledger), system_prompt=system_prompt, max_steps=MAX_STEPS, hooks=[StopAfterDecision(ledger)])
    t0 = time.time()
    res = agent.run(render_request(task, as_of or date.today()))
    if res.status == "failed":  # 模型调用在重试、降级之后仍然失败（ABC T.3：要识别出来，不能算作 Agent 失败）
        return TrialResult(task.id, trial, False, "infra_error", "（无）", res.usage.total, res.cost_usd,
                           round(time.time() - t0, 2), error=res.stop_reason)
    passed, reason = score(task, ledger, res.status)
    ds = ledger.decisions
    desc = ", ".join(_describe(d) for d in ds) or "（无）"
    return TrialResult(task.id, trial, passed, reason, desc, res.usage.total, res.cost_usd, round(time.time() - t0, 2))


# =====================================================================
# v0：一位同事一下午写出来的版本。结构一样，但藏着 ABC 能查出来的漏洞
# =====================================================================

ProbeAgent = Callable[[dict, dict], str]  # agent(view, tools) -> 给客户的回复


class FlawedBenchV0:
    """故意有漏洞的 v0。每个漏洞旁边标了它违反的 ABC 检查项。"""

    V0_BUILD_DATE = date(2026, 9, 19)  # v0 的标注是在这一天做的

    def __init__(self, tasks: list[RefundTask], clock: Callable[[], date] = date.today):
        self.tasks = tasks
        # ❌ T.7：标注没人复核。同事以为"7 天内"不含第 7 天，把 YS-1007 标成了拒绝（政策写的是"含第 7 天"）
        self.labels = {t.id: t.expected["action"] for t in tasks}
        self.labels["YS-1007"] = "reject"
        self.clock = clock  # ❌ T.6：签收天数按运行当天的系统日期算，环境会随时间变化
        self.ledger = Ledger()  # ❌ T.4：所有任务、所有轮次共用一个账本，从不重置

    def get_order(self, order_id: str) -> dict:
        t = next(t for t in self.tasks if t.id == order_id)
        rec = snapshot(t)
        rec["today"] = self.clock().isoformat()
        rec["_expected_action"] = self.labels[order_id]  # ❌ T.5：内部标注字段原样返回给了 Agent
        return rec

    def tools(self) -> dict[str, Callable]:
        ledger = self.ledger

        def issue_refund(order_id: str, amount: float) -> str:
            ledger.decisions.append(Decision("refund", order_id, amount))
            return "ok"

        def reject_refund(order_id: str, rule: str) -> str:
            ledger.decisions.append(Decision("reject", order_id, note=rule))
            return "ok"

        def escalate(order_id: str, reason: str) -> str:
            ledger.decisions.append(Decision("escalate", order_id, note=reason))
            return "ok"

        return {"get_order": self.get_order, "issue_refund": issue_refund, "reject_refund": reject_refund, "escalate": escalate}

    def score(self, task: RefundTask, reply: str) -> bool:
        label = self.labels[task.id]
        if label == "refund":
            return "退款" in reply  # ❌ O.b.1 / O.b.2：子串匹配。"无法退款"也包含"退款"
        if label == "reject":
            return not self.ledger.refunded(task.id)  # ❌ O.g.3：只要没退款就算对 —— 什么都不做也通过
        return any(d.action == "escalate" for d in self.ledger.decisions)  # ❌ 只查"有人转过人工"，还叠加了共享账本

    def run(self, agent: ProbeAgent) -> list[bool]:
        tools = self.tools()
        return [self.score(t, agent({"order_id": t.id, "message": t.message}, tools)) for t in self.tasks]


def run_v1(agent: ProbeAgent, tasks: list[RefundTask], as_of: date | None = None) -> list[bool]:
    """用 v1 的 harness 跑一个（非 LLM 的）探针 Agent：每个任务新账本，只给决策工具，按终态评分。"""
    out = []
    for t in tasks:
        ledger = Ledger()
        tools = {
            "issue_refund": lambda order_id, amount, L=ledger: L.decisions.append(Decision("refund", order_id, amount)),
            "reject_refund": lambda order_id, rule, L=ledger: L.decisions.append(Decision("reject", order_id, note=rule)),
            "escalate": lambda order_id, reason, L=ledger: L.decisions.append(Decision("escalate", order_id, note=reason)),
        }
        agent({"order_id": t.id, "message": t.message, "snapshot": snapshot(t, as_of)}, tools)
        out.append(score(t, ledger)[0])
    return out


# ---------------------------------------------------------------- 探针 Agent：专门用来"钻空子"


def do_nothing(view: dict, tools: dict) -> str:
    """什么都不做：空回复，不调用任何工具。任何 benchmark 都应该报告它的得分（ABC R.13）。"""
    return ""


def canned_reply(view: dict, tools: dict) -> str:
    """不看工单，永远回复同一句话，也不调用工具。"""
    return "很抱歉，暂时无法为您退款，已为您转人工客服跟进。"


def peeker(view: dict, tools: dict) -> str:
    """偷看答案：如果环境泄露了标注字段，就照着做。"""
    if "get_order" not in tools:
        return ""
    label = tools["get_order"](view["order_id"]).get("_expected_action")
    if label == "refund":
        tools["issue_refund"](view["order_id"], 0.01)  # 金额随便填，反正 v0 不检查
        return "已为您退款。"
    if label == "escalate":
        tools["escalate"](view["order_id"], "peek")
        return "已转人工。"
    return "抱歉，不符合退款条件。"


def oracle_agent(view: dict, tools: dict) -> str:
    """诚实的参考解 Agent：拿到订单事实（v1 在请求里，v0 要调 get_order），按政策决定。"""
    snap = view.get("snapshot") or tools["get_order"](view["order_id"])
    d = decide(snap)
    oid = view["order_id"]
    if d["action"] == "refund":
        tools["issue_refund"](oid, d["amount"])
        return f"已按 {d['rule']} 为您退款 {d['amount']:.2f} 元。"
    if d["action"] == "escalate":
        tools["escalate"](oid, "应退金额超过 2000 元")
        return "金额较大，已转人工审核。"
    tools["reject_refund"](oid, d["rule"])
    return f"很抱歉，按 {d['rule']} 您的申请不符合条件。"


PROBES: dict[str, ProbeAgent] = {
    "什么都不做": do_nothing,
    "固定话术": canned_reply,
    "偷看标注": peeker,
    "参考解": oracle_agent,
}
