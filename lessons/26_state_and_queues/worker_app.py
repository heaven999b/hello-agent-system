"""第 26 课的 worker 应用。每个 worker 进程由 WorkerPool 这样拉起（和生产里每个 Pod 跑的命令一样）：

    python -m agentkit.distributed.worker --queue postgresql://postgres@127.0.0.1:5432/helpdesk \\
        --app lessons/26_state_and_queues/worker_app.py:make_handler --opt redis=redis://127.0.0.1:6379/0 ...

**同一份代码、同一条命令，只把 --queue 从 sqlite:///… 换成 postgresql://…，就从第 13 课的单机多进程换到了多机后端。**
run_worker（领取、心跳、fence、优雅停机）和 AgentJobHandler（run / resume / 审批、fence 接管检查点）两边通用；
这里只根据队列 URL 选检查点：postgresql:// → PostgresCheckpointer，sqlite:/// → SQLiteCheckpointer（和队列共用 wctx.db）。

业务：IT 服务台 Agent，三个工具 —— search_kb（只读）、create_ticket（写：下游工单表的唯一约束做幂等）、
reset_password（高危：需要人工审批）。可选（--opt redis=URL）：Redis 幂等缓存 + 按租户的 Lua 令牌桶限流。

worker 进程和 demo 之间只通过数据库（队列、检查点、工单表）和 Redis 协作；另外把自定义事件以一行 JSON 打到标准输出
（和 run_worker 自己的事件同一个格式），demo 用 WorkerPool.events() 收集，据此打印时间线、决定什么时候注入故障。

--opt 选项：
    llm=scripted|real      scripted：离线剧本（ScriptedLLM，latency 秒的 asyncio.sleep 是显式的"模型耗时"模型）
    latency=0.15           每次模型调用的模拟耗时（秒）
    redis=URL              打开 Redis 幂等缓存 + 限流（RateLimitHook，等 rl_wait 秒还拿不到令牌就推迟任务）
    rl_wait=1.0
    pool=4                 Postgres 检查点连接池的上限
    hold=1                 反模式：每个任务在等模型的整段时间里都占着一个"业务库"连接（池只有 4 个）
    gate=PATH              第一次领取时，第二次模型调用停在这里，直到 demo 创建这个文件（网络分区场景用）
    llm_events=1           每次模型调用打一条事件（开始 / 结束时间），demo 用它算所有进程加起来的在途峰值
    slots=N / slots_db=P   真实模型模式：本机所有 worker 共用 N 个模型并发名额（SQLiteSemaphore，带租约）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Annotated, Literal

from pydantic import Field

from agentkit import Agent, PermissionPolicy, ScriptedLLM, ToolContext, call_tool, default_llm, reply, tool
from agentkit.distributed import AgentJobHandler, SQLiteCheckpointer, SQLiteSemaphore, WorkerContext, current_job

TENANTS = {  # 租户 → (套餐, 每秒模型调用数, 桶容量)
    "acme": ("标准", 4.0, 4),
    "globex": ("标准", 4.0, 4),
    "initech": ("免费", 1.0, 2),
}

SYSTEM_PROMPT = (
    "你是公司的 IT 服务台助手。每条用户消息只调用一次工具：\n"
    "1) 设备或系统故障 → 调用 create_ticket 建工单，然后用一句话告诉用户工单号；\n"
    "2) '怎么/如何'之类的问题 → 调用 search_kb，然后根据结果用一两句话回答；\n"
    "3) 要求重置密码 → 调用 reset_password。\n不要追问，回答用简短的中文。"
)

KB = {
    "VPN": "VPN 证书在自助门户 → 证书管理里一键续期，续期后重启客户端。",
    "Git": "在权限平台提交 Git 仓库权限申请，直属主管审批后自动开通。",
    "邮箱": "邮箱设置 → 存储管理里清理 30 天前的大附件，或申请扩容到 50GB。",
    "Wi-Fi": "访客 Wi-Fi 名为 Guest-5F，密码每天在前台屏幕上更新。",
}

# 下游系统（另一个团队的工单服务）的表：demo 在拉起 worker 之前建好（很多进程同时建表会撞车，见 README 问题 6）
TICKETS_DDL = [
    """CREATE TABLE IF NOT EXISTS tickets (
        id              serial PRIMARY KEY,
        idempotency_key text UNIQUE,            -- 同一个 key 只能建一张工单：由数据库保证，不靠调用方自觉
        tenant_id       text,
        run_id          text,
        title           text,
        created_by      text,
        created_at      timestamptz DEFAULT now())""",
    # 工具体每真正执行一次记一行（包括被唯一约束挡下的重放）：用来证明"执行了几次、用的是不是同一个幂等键"
    """CREATE TABLE IF NOT EXISTS ticket_calls (
        key text, run_id text, worker text, pid int, attempt int, fence bigint, created boolean,
        t timestamptz NOT NULL DEFAULT clock_timestamp())""",
]


_CHAOS_ONCE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
if redis.call('SET', KEYS[2], ARGV[1], 'NX') then
  redis.call('SET', KEYS[1], ARGV[1])
  return 1
end
return 0
"""


