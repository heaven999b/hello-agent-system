"""demo_agents.py / demo_scale.py 的 worker 应用。每个 worker 进程这样加载它（WorkerPool 替你拼好这条命令）：

    python -m agentkit.distributed.worker --queue sqlite:///runs/agents/jobs.db \\
        --app lessons/13_distributed_concurrency/worker_app.py:make_handler --opt offline=1 ...

它运行在**独立的子进程**里，和 demo 之间只通过同一个 SQLite 文件交流：队列、检查点、幂等记录、工单表。
另外它把几个自定义事件以一行 JSON 打到标准输出（llm_call / tool_executed / idempotency_hit / chaos_window），
和 run_worker 自己的事件（claimed / completed / ownership_lost ……）一起出现在 pool.events() 的时间线里。

和本课 demo.py 里手写的 worker 对照着看：领取、心跳、fence、提交、优雅停机都由 agentkit.distributed.run_worker 负责，
这里只剩业务：一个 Agent、一个有副作用的写工具，以及它们要用的共享存储。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from agentkit import Agent, Hook, ToolContext, call_tool, default_llm, reply, tool
from agentkit.distributed import (
    AgentJobHandler,
    SQLiteCheckpointer,
    SQLiteIdempotencyStore,
    SQLiteSemaphore,
    WorkerContext,
    current_job,
)
from agentkit.types import LLMResponse

SYSTEM_PROMPT = (
    "你是公司的 IT 服务台助手。用户每发来一条报修：先调用一次 create_ticket 建工单（title 用一句话概括问题），"
    "然后用一句简短的中文告诉用户工单号。每条报修只建一张工单，不要追问。"
)


def make_emit(worker_id: str, enabled: bool = True):
    """和 run_worker 的事件同样的格式：一行 JSON，带 event / worker_id / pid / t。"""

    def emit(event: str, **info) -> None:
        if enabled:
            line = {"event": event, "worker_id": worker_id, "pid": os.getpid(), "t": round(time.time(), 4), **info}
            print(json.dumps(line, ensure_ascii=False, default=str), flush=True)

    return emit


def _job_id() -> int | None:
    job = current_job()
    return job.id if job else None


class HelpdeskLLM:
    """离线剧本：第一轮让模型调用 create_ticket，看到工具结果后回复工单号。两轮的耗时分别可配（asyncio.sleep），
    等"模型"时事件循环照常运转：心跳续约、别的任务都不受影响。"""

    model = "scripted"

    def __init__(self, worker_id: str, first_latency: float, second_latency: float):
        self.worker_id, self.first_latency, self.second_latency = worker_id, first_latency, second_latency

    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        if messages[-1]["role"] == "tool":
            await asyncio.sleep(self.second_latency)
            return reply(f"已为你创建工单 {messages[-1]['content']}（{self.worker_id} 回复）")
        await asyncio.sleep(self.first_latency)
        return call_tool("create_ticket", title=messages[-1]["content"][:20])


class SlotLLM:
    """所有 worker 进程共用 limit 个模型并发名额（SQLiteSemaphore，带租约：持有者被 kill -9 也不会永久占着名额）。"""

    def __init__(self, inner, sem: SQLiteSemaphore):
        self.inner, self.sem, self.model = inner, sem, getattr(inner, "model", "llm")

    async def chat(self, messages, tools=None, **kwargs):
        async with self.sem.slot():
            return await self.inner.chat(messages, tools, **kwargs)


class LoggingIdempotencyStore(SQLiteIdempotencyStore):
    """跨进程的幂等存储；命中时打一条事件，时间线里能看到"这次重放没有再执行工具"。"""

    def __init__(self, db, emit):
        super().__init__(db)
        self._emit = emit

    async def get(self, key: str):
        result = await super().get(key)
        if result is not None:
            self._emit("idempotency_hit", job=_job_id(), key=key)
        return result


class Timeline(Hook):
    """两件事：① 每次调用模型前打一条事件（demo 据此判断"任务正在第几轮"，决定什么时候发信号）；
    ② 故障窗口：payload 里 chaos=kill_in_window 的任务，**第一次**执行时在"工具已执行、幂等记录已写入、
    检查点还没落盘"这个本来就存在的窗口里多停 window 秒，好让 demo 的 kill -9 恰好落在里面。"""

    def __init__(self, emit, window: float):
        self.emit, self.window = emit, window

    def before_llm(self, state, messages) -> None:
        self.emit("llm_call", job=_job_id(), phase="answer" if messages[-1]["role"] == "tool" else "plan")

    async def after_tool(self, state, call, result):
        job = current_job()
        if job is not None and job.payload.get("chaos") == "kill_in_window" and job.attempts == 1:
            self.emit("chaos_window", job=job.id, seconds=self.window)
            await asyncio.sleep(self.window)
        return None


