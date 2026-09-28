"""第 08 课 Demo：可靠性工程 —— 让 Agent 在失败中存活。

    python lessons/08_reliability/demo.py                     # 真实模型（读取 .env）
    python lessons/08_reliability/demo.py --offline           # 离线剧本（ScriptedLLM），无需 API key
    python lessons/08_reliability/demo.py --offline --only 1  # 只跑某个场景（1-4）
    python lessons/08_reliability/demo.py --offline --keep    # 运行产物（检查点、SQLite 文件）留在 runs/08_reliability/

开始时把 services.py 作为一个**独立的进程**拉起来，扮演三个下游：容量有限的模型网关、一个坏掉的主模型、
一个工单系统（它在 Agent 进程之外，Agent 崩溃不会让它"回滚"）。演示结束时关掉它。

四个场景：
  1. 重试：模型连续两次 429，第三次成功。然后是一个真实的惊群实验：500 个并发客户端同时打网关进程，
     对比 固定间隔 / 指数退避（无抖动）/ 指数退避 + 全抖动，所有数字来自网关自己的日志
  2. 熔断 + 降级：
     A. 进程内：主模型不可用 → 熔断 → 走备用模型；真的等 reset_timeout 秒 → 半开试探 → 关闭
     B. 跨进程：worker 进程通过 SQLiteCircuitBreaker 共享熔断状态 —— 进程 A 把熔断器打开，
        进程 B 第一次调用就快速失败，一次都没有打到坏掉的主模型（调用次数由主模型服务自己计数）
  3. 崩溃恢复：进程 1 调用建单工具后被杀死（退出码 137），进程 2 从检查点接手。四个用例对比：
     没有幂等 / SQLiteIdempotencyStore / 崩在"下游已建单、幂等记录还没写"的更窄窗口 / 再把幂等键传给下游
  4. 预算：BudgetHook 截停一个停不下来的 Agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Literal

import httpx
from pydantic import Field

from agentkit import (
    Agent,
    BudgetHook,
    FileCheckpointer,
    Hook,
    LLMError,
    OpenAICompatLLM,
    ResilientLLM,
    ScriptedLLM,
    ToolContext,
    call_tool,
    default_llm,
    render_tree,
    reply,
    tool,
    wait_for,
)
from agentkit.config import env
from agentkit.distributed import SQLiteCircuitBreaker, SQLiteIdempotencyStore
from agentkit.reliability import backoff_delay

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNS = ROOT / "runs" / "08_reliability"
MAIN_MODEL = env("LLM_MODEL", "gpt-5.5")


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 72 + f"\n  {title}\n" + "═" * 72, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def short(text: str | None, n: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def pad(text: str, width: int) -> str:
    """按终端显示宽度补空格（中文字符占 2 列），让中英文混排的表格对齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def answered_by(result) -> str:
    """从 trace 里读出最后一次模型调用实际用的是哪个模型。"""
    llm_spans = [s for s in result.trace.walk() if s.name == "llm.chat"]
    return llm_spans[-1].attrs.get("gen_ai.response.model", "?") if llm_spans else "（没有调用模型）"


async def run_child(*args: str) -> int:
    """再启动一个本脚本的进程（隐藏参数决定它扮演什么角色），等它退出，返回退出码。"""
    sys.stdout.flush()  # 子进程继承同一个标准输出：先把自己缓冲区里的字刷出去，输出才不会乱序
    proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), *args)
    return await proc.wait()


# ---------------------------------------------------------------- 外部服务进程


class LocalServices:
    """把 services.py 拉起为独立进程；退出 async with 时发 SIGTERM，5 秒内不退出就 SIGKILL。"""

    def __init__(self, capacity: int, service_time: float):
        self.capacity, self.service_time = capacity, service_time

    async def __aenter__(self) -> "LocalServices":
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "services.py"), "--capacity", str(self.capacity),
            "--service-time", str(self.service_time), stdout=asyncio.subprocess.PIPE,
        )
        line = await wait_for(self.proc.stdout.readline(), 30)
        if not line:
            raise RuntimeError("services.py 启动失败（看上面的报错）")
        meta = json.loads(line)
        self.port, self.pid = meta["port"], meta["pid"]
        self.url = f"http://127.0.0.1:{self.port}"
        self.http = httpx.AsyncClient(base_url=self.url, timeout=30)
        return self

    async def get(self, path: str) -> dict:
        r = await self.http.get(path)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str) -> dict:
        r = await self.http.post(path)
        r.raise_for_status()
        return r.json()

    async def __aexit__(self, *exc) -> None:
        await self.http.aclose()
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()


# ---------------------------------------------------------------- 故障注入


class FlakyLLM:
    """故障注入包装器：前几次调用按剧本抛错，之后透传给内层模型。

    这就是混沌工程（chaos engineering）的最小形态：不等故障自然发生，主动制造故障，
    验证你的重试 / 熔断 / 降级代码真的会按预期工作。
    """

    def __init__(self, inner, errors: list[Exception]):
        self.inner = inner
        self.errors = list(errors)
        self.model = inner.model
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return await self.inner.chat(messages, tools, **kwargs)


