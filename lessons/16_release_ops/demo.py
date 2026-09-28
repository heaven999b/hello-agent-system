"""第 16 课 Demo：发布、变更与运维 —— 一次完整的 prompt 灰度发布，跑在真实的 worker 进程上。

    python lessons/16_release_ops/demo.py             # 真实模型（场景 2 的离线回放调用真实模型，约 15 次调用）
    python lessons/16_release_ops/demo.py --offline   # 离线剧本，无需 API key

六个场景（一条完整的发布流水线）：
  1. 版本化：把 prompt 登记进 PromptRegistry —— 版本号、作者、变更说明、内容指纹、diff
  2. 影子对比：① 离线回放：同一批输入交给 v1 和 v2 各跑一遍，比较工具轨迹和输出（写操作只记录、不执行）
              ② 在线影子：用户请求由 v1 处理并立刻返回，v2 在后台跑 —— 用计时和在途数证明它不拖慢主路径、有上限、可取消
  3. 金丝雀灰度：5% → 20% → 50% → 100%。流量是 3 个真 worker 进程跑出来的，控制器用真实结果算指标、自动推进
  4. 自动回滚：v3 的新工具有 bug（代码真的抛异常），控制器从真实错误率发现它并自动回滚
  5. 紧急开关：从控制面停用一个工具，实测 3 个 worker 进程各自多久生效；在途运行怎么处理；租户只读；整体转人工
  6. 反馈闭环：一个"👎"变成一条回归评估用例

场景 3~5 共用一个"服务集群"：临时目录里的一个 SQLite 文件（任务队列 + 配置中心 + 记录表）和 3 个 worker 进程
（python -m agentkit.distributed.worker --app ops_app.py:make_handler）。控制面（这个 demo 进程）只做两件事：
往队列里放流量、往配置中心写配置；worker 进程每 0.2 秒轮询一次配置版本号。
worker 里的"模型"是剧本模型（ScriptedLLM，会读 system prompt 里的规则，每次调用 asyncio.sleep 20ms 的延迟模型），
两种模式相同 —— 这里测的是发布机制，不是模型；失败来自工具代码真实抛出的异常，指标全部从运行结果统计。
运行产物写在 runs/16_release_ops/ 下；集群的数据库和日志在临时目录里，结束后删除。
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import replace
from pathlib import Path

from configcenter import ConfigCenter
from flywheel import append_jsonl, bad_case_to_inbox, to_eval_case
from itbuddy import PROMPT_V1, PROMPT_V2, PROMPT_V3, make_tools, make_traffic, repair_traffic, scripted_model
from ops_app import RELEASE, create_tables
from registry import PromptRegistry, pick_version
from rollout import RolloutController, Thresholds, metrics_from_runs, min_sample_size, publish_release, two_proportion_test
from shadow import ShadowRunner, run_shadow, shadow_tools, summarize

from agentkit import Agent, ScriptedLLM, call_tool, default_llm, reply
from agentkit.config import env
from agentkit.distributed import SQLiteJobQueue, WorkerPool
from agentkit.evals import load_cases, rule_grader, run_eval

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNS = ROOT / "runs" / "16_release_ops"
OPS_APP = f"{HERE / 'ops_app.py'}:make_handler"
NAME = "itbuddy.system"


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


def pct(values: list[float], p: float) -> float:
    v = sorted(values)
    return v[max(1, -(-len(v) * p // 100)) - 1] if v else 0.0


# ---------------------------------------------------------------- 被发布的 Agent（定义在 itbuddy.py，worker 进程也用它）

TOOLS = make_tools()  # demo 进程里的工具：create_ticket 不接真实系统（影子模式下还会被换成替身）

SHADOW_INPUTS = [
    "VPN 连不上，报错 809，很急，我明天一早要出差",
    "怎么申请一台新显示器？",
    "帮我查一下工单 T-1001 的进度",
]

# 离线回放用的剧本：(版本, 输入) → 这次运行里模型依次给出的响应
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
    v1 = reg.publish(NAME, PROMPT_V1, model=model, author="zhang.san",
                     change_note="初版：先查知识库，查不到再建工单", params={"max_steps": 4})
    v2 = reg.publish(NAME, PROMPT_V2, model=model, author="li.si",
                     change_note="工单 #482：紧急问题跳过知识库，直接建 high 工单，缩短处理时间", params={"max_steps": 4})
    for v in reg.history(NAME):
        info(f"v{v.version}  作者 {v.author:<9} 模型 {v.model:<12} 指纹 {v.content_hash}  说明：{v.change_note}")

    step("v1 → v2 改了什么（评审时看的就是这个 diff）")
    for line in difflib.unified_diff(v1.template.splitlines(), v2.template.splitlines(), "v1", "v2", lineterm="", n=0):
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            info(line)
    r = reg.rollout(NAME)
    info(f"\n   当前流量：stable = v{r.stable}，没有进行中的灰度。发布新版本 ≠ 上线：v2 现在还没有接任何流量。")
    takeaway("版本不可变 + 发布与上线分离：回滚只是把指针拨回去，任何时候都能说清'线上跑的是哪一版、谁改的、为什么改'。")
    return reg


# =====================================================================
# 场景 2：影子对比
# =====================================================================


async def scenario_shadow(reg: PromptRegistry, offline: bool) -> None:
    banner("场景 2：影子对比 —— v2 上线前，先在同样的输入上和 v1 比一比")
    intents: list[dict] = []  # 影子工具记下的"本来要做的写操作"
    clients: list = []

    def runner(version: int):
        pv = reg.get(NAME, version)

        async def run(text: str):
            if offline:
                llm = ScriptedLLM(list(OFFLINE_SCRIPTS[(version, text)]), model=pv.model)
            else:
                llm = default_llm(pv.model)
                clients.append(llm)
            agent = Agent(llm, shadow_tools(TOOLS, intents), system_prompt=pv.template, max_steps=pv.params["max_steps"])
            return await agent.run(text, metadata={"tenant_id": "acme", "user_id": "replay", "roles": ["employee"]})

        return run

    step("① 离线回放：3 条线上真实输入，两个版本各跑一遍。两边的写工具（create_ticket）都换成了影子替身：只记录，不执行")
    try:
        diffs = await run_shadow(SHADOW_INPUTS, run_stable=runner(1), run_candidate=runner(2))
    finally:
        for c in clients:
            await c.aclose()
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

    await online_shadow(reg)


async def online_shadow(reg: PromptRegistry) -> None:
    step("② 在线影子：用户请求由 v1 处理并立刻返回；v2 在后台跑同一个输入（剧本模型 + 延迟模型：v1 每次调用 30ms，"
         "v2 每次 80ms，每 10 次里有 1 次卡 1 秒）")
    traffic = [r["input"] for r in make_traffic(60, seed=2)]
    stable_llm = ScriptedLLM(responder=scripted_model, latency=0.03, model="scripted", keep_calls=0)
    cand_llm = ScriptedLLM(responder=scripted_model, latency=lambda n: 1.0 if n % 10 == 0 else 0.08, model="scripted",
                           keep_calls=0)
    pv1, pv2 = reg.get(NAME, 1), reg.get(NAME, 2)
    meta = {"tenant_id": "acme", "user_id": "u1", "roles": ["employee"]}

    async def run_stable(text):
        return await Agent(stable_llm, TOOLS, system_prompt=pv1.template, max_steps=4).run(text, metadata=meta)

    async def run_candidate(text):
        return await Agent(cand_llm, shadow_tools(TOOLS), system_prompt=pv2.template, max_steps=4).run(text, metadata=meta)

    async def serve_all(shadow: ShadowRunner | None) -> list[float]:
        sem = asyncio.Semaphore(8)  # 这个进程同时服务 8 个用户请求
        latencies: list[float] = []

        async def one(text: str) -> None:
            async with sem:
                t0 = time.perf_counter()
                if shadow is None:
                    await run_stable(text)
                else:
                    await shadow.serve(text, run_stable)
                latencies.append((time.perf_counter() - t0) * 1000)

        await asyncio.gather(*(one(t) for t in traffic))
        return latencies

    await serve_all(None)  # 预热一遍（第一次导入、建 Schema 的开销不算进对比）
    off = await serve_all(None)
    shadow = ShadowRunner(run_candidate, max_concurrency=6, timeout_s=0.3)
    on = await serve_all(shadow)
    in_flight_at_end = shadow.in_flight
    cancelled = await shadow.aclose()  # 停机：还在跑的影子一律取消
    st = shadow.stats
    info(f"{len(traffic)} 个请求，同时服务 8 个；影子并发上限 6，每个影子最多 0.3 秒")
    info(f"{pad('', 16)}{'主路径 p50':>11}{'p95':>9}{'max':>9}")
    for name, lat in [("不开影子", off), ("开影子", on)]:
        info(f"{pad(name, 16)}{pct(lat, 50):>9.0f}ms{pct(lat, 95):>7.0f}ms{max(lat):>7.0f}ms")
    info(f"影子：启动 {st['started']}，比较完成 {st['compared']}，满了被丢弃 {st['dropped']}，超时被取消 {st['timeout']}，"
         f"在途峰值 {st['max_in_flight']}（上限 6）")
    info(f"主路径全部返回时还有 {in_flight_at_end} 个影子在跑 → 停机时取消 {cancelled} 个；之后 v2 模型的在途调用 {cand_llm.in_flight}")
    verdicts = Counter(d.verdict for d in shadow.diffs)
    info(f"比较结果：{dict(verdicts)}（candidate_error 里是超时的那几个）")
    takeaway("主路径从不等影子：影子版本更慢、会超时、满了被丢弃、停机时被取消 —— 这些都只影响'分析数据少了几条'，"
             "不影响任何一个用户。上限（并发、超时）是必须的：影子和主路径共用模型配额和这个进程的事件循环。")


# =====================================================================
# 服务集群：配置中心 + 任务队列 + 3 个 worker 进程（场景 3~5 共用）
# =====================================================================


class Fleet:
    """控制面手里的东西：一个 SQLite 文件（任务队列 + 配置中心 + 记录表）和 n 个 worker 进程。"""

    def __init__(self, workdir: Path, n: int = 3, poll: float = 0.2, concurrency: int = 32, latency: float = 0.02):
        self.workdir, self.n, self.poll = workdir, n, poll
        self.db = workdir / "ops.db"
        self.queue = SQLiteJobQueue(self.db)
        self.center = ConfigCenter(self.queue.db)  # 和队列共用一个连接
        self.pool = WorkerPool(f"sqlite:///{self.db}", OPS_APP, n=n, concurrency=concurrency, lease=30, poll=0.02, grace=10,
                               options={"poll": poll, "latency": latency}, log_dir=workdir / "logs", name="w")

    async def setup(self) -> None:
        await self.queue.setup()
        await self.center.setup()
        await self.queue.db.write(create_tables)  # 控制面先建好表（几个进程同时建表会撞锁）

    async def start(self) -> None:
        self.pool.start()
        await self.wait(lambda: len(self.pool.events("started")) >= self.n, 60, "worker 进程启动")

    async def stop(self) -> None:
        self.pool.stop()
        await self.queue.close()

    async def wait(self, cond, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = cond()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
            await asyncio.sleep(0.02)
        raise TimeoutError(f"{timeout}s 内没有等到：{what}\n{self.pool.logs()[-3000:]}")

    async def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in await self.queue.db.run(lambda c: c.execute(sql, params).fetchall())]

    async def count(self, phase: str) -> int:
        return (await self.rows("SELECT count(*) AS n FROM requests WHERE phase = ?", (phase,)))[0]["n"]

    async def submit(self, requests: list[dict], phase: str) -> None:
        for r in requests:
            await self.queue.enqueue("request", {**r, "phase": phase}, tenant_id=r["tenant"])

    async def send(self, requests: list[dict], phase: str) -> list[dict]:
        """把一批请求放进队列，等它们全部被 worker 处理完，返回 requests 表里这一批的记录。"""
        await self.submit(requests, phase)
        await self.wait(lambda: self._done(phase, len(requests)), 120, f"{phase} 的 {len(requests)} 个请求处理完")
        return await self.rows("SELECT * FROM requests WHERE phase = ?", (phase,))

    async def _done(self, phase: str, n: int) -> bool:
        return await self.count(phase) >= n

    async def propagation(self, name: str, version: int, since: float) -> dict[str, float]:
        """等到每个 worker 进程都报告看到了 name 的这个版本，返回 {worker: 看到的时刻 - since（秒）}。"""
        def seen():
            got = {}
            for e in self.pool.events("config_changed"):
                if e.get("name") == name and e.get("version", 0) >= version and e["worker_id"] not in got:
                    got[e["worker_id"]] = e["t"] - since
            return got if len(got) >= self.n else None

        return await self.wait(seen, 30, f"所有 worker 看到 {name} v{version}")


def show_propagation(lags: dict[str, float], poll: float) -> None:
    items = "  ".join(f"{w} {lag * 1000:.0f}ms" for w, lag in sorted(lags.items()))
    info(f"   配置下发：{items}（轮询间隔 {poll * 1000:.0f}ms）")


# =====================================================================
# 场景 3：金丝雀灰度（真实流量）
# =====================================================================

THRESHOLDS = Thresholds(min_requests=40, max_latency_ratio=1.5)
STAGES = (5, 20, 50, 100)
WINDOW = 400


async def run_rollout(fleet: Fleet, reg: PromptRegistry, candidate: int, *, first_seed: int, label: str) -> list[dict]:
    """一次完整的灰度：控制器按真实结果推进 / 暂停 / 回滚，每次变更写配置中心，等所有 worker 切换后再放下一批流量。"""
    ctl = RolloutController(reg, NAME, center=fleet.center, stages=STAGES, thresholds=THRESHOLDS)
    t = time.time()
    await ctl.start(candidate, force_candidate=frozenset({"u-dogfood-1", "u-dogfood-2"}))
    r = reg.rollout(NAME)
    info(f"开始灰度：candidate = v{candidate}，salt = {r.salt!r}，第一阶段 {r.percent}%，配置版本 {ctl.published}")
    show_propagation(await fleet.propagation(RELEASE, ctl.published, t), fleet.poll)
    windows = []
    for i in range(8):
        r = reg.rollout(NAME)
        if r.candidate is None:
            break
        release_v = ctl.published
        phase = f"{label}-w{i}"
        batch = await fleet.send(make_traffic(WINDOW, seed=first_seed + i), phase)
        stage_rows = await fleet.rows("SELECT * FROM requests WHERE release_v = ?", (release_v,))  # 本阶段累计
        cand = [x for x in stage_rows if x["version"] == r.candidate]
        base = [x for x in stage_rows if x["version"] == r.stable]
        stage_m, base_m = metrics_from_runs(cand), metrics_from_runs(base)
        before = r.percent
        t = time.time()
        d = await ctl.step(stage_m, base_m)
        after = reg.rollout(NAME)
        now = f"{after.percent}%" if after.candidate else (f"推全，v{after.stable} 成为 stable" if d.action == "advance"
                                                          else f"灰度终止，全部流量回到 v{after.stable}")
        on_cand = sum(1 for x in batch if x["version"] == r.candidate)
        info(f"{before:>3}% 窗口 {i + 1}：本批 {len(batch)} 个请求（v{r.candidate} {on_cand} 个），本阶段累计 v{r.candidate} "
             f"{stage_m.requests} 个：成功率 {stage_m.success_rate:.1%}（基线 {base_m.success_rate:.1%}），"
             f"错误率 {stage_m.error_rate:.1%}，p95 {stage_m.p95_latency_ms:.0f}ms（基线 {base_m.p95_latency_ms:.0f}ms）")
        info(f"         → {d.action:<8} → {now}   （{'；'.join(d.reasons)}）")
        windows.append({"percent": before, "cand": cand, "base": base, "decision": d})
        if d.action != "hold":
            show_propagation(await fleet.propagation(RELEASE, ctl.published, t), fleet.poll)
    return windows


async def scenario_canary(fleet: Fleet, reg: PromptRegistry) -> None:
    banner("场景 3：金丝雀灰度 —— 5% → 20% → 50% → 100%，用 3 个 worker 进程上的真实运行结果自动推进")
    info(f"服务集群：{fleet.n} 个 worker 进程（pid {', '.join(map(str, fleet.pool.pids))}），每个进程每 {fleet.poll}s 轮询一次配置中心")
    info("每个请求：worker 读本地配置快照 → pick_version(user_id) 选 prompt 版本 → 跑 Agent → 结果写进 requests 表")
    info(f"阈值：样本 ≥ {THRESHOLDS.min_requests}，错误率 ≤ {THRESHOLDS.max_error_rate:.0%}，成功率降幅 ≤ "
         f"{THRESHOLDS.max_success_drop:.0%}，p95 ≤ 基线 × {THRESHOLDS.max_latency_ratio}（机器负载高时延迟抖动大，放宽到 1.5）")

    users = [f"u{i:05d}" for i in range(10_000)]
    r = reg.rollout(NAME)
    step("按用户 id 稳定分桶：1 万个用户在各阶段的分布（预览，不改线上配置）")
    previous: set[str] = set()
    preview_salt = f"{NAME}:v2"
    for p in STAGES[:-1]:
        preview = replace(r, candidate=2, percent=p, salt=preview_salt)
        on_v2 = {u for u in users if pick_version(u, preview) == 2}
        kept = "全部仍在 v2 ✅" if previous <= on_v2 else "有人被甩回 v1 ❌"
        info(f"{p:>3}% → {len(on_v2):>5} 人在 v2（上一阶段的灰度用户：{kept}）")
        previous = on_v2

    step("控制器按观察窗口推进（每个窗口 400 个真实请求；每次变更写配置中心，等 3 个进程都切换后再放下一批）")
    windows = await run_rollout(fleet, reg, 2, first_seed=300, label="v2")

    step("为什么小流量阶段只能抓'灾难'，抓不了'细微变差'？")
    first = [w for w in windows if w["percent"] == STAGES[0]][-1]  # 第一阶段结束时的累计样本
    ok_c = sum(1 for x in first["cand"] if x["status"] == "completed" and x["errors"] == 0)
    ok_b = sum(1 for x in first["base"] if x["status"] == "completed" and x["errors"] == 0)
    n_c, n_b = len(first["cand"]), len(first["base"])
    diff, z, p = two_proportion_test(ok_b, n_b, ok_c, n_c)
    verdict = "差异不显著：这点样本说明不了'变好'还是'变差'" if p >= 0.05 else "p < 0.05，但样本这么小，计数上的一两次波动就能改写结论"
    info(f"{STAGES[0]}% 阶段累计：v2 {n_c} 个请求成功率 {ok_c / n_c:.1%} vs 同期 v1 {n_b} 个 {ok_b / n_b:.1%}："
         f"差 {diff:+.1%}，p 值 = {p:.2f}（{verdict}）")
    base_rate = ok_b / n_b
    n = min_sample_size(base_rate, 0.02)
    info(f"想以 80% 的把握发现'成功率从 {base_rate:.1%} 降 2 个百分点'，每组至少要 {n:,} 个样本 —— "
         f"{STAGES[0]}% 流量下要攒 {n * 100 // STAGES[0]:,} 个请求")
    takeaway("金丝雀的作用是限制'爆炸半径'，不是证明'没变差'。细微的质量回归要靠上线前的评估集、影子对比，"
             "以及有足够样本量的 A/B 实验。")


# =====================================================================
# 场景 4：自动回滚（真实流量）
# =====================================================================


async def scenario_auto_rollback(fleet: Fleet, reg: PromptRegistry, model: str) -> None:
    banner("场景 4：自动回滚 —— v3 的新工具有 bug，控制器从真实错误率里发现它")
    v3 = reg.publish(NAME, PROMPT_V3, model=model, author="wang.wu",
                     change_note="工单 #501：提到设备编号时先用 lookup_asset 查台账，减少来回追问", params={"max_steps": 4})
    await publish_release(fleet.center, reg, NAME, actor="wang.wu", reason=f"登记 v{v3.version}（还不接流量）")
    info("v3 让模型在用户提到设备编号时调用新工具 lookup_asset。这个工具只认新 CMDB 里迁移过的编号 ——")
    info("旧编号直接 KeyError（itbuddy.py 里真实的 bug）。离线评估集里恰好只有新编号，所以门禁没拦住。")
    windows = await run_rollout(fleet, reg, v3.version, first_seed=700, label="v3")
    last = windows[-1]
    if last["decision"].action == "rollback":
        failed = [x for x in last["cand"] if x["errors"] > 0]
        by_tool = Counter(t for x in failed for t in x["error_tools"].split(",") if t)
        base_by_tool = Counter(t for x in last["base"] if x["errors"] > 0 for t in x["error_tools"].split(",") if t)
        info(f"回滚依据：v3 本阶段 {len(last['cand'])} 个请求里 {len(failed)} 个中途有工具抛了异常，出错的工具 {dict(by_tool)}；"
             f"同期 v2 {len(last['base'])} 个请求出错的工具 {dict(base_by_tool)}")
        info("（worker 的 after_tool 钩子把 ToolResult.error_type 为 exception / timeout 的调用记下来 —— 都是代码真实抛出的异常）")

    step("配置中心的审计日志（谁、什么时候、改了哪个配置、为什么；所有进程共享、持久化）")
    for e in await fleet.center.audit(RELEASE):
        ro = e["after"].get("rollout", {})
        state = f"stable=v{ro.get('stable')}" + (f" candidate=v{ro['candidate']}@{ro['percent']}%" if ro.get("candidate") else "")
        info(f"v{e['version']:<3} {pad(e['actor'], 12)} {pad(state, 30)} {short(e['reason'], 60)}")
    path = RUNS / "registry.json"
    path.write_text(json.dumps(reg.export(), ensure_ascii=False, indent=2), encoding="utf-8")
    info(f"\n   PromptRegistry 的完整版本与审计记录已导出到 {path.relative_to(ROOT)}")
    takeaway("自动回滚的前提是：回滚动作本身足够便宜（移动指针 + 写一次配置）、足够快（一个轮询间隔内所有进程切换），"
             "而且指标能在几分钟内反映问题。")


# =====================================================================
# 场景 5：紧急开关（3 个 worker 进程）
# =====================================================================


async def scenario_kill_switch(fleet: Fleet) -> None:
    banner("场景 5：紧急开关 —— 从控制面停用一个工具，3 个 worker 进程各自多久生效？")
    tickets_before = (await fleet.rows("SELECT count(*) AS n FROM tickets"))[0]["n"]
    n_req, rate = 300, 200
    info(f"流量：{n_req} 个报修请求，每秒到达 {rate} 个（持续 {n_req / rate:.1f} 秒），每个都会调用写工具 create_ticket。")
    info("这一批每次模型调用 0.3 秒（延迟模型：比 0.2 秒的配置轮询间隔长 —— 真实的模型调用通常 1~5 秒），")
    info("所以开关打开的那一刻，有不少运行正跑到一半：有的还在等第一次模型调用，有的已经在建单")
    step("create_ticket 在批量重复建单 → 值班工程师在配置中心停用它（全局）")

    async def arrive() -> None:  # 请求陆续到达（而不是一次性全部入队），各个运行处在不同的阶段
        for r in repair_traffic(n_req, seed=5):
            await fleet.queue.enqueue("request", {**r, "model_latency": 0.3, "phase": "ks"}, tenant_id=r["tenant"])
            await asyncio.sleep(1 / rate)

    feeder = asyncio.create_task(arrive())
    await fleet.wait(lambda: _tickets_since(fleet, tickets_before, 40), 60, "开始建单")

    def disable(doc):
        doc["disabled_tools"] = sorted(set(doc.get("disabled_tools", [])) | {"create_ticket"})

    t_flip = time.time()  # 从发起写入的那一刻开始计时（update() 返回时事务已经提交，写入本身只要几毫秒）
    version = await fleet.center.update("flags", disable, actor="oncall.zhang", reason="create_ticket 在批量重复建单，紧急停用")
    lags = await fleet.propagation("flags", version, t_flip)
    await feeder
    await fleet.wait(lambda: _done(fleet, "ks", n_req), 120, f"{n_req} 个报修请求处理完")

    rows = await fleet.rows("SELECT * FROM requests WHERE phase = 'ks'")
    tickets = await fleet.rows("SELECT * FROM tickets WHERE id > ?", (tickets_before,))
    after_flip = [t for t in tickets if t["t"] > t_flip]
    stale = [t for t in after_flip if t["t"] > t_flip + lags[t["worker"]]]  # 该进程已经看到新开关之后还执行的
    blocked = [r for r in rows if r["blocked"] > 0]
    inflight_blocked = [r for r in blocked if r["started"] < t_flip]
    info(f"开关写入配置中心：flags v{version}")
    info(f"{pad('worker', 8)}{pad('pid', 8)}{'看到新开关':>10}{'之后仍建单':>11}{'最后一次建单':>13}{'在途被拦下':>11}")
    for w in fleet.pool.workers:
        mine = [t for t in after_flip if t["worker"] == w.worker_id]
        last = max((t["t"] - t_flip for t in mine), default=0.0)
        caught = sum(1 for r in inflight_blocked if r["worker"] == w.worker_id)
        info(f"{pad(w.worker_id, 8)}{pad(str(w.pid), 8)}{lags[w.worker_id] * 1000:>8.0f}ms{len(mine):>11}"
             f"{last * 1000:>11.0f}ms{caught:>11}")
    info("（时间都从开关写入那一刻算起；\"之后仍建单\" = 开关写入后、这个进程还没看到新开关时执行的建单）")
    worst = max(lags.values())
    info(f"所有进程生效用时：{worst * 1000:.0f}ms（上界 = 轮询间隔 {fleet.poll * 1000:.0f}ms + 一次数据库读）")
    info(f"{n_req} 个请求：建单 {len(tickets)} 张，其中 {len(after_flip)} 张在开关写入之后、执行它的进程看到新开关之前；"
         f"进程看到新开关之后执行的：{len(stale)} 张")
    info(f"被拦下 {len(blocked)} 个；其中 {len(inflight_blocked)} 个是开关写入时已经在跑的运行 —— "
         "它们在自己的下一次工具调用处被拦下（before_tool 检查）")
    info("在途策略：已经开始执行的那一次建单不会被中途打断；还没走到工具调用的运行，一律按新开关处理")

    step("修复上线，恢复工具；但 globex 租户的数据正在迁移 → 只对 globex 开启只读模式")
    version = await fleet.center.update("flags", lambda d: {"disabled_tools": [], "tenants": {"globex": {"read_only": True}}},
                                        actor="oncall.zhang", reason="修复已上线，恢复 create_ticket；globex 数据迁移期间只读")
    await fleet.propagation("flags", version, time.time())
    res = await fleet.send([{"user_id": "u00001", "tenant": "acme", "input": "3 楼打印机坏了，帮我报修"},
                            {"user_id": "u00002", "tenant": "globex", "input": "3 楼打印机坏了，帮我报修"}], "ks-tenant")
    for r in sorted(res, key=lambda x: x["tenant"]):
        info(f"[{r['tenant']}] 状态={r['status']}  被拦下的工具调用={r['blocked']}  回复：{short(r['output'], 40)}")

    step("模型供应商大面积故障，回答开始胡言乱语 → 整个 Agent 停用，转人工")
    version = await fleet.center.update("flags", lambda d: {**d, "agent_disabled": True},
                                        actor="oncall.zhang", reason="模型供应商故障，整体转人工")
    await fleet.propagation("flags", version, time.time())
    res = await fleet.send([{"user_id": "u00003", "tenant": "acme", "input": "VPN 连不上"}], "ks-off")
    info(f"[acme] 状态={res[0]['status']}（{res[0]['stop_reason']}）  回复：{res[0]['output']}")
    await fleet.center.set("flags", {}, actor="oncall.zhang", reason="故障恢复，关闭所有紧急开关")

    step("配置中心的审计日志：flags")
    for e in await fleet.center.audit("flags"):
        info(f"v{e['version']:<3} {pad(e['actor'], 14)} {pad(short(json.dumps(e['after'], ensure_ascii=False), 46), 48)} "
             f"{short(e['reason'], 40)}")
    takeaway("开关要分级：单个工具 → 某个租户只读 → 全局只读 → 整个 Agent 转人工。生效时间有明确上界（轮询间隔），"
             "要在演练里实测过，事故时才敢按。")


async def _tickets_since(fleet: Fleet, before: int, n: int) -> bool:
    return (await fleet.rows("SELECT count(*) AS n FROM tickets"))[0]["n"] - before >= n


async def _done(fleet: Fleet, phase: str, n: int) -> bool:
    return await fleet.count(phase) >= n


async def scenarios_on_fleet(reg: PromptRegistry, model: str) -> None:
    with tempfile.TemporaryDirectory(prefix="agentkit_l16_") as tmp:
        fleet = Fleet(Path(tmp))
        try:
            await fleet.setup()
            # 集群启动前，控制面先把"当前线上配置"写进配置中心：stable = v1，没有开关
            await publish_release(fleet.center, reg, NAME, actor="release-bot", reason="初始上线：v1 全量")
            await fleet.center.set("flags", {}, actor="oncall.zhang", reason="初始化：没有任何开关")
            await fleet.start()
            await scenario_canary(fleet, reg)
            await scenario_auto_rollback(fleet, reg, model)
            await scenario_kill_switch(fleet)
        finally:
            await fleet.stop()  # SIGTERM 优雅停机，超时仍不退出的 SIGKILL；临时目录随后删除


# =====================================================================
# 场景 6：反馈闭环
# =====================================================================


async def scenario_flywheel() -> None:
    banner("场景 6：反馈闭环 —— 一个 👎 变成一条回归用例（剧本模型）")
    inbox, regression = RUNS / "inbox.jsonl", RUNS / "regression_cases.jsonl"
    inbox.unlink(missing_ok=True)
    regression.unlink(missing_ok=True)

    text = "我手机 13812345678 收不到重置密码的验证码，帮我处理下"
    bad = Agent(ScriptedLLM([reply("请打开 id.example.com 自助重置密码。")]), TOOLS, name="itbuddy")
    res = await bad.run(text, metadata={"tenant_id": "acme", "user_id": "u-zhang", "roles": ["employee"]})
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
    old = await run_eval(lambda: Agent(ScriptedLLM([reply("请打开 id.example.com 自助重置密码。")]), TOOLS), cases, [rule_grader])
    fixed = await run_eval(lambda: Agent(ScriptedLLM([call_tool("create_ticket", title="收不到重置密码验证码", priority="medium"),
                                                reply("已为您建单 T-2001，IT 会人工协助重置。")]), TOOLS),
                     cases, [rule_grader])
    info(f"旧版本：{old.pass_rate:.0%} 通过   修复后：{fixed.pass_rate:.0%} 通过")
    takeaway("每个线上 bad case 都应该变成一条永久的回归用例：同一个坑，不踩第二次。")


# =====================================================================


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用剧本模型离线运行，不需要 API key")
    args = parser.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    model = "scripted" if args.offline else env("LLM_MODEL", "gpt-5.5")
    print(f"模式：{'离线剧本' if args.offline else '真实模型'}   发布对象：itbuddy.system（固定模型 {model}）")

    reg = scenario_registry(model)
    await scenario_shadow(reg, args.offline)
    await scenarios_on_fleet(reg, model)
    await scenario_flywheel()
    print()


if __name__ == "__main__":
    asyncio.run(main())