class CpuReport:
    """demo_scale.py 用：包一层 handler，进程退出前报告"从第一个任务开始到现在，这个进程用了多少 CPU 秒"。
    CPU 利用率低而吞吐上不去，说明进程大部分时间在等（等模型、等数据库的写锁），而不是在算。"""

    def __init__(self, inner, emit):
        self.inner, self.emit, self.first = inner, emit, None

    async def __call__(self, job):
        if self.first is None:
            self.first = time.process_time()
        return await self.inner(job)

    async def aclose(self) -> None:  # worker 进程退出前会调用 handler.aclose()
        if self.first is not None:
            self.emit("cpu", seconds=round(time.process_time() - self.first, 4))
        inner_close = getattr(self.inner, "aclose", None)
        if inner_close is not None:
            await inner_close()


async def make_handler(wctx: WorkerContext):
    """worker 进程启动时调用一次：组装一个共享的 Agent，返回交给 run_worker 的 handler。"""
    opts = wctx.options
    wid = wctx.worker_id
    db = wctx.db  # 队列用的 SQLiteDB：检查点、幂等存储、工单表共用这一个连接和它的专用线程
    emit = make_emit(wid, opts.get("events", "1") == "1")

    ckpt = SQLiteCheckpointer(db)
    idem = LoggingIdempotencyStore(db, emit)
    await ckpt.setup()
    await idem.setup()

    def ddl(c):
        # tool_calls：工具函数每真正执行一次记一行；tickets：下游工单系统，按幂等键唯一
        c.execute("CREATE TABLE IF NOT EXISTS tool_calls (key TEXT, run_id TEXT, worker TEXT, pid INTEGER, t REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, key TEXT UNIQUE, run_id TEXT, "
                  "title TEXT, worker TEXT, t REAL)")

    await db.write(ddl)

    @tool(risk="write")
    async def create_ticket(title: str, ctx: ToolContext) -> str:
        """为用户的 IT 报修创建工单，返回工单号。每条报修只调用一次。"""
        key = ctx.idempotency_key  # run_id:tool_call_id —— 接手的 worker 重放同一个调用时，key 一模一样

        def op(c):
            now = time.time()
            c.execute("INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)", (key, ctx.run_id, wid, os.getpid(), now))
            c.execute("INSERT OR IGNORE INTO tickets (key, run_id, title, worker, t) VALUES (?, ?, ?, ?, ?)",
                      (key, ctx.run_id, title, wid, now))
            return c.execute("SELECT id FROM tickets WHERE key = ?", (key,)).fetchone()["id"]

        tid = await db.write(op)  # async：在数据库线程里执行，不阻塞事件循环
        emit("tool_executed", job=_job_id(), ticket=f"T-{1000 + tid}", key=key)
        return f"T-{1000 + tid}"

    if opts.get("offline", "1") == "1":
        llm = HelpdeskLLM(wid, float(opts.get("first_latency", 0.2)), float(opts.get("second_latency", 0.8)))
    else:
        llm = default_llm()
    slots = int(opts.get("model_slots", 0))
    if slots:
        sem = SQLiteSemaphore(db, "model", limit=slots, lease_seconds=float(opts.get("slot_lease", 10)))
        await sem.setup()
        llm = SlotLLM(llm, sem)

    agent = Agent(llm, [create_ticket], system_prompt=SYSTEM_PROMPT, name="helpdesk", max_steps=4,
                  checkpointer=ckpt, idempotency_store=idem,
                  hooks=[Timeline(emit, float(opts.get("chaos_window", 5.0)))])
    handler = AgentJobHandler(agent, ckpt)  # 一个共享的 Agent；每个任务用这次领取的 fence 创建检查点视图
    handler.aclose = agent.aclose  # 进程退出前关掉模型客户端的连接池（真实模型时）
    if opts.get("cpu_report") == "1":
        return CpuReport(handler, make_emit(wid))
    return handler


async def make_noop_handler(wctx: WorkerContext):
    """什么都不做的任务：只剩"领取 + 提交"两次写事务，用来测队列本身（SQLite 单写者）的吞吐上限。"""

    async def handler(job):
        return None

    return CpuReport(handler, make_emit(wctx.worker_id))
