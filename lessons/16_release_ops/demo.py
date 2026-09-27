"""第 16 课 Demo：发布、变更与运维 —— 模拟一次完整的 prompt 灰度发布。

    python lessons/16_release_ops/demo.py             # 真实模型（场景 2 的影子对比调用真实模型，约 15 次调用）
    python lessons/16_release_ops/demo.py --offline   # 离线剧本，无需 API key

六个场景（一条完整的发布流水线）：
  1. 版本化：把 prompt 登记进 PromptRegistry —— 版本号、作者、变更说明、内容指纹、diff
  2. 影子对比：同一批输入交给 v1 和 v2 各跑一遍，比较工具轨迹和输出（写操作只记录、不执行）
  3. 金丝雀灰度：1% → 10% → 50% → 100%，按指标自动推进；为什么 1% 阶段证明不了"没变差"
  4. 自动回滚：下一次发布（v3）在 10% 阶段错误率飙升，控制器自动回滚
  5. 紧急开关：停用一个工具 / 某租户只读 / 整个 Agent 转人工，全部不用重启
  6. 反馈闭环：一个"👎"变成一条回归评估用例

场景 3~6 的线上指标和用户行为是剧本模拟的（真实模式下也一样），这样每次运行结果都可复现。
运行产物写在 runs/16_release_ops/ 下。
"""

from __future__ import annotations

import argparse
import difflib
import json
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Literal

from flywheel import append_jsonl, bad_case_to_inbox, to_eval_case
from killswitch import KillSwitch
from registry import PromptRegistry, pick_version
from rollout import RolloutController, StageMetrics, Thresholds, min_sample_size, two_proportion_test
from shadow import run_shadow, shadow_tools, summarize

