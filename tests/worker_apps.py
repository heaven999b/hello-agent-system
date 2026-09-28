"""tests/test_distributed.py 用的 worker 应用：每个 worker 进程通过 --app tests/worker_apps.py:xxx 加载。

它们运行在**独立的子进程**里，和测试进程之间只通过同一个 SQLite 文件交流（队列、检查点、记录表）。
"""

from __future__ import annotations

import asyncio
import os
import time

from agentkit import Agent, ToolContext, call_tool, reply, tool
from agentkit.distributed import (
    AgentJobHandler,
    SQLiteCheckpointer,
    SQLiteIdempotencyStore,
    SQLiteSemaphore,
    SQLiteTokenBucket,
    WorkerContext,
)
from agentkit.types import LLMResponse


class TwoStepLLM:
    """第一轮让模型调用 create_ticket，看到工具结果后给出答案。两轮的耗时分别可配，
    用来把"进程被杀 / 被冻结"的时刻卡在想要的位置（例如：工具已执行、第二次模型调用进行中）。"""

    model = "scripted"

    def __init__(self, first_latency: float, second_latency: float):
        self.first_latency, self.second_latency = first_latency, second_latency

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        if messages[-1]["role"] == "tool":
            await asyncio.sleep(self.second_latency)
            return reply(f"已建单：{messages[-1]['content']}")
        await asyncio.sleep(self.first_latency)
        return call_tool("create_ticket", title=messages[-1]["content"])


async def make_agent_app(wctx: WorkerContext):
    opts = wctx.options
    db = wctx.db
    worker_id = wctx.worker_id
    ckpt = SQLiteCheckpointer(db)
    idem = SQLiteIdempotencyStore(db)
    await ckpt.setup()
    await idem.setup()
    await db.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS ticket_calls (key TEXT, title TEXT, worker TEXT, pid INTEGER, t REAL)"
    ))

    @tool(risk="write")
    async def create_ticket(title: str, ctx: ToolContext) -> str:
        """创建工单（有副作用：每次真正执行都会在 ticket_calls 里留下一行）"""
        key = ctx.idempotency_key
        await db.write(lambda c: c.execute(
            "INSERT INTO ticket_calls VALUES (?, ?, ?, ?, ?)", (key, title, worker_id, os.getpid(), time.time())
        ))
        return f"T-{key}"

    llm = TwoStepLLM(float(opts.get("first_latency", 0.02)), float(opts.get("second_latency", 0.02)))
    agent = Agent(llm, [create_ticket], checkpointer=ckpt, idempotency_store=idem)
    return AgentJobHandler(agent, ckpt)


async def make_recording_app(ctx: WorkerContext):
    """不跑 Agent，只记录"哪个进程、什么时候处理了哪个任务"。mode=semaphore / bucket 时经过跨进程限流。"""
    opts = ctx.options
    db = ctx.db
    hold = float(opts.get("hold", 0.05))
    mode = opts.get("mode", "plain")
    await db.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS job_log (job_id INTEGER, fence INTEGER, worker TEXT, pid INTEGER, start REAL, end REAL)"
    ))
    sem = bucket = None
    if mode == "semaphore":
        sem = SQLiteSemaphore(db, "gateway", limit=int(opts["limit"]), lease_seconds=float(opts.get("slot_lease", 5)))
        await sem.setup()
    elif mode == "bucket":
        bucket = SQLiteTokenBucket(db, rate=float(opts["rate"]), capacity=float(opts["capacity"]))
        await bucket.setup()

    async def work(job):
        start = time.time()
        await asyncio.sleep(hold)
        end = time.time()
        await db.write(lambda c: c.execute(
            "INSERT INTO job_log VALUES (?, ?, ?, ?, ?, ?)", (job.id, job.fence, ctx.worker_id, os.getpid(), start, end)
        ))
        return {"worker": ctx.worker_id}

    async def handler(job):
        if sem is not None:
            async with sem.slot():
                return await work(job)
        if bucket is not None:
            await bucket.acquire("gateway")
        return await work(job)

    return handler