def make_emit(worker_id: str):
    def emit(event: str, **info) -> None:
        line = {"event": event, "worker_id": worker_id, "pid": os.getpid(), "t": round(time.time(), 4), **info}
        print(json.dumps(line, ensure_ascii=False, default=str), flush=True)

    return emit


def _job():
    job = current_job()
    return (job.id, job.attempts, job.fence, job.payload or {}) if job is not None else (None, 0, None, {})


# =====================================================================================
# 模型
# =====================================================================================


def scripted_reply(messages):
    """离线剧本：按对话内容决定"调用哪个工具"或"怎么回答"（responder 模式：每个任务看自己的对话，接手的 worker 也一样）。"""
    last = messages[-1]
    if last["role"] == "tool":
        content = last.get("content") or ""
        if m := re.search(r"T-\d+", content):
            return reply(f"已为你创建工单 {m.group(0)}，IT 同事会尽快联系你。")
        if "重置" in content:
            return reply("密码已重置，新密码已发到你的企业邮箱。")
        if content.startswith(("拒绝", "未执行", "错误")):
            return reply("这个操作没有完成，请稍后再试或联系 IT 同事。")
        return reply(f"根据知识库：{content}")
    text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if "密码" in text:
        return call_tool("reset_password", user="zhang.san")
    if "怎么" in text or "如何" in text:
        return call_tool("search_kb", query=text[:12])
    return call_tool("create_ticket", title=text[:20], priority="medium")


class ObservedLLM:
    """包一层：① 可选地把每次调用的开始 / 结束时间打成事件（算跨进程的在途峰值）；
    ② 可选的 gate：第一次领取时，第二次模型调用停在 gate 上，直到 demo 创建 gate 文件 ——
       demo 就能在"工具已执行、检查点已写入、模型调用进行中"这个确定的时刻断网，而不是赌 sleep 的时长；
    ③ 可选的跨进程模型并发名额（真实模型模式，SQLiteSemaphore）。"""

    def __init__(self, inner, emit, *, events: bool, gate: str | None, slots: SQLiteSemaphore | None):
        self.inner, self.emit, self.events, self.gate, self.slots = inner, emit, events, gate, slots
        self.model = getattr(inner, "model", "llm")

    async def chat(self, messages, tools=None, **kwargs):
        job_id, attempt, _, _ = _job()
        second = messages[-1]["role"] == "tool"
        if self.gate:
            self.emit("llm_call", job=job_id, attempt=attempt, phase="second" if second else "first")
            if second and attempt == 1:
                while not os.path.exists(self.gate):
                    await asyncio.sleep(0.05)
        t0 = time.time()
        if self.slots is not None:
            async with self.slots.slot():
                response = await self.inner.chat(messages, tools, **kwargs)
        else:
            response = await self.inner.chat(messages, tools, **kwargs)
        if self.events:
            self.emit("llm", job=job_id, t0=round(t0, 4), t1=round(time.time(), 4))
        return response

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


# =====================================================================================
# handler
# =====================================================================================


class HelpdeskHandler(AgentJobHandler):
    """AgentJobHandler + 两件 demo 用的小事：接手时打印前任留下的检查点；一个"跑完之后、提交之前被冻结"的故障窗口。"""

    def __init__(self, agent, checkpointer, *, emit, closers, chaos_once, **kw):
        super().__init__(agent, checkpointer, **kw)
        self.emit, self.closers, self.chaos_once = emit, closers, chaos_once

    async def __call__(self, job) -> dict:
        if job.attempts > 1 and (job.payload or {}).get("op") == "run":
            prev = await self.checkpointer.get_run(job.payload.get("run_id") or f"job-{job.id}")
            if prev is not None:
                self.emit("takeover", job=job.id, attempts=job.attempts, fence=job.fence, prev_writer=prev["writer"],
                          prev_status=prev["status"], prev_step=prev["state"]["step"])
        result = await super().__call__(job)
        if (job.payload or {}).get("chaos") == "freeze_before_complete" and await self.chaos_once(job.id, "before_complete"):
            self.emit("chaos_window", job=job.id, fence=job.fence, action="freeze", stage="before_complete")
            await asyncio.sleep(1.0)  # demo 在这 1 秒里 SIGSTOP 这个进程；醒来后接着提交 —— 会被 fence 拒绝
            self.emit("woke", job=job.id, stage="before_complete")
        return result

    async def aclose(self) -> None:
        await super().aclose()  # 关掉共享 Agent（模型客户端、工具线程池）和检查点自己的连接池
        for close in self.closers:
            await close()


