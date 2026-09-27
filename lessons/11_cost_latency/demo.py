"""第 11 课 Demo：成本与延迟优化 —— 把账单和等待时间都降下来。

    python lessons/11_cost_latency/demo.py             # 真实模型（读取 .env，约 20 次模型调用）
    python lessons/11_cost_latency/demo.py --offline   # 离线剧本（ScriptedLLM），无需 API key

五个场景：
  1. 精确缓存：同一批请求，开 / 关缓存的成本和延迟对比；不同租户互不命中；TTL 过期
  2. 级联：工单分类，"先小模型、不合格再升级" vs "全部用大模型"
  3. 对冲请求：模拟长尾延迟，对比 p50 / p90 / p99 和多花的钱
  4. 上下文膨胀：只追加 / 提示词缓存 / 滑动窗口 的成本曲线（纯计算，不调用模型）
  5. 成本归因：用场景 1 导出的 trace 算出每个租户、每个功能的成本与单位经济，检查预算

模型：大模型 = .env 的 LLM_MODEL；小模型 = LLM_SMALL_MODEL（没配就用 LLM_FALLBACK_MODEL）。
价格：用下面 DEMO_PRICES 里的**示例单价**（不是任何厂商的报价），只为了让成本对比有数可算。
运行产物（trace）写在 runs/11_cost_latency/ 下。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from costkit import CachingLLM, CascadeLLM, ResponseCache, budget_alerts, context_cost, cost_report, hedged_call

from agentkit import Agent, ScriptedLLM, Tracer, default_llm, jsonl_exporter, render_tree, reply
from agentkit.config import env
from agentkit.pricing import PRICES, estimate_cost
from agentkit.types import LLMResponse

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs" / "11_cost_latency"


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 72 + f"\n  {title}\n" + "═" * 72, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def short(text: str | None, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def pad(text: str, width: int) -> str:
    """按"显示宽度"左对齐：中文字符在终端里占两个英文字符宽，直接用 :<N 会对不齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def usd(x: float) -> str:
    return f"${x:.5f}"