def rate_limited() -> LLMError:
    return LLMError("Error code: 429 - Rate limit reached, please retry later", status_code=429, retryable=True)


def model_not_found(name: str) -> LLMError:
    # 与真实接口返回的错误同形：400 属于"重试也没用"的错误
    return LLMError(f"Error code: 400 - unknown provider for model {name}", status_code=400, retryable=False)


# =====================================================================
# 场景 1：重试
# =====================================================================


async def scenario_retry(offline: bool, svc: LocalServices, n_clients: int) -> None:
    banner("场景 1：重试 —— 模型连续两次 429（限流），第三次成功")
    inner = (
        ScriptedLLM([reply("因为失败往往是暂时的：等待时间指数增长给服务端恢复的时间，随机抖动避免所有客户端同时重试。")])
        if offline
        else default_llm()
    )
    flaky = FlakyLLM(inner, [rate_limited(), rate_limited()])
    llm = ResilientLLM(flaky, max_attempts=4, base_delay=0.4)  # 用真实的 asyncio.sleep，让你感受到退避的等待

    step("Agent → ResilientLLM → FlakyLLM（前两次抛 429）→ 模型")
    t0 = time.time()
    res = await Agent(llm, [], system_prompt="你是简洁的技术讲师，回答不超过 60 个字。").run(
        "用一句话解释：调用模型 API 时，为什么要用'指数退避 + 随机抖动'来重试？"
    )
    for e in llm.events:
        info(f"📝 {short(e)}")
    info(f"✅ 状态={res.status}，底层共调用 {flaky.calls} 次，总耗时 {time.time() - t0:.1f}s")
    info(f"🤖 回答：{short(res.output, 200)}")
    takeaway("Agent 完全感知不到这两次失败 —— 重试被封装在 LLM 装饰器里，业务代码零改动。")

    step("对照：如果错误是 400（参数错误），重试有用吗？")
    bad = FlakyLLM(ScriptedLLM([]), [LLMError("Error code: 400 - invalid 'messages' format", status_code=400, retryable=False)])
    llm2 = ResilientLLM(bad, max_attempts=4, base_delay=0.4)
    res2 = await Agent(llm2, []).run("你好")
    info(f"底层调用次数：{bad.calls}（没有重试），状态={res2.status}，给用户的回复：{res2.output}")
    takeaway("只重试'可能成功'的错误：429/5xx/超时/断连。400/401/403 重试一万次也是同样的结果，只会浪费配额。")

    await thundering_herd(svc, n_clients)


# ---------------------------------------------------------------- 惊群实验


HERD_CAPACITY = 50  # 网关同时处理的请求数上限（启动 services.py 时传入）
HERD_SERVICE_TIME = 0.1  # 网关处理一个请求的耗时：吞吐上限 = 50 / 0.1s = 每秒 500 个
HERD_MAX_ATTEMPTS = 30


def herd_strategies() -> list[tuple[str, Callable[[int], float]]]:
    """三种重试等待策略。唯一的区别是"第 n 次失败后等多久"。"""
    return [
        ("固定间隔 0.5s", lambda n: 0.5),
        ("指数退避，无抖动", lambda n: min(1.0, 0.1 * 2 ** (n - 1))),  # 0.1、0.2、0.4、0.8、1.0、1.0……
        ("指数退避 + 全抖动", lambda n: backoff_delay(n, base=0.1, cap=1.0)),  # agentkit 的实现：[0, 上面那个值] 里随机
    ]


