"""任务、租约与 worker 循环：多个 worker 进程怎么安全地分活干（第 13 课）。

一个进程里，Agent 用 asyncio 同时推进几百个会话；但一个进程终究会崩溃、会被部署替换、会被卡住。
所以服务端的标准结构是：API 进程只负责把请求写进**持久化队列**，若干个 worker 进程从队列里领任务执行。

    API ──enqueue──► 队列（SQLite / Postgres） ◄──claim / heartbeat / complete── worker 进程 × N

这里的代码与"队列存在哪里"无关：
- `SQLiteJobQueue` / `SQLiteCheckpointer`（agentkit.distributed.sqlite）：单机多进程，零依赖；
- `PostgresJobQueue` / `PostgresCheckpointer`（agentkit.contrib.postgres）：多机，第 26 课。
它们实现同一个 JobQueue 协议，`run_worker` 和 `AgentJobHandler` 两边通用。

三个关键设计（第 13 课详讲）：
1. 租约（lease）：领取不是"拿走"，而是"借走一段时间"，要靠心跳续租。worker 崩溃 → 租约过期 → 别人接手，任务不丢；
2. fencing token（fence）：每次领取发一个全局递增的号。心跳 / 提交 / 检查点写入都要带上它，
   对不上就拒绝 → 租约过期后"诈尸"的旧 worker 没法覆盖新 worker 的结果；
3. 尝试次数上限 + 死信：一个每次都让 worker 崩溃的"毒消息"，最多害死 max_attempts 个 worker。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import signal
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol

from ..hooks import Hook, StopRun
from ..state import RunState
from ..timeouts import wait_for
from ..tools import maybe_await

logger = logging.getLogger("agentkit.distributed")

JOB_STATUSES = ("queued", "leased", "succeeded", "failed", "dead")


# =====================================================================================
# 异常
# =====================================================================================


class CheckpointConflict(RuntimeError):
    """检查点的版本号对不上：在你上次读 / 写之后，另一个 worker 已经写过或接管了这个 run。

    正确的反应是**立刻停手**（不要重试、不要覆盖）：你手里的状态已经过时了。
    """

    def __init__(self, run_id: str, expected: int | None, actual: int | None, writer: str | None = None, detail: str = ""):
        self.run_id, self.expected, self.actual, self.writer = run_id, expected, actual, writer
        msg = f"检查点冲突：run {run_id} 期望版本 {expected}，实际版本 {actual}"
        if writer:
            msg += f"（最后写入者 {writer}）"
        msg += " —— 另一个 worker 已经接管了这个 run，停止处理，不要覆盖。"
        if detail:
            msg += f" {detail}"
        super().__init__(msg)


class LeaseLost(RuntimeError):
    """带 fence 的写入（心跳 / 提交 / 失败 / 归还）被拒绝：任务已经不归你了。"""


class RetryLater(Exception):
    """handler 抛出它表示"现在做不了，过一会儿再来"（如被限流）：任务放回队列，**不消耗**重试次数。"""

    def __init__(self, delay_seconds: float, reason: str = ""):
        super().__init__(reason or f"retry after {delay_seconds}s")
        self.delay_seconds = float(delay_seconds)
        self.reason = reason


class PermanentJobError(Exception):
    """handler 抛出它表示"再试也没用"（参数非法、找不到 run、租户不匹配）：任务直接进入 failed。"""


# =====================================================================================
# 任务与队列协议
# =====================================================================================


@dataclass
class Job:
    id: int
    kind: str
    tenant_id: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    fence: int
    priority: int = 0
    worker_id: str | None = None
    lease_until: Any = None  # Postgres：datetime（服务器时钟）；SQLite：unix 秒（本机时钟）
    run_at: Any = None
    idempotency_key: str | None = None
    result: Any = None
    last_error: str | None = None
    created_at: Any = None
    updated_at: Any = None
    finished_at: Any = None
    # 心跳发现租约丢失时置位；handler 可以据此提前停手（AgentJobHandler 会自动检查）
    lost: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @classmethod
    def from_row(cls, row: dict) -> "Job":
        names = {f for f in cls.__dataclass_fields__ if f != "lost"}
        return cls(**{k: v for k, v in row.items() if k in names})


class JobQueue(Protocol):
    """持久化任务队列。所有写操作都带 fence 校验：对不上 → LeaseLost。"""

    async def enqueue(self, kind: str, payload: dict, *, tenant_id: str, idempotency_key: str | None = None,
                      priority: int = 0, delay_seconds: float = 0.0, max_attempts: int | None = None) -> int: ...

    async def claim(self, worker_id: str, lease_seconds: float = 30, kinds: Iterable[str] | None = None) -> Job | None: ...

    async def heartbeat(self, job: Job, lease_seconds: float = 30) -> Any: ...

    async def complete(self, job: Job, result: Any = None) -> None: ...

    async def fail(self, job: Job, error: str, retryable: bool = True) -> str: ...

    async def release(self, job: Job, *, delay_seconds: float = 0.0, reason: str | None = None,
                      count_attempt: bool = False) -> None: ...

    async def get(self, job_id: int) -> Job | None: ...

    async def stats(self) -> dict: ...


def explain_rejection(job: Job, cur: Job | None, action: str) -> str:
    """带 fence 的写入被拒绝时，给出人能看懂的原因。"""
    if cur is None:
        return f"任务 #{job.id} 不存在，{action}被拒绝"
    if cur.fence != job.fence:
        return (f"任务 #{job.id} 已被重新领取：当前 fence={cur.fence}（持有者 {cur.worker_id}），"
                f"你的 fence={job.fence} 已过期，{action}被拒绝")
    return f"任务 #{job.id} 当前状态为 {cur.status}（不再是 leased：已被回收或已结束），{action}被拒绝"


def summarize_stats(counts: dict[str, int], ready: int, expired: int, oldest_age: float | None) -> dict:
    return {"counts": {s: counts.get(s, 0) for s in JOB_STATUSES}, "ready": ready,
            "oldest_queued_age_s": oldest_age, "expired_leases": expired}


# =====================================================================================
# worker 循环
# =====================================================================================


def _emitter(worker_id: str, on_event):
    def emit(name: str, **info) -> None:
        if on_event is not None:
            try:
                on_event(name, {"worker_id": worker_id, **info})
            except Exception:  # noqa: BLE001 —— 日志回调出错不能拖垮 worker
                logger.exception("on_event 回调出错")

    return emit


async def _acquire_or_stop(sem: asyncio.Semaphore, stop_event: asyncio.Event) -> bool:
    """等一个并发名额，或者等到停机信号，谁先到算谁。拿到名额返回 True；停机返回 False（名额已归还）。

    只 await sem.acquire() 的话，满载时主循环停在这里，看不到停机信号：在途任务要跑 8 秒，
    grace_period=1 也要等 8 秒才退出（第 12/13 课在真实进程上测出来的）。
    """
    if stop_event.is_set():
        return False
    acquire = asyncio.ensure_future(sem.acquire())
    stop = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
        if not acquire.done():
            acquire.cancel()
        await asyncio.gather(acquire, stop, return_exceptions=True)
    got = acquire.done() and not acquire.cancelled() and acquire.exception() is None
    if got and stop_event.is_set():  # 两个同时到达：停机优先，名额还回去
        sem.release()
        return False
    return got


async def _wait_or_timeout(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await wait_for(stop_event.wait(), timeout)  # 取消安全版：3.12 之前的 asyncio.wait_for 可能吞掉停机时的取消
    except asyncio.TimeoutError:
        pass


async def run_worker(
    queue: JobQueue,
    handler: Callable[[Job], Awaitable[Any]],
    *,
    worker_id: str,
    stop_event: asyncio.Event,
    concurrency: int = 16,
    lease_seconds: float = 30,
    poll_interval: float = 0.5,
    heartbeat_interval: float | None = None,
    grace_period: float = 25.0,
    kinds: Iterable[str] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    max_jobs: int | None = None,
    transient_errors: tuple[type[BaseException], ...] | None = None,
    release_on_cancel: bool = False,
) -> dict:
    """worker 主循环：**一个进程**用 asyncio 同时处理最多 concurrency 个任务。返回计数统计。

    - **背压**：领取前先拿 asyncio.Semaphore 的一个名额；满载时停在这里，不再 claim ——
      任务留在队列里，别的 worker 进程还能领走（而不是被这个进程"囤"在内存里）。
    - 每个任务一个续租协程，每 heartbeat_interval（默认租约的 1/3）续一次；续租被拒（fence 过期）→ job.lost 置位。
    - handler 是 async 函数（AgentJobHandler 就是）。handler 里千万不要做阻塞调用：
      一个阻塞调用会卡住整个事件循环，这个进程里所有任务的心跳一起停，租约一起过期。
    - **停机**：stop_event 置位后不再领取；等在途任务最多 grace_period 秒，超时的任务被取消，
      **不提交、不归还**，它们的租约自然过期后由别的 worker 从检查点接手（fence 保证取消前的写入不会覆盖接手者）。
      grace_period 要小于 K8s 的 terminationGracePeriodSeconds（默认 30 秒），给取消和清理留出时间。
      release_on_cancel=True 时，被取消的任务立刻（带 fence）归还队列，别的 worker 马上就能领，不必等租约过期；
      代价是多一次数据库写，而且归还时如果数据库不可用，照样只能等租约过期。
    - transient_errors：队列后端"暂时不可用"的异常类型（数据库重启、连接池借不到连接），领取时遇到就退避重试。
      不传则用队列自己声明的 queue.transient_errors（PostgresJobQueue 声明了 psycopg 的连接错误）。

    统计：claimed / succeeded / failed / deferred / fence_rejected / ownership_lost / commit_errors /
          cancelled（停机时被取消的任务数）/ max_in_flight（同时在跑的任务数峰值）。
    """
    if concurrency < 1:
        raise ValueError("concurrency 至少为 1")
    if not (inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(getattr(handler, "__call__", None))):
        raise TypeError("handler 必须是 async 函数（或 __call__ 是 async 的对象，例如 AgentJobHandler）")
    hb_interval = heartbeat_interval or max(lease_seconds / 3, 0.05)
    if transient_errors is None:
        transient_errors = tuple(getattr(queue, "transient_errors", ()))
    stats = {"claimed": 0, "succeeded": 0, "failed": 0, "deferred": 0, "fence_rejected": 0, "ownership_lost": 0,
             "commit_errors": 0, "cancelled": 0, "max_in_flight": 0}
    emit = _emitter(worker_id, on_event)
    sem = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task] = set()

    async def heartbeat(job: Job) -> None:
        while True:
            await asyncio.sleep(hb_interval)
            try:
                await queue.heartbeat(job, lease_seconds)
            except LeaseLost as e:
                job.lost.set()
                emit("heartbeat_rejected", job=job.id, fence=job.fence, error=str(e))
                return
            except Exception as e:  # noqa: BLE001 —— 暂时连不上数据库：下一轮再试，租约还没到期
                emit("heartbeat_error", job=job.id, error=f"{type(e).__name__}: {e}")

    async def process(job: Job) -> None:
        hb = asyncio.create_task(heartbeat(job))
        try:
            try:
                result = await handler(job)
            except RetryLater as e:
                await queue.release(job, delay_seconds=e.delay_seconds, reason=f"deferred: {e.reason}")
                stats["deferred"] += 1
                emit("deferred", job=job.id, delay_seconds=e.delay_seconds, reason=e.reason)
            except (CheckpointConflict, LeaseLost) as e:
                # 任务已经归别人了：什么都不提交（提交也会被 fence 拒绝），安静地放手
                stats["ownership_lost"] += 1
                emit("ownership_lost", job=job.id, fence=job.fence, error=str(e))
            except PermanentJobError as e:
                await queue.fail(job, f"PermanentJobError: {e}", retryable=False)
                stats["failed"] += 1
                emit("failed", job=job.id, error=str(e), status="failed")
            except Exception as e:  # noqa: BLE001  （CancelledError 不是 Exception，会直接穿过去）
                status = await queue.fail(job, f"{type(e).__name__}: {e}", retryable=True)
                stats["failed"] += 1
                emit("failed", job=job.id, error=f"{type(e).__name__}: {e}", status=status)
            else:
                await queue.complete(job, result)
                stats["succeeded"] += 1
                emit("completed", job=job.id, fence=job.fence, kind=job.kind, tenant_id=job.tenant_id, result=result)
        except LeaseLost as e:
            stats["fence_rejected"] += 1
            emit("fence_rejected", job=job.id, fence=job.fence, error=str(e))
        except asyncio.CancelledError:
            if release_on_cancel:
                # shield：这次归还本身不能再被取消打断；归还失败（租约已丢、数据库不可用）就算了，等租约过期
                try:
                    await asyncio.shield(queue.release(job, reason="cancelled on shutdown"))
                    emit("released_on_cancel", job=job.id, fence=job.fence)
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as e:  # noqa: BLE001 —— 提交时数据库出错：不确定是否已提交，交给租约过期后重新领取
            stats["commit_errors"] += 1
            emit("commit_error", job=job.id, error=f"{type(e).__name__}: {e}")
        finally:
            hb.cancel()
            sem.release()

    emit("started", concurrency=concurrency)
    idle_errors = 0
    while not stop_event.is_set():
        if max_jobs is not None and stats["claimed"] >= max_jobs:
            break
        if not await _acquire_or_stop(sem, stop_event):  # 背压：满载时停在这里（但停机信号能叫醒它）
            break
        try:
            job = await queue.claim(worker_id, lease_seconds, kinds)
            idle_errors = 0
        except transient_errors as e:  # 数据库暂时不可用 / 连接池借不到连接：退避后重试
            sem.release()
            idle_errors += 1
            emit("claim_error", error=f"{type(e).__name__}: {e}")
            await _wait_or_timeout(stop_event, min(30.0, poll_interval * 2 ** min(idle_errors, 6)))
            continue
        except BaseException:
            sem.release()
            raise
        if job is None:
            sem.release()
            # 空闲时的轮询间隔加抖动：N 个 worker 不会整齐地同时打数据库
            await _wait_or_timeout(stop_event, poll_interval * random.uniform(0.5, 1.5))
            continue
        stats["claimed"] += 1
        emit("claimed", job=job.id, fence=job.fence, attempts=job.attempts, kind=job.kind, tenant_id=job.tenant_id)
        task = asyncio.create_task(process(job), name=f"job-{job.id}")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        stats["max_in_flight"] = max(stats["max_in_flight"], len(tasks))

    if tasks:
        emit("draining", in_flight=len(tasks), grace_period=grace_period)
        _, pending = await asyncio.wait(set(tasks), timeout=grace_period)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        stats["cancelled"] = len(pending)
        if pending:
            emit("cancelled", count=len(pending))
    emit("stopped", stats=dict(stats))
    return stats


def stop_on_signals(stop_event: asyncio.Event, signals: Iterable[int] = (signal.SIGTERM, signal.SIGINT)) -> None:
    """把 SIGTERM / SIGINT 接到 stop_event 上（要在事件循环里调用）。K8s 删除 Pod 时发送的就是 SIGTERM。

    用 loop.add_signal_handler：信号到来时在事件循环里 set，而不是在任意字节码之间打断协程。
    """
    loop = asyncio.get_running_loop()
    for s in signals:
        loop.add_signal_handler(s, stop_event.set)


# =====================================================================================
# Agent 任务
# =====================================================================================


_CURRENT_JOB: ContextVar[Job | None] = ContextVar("agentkit_current_job", default=None)


def current_job() -> Job | None:
    """当前 asyncio 任务正在处理的队列任务（在 AgentJobHandler 里运行时可用）。"""
    return _CURRENT_JOB.get()


class LeaseGuard(Hook):
    """心跳发现租约丢了 → 在下一次模型 / 工具调用前停手，少做无用功（最终的安全仍然靠 fence 和 CAS）。

    一个 Agent 只装一个：当前是哪个任务从 ContextVar 里取。共享的 Agent 同时跑几十个任务时，
    每个任务是一个独立的 asyncio Task，各自看到自己的 job。
    """

    def _check(self) -> None:
        job = _CURRENT_JOB.get()
        if job is not None and job.lost.is_set():
            raise StopRun("lease_lost", "租约已丢失：任务已被别的 worker 接手，停止执行。")

    def before_llm(self, state, messages) -> None:
        self._check()

    def before_tool(self, state, call, tool) -> str | None:
        self._check()
        return None


class FencedCheckpointer(Protocol):
    """支持 fence 接管的检查点（SQLiteCheckpointer、PostgresCheckpointer）。"""

    def fenced(self, fence: int, writer: str | None = None) -> "FencedCheckpointer": ...

    async def load(self, run_id: str) -> RunState | None: ...

    async def save(self, state: RunState) -> None: ...


class AgentJobHandler:
    """把 Agent 的运行 / 恢复包装成队列任务：await run_worker(queue, AgentJobHandler(agent, checkpointer), ...)

    第一个参数两种写法：
      - **一个 Agent 实例（推荐）**：所有任务共用这一个 Agent（也就共用它的工具执行器、模型客户端的连接池），
        每个任务通过 run / resume / approve 的 checkpointer= 参数传入带本次 fence 的视图；
      - make_agent(checkpointer, job)：每个任务调用一次（可以是 async 函数），用来按任务定制 Agent
        （例如按租户选模型）。想共用线程池，就在工厂里给每个 Agent 传同一个 executor=ToolExecutor(...)。

    payload 格式：
        {"op": "run", "input": "...", "metadata": {...}, "run_id": "可选", "history": [多轮对话的前几轮，可选]}
        {"op": "resume", "run_id": "...", "approvals": {"call_id": true}, "by": "审批人", "comment": "可选"}

    - run_id 默认 f"job-{job.id}"：由任务决定而不是随机生成，接手的 worker 才能找到同一个检查点、算出同样的幂等键。
    - 已有检查点（前任崩溃 / 被取消 / 被接管 / 被限流推迟）→ resume，从断点继续，不从头再来。
    - metadata 里的 tenant_id 一律以 job.tenant_id 为准：payload 是调用方填的，不可信（第 09 课）。
    - 结果为 paused（等待审批）时任务**正常完成**，返回值里 awaiting_approval=True；审批后由 API 入队 resume 任务。
    - stop_reason 在 defer_stop_reasons 里（默认 rate_limited）→ 抛 RetryLater，过一会儿再来，不消耗重试次数；
      status == failed（模型彻底不可用）→ 抛异常，由队列退避重试。
    """

    def __init__(
        self,
        agent_or_factory: Any,
        checkpointer: FencedCheckpointer,
        *,
        defer_stop_reasons: Iterable[str] = ("rate_limited",),
        defer_seconds: float = 2.0,
    ):
        self.checkpointer = checkpointer
        self.defer_stop_reasons = set(defer_stop_reasons)
        self.defer_seconds = defer_seconds
        self.agents_created = 0  # 工厂被调用的次数（共用模式下是 0）
        if hasattr(agent_or_factory, "run") and hasattr(agent_or_factory, "resume") and hasattr(agent_or_factory, "hooks"):
            self._shared, self.make_agent = agent_or_factory, None
            self._adopt(agent_or_factory)
        else:
            self._shared, self.make_agent = None, agent_or_factory

    async def aclose(self) -> None:
        """worker 进程退出前调用（worker 命令行会自动调用）：释放共享 Agent 的线程池和模型连接、关闭检查点的连接。"""
        if self._shared is not None:
            await self._shared.aclose()
        close = getattr(self.checkpointer, "close", None)
        if close is not None:
            await maybe_await(close())

    @staticmethod
    def _adopt(agent) -> None:
        """一个 Agent 实例只装一个租约守卫。"""
        if not any(isinstance(h, LeaseGuard) for h in agent.hooks):
            agent.hooks.append(LeaseGuard())

    async def _agent_for(self, ckpt, job: Job):
        if self._shared is not None:
            return self._shared
        self.agents_created += 1
        agent = self.make_agent(ckpt, job)
        agent = (await agent) if inspect.isawaitable(agent) else agent
        self._adopt(agent)
        return agent

    @staticmethod
    def _op(job: Job) -> str:
        op = (job.payload or {}).get("op", "run")
        if op not in ("run", "resume"):
            raise PermanentJobError(f"未知的 op：{op!r}（只支持 run / resume）")
        return op

    @staticmethod
    def _check_tenant(state: RunState, job: Job) -> None:
        owner = state.metadata.get("tenant_id")
        if owner is not None and owner != job.tenant_id:
            raise PermanentJobError(f"run {state.run_id} 属于租户 {owner}，不能由租户 {job.tenant_id} 的任务操作")

    @staticmethod
    def _approval_step(state: RunState, payload: dict) -> tuple[dict, dict | None]:
        """返回 (approvals, 需要 approve() 的那次决定)。重试的 resume 任务不会重复写审批记录。"""
        approvals = {str(k): bool(v) for k, v in (payload.get("approvals") or {}).items()}
        pending = state.pending
        logged = pending is not None and any(a.get("call_id") == pending["id"] for a in state.approval_log)
        if pending and pending["id"] in approvals and not logged:
            return approvals, {"approved": approvals[pending["id"]], "by": payload.get("by"),
                               "comment": payload.get("comment", "")}
        return approvals, None

    async def __call__(self, job: Job) -> dict:
        op = self._op(job)
        # 每领取一次任务，就用这次领取的 fence 创建一个检查点视图：load 时"接管"这个 run，
        # 之后任何 fence 更旧的写入（诈尸的前任）都会被拒绝
        ckpt = self.checkpointer.fenced(job.fence, writer=job.worker_id)
        agent = await self._agent_for(ckpt, job)
        kw = {"checkpointer": ckpt}
        token = _CURRENT_JOB.set(job)
        try:
            p = job.payload
            if op == "run":
                run_id = p.get("run_id") or f"job-{job.id}"
                existing = await ckpt.load(run_id)
                if existing is not None:
                    self._check_tenant(existing, job)
                    result = await agent.resume(run_id, **kw)
                elif p.get("input") is None:
                    raise PermanentJobError("run 任务缺少 input")
                else:
                    metadata = {**(p.get("metadata") or {}), "tenant_id": job.tenant_id}
                    # history：多轮对话的前几轮（RunResult.history）。不带它，走队列的每一轮都会"失忆"
                    result = await agent.run(p["input"], history=p.get("history"), metadata=metadata, run_id=run_id, **kw)
            else:
                run_id = p.get("run_id")
                state = await ckpt.load(run_id) if run_id else None
                if state is None:
                    raise PermanentJobError(f"找不到 run {run_id!r} 的检查点")
                self._check_tenant(state, job)
                approvals, decision = self._approval_step(state, p)
                if decision is not None:  # approve() 会把"谁、何时、批没批"写进 approval_log，再继续运行
                    result = await agent.approve(run_id, decision["approved"], by=decision["by"],
                                                 comment=decision["comment"], **kw)
                else:
                    result = await agent.resume(run_id, approvals, **kw)
        finally:
            _CURRENT_JOB.reset(token)
        return self._outcome(result)

    def _outcome(self, result) -> dict:
        if result.status == "stopped" and result.stop_reason == "lease_lost":
            raise LeaseLost(f"run {result.run_id}：心跳发现租约已丢失，停止执行")
        if result.status == "stopped" and result.stop_reason in self.defer_stop_reasons:
            raise RetryLater(self.defer_seconds * random.uniform(1.0, 1.5), f"run {result.run_id} {result.stop_reason}")
        if result.status == "failed":
            raise RuntimeError(f"run {result.run_id} 失败：{result.stop_reason}")
        pending = None
        if result.pending_approval is not None:
            c = result.pending_approval
            pending = {"id": c.id, "name": c.name, "arguments": c.arguments}
        return {
            "run_id": result.run_id,
            "status": result.status,
            "output": result.output,
            "stop_reason": result.stop_reason,
            "awaiting_approval": result.status == "paused",
            "pending": pending,
            "steps": result.steps,
            "cost_usd": round(result.cost_usd, 6),
            "tools_called": result.tools_called(),
        }
