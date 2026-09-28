"""第 30 课场景 1c 的 worker 应用：demo.py 用 WorkerPool 拉起 K 个真实的 worker 进程，每个进程这样加载它：

    python -m agentkit.distributed.worker --queue sqlite:///runs/scale/jobs.db \\
        --app lessons/30_async_runtime/worker_app.py:make_handler --concurrency 256 --opt checkpoint=memory ...

每个进程：一个事件循环、一个共享的 Agent，run_worker 同时推进最多 --concurrency 个任务。
一个任务 = 一次 Agent 运行：模型调用 2 次（每次 asyncio.sleep latency 秒，扮演"等模型"）+ 1 次 async 工具。
每次调用模型前，CpuHook 做 cpu_ms 毫秒的纯 Python 计算 —— 它代表"你自己的 Hook、上下文策略、JSON 处理"加进来的 CPU，
按本进程实测校准成固定的计算量（不是按墙钟空转：进程被抢占时，墙钟在走，CPU 活并没有干）。

选项（--opt key=value）：
    latency=0.1          每次模型调用等多久
    cpu_ms=1.0           每次模型调用前的 CPU 开销
    checkpoint=memory    memory：检查点在进程内存里（只剩队列的"领取 + 提交"两次写事务）；
                         sqlite：AgentJobHandler + SQLiteCheckpointer（每一步都写共享的 SQLite 文件，带 fence）
    model_slots=0        >0 时所有 worker 进程共用这么多个模型并发名额（SQLiteSemaphore，模拟网关给的配额）

进程退出前打一条 report 事件：从第一个任务开始到现在用了多少 CPU 秒、忙碌期间事件循环延迟的 p99 / 最大值。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from agentkit import Agent, Hook, InMemoryCheckpointer, call_tool, reply, tool
from agentkit.distributed import AgentJobHandler, SQLiteCheckpointer, SQLiteSemaphore, WorkerContext


def emit(event: str, **info) -> None:
    """和 run_worker 的事件同样的格式：一行 JSON，带 event / pid / t。"""
    print(json.dumps({"event": event, "pid": os.getpid(), "t": round(time.time(), 4), **info}, ensure_ascii=False), flush=True)


def _work(n: int) -> int:
    x = 0
    for i in range(n):
        x += i * i
    return x


def calibrate(target_ms: float) -> int:
    """算出"多少次循环 ≈ target_ms 毫秒的 CPU"（用 process_time 量，取 3 次里最快的，排除被抢占的那次）。"""
    n = 20000
    best = min(_timed(n) for _ in range(3))
    return max(1, int(n * target_ms / 1000 / best))


def _timed(n: int) -> float:
    t0 = time.process_time()
    _work(n)
    return max(time.process_time() - t0, 1e-6)


class CpuHook(Hook):
    def __init__(self, loops: int):
        self.loops = loops

    def before_llm(self, state, messages) -> None:
        _work(self.loops)  # 故意是同步的纯计算：它就是事件循环要付的 CPU


class BenchLLM:
    model = "scripted"

    def __init__(self, latency: float):
        self.latency = latency

    async def chat(self, messages, tools=None, **kwargs):
        await asyncio.sleep(self.latency)
        if messages[-1]["role"] == "tool":
            return reply(f"查到了：{messages[-1]['content']}")
        return call_tool("lookup_ticket", ticket_id=messages[-1]["content"].split()[-1])


class SlotLLM:
    """所有 worker 进程共用 limit 个模型并发名额（SQLiteSemaphore：带租约，持有者被 kill -9 也不会永久占着名额）。"""

    def __init__(self, inner, sem: SQLiteSemaphore):
        self.inner, self.sem, self.model = inner, sem, inner.model

    async def chat(self, messages, tools=None, **kwargs):
        async with self.sem.slot():
            return await self.inner.chat(messages, tools, **kwargs)


@tool
async def lookup_ticket(ticket_id: str) -> str:
    """查询工单状态"""
    return f"{ticket_id}：处理中，已分配给二线支持"


class MemoryJobHandler:
    """检查点只在本进程内存里：一个任务就是一次 agent.run，除了队列本身不写共享数据库。"""

    def __init__(self, agent: Agent):
        self.agent = agent

    async def __call__(self, job) -> dict:
        result = await self.agent.run(job.payload["input"], run_id=f"job-{job.id}", metadata={"tenant_id": job.tenant_id})
        return {"status": result.status}

    async def aclose(self) -> None:
        await self.agent.aclose()


class Report:
    """包一层 handler：统计 CPU 秒、在途任务数，以及"有任务在跑时"事件循环的延迟（每 10ms 醒一次的心跳迟到了多久）。"""

    def __init__(self, inner):
        self.inner = inner
        self.first_cpu = self.first_wall = self.last_wall = None
        self.in_flight = self.jobs = 0
        self.lags: list[float] = []
        self._monitor: asyncio.Task | None = None

    async def _watch(self) -> None:
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            if self.in_flight:
                self.lags.append(time.perf_counter() - t0 - 0.01)

    async def __call__(self, job):
        if self.first_cpu is None:
            self.first_cpu, self.first_wall = time.process_time(), time.time()
            self._monitor = asyncio.create_task(self._watch())
        self.in_flight += 1
        try:
            return await self.inner(job)
        finally:
            self.in_flight -= 1
            self.jobs += 1
            self.last_wall = time.time()

    async def aclose(self) -> None:  # worker 进程退出前会调用 handler.aclose()
        if self._monitor is not None:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
        if self.first_cpu is not None:
            lags = sorted(self.lags) or [0.0]
            emit("report", jobs=self.jobs, cpu_s=round(time.process_time() - self.first_cpu, 4),
                 busy_s=round(self.last_wall - self.first_wall, 4),
                 lag_p99_ms=round(lags[int(0.99 * (len(lags) - 1))] * 1000, 1), lag_max_ms=round(lags[-1] * 1000, 1))
        await self.inner.aclose()


async def make_handler(wctx: WorkerContext):
    """worker 进程启动时调用一次：组装一个共享的 Agent，返回交给 run_worker 的 handler。"""
    opts = wctx.options
    loops = calibrate(float(opts.get("cpu_ms", 1.0)))
    llm = BenchLLM(float(opts.get("latency", 0.1)))
    slots = int(opts.get("model_slots", 0))
    if slots:
        sem = SQLiteSemaphore(wctx.db, "model", limit=slots, lease_seconds=10, poll_interval=0.02)
        await sem.setup()
        llm = SlotLLM(llm, sem)
    hooks = [CpuHook(loops)]
    if opts.get("checkpoint", "memory") == "sqlite":
        ckpt = SQLiteCheckpointer(wctx.db)  # 和队列共用一个连接：所有进程的写入排队经过同一把写锁
        await ckpt.setup()
        handler = AgentJobHandler(Agent(llm, [lookup_ticket], hooks=hooks, checkpointer=ckpt), ckpt)
    else:
        handler = MemoryJobHandler(Agent(llm, [lookup_ticket], hooks=hooks, checkpointer=InMemoryCheckpointer()))
    return Report(handler)