class RawHttpClient:
    """一个客户端 = 一条 keep-alive 的 TCP 连接。请求字节手工拼好，响应只解析状态行和 Content-Length。

    为什么不用 httpx？实测在这台机器上，httpx 在一个进程里每秒只能发出一两百个请求：
    500 个"同时"发出的请求，被它自己的 CPU 开销摊开到了好几秒 —— 惊群还没到网关，就先被客户端抹平了。
    （真实世界里 500 个客户端跑在 500 台机器上，它们是真的同时到达。）手写的客户端每个请求只要几十微秒。
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer

    async def post(self, path: str) -> int:
        self.writer.write(f"POST {path} HTTP/1.1\r\nHost: gateway\r\nContent-Length: 0\r\n\r\n".encode())
        head = await self.reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
        await self.reader.readexactly(length)
        return int(head.split(b" ", 2)[1])

    def close(self) -> None:
        self.writer.close()


async def open_clients(port: int, n: int) -> list[RawHttpClient]:
    clients = []
    for i in range(0, n, 100):  # 分批建连接：macOS 的 listen 队列上限是 128，一次建 500 条会有连接被丢弃重试
        batch = await asyncio.gather(*(asyncio.open_connection("127.0.0.1", port) for _ in range(min(100, n - i))))
        clients += [RawHttpClient(r, w) for r, w in batch]
    return clients


@dataclass
class HerdResult:
    name: str
    requests: int
    rejected: int
    retry_peak_10ms: int  # 任意 10ms 里到达的"重试"请求数的最大值：衡量重试有多"整齐"
    per_100ms: list[int]  # 每 100ms 到达网关的请求数（直方图）
    all_done: float  # 最后一个客户端成功的时刻（秒）
    half_done: float  # 一半客户端成功的时刻
    gave_up: int
    utilization: float  # 网关处理槽位的利用率


async def run_herd(svc: LocalServices, clients: list[RawHttpClient], name: str, delay: Callable[[int], float]) -> HerdResult:
    go = asyncio.Event()

    async def one_client(c: RawHttpClient) -> float | None:
        await go.wait()  # 所有客户端在同一时刻出发 —— 就像它们在 t=0 同时收到了 429
        for attempt in range(1, HERD_MAX_ATTEMPTS + 1):
            if await c.post(f"/gateway?attempt={attempt}") == 200:
                return time.time()
            if attempt < HERD_MAX_ATTEMPTS:
                await asyncio.sleep(delay(attempt))
        return None  # 重试次数用尽，放弃

    tasks = [asyncio.create_task(one_client(c)) for c in clients]
    await asyncio.sleep(0.2)  # 让所有客户端都停在 go.wait() 上
    start = time.time()
    go.set()
    finished = await asyncio.gather(*tasks)
    log = (await svc.get("/gateway/log"))["log"]  # 服务端自己的记录：[到达时间, 第几次尝试, 返回码]

    rel = [(t - start, attempt, status) for t, attempt, status in log]
    last = max(t for t, _, _ in rel)
    buckets = Counter(int(t / 0.1) for t, _, _ in rel)
    done = sorted(t - start for t in finished if t is not None)
    accepted = sum(1 for _, _, s in rel if s == 200)
    makespan = done[-1] if done else last
    return HerdResult(
        name=name, requests=len(rel), rejected=sum(1 for _, _, s in rel if s == 429),
        retry_peak_10ms=densest_window(sorted(t for t, attempt, _ in rel if attempt > 1), 0.01),
        per_100ms=[buckets.get(i, 0) for i in range(int(last / 0.1) + 1)],
        all_done=makespan, half_done=done[len(done) // 2] if done else float("nan"),
        gave_up=finished.count(None),
        utilization=accepted * HERD_SERVICE_TIME / (max(makespan, 1e-9) * HERD_CAPACITY),
    )


def densest_window(times: list[float], width: float) -> int:
    """滑动窗口：任意 width 秒内最多有几个时间点（times 已排序）。"""
    best, lo = 0, 0
    for hi, t in enumerate(times):
        while t - times[lo] > width:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def sparkline(counts: list[int], full: int) -> str:
    """一个字符 = 100ms。· 表示这 100ms 里一个请求都没有；█ 表示达到 full。"""
    blocks = "▁▂▃▄▅▆▇█"
    return "".join("·" if c == 0 else blocks[min(7, max(0, math.ceil(c / full * 8) - 1))] for c in counts)


async def thundering_herd(svc: LocalServices, n: int) -> None:
    step(f"为什么一定要加抖动？真实实验：{n} 个客户端在同一时刻打同一个网关进程")
    info(f"网关（services.py，独立进程 pid={svc.pid}）：同时最多处理 {HERD_CAPACITY} 个请求，每个耗时 "
         f"{HERD_SERVICE_TIME * 1000:.0f}ms，满了立刻返回 429 → 吞吐上限每秒 {HERD_CAPACITY / HERD_SERVICE_TIME:.0f} 个")
    info(f"客户端：{n} 个协程，每个一条 keep-alive 连接；收到 429 就等一会儿再试（最多 {HERD_MAX_ATTEMPTS} 次）。"
         "三种策略只有'等多久'不同")
    clients = await open_clients(svc.port, n)
    await svc.get("/gateway/log")  # 清空日志
    results = []
    try:
        for name, delay in herd_strategies():
            results.append(await run_herd(svc, clients, name, delay))
    finally:
        for c in clients:
            c.close()

    info("")
    info(f"每 100ms 到达网关的请求数（一个字符 = 100ms，█ = {n} 个，· = 0 个；数据来自网关日志）：")
    for r in results:
        info(f"  {pad(r.name, 18)} {sparkline(r.per_100ms[:80], n)}{' …' if len(r.per_100ms) > 80 else ''}")
        info(f"  {pad('', 18)} 前 1 秒：{' '.join(str(c) for c in r.per_100ms[:10])}")
    info("")
    head = (f"{pad('策略', 20)}{pad('总请求', 8)}{pad('被 429', 8)}{pad('重试最密的 10ms', 17)}"
            f"{pad('全部成功', 10)}{pad('一半成功', 10)}网关利用率")
    info(head)
    for r in results:
        info(f"{pad(r.name, 20)}{pad(str(r.requests), 8)}{pad(str(r.rejected), 8)}{pad(str(r.retry_peak_10ms), 17)}"
             f"{pad(f'{r.all_done:.2f}s', 10)}{pad(f'{r.half_done:.2f}s', 10)}{r.utilization:.0%}"
             + (f"   （{r.gave_up} 个客户端放弃）" if r.gave_up else ""))

    fixed, nojit, jitter = results
    takeaway(f"固定间隔：重试整齐地成批到达 —— 最密的 10ms 里挤进 {fixed.retry_peak_10ms} 个重试，网关一次只接得住 "
             f"{HERD_CAPACITY} 个，其余全部 429；两批之间网关却闲着（利用率只有 {fixed.utilization:.0%}）。这就是惊群。")
    takeaway(f"无抖动的指数退避只是把批次之间的间隔拉长（全部成功用了 {nojit.all_done:.1f}s），每一批仍然是整齐的冲锋。")
    takeaway(f"全抖动：同样的客户端、同样的网关，429 从 {fixed.rejected} 个降到 {jitter.rejected} 个，"
             f"全部成功从 {fixed.all_done:.1f}s 缩短到 {jitter.all_done:.1f}s，网关利用率从 {fixed.utilization:.0%} "
             f"升到 {jitter.utilization:.0%} —— 重试被摊开在时间轴上（最密的 10ms 只有 {jitter.retry_peak_10ms} 个），"
             "网关空闲的处理能力才用得上。")
    takeaway("全抖动的第一个 100ms 请求反而最多：base=0.1s 时，第一次重试的等待在 [0, 0.1s] 里随机，大部分也落在这 100ms 内。"
             "但它们是均匀摊开的，不是同一瞬间的一堵墙。")


# =====================================================================
# 场景 2：熔断 + 降级
# =====================================================================

# A 部分：进程内熔断器打开后多久进入半开（真实时间）。真实模型每次回答要 2-3 秒，
# 如果 reset_timeout 比这还短，熔断器在备用模型回答的过程中就已经进入半开，"打开期间快速失败"就看不到了
RESET_TIMEOUT = {"offline": 1.5, "online": 8.0}
PRIMARY_MODEL = "primary-model"  # B 部分：services.py 里那个坏掉的"主模型"
SHARED_THRESHOLD = 3
SHARED_RESET = 2.0


async def scenario_fallback(offline: bool, svc: LocalServices, workdir: Path) -> None:
    banner("场景 2：熔断 + 降级 —— 主模型挂了，Agent 还能继续服务吗？")
    step("A. 进程内的熔断器（CircuitBreaker）：真实时钟 —— 真的等 reset_timeout 秒，而不是拨快一个假时钟")
    fallback_name = env("LLM_FALLBACK_MODEL") or MAIN_MODEL
    broken_name = f"{MAIN_MODEL}-does-not-exist"  # 故意写一个不存在的模型名，模拟"主模型不可用"
    if offline:
        primary = ScriptedLLM([model_not_found(broken_name), model_not_found(broken_name),
                               reply("幂等：同一个操作执行一次和执行多次，结果完全一样。")], model=broken_name)
        backup = ScriptedLLM([reply("熔断器：下游持续失败时直接快速失败，给它恢复的时间。"),
                              reply("降级：主方案不可用时，切换到能力稍弱但可用的备选方案。"),
                              reply("重试预算：限制重试占总请求的比例，防止重试风暴。")], model=fallback_name)
    else:
        primary = default_llm(broken_name)
        backup = default_llm(fallback_name)

    reset = RESET_TIMEOUT["offline" if offline else "online"]
    counted = FlakyLLM(primary, errors=[])  # 不注入故障，只数主模型被调用了几次
    llm = ResilientLLM(counted, [backup], max_attempts=2, base_delay=0.2, failure_threshold=2, reset_timeout=reset)
    breaker = llm.chain[0][1]
    agent = Agent(llm, [], system_prompt="你是分布式系统方向的讲师，从软件工程角度回答，不超过 30 个字。")
    info(f"主模型：{broken_name}（不存在）   备用模型：{fallback_name}")
    info(f"熔断器配置：连续失败 2 次打开，打开 {reset} 秒后进入半开（half_open）")

    questions = ["用一句话解释：什么是熔断器？", "用一句话解释：什么是降级？",
                 "用一句话解释：什么是重试预算？", "用一句话解释：什么是幂等？"]
    primary_calls = []
    for i, q in enumerate(questions, 1):
        if i == 4:
            step(f"⏳ 真的等熔断器进入半开（reset_timeout={reset}s，从它打开的那一刻算起）……")
            waited = time.monotonic()
            while breaker.state == "open":
                await asyncio.sleep(0.05)
            if not offline:
                primary.model = MAIN_MODEL  # 模拟"运维修好了主模型"
            info(f"又等了 {time.monotonic() - waited:.1f}s，主模型已经修好。熔断器现在是 {breaker.state}，放一个试探请求过去……")
        before, n, calls0 = breaker.state, len(llm.events), counted.calls
        t0 = time.time()
        res = await agent.run(q)
        primary_calls.append(counted.calls - calls0)
        step(f"请求 {i}：{q}")
        info(f"熔断器：{before} → {breaker.state}    主模型被调用 {primary_calls[-1]} 次    "
             f"实际回答的模型：{answered_by(res)}    耗时 {time.time() - t0:.1f}s")
        for e in llm.events[n:]:
            info(f"📝 {short(e)}")
        info(f"🤖 {short(res.output, 120)}")

    if primary_calls[2] == 0:
        takeaway("请求 3 里主模型一次都没有被调用（'快速失败'）：熔断器打开时，不再浪费时间和配额去撞一堵墙。")
    takeaway("请求 4 是半开状态下的试探：成功 → 熔断器关闭，流量回到主模型。用户全程没有看到一次报错。")

    await shared_breaker(svc, workdir)


async def shared_breaker(svc: LocalServices, workdir: Path) -> None:
    step("B. 跨进程共享的熔断器（SQLiteCircuitBreaker）：每个 worker 都是一个独立的操作系统进程")
    info(f"坏掉的主模型：services.py 里的 /v1/chat/completions（每次都返回 503，它自己给调用计数）")
    info(f"每个 worker 进程：ResilientLLM(OpenAICompatLLM → 主模型, [备用模型], max_attempts=2, "
         f"breaker_factory=lambda m: SQLiteCircuitBreaker(db, m, failure_threshold={SHARED_THRESHOLD}, "
         f"reset_timeout={SHARED_RESET}))")
    info("进程之间不共享任何内存，也不直接通信，只共用一个 SQLite 文件。这一部分两种模式都用本地的假模型服务。")
    db = workdir / "breakers.db"
    common = ("--db", str(db), "--url", svc.url, "--requests", "4")

    async def phase(title: str, *args: str) -> int:
        step(title)
        before = (await svc.get("/stats"))["model_calls"]
        await run_child(*args)
        return (await svc.get("/stats"))["model_calls"] - before

    a = await phase("进程 A：第一个发现故障的 worker", "--breaker-worker", "A", *common)
    b = await phase("进程 B：另一个 worker（A 已经退出，B 只看得到那个 SQLite 文件）", "--breaker-worker", "B", *common)
    b_local = await phase("对照：进程 B′ 用各自内存里的熔断器（默认的 CircuitBreaker），不共享",
                          "--breaker-worker", "B′", "--local-breaker", *common)

    await svc.post("/admin/heal")
    step(f"修好主模型，父进程读那个 SQLite 文件，等熔断器从 open 进入 half_open（reset_timeout={SHARED_RESET}s）")
    watcher = SQLiteCircuitBreaker(db, PRIMARY_MODEL, failure_threshold=SHARED_THRESHOLD, reset_timeout=SHARED_RESET)
    try:
        t0 = time.monotonic()
        while await watcher.current_state() == "open":
            await asyncio.sleep(0.05)
        info(f"熔断器 → {await watcher.current_state()}（又等了 {time.monotonic() - t0:.1f}s）")
        c = await phase("进程 C：恢复后的第一个请求就是试探请求", "--breaker-worker", "C", *common)
        final = await watcher.current_state()
    finally:
        await watcher.close()

    step("主模型服务自己统计的调用次数（每个请求最多尝试 2 次）")
    info(f"进程 A（共享熔断器）         打了主模型 {a} 次：连续 {SHARED_THRESHOLD} 个请求失败（每个 2 次尝试）后熔断器打开，"
         "之后的请求不再打它")
    info(f"进程 B（共享熔断器）         打了主模型 {b} 次 {'✅ 第一次调用就快速失败，直接走备用模型' if b == 0 else ''}")
    info(f"进程 B′（各自的内存熔断器）   打了主模型 {b_local} 次：它得自己再失败 {SHARED_THRESHOLD} 个请求才会熔断")
    info(f"进程 C（恢复后）             打了主模型 {c} 次：每个请求 1 次，第 1 个是试探，成功后熔断器关闭（最终状态：{final}）")
    takeaway(f"进程内的熔断器，每个进程都要自己撞墙：N 个 worker 就是 N × {SHARED_THRESHOLD} 个失败请求，"
             "新扩容出来的进程还要从头再撞一遍。共享之后，一个进程熔断，所有进程立刻都知道。")
    takeaway("半开时的试探也是跨进程唯一的：SQLiteCircuitBreaker 用一个带过期时间的'试探租约'（probe_until），"
             "拿到租约的进程去试探，其余进程继续快速失败；试探者崩溃了，租约过期后别人接着试。")


async def breaker_worker(args) -> None:
    """（子进程）一个 worker：连续发几个请求，打印每次的熔断器状态和实际回答的模型。"""
    label, pid = args.breaker_worker, os.getpid()
    primary = OpenAICompatLLM(model=PRIMARY_MODEL, base_url=f"{args.url}/v1", api_key="offline-demo-key", timeout=5)
    backup = ScriptedLLM(responder=lambda msgs: reply("（备用模型的回答）"), model="backup-model")

    def shared(model: str) -> SQLiteCircuitBreaker:
        return SQLiteCircuitBreaker(args.db, model, failure_threshold=SHARED_THRESHOLD, reset_timeout=SHARED_RESET)

    llm = ResilientLLM(primary, [backup], max_attempts=2, base_delay=0.05, failure_threshold=SHARED_THRESHOLD,
                       reset_timeout=SHARED_RESET, breaker_factory=None if args.local_breaker else shared)
    breaker = llm.chain[0][1]
    agent = Agent(llm, [])
    try:
        for i in range(1, args.requests + 1):
            before = await breaker.current_state()
            res = await agent.run(f"请求 {i}")
            after = await breaker.current_state()
            print(f"      [进程 {label} pid={pid}] 请求 {i}：熔断器 {before} → {after}    回答来自 {answered_by(res)}",
                  flush=True)
    finally:
        await llm.aclose()  # 关闭链上各模型的连接池和各熔断器的数据库连接


# =====================================================================
# 场景 3：崩溃恢复 + 幂等
# =====================================================================

RUN_ID = "ticket-demo"
TICKET_PROMPT = "请帮我提交一个 IT 工单：3 楼打印机卡纸了，优先级高。"
TICKET_SYSTEM = "你是公司 IT 服务台助手。用户报告故障时，直接调用 create_ticket 建工单（不要追问），然后把工单号告诉用户。"

# (标题, 用 SQLiteIdempotencyStore, 把幂等键传给下游, 崩溃点)
CRASH_CASES = [
    ("没有幂等保护", False, False, "after_tool"),
    ("SQLiteIdempotencyStore（所有进程共享的幂等记录）", True, False, "after_tool"),
    ("同上，但崩在更窄的窗口：下游已经建单、幂等记录还没写", True, False, "in_tool"),
    ("再把幂等键传给下游（Idempotency-Key 请求头）", True, True, "in_tool"),
]


class CrashAfterTool(Hook):
    """故障注入：工具执行完毕（幂等记录已写）、结果还没写进检查点的那一刻，进程被杀死。"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def after_tool(self, state, call, result):
        if call.name == self.tool_name:
            print(f"      [进程 1 pid={os.getpid()}] 工具已执行：{result.content}", flush=True)
            print("      [进程 1] 💥 就在此刻进程死亡（os._exit(137)）—— 工具结果还没来得及写进检查点", flush=True)
            os._exit(137)  # 真正的进程死亡：不执行 finally、不保存状态、不做任何清理


