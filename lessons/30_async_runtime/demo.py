"""第 30 课 Demo：生产环境的高并发异步运行时 —— 规模上来之后什么会坏、运行时怎么扛住。

    python lessons/30_async_runtime/demo.py --offline              # 场景 1–5，不调用模型，约 1–2 分钟
    python lessons/30_async_runtime/demo.py --offline --full       # 场景 1a 的"一个接一个"也跑满 200 个会话（多约 70 秒）
    python lessons/30_async_runtime/demo.py --offline --repeat 3   # 场景 1a 每种方式跑 3 次、取耗时中位数
    python lessons/30_async_runtime/demo.py --offline --only 3,4   # 只跑指定场景
    python lessons/30_async_runtime/demo.py                        # 再加场景 6：真实模型（约 8 次调用）

并发都是真的：真的线程、真的 worker 进程（WorkerPool）、真的 uvicorn 子进程、真的数据库（SQLite 文件；有 pgserver 时再加嵌入式
Postgres）。场景 1–5 里的"模型"是剧本模型：用 asyncio.sleep 扮演"等模型"，这样数字可复现、断开的时刻可控。

  1. 吞吐与天花板
     1a 同一批 200 个会话（W ≈ 0.4 秒）：一个接一个 await / 16 个线程各跑一个事件循环 / asyncio.gather / gather + 舱壁；
        2000 个会话看单个事件循环的 CPU 天花板。每种方式在独立子进程里跑（内存数字互不干扰）
     1b 一个"在等 IO 的会话"要花多少内存：线程 vs 协程
     1c 多进程（WorkerPool 拉起真实的 worker 进程）：1 / 2 / 4 个进程；换成 SQLite 检查点；共享模型配额 —— 天花板怎么移动
  2. 并行工具：只读工具并行，写工具串行
  3. 事件循环与三种执行方式
     3a async 钩子里的阻塞调用：心跳测事件循环延迟；调试模式报慢回调
     3b 同步工具的线程池：卡住的线程杀不掉，池满之后别的同步工具一行都没执行就"超时"了
     3c CPU 密集 / 不可信的工具：在线程里超时后还在烧 CPU；进程隔离的硬超时
  4. 取消：uvicorn 真实子进程 + httpx；客户端断开 → 服务端运行被取消，检查点（真实的异步数据库）记为 cancelled；
     R1 在真实 SQLite 上的复现；R3 wait_for 竞态；依赖库吞掉取消 → 步骤边界补抛；按 run_id 恢复
  5. 舱壁与背压：进程内 KeyedLimiter（吵闹租户 vs 安静租户）；跨进程：各自的信号量 vs 共享的 SQLiteSemaphore
  6. 真实模型：流式 TTFT + 并发上限

场景 4 需要 fastapi、uvicorn、httpx；缺少时打印安装命令并跳过该场景，demo 仍以退出码 0 结束。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import importlib.util
import json
import logging
import multiprocessing
import os
import platform
import queue
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNS = HERE / "runs"
if str(REPO) not in sys.path:  # 直接 python lessons/.../demo.py 运行时也能 import agentkit
    sys.path.insert(0, str(REPO))

from agentkit import (  # noqa: E402
    Agent,
    Hook,
    KeyedLimiter,
    ResilientLLM,
    RunFinished,
    ScriptedLLM,
    TextDelta,
    ToolFinished,
    ToolStarted,
    call_tool,
    call_tools,
    maybe_await,
    reply,
    tool,
)
from agentkit.distributed import SQLiteCheckpointer, SQLiteDB, SQLiteSemaphore  # noqa: E402

INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'
LATENCY = 0.2  # 场景 1a：每次"模型调用"等多久（秒）
CALLS_PER_SESSION = 2  # 场景 1a：每个会话 = 调一次模型拿到工具调用 + 执行工具 + 再调一次模型给答案
SERVICE_TIME = LATENCY * CALLS_PER_SESSION  # 利特尔法则里的 W：一个会话占用"一个并发名额"的时长
BULKHEAD = 50  # 场景 1a 的 D：舱壁允许同时在跑的会话数


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


def has(*modules: str) -> bool:
    return all(importlib.util.find_spec(m) is not None for m in modules)


def load_avg() -> str:
    try:
        return "/".join(f"{x:.1f}" for x in os.getloadavg())
    except OSError:
        return "n/a"


@contextlib.contextmanager
def capture_logs(logger_name: str, sink: list, level: int = logging.WARNING):
    """把某个 logger 的记录收进 sink（不打印），退出时还原。收的是 LogRecord 本身，可以读 extra 字段。"""

    class Grab(logging.Handler):
        def emit(self, record):
            sink.append(record)

    logger = logging.getLogger(logger_name)
    handler = Grab(level=level)
    logger.addHandler(handler)
    old_propagate, logger.propagate = logger.propagate, False
    try:
        yield sink
    finally:
        logger.removeHandler(handler)
        logger.propagate = old_propagate


async def heartbeat(lags: list[float], stop: asyncio.Event, period: float = 0.01) -> None:
    """事件循环延迟：每 period 秒醒一次，醒得越晚，说明事件循环被卡得越久（线上导出成指标，就是"事件循环延迟"）。"""
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(period)
        lags.append(time.perf_counter() - t0 - period)


# ==================================================================================== 场景 1：吞吐与天花板


@tool
async def lookup_ticket(ticket_id: str) -> str:
    """查询工单状态"""
    return f"{ticket_id}：处理中，已分配给二线支持"


def ticket_responder(messages):
    """剧本：第一轮调用工具查工单，看到工具结果后给出答案。根据当前对话决定回复，几千个会话并发也不会串台。"""
    if messages[-1]["role"] == "tool":
        return reply(f"查到了：{messages[-1]['content']}")
    return call_tool("lookup_ticket", ticket_id=messages[-1]["content"].split()[-1])


class BenchLLM:
    """场景 1a 的剧本模型：每次调用 await asyncio.sleep(latency) 扮演"等模型"，统计同一时刻在途的调用数。
    计数加了线程锁：B 方式里 16 个线程各跑各的事件循环，共用这一个对象。
    不像 ScriptedLLM 那样深拷贝每次调用的消息（那是测试断言用的开销，生产里没有）。"""

    model = "scripted"

    def __init__(self, latency: float):
        self.latency = latency
        self.calls = self.in_flight = self.max_in_flight = 0
        self._lock = threading.Lock()

    async def chat(self, messages, tools=None, **kwargs):
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
            return ticket_responder(messages)
        finally:
            with self._lock:
                self.in_flight -= 1


def question(i: int) -> str:
    return f"查一下工单 T{i:04d}"


def bench_sessions(mode: str, n: int) -> dict:
    """在当前（独立的子）进程里跑一种并发方式，返回测量结果。

    不开 tracemalloc：它会让每次内存分配变慢好几倍，把计时扭曲得面目全非。内存用进程的峰值 RSS 增量（ru_maxrss），
    它包含线程栈，正是"每会话一个线程"真正的内存代价。计时前先空转约 0.2 秒预热（繁忙的机器上不预热，CPU 时间能差到 2 倍）。"""
    end = time.perf_counter() + 0.2
    while time.perf_counter() < end:
        pass
    base_rss = peak_rss_bytes()
    llm = BenchLLM(LATENCY)
    lags: list[float] = []
    cpu0, t0 = time.process_time(), time.perf_counter()
    if mode.startswith("threads"):
        # 16 个线程，每个线程一个事件循环，一次跑一个会话（同步 Web 框架里调用 async 代码时常见的写法）
        agent = Agent(llm, [lookup_ticket])  # 一个实例被所有线程共用
        todo: queue.SimpleQueue = queue.SimpleQueue()
        for i in range(n):
            todo.put(i)
        results: list = [None] * n

        def worker() -> None:
            async def drain() -> None:
                while True:
                    try:
                        i = todo.get_nowait()
                    except queue.Empty:
                        return
                    results[i] = await agent.run(question(i))

            asyncio.run(drain())

        threads = [threading.Thread(target=worker) for _ in range(int(mode.removeprefix("threads")))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        limiter = KeyedLimiter(per_key=BULKHEAD) if mode == "bulkhead" else None
        agent = Agent(llm, [lookup_ticket], limiter=limiter)  # 同一个实例被 n 个会话并发复用

        async def main():
            stop = asyncio.Event()
            hb = asyncio.create_task(heartbeat(lags, stop))
            try:
                if mode == "serial":
                    return [await agent.run(question(i)) for i in range(n)]  # 一个接一个：await 完一个再开始下一个
                return await asyncio.gather(*(agent.run(question(i), metadata={"tenant_id": "acme"}) for i in range(n)))
            finally:
                stop.set()
                await hb

        results = asyncio.run(main())
    elapsed, cpu = time.perf_counter() - t0, time.process_time() - cpu0
    peak = peak_rss_bytes()
    return {
        "mode": mode,
        "n": n,
        "ok": sum(r.ok for r in results),
        "crossed": sum(r.messages[1]["content"] != question(i) for i, r in enumerate(results)),
        "elapsed": elapsed,
        "max_in_flight": llm.max_in_flight,
        "cpu": cpu,
        "rss_delta": None if base_rss is None else peak - base_rss,
        "lag_p99": pct(lags, 99) if lags else None,
        "lag_max": max(lags) if lags else None,
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
        [sys.executable, str(Path(__file__).resolve()), *args], capture_output=True, text=True, timeout=900, cwd=str(REPO)
    )
    if proc.returncode != 0:
        raise RuntimeError(f"子进程失败：{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def fmt_ms(x: float | None) -> str:
    return "—" if x is None else f"{x * 1000:.0f}ms"


def scenario_throughput_one_process(args) -> None:
    n = 200
    serial_n = n if args.full else 20
    step(f"1a：同一批 {n} 个会话，一个进程里的四种跑法（外加 2000 个会话看单个事件循环的天花板）")
    info(f"每个会话：模型调用 {CALLS_PER_SESSION} 次 × {LATENCY}s + 1 次 async 工具，所以服务时间 W ≈ {SERVICE_TIME:.1f}s。")
    info(f"每种方式在一个全新的子进程里跑；{'重复 ' + str(args.repeat) + ' 次取耗时中位数' if args.repeat > 1 else '各跑 1 次（机器繁忙时加 --repeat 3）'}；"
         f"测量时 load average {load_avg()}。")
    if not args.full:
        info(f"A 只跑 {serial_n} 个会话再按线性外推（跑满 {n} 个要约 {n * SERVICE_TIME:.0f}s；加 --full 可实测）。")
    rows = [
        ("A. 一个接一个 await", "serial", serial_n, 1),
        ("B. 16 个线程 × 各自的事件循环", "threads16", n, 16),
        ("C. asyncio.gather 全部", "gather", n, n),
        (f"D. gather + 舱壁（{BULKHEAD}）", "bulkhead", n, BULKHEAD),
        ("E. gather 全部，2000 个会话", "gather", 2000, 2000),
    ]
    print("   方式                           会话     耗时  吞吐(会话/s) 峰值在途 利特尔预测  CPU 时间   RSS 增量  循环延迟 p99/max", flush=True)
    measured = {}
    for label, mode, count, conc in rows:
        runs = [run_child("--_bench", mode, str(count)) for _ in range(1 if mode == "serial" else args.repeat)]
        r = sorted(runs, key=lambda x: x["elapsed"])[len(runs) // 2]  # 多次运行取耗时的中位数
        measured[label[0]] = r
        thr = r["n"] / r["elapsed"]
        predicted = min(conc, r["n"]) / SERVICE_TIME  # 利特尔法则：吞吐 λ = L / W
        lag = "—" if r["lag_p99"] is None else f"{fmt_ms(r['lag_p99'])}/{fmt_ms(r['lag_max'])}"
        note = f"  → 外推 {n} 个 ≈ {r['elapsed'] / r['n'] * n:.0f}s" if mode == "serial" and not args.full else ""
        info(f"{label:<28}{r['n']:>5}{r['elapsed']:>8.2f}s{thr:>11.1f}{r['max_in_flight']:>9}{predicted:>10.1f}"
             f"{r['cpu']:>9.2f}s{mb(r['rss_delta']):>11}  {lag:>12}{note}")
        assert r["ok"] == r["n"] and r["crossed"] == 0, "每个会话都应成功，且不串台"
    info("峰值在途 = 同一时刻真实在等模型的调用数（剧本模型自己计数）；利特尔预测 = min(并发, 会话数) / W；")
    info("CPU 时间 = 整个进程消耗的 CPU 秒（所有线程之和）；循环延迟 = 每 10ms 醒一次的心跳迟到了多久（B 有 16 个循环，不测）。")
    c, t16, big = measured["C"], measured["B"], measured["E"]
    per_session_ms = big["cpu"] / big["n"] * 1000
    takeaway(
        f"利特尔法则 L = λ × W：W 固定为 {SERVICE_TIME:.1f}s 时，吞吐只取决于'同时在飞'的会话数 L。"
        f"16 个线程 → L=16 → 最多 {16 / SERVICE_TIME:.0f} 会话/s；gather 让 L 达到 {c['max_in_flight']}，实测 {c['n'] / c['elapsed']:.0f} 会话/s，"
        f"是 16 线程的 {t16['elapsed'] / c['elapsed']:.1f} 倍；舱壁把 L 定在 {BULKHEAD}，吞吐就按 {BULKHEAD}/{SERVICE_TIME:.1f} 封顶 —— 舱壁就是你亲手选的 L。"
    )
    info(f"    E 没有达到预测的 {2000 / SERVICE_TIME:.0f} 会话/s：事件循环是单线程的，每个会话还要花约 {per_session_ms:.2f}ms CPU"
         f"（校验、序列化检查点、追踪），单核上限约 {1000 / per_session_ms:.0f} 会话/s；CPU 吃紧时循环延迟也跟着涨。")
    info("    这就是一个进程的第二个天花板：等得起几千个，但算不过来。下一步是加进程（1c）。")


def scenario_waiters(args) -> None:
    step("1b：一个'正在等 IO 的会话'要花多少内存？（线程 vs 协程，各自在独立子进程里测）")
    for kind, count in (("threads", 2000), ("tasks", 2000), ("tasks", 50000)):
        r = run_child("--_waiters", kind, str(count))
        per = "n/a" if r["rss_delta"] is None else f"{r['rss_delta'] / r['n'] / 1024:.1f} KB/个"
        label = "线程（阻塞在 Event.wait）" if kind == "threads" else "协程（await Event.wait）"
        info(f"{r['n']:>6} 个{label:<22} 创建耗时 {r['create_s'] * 1000:>7.0f} ms ｜ RSS 增量 {mb(r['rss_delta']):>9} ｜ {per}")
    takeaway("线程不是不能用；差别在'每个等待者的成本'、取消语义，以及上万并发时的扩展性。")


async def measure_workers(name: str, *, procs: int, concurrency: int, n_jobs: int, options: dict) -> dict:
    """用 WorkerPool 拉起 procs 个真实的 worker 进程（python -m agentkit.distributed.worker），跑 n_jobs 个 Agent 任务。

    计时从"开闸"算起：任务先带着很长的延迟入队，等所有 worker 进程都启动完毕，再用一条 UPDATE 让它们同时变成可领取，
    所以进程启动、import、校准的时间都不算在内（和第 13 课 demo_scale.py 同一个做法）。"""
    from agentkit.distributed import SQLiteJobQueue, WorkerPool

    workdir = RUNS / "scale" / name
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    db = workdir / "jobs.db"
    queue_ = SQLiteJobQueue(db)
    await queue_.setup()
    # 调度方先建好所有表：几个进程同时新建 SQLite 文件、切换 WAL 模式时会撞锁
    for store in (SQLiteCheckpointer(db), SQLiteSemaphore(db, "model", limit=1)):
        await store.setup()
        await store.close()
    for i in range(n_jobs):
        await queue_.enqueue("run", {"op": "run", "input": question(i)}, tenant_id="acme", delay_seconds=3600)  # 先关着闸
    pool = WorkerPool(f"sqlite:///{db}", f"{HERE / 'worker_app.py'}:make_handler", n=procs, concurrency=concurrency,
                      lease=60, poll=0.02, grace=10, options=options, log_dir=workdir / "logs")
    with pool:
        deadline = time.monotonic() + 90
        while len(pool.events("started")) < procs:
            if time.monotonic() > deadline:
                raise TimeoutError(pool.logs()[-3000:])
            await asyncio.sleep(0.05)
        gate = time.time()
        conn = sqlite3.connect(db, timeout=30)
        conn.execute("UPDATE agent_jobs SET run_at = ?", (gate,))  # 开闸：所有任务同时变成可领取
        conn.commit()
        conn.close()
        deadline = time.monotonic() + 180
        while (await queue_.stats())["counts"]["succeeded"] < n_jobs:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{name}: 180 秒内没有全部完成\n{pool.logs()[-3000:]}")
            await asyncio.sleep(0.02)
    # 退出 with = 对所有 worker 发 SIGTERM，它们退出前各打一条 report 事件
    finished = max(j.finished_at for j in await queue_.list_jobs(limit=n_jobs))
    await queue_.close()
    elapsed = finished - gate
    reports = pool.events("report")
    cpu = sum(r["cpu_s"] for r in reports)
    busy = max((r["busy_s"] for r in reports), default=elapsed) or elapsed
    return {
        "elapsed": elapsed,
        "rate": n_jobs / elapsed,
        "cpu_util": cpu / (busy * procs),
        "lag_p99": max((r["lag_p99_ms"] for r in reports), default=0.0),
        "procs_reported": len(reports),
    }


def scenario_throughput_processes(args) -> None:
    step("1c：加进程 —— WorkerPool 拉起真实的 worker 进程，看天花板怎么移动")
    info("每个任务 = 一次 Agent 运行：模型调用 2 次（各等 0.1s）+ 1 次 async 工具；每次调用模型前，Hook 做约 1ms 的纯 Python 计算")
    info("（代表你自己的 Hook、上下文策略、JSON 处理加进来的 CPU；按本进程实测校准成固定计算量）。所有进程共享一个 SQLite 文件做队列。")
    info(f"测量时 load average {load_avg()}（本机 {os.cpu_count()} 核，同时还跑着别的任务）。")
    base = {"latency": 0.1, "cpu_ms": 1.0}
    configs = [
        ("1 进程 × 并发 256，检查点在内存", 1, 256, 1200, {**base, "checkpoint": "memory"}),
        ("2 进程 × 并发 256，检查点在内存", 2, 256, 1200, {**base, "checkpoint": "memory"}),
        ("4 进程 × 并发 256，检查点在内存", 4, 256, 1200, {**base, "checkpoint": "memory"}),
        ("4 进程 × 并发 256，SQLite 检查点", 4, 256, 1200, {**base, "checkpoint": "sqlite"}),
        ("4 进程 × 并发 16，共享 8 个模型名额", 4, 16, 160, {**base, "checkpoint": "memory", "model_slots": 8}),
    ]
    print("   配置                                任务数     耗时   任务/秒  等待上限  worker CPU 利用率  循环延迟 p99（最差进程）", flush=True)
    for i, (label, procs, conc, n_jobs, options) in enumerate(configs):
        r = asyncio.run(measure_workers(f"c{i}", procs=procs, concurrency=conc, n_jobs=n_jobs, options=options))
        slots = options.get("model_slots")
        wait_bound = min(procs * conc, slots or procs * conc) / 0.2  # 只受"等模型"限制时的上限：同时在跑的任务数 ÷ 0.2 秒
        info(f"{label:<34}{n_jobs:>6}{r['elapsed']:>8.2f}s{r['rate']:>9.0f}{wait_bound:>9.0f}{r['cpu_util']:>14.0%}"
             f"{r['lag_p99']:>16.0f}ms")
    info("等待上限 = 同时在跑的任务数 ÷ 每个任务等模型的 0.2 秒；worker CPU 利用率 = 各进程 CPU 秒 ÷ (忙碌时长 × 进程数)。")
    takeaway("1 个进程时，事件循环的 CPU 先到顶（利用率接近 100%，吞吐远低于等待上限）；加进程，吞吐跟着涨，每个进程的 CPU 回落。"
             "检查点换成共享的 SQLite 之后，每个任务多出好几次写事务，瓶颈变成'同一时刻只有一个写者'，worker 的 CPU 反而闲下来；"
             "所有进程共用 8 个模型名额时，加多少进程吞吐都被配额封顶。天花板一直在移动：先找到它在哪，再决定加什么。")


def scenario_throughput(args) -> None:
    banner("场景 1：吞吐与天花板 —— 一个进程能扛多少，加进程之后瓶颈跑到哪里去")
    scenario_throughput_one_process(args)
    scenario_waiters(args)
    scenario_throughput_processes(args)


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
    llm = ScriptedLLM([call_tools(*calls), reply("好的")])
    t0 = time.perf_counter()
    res = await Agent(llm, tools, parallel_tools=parallel).run("VPN 又连不上了，帮我看看")
    assert res.ok
    return time.perf_counter() - t0, [(name, t - t0) for name, t in tool_log]


async def scenario_parallel_tools(args) -> None:
    banner("场景 2：并行工具 —— 只读工具并行，写工具串行")
    reads = [("search_kb", {"query": "VPN 证书过期"}), ("get_user_profile", {"user": "alice"}), ("get_ticket_history", {"user": "alice"})]
    tools = [search_kb, get_user_profile, get_ticket_history, update_ticket]
    for parallel in (False, True):
        elapsed, log = await run_tools_once(reads, tools, parallel)
        starts = "，".join(f"{name.split()[0]} @{t * 1000:.0f}ms" for name, t in log)
        info(f"parallel_tools={parallel!s:<5}  3 个只读工具（各 0.3s）总耗时 {elapsed:.2f}s ｜ 开始时刻：{starts}")
    writes = [("update_ticket", {"ticket_id": "T1", "note": "第1步"}), ("update_ticket", {"ticket_id": "T1", "note": "第2步"})]
    elapsed, log = await run_tools_once(writes, tools, True)
    info(f"parallel_tools=True   2 个写工具：{' → '.join(name for name, _ in log)}（{elapsed:.2f}s）")
    elapsed, _ = await run_tools_once(reads[:2] + writes[:1], tools, True)
    info(f"parallel_tools=True   2 读 + 1 写混在一轮：{elapsed:.2f}s —— 只要有一个写工具，整轮都按顺序执行")
    takeaway("只读工具并行，一轮的耗时从'求和'变成'取最大'；写工具保持模型给出的顺序，副作用才可预测。")


# ==================================================================================== 场景 3：事件循环与三种执行方式


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
    llm = ScriptedLLM(responder=ticket_responder, latency=0.1)
    agent = Agent(llm, [lookup_ticket], hooks=[LegacyAuditHook(blocking)])
    hb = asyncio.create_task(heartbeat(lags, stop))
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
    step("3a：在 async 函数里调用阻塞 IO —— 一个租户的慢操作拖慢所有人")
    info("22 个会话同时跑：20 个普通租户（每个 2 次模型调用 × 0.1s，理想耗时 0.2s）+ 2 个 legacy 租户；")
    info(f"legacy 租户每次模型调用后要写一次审计（同步驱动，{AUDIT_WRITE}s）。一个心跳协程每 10ms 醒一次。")
    for blocking in (True, False):
        r = asyncio.run(blocking_experiment(blocking))
        label = "❌ 钩子里直接 time.sleep       " if blocking else "✅ await asyncio.to_thread(...)"
        info(
            f"{label} 心跳最大延迟 {r['lag_max'] * 1000:>5.0f}ms（p99 {r['lag_p99'] * 1000:>4.0f}ms，卡顿 >50ms 的次数 {r['stalls']}）"
            f" ｜ 普通租户完成时间 {dist(r['normal'])}"
        )
    info("打开 asyncio 调试模式，让事件循环自己报告'慢回调'（默认阈值 100ms）：")
    records: list = []
    with capture_logs("asyncio", records):
        asyncio.run(blocking_experiment(True), debug=True)
    slow = [r.getMessage() for r in records if "took" in r.getMessage() and "seconds" in r.getMessage()]
    info(f"调试模式记录到 {len(slow)} 条慢回调警告，例如（省略了中间的协程栈信息）：")
    for m in slow[:2]:
        task = re.search(r"<Task \w+ name='[^']+'", m)
        took = re.search(r"took [\d.]+ seconds", m)
        info(f"  Executing {task.group(0) if task else '<Task'} coro=<...one()...>> {took.group(0) if took else ''}")


ERP_STUCK = 2.0  # 3b：老 ERP 卡住多久（真实阻塞的线程）
erp_stats: dict = {"started": {}, "finished": set()}
erp_lock = threading.Lock()


@tool(timeout_s=0.5)
def legacy_erp(order_id: str) -> str:
    """查询老 ERP 系统里的订单（只有同步 SDK，框架会把它放进线程池执行）"""
    with erp_lock:
        erp_stats["started"][order_id] = time.perf_counter()
    time.sleep(ERP_STUCK if order_id.startswith("STUCK") else 0.05)  # 真实阻塞：这个线程在这段时间里什么都干不了
    with erp_lock:
        erp_stats["finished"].add(order_id)
    return f"{order_id}：已发货"


async def thread_pool_experiment(max_threads: int) -> dict:
    erp_stats["started"].clear()
    erp_stats["finished"].clear()

    def responder(messages):
        if messages[-1]["role"] == "tool":
            return reply(messages[-1]["content"])
        return call_tool("legacy_erp", order_id=messages[-1]["content"].split()[-1])

    agent = Agent(ScriptedLLM(responder=responder), [legacy_erp], max_threads=max_threads)
    t0 = time.perf_counter()

    async def one(order_id: str, delay: float):
        await asyncio.sleep(delay)
        res = await agent.run(f"查一下订单 {order_id}")
        tool_msg = next(m["content"] for m in res.messages if m["role"] == "tool")
        return order_id, "超时" in tool_msg, time.perf_counter() - t0

    stuck = [one(f"STUCK-{i}", 0) for i in range(4)]  # 4 个卡死的请求先到
    normal = [one(f"A-{i}", 0.01) for i in range(8)]  # 10ms 后 8 个正常请求
    results = await asyncio.gather(*stuck, *normal)
    returned_at = max(t for _, _, t in results)
    stuck_done_when_returned = sum(k.startswith("STUCK") for k in erp_stats["finished"])
    while len(erp_stats["finished"]) < len(erp_stats["started"]):  # 线程杀不掉：等它们自己跑完
        await asyncio.sleep(0.05)
    background_done = time.perf_counter() - t0
    await agent.aclose()
    normal_rows = [r for r in results if not r[0].startswith("STUCK")]
    return {
        "normal_timeout": sum(r[1] for r in normal_rows),
        "normal_started": sum(r[0] in erp_stats["started"] for r in normal_rows),
        "stuck_timeout": sum(r[1] for r in results if r[0].startswith("STUCK")),
        "returned_at": returned_at,
        "stuck_done_when_returned": stuck_done_when_returned,
        "background_done": background_done,
    }


def scenario_thread_pool(args) -> None:
    step("3b：同步工具的线程池 —— 4 个请求把老 ERP 卡住（每个 2 秒），紧接着来了 8 个正常请求（每个 0.05 秒）；工具超时 0.5 秒")
    for threads in (4, 16):
        r = asyncio.run(thread_pool_experiment(threads))
        info(f"max_threads={threads:<3} 正常请求：{8 - r['normal_timeout']}/8 成功，{r['normal_timeout']} 个超时"
             f"（其中真正开始执行过的 {r['normal_started']} 个）；卡住的 4 个全部按时超时（{r['stuck_timeout']}/4）")
        info(f"{'':15}所有运行 {r['returned_at']:.2f}s 就返回了，那时卡住的 4 个线程结束了 {r['stuck_done_when_returned']} 个；"
             f"它们在后台一直跑到 {r['background_done']:.2f}s 才结束 —— 线程杀不掉")
    takeaway("超时只是调用方'不等了'：卡住的线程照样占着池子。池子满了以后，新来的同步工具在队列里排队，"
             "而超时从排队就开始算，于是它们一行代码都没执行就'超时'了。线程池要有上限、要监控排队，IO 型工具尽快换 async。")


async def isolation_experiment(isolation: str | None) -> dict:
    from agentkit import ToolRegistry
    from agentkit.testing import busy_loop
    from agentkit.types import ToolCall

    spin = tool(busy_loop, name="spin", description="一段没有 await 点的纯 CPU 计算（模型写的代码陷入死循环）", timeout_s=0.5,
                isolation=isolation)
    lags: list[float] = []
    stop = asyncio.Event()
    hb = asyncio.create_task(heartbeat(lags, stop))
    cpu0, t0 = time.process_time(), time.perf_counter()
    result = await ToolRegistry([spin]).execute(ToolCall(id="c1", name="spin", arguments=json.dumps({"seconds": 2.0})))
    returned = time.perf_counter() - t0
    children_after = len(multiprocessing.active_children())
    await asyncio.sleep(max(0.0, 2.3 - returned))  # 再看 2.3 秒：超时之后，这段计算还在不在烧本进程的 CPU？
    stop.set()
    await hb
    return {"returned": returned, "error_type": result.error_type, "cpu": time.process_time() - cpu0,
            "lag_max": max(lags), "children_after": children_after}


async def isolated_roundtrip() -> tuple[float, int]:
    from agentkit import ToolRegistry
    from agentkit.testing import whoami_pid
    from agentkit.types import ToolCall

    who = tool(whoami_pid, name="whoami", description="返回执行它的进程号", isolation="process")
    t0 = time.perf_counter()
    result = await ToolRegistry([who]).execute(ToolCall(id="c1", name="whoami", arguments="{}"))
    return time.perf_counter() - t0, int(result.content)


def scenario_isolation(args) -> None:
    step("3c：CPU 密集 / 不可信的工具 —— 一段 2 秒的纯计算（没有 await 点），工具超时 0.5 秒")
    for isolation, label in ((None, "普通同步工具（线程池）"), ("process", "@tool(isolation=\"process\")")):
        r = asyncio.run(isolation_experiment(isolation))
        info(f"{label:<28} {r['returned']:.2f}s 拿到结果（{r['error_type']}）；从调用开始的 2.3 秒里本进程一共用了 "
             f"{r['cpu']:.2f}s CPU；心跳最大延迟 {r['lag_max'] * 1000:.0f}ms；拿到结果时还活着的子进程 {r['children_after']} 个")
    took, pid = asyncio.run(isolated_roundtrip())
    info(f"一次什么都不做的隔离调用（whoami_pid）：{took * 1000:.0f}ms，子进程 pid {pid} ≠ 本进程 {os.getpid()} —— 这是 spawn 的固定开销")
    takeaway("线程里的死循环：超时后还在后台把 CPU 烧完，而且通过 GIL 和事件循环抢时间片。进程隔离：到点直接 kill，"
             "CPU 当场释放；代价是每次几百毫秒的进程启动和参数必须能 pickle。不可信代码再往上是容器 / gVisor / microVM（第 19 课）。")


def scenario_runtime(args) -> None:
    banner("场景 3：事件循环与三种执行方式 —— 阻塞调用、线程池、进程隔离")
    scenario_blocking(args)
    scenario_thread_pool(args)
    scenario_isolation(args)


# ==================================================================================== 场景 4：取消


class FirstFixAgent(Agent):
    """仅作"修复前后对比"：第一次修复时的 _save。

    那一版只保护了收尾时的最后一次保存（它在受 shield 保护的 _finish 任务里），运行中途的保存直接 await。
    取消如果打在一次中途保存的"数据库已提交、客户端还没收到回复"之间，带版本号 CAS 的检查点记住的版本号就过期了，
    收尾保存会被当成冲突拒绝（讲义 2.5 节、7.2 节 R1）。现在的 Agent 对异步检查点的每一次保存都做了保护。
    """

    async def _save(self, state) -> None:
        async with self._io_locks.hold(state.run_id):
            await maybe_await(self._checkpointer().save(state))


class CommitThenCancel(SQLiteDB):
    """真实的 SQLite 数据库，外加一个故障注入点：第 k 次写事务**提交之后**、回复回到事件循环之前，取消正在运行的任务。

    这正是 R1 的窗口："数据库已经提交，客户端还没收到回复"。在数据库线程里提交完成后，用 call_soon_threadsafe
    把 task.cancel 排进事件循环 —— 它比"回复到达"的回调先执行。事务是真的提交了，取消也是真的打在那一刻。"""

    def __init__(self, path: Path):
        super().__init__(path)
        self.commits = 0
        self.cancel_at: int | None = None
        self.target: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def write(self, fn):
        def txn(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            self.commits += 1
            if self.commits == self.cancel_at and self.target is not None:
                self.loop.call_soon_threadsafe(self.target.cancel)  # 已提交；回复还在路上
            return out

        return await self.run(txn)


COMMIT_POINTS = {1: "用户消息落盘", 2: "模型回复（工具调用）落盘", 3: "工具结果落盘", 4: "最终回答落盘", 5: "收尾保存（状态 completed）"}


async def commit_sweep(cls, workdir: Path) -> list[tuple[int, str, str]]:
    """对每一个写入点 k：第 k 次提交之后立刻取消，记录调用方看到了什么、检查点最后停在什么状态。"""
    rows = []
    for k in COMMIT_POINTS:
        db = CommitThenCancel(workdir / f"{cls.__name__}_{k}.db")
        cp = SQLiteCheckpointer(db, table="runs")
        await cp.setup()  # 建表也是一次写事务：先建好，再开始数提交
        db.commits, db.cancel_at, db.loop = 0, k, asyncio.get_running_loop()
        llm = ScriptedLLM([call_tool("lookup_ticket", ticket_id="T1"), reply("工单 T1 处理中")], latency=lambda n: 0 if n == 1 else 0.05)
        agent = cls(llm, [lookup_ticket], checkpointer=cp)
        db.target = asyncio.create_task(agent.run("查一下工单 T1", run_id="r1"))
        try:
            await db.target
            seen = "正常返回"
        except asyncio.CancelledError:
            seen = "CancelledError"
        except Exception as e:  # noqa: BLE001 —— 正是要看到它：取消被一个检查点冲突"替换"了
            seen = type(e).__name__
        row = await cp.get_run("r1")  # 只读，不"接管"
        rows.append((k, seen, row["status"] if row else "none"))
        await db.close()
    return rows


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
    """同一个竞态出现在 Agent 里：同步工具（线程池 + 超时）执行完的那一刻，用户断开。
    返回 (取消丢失、运行跑完的次数, 尝试次数)。agentkit 用自己的取消安全 wait_for（timeouts.py），应为 0。"""
    lost = 0
    for _ in range(attempts):
        loop = asyncio.get_running_loop()
        holder: dict[str, asyncio.Task] = {}

        @tool(name="lookup_ticket")
        def racing_lookup(ticket_id: str) -> str:
            """查询工单状态（同步工具，在线程池里执行；返回前用户恰好断开）"""
            loop.call_soon_threadsafe(holder["task"].cancel)
            return f"{ticket_id}：处理中"

        llm = ScriptedLLM([call_tool("lookup_ticket", ticket_id="T1"), reply("完成")], latency=lambda n: 0 if n == 1 else 0.05)
        holder["task"] = asyncio.create_task(Agent(llm, [racing_lookup]).run("查一下工单 T1"))
        try:
            await holder["task"]
            lost += 1  # 正常返回 = 取消丢了，第二次模型调用照样发生
        except asyncio.CancelledError:
            pass
    return lost, attempts


class RedisLikeHook(Hook):
    """after_llm 里调用一个"依赖库"：它内部用标准库的 asyncio.wait_for 等回复 —— 3.12 之前的 redis-py、psycopg_pool 就是这样写的
    （第 31 课压测时实测：限流 Hook 调 Redis，约 1/4 的这类取消被吞）。这里让回复和取消在同一轮事件循环里到达（gh-86296 的窗口），
    看框架能不能把被吞掉的取消补回来。"""

    def __init__(self):
        self.swallowed: bool | None = None

    async def after_llm(self, state, response):
        if state.step != 1:
            return None
        loop = asyncio.get_running_loop()
        reply_fut = loop.create_future()
        loop.call_soon(reply_fut.set_result, "OK")  # 依赖库的回复到了……
        loop.call_soon(asyncio.current_task().cancel)  # ……用户恰好在同一刻断开
        try:
            await asyncio.wait_for(reply_fut, 5)
        except asyncio.CancelledError:
            self.swallowed = False
            raise
        self.swallowed = True  # wait_for 正常返回了：取消被依赖库吞掉
        return None


side_effects: list[str] = []


@tool(risk="write")
async def create_ticket(title: str) -> str:
    """创建工单（写操作，有副作用）"""
    side_effects.append(title)
    return "T-1001"


async def swallowed_cancel_experiment() -> dict:
    side_effects.clear()
    hook = RedisLikeHook()
    llm = ScriptedLLM([call_tool("create_ticket", title="VPN 连不上"), reply("已建单 T-1001")])
    agent = Agent(llm, [create_ticket], hooks=[hook])
    records: list = []
    with capture_logs("agentkit", records):
        task = asyncio.create_task(agent.run("VPN 连不上了，帮我建个工单", run_id="swallow"))
        try:
            await task
            seen = "正常返回"
        except asyncio.CancelledError:
            seen = "CancelledError"
    state = await maybe_await(agent.checkpointer.load("swallow"))
    events = [getattr(r, "agentkit_event", None) for r in records]
    return {"swallowed": hook.swallowed, "seen": seen, "status": state.status, "tickets": len(side_effects),
            "llm_calls": llm.call_count, "logged": "swallowed_cancellation" in events}


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


class UvicornProcess:
    """用**真实的子进程**启动 uvicorn：python -m uvicorn --app-dir lessons/30_async_runtime sse_app:app --fd N。

    监听 socket 由本进程先绑好端口（127.0.0.1:0，由系统分配），再把文件描述符交给子进程（pass_fds），
    这样不用猜端口、也没有"先查空闲端口再启动"的竞态。退出时发 SIGTERM（uvicorn 的优雅停机），超时再 SIGKILL。"""

    def __init__(self, env: dict, log_path: Path):
        self.env, self.log_path = env, log_path
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        import httpx

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
        env.update(self.env)
        cmd = [sys.executable, "-m", "uvicorn", "--app-dir", str(HERE), "sse_app:app", "--fd", str(sock.fileno()),
               "--lifespan", "on", "--log-level", "warning"]
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(cmd, pass_fds=(sock.fileno(),), env=env, cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
        sock.close()  # 子进程手里有一份，本进程这份可以关掉
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    return base
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"uvicorn 没能启动：\n{self.log_path.read_text(errors='replace')[-2000:]}")
            time.sleep(0.1)

    def __exit__(self, *exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)


async def sse_until(client, path: str, params: dict, stop_event: str | None) -> list[str]:
    """读 SSE 事件；读到 stop_event 就断开连接（模拟用户关掉页面）。返回读到的事件名。"""
    seen: list[str] = []
    async with client.stream("GET", path, params=params) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                seen.append(line.removeprefix("event: "))
                if seen[-1] == stop_event:
                    break  # 退出 async with → 关闭 TCP 连接
    return seen


async def poll_status(client, run_id: str, variant: str, want: str, timeout: float = 3.0) -> tuple[dict, float]:
    t0 = time.perf_counter()
    while True:
        st = (await client.get(f"/runs/{run_id}", params={"variant": variant})).json()
        if (st["status"] == want and st["llm_in_flight"] == 0) or time.perf_counter() - t0 > timeout:
            return st, time.perf_counter() - t0
        await asyncio.sleep(0.01)


REPEATS = 20  # 4c ③ 每种检查点断开几次（竞态问题要看比例，不能只看一次）


async def cancel_experiment(base_url: str, backends: list[tuple[str, str]], workdir: Path) -> None:
    import httpx

    q = {"q": "VPN 连不上了"}
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        health = (await client.get("/health")).json()
        info(f"服务端是另一个进程：pid {health['pid']}（本进程 {os.getpid()}）；可用的检查点：{health['variants']}")

        step("4a. 客户端读到第一个工具事件 tool_started 就断开（工具要跑 0.5 秒，此时正在执行）")
        seen = await sse_until(client, "/chat/stream", {**q, "run_id": "r-a", "variant": "a"}, "tool_started")
        st, waited = await poll_status(client, "r-a", "a", "cancelled")
        info(f"客户端读到：{' → '.join(seen)}，然后断开")
        info(f"断开后 {waited * 1000:.0f}ms：服务端检查点（SQLite）status={st['status']!r}；工具开始 {st['tool_started']} 次、"
             f"执行完 {st['tool_completed']} 次（被取消在半路）；这次只读调用在历史里被补成：{st['last_tool_message']!r}")
        assert st["status"] == "cancelled" and st["tool_completed"] == 0, st

        step("4b. 读到 tool_finished 再断开：此时服务端正在进行第 2 次模型调用（剧本设定要 30 秒）")
        seen = await sse_until(client, "/chat/stream", {**q, "run_id": "r-b", "variant": "b"}, "tool_finished")
        st, waited = await poll_status(client, "r-b", "b", "cancelled")
        info(f"客户端读到：{' → '.join(seen)}，然后断开")
        info(f"断开后 {waited * 1000:.0f}ms：检查点 status={st['status']!r}（版本 {st['version']}），模型累计被调用 {st['llm_calls']} 次，"
             f"在途模型调用 {st['llm_in_flight']} 个 —— 30 秒的调用被当场取消，没有继续花钱")
        assert st["status"] == "cancelled" and st["llm_calls"] == 2 and st["llm_in_flight"] == 0, st

        step("4c. 异步检查点 + 取消：本课发现并修复的缺陷（讲义 2.5 节）")
        before = await level_triggered_cleanup(shielded=False)
        after = await level_triggered_cleanup(shielded=True)
        info(f"① 根因的最小复现（AnyIO 电平触发取消）：finally 里直接 await 保存 → {before!r}；放进 shield 作用域 → {after!r}")
        info("② R1 在真实 SQLite 上复现：第 k 次写事务提交之后、回复回到事件循环之前取消（每个写入点各一次）")
        sweeps = {cls.__name__: await commit_sweep(cls, workdir) for cls in (FirstFixAgent, Agent)}
        info(f"   {'写入点':<22}{'第一次修复（只保护收尾保存）':<34}现在的 Agent（每次异步保存都 shield）")
        for (k, seen_a, status_a), (_, seen_b, status_b) in zip(sweeps["FirstFixAgent"], sweeps["Agent"]):
            label = f"{k}. {COMMIT_POINTS[k]}"
            info(f"   {label:<20}{f'{seen_a} / 检查点 {status_a}':<36}{seen_b} / 检查点 {status_b}")
        info("③ SSE 端到端：客户端读到 tool_finished 就断开，每种真实检查点各 20 次")
        for variant, label in backends:
            outcomes: dict[str, int] = {}
            in_flight = 0
            for i in range(REPEATS):
                run_id = f"r-{variant}-{i}"
                await sse_until(client, "/chat/stream", {**q, "run_id": run_id, "variant": variant}, "tool_finished")
                st, _ = await poll_status(client, run_id, variant, "cancelled", timeout=1.0)
                outcomes[st["status"]] = outcomes.get(st["status"], 0) + 1
                in_flight = max(in_flight, st["llm_in_flight"])
            summary = "，".join(f"{k} × {v}" for k, v in sorted(outcomes.items()))
            info(f"   {label:<40}断开 {REPEATS} 次 → {summary}；在途模型调用最多 {in_flight} 个")
        stats = (await client.get("/stats")).json()
        info(f"   服务端日志里 'Task exception was never retrieved'：{stats['never_retrieved']} 条（R2 修复后应为 0）")

        info(f"④ Python {platform.python_version()} 的 asyncio.wait_for 竞态（CPython gh-86296）")
        info(f"   最小复现：内部结果和外部取消同时到达 → {await waitfor_race_minimal()}")
        lost, attempts = await waitfor_race_in_agent()
        info(f"   Agent：同步工具执行完的那一刻断开，{attempts} 次里有 {lost} 次取消丢失"
             f"（agentkit 的工具执行、run_timeout、舱壁都用 timeouts.wait_for，应为 0）")
        r = await swallowed_cancel_experiment()
        info("⑤ 依赖库吞掉取消：after_llm 钩子里一个'像 redis-py 那样用 asyncio.wait_for'的调用，回复和取消同时到达")
        how = "正常返回（取消被吞掉）" if r["swallowed"] else "抛出 CancelledError（这个 Python 版本的 wait_for 不吞取消）"
        info(f"   钩子里的 wait_for：{how}；调用方看到 {r['seen']}；检查点 status={r['status']!r}；"
             f"写工具执行了 {r['tickets']} 次，模型调用了 {r['llm_calls']} 次")
        if r["swallowed"]:
            info(f"   Agent 在执行工具之前发现 Task.cancelling() 比进入运行时大，补抛了取消；"
                 f"日志带稳定字段 agentkit_event=swallowed_cancellation：{r['logged']}")

        step("4d. 断线重连 ≠ 从头再来：用 4b 的 run_id 调 /chat/resume，从检查点接着跑")
        seen = await sse_until(client, "/chat/resume", {"run_id": "r-b", "variant": "b"}, None)
        st, _ = await poll_status(client, "r-b", "b", "completed")
        info(f"恢复后的事件：{' → '.join(dict.fromkeys(seen))}；最终 status={st['status']!r}；"
             f"工具累计执行完 {st['tool_completed']} 次（结果在检查点里，没有重跑），模型累计调用 {st['llm_calls']} 次")


def scenario_cancel(args) -> None:
    banner("场景 4：取消 —— 用户关掉页面，服务端要真的停下来")
    if not has("fastapi", "uvicorn", "httpx", "anyio"):
        info("⚠️  缺少 fastapi / uvicorn / httpx，跳过本场景。安装：")
        info(f"    {INSTALL_HINT}")
        return
    workdir = RUNS / "cancel"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    with contextlib.ExitStack() as stack:
        env = {"LESSON30_SQLITE": str(workdir / "server.db")}
        backends = [("sqlite", "SQLiteCheckpointer（真实 SQLite 文件）")]
        if has("pgserver", "psycopg", "psycopg_pool"):
            from agentkit.testing import embedded_postgres

            env["LESSON30_PG_URI"] = stack.enter_context(embedded_postgres())
            backends.append(("pg", "PostgresCheckpointer（嵌入式 Postgres）"))
            info("嵌入式 Postgres（pgserver，真正的 postgres 进程，unix socket）已启动")
        else:
            info(f"（没有 pgserver / psycopg，4c ③ 只用 SQLite；想再用真实 Postgres 验证一遍：{INSTALL_HINT}）")
        server = UvicornProcess(env, workdir / "uvicorn.log")
        base_url = stack.enter_context(server)
        info(f"uvicorn 子进程已启动：{base_url}（pid {server.proc.pid}；python -m uvicorn --app-dir lessons/30_async_runtime sse_app:app）")
        asyncio.run(cancel_experiment(base_url, backends, workdir))
    takeaway("取消链路：TCP 断开 → uvicorn 发出 http.disconnect → Starlette 取消响应任务 → aclosing 关闭 agent.stream → "
             "运行任务被取消 → 模型调用停止、检查点（每次保存都受 shield 保护）记为 cancelled。")


# ==================================================================================== 场景 5：舱壁与背压


async def bulkhead_experiment(limiter: KeyedLimiter | None, limiter_timeout: float | None) -> dict:
    model = ScriptedLLM(responder=lambda m: reply("好的"), latency=0.2)
    llm = ResilientLLM(model, max_concurrency=8)  # 这个进程对模型的并发上限（例如网关给的配额）
    agent = Agent(llm, [], limiter=limiter, limiter_timeout=limiter_timeout)
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


async def limit_child(mode: str, db_path: str, start_at: float, calls: int, latency: float, limit: int) -> dict:
    """场景 5b 的子进程：calls 个并发的"模型调用"，mode=local 用本进程的 asyncio.Semaphore(limit)，
    mode=shared 用所有进程共享的 SQLiteSemaphore(limit)。返回每次调用占用名额的时间段（time.time()，同一台机器的时钟）。"""
    if mode == "local":
        sem = asyncio.Semaphore(limit)
        slot = lambda: sem  # noqa: E731
    else:
        shared = SQLiteSemaphore(db_path, "gateway", limit=limit, lease_seconds=10, poll_interval=0.02)
        await shared.setup()
        slot = shared.slot
    await asyncio.sleep(max(0.0, start_at - time.time()))  # 所有进程在同一时刻开始
    intervals: list[tuple[float, float]] = []

    async def one() -> None:
        async with slot():
            t0 = time.time()
            await asyncio.sleep(latency)  # 在名额里"等模型"
            intervals.append((t0, time.time()))

    await asyncio.gather(*(one() for _ in range(calls)))
    if mode == "shared":
        await shared.close()
    return {"pid": os.getpid(), "intervals": intervals}


def peak_overlap(intervals: list[tuple[float, float]]) -> int:
    events = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals], key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def cross_process_limits(procs: int = 3, calls: int = 12, limit: int = 4, latency: float = 0.2) -> None:
    step(f"5b. 跨进程：{procs} 个真实进程，每个同时发 {calls} 个模型调用（各占名额 {latency}s）；网关只给了 {limit} 个并发")
    workdir = RUNS / "limits"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    db = workdir / "limits.db"

    async def prepare():
        s = SQLiteSemaphore(db, "gateway", limit=limit)
        await s.setup()
        await s.close()

    asyncio.run(prepare())
    for mode, label in (("local", f"每个进程各自 asyncio.Semaphore({limit})"), ("shared", f"共享 SQLiteSemaphore({limit})")):
        start_at = time.time() + 3.0  # 给子进程留出启动、import 的时间
        children = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_limit_child", mode, str(db), str(start_at),
                                      str(calls), str(latency), str(limit)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(REPO))
                    for _ in range(procs)]
        outs = []
        for c in children:
            out, err = c.communicate(timeout=120)
            if c.returncode != 0:
                raise RuntimeError(err[-2000:])
            outs.append(json.loads(out.strip().splitlines()[-1]))
        all_iv = [iv for o in outs for iv in o["intervals"]]
        per_proc = [peak_overlap(o["intervals"]) for o in outs]
        elapsed = max(b for _, b in all_iv) - start_at
        info(f"{label:<34} 全局同时在途峰值 {peak_overlap(all_iv):>2}（各进程 {per_proc}，pid 各不相同：{len({o['pid'] for o in outs})} 个）；"
             f"{len(all_iv)} 个调用用时 {elapsed:.2f}s")
    takeaway(f"进程内的信号量只管得了自己：{procs} 个进程各设 {limit}，网关实际收到 {procs}×{limit}。"
             "跨进程的配额要放进所有进程都看得见的地方：单机用 SQLiteSemaphore（名额带租约，持有者被 kill -9 也会自动归还），"
             "多机用 Redis 或网关自己的限额（第 26、29 课）。代价是每次拿名额多一次数据库往返。")


def scenario_bulkhead(args) -> None:
    banner("场景 5：舱壁与背压 —— 进程内按租户隔离，跨进程共享配额")
    step("5a. 进程内：一个租户发 50 个请求，另一个租户只发 2 个")
    info("每个请求 1 次模型调用 × 0.2s；模型并发上限 8（ResilientLLM(max_concurrency=8)）。时间从请求到达算起。")
    configs = (
        ("只有全局上限（没有按租户隔离）", None, None),
        ("KeyedLimiter(per_key=4, global_limit=8)", KeyedLimiter(per_key=4, global_limit=8), None),
        ("同上 + limiter_timeout=0.5s（排不上就 429）", KeyedLimiter(per_key=4, global_limit=8), 0.5),
    )
    for label, limiter, timeout in configs:
        r = asyncio.run(bulkhead_experiment(limiter, timeout))
        info(f"【{label}】")
        info(f"   安静租户 {len(r['quiet'])} 个完成：{dist(r['quiet'])}")
        rejected = f"，{r['noisy_rejected']} 个被快速拒绝（rate_limited）" if r["noisy_rejected"] else ""
        info(f"   吵闹租户 {len(r['noisy'])} 个完成：{dist(r['noisy'])}{rejected}")
        info(f"   模型同一时刻最多 {r['max_in_flight']} 个在途调用")
    takeaway("没有舱壁时，安静租户排在吵闹租户的 50 个请求后面；有了舱壁，它的延迟和'系统空闲时'一样。"
             "代价：吵闹租户被限在 4 并发，即使模型还有空闲名额。排不上就快速拒绝（背压），比让请求挂着等更好。")
    cross_process_limits()


# ==================================================================================== 场景 6：真实模型


class TimedLLM:
    """包在真实模型外面：记录每次调用的耗时和同时在途数（证明并发上限生效）。"""

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
    from agentkit import default_llm

    timed = TimedLLM(default_llm(max_connections=10))
    llm = ResilientLLM(timed, max_concurrency=3, max_attempts=2)
    agent = Agent(llm, [get_ticket_status], system_prompt="你是 IT 服务台助手。需要工单信息时调用工具。回答不超过两句话。")
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
    parser = argparse.ArgumentParser(description="第 30 课 Demo：生产环境的高并发异步运行时")
    parser.add_argument("--offline", action="store_true", help="不调用真实模型（跳过场景 6）")
    parser.add_argument("--full", action="store_true", help="场景 1a 的'一个接一个'跑满 200 个会话（约 80 秒）")
    parser.add_argument("--only", default="1,2,3,4,5,6", help="只运行指定场景，如 --only 3,4")
    parser.add_argument("--repeat", type=int, default=1, help="场景 1a 每种方式重复运行几次、取中位数（机器繁忙时用 3）")
    parser.add_argument("--_bench", nargs=2, help=argparse.SUPPRESS)
    parser.add_argument("--_waiters", nargs=2, help=argparse.SUPPRESS)
    parser.add_argument("--_limit_child", nargs=6, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args._bench:  # 场景 1a 的子进程入口
        print(json.dumps(bench_sessions(args._bench[0], int(args._bench[1]))))
        return
    if args._waiters:
        print(json.dumps(bench_waiters(args._waiters[0], int(args._waiters[1]))))
        return
    if args._limit_child:
        mode, db_path, start_at, calls, latency, limit = args._limit_child
        print(json.dumps(asyncio.run(limit_child(mode, db_path, float(start_at), int(calls), float(latency), int(limit)))))
        return

    print(
        f"🖥  {platform.system()} {platform.release()} / {platform.machine()} / {os.cpu_count()} 核 ｜ "
        f"Python {platform.python_version()} ({platform.python_implementation()}) ｜ load average {load_avg()}"
    )
    print("🔌 离线模式：场景 1–5 用剧本模型（asyncio.sleep 扮演等模型）" if args.offline else "🌐 场景 1–5 用剧本模型；场景 6 调用真实模型")
    started = time.time()
    scenarios = {
        "1": scenario_throughput,
        "2": lambda a: asyncio.run(scenario_parallel_tools(a)),
        "3": scenario_runtime,
        "4": scenario_cancel,
        "5": scenario_bulkhead,
        "6": scenario_real,
    }
    for key in args.only.replace(" ", "").split(","):
        scenarios[key](args)

    banner("小结")
    info("1. 容量 = 同时在飞的会话数 ÷ 每个会话的时长（利特尔法则）；asyncio 让一个进程等得起几千个，但事件循环的 CPU、")
    info("   共享数据库的写锁、模型配额会轮流成为天花板 —— 先测出它在哪，再决定加进程、换数据库还是去谈配额。")
    info("2. 规模上来之后最容易坏的是'停下来'：取消要一路传到模型调用，收尾保存要受保护，被依赖库吞掉的取消要补抛。")
    info("3. 事件循环是共享的：一次同步 IO、一个卡住的线程池、一段死循环，影响的是整个进程；进程内的限额管不到别的进程。")
    info(f"总耗时 {time.time() - started:.0f} 秒。")


if __name__ == "__main__":
    main()