class HoldConnection:
    """❌ 反模式：开着一个业务库连接去等模型。并发度被卡成这个池的大小（第 3 部分最后一行）。"""

    def __init__(self, inner, pool):
        self.inner, self.pool = inner, pool

    async def __call__(self, job):
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")
            return await self.inner(job)

    async def aclose(self) -> None:
        await self.inner.aclose()
        await self.pool.close()


async def make_handler(wctx: WorkerContext):
    """worker 进程启动时调用一次：组装一个共享的 Agent（所有任务共用），返回交给 run_worker 的 handler。"""
    opts, wid = wctx.options, wctx.worker_id
    emit = make_emit(wid)
    postgres = wctx.queue_url.startswith(("postgres://", "postgresql://"))
    closers: list = []

    # ---- 检查点：唯一和后端有关的一行
    if postgres:
        from psycopg_pool import AsyncConnectionPool

        from agentkit.contrib.postgres import PostgresCheckpointer

        # check_connection：借出连接前先检查。网络分区 / 数据库重启之后，池里空闲的连接其实已经断了，
        # 不检查的话，恢复后的第一次写入会撞上一个死连接 —— 失败原因就变成了"网络错误"而不是"你已经不是持有者"
        ckpt = PostgresCheckpointer(wctx.queue_url, pool_kwargs={
            "min_size": 1, "max_size": int(opts.get("pool", 4)), "check": AsyncConnectionPool.check_connection})
    else:
        ckpt = SQLiteCheckpointer(wctx.db)  # 和队列共用一个 SQLiteDB（一个连接、一个数据库线程）
    await ckpt.setup()

    # ---- 下游工单系统（只在 Postgres 上建了表；它是"另一个团队的服务"，有自己的连接池）
    tickets_pool = None
    if postgres:
        from psycopg_pool import AsyncConnectionPool

        tickets_pool = AsyncConnectionPool(wctx.queue_url, min_size=1, max_size=4, kwargs={"autocommit": True},
                                           check=AsyncConnectionPool.check_connection, open=False)
        await tickets_pool.open(wait=False)
        closers.append(tickets_pool.close)

    # ---- Redis：幂等缓存 + 按租户限流（可选）
    idem, hooks, r = None, [PermissionPolicy()], None
    if opts.get("redis"):
        import redis.asyncio as aredis

        from agentkit.contrib.redis_store import RateLimitHook, RedisIdempotencyStore, RedisTokenBucket
        from agentkit.hooks import StopRun

        r = aredis.Redis.from_url(opts["redis"])
        closers.append(r.aclose)
        idem = RedisIdempotencyStore(r, ttl_seconds=3600)
        bucket = RedisTokenBucket(r, 4.0, 4, overrides={t: (rate, cap) for t, (_, rate, cap) in TENANTS.items()})

        class MeteredRateLimit(RateLimitHook):
            """把每个租户的限流数据记进 Redis：进程被 kill 了数据也还在，demo 最后统一汇总。
            before_llm 是 async 的（等令牌时让出事件循环），覆盖它也要写成 async def、await 父类。"""

            async def before_llm(self, state, messages) -> None:
                key = state.metadata.get("tenant_id") or "anonymous"
                t0 = time.monotonic()
                try:
                    await super().before_llm(state, messages)
                    await r.hincrby(f"demo:rl:{key}", "calls", 1)
                except StopRun:
                    await r.hincrby(f"demo:rl:{key}", "rejected", 1)
                    raise
                finally:
                    await r.hincrbyfloat(f"demo:rl:{key}", "waited_s", time.monotonic() - t0)

        hooks.append(MeteredRateLimit(bucket, wait_timeout=float(opts.get("rl_wait", 1.0))))

    async def chaos_once(job_id, stage: str) -> bool:
        """决定这次要不要开故障窗口（只在 demo 第 1 部分用）：每个任务只开一次，每个 worker 进程也只承受一次故障。

        只按"第一次领取"判断不够：并发 2 时，同一个进程里的另一个任务被冻结 / 被杀，这个任务也会被别人接手，
        窗口就永远开不了；而一个进程同时撞上两次故障（先被 kill，再被要求冻结）也没有意义。所以用一段 Lua 在 Redis 里
        原子地判断：任务的标记还没有、并且这个进程还没承受过故障 → 两个标记一起写上。没开成的任务，接手它的进程
        重放到同一个位置时还会再试一次。"""
        if r is None:
            return False
        return bool(await r.eval(_CHAOS_ONCE_LUA, 2, f"demo:chaos:{job_id}:{stage}", f"demo:chaos:worker:{wid}", wid))

    # ---- 工具
    @tool
    def search_kb(query: Annotated[str, Field(description="要查的问题关键词")]) -> str:
        """在 IT 知识库里查找操作指引。"""
        for k, v in KB.items():
            if k.lower() in query.lower():
                return v
        return "知识库里没有找到相关条目，建议提交工单。"

    @tool(risk="write", timeout_s=120)
    async def create_ticket(
        title: Annotated[str, Field(description="一句话概括问题，不超过 30 字")],
        priority: Annotated[Literal["low", "medium", "high"], Field(description="影响个人 medium，影响多人 high")],
        ctx: ToolContext,
    ) -> str:
        """为设备或系统故障创建 IT 工单，返回工单号。每条报修只调用一次。"""
        if tickets_pool is None:
            return "工单系统不可用（本 worker 没有连接工单库）"
        job_id, attempt, fence, payload = _job()
        # 幂等键 = run_id + tool_call_id：run_id 由任务决定（job-7），tool_call_id 在检查点里，
        # 所以无论哪个 worker、第几次尝试，重放这次调用时 key 都一样。INSERT ... ON CONFLICT DO NOTHING：
        # 执行副作用和记下幂等键是同一条语句，中间没有缝
        async with tickets_pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO tickets (idempotency_key, tenant_id, run_id, title, created_by) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                (ctx.idempotency_key, ctx.tenant_id, ctx.run_id, title, wid))
            row = await cur.fetchone()
            created = row is not None
            if not created:
                row = await (await conn.execute("SELECT id FROM tickets WHERE idempotency_key = %s",
                                                (ctx.idempotency_key,))).fetchone()
            await conn.execute("INSERT INTO ticket_calls (key, run_id, worker, pid, attempt, fence, created) "
                               "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                               (ctx.idempotency_key, ctx.run_id, wid, os.getpid(), attempt, fence, created))
        no = f"T-{1000 + row[0]}"
        emit("ticket", job=job_id, ticket=no, key=ctx.idempotency_key, created=created)
        chaos = payload.get("chaos")
        if chaos in ("kill_after_ticket", "freeze_mid_run") and await chaos_once(job_id, "after_ticket"):
            action = "kill" if chaos == "kill_after_ticket" else "freeze"
            emit("chaos_window", job=job_id, fence=fence, action=action, stage="after_ticket")
            await asyncio.sleep(30 if action == "kill" else 1.0)  # 等 demo 下手（被冻结时这段等待也一起冻结）
            emit("woke", job=job_id, stage="after_ticket")
        if payload.get("hang") and attempt == 1:  # 优雅停机场景：下游已经建好了工单，响应"还在路上"
            emit("downstream_done", job=job_id, key=ctx.idempotency_key)
            await asyncio.sleep(60)
        return f"已创建工单 {no}"

    @tool(risk="dangerous")
    def reset_password(user: Annotated[str, Field(description="要重置密码的账号")]) -> str:
        """重置员工账号密码（高风险：需要人工审批）。"""
        return f"{user} 的密码已重置，新密码已发送到企业邮箱。"

    # ---- 模型
    slots = None
    if opts.get("llm", "scripted") == "real":
        inner = default_llm(max_connections=1)
        if int(opts.get("slots", 0)):
            slots = SQLiteSemaphore(opts["slots_db"], "model", limit=int(opts["slots"]), lease_seconds=10)
            await slots.setup()
            closers.append(slots.close)
    else:
        inner = ScriptedLLM(responder=scripted_reply, latency=float(opts.get("latency", 0.15)), keep_calls=0)
    llm = ObservedLLM(inner, emit, events=opts.get("llm_events") == "1", gate=opts.get("gate"), slots=slots)

    agent = Agent(llm, [search_kb, create_ticket, reset_password], system_prompt=SYSTEM_PROMPT, name="helpdesk",
                  max_steps=8, checkpointer=ckpt, hooks=hooks, idempotency_store=idem)
    # 一个共享的 Agent；每个任务用这次领取的 fence 创建检查点视图（接管），rate_limited → RetryLater（推迟、不计入尝试次数）
    handler = HelpdeskHandler(agent, ckpt, emit=emit, closers=closers, chaos_once=chaos_once, defer_seconds=1.0)
    if opts.get("hold") == "1" and postgres:
        from psycopg_pool import AsyncConnectionPool

        business = AsyncConnectionPool(wctx.queue_url, min_size=4, max_size=4, kwargs={"autocommit": True}, open=False)
        await business.open(wait=True)
        return HoldConnection(handler, business)
    return handler