class VisibleIdempotencyStore(SQLiteIdempotencyStore):
    """SQLiteIdempotencyStore，命中时多打印一行 —— 只是为了让你在输出里看见它起了作用。"""

    def __init__(self, path: Path, label: str):
        super().__init__(path)
        self.label = label

    async def get(self, key: str):
        hit = await super().get(key)
        if hit is not None:
            print(f"      [{self.label}] ♻️  幂等存储命中 {key} → 直接返回上次的结果，工具没有再执行", flush=True)
        return hit


def build_ticket_agent(args, script: list, label: str, store, crash_at: str | None = None) -> Agent:
    url, downstream_key = args.url, args.downstream_key

    @tool(risk="write")  # 写操作：幂等存储只对 write / dangerous 工具生效
    async def create_ticket(
        title: Annotated[str, Field(description="工单标题，简要描述故障")],
        ctx: ToolContext,
        priority: Annotated[Literal["low", "medium", "high"], Field(description="优先级")] = "medium",
    ) -> str:
        """在 IT 工单系统中创建一张工单，返回工单号。"""
        headers = {"Idempotency-Key": ctx.idempotency_key} if downstream_key else {}
        async with httpx.AsyncClient(timeout=10) as http:  # 真的调用另一个进程里的工单系统
            r = await http.post(f"{url}/tickets", json={"title": title, "priority": priority}, headers=headers)
        t = r.json()
        if t["replayed"]:
            print(f"      [{label}] 工单系统认出了同一个 Idempotency-Key（{ctx.idempotency_key}）→ 返回原来的 {t['id']}，"
                  "没有新建", flush=True)
        if crash_at == "in_tool":
            print(f"      [{label} pid={os.getpid()}] 工单系统已经建好 {t['id']}", flush=True)
            print(f"      [{label}] 💥 进程在工具返回之前死亡 —— 幂等记录和检查点都还没来得及写", flush=True)
            os._exit(137)
        return f"工单已创建：{t['id']}（{title}，优先级 {priority}）"

    return Agent(
        ScriptedLLM(script) if args.offline else default_llm(),
        [create_ticket],
        system_prompt=TICKET_SYSTEM,
        checkpointer=FileCheckpointer(Path(args.workdir) / "checkpoints"),
        idempotency_store=store,
        hooks=[CrashAfterTool("create_ticket")] if crash_at == "after_tool" else [],
    )


