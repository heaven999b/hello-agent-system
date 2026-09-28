"""队列 worker：一个进程用 asyncio 同时处理 WORKER_CONCURRENCY 个任务。

骨架全部来自框架 agentkit.distributed（第 13 课讲原理，第 26 课换成 Postgres）：
    run_worker        领取（SKIP LOCKED）、背压、每个任务一个续租协程、fence、退避重试 / 死信、SIGTERM 后排空
    AgentJobHandler   用 job.fence 创建带 fence 的检查点视图，交给进程里唯一的 Agent 去 run / resume / approve；
                      paused → 任务正常完成（awaiting_approval）；rate_limited → RetryLater；failed → 退避重试
这个文件只补框架不管的四件事：
    1. 每个任务外面接上 API 那条 trace：continue_trace(payload["trace"]) + CONSUMER span（第 28 课）；
    2. 停机时被取消的任务**立刻归还**（框架默认不归还、等租约过期，见下面的时间线）；
    3. 把 run_worker 的任务事件变成 Redis Streams 里的进度事件 + Prometheus 计数 + 结构化日志（on_event）；
    4. /healthz、/readyz 由事件循环自己应答；排空期间 /readyz 返回 503。

为什么不直接用框架的 worker 命令行（python -m agentkit.distributed.worker --queue postgresql://... --app ...）？
它适合第 12、13 课那样"一个工厂函数返回 handler"的 worker，但这个服务需要的四个接口它没有开放：
    - on_event 固定为"打印一行 JSON"，而这里要在**队列提交之后**推送 completed 等进度事件
      （先推后提交的话，客户端收到 completed 时任务表里可能还是 leased）；
    - 停机信号的 stop_event 在命令行内部，工厂拿不到，/readyz 没法在排空时变成 503；
    - 配置来自命令行参数、没有心跳间隔参数，这个服务的配置全部来自环境变量（12-factor，K8s ConfigMap）；
    - open_queue 按 URL 自己建连接池、不能传池参数，这里队列、检查点、业务表共用 Runtime 的一个池。
所以这里直接调用 run_worker + AgentJobHandler（和命令行内部做的是同一件事），只多上面四件事。

SIGTERM（K8s 删除 Pod、滚动发布）时间线：
    t=0            stop_on_signals 置位 stop_event → /readyz 变 503 → 不再领取新任务（满载时也能立刻看到信号）
    0…grace        在途任务继续跑，心跳继续续租（所以租约不必长于宽限期）
    t=grace        还没做完的任务被取消：Agent 把检查点记为 cancelled（写工具保持未回答），
                   本 worker **立刻归还**任务（release，fence 校验）→ 别的 worker 马上接手，用同一个 call_id 重放
    之后           flush 追踪、关连接池，退出码 0 —— 全部要在 terminationGracePeriodSeconds 之内完成
kill -9 则什么都来不及做：任务留在 leased，租约过期后被 reap，别的 worker 从最后一次检查点接着跑。

fence（第 26 课）：每次领取任务，队列从**整张表共用的序列**里取一个新的 fence（nextval），全局单调递增。
所以同一个 run 后来的任务（审批后的 resume、用户点"继续"的 resume）的 fence 一定比之前任何一次领取都大，
可以直接接管检查点；被取代的旧持有者再写检查点会得到 CheckpointConflict，队列那一侧也会拒绝它的提交。

启动：python -m production.service.worker
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from agentkit import wait_for  # 取消安全的 wait_for（Python 3.12 之前的 asyncio.wait_for 会吞掉取消，gh-86296）
from agentkit.contrib.otel import continue_trace, start_metrics_server
from agentkit.distributed import AgentJobHandler, Job, LeaseLost, run_worker, stop_on_signals

from .config import Settings
from .runtime import Runtime
from .telemetry import JOB_EVENTS, JOB_RELEASED, log

# 这些事件之后，这次领取就结束了（提交、失败、推迟、被接手、提交出错）：从 inflight 里拿掉
SETTLED_EVENTS = frozenset({"completed", "failed", "deferred", "ownership_lost", "fence_rejected", "commit_error"})


class Worker:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.s = rt.settings
        self.worker_id = self.s.instance_id
        self.stop = asyncio.Event()
        self.started = time.time()
        # 整个进程共用**一个** Agent（它可以被并发复用）：模型客户端、工具执行器、Hook 都只有一份。
        # AgentJobHandler 每领到一个任务，就通过 run / resume / approve 的 checkpointer= 传入带本次 fence 的视图。
        self.agent = rt.new_agent()
        self.handler = AgentJobHandler(self.agent, rt.ckpt, defer_seconds=1.0)
        # run_worker 的事件里只有任务 id（这样事件能直接打成一行 JSON）；推送进度要 run_id，所以按 id 记下在途的任务
        self.inflight: dict[int, Job] = {}

    # ------------------------------------------------------------------ 每个任务

    async def handle(self, job: Job):
        self.inflight[job.id] = job
        run_id = (job.payload or {}).get("run_id")
        try:
            await self.rt.bus.publish(run_id, "claimed", worker=self.worker_id, attempt=job.attempts, fence=job.fence,
                                      op=job.payload.get("op"))
            async with continue_trace(job.payload.get("trace")):
                with self.rt.tracer.span("process agent_jobs", **{
                    "otel.kind": "consumer", "messaging.system": "postgresql", "messaging.destination.name": "agent_jobs",
                    "messaging.operation.type": "process", "messaging.message.id": str(job.id), "run_id": run_id,
                    "job.attempt": job.attempts, "job.fence": job.fence,
                }):
                    return await self.handler(job)
        except asyncio.CancelledError:
            # 停机宽限期到了还没做完：Agent 已经把检查点记为 cancelled。立刻归还任务，别让它空等一个租约。
            # 安全性不靠"时机"：归还带 fence 校验；就算检查点的最后一次写入还在路上，接手者的 fence 接管会让它作废。
            self.inflight.pop(job.id, None)  # 被取消的任务不会再有 run_worker 的事件
            if self.s.release_on_cancel:
                await asyncio.shield(self._release(job))
            raise

    async def _release(self, job: Job) -> None:
        try:
            await wait_for(self.rt.queue.release(job, reason=f"worker {self.worker_id} shutting down"), 3)
            JOB_RELEASED.inc()
            await self.rt.bus.publish(job.payload.get("run_id"), "released", worker=self.worker_id, reason="shutdown")
        except (LeaseLost, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 —— 归还失败就等租约过期，结果一样正确，只是慢
            log("release_failed", job_id=job.id, error=f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ run_worker 的事件回调（同步函数）

    def on_event(self, name: str, info: dict) -> None:
        JOB_EVENTS.labels(name).inc()
        job_id = info.get("job")
        job: Job | None = None
        if job_id is not None:
            job = self.inflight.pop(job_id, None) if name in SETTLED_EVENTS else self.inflight.get(job_id)
        fields = {k: v for k, v in info.items() if k not in ("job", "result")}
        if job_id is not None:
            fields["job_id"] = job_id
        if job is not None:
            fields.update(run_id=job.payload.get("run_id"), attempt=job.attempts, fence=job.fence)
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
            stats = await run_worker(
                self.rt.queue, self.handle,
                worker_id=self.worker_id, stop_event=self.stop, concurrency=self.s.worker_concurrency,
                lease_seconds=self.s.lease_seconds, heartbeat_interval=self.s.heartbeat_interval,
                poll_interval=self.s.poll_seconds, grace_period=self.s.worker_grace_seconds,
                kinds=["agent"], on_event=self.on_event,
            )
        finally:
            if health is not None:
                health.close()
        # inflight_left 应当为 0：每个领取过的任务要么有了结果事件，要么在停机时被取消（e2e 测试据此检查没有泄漏）
        log("worker_stopped", worker=self.worker_id, stats=stats, inflight_left=len(self.inflight))
        return stats


async def amain() -> int:
    settings = Settings.from_env(role="worker")
    if settings.metrics_port:
        start_metrics_server(settings.metrics_port, addr=settings.metrics_addr)
    rt = await Runtime.create(settings)
    try:
        # Runtime 持有所有要关闭的资源（连接池、Redis、线程池、模型客户端）；Agent 和 AgentJobHandler 只是借用，
        # 所以这里不调 handler.aclose()（worker 命令行会调它，因为那里 Agent 归 handler 所有）
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
