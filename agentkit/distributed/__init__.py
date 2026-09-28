"""agentkit.distributed —— 多进程执行：持久化任务队列、租约、fencing、worker 进程池（第 13 课）。

一个进程里，Agent 用 asyncio 同时推进几百个会话；多个进程之间靠这里的东西分工和接手：

    jobs       Job、JobQueue 协议、run_worker（worker 主循环：背压、心跳续租、优雅停机）、
               AgentJobHandler（把 Agent 的 run / resume / 审批包装成任务）、异常（LeaseLost、CheckpointConflict……）
    sqlite     单机多进程后端（零依赖）：SQLiteJobQueue、SQLiteCheckpointer（版本号 CAS + fence 接管）、
               SQLiteIdempotencyStore、SQLiteTokenBucket、SQLiteSemaphore（跨进程并发槽位，崩溃自动释放）、
               SQLiteCircuitBreaker（所有进程共享的熔断器）
    worker     worker 进程入口：python -m agentkit.distributed.worker --queue sqlite:///jobs.db --app app.py:factory
    processes  WorkerPool：在本机拉起 N 个 worker 进程，kill -9 / SIGSTOP / SIGTERM 故障注入
    chaos      TcpProxy：放在 worker 和数据库之间的 TCP 代理，随时断网（网络分区）/ 加延迟（第 26 课）

多机部署：把 SQLite 换成 agentkit.contrib.postgres 里接口相同的 PostgresJobQueue / PostgresCheckpointer（第 26 课），
run_worker、AgentJobHandler、worker 命令行都不用改。
"""

from .chaos import TcpProxy
from .jobs import (
    JOB_STATUSES,
    AgentJobHandler,
    CheckpointConflict,
    Job,
    JobQueue,
    LeaseGuard,
    LeaseLost,
    PermanentJobError,
    RetryLater,
    current_job,
    run_worker,
    stop_on_signals,
)
from .processes import WorkerPool, WorkerProcess
from .sqlite import (
    SQLiteCheckpointer,
    SQLiteCircuitBreaker,
    SQLiteDB,
    SQLiteIdempotencyStore,
    SQLiteJobQueue,
    SQLiteSemaphore,
    SQLiteTokenBucket,
)
from .app import WorkerContext, load_attr, open_queue

__all__ = [name for name in dir() if not name.startswith("_")]