async def open_store(args, label: str):
    if not args.idempotent:
        return None
    store = VisibleIdempotencyStore(Path(args.workdir) / "idempotency.db", label)
    await store.setup()
    return store


async def crash_child(args) -> None:
    """（子进程 1）Agent 调用 create_ticket 之后立刻"崩溃"。"""
    store = await open_store(args, "进程 1")
    script = [call_tool("create_ticket", title="3 楼打印机卡纸", priority="high")]
    agent = build_ticket_agent(args, script, "进程 1", store, crash_at=args.crash_at)
    res = await agent.run(TICKET_PROMPT, run_id=RUN_ID)
    print(f"      [进程 1] 模型这次没有调用 create_ticket，而是直接回答：{short(res.output)}", flush=True)
    if store is not None:
        await store.close()


async def resume_child(args) -> None:
    """（子进程 2）一个全新的进程：用同一个 run_id 从检查点恢复。"""
    store = await open_store(args, "进程 2")
    script = [lambda msgs: reply(f"已为您提交工单。{msgs[-1]['content']}")]
    agent = build_ticket_agent(args, script, "进程 2", store)
    res = await agent.resume(RUN_ID)
    print(f"      [进程 2 pid={os.getpid()}] 恢复后状态：{res.status}    🤖 {short(res.output, 100)}", flush=True)
    if store is not None:
        await store.close()


