"""迷你部署的 worker 进程。WorkerPool 这样启动它：

    python -m agentkit.distributed.worker --queue sqlite:///runs/mini/jobs.db \\
        --app lessons/12_production_architecture/worker_app.py:make_handler --opt offline=1 ...

每个 worker 是一个独立的操作系统进程：从共享队列领任务，用 AgentJobHandler 跑 HR Agent，
每走一步把检查点写进同一个 SQLite 文件 —— 所以任何一个 worker 被杀掉，另一个都能从检查点接着跑（无状态 worker）。

两层按租户的舱壁（bulkhead），在交给 Agent 之前先拿槽位：
  - 进程内：agentkit.limits.KeyedLimiter，每个租户在这个进程里最多 tenant_local 个同时在跑；
  - 跨进程：agentkit.distributed.SQLiteSemaphore，每个租户在所有 worker 进程加起来最多 tenant_shared 个。
拿不到槽位 → RetryLater：任务放回队列、稍后再来，不消耗重试次数，这个 worker 转去领别的租户的任务。
每次真正占用槽位的起止时间记在 slot_log 表里，demo 用它算"同一时刻最多有几个"。
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hr_agent  # noqa: E402  同目录模块

from agentkit.distributed import (  # noqa: E402
    AgentJobHandler,
    RetryLater,
    SQLiteCheckpointer,
    SQLiteSemaphore,
    WorkerContext,
)
from agentkit.limits import KeyedLimiter, LimitExceeded  # noqa: E402


async def make_handler(wctx: WorkerContext):
    opts = wctx.options
    db = wctx.db
    offline = opts.get("offline", "1") == "1"
    latency, long_latency = float(opts.get("latency", 0.25)), float(opts.get("long_latency", 1.5))
    ckpt = SQLiteCheckpointer(db)
    await ckpt.setup()
    await db.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS slot_log (tenant TEXT, job INTEGER, worker TEXT, pid INTEGER, start REAL, end REAL)"
    ))

    # 真实模型：整个进程共用一个客户端（和它的连接池）；本机只有一个真实模型，所有逻辑模型都映射到它
    real_llm = None if offline else hr_agent.make_llm("mini", offline=False)

    def make_agent(ckpt_view, job):
        # 每个任务一个 Agent：模型是 API 路由出来的逻辑模型（在 payload 里），检查点是这次领取的 fence 视图
        model = (job.payload.get("metadata") or {}).get("model", "mini")
        llm = hr_agent.make_llm(model, True, latency, long_latency) if offline else real_llm
        return hr_agent.make_agent(llm, checkpointer=ckpt_view)

    agent_handler = AgentJobHandler(make_agent, ckpt)
    local = KeyedLimiter(per_key=int(opts.get("tenant_local", 2)))
    shared_limit = int(opts.get("tenant_shared", 3))
    shared: dict[str, SQLiteSemaphore] = {}

    async def shared_slots(tenant: str) -> SQLiteSemaphore:
        if tenant not in shared:
            sem = SQLiteSemaphore(db, f"tenant:{tenant}", limit=shared_limit, lease_seconds=5)
            await sem.setup()
            shared[tenant] = sem
        return shared[tenant]

    async def handler(job):
        tenant = job.tenant_id
        try:
            async with local.slot(tenant, timeout=0.2):  # 这个进程里的舱壁
                async with (await shared_slots(tenant)).slot(timeout=0.2):  # 所有进程共用的舱壁
                    start = time.time()
                    try:
                        return await agent_handler(job)
                    finally:
                        end = time.time()
                        await db.write(lambda c: c.execute("INSERT INTO slot_log VALUES (?, ?, ?, ?, ?, ?)",
                                                           (tenant, job.id, wctx.worker_id, os.getpid(), start, end)))
        except LimitExceeded:
            # 舱壁满了：不在这里干等（会占着 worker 的并发名额），把任务放回队列，稍后再来，不消耗重试次数
            raise RetryLater(random.uniform(0.3, 0.6), f"租户 {tenant} 的舱壁已满") from None

    async def aclose() -> None:  # worker 进程退出前调用：关掉真实模型客户端的连接池
        close = getattr(real_llm, "aclose", None)
        if close is not None:
            await close()

    handler.aclose = aclose
    return handler
