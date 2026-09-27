"""第 30 课 Demo：异步运行时与高并发服务 —— 用可测量的数字证明 agentkit.aio 做到了什么。

    python lessons/30_async_runtime/demo.py                    # 场景 1–5（剧本模型）+ 场景 6（真实模型，正常 8 次调用）
    python lessons/30_async_runtime/demo.py --offline          # 只跑场景 1–5，不调用模型
    python lessons/30_async_runtime/demo.py --offline --full   # 场景 1 的"同步串行"也跑满 200 个会话（约 80 秒）
    python lessons/30_async_runtime/demo.py --only 3,4         # 只跑指定场景

六个场景：
  1. 吞吐：同一批 200 个会话（每个 2 次模型调用 × 0.2 秒），同步串行 / 同步 + 16 线程 / 同步 + 200 线程 / AsyncAgent 单进程。
     每种方式在独立子进程里跑，测耗时、吞吐、峰值并发、内存；再单独测"一个等待者"的成本：线程 vs 协程
  2. 并行工具：3 个只读工具串行 vs 并行；写工具仍然严格按顺序
  3. 阻塞事件循环的代价：async 钩子里 time.sleep（错误）vs asyncio.to_thread（正确），测心跳延迟和其他会话的完成时间
  4. 取消：FastAPI SSE 端点 /chat/stream，客户端读到第一个工具事件后断开 → 服务端运行被取消（检查点 cancelled、在途模型调用归零）；
     本课发现、已在 agentkit.aio 修复（修了两次）的缺陷：根因的最小复现 → 取消时刻扫描（第一次修复 vs 现在）
     → SSE 端到端断开 20 次（模拟异步存储；有 pgserver 时再用第 26 课的 AsyncPostgresCheckpointer 连嵌入式 Postgres）
     → 复测时的新发现：Python 3.11 的 asyncio.wait_for 会吞掉取消；最后用同一个 run_id 从检查点恢复
  5. 舱壁：一个租户 50 个请求、另一个租户 2 个 —— 只有全局上限 / 按租户舱壁 / 舱壁 + 等待上限（快速失败）
  6. 真实模型：AsyncOpenAICompatLLM + AsyncResilientLLM(max_concurrency=3)：1 次流式会话（TTFT）+ 6 个并发会话

场景 4 需要 fastapi、uvicorn、httpx；缺少时打印安装命令并跳过该场景，demo 仍以退出码 0 结束。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import gc
import importlib.util
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:  # 直接 python lessons/.../demo.py 运行时也能 import agentkit
    sys.path.insert(0, str(REPO))

from agentkit import Agent, ToolContext, call_tool, call_tools, reply, tool  # noqa: E402
from agentkit.hooks import Hook  # noqa: E402
from agentkit.state import RunState  # noqa: E402
from agentkit.aio import (  # noqa: E402
    AsyncAgent,
    AsyncResilientLLM,
    AsyncScriptedLLM,
    KeyedLimiter,
    RunFinished,
    RunStarted,
    StreamDone,
    TextDelta,
    ToolFinished,
    ToolStarted,
    maybe_await,
)

INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'
LATENCY = 0.2  # 场景 1：每次"模型调用"的模拟耗时（秒）
CALLS_PER_SESSION = 2  # 场景 1：每个会话 = 调一次模型拿到工具调用 + 执行工具 + 再调一次模型给答案
SERVICE_TIME = LATENCY * CALLS_PER_SESSION  # 利特尔法则里的 W：一个会话占用"一个并发名额"的时长


def banner(title: str) -> None:
    print("\n" + "═" * 78 + f"\n  {title}\n" + "═" * 78, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def pct(values: list[float], p: float) -> float:
    """最近秩法取百分位（样本少时比插值更直观）。"""
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[k]


def dist(values: list[float]) -> str:
    return f"p50 {pct(values, 50):.2f}s ｜ p95 {pct(values, 95):.2f}s ｜ max {max(values):.2f}s"


def peak_rss_bytes() -> int | None:
    """进程的峰值常驻内存（RSS）。macOS 的 ru_maxrss 单位是字节，Linux 是 KB；Windows 没有 resource 模块。"""
    try:
        import resource
    except ImportError:
        return None
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024


def mb(n: int | None) -> str:
    return "n/a" if n is None else f"{n / 1024 / 1024:.1f} MB"


@contextlib.contextmanager
def capture_logs(logger_name: str, sink: list[str], level: int = logging.WARNING):
    """把某个 logger 的记录收进 sink（不打印），退出时还原。"""

    class Grab(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f" | {type(record.exc_info[1]).__name__}"
            sink.append(text)

    logger = logging.getLogger(logger_name)
    handler = Grab(level=level)
    logger.addHandler(handler)
    old_propagate, logger.propagate = logger.propagate, False
    try:
        yield sink
    finally:
        logger.removeHandler(handler)
        logger.propagate = old_propagate


def has(*modules: str) -> bool:
    return all(importlib.util.find_spec(m) is not None for m in modules)


# ==================================================================================== 场景 1：吞吐


@tool
def lookup_ticket(ticket_id: str) -> str:
    """查询工单状态"""
    return f"{ticket_id}：处理中，已分配给二线支持"


@tool(name="lookup_ticket")
async def alookup_ticket(ticket_id: str) -> str:
    """查询工单状态"""
    return f"{ticket_id}：处理中，已分配给二线支持"


def ticket_responder(messages):
    """剧本：第一轮调用工具查工单，看到工具结果后给出答案。根据当前对话决定回复，几百个会话并发也不会串台。"""
    if messages[-1]["role"] == "tool":
        return reply(f"查到了：{messages[-1]['content']}")
    return call_tool("lookup_ticket", ticket_id=messages[-1]["content"].split()[-1])


class SyncSlowLLM:
    """同步版的"带延迟剧本模型"：time.sleep 模拟等模型（和真实的同步 HTTP 客户端一样，整个线程卡住），
    线程安全地统计在途调用数。与 AsyncScriptedLLM 一样把每次调用的消息深拷贝进 calls，保证内存对比公平。"""

    model = "scripted"

    def __init__(self, responder, latency: float):
        self.responder, self.latency = responder, latency
        self.calls: list[dict] = []
        self.in_flight = self.max_in_flight = 0
        self._lock = threading.Lock()

    def chat(self, messages, tools=None, **kwargs):
        with self._lock:
            self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "kwargs": kwargs})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.latency)
            item = copy.deepcopy(self.responder(messages))
            item.model = item.model or self.model
            return item
        finally:
            with self._lock:
                self.in_flight -= 1


def bench_sessions(mode: str, n: int) -> dict:
    """在当前（独立的子）进程里跑一种并发方式，返回测量结果。

    不开 tracemalloc：它会让每次内存分配变慢好几倍，把计时数字扭曲得面目全非（实测 AsyncAgent 200 会话从 0.5s 变成 1.0s）。
    内存用进程的峰值 RSS 增量（ru_maxrss），它包含线程栈，正是"每会话一个线程"真正的内存代价。
    计时前先空转约 0.2 秒预热：在这台繁忙的 8 核 M1 上，不预热时同一段代码的 CPU 时间实测能差到 2 倍。"""
    end = time.perf_counter() + 0.2
    while time.perf_counter() < end:
        pass
    base_rss = peak_rss_bytes()
    cpu0, t0 = time.process_time(), time.perf_counter()
    if mode == "async":
        llm = AsyncScriptedLLM(responder=ticket_responder, latency=LATENCY)
        agent = AsyncAgent(llm, [alookup_ticket])  # 同一个实例被 n 个会话并发复用

        async def main():
            return await asyncio.gather(*(agent.run(f"查一下工单 T{i:04d}") for i in range(n)))

        results = asyncio.run(main())
    else:
        llm = SyncSlowLLM(ticket_responder, LATENCY)
        agent = Agent(llm, [lookup_ticket])
        if mode == "serial":
            results = [agent.run(f"查一下工单 T{i:04d}") for i in range(n)]
        else:
            with ThreadPoolExecutor(max_workers=int(mode.removeprefix("threads"))) as pool:
                results = list(pool.map(lambda i: agent.run(f"查一下工单 T{i:04d}"), range(n)))
    elapsed, cpu = time.perf_counter() - t0, time.process_time() - cpu0
    peak = peak_rss_bytes()
    return {
        "mode": mode,
        "n": n,
        "ok": sum(r.ok for r in results),
        "crossed": sum(r.messages[1]["content"] != f"查一下工单 T{i:04d}" for i, r in enumerate(results)),
        "elapsed": elapsed,
        "max_in_flight": llm.max_in_flight,
        "cpu": cpu,
        "rss_delta": None if base_rss is None else peak - base_rss,
        "threads_peak": threading.active_count(),
    }


def bench_waiters(kind: str, n: int) -> dict:
    """一个"正在等 IO 的会话"最少要花多少内存：n 个阻塞在 Event 上的线程 vs n 个 await Event 的协程。"""
    base = peak_rss_bytes()
    t0 = time.perf_counter()
    if kind == "threads":
        ev = threading.Event()
        threads = [threading.Thread(target=ev.wait) for _ in range(n)]
        for t in threads:
            t.start()
        created, peak = time.perf_counter() - t0, peak_rss_bytes()
        ev.set()
        for t in threads:
            t.join()
    else:

        async def main():
            ev = asyncio.Event()
            tasks = [asyncio.create_task(ev.wait()) for _ in range(n)]
            await asyncio.sleep(0)  # 让所有任务真正跑到 await 处挂起
            result = time.perf_counter() - t0, peak_rss_bytes()
            ev.set()
            await asyncio.gather(*tasks)
            return result

        created, peak = asyncio.run(main())
    return {"kind": kind, "n": n, "create_s": created, "rss_delta": None if base is None else peak - base}


def run_child(*args: str) -> dict:
    """在独立子进程里跑一次测量：每种方式的内存互不干扰（RSS 峰值只增不减，同一进程里没法分开测）。"""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args],
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(REPO),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"子进程失败：{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def scenario_throughput(args) -> None:
    banner("场景 1：吞吐 —— 同一批会话，四种并发方式（外加 1000 个会话看扩展性）")
    n = 200
    serial_n = n if args.full else 20
    info(f"每个会话：模型调用 {CALLS_PER_SESSION} 次 × {LATENCY}s + 1 次工具调用，所以单个会话的服务时间 W ≈ {SERVICE_TIME:.1f}s")
    info(f"每种方式都在一个全新的子进程里跑（内存数字互不干扰）；{'重复 ' + str(args.repeat) + ' 次取耗时中位数' if args.repeat > 1 else '各跑 1 次（机器繁忙时加 --repeat 3）'}。")
    if not args.full:
        info(f"同步串行只跑 {serial_n} 个会话再按线性外推（跑满 200 个要约 {n * SERVICE_TIME:.0f}s；加 --full 可实测）。")

    rows = [
        ("A. 同步 Agent 串行", "serial", serial_n, 1),
        ("B. 同步 Agent + 16 线程", "threads16", n, 16),
        ("C. 同步 Agent + 200 线程", "threads200", n, 200),
        ("D. AsyncAgent 单进程单线程", "async", n, n),
        ("E. AsyncAgent，1000 个会话", "async", 1000, 1000),
    ]
    step("方式                           会话     耗时   吞吐(会话/s)  峰值在途  利特尔预测  CPU 时间   RSS 增量")
    measured = {}
    for label, mode, count, conc in rows:
        runs = [run_child("--_bench", mode, str(count)) for _ in range(1 if mode == "serial" else args.repeat)]
        r = sorted(runs, key=lambda x: x["elapsed"])[len(runs) // 2]  # 多次运行取耗时的中位数
        measured[label[0]] = r
        thr = r["n"] / r["elapsed"]
        predicted = min(conc, r["n"]) / SERVICE_TIME  # 利特尔法则：吞吐 λ = L / W
        note = "" if r["n"] != serial_n or mode != "serial" or args.full else f"  → 外推 200 个 ≈ {r['elapsed'] / r['n'] * n:.0f}s"
        info(
            f"{label:<27}{r['n']:>5}{r['elapsed']:>8.2f}s{thr:>12.1f}{r['max_in_flight']:>9}{predicted:>11.1f}"
            f"{r['cpu']:>9.2f}s{mb(r['rss_delta']):>11}{note}"
        )
        assert r["ok"] == r["n"] and r["crossed"] == 0, "每个会话都应成功，且不串台"
    info("峰值在途 = 同一时刻真实在等模型的调用数（剧本模型自己计数）；利特尔预测 = min(并发, 会话数) / W；")
    info("CPU 时间 = 整个进程消耗的 CPU 秒数（所有线程之和）；RSS 增量 = 运行期间峰值常驻内存的增长。")

    a, t16, big = measured["D"], measured["B"], measured["E"]
    takeaway(
        f"利特尔法则 L = λ × W：W 固定为 {SERVICE_TIME:.1f}s 时，吞吐只取决于'同时在飞'的会话数 L。"
        f"16 线程 → L=16 → 最多 {16 / SERVICE_TIME:.0f} 会话/s；AsyncAgent 让 L 达到 {a['max_in_flight']}，实测 {a['n'] / a['elapsed']:.0f} 会话/s，"
        f"是 16 线程的 {t16['elapsed'] / a['elapsed']:.1f} 倍。"
    )
    info(
        f"    D 没有达到预测的 {n / SERVICE_TIME:.0f} 会话/s：事件循环是单线程的，每个会话还要花 CPU（校验、深拷贝、追踪），"
        f"E 里约 {big['cpu'] / big['n'] * 1000:.2f}ms/会话 —— 单核 CPU 是 asyncio 的第二个天花板。"
    )
    info("    反过来做容量估算：每秒 50 个请求 × 每个 8 秒 = 需要 400 个并发名额 —— 这就是'为什么必须异步'的那笔账。")

    step("一个'正在等 IO 的会话'要花多少内存？（线程 vs 协程，各自在独立子进程里测）")
    for kind, count in (("threads", 2000), ("tasks", 2000), ("tasks", 50000)):
        r = run_child("--_waiters", kind, str(count))
        per = "n/a" if r["rss_delta"] is None else f"{r['rss_delta'] / r['n'] / 1024:.1f} KB/个"
        label = "线程（阻塞在 Event.wait）" if kind == "threads" else "协程（await Event.wait）"
        info(f"{r['n']:>6} 个{label:<22} 创建耗时 {r['create_s'] * 1000:>7.0f} ms ｜ RSS 增量 {mb(r['rss_delta']):>9} ｜ {per}")
    takeaway("200 个线程在这个规模上也能跑出接近的吞吐 —— 线程不是不能用；差别在'每个等待者的成本'、取消语义和上万并发时的扩展性。")


# ==================================================================================== 场景 2：并行工具


tool_log: list[tuple[str, float]] = []


@tool
async def search_kb(query: str) -> str:
    """在知识库里搜索（只读）"""
    tool_log.append(("search_kb 开始", time.perf_counter()))
    await asyncio.sleep(0.3)
    return f"关于「{query}」的 3 篇文章"


@tool
async def get_user_profile(user: str) -> str:
    """查询用户资料（只读）"""
    tool_log.append(("get_user_profile 开始", time.perf_counter()))
    await asyncio.sleep(0.3)
    return f"{user}：研发部，MacBook Pro"


@tool
async def get_ticket_history(user: str) -> str:
    """查询用户的历史工单（只读）"""
    tool_log.append(("get_ticket_history 开始", time.perf_counter()))
    await asyncio.sleep(0.3)
    return f"{user}：近 30 天 2 张 VPN 工单"


@tool(risk="write")
async def update_ticket(ticket_id: str, note: str) -> str:
    """更新工单（写操作）"""
    tool_log.append((f"update_ticket({note}) 开始", time.perf_counter()))
    await asyncio.sleep(0.1)
    tool_log.append((f"update_ticket({note}) 结束", time.perf_counter()))
    return "ok"


async def run_tools_once(calls, tools, parallel: bool) -> tuple[float, list]:
    tool_log.clear()
    llm = AsyncScriptedLLM([call_tools(*calls), reply("好的")])
    t0 = time.perf_counter()
    res = await AsyncAgent(llm, tools, parallel_tools=parallel).run("VPN 又连不上了，帮我看看")
    assert res.ok
    return time.perf_counter() - t0, [(name, t - t0) for name, t in tool_log]


def scenario_parallel_tools(args) -> None:
    banner("场景 2：并行工具 —— 只读工具并行，写工具串行")
    reads = [("search_kb", {"query": "VPN 证书过期"}), ("get_user_profile", {"user": "alice"}), ("get_ticket_history", {"user": "alice"})]
    tools = [search_kb, get_user_profile, get_ticket_history, update_ticket]
    for parallel in (False, True):
        elapsed, log = asyncio.run(run_tools_once(reads, tools, parallel))
        starts = "，".join(f"{name.split()[0]} @{t * 1000:.0f}ms" for name, t in log)
        info(f"parallel_tools={parallel!s:<5}  3 个只读工具（各 0.3s）总耗时 {elapsed:.2f}s ｜ 开始时刻：{starts}")
    writes = [("update_ticket", {"ticket_id": "T1", "note": "第1步"}), ("update_ticket", {"ticket_id": "T1", "note": "第2步"})]
    elapsed, log = asyncio.run(run_tools_once(writes, tools, True))
    info(f"parallel_tools=True   2 个写工具：{' → '.join(name for name, _ in log)}（{elapsed:.2f}s）")
    mixed = reads[:2] + writes[:1]
    elapsed, _ = asyncio.run(run_tools_once(mixed, tools, True))
    info(f"parallel_tools=True   2 读 + 1 写混在一轮：{elapsed:.2f}s —— 只要有一个写工具，整轮都按顺序执行")
    takeaway("只读工具并行，一轮的耗时从'求和'变成'取最大'；写工具保持模型给出的顺序，副作用才可预测。")


# ==================================================================================== 场景 3：阻塞事件循环


AUDIT_WRITE = 0.3  # legacy 租户的审计写入耗时（同步驱动）


class LegacyAuditHook(Hook):
    """每次模型调用后写一条审计记录。legacy 租户的审计库只有同步驱动 —— 错误做法是直接在 async 函数里调用它。"""

    def __init__(self, blocking: bool):
        self.blocking = blocking

    async def after_llm(self, state, response):
        if state.metadata.get("tenant_id") != "legacy":
            return None
        if self.blocking:
            time.sleep(AUDIT_WRITE)  # ❌ 同步 IO：这 0.3 秒里整个事件循环停住，所有会话一起卡
        else:
            await asyncio.to_thread(time.sleep, AUDIT_WRITE)  # ✅ 放进线程池，事件循环继续推进别的会话
        return None


async def blocking_experiment(blocking: bool) -> dict:
    lags: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():  # 每 10ms 醒一次；醒得越晚，说明事件循环被卡得越久
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t0 - 0.01)

    llm = AsyncScriptedLLM(responder=ticket_responder, latency=0.1)
    agent = AsyncAgent(llm, [alookup_ticket], hooks=[LegacyAuditHook(blocking)])
    hb = asyncio.create_task(heartbeat())
    t0 = time.perf_counter()
    done: dict[str, float] = {}

    async def one(tenant: str, i: int):
        await agent.run(f"查一下工单 T{i}", metadata={"tenant_id": tenant})
        done[f"{tenant}{i}"] = time.perf_counter() - t0

    await asyncio.gather(*(one("acme", i) for i in range(20)), *(one("legacy", i) for i in range(2)))
    stop.set()
    await hb
    normal = [v for k, v in done.items() if k.startswith("acme")]
    return {"lag_max": max(lags), "lag_p99": pct(lags, 99), "stalls": sum(x > 0.05 for x in lags), "normal": normal}


def scenario_blocking(args) -> None:
    banner("场景 3：在 async 函数里调用阻塞 IO —— 一个租户的慢操作拖慢所有人")
    info("22 个会话同时跑：20 个普通租户（每个 2 次模型调用 × 0.1s，理想耗时 0.2s）+ 2 个 legacy 租户；")
    info(f"legacy 租户每次模型调用后要写一次审计（同步驱动，{AUDIT_WRITE}s）。一个心跳协程每 10ms 醒一次。")
    for blocking in (True, False):
        r = asyncio.run(blocking_experiment(blocking))
        label = "❌ 钩子里直接 time.sleep       " if blocking else "✅ await asyncio.to_thread(...)"
        info(
            f"{label} 心跳最大延迟 {r['lag_max'] * 1000:>5.0f}ms（p99 {r['lag_p99'] * 1000:>4.0f}ms，卡顿 >50ms 的次数 {r['stalls']}）"
            f" ｜ 普通租户完成时间 {dist(r['normal'])}"
        )

    step("打开 asyncio 调试模式，让事件循环自己报告'慢回调'（默认阈值 100ms）")
    records: list[str] = []
    with capture_logs("asyncio", records):
        asyncio.run(blocking_experiment(True), debug=True)
    slow = [m for m in records if "took" in m and "seconds" in m]
    info(f"调试模式记录到 {len(slow)} 条慢回调警告，例如（省略了中间的协程栈信息）：")
    for m in slow[:2]:
        task = re.search(r"<Task \w+ name='[^']+'", m)
        took = re.search(r"took [\d.]+ seconds", m)
        info(f"  Executing {task.group(0) if task else '<Task'} coro=<...one()...>> {took.group(0) if took else ''}")
    takeaway("async 函数里一次同步 IO，卡住的不是'这个会话'，而是'这个进程里的所有会话'。线上用调试模式或 lint（练习 c）尽早发现。")


# ==================================================================================== 场景 4：SSE 与取消


class SlowAsyncCheckpointer:
    """模拟一个异步检查点存储（没有嵌入式 Postgres 时用）：每次写入先等 20ms 网络往返，再落盘。"""

    def __init__(self, write_latency: float = 0.02):
        self.write_latency = write_latency
        self.data: dict[str, dict] = {}

    async def save(self, state: RunState) -> None:
        snapshot = state.to_dict()
        await asyncio.sleep(self.write_latency)  # 在这里被取消 = 这次状态没能落盘
        self.data[snapshot["run_id"]] = snapshot

    async def load(self, run_id: str) -> RunState | None:
        d = self.data.get(run_id)
        return RunState.from_dict(copy.deepcopy(d)) if d else None


class FirstFixAgent(AsyncAgent):
    """仅作"修复前后对比"：第一次修复时的 _save。

    那一版只保护了收尾时的最后一次保存（它在受 shield 保护的 _finish 任务里），运行中途的保存直接 await。
    取消如果打在一次中途保存的"数据库已提交、客户端还没收到回复"之间，带版本号 CAS 的检查点记住的版本号就过期了，
    收尾保存会被当成冲突拒绝（讲义 2.5 节、7.2 节 R1）。现在的 AsyncAgent 对异步检查点的每一次保存都做了保护。
    """

    async def _save(self, state: RunState) -> None:
        async with self._io_locks.hold(state.run_id):
            await maybe_await(self._checkpointer().save(state))


class CommitGapStore:
    """带版本号 CAS 的异步检查点，模拟"数据库已提交、响应还在路上"：提交前 2ms、提交后 4ms 才回到客户端。
    （和 tests/test_aio.py 里回归测试用的 CASStore 是同一个思路。）"""

    def __init__(self):
        self.db: dict[str, tuple[int, str]] = {}
        self.local: dict[str, int] = {}  # 客户端记住的版本号
        self.conflicts = 0

    async def save(self, state: RunState) -> None:
        payload = state.to_json()
        expected = self.local.get(state.run_id, 0)
        await asyncio.sleep(0.002)  # 请求发往数据库
        version = self.db.get(state.run_id, (0, ""))[0]
        if version != expected:
            self.conflicts += 1
            raise RuntimeError(f"CheckpointConflict: 期望版本 {expected}，实际 {version}")
        self.db[state.run_id] = (version + 1, payload)  # 数据库已提交
        await asyncio.sleep(0.004)  # 响应在路上：取消最容易打在这里
        self.local[state.run_id] = version + 1

    async def load(self, run_id: str) -> RunState | None:
        row = self.db.get(run_id)
        return RunState.from_dict(json.loads(row[1])) if row else None


async def cancel_sweep(cls, points: int = 20) -> dict[str, int]:
    """在一次运行的 20 个不同时刻（1ms、3ms……39ms）取消它，统计检查点的最终状态。

    这就是"回归测试要覆盖故障窗口的各个位置"：只在一个时刻取消，很可能恰好错过出问题的那几毫秒。"""
    outcomes: dict[str, int] = {}
    for i in range(points):
        llm = AsyncScriptedLLM([call_tool("lookup_ticket", ticket_id="T1"), reply("晚了")], latency=lambda n: 0 if n == 1 else 0.3)
        agent = cls(llm, [lookup_ticket], checkpointer=CommitGapStore())
        task = asyncio.create_task(agent.run("查一下工单 T1", run_id="r"))
        await asyncio.sleep((1 + 2 * i) / 1000)
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        state = await agent._load("r")  # _load 会排在后台还没写完的保存之后
        key = state.status if state else "none"
        if key == "completed":
            key = "completed（取消丢失）"
        outcomes[key] = outcomes.get(key, 0) + 1
    return outcomes


async def waitfor_race_minimal() -> str:
    """Python 3.11 及更早的 asyncio.wait_for 竞态（CPython gh-86296）的最小复现，不依赖 agentkit：
    内部结果和外部取消在同一轮事件循环里到达时，wait_for 返回结果、吞掉取消。3.12 用 asyncio.timeout 重写后不再出现。"""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()  # 相当于线程池里正在执行的同步工具

    async def run():
        return await asyncio.wait_for(fut, 10)

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    fut.set_result("工具结果")  # 工具恰好完成……
    task.cancel()  # ……取消恰好在同一轮事件循环里到达
    try:
        return f"返回了 {await task!r}，取消被吞掉"
    except asyncio.CancelledError:
        return "抛出 CancelledError，取消正常传播"


async def waitfor_race_in_agent(attempts: int = 40) -> tuple[int, int]:
    """同一个竞态出现在 AsyncAgent 里：同步工具（线程池 + wait_for）执行完的那一刻，用户断开。
    返回 (取消丢失、运行跑完的次数, 尝试次数)。"""
    lost = 0
    for _ in range(attempts):
        loop = asyncio.get_running_loop()
        holder: dict[str, asyncio.Task] = {}

        @tool(name="lookup_ticket")
        def racing_lookup(ticket_id: str) -> str:
            """查询工单状态（同步工具，在线程池里执行；返回前用户恰好断开）"""
            loop.call_soon_threadsafe(holder["task"].cancel)
            return f"{ticket_id}：处理中"

        llm = AsyncScriptedLLM([call_tool("lookup_ticket", ticket_id="T1"), reply("完成")], latency=lambda n: 0 if n == 1 else 0.05)
        holder["task"] = asyncio.create_task(AsyncAgent(llm, [racing_lookup]).run("查一下工单 T1"))
        try:
            await holder["task"]
            lost += 1  # 正常返回 = 取消丢了，第二次模型调用照样发生
        except asyncio.CancelledError:
            pass
    return lost, attempts


async def level_triggered_cleanup(shielded: bool) -> str:
    """缺陷根因的最小复现（不依赖 agentkit）：AnyIO 的取消是"电平触发"的。

    在一个已经被取消的作用域里，finally 中的每个 await 都会**再次**被取消 —— 收尾时的保存根本写不进去。
    Starlette/FastAPI 的流式响应就跑在这样的作用域里。修复办法是把收尾放进受保护的作用域（asyncio.shield
    或 anyio.CancelScope(shield=True)）。
    """
    import anyio

    store: dict[str, str] = {"status": "running"}

    async def save(status: str) -> None:
        await anyio.sleep(0.02)  # 一次需要网络往返的写入
        store["status"] = status

    with anyio.CancelScope() as scope:  # 相当于 Starlette 为一次流式响应开的作用域
        try:
            scope.cancel()  # 客户端断开
            await anyio.sleep(30)  # 正在等模型：这里收到取消
        finally:
            if shielded:
                with anyio.CancelScope(shield=True):
                    await save("cancelled")
            else:
                await save("cancelled")  # 仍在已取消的作用域里：这个 await 立刻又被取消
    return store["status"]


class PacedLLM:
    """场景 4 的剧本模型：还没有工具结果时，很快返回一次工具调用；看到工具结果后给出回答，
    前 slow_answers 次回答很慢（30 秒）。按对话内容决定快慢，所以同一个 Agent 可以反复做"断开"实验。"""

    model = "scripted"

    def __init__(self, slow_answers: int | None = None, slow: float = 30.0, fast: float = 0.05):
        self.slow_answers, self.slow, self.fast = slow_answers, slow, fast
        self.calls = self.in_flight = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.in_flight += 1
        try:
            if messages[-1]["role"] != "tool":
                await asyncio.sleep(self.fast)
                return call_tool("check_vpn_cert", user="alice")
            slow = self.slow_answers is None or self.slow_answers > 0
            if self.slow_answers:
                self.slow_answers -= 1
            await asyncio.sleep(self.slow if slow else self.fast)  # 被取消时 CancelledError 从这里抛出
            return reply("您的 VPN 证书已过期。请打开自助门户 → 证书 → 重新签发，完成后重连即可。")
        finally:
            self.in_flight -= 1

    async def stream(self, messages, tools=None, **kwargs):
        response = await self.chat(messages, tools, **kwargs)
        text = response.content or ""
        for i in range(0, len(text), 6):
            await asyncio.sleep(0)
            yield TextDelta(text[i : i + 6])
        yield StreamDone(response)


def build_sse_app(pg_uri: str | None = None):
    """一个最小的 Agent SSE 服务：/chat/stream 流式对话，/chat/resume 从检查点恢复，/runs/{id} 查看服务端状态。

    pg_uri：有嵌入式 Postgres 时，4c 额外用第 26 课的 AsyncPostgresCheckpointer（真实的异步检查点、带版本号 CAS）再验证一遍。"""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    completed: dict[str, int] = {}  # run_id → 工具真正执行完的次数（被取消的不算）

    @tool
    async def check_vpn_cert(user: str, ctx: ToolContext) -> str:
        """检查用户的 VPN 证书（只读，要 0.5 秒）"""
        await asyncio.sleep(0.5)
        completed[ctx.run_id] = completed.get(ctx.run_id, 0) + 1
        return f"{user} 的 VPN 证书已于 3 天前过期"

    @tool(name="check_vpn_cert")
    async def check_vpn_cert_fast(user: str) -> str:
        """检查用户的 VPN 证书（只读）"""
        await asyncio.sleep(0.05)
        return f"{user} 的 VPN 证书已于 3 天前过期"

    # 每个实验一个独立的 Agent（各自的模型和检查点），互不影响。
    # 模型：第一轮很快返回工具调用；看到工具结果后的回答很慢（30 秒，模拟长回答），客户端会在它进行中断开。
    agents: dict[str, AsyncAgent] = {
        "a": AsyncAgent(PacedLLM(), [check_vpn_cert]),
        "b": AsyncAgent(PacedLLM(slow_answers=1), [check_vpn_cert]),  # 只慢一次：4d 恢复时正常回答
        "asyncdb": AsyncAgent(PacedLLM(), [check_vpn_cert_fast], checkpointer=SlowAsyncCheckpointer()),
    }
    if pg_uri is not None:
        from agentkit.contrib.postgres import AsyncPostgresCheckpointer

        agents["pg"] = AsyncAgent(PacedLLM(), [check_vpn_cert_fast], checkpointer=AsyncPostgresCheckpointer(pg_uri, table="lesson30_runs"))

    def encode(seq: int, run_id: str, event) -> str:
        if isinstance(event, RunStarted):
            name, data = "run_started", {"run_id": event.run_id}
        elif isinstance(event, ToolStarted):
            name, data = "tool_started", {"tool": event.call.name}
        elif isinstance(event, ToolFinished):
            name, data = "tool_finished", {"tool": event.call.name, "ok": event.result.ok}
        elif isinstance(event, TextDelta):
            name, data = "delta", {"text": event.text}
        elif isinstance(event, RunFinished):
            name, data = "done", {"status": event.result.status, "output": event.result.output}
        else:
            name, data = type(event).__name__, {}
        # id 让浏览器的 EventSource 断线重连时带上 Last-Event-ID；这里用 run_id:序号，服务端据此知道该接着哪个运行
        return f"id: {run_id}:{seq}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def sse_response(events, run_id: str):
        async def body():
            seq = 0
            async with contextlib.aclosing(events) as stream:  # 客户端断开 → Starlette 取消这个生成器 → aclosing 取消运行
                async for event in stream:
                    seq += 1
                    yield encode(seq, run_id, event)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 关掉 Nginx 这类代理的缓冲，否则事件会攒着一起到
        return StreamingResponse(body(), media_type="text/event-stream", headers=headers)

    app = FastAPI()

    @app.get("/chat/stream")
    async def chat_stream(q: str, run_id: str, variant: str):
        return sse_response(agents[variant].stream(q, run_id=run_id), run_id)

    @app.get("/chat/resume")
    async def chat_resume(run_id: str, variant: str):
        return sse_response(agents[variant].stream_resume(run_id), run_id)

    @app.get("/runs/{run_id}")
    async def run_status(run_id: str, variant: str):
        agent = agents[variant]
        cp = agent.checkpointer
        # Postgres 检查点用只读的 get_run：load 会记住版本号，在这里调用会干扰正在运行的会话
        if hasattr(cp, "get_run"):
            row = await cp.get_run(run_id)
            state = RunState.from_dict(row["state"]) if row else None
        else:
            state = await maybe_await(cp.load(run_id))
        return {
            "status": state.status if state else None,
            "stop_reason": state.stop_reason if state else None,
            "tool_started": state.tool_calls_count if state else None,  # agentkit 在执行前计数
            "tool_completed": completed.get(run_id, 0),
            "last_tool_message": next((m["content"] for m in reversed(state.messages) if m["role"] == "tool"), None) if state else None,
            "llm_calls": agent.llm.calls,
            "llm_in_flight": agent.llm.in_flight,
        }

    return app


class BackgroundServer:
    """在后台线程里跑 uvicorn（它有自己的事件循环），端口由系统分配。"""

    def __init__(self, app):
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn 没能启动")
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


async def sse_until(client, path: str, params: dict, stop_event: str | None) -> list[str]:
    """读 SSE 事件；读到 stop_event 就断开连接（模拟用户关掉页面）。返回读到的事件名。"""
    seen: list[str] = []
    async with client.stream("GET", path, params=params) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                seen.append(line.removeprefix("event: "))
                if seen[-1] == stop_event:
                    break  # 退出 async with → 关闭连接
    return seen


async def poll_status(client, run_id: str, variant: str, want: str, timeout: float = 2.0) -> tuple[dict, float]:
    t0 = time.perf_counter()
    while True:
        st = (await client.get(f"/runs/{run_id}", params={"variant": variant})).json()
        if (st["status"] == want and st["llm_in_flight"] == 0) or time.perf_counter() - t0 > timeout:
            return st, time.perf_counter() - t0
        await asyncio.sleep(0.01)


REPEATS = 20  # 4c 每种检查点断开几次（竞态问题要看比例，不能只看一次）


async def cancel_experiment(base_url: str, backends: set[str]) -> None:
    import httpx

    q = {"q": "VPN 连不上了"}
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        step("4a. 客户端读到第一个工具事件 tool_started 就断开（工具要跑 0.5 秒，此时正在执行）")
        seen = await sse_until(client, "/chat/stream", {**q, "run_id": "r-a", "variant": "a"}, "tool_started")
        st, waited = await poll_status(client, "r-a", "a", "cancelled")
        info(f"客户端读到：{' → '.join(seen)}，然后断开")
        info(
            f"断开后 {waited * 1000:.0f}ms：服务端检查点 status={st['status']!r}；工具开始 {st['tool_started']} 次、执行完 {st['tool_completed']} 次"
            f"（被取消在半路）；这次只读调用在历史里被补成：{st['last_tool_message']!r}"
        )
        assert st["status"] == "cancelled" and st["tool_completed"] == 0, st

        step("4b. 读到 tool_finished 再断开：此时服务端正在进行第 2 次模型调用（剧本设定要 30 秒）")
        seen = await sse_until(client, "/chat/stream", {**q, "run_id": "r-b", "variant": "b"}, "tool_finished")
        st, waited = await poll_status(client, "r-b", "b", "cancelled")
        info(f"客户端读到：{' → '.join(seen)}，然后断开")
        info(
            f"断开后 {waited * 1000:.0f}ms：检查点 status={st['status']!r}，模型累计被调用 {st['llm_calls']} 次，"
            f"在途模型调用 {st['llm_in_flight']} 个 —— 30 秒的调用被当场取消，没有继续花钱"
        )
        assert st["status"] == "cancelled" and st["llm_calls"] == 2 and st["llm_in_flight"] == 0, st

        step("4c. 异步检查点 + 取消：本课发现的缺陷，修了两次（讲义 2.5 节）")
        before = await level_triggered_cleanup(shielded=False)
        after = await level_triggered_cleanup(shielded=True)
        info(f"① 根因的最小复现（AnyIO 电平触发取消）：finally 里直接 await 保存 → {before!r}；放进 shield 作用域 → {after!r}")
        info("② 取消时刻扫描：带版本号 CAS、'提交后 4ms 才回复'的异步存储，在一次运行的 20 个时刻（1ms…39ms）各取消一次")
        for cls, label in ((FirstFixAgent, "第一次修复（只保护收尾那次保存）"), (AsyncAgent, "现在的 AsyncAgent（每次异步保存都保护）")):
            outcomes = await cancel_sweep(cls)
            info(f"   {label:<26}{'，'.join(f'{k} × {v}' for k, v in sorted(outcomes.items()))}")
        info("③ SSE 端到端：客户端读到 tool_finished 就断开，每种检查点各 20 次")
        variants = [("asyncdb", "模拟异步存储（每次写 20ms）")]
        if "pg" in backends:
            variants.append(("pg", "AsyncPostgresCheckpointer（嵌入式 Postgres）"))
        for variant, label in variants:
            outcomes: dict[str, int] = {}
            in_flight = 0
            for i in range(REPEATS):
                run_id = f"r-{variant}-{i}"
                await sse_until(client, "/chat/stream", {**q, "run_id": run_id, "variant": variant}, "tool_finished")
                st, _ = await poll_status(client, run_id, variant, "cancelled", timeout=0.5)
                outcomes[st["status"]] = outcomes.get(st["status"], 0) + 1
                in_flight = max(in_flight, st["llm_in_flight"])
            summary = "，".join(f"{k} × {v}" for k, v in sorted(outcomes.items()))
            info(f"   {label:<34}断开 {REPEATS} 次 → {summary}；在途模型调用最多 {in_flight} 个")
        info(f"④ 复测时的新发现：Python {platform.python_version()} 的 asyncio.wait_for 竞态（CPython gh-86296）")
        info(f"   最小复现：内部结果和外部取消同时到达 → {await waitfor_race_minimal()}")
        lost, attempts = await waitfor_race_in_agent()
        info(f"   AsyncAgent：同步工具执行完的那一刻断开，{attempts} 次里有 {lost} 次取消丢失、运行照样跑完（agentkit.aio 已改用取消安全的 wait_for，应为 0）")

        step("4d. 断线重连 ≠ 从头再来：用 4b 的 run_id 调 /chat/resume，从检查点接着跑")
        seen = await sse_until(client, "/chat/resume", {"run_id": "r-b", "variant": "b"}, None)
        st, _ = await poll_status(client, "r-b", "b", "completed")
        info(
            f"恢复后的事件：{' → '.join(dict.fromkeys(seen))}；最终 status={st['status']!r}；"
            f"工具累计执行完 {st['tool_completed']} 次（结果在检查点里，没有重跑），模型累计调用 {st['llm_calls']} 次"
        )


def scenario_cancel(args) -> None:
    banner("场景 4：取消 —— 用户关掉页面，服务端要真的停下来")
    if not has("fastapi", "uvicorn", "httpx", "anyio"):
        info("⚠️  缺少 fastapi / uvicorn / httpx，跳过本场景。安装：")
        info(f"    {INSTALL_HINT}")
        return
    with contextlib.ExitStack() as stack:
        pg_uri, backends = None, {"asyncdb"}
        if has("pgserver", "psycopg", "psycopg_pool"):
            from agentkit.contrib.postgres import PostgresCheckpointer
            from agentkit.testing import embedded_postgres

            # 查询进行到一半被取消时 psycopg 会打 WARNING：这里正是在制造这种情况，所以调高它的日志级别
            for name in ("psycopg", "psycopg.pool"):
                logging.getLogger(name).setLevel(logging.ERROR)
            pg_uri = stack.enter_context(embedded_postgres())
            PostgresCheckpointer(pg_uri, table="lesson30_runs").setup()
            backends.add("pg")
        else:
            info(f"（没有 pgserver / psycopg，4c 只用模拟的异步存储；想用真实 Postgres 再验证一遍：{INSTALL_HINT}）")
        unretrieved: list[str] = []
        stack.enter_context(capture_logs("asyncio", unretrieved))
        base_url = stack.enter_context(BackgroundServer(build_sse_app(pg_uri)))
        info(f"FastAPI 服务已在后台线程启动：{base_url}（uvicorn，自己的事件循环）")
        asyncio.run(cancel_experiment(base_url, backends))
        gc.collect()  # "never retrieved" 在任务对象被回收时才记录
        never = [m for m in unretrieved if "never retrieved" in m]
        if never:
            info(f"（服务端有 {len(never)} 条 'Task exception was never retrieved' —— 修复后本应为 0，请检查）")
    takeaway("取消链路：TCP 断开 → uvicorn 发出 http.disconnect → Starlette 取消响应任务 → aclosing 关闭 agent.stream → 运行任务被取消 → 模型调用停止、检查点记 cancelled。")


# ==================================================================================== 场景 5：舱壁


async def bulkhead_experiment(limiter: KeyedLimiter | None, limiter_timeout: float | None) -> dict:
    model = AsyncScriptedLLM(responder=lambda m: reply("好的"), latency=0.2)
    llm = AsyncResilientLLM(model, max_concurrency=8)  # 这个进程对模型的并发上限（例如网关给的配额）
    agent = AsyncAgent(llm, [], limiter=limiter, limiter_timeout=limiter_timeout)
    t0 = time.perf_counter()
    out: dict[str, list] = {"noisy": [], "quiet": [], "noisy_rejected": 0, "quiet_rejected": 0}

    async def one(tenant: str, delay: float):
        await asyncio.sleep(delay)
        res = await agent.run("帮我总结这份周报", metadata={"tenant_id": tenant})
        if res.stop_reason == "rate_limited":
            out[f"{tenant}_rejected"] += 1
        else:
            out[tenant].append(time.perf_counter() - t0 - delay)  # 从这个请求到达开始算

    # 吵闹租户一口气发 50 个；10ms 后安静租户发 2 个
    await asyncio.gather(*(one("noisy", 0) for _ in range(50)), *(one("quiet", 0.01) for _ in range(2)))
    out["max_in_flight"] = model.max_in_flight
    return out


def scenario_bulkhead(args) -> None:
    banner("场景 5：舱壁 —— 一个租户发 50 个请求，另一个租户只发 2 个")
    info("每个请求 1 次模型调用 × 0.2s；模型并发上限 8（AsyncResilientLLM(max_concurrency=8)）。时间从请求到达算起。")
    configs = (
        ("只有全局上限（没有按租户隔离）", None, None),
        ("KeyedLimiter(per_key=4, global_limit=8)", KeyedLimiter(per_key=4, global_limit=8), None),
        ("同上 + limiter_timeout=0.5s（排不上就 429）", KeyedLimiter(per_key=4, global_limit=8), 0.5),
    )
    for label, limiter, timeout in configs:
        r = asyncio.run(bulkhead_experiment(limiter, timeout))
        step(label)
        info(f"安静租户 {len(r['quiet'])} 个完成：{dist(r['quiet'])}")
        rejected = f"，{r['noisy_rejected']} 个被快速拒绝（rate_limited）" if r["noisy_rejected"] else ""
        info(f"吵闹租户 {len(r['noisy'])} 个完成：{dist(r['noisy'])}{rejected}")
        info(f"模型同一时刻最多 {r['max_in_flight']} 个在途调用")
    takeaway("没有舱壁时，安静租户排在吵闹租户的 50 个请求后面；有了舱壁，它的延迟和'系统空闲时'一样。代价：吵闹租户被限在 4 并发，即使模型还有空闲名额。")


# ==================================================================================== 场景 6：真实模型


class TimedLLM:
    """包在真实模型外面：记录每次调用的耗时、首 token 时间和同时在途数（证明并发上限生效）。"""

    def __init__(self, inner):
        self.inner, self.model = inner, inner.model
        self.durations: list[float] = []
        self.in_flight = self.max_in_flight = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        t0 = time.perf_counter()
        try:
            return await self.inner.chat(messages, tools, **kwargs)
        finally:
            self.durations.append(time.perf_counter() - t0)
            self.in_flight -= 1

    async def stream(self, messages, tools=None, **kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        t0 = time.perf_counter()
        try:
            async for event in self.inner.stream(messages, tools, **kwargs):
                yield event
        finally:
            self.durations.append(time.perf_counter() - t0)
            self.in_flight -= 1

    async def aclose(self):
        await self.inner.aclose()


@tool
async def get_ticket_status(ticket_id: str) -> str:
    """查询 IT 工单的处理状态"""
    await asyncio.sleep(0.05)
    return f"{ticket_id}：二线支持处理中，预计今天 18:00 前完成"


async def real_model_experiment() -> None:
    from agentkit.aio import default_async_llm

    timed = TimedLLM(default_async_llm(max_connections=10))
    llm = AsyncResilientLLM(timed, max_concurrency=3, max_attempts=2)
    agent = AsyncAgent(llm, [get_ticket_status], system_prompt="你是 IT 服务台助手。需要工单信息时调用工具。回答不超过两句话。")
    try:
        step(f"6a. 流式会话（模型 {timed.model}）：一次工具调用 + 流式回答")
        t0 = time.perf_counter()
        ttft = tool_done = None
        text: list[str] = []
        final = None
        async with contextlib.aclosing(agent.stream("帮我查一下工单 T-1024 的进度")) as events:
            async for event in events:
                now = time.perf_counter() - t0
                if isinstance(event, ToolStarted):
                    info(f"[{now:5.2f}s] 工具开始：{event.call.name}({event.call.arguments})")
                elif isinstance(event, ToolFinished):
                    tool_done = now
                elif isinstance(event, TextDelta):
                    if ttft is None:
                        ttft = now
                        second = f"；第二轮模型调用从发出到首个片段 {ttft - tool_done:.2f}s" if tool_done is not None else ""
                        info(f"[{now:5.2f}s] 首个文本片段到达（TTFT，从请求开始算{second}）")
                    text.append(event.text)
                elif isinstance(event, RunFinished):
                    final = event.result
        total = time.perf_counter() - t0
        info(f"[{total:5.2f}s] 完成：status={final.status}，模型调用 {final.steps} 次，文本分 {len(text)} 个片段到达")
        info(f"         回答：{''.join(text)[:100]}")

        step("6b. 6 个并发会话，模型并发上限 3")
        questions = ["VPN", "单点登录（SSO）", "零信任网络", "MFA 多因素认证", "端点检测与响应（EDR）", "数据防泄漏（DLP）"]
        timed.durations.clear()
        timed.max_in_flight = 0
        t0 = time.perf_counter()
        results = await asyncio.gather(*(agent.run(f"用一句话向新员工解释什么是{q}。") for q in questions))
        wall = time.perf_counter() - t0
        serial = sum(timed.durations)
        info(f"{sum(r.ok for r in results)}/6 成功；墙钟总耗时 {wall:.2f}s；逐个调用耗时之和（串行估计）{serial:.2f}s；加速 {serial / wall:.1f}×")
        info(f"单次调用耗时 {dist(timed.durations)}；同一时刻最多 {timed.max_in_flight} 个在途（上限 3）")
        info("理想加速 ≈ min(6, 3) = 3×；达不到 3× 是因为 6 个请求的耗时参差不齐，最后一'波'要等最慢的那个。")
        if llm.events:
            info(f"可靠性事件：{llm.events}")
    finally:
        await agent.aclose()


def scenario_real(args) -> None:
    banner("场景 6：真实模型 —— 流式 TTFT 与并发上限（本场景约 8 次模型调用）")
    if args.offline:
        info("--offline：跳过（不调用模型）。")
        return
    from agentkit.config import env

    if not env("LLM_API_KEY"):
        info("⚠️  没有找到 LLM_API_KEY（.env 或环境变量）。没有 key 也没关系：加上 --offline 只跑场景 1–5。")
        return
    asyncio.run(real_model_experiment())


# ==================================================================================== 入口


def main() -> None:
    parser = argparse.ArgumentParser(description="第 30 课 Demo：异步运行时与高并发服务")
    parser.add_argument("--offline", action="store_true", help="不调用真实模型（跳过场景 6）")
    parser.add_argument("--full", action="store_true", help="场景 1 的同步串行跑满 200 个会话（约 80 秒）")
    parser.add_argument("--only", default="1,2,3,4,5,6", help="只运行指定场景，如 --only 3,4")
    parser.add_argument("--repeat", type=int, default=1, help="场景 1 每种并发方式重复运行几次、取中位数（机器繁忙时用 3）")
    parser.add_argument("--_bench", nargs=2, help=argparse.SUPPRESS)
    parser.add_argument("--_waiters", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args._bench:  # 场景 1 的子进程入口
        print(json.dumps(bench_sessions(args._bench[0], int(args._bench[1]))))
        return
    if args._waiters:
        print(json.dumps(bench_waiters(args._waiters[0], int(args._waiters[1]))))
        return

    print(
        f"🖥  {platform.system()} {platform.release()} / {platform.machine()} / {os.cpu_count()} 核 ｜ "
        f"Python {platform.python_version()} ({platform.python_implementation()})"
    )
    print("🔌 离线模式：场景 1–5 用剧本模型 + sleep 模拟延迟" if args.offline else "🌐 场景 1–5 用剧本模型；场景 6 调用真实模型")
    started = time.time()
    scenarios = {
        "1": scenario_throughput,
        "2": scenario_parallel_tools,
        "3": scenario_blocking,
        "4": scenario_cancel,
        "5": scenario_bulkhead,
        "6": scenario_real,
    }
    for key in args.only.replace(" ", "").split(","):
        scenarios[key](args)

    banner("小结")
    info("1. Agent 服务是 IO 密集型：容量 = 同时在飞的会话数（利特尔法则），asyncio 让一个进程撑起几百上千个。")
    info("2. 并发之后要补齐四件事：只读工具并行、真正的取消、截止时间、按租户的舱壁（背压）。")
    info("3. 事件循环是共享的：一次同步 IO、一次吞掉的 CancelledError，影响的是整个进程。")
    info(f"总耗时 {time.time() - started:.0f} 秒。")


if __name__ == "__main__":
    main()