async def run_crash_case(offline: bool, svc: LocalServices, workdir: Path, idx: int, case: tuple) -> list[dict]:
    title, idempotent, downstream_key, crash_at = case
    case_dir = workdir / f"crash_{idx}"
    shutil.rmtree(case_dir, ignore_errors=True)
    case_dir.mkdir(parents=True)
    await svc.post("/admin/reset")  # 每个用例从一个空的工单系统开始
    flags = ["--workdir", str(case_dir), "--url", svc.url]
    flags += ["--idempotent"] if idempotent else []
    flags += ["--downstream-key"] if downstream_key else []
    flags += ["--offline"] if offline else []

    step(f"用例 {idx}：{title}")
    code = await run_child("--crash-child", "--crash-at", crash_at, *flags)
    info(f"进程 1 退出码：{code}" + ("（被杀死）" if code == 137 else ""))
    saved = FileCheckpointer(case_dir / "checkpoints").load(RUN_ID)
    tickets = (await svc.get("/stats"))["tickets"]
    if code != 137 or saved is None:
        info("（这次没有触发崩溃，跳过恢复步骤）")
        return tickets
    # 留一份"死亡时刻"的检查点副本（--keep 时可以打开看）：进程 2 恢复完成后会覆盖 checkpoints/ 里的那份
    shutil.copy(case_dir / "checkpoints" / f"{RUN_ID}.json", case_dir / "checkpoint_at_crash.json")
    last = saved.messages[-1]
    info(f"工单系统（另一个进程）里已有 {len(tickets)} 张工单；检查点里最后一条消息是 role={last['role']}，"
         f"带 {len(last.get('tool_calls') or [])} 个工具调用、没有工具结果 → 看起来'还没执行'")
    code2 = await run_child("--resume-child", *flags)
    tickets = (await svc.get("/stats"))["tickets"]
    mark = "✅" if len(tickets) == 1 else "❌"
    info(f"进程 2 退出码：{code2}    {mark} 工单系统里现在一共有 {len(tickets)} 张工单：{', '.join(t['id'] for t in tickets)}")
    return tickets


