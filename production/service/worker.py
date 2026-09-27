"""队列 worker：一个进程用 asyncio 同时处理 WORKER_CONCURRENCY 个任务（第 26 课 run_async_worker + AgentJobHandler）。

每领到一个任务：
    1. continue_trace(payload["trace"])：接着 API 那条 trace（第 28 课），外面套一个 CONSUMER span；
    2. AgentJobHandler 用 job.fence 创建带 fence 的检查点视图，交给进程里唯一的 AsyncAgent（run / resume 的 checkpointer=）；
       幂等存储是 Redis，模型调用前走 Redis 令牌桶（AsyncRateLimitHook），OTelTracer / PrometheusHook 照常；
    3. run / resume / approve；paused → 任务正常完成（awaiting_approval）；rate_limited → RetryLater；failed → 退避重试。

SIGTERM（K8s 删除 Pod、滚动发布）时间线：
    t=0            stop_on_signals 置位 stop_event → /readyz 变 503 → 不再领取新任务
    0…grace        在途任务继续跑，心跳继续续租（所以租约不必长于宽限期）
    t=grace        还没做完的任务被取消：AsyncAgent 把检查点记为 cancelled（写工具保持未回答），
                   本 worker **立刻归还**任务（release，fence 校验）→ 别的 worker 马上接手，用同一个 call_id 重放
    之后           flush 追踪、关连接池，退出码 0 —— 全部要在 terminationGracePeriodSeconds 之内完成
kill -9 则什么都来不及做：任务留在 leased，租约过期后被 reap，别的 worker 从最后一次检查点接着跑。

fence 的作用域（本课压测发现的问题，见讲义 7.2）：队列的 fence 是**每个任务**各自从 1 数起的，而检查点的 fence
保护的是**一个 run**。同一个 run 会先后有好几个任务（run → 审批后的 resume → 用户点"继续"的 resume）。
run 任务如果被接手过（fence=2），之后的 resume 任务从 fence=1 开始 → 被检查点当成"旧持有者"拒绝，
只能等租约过期、重新领取把 fence 数到 2 才能继续（接手次数多于 max_attempts 时直接进死信）。
这里用 RunScopedFences 把检查点 fence 换算成 job_id × 10^6 + job.fence：同一个 run 上，后入队的任务永远比先入队的大，
同一个任务里，后领取的永远比先领取的大 —— fence 在"run"这个作用域里单调递增。

启动：python -m production.service.worker
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextvars import ContextVar

from agentkit.aio import wait_for  # 取消安全的 wait_for（Python 3.12 之前的 asyncio.wait_for 会吞掉取消，gh-86296）
from agentkit.contrib.otel import continue_trace, start_metrics_server
from agentkit.contrib.postgres import (
    AgentJobHandler,
    AsyncPostgresCheckpointer,
    CheckpointConflict,
    Job,
    LeaseLost,
    PermanentJobError,
    run_async_worker,
    stop_on_signals,
)

from .config import Settings
from .runtime import RUNS_TABLE, Runtime
from .telemetry import JOB_EVENTS, JOB_RELEASED, log

CURRENT_JOB: ContextVar[Job | None] = ContextVar("itdesk_current_job", default=None)
FENCE_STRIDE = 1_000_000  # 单个任务被重新领取的次数远小于这个数（max_attempts=5，redrive 也不会到 10^6）


class RunScopedFences(AsyncPostgresCheckpointer):
    """把"每个任务各自的 fence"换算成"在同一个 run 上单调递增的 fence"：job_id × 10^6 + job.fence。

    AgentJobHandler 调用 fenced(job.fence) 创建本次领取的检查点视图；当前任务从 ContextVar 取（每个任务一个 asyncio Task）。
    bigint 装得下：job_id 到 9×10^12 才会溢出。
    """

    def fenced(self, fence: int, writer: str | None = None) -> AsyncPostgresCheckpointer:
        job = CURRENT_JOB.get()
        scoped = job.id * FENCE_STRIDE + int(fence) if job is not None else int(fence)
        return AsyncPostgresCheckpointer("", self.table, fence=scoped, writer=writer or self.writer, _db=self._db)


class Worker:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s = rt.settings
        self.worker_id = self.s.instance_id
        self.stop = asyncio.Event()
        self.started = time.time()
        # 整个进程共用**一个** AsyncAgent（它可以被并发复用）：模型客户端、工具执行器、Hook 都只有一份。
        # AgentJobHandler 每领到一个任务，就通过 run / resume / approve 的 checkpointer= 传入带本次 fence 的视图。
        self.agent = rt.new_agent()
        self.handler = AgentJobHandler(self.agent, RunScopedFences(rt.pool, RUNS_TABLE), defer_seconds=1.0)

    # ------------------------------------------------------------------ 每个任务

    async def handle(self, job: Job):
        run_id = (job.payload or {}).get("run_id")
        await self.rt.bus.publish(run_id, "claimed", worker=self.worker_id, attempt=job.attempts, fence=job.fence,
                                  op=job.payload.get("op"))
        async with continue_trace(job.payload.get("trace")):
            with self.rt.tracer.span("process agent_jobs", **{
                "otel.kind": "consumer", "messaging.system": "postgresql", "messaging.destination.name": "agent_jobs",
                "messaging.operation.type": "process", "messaging.message.id": str(job.id), "run_id": run_id,
                "job.attempt": job.attempts, "job.fence": job.fence,
            }):
                token = CURRENT_JOB.set(job)
                try:
                    return await self.handler(job)
                except CheckpointConflict as e:
                    # 这个任务还持有租约（否则队列那一侧会拒绝），但 run 已经被一个**更新的任务**接管了：
                    # 它永远不会成功，明确地结束它（failed，不重试），而不是一轮轮等租约过期、最后进死信。
                    raise PermanentJobError(f"superseded: run {run_id} 已被更新的任务接管（{e}）") from e
                except asyncio.CancelledError:
                    # 停机宽限期到了还没做完：AsyncAgent 已经把检查点记为 cancelled。立刻归还任务，别让它空等一个租约。
                    # 安全性不靠"时机"：归还带 fence 校验；就算检查点的最后一次写入还在路上，接手者的 fence 接管会让它作废。
                    if self.s.release_on_cancel:
                        await asyncio.shield(self._release(job))
                    raise
                finally:
                    CURRENT_JOB.reset(token)

    async def _release(self, job: Job) -> None:
        try:
            await wait_for(self.rt.queue.release(job, reason=f"worker {self.worker_id} shutting down"), 3)
            JOB_RELEASED.inc()
            await self.rt.bus.publish(job.payload.get("run_id"), "released", worker=self.worker_id, reason="shutdown")
        except (LeaseLost, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 —— 归还失败就等租约过期，结果一样正确，只是慢
            log("release_failed", job_id=job.id, error=f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ run_async_worker 的事件回调（同步函数）

    def on_event(self, name: str, info: dict) -> None:
        JOB_EVENTS.labels(name).inc()
        job: Job | None = info.get("job")
        fields = {k: v for k, v in info.items() if k not in ("job", "result")}
        if job is not None:
            fields.update(job_id=job.id, run_id=job.payload.get("run_id"), attempt=job.attempts, fence=job.fence)
        log(f"job_{name}", **fields)
        if job is None:
            return
        run_id = job.payload.get("run_id")
        publish = self.rt.bus.publish
        if name == "completed":
            result = info.get("result") or {}
            if result.get("awaiting_approval"):
                pending = result.get("pending") or {}
                self.rt.tasks.spawn(publish(run_id, "awaiting_approval", tool=pending.get("name"), call_id=pending.get("id")))
            elif result.get("status") == "completed":
                self.rt.tasks.spawn(publish(run_id, "completed", status="completed", output=result.get("output"),
                                            worker=self.worker_id, attempt=job.attempts))
            else:  # max_steps / stopped（预算、护栏拦截）：也是终态，但不是成功
                self.rt.tasks.spawn(publish(run_id, "failed", status=result.get("status"), stop_reason=result.get("stop_reason"),
                                            output=result.get("output")))
        elif name == "failed":
            status = info.get("status")
            event = "retrying" if status == "queued" else "failed"
            if "superseded" in str(info.get("error")):
                event = "superseded"  # 不是运行失败：run 由更新的任务继续，SSE 客户端不应该在这里结束
            self.rt.tasks.spawn(publish(run_id, event, job_status=status, error=str(info.get("error"))[:300]))
        elif name in ("deferred", "ownership_lost", "fence_rejected"):
            self.rt.tasks.spawn(publish(run_id, name, worker=self.worker_id))

    # ------------------------------------------------------------------ 健康检查（由事件循环自己应答）

    async def serve_health(self) -> asyncio.AbstractServer:
        """/healthz：事件循环还能在 1 秒内应答（被同步代码卡死时应答不了 → liveness 失败 → 重启）。
        /readyz：没有在停机、依赖可用。

        注意 /metrics 由 prometheus_client 在**另一个线程**里提供：事件循环卡死时它照样能返回 200，
        所以它不能当 liveness 用。"""
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await wait_for(reader.readline(), 2)
                path = line.split()[1].decode() if len(line.split()) > 1 else "/"
                if path.startswith("/readyz"):
                    deps = await self.rt.check_dependencies()
                    ok = not self.stop.is_set() and all(v == "ok" for v in deps.values())
                    body = {"status": "ready" if ok else ("draining" if self.stop.is_set() else "not_ready"), "dependencies": deps}
                else:
                    ok, body = True, {"status": "ok", "worker": self.worker_id, "uptime_s": round(time.time() - self.started, 1)}
                payload = json.dumps(body).encode()
                status = b"200 OK" if ok else b"503 Service Unavailable"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                writer.close()

        return await asyncio.start_server(handle, self.s.health_addr, self.s.health_port)

    # ------------------------------------------------------------------ 主循环

    async def run(self) -> dict:
        stop_on_signals(self.stop)
        health = await self.serve_health() if self.s.health_port else None
        log("worker_started", worker=self.worker_id, concurrency=self.s.worker_concurrency, lease_s=self.s.lease_seconds,
            grace_s=self.s.worker_grace_seconds, llm=self.s.llm_backend)
        try:
            stats = await run_async_worker(
                self.rt.queue, self.handle,
                worker_id=self.worker_id, stop_event=self.stop, concurrency=self.s.worker_concurrency,
                lease_seconds=self.s.lease_seconds, heartbeat_interval=self.s.heartbeat_interval,
                poll_interval=self.s.poll_seconds, grace_period=self.s.worker_grace_seconds,
                kinds=["agent"], on_event=self.on_event,
            )
        finally:
            if health is not None:
                health.close()
        log("worker_stopped", worker=self.worker_id, stats=stats)
        return stats


async def amain() -> int:
    settings = Settings.from_env(role="worker")
    if settings.metrics_port:
        start_metrics_server(settings.metrics_port, addr=settings.metrics_addr)
    rt = await Runtime.create(settings)
    try:
        await Worker(rt).run()
    finally:
        await rt.aclose()
    return 0


def main() -> None:
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:  # pragma: no cover
        code = 130
    sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    main()