from agentkit import Agent, ScriptedLLM, call_tool, default_llm, reply, tool
from agentkit.config import env
from agentkit.evals import load_cases, rule_grader, run_eval

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs" / "16_release_ops"


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 72 + f"\n  {title}\n" + "═" * 72, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def short(text: str | None, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


# ---------------------------------------------------------------- 被发布的 Agent：IT 服务台

KB = {
    "vpn": "VPN 连不上：确认没有连访客 Wi-Fi；用工号登录 GlobalConnect；报错 809 时重启客户端。",
    "显示器": "申请显示器：在 IT 门户提交'外设申请'，主管审批后 3 个工作日内发放。",
    "密码": "忘记密码：打开 id.example.com 自助重置，需要手机验证码。",
}


@tool
def search_kb(query: str) -> str:
    """搜索 IT 知识库，返回最相关的文章"""
    q = query.lower()
    hits = [text for key, text in KB.items() if key in q]
    return "\n".join(hits) or "没有找到相关文章"


@tool
def get_ticket_status(ticket_id: str) -> str:
    """查询工单的处理状态"""
    return f"{ticket_id}：处理中，负责人 李工，预计今天 18:00 前完成"


@tool(risk="write")
def create_ticket(title: str, priority: Literal["low", "medium", "high"]) -> str:
    """创建一张 IT 工单，返回工单号"""
    return f"已创建工单 T-2001（{priority}）：{title}"


TOOLS = [search_kb, get_ticket_status, create_ticket]

PROMPT_V1 = """你是 ACME 公司的 IT 服务台助手。
规则：
1. 先用 search_kb 查知识库，按文章内容给出步骤。
2. 知识库解决不了时，再用 create_ticket 建工单，priority 用 medium。
3. 回答简洁，不超过 80 字。"""

PROMPT_V2 = """你是 ACME 公司的 IT 服务台助手。
规则：
1. 先用 search_kb 查知识库，按文章内容给出步骤。
2. 知识库解决不了时，再用 create_ticket 建工单，priority 用 medium。
3. 用户说明情况紧急（如"很急""马上要用"），或问题影响多人时，不要查知识库，直接用 create_ticket 建 high 优先级工单，并告知工单号。
4. 回答简洁，不超过 80 字。"""

SHADOW_INPUTS = [
    "VPN 连不上，报错 809，很急，我明天一早要出差",
    "怎么申请一台新显示器？",
    "帮我查一下工单 T-1001 的进度",
]

# 离线剧本：(版本, 输入) → 这次运行里模型依次给出的响应
OFFLINE_SCRIPTS = {
    (1, SHADOW_INPUTS[0]): [call_tool("search_kb", query="vpn 809"),
                            reply("请确认没连访客 Wi-Fi，用工号登录 GlobalConnect；报错 809 就重启客户端。")],
    (2, SHADOW_INPUTS[0]): [call_tool("create_ticket", title="VPN 报错 809 无法连接（明早出差）", priority="high"),
                            reply("已为您创建高优先级工单 T-2001，工程师会尽快联系您。")],
    (1, SHADOW_INPUTS[1]): [call_tool("search_kb", query="显示器"),
                            reply("在 IT 门户提交'外设申请'，主管审批后 3 个工作日内发放。")],
    (2, SHADOW_INPUTS[1]): [call_tool("search_kb", query="显示器"),
                            reply("请在 IT 门户提交'外设申请'，主管审批后 3 个工作日内发放。")],
    (1, SHADOW_INPUTS[2]): [call_tool("get_ticket_status", ticket_id="T-1001"),
                            reply("T-1001 处理中，负责人李工，预计今天 18:00 前完成。")],
    (2, SHADOW_INPUTS[2]): [call_tool("get_ticket_status", ticket_id="T-1001"),
                            reply("T-1001 正在处理，负责人李工，预计今天 18:00 前完成。")],
}


# =====================================================================
# 场景 1：版本化
# =====================================================================


def scenario_registry(model: str) -> PromptRegistry:
    banner("场景 1：prompt 当成可发布的制品来管理 —— 版本、作者、变更说明、指纹")
    reg = PromptRegistry()
    v1 = reg.publish("itbuddy.system", PROMPT_V1, model=model, author="zhang.san",
                     change_note="初版：先查知识库，查不到再建工单", params={"max_steps": 4})
    v2 = reg.publish("itbuddy.system", PROMPT_V2, model=model, author="li.si",
                     change_note="工单 #482：紧急问题跳过知识库，直接建 high 工单，缩短处理时间", params={"max_steps": 4})
    for v in reg.history("itbuddy.system"):
        info(f"v{v.version}  作者 {v.author:<9} 模型 {v.model:<12} 指纹 {v.content_hash}  说明：{v.change_note}")

    step("v1 → v2 改了什么（评审时看的就是这个 diff）")
    for line in difflib.unified_diff(v1.template.splitlines(), v2.template.splitlines(), "v1", "v2", lineterm="", n=0):
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            info(line)
    r = reg.rollout("itbuddy.system")
    info(f"\n   当前流量：stable = v{r.stable}，没有进行中的灰度。发布新版本 ≠ 上线：v2 现在还没有接任何流量。")
    takeaway("版本不可变 + 发布与上线分离：回滚只是把指针拨回去，任何时候都能说清'线上跑的是哪一版、谁改的、为什么改'。")
    return reg


# =====================================================================
# 场景 2：影子对比
# =====================================================================


def scenario_shadow(reg: PromptRegistry, offline: bool) -> None:
    banner("场景 2：影子对比 —— v2 上线前，先在同样的输入上和 v1 比一比")
    intents: list[dict] = []  # 影子工具记下的"本来要做的写操作"

    def runner(version: int):
        pv = reg.get("itbuddy.system", version)

        def run(text: str):
            llm = ScriptedLLM(list(OFFLINE_SCRIPTS[(version, text)]), model=pv.model) if offline else default_llm(pv.model)
            agent = Agent(llm, shadow_tools(TOOLS, intents), system_prompt=pv.template, max_steps=pv.params["max_steps"])
            return agent.run(text, metadata={"tenant_id": "acme", "user_id": "replay", "roles": ["employee"]})

        return run

    info("回放 3 条线上真实输入。两边的写工具（create_ticket）都换成了影子替身：只记录，不执行。")
    diffs = run_shadow(SHADOW_INPUTS, run_stable=runner(1), run_candidate=runner(2))
    for d in diffs:
        step(short(d.input, 40))
        info(f"v1 工具：{d.stable.tool_names}   →   v2 工具：{d.candidate.tool_names}")
        info(f"v1 输出：{short(d.stable.output)}")
        info(f"v2 输出：{short(d.candidate.output)}")
        info(f"文本相似度 {d.similarity:.2f}   判定：{d.verdict}  {'；'.join(d.notes)}")
    s = summarize(diffs)
    step("汇总（贴进发布评审单）")
    info(f"判定分布 {s['verdicts']}   平均相似度 {s['avg_similarity']:.2f}   成本比 v2/v1 = {s['cost_ratio']:.2f}")
    info(f"影子模式拦下的写操作意图：{[i['tool'] for i in intents]}（一个都没有真的执行）")
    takeaway("影子对比回答的是'行为变了没有、变在哪'，不回答'变好了没有'：tools_changed 的那条正是 v2 想要的改变，"
             "其他两条应该 equivalent。是好是坏要靠评估集和人来判断。")


# =====================================================================
# 场景 3：金丝雀灰度（模拟指标）
# =====================================================================

BASELINE = StageMetrics(requests=24_000, success_rate=0.91, error_rate=0.012, p95_latency_ms=9_200, cost_per_task=0.021)


def scenario_canary(reg: PromptRegistry) -> None:
    banner("场景 3：金丝雀灰度 —— 1% → 10% → 50% → 100%，按指标自动推进（指标为模拟数据）")
    users = [f"u{i:05d}" for i in range(10_000)]
    ctl = RolloutController(reg, "itbuddy.system", thresholds=Thresholds(min_requests=200))
    ctl.start(2, force_candidate=frozenset({"u-dogfood-1", "u-dogfood-2"}))
    r = reg.rollout("itbuddy.system")
    info(f"开始灰度：candidate = v2，salt = {r.salt!r}，内部员工 {sorted(r.force_candidate)} 强制使用 v2")

    step("按用户 id 稳定分桶：1 万个用户在各阶段的分布")
    previous: set[str] = set()
    for p in (1, 10, 50):
        preview = replace(r, percent=p)  # 复制一份只用来"预览"分布，不改线上配置
        on_v2 = {u for u in users if pick_version(u, preview) == 2}
        kept = "全部仍在 v2 ✅" if previous <= on_v2 else "有人被甩回 v1 ❌"
        info(f"{p:>3}% → {len(on_v2):>5} 人在 v2（上一阶段的灰度用户：{kept}）")
        previous = on_v2
    info(f"同一个用户 u00042 查 3 次：{[reg.resolve('itbuddy.system', 'u00042').version for _ in range(3)]}（粘性）")

    step("控制器按观察窗口推进（每个窗口结束时调用一次 step）")
    windows = [
        StageMetrics(requests=80, success_rate=0.93, error_rate=0.0, p95_latency_ms=8_900, cost_per_task=0.020),
        StageMetrics(requests=260, success_rate=239 / 260, error_rate=0.012, p95_latency_ms=9_000, cost_per_task=0.021),
        StageMetrics(requests=2_450, success_rate=0.915, error_rate=0.011, p95_latency_ms=9_100, cost_per_task=0.021),
        StageMetrics(requests=12_100, success_rate=0.917, error_rate=0.012, p95_latency_ms=9_300, cost_per_task=0.022),
    ]
    for w in windows:
        before = reg.rollout("itbuddy.system").percent
        d = ctl.step(w, BASELINE)
        after = reg.rollout("itbuddy.system")
        now = "推全，v2 成为 stable" if after.candidate is None else f"{after.percent}%"
        info(f"{before:>3}% 窗口：{w.requests:>6} 个请求，成功率 {w.success_rate:.1%}，错误率 {w.error_rate:.1%} "
             f"→ {d.action:<8} → {now}   （{'；'.join(d.reasons)}）")

    step("为什么 1% 阶段只能抓'灾难'，抓不了'细微变差'？")
    base_ok, base_n, canary_ok, canary_n = 21_840, 24_000, 239, 260  # 基线 91.0%，1% 阶段 239/260
    diff, z, p = two_proportion_test(base_ok, base_n, canary_ok, canary_n)
    info(f"1% 阶段：{canary_n} 个请求成功率 {canary_ok / canary_n:.1%} vs 基线 {base_ok / base_n:.1%}："
         f"差 {diff:+.1%}，p 值 = {p:.2f}（远大于 0.05，说明不了任何事）")
    n = min_sample_size(0.91, 0.02)
    info(f"想以 80% 的把握发现'成功率下降 2 个百分点'，每组至少要 {n:,} 个样本 —— 1% 流量下要攒好几天")
    takeaway("金丝雀的作用是限制'爆炸半径'，不是证明'没变差'。细微的质量回归要靠上线前的评估集、影子对比，"
             "以及有足够样本量的 A/B 实验。")


# =====================================================================
# 场景 4：自动回滚（模拟指标）
# =====================================================================


def scenario_auto_rollback(reg: PromptRegistry, model: str) -> None:
    banner("场景 4：自动回滚 —— v3 在 10% 阶段出事了（指标为模拟数据）")
    reg.publish("itbuddy.system", PROMPT_V2 + "\n5. 输出 JSON 格式的结构化回答。", model=model, author="wang.wu",
                change_note="工单 #501：回答改成 JSON，方便前端渲染", params={"max_steps": 4})
    ctl = RolloutController(reg, "itbuddy.system")
    ctl.start(3)
    windows = [
        StageMetrics(requests=240, success_rate=0.905, error_rate=0.015, p95_latency_ms=9_400, cost_per_task=0.022),
        StageMetrics(requests=2_300, success_rate=0.84, error_rate=0.093, p95_latency_ms=9_800, cost_per_task=0.023),
    ]
    for w in windows:
        before = reg.rollout("itbuddy.system").percent
        d = ctl.step(w, BASELINE)
        after = reg.rollout("itbuddy.system")
        state = f"{after.percent}%" if after.candidate else f"灰度终止，全部流量回到 v{after.stable}"
        info(f"{before:>3}% 窗口：错误率 {w.error_rate:.1%}，成功率 {w.success_rate:.1%} → {d.action} → {state}")
        if d.action == "rollback":
            info(f"回滚原因：{'；'.join(d.reasons)}")

    step("审计日志（谁、什么时候、做了什么、为什么）")
    for e in reg.audit_log:
        detail = {k: v for k, v in e.items() if k not in ("ts", "action", "prompt", "actor", "note", "hash")}
        info(f"{pad(e['action'], 14)} by {pad(e['actor'], 12)} {json.dumps(detail, ensure_ascii=False)}")
    path = RUNS / "registry.json"
    path.write_text(json.dumps(reg.export(), ensure_ascii=False, indent=2), encoding="utf-8")
    info(f"\n   完整的版本与审计记录已导出到 {path.relative_to(ROOT)}")
    takeaway("自动回滚的前提是：回滚动作本身足够便宜（移动指针）、足够快（配置下发），而且指标能在几分钟内反映问题。")


# =====================================================================
# 场景 5：紧急开关
# =====================================================================


def scenario_kill_switch() -> None:
    banner("场景 5：紧急开关 —— 出事时几秒内止血，不发布、不重启（剧本模型）")
    flags: dict = {}  # 模拟配置中心
    tickets: list[str] = []

    @tool(risk="write")
    def create_ticket(title: str, priority: Literal["low", "medium", "high"]) -> str:
        """创建一张 IT 工单，返回工单号"""
        tickets.append(title)
        return f"已创建工单 T-{2000 + len(tickets)}"

    tools = [search_kb, create_ticket]

    def ask(tenant: str, text: str, script: list) -> None:
        agent = Agent(ScriptedLLM(script), tools, hooks=[KillSwitch(lambda: flags, tools)])
        res = agent.run(text, metadata={"tenant_id": tenant, "user_id": "u1", "roles": ["employee"]})
        denied = [m["content"] for m in res.messages if m["role"] == "tool" and ("停用" in m["content"] or "只读" in m["content"])]
        info(f"[{tenant}] {text}")
        info(f"    状态={res.status}  工单库={tickets}  {('拦截：' + short(denied[0], 50)) if denied else ''}")
        info(f"    回复：{short(res.output)}")

    make = lambda: [call_tool("create_ticket", title="打印机卡纸", priority="low"), reply("已为您创建工单。")]  # noqa: E731
    step("平时：正常建工单")
    ask("acme", "3 楼打印机卡纸了，帮我报修", make())
    step("发现 create_ticket 在批量重复建单 → 值班工程师在配置中心停用它（全局）")
    flags["disabled_tools"] = ["create_ticket"]
    ask("acme", "3 楼打印机卡纸了，帮我报修", [call_tool("create_ticket", title="打印机卡纸", priority="low"),
                                           reply("报修功能暂时停用，请稍后再试或拨打 IT 热线。")])
    step("修复上线，恢复工具；但 globex 租户的数据正在迁移 → 只对 globex 开启只读模式")
    flags["disabled_tools"] = []
    flags["tenants"] = {"globex": {"read_only": True}}
    ask("globex", "帮我报修打印机", [call_tool("create_ticket", title="打印机", priority="low"),
                                reply("系统维护中，暂时不能创建工单，查询类问题可以继续问我。")])
    ask("acme", "帮我报修打印机", make())
    step("模型供应商大面积故障，回答开始胡言乱语 → 整个 Agent 停用，转人工")
    flags["agent_disabled"] = True
    ask("acme", "VPN 连不上", [])
    takeaway("开关要分级：单个工具 → 某个租户只读 → 全局只读 → 整个 Agent 转人工。每一级都要提前演练过，事故时才敢按。")


# =====================================================================
# 场景 6：反馈闭环
# =====================================================================


def scenario_flywheel() -> None:
    banner("场景 6：反馈闭环 —— 一个 👎 变成一条回归用例（剧本模型）")
    inbox, regression = RUNS / "inbox.jsonl", RUNS / "regression_cases.jsonl"
    inbox.unlink(missing_ok=True)
    regression.unlink(missing_ok=True)

    text = "我手机 13812345678 收不到重置密码的验证码，帮我处理下"
    bad = Agent(ScriptedLLM([reply("请打开 id.example.com 自助重置密码。")]), TOOLS, name="itbuddy")
    res = bad.run(text, metadata={"tenant_id": "acme", "user_id": "u-zhang", "roles": ["employee"]})
    info(f"用户：{text}")
    info(f"Agent（v2）：{res.output}")
    info("用户点了 👎，理由：'我说了收不到验证码，还让我自助重置'")

    record = bad_case_to_inbox(res, text, {"reason": "not_helpful", "comment": "我说了收不到验证码，还让我自助重置"},
                               prompt_version="itbuddy.system@v2")
    append_jsonl(inbox, record)
    step(f"① 进入待标注收件箱 {inbox.relative_to(ROOT)}（已脱敏、去掉 user_id）")
    info(f"input = {record['input']}")
    info(f"metadata = {record['metadata']}   tags = {record['tags']}")

    step("② 人工标注：收不到验证码 = 自助重置走不通，正确做法是建工单，而不是再推一遍自助重置")
    record["expect"] = {"must_call": ["create_ticket"], "status": "completed"}
    case = to_eval_case(record)
    append_jsonl(regression, {k: getattr(case, k) for k in ("id", "input", "expect", "metadata", "tags")})
    info(f"写入回归评估集 {regression.relative_to(ROOT)}：expect = {case.expect}")

    step("③ 下次发布前跑评估：旧行为过不了，修好的新版本才能过")
    cases = load_cases(regression)
    old = run_eval(lambda: Agent(ScriptedLLM([reply("请打开 id.example.com 自助重置密码。")]), TOOLS), cases, [rule_grader])
    fixed = run_eval(lambda: Agent(ScriptedLLM([call_tool("create_ticket", title="收不到重置密码验证码", priority="medium"),
                                                reply("已为您建单 T-2001，IT 会人工协助重置。")]), TOOLS),
                     cases, [rule_grader])
    info(f"旧版本：{old.pass_rate:.0%} 通过   修复后：{fixed.pass_rate:.0%} 通过")
    takeaway("每个线上 bad case 都应该变成一条永久的回归用例：同一个坑，不踩第二次。")


# =====================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用剧本模型离线运行，不需要 API key")
    args = parser.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    model = "scripted" if args.offline else env("LLM_MODEL", "gpt-5.5")
    print(f"模式：{'离线剧本' if args.offline else '真实模型'}   发布对象：itbuddy.system（固定模型 {model}）")

    reg = scenario_registry(model)
    scenario_shadow(reg, args.offline)
    scenario_canary(reg)
    scenario_auto_rollback(reg, model)
    scenario_kill_switch()
    scenario_flywheel()
    print()


if __name__ == "__main__":
    main()