async def scenario_crash(offline: bool, svc: LocalServices, workdir: Path) -> None:
    banner("场景 3：崩溃恢复 —— '工具执行了，但没来得及记下来'")
    info("流程：进程 1 里的 Agent 调用 create_ticket（真的 POST 到工单系统进程）→ 在写检查点之前进程死亡")
    info("      → 进程 2（一个全新的进程）从检查点恢复。框架会补执行'没有结果的工具调用'。问题是：它其实已经执行过了。")
    results = [await run_crash_case(offline, svc, workdir, i, case) for i, case in enumerate(CRASH_CASES, 1)]
    step("结果对比（工单数来自工单系统自己的记录）")
    for (title, *_), tickets in zip(CRASH_CASES, results):
        info(f"{pad(title, 56)} {len(tickets)} 张  {'✅' if len(tickets) == 1 else '❌ 同一个故障被报了两次'}")
    takeaway("检查点保证'不丢进度'，幂等键保证'不重复执行'。两者合起来才是'恰好一次'的效果。")
    takeaway("幂等记录必须在所有进程都看得到的地方（这里是 SQLite 文件）：内存里的 IdempotencyStore 随进程 1 一起死了。")
    takeaway("用例 3 是更窄的窗口：副作用完成、幂等记录还没写就崩溃，调用方的幂等存储无能为力。"
             "只有把幂等键传给下游、让下游自己去重（用例 4），才真正堵上。")


# =====================================================================
# 场景 4：预算
# =====================================================================


