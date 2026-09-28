"""tests/contrib/test_postgres.py 用的 worker 应用：每个 worker 进程通过 --app tests/contrib/pg_worker_apps.py:xxx 加载。

它们运行在**独立的子进程**里（python -m agentkit.distributed.worker --queue postgresql://...），
业务上和测试进程之间只通过同一个 Postgres 数据库交流（队列、检查点、记录表）—— 多机部署时就是这样：
每台机器上的 worker 只知道数据库的连接串。另外两条通道只用于测试观测和控制：
- 标准输出：模型调用以一行 JSON 事件打出来（{"event": "llm_call", ...}），和 worker 自己的事件一起由 WorkerPool.events() 收集；
- gate 文件：测试创建它的那一刻，停在 gate 上的调用才返回（决定故障注入发生在哪一步，不靠 sleep 赌时间）。

记录表由测试在拉起进程之前建好（见 test_postgres.py 的 create_record_tables）。

直接运行这个文件（python pg_worker_apps.py setup-race URI KEY）是"多个进程同时建表"测试的子进程，见 setup_race。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import psycopg
from psycopg_pool import AsyncConnectionPool

from agentkit import Agent, ToolContext, call_tool, reply, tool
from agentkit.contrib.postgres import AgentJobHandler, PostgresCheckpointer, PostgresJobQueue
from agentkit.distributed import WorkerContext, current_job
from agentkit.types import LLMResponse


def _event(name: str, worker_id: str, **info) -> None:
    print(json.dumps({"event": name, "worker_id": worker_id, "pid": os.getpid(), "t": round(time.time(), 4), **info},
                     ensure_ascii=False), flush=True)


async def _wait_for_gate(gate: str | None, limit: float = 60.0) -> None:
    """等测试进程创建 gate 文件（最多 limit 秒）。用来把"这次调用什么时候返回"交给测试控制，而不是赌 sleep 的时长。"""
    deadline = time.monotonic() + limit
    while gate and not os.path.exists(gate) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def _attempt() -> int:
    job = current_job()
    return job.attempts if job is not None else 0


class TwoStepLLM:
    """第一轮让模型调用 create_ticket，看到工具结果后给出答案。

    hang="llm" 且设置了 gate 时：**第一次领取**（attempts == 1）的第二次模型调用停在 gate 上，
    直到测试创建 gate 文件 —— 测试就能在"工具已执行、检查点已写入、模型调用进行中"这个确定的时刻
    kill -9 / SIGSTOP / 断网。接手者（attempts ≥ 2）不等。"""

    model = "scripted"

    def __init__(self, worker_id: str, gate: str | None, hang: str):
        self.worker_id, self.gate, self.hang = worker_id, gate, hang

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        job = current_job()
        second = messages[-1]["role"] == "tool"
        _event("llm_call", self.worker_id, job=job.id if job else None, attempt=_attempt(),
               phase="second" if second else "first")
        if not second:
            return call_tool("create_ticket", title=messages[-1]["content"])
        if self.hang == "llm" and _attempt() == 1:
            await _wait_for_gate(self.gate)
        return reply(f"已建单：{messages[-1]['content']}")


class PgAgentHandler(AgentJobHandler):
    """AgentJobHandler + aclose()：worker 进程退出前会调用它（关闭 Agent 和检查点自己的连接池）。"""

    async def aclose(self) -> None:
        await self._shared.aclose()
        await self.checkpointer.close()


async def make_agent_app(ctx: WorkerContext) -> PgAgentHandler:
    """Postgres 检查点 + 带副作用的写工具。选项：gate=文件路径，hang=llm（默认）/ tool。

    hang=tool：第一次领取时，create_ticket 在**下游已经建好工单之后**停在 gate 上（"响应还在路上"），
    用来测试优雅停机取消了一个写操作之后，接手者用同一个幂等键重放、下游去重。"""
    opts = ctx.options
    gate, hang = opts.get("gate"), opts.get("hang", "llm")
    url, worker_id = ctx.queue_url, ctx.worker_id
    # check_connection：借出连接前先检查。网络分区 / 数据库重启之后，池里空闲的连接其实已经断了，
    # 不检查的话，恢复后的第一次写入会撞上一个死连接 —— 失败的原因就变成了"网络错误"而不是"你已经不是持有者"
    ckpt = PostgresCheckpointer(url, pool_kwargs={"min_size": 1, "max_size": 4,
                                                  "check": AsyncConnectionPool.check_connection})
    await ckpt.setup()

    @tool(risk="write")
    async def create_ticket(title: str, ctx: ToolContext) -> str:
        """创建工单（有副作用）。tickets 表按幂等键去重（下游的唯一约束）；ticket_calls 记录工具体每一次真正执行。"""
        key = ctx.idempotency_key
        job = current_job()
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO ticket_calls (key, title, worker, pid, attempt, fence) VALUES (%s, %s, %s, %s, %s, %s)",
                (key, title, worker_id, os.getpid(), _attempt(), job.fence if job else None),
            )
            cur = await conn.execute(
                "INSERT INTO tickets (idempotency_key, title) VALUES (%s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id", (key, title))
            row = await cur.fetchone()
            if row is None:  # 重放：下游已经建过了，返回同一张工单
                row = await (await conn.execute("SELECT id FROM tickets WHERE idempotency_key = %s", (key,))).fetchone()
        if hang == "tool" and _attempt() == 1:
            await _wait_for_gate(gate)
        return f"T-{1000 + row[0]}"

    agent = Agent(TwoStepLLM(worker_id, gate, hang), [create_ticket], checkpointer=ckpt)
    return PgAgentHandler(agent, ckpt)


class Recorder:
    """不跑 Agent，只记录"哪个进程、用哪个 fence 处理了哪个任务"。payload 里的 hold 覆盖默认的处理时长。"""

    def __init__(self, ctx: WorkerContext, pool: AsyncConnectionPool):
        self.ctx, self.pool = ctx, pool
        self.hold = float(ctx.options.get("hold", 0.05))

    async def __call__(self, job) -> dict:
        start = time.time()
        await asyncio.sleep(float(job.payload.get("hold", self.hold)))
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO job_log (job_id, fence, worker, pid, start_t, end_t) VALUES (%s, %s, %s, %s, %s, %s)",
                (job.id, job.fence, self.ctx.worker_id, os.getpid(), start, time.time()),
            )
        return {"worker": self.ctx.worker_id}

    async def aclose(self) -> None:
        await self.pool.close()


async def make_recording_app(ctx: WorkerContext) -> Recorder:
    pool = AsyncConnectionPool(ctx.queue_url, min_size=1, max_size=4, kwargs={"autocommit": True}, open=False)
    await pool.open(wait=True)
    return Recorder(ctx, pool)


# --------------------------------------------------------------------------- 多个进程同时建表


async def setup_race(uri: str, barrier: int) -> None:
    """一个子进程：先把连接建好，然后在 advisory lock（键 barrier）上等发令枪；枪响后立刻建表。

    测试进程先持有这个键的排他锁，子进程申请共享锁 —— 全部排队等着；测试释放排他锁的那一刻，
    所有子进程被同一次解锁唤醒，几乎同时执行 CREATE TABLE IF NOT EXISTS：一批 Pod 同时启动时的真实情况。"""
    async with AsyncConnectionPool(uri, min_size=1, max_size=1, kwargs={"autocommit": True}) as pool, \
            await psycopg.AsyncConnection.connect(uri, autocommit=True) as starter:
        await pool.wait()  # 连接提前建好：枪响之后没有任何握手延迟
        queue, ckpt = PostgresJobQueue(pool), PostgresCheckpointer(pool)
        await starter.execute("SELECT pg_advisory_lock_shared(%s)", (barrier,))
        await queue.setup()
        await ckpt.setup()
    print("setup ok", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "setup-race":
        sys.exit("用法：python pg_worker_apps.py setup-race POSTGRES_URI BARRIER_KEY")
    asyncio.run(setup_race(sys.argv[2], int(sys.argv[3])))
