"""ITBuddy 的 worker 进程。deploy.py（通过 agentkit.distributed.WorkerPool）这样启动 N 个：

    python -m agentkit.distributed.worker --queue sqlite:///capstone/runs/deploy/itbuddy.db \\
        --app capstone/worker_app.py:make_handler --worker-id worker-0 --lease 30 \\
        --opt enterprise_db=capstone/runs/deploy/enterprise.db --opt offline=1

每个 worker 是一个独立的操作系统进程，**不保存任何会话状态**（无状态 worker）：
- 从共享队列领任务（租约 + 心跳 + fence），用 AgentJobHandler 跑 ITBuddy：run 任务从头跑（任务里带着多轮对话的
  history），resume 任务从检查点恢复；
- 进程里只有**一个** Agent，asyncio 同时推进 --concurrency 个任务（工具是 async 的，等数据库 / 模型时让出事件循环）；
- 检查点、幂等记录、审计写进队列所在的同一个 SQLite 文件（共用这个进程的一个连接）；
  企业后端是另一个文件（模拟的外部系统，自己按 Idempotency-Key 去重）；
- 熔断器状态也在 SQLite 里（SQLiteCircuitBreaker）：一个 worker 发现模型挂了，所有 worker 立刻快速失败 / 降级；
- 追踪写 traces/<worker-id>.jsonl（每个进程一个文件）。
任何一个 worker 被 kill -9，它手上的任务在租约过期后被别的 worker 领走，从检查点接着跑。

--opt 选项：
    enterprise_db      企业后端的 SQLite 文件（必填）
    runs_dir           追踪文件的目录（默认：队列文件所在目录）
    offline            1 = 离线剧本模型（itbuddy/offline.py，零成本）；0 = 真实模型（读 .env）
    latency            离线模型每次调用的耗时（秒），默认 0.2
    post_commit_delay  故障注入：建单在下游提交之后再等这么多秒才返回。用来把 kill -9 卡在
                       "工单已经建好、结果还没记进检查点和幂等记录"的窗口里（test_server.py、deploy.py --demo）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentkit import Tracer, jsonl_exporter  # noqa: E402
from agentkit.distributed import AgentJobHandler, SQLiteCircuitBreaker, WorkerContext  # noqa: E402
from itbuddy import Backend, ITBuddyStores, build_agent, offline_llm  # noqa: E402


def _slow_after_commit(backend: Backend, delay: float) -> None:
    """故障注入：下游已经提交，但"响应"迟迟不回来（网络慢、下游慢）。worker 在这时被 kill -9，
    Agent 这一侧什么都没记下 —— 接手的 worker 只能重放同一个调用，靠下游的 Idempotency-Key 去重。"""
    create = backend.create_ticket

    async def create_ticket(*args, **kwargs):
        result = await create(*args, **kwargs)
        await asyncio.sleep(delay)
        return result

    backend.create_ticket = create_ticket


async def make_handler(wctx: WorkerContext) -> AgentJobHandler:
    opts = wctx.options
    queue_file = Path(wctx.queue_url[len("sqlite:///"):])
    runs = Path(opts.get("runs_dir") or queue_file.parent)
    stores = await ITBuddyStores.open(wctx.db, writer=wctx.worker_id)  # 借用队列的连接：同一个文件、同一个专用线程
    llm = offline_llm(latency=float(opts.get("latency", 0.2))) if opts.get("offline", "1") == "1" else None
    agent = await build_agent(
        llm,  # None → 真实模型（.env），同一个客户端（连接池）被这个进程里所有任务共用
        backend=opts["enterprise_db"],  # 传路径：build_agent 打开企业后端，agent.aclose() 时关闭
        stores=stores,
        approver=None,  # 高危操作一律暂停，等审批人通过 API 决定；恢复它的可能是另一个 worker 进程
        tracer=Tracer(exporter=jsonl_exporter(runs / "traces" / f"{wctx.worker_id}.jsonl")),
        breaker_factory=lambda model: SQLiteCircuitBreaker(wctx.db, model, failure_threshold=5, reset_timeout=30),
    )
    if float(opts.get("post_commit_delay", 0)) > 0:
        _slow_after_commit(agent.backend, float(opts["post_commit_delay"]))  # 工具在调用时才取 backend.create_ticket
    # 所有任务共用这一个 Agent；run 任务里的 history（多轮对话）由 AgentJobHandler 交给 agent.run。
    # 进程退出前 worker 命令行调用 handler.aclose() → agent.aclose()：关闭模型客户端和企业后端的连接
    return AgentJobHandler(agent, stores.checkpointer)