def percentile(values: list[float], p: float) -> float:
    """最近秩法百分位数（第 07 课练习里的定义）。"""
    v = sorted(values)
    rank = max(1, -(-len(v) * p // 100))  # ceil(p * N / 100)
    return v[int(rank) - 1]


# ---------------------------------------------------------------- 模型与价格


class SlowLLM:
    """离线模式下给剧本模型加上"模拟延迟"，让延迟对比有意义。真实模式不用它。"""

    def __init__(self, inner, seconds: float):
        self.inner, self.seconds, self.model = inner, seconds, inner.model

    def chat(self, messages, tools=None, **kwargs):
        time.sleep(self.seconds)
        return self.inner.chat(messages, tools, **kwargs)


def setup_models(offline: bool) -> tuple[str, str]:
    if offline:
        large, small = "demo-large", "demo-small"
    else:
        large = env("LLM_MODEL", "gpt-5.5")
        small = env("LLM_SMALL_MODEL") or env("LLM_FALLBACK_MODEL") or large
    # 示例单价（美元 / 百万 token）：(输入, 输出, 缓存命中的输入)。假设大模型单价是小模型的 10 倍。
    # 这不是任何厂商的真实报价 —— 换成你自己的合同价，结论的"形状"不会变。
    PRICES[large] = (2.50, 20.00, 0.25)
    if small != large:
        PRICES[small] = (0.25, 2.00, 0.025)
    return large, small


class Tap:
    """透明地记录"哪些工单真的走到了这个模型"，用来在表格里标出级联的每一条走的是小模型还是大模型。"""

    def __init__(self, inner):
        self.inner, self.model, self.seen = inner, inner.model, set()

    def chat(self, messages, tools=None, **kwargs):
        self.seen.add(messages[-1]["content"])
        return self.inner.chat(messages, tools, **kwargs)


def llm_for(model: str, offline: bool, script_fn=None, latency: float = 0.3):
    if not offline:
        return default_llm(model)
    return SlowLLM(ScriptedLLM([script_fn] * 200, model=model), latency)


# =====================================================================
# 场景 1：精确缓存
# =====================================================================

KB = """知识库：
- VPN 连不上：先确认没有连在访客 Wi-Fi 上；打开 GlobalConnect 客户端用工号登录；报错 809 时重启客户端。
- 申请显示器：在 IT 门户提交"外设申请"，主管审批后 3 个工作日内发放。
- 忘记密码：打开 id.example.com 自助重置，需要手机验证码。"""
SYSTEM = f"你是企业 IT 服务台助手。只根据知识库回答，不超过 40 个字。\n{KB}"

# (租户, 功能, 问题)。真实的企业流量里，FAQ 类问题的重复度非常高 —— 这正是精确缓存的用武之地
WORKLOAD = [
    ("acme", "faq", "VPN 连不上怎么办？"),
    ("acme", "faq", "怎么申请新显示器？"),
    ("acme", "faq", "VPN 连不上怎么办？"),
    ("globex", "faq", "VPN 连不上怎么办？"),  # 同样的问题，不同的租户：绝不能命中 acme 的缓存
    ("acme", "faq", "VPN 连不上怎么办？"),
    ("globex", "summary", "把这句话总结成 10 个字以内的工单标题：3 楼东侧打印机从早上开始一直卡纸，已经重启过两次。"),
    ("globex", "faq", "VPN 连不上怎么办？"),
]

OFFLINE_ANSWERS = {
    "VPN 连不上怎么办？": "先确认没连访客 Wi-Fi，再用工号登录 GlobalConnect；报错 809 就重启客户端。",
    "怎么申请新显示器？": "在 IT 门户提交外设申请，主管审批后 3 个工作日内发放。",
}


def offline_faq(messages) -> LLMResponse:
    q = messages[-1]["content"]
    if q in OFFLINE_ANSWERS:
        return reply(OFFLINE_ANSWERS[q], input_tokens=380, output_tokens=40)
    return reply("3 楼打印机持续卡纸", input_tokens=400, output_tokens=300)  # 推理型模型：输出里含大量推理 token


def serve(workload, llm_for_tenant, tracer: Tracer | None = None, cache: ResponseCache | None = None):
    rows = []
    for tenant, feature, question in workload:
        llm = llm_for_tenant(tenant)
        agent = Agent(llm, [], system_prompt=SYSTEM, name="it-helpdesk", tracer=tracer or Tracer())
        hits_before = cache.stats.hits if cache else 0
        t0 = time.time()
        with agent.tracer.span("request", **{"tenant.id": tenant, "app.feature": feature}):
            res = agent.run(question, metadata={"tenant_id": tenant, "user_id": "u1", "roles": ["employee"]})
        rows.append({
            "tenant": tenant, "feature": feature, "q": question, "res": res,
            "hit": bool(cache and cache.stats.hits > hits_before), "latency": time.time() - t0,
        })
    return rows


def scenario_cache(offline: bool, large: str) -> Path:
    banner("场景 1：精确缓存 —— 同样的请求，不再付第二次钱")
    base = llm_for(large, offline, offline_faq)

    step(f"对照组：不开缓存，依次处理 {len(WORKLOAD)} 个请求")
    plain = serve(WORKLOAD, lambda tenant: base)
    plain_cost = sum(r["res"].cost_usd for r in plain)
    plain_time = sum(r["latency"] for r in plain)
    info(f"模型调用 {len(plain)} 次，总成本 {usd(plain_cost)}，总耗时 {plain_time:.1f}s")

    step("实验组：开启精确缓存（ResponseCache 全局共享，CachingLLM 按请求创建，作用域 = 租户 + 角色）")
    now = [0.0]  # 缓存用假时钟：待会儿要"快进 11 分钟"演示 TTL 过期
    cache = ResponseCache(ttl_s=600, clock=lambda: now[0])
    trace_path = RUNS / "traces.jsonl"
    trace_path.unlink(missing_ok=True)
    tracer = Tracer(exporter=jsonl_exporter(trace_path))

    def cached_llm(tenant: str) -> CachingLLM:
        return CachingLLM(base, cache, scope={"tenant_id": tenant, "roles": ["employee"]})

    rows = serve(WORKLOAD, cached_llm, tracer, cache)
    info(f"{'#':<3}{pad('租户', 9)}{pad('问题', 44)}{pad('缓存', 10)}{'耗时':>5}{'成本':>11}")
    for i, r in enumerate(rows, 1):
        mark = "✅ 命中" if r["hit"] else "· 未命中"
        info(f"{i:<3}{pad(r['tenant'], 9)}{pad(short(r['q'], 22), 44)}{pad(mark, 10)}{r['latency']:>6.2f}s{usd(r['res'].cost_usd):>11}")
    cost = sum(r["res"].cost_usd for r in rows)
    hit_lat = [r["latency"] for r in rows if r["hit"]]
    miss_lat = [r["latency"] for r in rows if not r["hit"]]
    s = cache.stats
    info(f"\n   命中率 {s.hit_rate:.0%}（{s.hits}/{s.hits + s.misses}），省下 {s.saved.input_tokens}+{s.saved.output_tokens} token")
    saving = 1 - cost / plain_cost
    info(f"成本：{usd(plain_cost)} → {usd(cost)}（-{saving:.0%}）")
    info(f"平均延迟：不开缓存 {plain_time / len(plain):.2f}s → 开缓存 {statistics.mean(hit_lat + miss_lat):.2f}s"
         f"（命中 {statistics.mean(hit_lat):.2f}s / 未命中 {statistics.mean(miss_lat):.2f}s）")
    if saving < s.hit_rate - 0.1:
        info(f"⚠️ 命中率 {s.hit_rate:.0%}，成本却只降了 {saving:.0%}：命中的都是便宜的 FAQ，最贵的请求没有重复。"
             "缓存省多少钱，看的是'重复流量占成本的比例'，不是命中率。")
    if not offline:
        cached_in = sum(r["res"].usage.cached_input_tokens for r in plain + rows)
        info(f"（厂商侧提示词缓存命中的输入 token：{cached_in}。网关不返回这个字段时恒为 0，它和本场景的应用层缓存是两回事）")

    hit_run = next(r["res"] for r in rows if r["hit"])
    step("命中缓存的那次运行，trace 长这样（llm.chat 的 token 是 0 → 0：这次调用没花钱）")
    for line in render_tree(hit_run.trace).splitlines():
        info(line)

    step("第 4 个请求：globex 问了和 acme 一字不差的问题")
    info(f"globex 的第一次 VPN 提问：{'命中（❌ 泄露！）' if rows[3]['hit'] else '未命中 ✅'} —— 缓存键里有租户，两家公司的缓存天然隔离")

    step("⏩ 11 分钟过去了（TTL = 10 分钟），acme 再问一次 VPN")
    now[0] += 11 * 60
    again = serve([WORKLOAD[0]], cached_llm, tracer, cache)[0]
    info(f"结果：{'命中' if again['hit'] else '未命中（条目已过期，重新调用模型）'}；过期条目数 {cache.stats.expired}")
    takeaway("精确缓存最适合'高重复 + 答案稳定'的流量（FAQ、分类、固定模板）。键里必须有租户和权限上下文，TTL 决定你能容忍多旧的答案。")
    return trace_path


# =====================================================================
# 场景 2：级联
# =====================================================================

CATEGORIES = ["network", "hardware", "account", "software", "other"]
CLASSIFY_PROMPT = (
    "你是 IT 工单分类器。只输出一个 JSON 对象，不要任何其他文字：\n"
    '{"category": "network|hardware|account|software|other", "priority": "P1|P2|P3", '
    '"confidence": 0 到 1 之间的小数，表示你对分类的把握}\n'
    "P1 = 影响多人或有紧急截止时间；P3 = 不紧急。"
)
TICKETS = [
    "VPN 从今早开始一直连不上，报错 809，整个销售部都受影响",
    "新员工小王明天入职，需要开通邮箱和 OA 账号",
    "3 楼打印机又卡纸了",
    "Excel 一打开大文件就崩溃",
    "电脑最近有点怪，时快时慢，可能是网络问题，也可能是中毒了，说不清",
    "会议室投影仪连不上笔记本，下午两点要给客户做演示",
]

# 离线剧本：小模型前 4 个答得好；第 5 个自己都没把握；第 6 个没按格式输出 → 这两个会被升级
SMALL_OUT = {
    TICKETS[0]: '{"category": "network", "priority": "P1", "confidence": 0.95}',
    TICKETS[1]: '{"category": "account", "priority": "P2", "confidence": 0.93}',
    TICKETS[2]: '{"category": "hardware", "priority": "P3", "confidence": 0.9}',
    TICKETS[3]: '{"category": "software", "priority": "P3", "confidence": 0.88}',
    TICKETS[4]: '{"category": "network", "priority": "P3", "confidence": 0.55}',
    TICKETS[5]: "这个应该是硬件问题，而且比较急。",
}
LARGE_OUT = {
    TICKETS[0]: '{"category": "network", "priority": "P1", "confidence": 0.97}',
    TICKETS[1]: '{"category": "account", "priority": "P2", "confidence": 0.95}',
    TICKETS[2]: '{"category": "hardware", "priority": "P3", "confidence": 0.93}',
    TICKETS[3]: '{"category": "software", "priority": "P3", "confidence": 0.9}',
    TICKETS[4]: '{"category": "software", "priority": "P2", "confidence": 0.7}',
    TICKETS[5]: '{"category": "hardware", "priority": "P1", "confidence": 0.9}',
}


def scripted(table: dict, tokens: tuple[int, int]):
    return lambda messages: reply(table[messages[-1]["content"]], input_tokens=tokens[0], output_tokens=tokens[1])


def parse_ticket(content: str | None) -> dict | None:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # 有的模型爱加 ```json 代码块
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def ticket_validator(messages, response) -> bool:
    """确定性校验（几乎零成本）：格式对、取值合法、自报置信度够高。"""
    d = parse_ticket(response.content)
    if d is None or d.get("category") not in CATEGORIES or d.get("priority") not in ("P1", "P2", "P3"):
        return False
    try:
        return float(d.get("confidence", 0)) >= 0.8
    except (TypeError, ValueError):
        return False


def classify_all(llm) -> list[tuple[LLMResponse, float]]:
    def one(ticket: str):
        t0 = time.time()
        r = llm.chat([{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": ticket}])
        return r, time.time() - t0

    with ThreadPoolExecutor(max_workers=3) as pool:  # 单机网关：并发控制在 3 路以内
        return list(pool.map(one, TICKETS))


def scenario_cascade(offline: bool, large: str, small: str) -> None:
    banner("场景 2：级联 —— 先让小模型试，不合格再升级到大模型")
    if small == large:
        info(f"⚠️ 没有配置单独的小模型（LLM_SMALL_MODEL / LLM_FALLBACK_MODEL），大小都用 {large}：只能演示机制，看不出省钱。")
    info(f"小模型：{small}（示例单价 ${PRICES[small][0]}/${PRICES[small][1]} 每百万 token）  "
         f"大模型：{large}（${PRICES[large][0]}/${PRICES[large][1]}）")
    big = llm_for(large, offline, scripted(LARGE_OUT, (160, 30)), latency=0.6)
    little = llm_for(small, offline, scripted(SMALL_OUT, (160, 30)), latency=0.2)

    step(f"方案 A：{len(TICKETS)} 个工单全部交给大模型")
    base = classify_all(big)
    base_cost = sum(estimate_cost(r.usage, large) for r, _ in base)

    step("方案 B：级联（validator = JSON 格式 + 取值合法 + 自报置信度 ≥ 0.8）")
    big_tap = Tap(big)
    cascade = CascadeLLM(little, big_tap, ticket_validator)
    casc = classify_all(cascade)
    casc_cost = estimate_cost(cascade.small_usage, small) + estimate_cost(cascade.large_usage, large)

    info(f"{pad('工单', 46)}{pad('全用大模型', 16)}{pad('级联', 20)}耗时 A→B")
    agree = agree_p = 0
    for ticket, (ra, ta), (rb, tb) in zip(TICKETS, base, casc):
        da, db = parse_ticket(ra.content) or {}, parse_ticket(rb.content) or {}
        same = da.get("category") == db.get("category")
        agree += same
        agree_p += da.get("priority") == db.get("priority")
        via = "大" if ticket in big_tap.seen else "小"
        a_txt = f"{da.get('category', '?')}/{da.get('priority', '?')}"
        b_txt = f"{db.get('category', '?')}/{db.get('priority', '?')}（{via}）"
        info(f"{pad(short(ticket, 22), 46)}{pad(a_txt, 16)}{pad(b_txt, 20)}{ta:.1f}s → {tb:.1f}s")

    info("")
    info(f"升级率：{cascade.escalation_rate:.0%}（{cascade.escalations}/{cascade.calls}），原因：{dict(cascade.reasons)}")
    info(f"被丢弃的小模型输出：{cascade.wasted.total} token —— 白花了，但照样计费")
    info(f"成本：A {usd(base_cost)}  vs  B {usd(casc_cost)}（{casc_cost / base_cost - 1:+.0%}）")
    info(f"平均延迟：A {statistics.mean(t for _, t in base):.2f}s  vs  B {statistics.mean(t for _, t in casc):.2f}s")
    info(f"与'全用大模型'的结果一致：类别 {agree}/{len(TICKETS)}，优先级 {agree_p}/{len(TICKETS)}"
         "（不一致的部分就是级联的质量代价，要用评估集持续盯住）")
    ratio = PRICES[small][0] / PRICES[large][0]
    takeaway(f"级联划算的条件：小模型成本 + 升级率 × 大模型成本 < 大模型成本，即升级率 < 1 - {ratio:.2f} = {1 - ratio:.0%}。"
             "升级的那部分请求延迟是'小 + 大'，所以升级率高时，级联在延迟上也会输。")


# =====================================================================
# 场景 3：对冲请求
# =====================================================================


class LongTailLLM:
    """模拟一个有长尾的模型服务：90% 的请求 20~60ms，10% 卡在 500ms（排队、冷启动、慢节点）。
    时间都缩小了约 50 倍，让 Demo 几秒内跑完；形状和真实的长尾分布一样。"""

    model = "long-tail-sim"

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        with self.lock:
            self.calls += 1
            delay = 0.5 if self.rng.random() < 0.10 else self.rng.uniform(0.02, 0.06)
        time.sleep(delay)
        return reply("ok")


def scenario_hedging(offline: bool, large: str) -> None:
    banner("场景 3：对冲请求 —— 用一点点额外成本，砍掉延迟长尾（模拟）")
    n = 60
    msgs = [{"role": "user", "content": "ping"}]

    def run(hedge: bool) -> tuple[list[float], int]:
        llm = LongTailLLM(seed=7)
        latencies = []
        for _ in range(n):
            t0 = time.time()
            if hedge:
                hedged_call(lambda: llm.chat(msgs), hedge_after_s=0.08)
            else:
                llm.chat(msgs)
            latencies.append((time.time() - t0) * 1000)
        return latencies, llm.calls

    plain, plain_calls = run(False)
    hedged, hedged_calls = run(True)
    info(f"{pad('', 14)}{'p50':>8}{'p90':>8}{'p99':>8}   调用次数")
    for name, lat, calls in [("不对冲", plain, plain_calls), ("80ms 后对冲", hedged, hedged_calls)]:
        info(f"{pad(name, 14)}{percentile(lat, 50):>6.0f}ms{percentile(lat, 90):>6.0f}ms{percentile(lat, 99):>6.0f}ms{calls:>9}")
    info(f"多花的钱：{hedged_calls / plain_calls - 1:+.0%} 的调用量")
    takeaway("对冲阈值设在 p90~p95 附近：只有最慢的那一小撮请求会多发一次，却能把 p99 拉回接近 p90。")

    if not offline:
        step("真实模型上试一次：阈值故意设成 1.5 秒（低于这个模型的中位延迟）")
        llm = default_llm(large)
        real = [{"role": "user", "content": "用一句话说明什么是对冲请求。"}]
        out = hedged_call(lambda: llm.chat(real), hedge_after_s=1.5)
        info(f"发出 {out.launched} 个请求，第 {out.winner + 1} 个先回来，耗时 {out.elapsed_s:.1f}s：{short(out.value.content, 50)}")
        takeaway("阈值低于中位延迟 = 几乎每个请求都付双份钱，却几乎没有收益。阈值必须来自你自己的延迟分布（第 07 课的 p95）。")


# =====================================================================
# 场景 4：上下文膨胀的成本曲线
# =====================================================================


def scenario_context_growth(large: str) -> None:
    banner("场景 4：上下文膨胀 —— 为什么步数翻倍，账单不止翻倍（纯计算）")
    base, growth, window, r = 3000, 800, 12000, 0.1
    info(f"假设：system + 工具定义 {base} token；每步新增 {growth} token（一次工具调用 + 结果）；")
    info(f"      提示词缓存命中价 = 正常输入价 × {r}；滑动窗口上限 {window} token；输入单价 ${PRICES[large][0]}/百万 token")
    info("")
    info(f"{pad('步数', 6)}{pad('只追加', 14)}{pad('只追加+缓存', 14)}{pad('滑动窗口', 14)}{pad('窗口+缓存', 14)}")
    for n in (5, 10, 20, 40):
        rows = [
            context_cost(n, base=base, growth=growth),
            context_cost(n, base=base, growth=growth, cache_ratio=r),
            context_cost(n, base=base, growth=growth, window=window),
            context_cost(n, base=base, growth=growth, window=window, cache_ratio=r),
        ]
        cells = "".join(f"{row['billed']:<14,}" for row in rows)
        info(f"{n:<6}{cells}")
    worst = context_cost(40, base=base, growth=growth)["billed"] * PRICES[large][0] / 1e6
    info("")
    info(f"（单位：折算成全价的输入 token。40 步只追加 ≈ {usd(worst)} / 次运行，一天 1 万次就是 ${worst * 10000:,.0f}）")
    takeaway("只追加时总量 ≈ N·S + d·N²/2，是平方增长。缓存把'重复的前缀'打一折，窗口把增长压成线性，"
             "但窗口一滑动，前缀就变了，缓存只剩 system + 工具定义能命中 —— 两者不能简单叠加。")


# =====================================================================
# 场景 5：成本归因与预算
# =====================================================================


def scenario_attribution(trace_path: Path) -> None:
    banner("场景 5：成本归因 —— 钱花在了哪个租户、哪个功能上？")
    spans = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    info(f"读取 {trace_path.relative_to(ROOT)}：{len(spans)} 个 span（场景 1 开启缓存那一组导出的 trace）")

    by_tenant = cost_report(spans, ("tenant.id",))
    by_feature = cost_report(spans, ("tenant.id", "app.feature"))
    step("按租户")
    for (tenant,), row in sorted(by_tenant.items()):
        info(f"{tenant:<8} 运行 {row['runs']} 次  成功 {row['completed']} 次  成本 {usd(row['cost_usd'])}  "
             f"每次成功任务成本 {usd(row['cost_per_success'] or 0)}")
    step("按 租户 × 功能")
    for (tenant, feature), row in sorted(by_feature.items()):
        info(f"{tenant:<10} {feature:<10} 成本 {usd(row['cost_usd'])}")

    step("预算检查（示例预算：acme $0.006 / 月，globex $0.003 / 月；80% 预警，100% 超限）")
    costs = {t: row["cost_usd"] for (t,), row in by_tenant.items()}
    alerts = budget_alerts(costs, {"acme": 0.006, "globex": 0.003})
    for tenant, level, ratio in alerts:
        icon = "🔴 超限" if level == "exceeded" else "🟡 预警"
        info(f"{icon} {tenant}：已用 {ratio:.0%}")
    if not alerts:
        info("没有租户触发预警")
    takeaway("成本数据只有'能归因'才有用：租户和功能标签必须在请求入口由服务端打上，并贯穿 trace、账单和告警。")


# =====================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用剧本模型离线运行，不需要 API key")
    args = parser.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)

    large, small = setup_models(args.offline)
    print(f"模式：{'离线剧本（延迟为模拟值）' if args.offline else '真实模型'}   大模型：{large}   小模型：{small}")
    print("价格：demo 内置的示例单价（不是任何厂商报价），只用于对比")

    trace_path = scenario_cache(args.offline, large)
    scenario_cascade(args.offline, large, small)
    scenario_hedging(args.offline, large)
    scenario_context_growth(large)
    scenario_attribution(trace_path)
    print()


if __name__ == "__main__":
    main()
