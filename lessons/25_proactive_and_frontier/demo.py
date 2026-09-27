"""第 25 课 Demo：主动式 Agent —— 什么时候该开口，说什么，凭什么。

    python lessons/25_proactive_and_frontier/demo.py             # 真实模型：只有 3 张建议卡片调用模型（约 3 次调用）
    python lessons/25_proactive_and_frontier/demo.py --offline   # 离线：卡片用剧本（ScriptedLLM），其余部分完全相同

模拟后端工程师小林（本周值班）的一天：22 条事件（日历、邮件、工单、告警）+ 12 条状态变化（专注、开会、下班）。
七个部分：
  1. 一天的事件流
  2. 用户模型：带证据和置信度的推断（以及一条错误推断）
  3. 三种策略对比：从不打扰 / 每件事都打扰 / 决策器
  4. 决策器的一天：每条事件为什么被"现在说 / 攒着说 / 不说"
  5. 建议卡片：LLM + complete_json 生成结构化建议，并做事后检查
  6. 用户纠正错误推断之后，行为怎么变
  7. 遗忘、敏感推断与时间衰减
除了第 5 部分，其余部分都是确定性的：两种模式输出一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import unicodedata
from pathlib import Path
from types import ModuleType

from agentkit import ResilientLLM, default_llm

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（课程目录名以数字开头，没法写普通的 import）。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


pk = _load_sibling("proactive_kit")
hhmm = pk.hhmm


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 78 + f"\n  {title}\n" + "═" * 78, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, w: int) -> str:
    return text + " " * max(0, w - width(text))


def clip(text: str, w: int) -> str:
    """按显示宽度截断（中文占两格）。"""
    out = ""
    for ch in text:
        if width(out + ch) > w - 1:
            return out + "…"
        out += ch
    return out


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    info("".join(pad(h, w) for h, w in zip(headers, widths)))
    info("─" * sum(widths))
    for row in rows:
        info("".join(pad(c, w) for c, w in zip(row, widths)))


ICON = {"calendar": "📅", "email": "✉️ ", "ticket": "🎫", "alert": "🚨", "activity": "  "}
ACTION_TEXT = {"interrupt": "🔔 现在说", "defer": "📥 攒着说", "drop": "🔕 不说", "digest": "📬 推摘要"}
REASON_TEXT = {
    "worth_it": "值得，且用户有空",
    "urgent_override": "紧急：越过专注/勿扰/上限",
    "quiet_hours": "勿扰时段 → 明早摘要",
    "busy": "专注/开会中 → 空档摘要",
    "rate_limited": "60 分钟内已打扰 3 次",
    "not_worth_it": "收益 × 置信度不够",
    "urgent_low_confidence": "紧急但把握太低",
}


class CountingLLM:
    """给 LLM 套一层计数：演示结束时报告调用次数和 token 用量。"""

    def __init__(self, inner):
        self.inner, self.model = inner, inner.model
        self.calls = 0
        self.input_tokens = self.output_tokens = 0

    def chat(self, messages, tools=None, **kwargs):
        resp = self.inner.chat(messages, tools, **kwargs)
        self.calls += 1
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        return resp


# =====================================================================


def part1_events(events, truth) -> None:
    banner("1. 一天的事件流：小林，后端工程师，本周值班")
    info("Agent 能看到下面这些事件；右边的【真实需求】只有模拟用户知道（用来打分），Agent 看不到。\n")
    for e in events:
        if e.kind in pk.ACTIVITY_KINDS:
            info(f"{pad(hhmm(e.t), 11)}   ── {e.title} ──")
            continue
        tr = truth[e.id]
        need = f"【真实需求：{tr.need}，价值 {tr.value:g}】" if tr.value > 0 else "【用户其实不需要】"
        urgent = "【紧急】" if e.urgent else ""
        info(f"{pad(hhmm(e.t), 11)}{ICON[e.source]} {pad(clip(urgent + e.title, 44), 45)}{need}")
    n = sum(1 for e in events if e.kind not in pk.ACTIVITY_KINDS)
    need = sum(1 for e in events if e.kind not in pk.ACTIVITY_KINDS and truth[e.id].value > 0)
    takeaway(f"{n} 条事件里只有 {need} 条是用户真正需要的；5 条 staging 告警全是噪音（运维值班会处理）。\n"
             "      主动式 Agent 的难点不是'发现事件'，而是分辨哪些值得打扰、什么时候打扰。")


def part2_user_model(model) -> None:
    banner("2. 用户模型：每条推断都带置信度和证据")
    info("这些置信度不是写死的：每条都从先验 0.5 出发，由'上周的观察'一条条更新出来（对数几率形式的贝叶斯更新）。\n")
    rows = []
    for b in model.beliefs.values():
        flag = "（敏感）" if b.sensitive else ""
        rows.append([b.key, clip(b.statement + flag, 42), f"{b.confidence:.0%}", str(len(b.evidence))])
    table(["key", "推断", "置信度", "证据数"], rows, [24, 44, 8, 6])

    step("一条证据怎么改变置信度：logit(后验) = logit(先验) ± 证据权重")
    for prior, w, sup in [(0.5, 1.0, True), (0.5, 1.0, False), (0.769, 0.5, False), (0.2, 1.386, True), (0.999, 3.0, False)]:
        post = pk.update_belief(prior, w, sup)
        info(f"先验 {prior:<6} {'支持' if sup else '反对'}证据，权重 {w:<5} → {post:.3f}")
    info("注意第 4 行：先验 0.2（几率 1:4）遇到似然比 4 的证据（权重 ln4≈1.386），后验正好是 0.5（几率 1:1）。")
    info("注意第 5 行：置信度被夹在 [0.001, 0.999] 里。如果允许到 1.0，logit(1.0)=+∞，再多反证也拉不回来。")

    step("解释一条推断：explain(model, 'cares_staging_alerts')")
    ex = pk.explain(model, "cares_staging_alerts")
    info(ex["summary"])
    for e in ex["evidence"]:
        info(f"   {'＋' if e['supports'] else '－'} 权重 {e['weight']:<4} {e['note']}")
    takeaway("这是一条错误推断：上周是发版周，小林才频繁看 staging；平时 staging 归运维管。\n"
             "      从证据看其实有线索（第 3 条'设成免打扰'），但它的权重给低了。第 6 部分看用户怎么纠正它。")


def part3_compare(events, truth, model):
    banner("3. 三种策略对比：从不打扰 / 每件事都打扰 / 决策器")
    reports = [pk.run_day(events, truth, p, model.copy()) for p in ("never", "always", "decider")]
    headers = ["策略", "有用建议", "打扰次数", "打断专注/会议", "深夜打扰", "错过", "满意度"]
    table(headers, pk.policy_table(reports), [10, 10, 10, 16, 10, 6, 8])
    info()
    info("（'打扰次数'= 即时打扰 + 摘要推送；'打断专注/会议'和'深夜打扰'不含真正紧急的事件；")
    info(" '满意度'是代理指标：有用 +价值、打扰没用 −1、打断专注 −1.5、打断会议 −2、深夜吵醒 −3、错过 −价值×0.5（紧急 ×1）。）")

    zero = pk.Scoring(useless_interrupt=0, focus_penalty=0, meeting_penalty=0, quiet_penalty=0, digest_cost=0, digest_useless_item=0)
    alt = [pk.run_day(events, truth, p, model.copy(), scoring=zero).satisfaction for p in ("never", "always", "decider")]
    step("敏感性分析：如果打扰完全没有成本（上面的扣分全部设为 0）")
    info(f"满意度：从不打扰 {alt[0]:+.1f}，每件事都打扰 {alt[1]:+.1f}，决策器 {alt[2]:+.1f}")
    never, always, decider = reports
    takeaway(f"决策器用 {decider.notifications} 次打扰换来 {decider.useful} 条有用建议，"
             f"'每件事都打扰'用 {always.notifications} 次换来 {always.useful} 条。\n"
             "      但如果打扰不花钱，'每件事都说'反而最好 —— 最优策略取决于打扰成本。\n"
             "      所以别抄别人的阈值：打扰成本要按你的用户、你的场景去测（第 22 课的评估方法）。")
    return reports[2]


def part4_timeline(report) -> None:
    banner("4. 决策器的一天：每条事件的决定和理由")
    info("score = 收益 × 置信度 − 打扰成本 ×（专注 3 倍 / 开会 5 倍），阈值 1.0；勿扰 22:00–08:00；每小时最多 3 次。\n")
    for r in report.rows:
        if r.action in ("skip", "missed", "digest_item"):
            continue
        if r.action == "digest":
            items = next(items for t, items in report.digests if t == r.t)
            info(f"{pad(hhmm(r.t), 11)}{pad(ACTION_TEXT['digest'], 11)}{r.event.title}：" + "；".join(clip(e.title, 20) for e in items))
            continue
        score = f"{r.score:+.2f}" if r.score is not None else ""
        outcome = f"→ {r.outcome}" if r.outcome else ""
        info(f"{pad(hhmm(r.t), 11)}{pad(ACTION_TEXT[r.action], 11)}{pad(clip(r.event.title, 30), 31)}"
             f"{pad(score, 7)}{pad(REASON_TEXT.get(r.reason, r.reason), 26)}{outcome}")
    missed = [r for r in report.rows if r.action == "missed"]
    info()
    info("错过的需求：" + "；".join(f"{hhmm(r.t)} {clip(r.event.title, 22)}" for r in missed))
    takeaway("三个值得注意的时刻：\n"
             "      · 13:50 的 SEV1 在专注模式里、23:20 的 SEV2 在勿扰时段里，都因为'紧急'直接越权；\n"
             "      · 16:50 的工单本来值得说，但 60 分钟内已经打扰了 3 次，被频率上限挡下，攒到了 18:00 的摘要；\n"
             "      · 11:10 和 13:10 两次 staging 告警都是误报：错误推断的代价，就是这两次'没用的打扰'。")


def part5_cards(llm, events, model, offline: bool) -> None:
    banner("5. 建议卡片：LLM + complete_json（只建议，不执行）")
    info("只为 3 个时刻生成卡片（真实模式下 = 约 3 次模型调用）。提示词里只放和事件相关、非敏感的推断。\n")
    by_id = {e.id: e for e in events}
    jobs = [
        ([by_id["e02"]], pk.Decision("interrupt", "worth_it", 1.9), None, "08:35 会前提醒"),
        ([by_id["e11"]], pk.Decision("interrupt", "urgent_override", 7.0), None, "13:50 SEV1（专注中，越权打扰）"),
        ([by_id["e03"], by_id["e05"]], pk.Decision("defer", "busy", 0.7), pk.at("09:55"), "09:55 专注结束后的摘要"),
    ]
    for i, (evs, decision, now, label) in enumerate(jobs):
        step(f"卡片 {i + 1}：{label}")
        try:
            result = pk.make_card(llm, evs, model, decision, now=now)
        except Exception as e:  # 真实模型可能失败：演示继续，但如实报告
            info(f"❌ 生成失败：{type(e).__name__}: {str(e)[:160]}")
            continue
        c = result.card
        info(f"┌ {c.title}")
        info(f"│ {c.message}")
        info(f"│ 为什么现在：{c.why_now}")
        info(f"│ 建议下一步：{c.proposed_action}（{'可撤销' if c.reversible else '不可撤销'}）")
        info(f"└ 引用的推断：{', '.join(c.uses_beliefs) or '（无）'}")
        for w in result.warnings:
            info(f"⚠️  事后检查：{w}")
        if i == 0:
            info()
            info(f"发给模型的提示词（注意：{len(model.beliefs)} 条推断里只放了 {len(result.beliefs_sent)} 条相关的）：")
            for line in result.prompt.splitlines():
                info(f"   │ {line}")
    if not offline:
        takeaway(f"本部分共调用模型 {llm.calls} 次，输入 {llm.input_tokens} tokens、输出 {llm.output_tokens} tokens。\n"
                 "      决定'要不要打扰'的是便宜、确定的决策器；模型只负责'怎么说'。")
    else:
        takeaway("离线模式用剧本返回卡片，但走的是同一条 complete_json + 事后检查的路径。\n"
                 "      决定'要不要打扰'的是便宜、确定的决策器；模型只负责'怎么说'。")


def part6_correction(events, truth, model) -> None:
    banner("6. 用户纠正错误推断之后，行为怎么变")
    info("11:10 第一次 staging 误报时，小林点开'为什么推荐这个？'，看到的是：")
    probe = model.copy()
    pk.run_day([e for e in events if e.t < pk.at("11:10")], truth, "decider", probe)  # 跑到 11:10 之前
    info(f"   {pk.explain(probe, 'cares_staging_alerts')['summary']}")
    info("他回复：'那周是发版周，平时 staging 归运维管，我不关心。' → correct('cares_staging_alerts', False)")

    passive, corrected = model.copy(), model.copy()
    r_passive = pk.run_day(events, truth, "decider", passive)
    r_corrected = pk.run_day(events, truth, "decider", corrected, corrections={"cares_staging_alerts": False})

    step("同一天的 5 条 staging 告警：只靠隐式反馈 vs 用户显式纠正")
    staging = [e for e in events if e.kind == "staging_alert"]

    def decision_of(report, e):
        rows = [r for r in report.rows if r.event.id == e.id and r.action in ("interrupt", "defer", "drop")]
        return ACTION_TEXT[rows[0].action] if rows else ""

    table(["时间", "告警", "只靠隐式反馈", "用户纠正后"],
          [[hhmm(e.t), clip(e.title, 26), decision_of(r_passive, e), decision_of(r_corrected, e)] for e in staging],
          [8, 28, 16, 14])
    info()
    info(f"满意度：{r_passive.satisfaction:+.1f} → {r_corrected.satisfaction:+.1f}；"
         f"打扰次数：{r_passive.notifications} → {r_corrected.notifications}")
    info(f"一天结束时 cares_staging_alerts 的置信度：只靠隐式反馈 {passive.confidence('cares_staging_alerts'):.0%}，"
         f"纠正后 {corrected.confidence('cares_staging_alerts'):.0%}（已锁定）")

    step("为什么显式纠正更重要：明天发版，小林又打开了一次 staging 面板（+0.8 的支持证据）")
    for name, m in (("只靠隐式反馈", passive), ("用户纠正后", corrected)):
        m.observe("cares_staging_alerts", weight=0.8, supports=True, t=pk.at("次日 10:00"), source="activity",
                  note="次日 10:00 你打开了 staging 监控面板")
        benefit, conf, _ = pk.estimate(pk.Event("x", pk.at("次日 11:00"), "alert", "staging_alert", "staging 内存 80%"), m)
        d = pk.should_interrupt(benefit, conf, pk.Limits().base_cost, pk.Context(now=pk.at("次日 11:00")), [], pk.Limits())
        info(f"{pad(name, 14)}置信度 {conf:.0%} → 次日 11:00 的 staging 告警：{ACTION_TEXT[d.action]}（score {d.score:+.2f}）")
    takeaway("隐式反馈（'忽略了'）是弱证据：用户可能只是忙。所以它学得慢（两次误报才压下去），\n"
             "      还会被下一条弱证据推回去。给用户一个'为什么推荐这个 → 纠正'的入口，一次就改对，而且不会反弹。")


def part7_privacy(model) -> None:
    banner("7. 遗忘、敏感推断与时间衰减")
    listing = pk.Event("e99", pk.at("12:30"), "email", "listing", "租房平台：你关注的小区有 2 套新房源")
    step("敏感推断默认不进提示词：house_hunting 来自私人邮箱")
    info(f"context_for(['house_hunting']) = {model.context_for(['house_hunting'])}  ← 默认不发给模型")
    info(f"context_for(['house_hunting'], allow_sensitive=True) = {model.context_for(['house_hunting'], allow_sensitive=True)}")

    step("小林说：'别根据我的私人邮件推断任何事。' → forget(model, 'house_hunting')")
    m = model.copy()
    info(f"forget 返回 {pk.forget(m, 'house_hunting')}；再 forget 一次返回 {pk.forget(m, 'house_hunting')}")
    try:
        pk.explain(m, "house_hunting")
    except KeyError:
        info("explain(model, 'house_hunting') → KeyError：推断和它的证据都删掉了")
    again = m.observe("house_hunting", weight=0.8, supports=True, t=listing.t, source="email",
                      note="又收到租房平台邮件", statement="你最近在找房子")
    info(f"12:30 又来一封租房平台邮件，observe(...) 返回 {again}：key 在 blocked 里，连证据都不记")

    step("时间衰减：14 天没有任何新证据之后")
    m2 = model.copy()
    before = {k: m2.confidence(k) for k in ("is_oncall", "cares_staging_alerts", "wants_meeting_prep")}
    m2.decay(14)
    for k, v in before.items():
        info(f"{pad(k, 24)}半衰期 {m2.beliefs[k].half_life_days:>4g} 天：{v:.0%} → {m2.confidence(k):.0%}")
    takeaway("'本周值班'是临时状态，两周后就不该再当真；'会前要提醒'是稳定偏好，衰减得很慢。\n"
             "      用户能看（explain）、能改（correct）、能删（forget），推断会过期（decay）—— 这是主动式 Agent 的信任底线。")


def main() -> None:
    parser = argparse.ArgumentParser(description="第 25 课 Demo：主动式 Agent")
    parser.add_argument("--offline", action="store_true", help="建议卡片用离线剧本（ScriptedLLM），不调用真实模型")
    args = parser.parse_args()

    if args.offline:
        print("🔌 离线模式：建议卡片用剧本生成；其余部分是确定性模拟，与真实模式输出一致。")
        llm = pk.offline_card_llm()
    else:
        try:
            base = default_llm()
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"🌐 真实模型：{base.model}（只有第 5 部分的 3 张建议卡片调用模型）")
        llm = CountingLLM(ResilientLLM(base, max_attempts=4, base_delay=1.0))

    events, truth = pk.simulate_day()
    model = pk.initial_user_model()
    part1_events(events, truth)
    part2_user_model(model)
    decider_report = part3_compare(events, truth, model)
    part4_timeline(decider_report)
    part5_cards(llm, events, model, args.offline)
    part6_correction(events, truth, model)
    part7_privacy(model)

    banner("小结")
    info("1. 主动 = 发现需求 + 决定时机 + 说清理由。难的是后两件：什么时候不该开口。")
    info("2. 决策：收益 × 置信度 − 打扰成本（受专注、开会、勿扰时段影响），过阈值才说；值得说但不是时候 → 攒进摘要。")
    info("3. 护栏：频率上限防轰炸；紧急事件可以越权，但把握太低时不行。")
    info("4. 用户模型：每条推断带证据和置信度；显式纠正 > 隐式反馈；敏感推断默认不用；推断会过期。")
    info("5. LLM 只负责'怎么说'，并且只建议、不执行；输出要做事后检查（引用了不存在的推断？声称已执行？）。")


if __name__ == "__main__":
    main()