async def scenario_budget(offline: bool) -> None:
    banner("场景 4：预算 —— 截停一个停不下来的 Agent")
    polled: list[str] = []

    @tool
    def check_report_status(report_id: Annotated[str, Field(description="报表编号，如 R-42")]) -> str:
        """查询报表的生成状态。"""
        polled.append(report_id)
        return f"报表 {report_id} 状态：生成中（进度 99%），请稍后再次查询。"

    system = ("你是报表助手。只有当报表状态变为'已完成'时才能回答用户；"
              "在此之前必须持续调用 check_report_status 查询，不要放弃，也不要询问用户。")
    # 离线剧本：输入 token 随对话历史变长而增长（和真实模型的计费方式一致）
    script = [lambda msgs: call_tool("check_report_status", report_id="R-42", input_tokens=60 * len(msgs) + 300,
                                     output_tokens=22)] * 30
    llm = ScriptedLLM(script) if offline else default_llm()
    budget = BudgetHook(max_tool_calls=3, max_cost_usd=0.05, max_seconds=180)
    agent = Agent(llm, [check_report_status], system_prompt=system, hooks=[budget], max_steps=30)

    step("工具永远返回'生成中 99%'，系统提示又要求'不许放弃'—— 一个典型的失控场景")
    info("预算：最多 3 次工具调用 / $0.05 / 180 秒；max_steps=30 作为最后一道保险")
    res = await agent.run("帮我拿一下报表 R-42 的结果。")
    info(f"状态={res.status}  stop_reason={res.stop_reason}  模型调用 {res.steps} 次  工具执行 {len(polled)} 次")
    info(f"tokens={res.usage.total}  估算成本=${res.cost_usd:.5f}")
    info(f"给用户的回复：{short(res.output, 120)}")
    step("链路追踪（第 10 课详讲）—— 一眼看出它在原地打转：")
    for line in render_tree(res.trace).splitlines():
        info(line)
    per_step = [s.attrs.get("gen_ai.usage.input_tokens", 0) for s in res.trace.walk() if s.name == "llm.chat"]
    info(f"每一步的输入 token：{' → '.join(map(str, per_step))}")
    takeaway("每一轮都要把完整历史重新发给模型：步数越多，每一步越贵。循环 N 步的总成本近似按 N² 增长。")
    if res.stop_reason != "budget_exceeded":
        takeaway("模型这次自己停了下来。但你不能指望每次都这样 —— 预算是给'最坏情况'准备的。")
    takeaway("没有预算，它会一直跑到 max_steps=30。线上一个用户触发 30 次模型调用还算便宜，"
             "如果是 30 万个并发会话呢？预算要按步数、token、金额、时长、工具次数多维度设置。")
    takeaway("它每次都用完全相同的参数调用同一个工具 —— 练习 (a) 的 LoopGuard 能比预算更早、更准地识别这种循环。")


# =====================================================================


async def main() -> None:
    parser = argparse.ArgumentParser(description="第 08 课 Demo：可靠性工程")
    parser.add_argument("--offline", action="store_true", help="使用离线剧本（ScriptedLLM），不调用真实模型")
    parser.add_argument("--only", type=int, choices=[1, 2, 3, 4], help="只跑某一个场景")
    parser.add_argument("--clients", type=int, default=500, help="场景 1 惊群实验的客户端数（默认 500）")
    parser.add_argument("--keep", action="store_true", help="运行产物留在 runs/08_reliability/（默认用临时目录，结束时删除）")
    # 以下是子进程用的隐藏参数
    parser.add_argument("--breaker-worker", help=argparse.SUPPRESS)
    parser.add_argument("--local-breaker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--requests", type=int, default=4, help=argparse.SUPPRESS)
    parser.add_argument("--db", help=argparse.SUPPRESS)
    parser.add_argument("--url", help=argparse.SUPPRESS)
    parser.add_argument("--crash-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resume-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--crash-at", choices=["after_tool", "in_tool"], help=argparse.SUPPRESS)
    parser.add_argument("--workdir", help=argparse.SUPPRESS)
    parser.add_argument("--idempotent", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--downstream-key", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.breaker_worker:
        return await breaker_worker(args)
    if args.crash_child:
        return await crash_child(args)
    if args.resume_child:
        return await resume_child(args)

    if args.offline:
        print("模式：离线剧本（ScriptedLLM）—— 结果确定、零成本")
    else:
        try:
            default_llm()
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"模式：真实模型（{MAIN_MODEL}）—— 每次运行结果可能略有不同")

    if args.keep:
        shutil.rmtree(RUNS, ignore_errors=True)
        RUNS.mkdir(parents=True)
        workdir = RUNS
    else:
        workdir = Path(tempfile.mkdtemp(prefix="lesson08_"))
    wanted = [args.only] if args.only else [1, 2, 3, 4]
    try:
        async with LocalServices(HERD_CAPACITY, HERD_SERVICE_TIME) as svc:
            print(f"外部服务进程已启动：{svc.url}（pid {svc.pid}）")
            if 1 in wanted:
                await scenario_retry(args.offline, svc, args.clients)
            if 2 in wanted:
                await scenario_fallback(args.offline, svc, workdir)
            if 3 in wanted:
                await scenario_crash(args.offline, svc, workdir)
            if 4 in wanted:
                await scenario_budget(args.offline)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    where = f"运行产物在 {RUNS.relative_to(ROOT)}/ 下" if args.keep else "运行产物在临时目录里，已删除（加 --keep 可以保留下来查看）"
    banner(f"完成 🎉  外部服务进程已关闭；{where}")


if __name__ == "__main__":
    asyncio.run(main())
