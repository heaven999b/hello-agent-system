"""API 服务（FastAPI）。一个进程一个事件循环；横向扩展靠副本数（K8s Deployment + HPA），不靠进程内多 worker。

两种交互模式（讲义问题卡片 1、5 讲何时用哪个）：

    交互式  POST /v1/chat/stream        本进程直接跑 AsyncAgent，SSE 边生成边推；客户端断开 → 运行取消、检查点记 cancelled
    后台    POST /v1/runs               入队（Postgres SKIP LOCKED 队列），立刻返回 run_id；worker 执行
            GET  /v1/runs/{id}          状态：检查点（Postgres）+ 任务表
            GET  /v1/runs/{id}/events   SSE 进度：Redis Streams，支持 Last-Event-ID 断线续传
            POST /v1/runs/{id}/approval 记录审批人，入队 resume 任务；幂等键 approve:{run_id}:{call_id}，连点只入队一次
            POST /v1/runs/{id}/resume   把被取消 / 被限流中止的运行交给 worker 继续（同一个 call_id 重放，副作用不重复）
            GET  /v1/approvals          审批收件箱（it_admin）

运维端点：/healthz（存活：只看本进程）、/readyz（就绪：Postgres + Redis）、/metrics（Prometheus）。

启动：uvicorn --factory production.service.api:create_app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 20
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from agentkit.aio import ApprovalRequired, KeyedLimiter, LimitExceeded, RunFinished, RunStarted, TextDelta, ToolFinished, ToolStarted
from agentkit.contrib.otel import continue_trace, inject_context

from . import telemetry
from .config import Settings
from .events import FINAL_EVENTS, PAUSE_EVENTS
from .identity import KeyRing, Principal
from .runtime import Runtime
from .telemetry import HTTP_LATENCY, HTTP_REQUESTS, RATE_LIMIT_ERRORS, RATE_LIMITED, SSE_STREAMS, TTFT, log

STREAM_ROUTES = {"/v1/chat/stream", "/v1/runs/{run_id}/events"}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(None, max_length=100)


class RunIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    priority: int = Field(0, ge=0, le=9)


class ApprovalIn(BaseModel):
    approved: bool = True
    comment: str = Field("", max_length=500)


# ------------------------------------------------------------------ 中间件：按路由模板计数（不能按原始路径：run_id 会让时间序列爆炸）

class MetricsMiddleware:
    """纯 ASGI 中间件。不用 @app.middleware("http")（BaseHTTPMiddleware）：它会改变流式响应和断开连接时的取消行为。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        code = {"v": 500}
        t0 = time.perf_counter()

        async def _send(message):
            if message["type"] == "http.response.start":
                code["v"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(route, str(code["v"])).inc()
            if route not in STREAM_ROUTES:
                HTTP_LATENCY.labels(route).observe(time.perf_counter() - t0)


# ------------------------------------------------------------------ 依赖：身份、限流、舱壁

def rt_of(request: Request) -> Runtime:
    return request.app.state.rt


async def principal(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key")
    p = request.app.state.keys.resolve(key)
    if p is None:
        raise HTTPException(401, "API key 无效或缺失", headers={"WWW-Authenticate": "Bearer"})
    return p


async def rate_limited(request: Request, p: Principal = Depends(principal)) -> Principal:
    """按租户的 Redis 令牌桶（所有 API 副本共享一个桶）。超限 → 429 + Retry-After。

    Redis 不可用时 **fail open**（放行并计数）：限流器是保护手段，不应该让整个 API 跟着 Redis 一起挂；
    进程内舱壁（KeyedLimiter）和网关配额（第 29 课）仍然在。合同额度这类"必须 fail closed"的限额放在网关。
    """
    rt = rt_of(request)
    try:
        ok, wait, _ = await rt.api_bucket.take(p.tenant_id)
    except Exception as e:  # noqa: BLE001
        RATE_LIMIT_ERRORS.inc()
        log("rate_limiter_error", error=f"{type(e).__name__}: {e}")
        return p
    if not ok:
        RATE_LIMITED.labels("api_bucket").inc()
        retry = max(1, math.ceil(wait)) if wait >= 0 else 60
        raise HTTPException(429, f"租户 {p.tenant_id} 请求过于频繁，请 {retry} 秒后重试", headers={"Retry-After": str(retry)})
    return p


class StreamGuard:
    """一条交互式流的"请求作用域"：持有舱壁槽位，登记这条流背后的运行任务，请求结束时一并收尾。"""

    def __init__(self, principal: Principal):
        self.principal = principal
        self.tasks: list[tuple[str, asyncio.Task]] = []

    def track(self, run_id: str, task: asyncio.Task) -> asyncio.Task:
        self.tasks.append((run_id, task))
        return task


async def stream_slot(request: Request, p: Principal = Depends(rate_limited)) -> AsyncIterator[StreamGuard]:
    """交互式流的舱壁：每个租户在**本进程**里同时打开的流不超过 STREAMS_PER_TENANT。

    这是 yield 依赖，并且注册时用 scope="request"：退出代码在**响应发送完之后**才执行（包括客户端断开），
    所以：1) 槽位一直占到 SSE 结束；2) 在这里**确定性地**取消这条流背后的运行。
    为什么不只靠生成器的 finally？FastAPI 在一个单独的 producer 任务里迭代 SSE 生成器；断开时如果 producer 恰好
    停在"把事件交给响应"那一步（而不是生成器内部），取消落在生成器外面，生成器停在 yield 上，
    它的 finally 要等生成器被回收才执行（引用计数归零时很快，卷进引用环就要等垃圾回收）。
    请求作用域的收尾不依赖这个时机（纵深防御）。
    拿不到槽位 → 429，而不是无限排队。
    """
    limiter: KeyedLimiter = request.app.state.limiter
    rt = rt_of(request)
    slot = limiter.slot(p.tenant_id, timeout=rt.settings.stream_admission_timeout_s)
    try:
        await slot.__aenter__()
    except LimitExceeded:
        RATE_LIMITED.labels("stream_bulkhead").inc()
        raise HTTPException(429, f"租户 {p.tenant_id} 同时打开的对话流太多，请稍后重试", headers={"Retry-After": "1"}) from None
    SSE_STREAMS.labels("chat").inc()
    guard = StreamGuard(p)
    try:
        yield guard
    finally:
        for run_id, task in guard.tasks:
            if not task.done():
                rt_of(request).fence.abandon(run_id)  # 兜底：取消万一被第三方库吞掉，下一个步骤边界再停（runtime.CancellationFence）
                task.cancel()  # 客户端断开 / 响应结束：运行还没完就取消它（AsyncAgent 会把检查点记为 cancelled）
        SSE_STREAMS.labels("chat").dec()
        await slot.__aexit__(None, None, None)  # 释放只做同步操作，取消过程中也能完成


# ------------------------------------------------------------------ 运行登记（所有权）

async def register_run(rt: Runtime, run_id: str, p: Principal, mode: str, idem_key: str | None) -> tuple[str, bool]:
    """返回 (run_id, 是否重复请求)。同一租户 + 同一个 Idempotency-Key 只登记一次。"""
    async with rt.pool.connection() as c:
        row = await (await c.execute(
            "INSERT INTO service_runs (run_id, tenant_id, user_id, mode, idempotency_key) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING run_id",
            (run_id, p.tenant_id, p.user_id, mode, idem_key),
        )).fetchone()
        if row:
            return row[0], False
        row = await (await c.execute(
            "SELECT run_id FROM service_runs WHERE tenant_id = %s AND idempotency_key = %s", (p.tenant_id, idem_key)
        )).fetchone()
        return row[0], True


async def owned_run(rt: Runtime, run_id: str, p: Principal) -> dict:
    """租户隔离：别的租户的 run 一律 404（不是 403 —— 连"存在"这件事都不泄露）。
    同租户内：本人或 it_admin（审批人要看待审批的运行）可见。"""
    async with rt.pool.connection() as c:
        row = await (await c.execute(
            "SELECT run_id, tenant_id, user_id, mode, job_id, created_at FROM service_runs WHERE run_id = %s AND tenant_id = %s",
            (run_id, p.tenant_id),
        )).fetchone()
    if row is None or (row[2] != p.user_id and not p.is_approver):
        raise HTTPException(404, "run 不存在")
    return {"run_id": row[0], "tenant_id": row[1], "user_id": row[2], "mode": row[3], "job_id": row[4], "created_at": row[5]}


async def visible_run(run_id: str, request: Request, p: Principal = Depends(principal)) -> dict:
    """依赖版的所有权检查。SSE 端点**必须**在依赖里鉴权：生成器函数体是在响应头（200）已经发出之后才执行的，
    在生成器里抛 HTTPException，客户端收到的是 200 + 一条空流（本课端到端测试抓到的真实问题）。"""
    return await owned_run(rt_of(request), run_id, p)


def new_run_id() -> str:
    return "r-" + uuid.uuid4().hex[:20]


# ------------------------------------------------------------------ 应用

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env(role="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = await Runtime.create(settings)
        app.state.rt = rt
        app.state.keys = KeyRing(settings.api_keys)
        app.state.limiter = KeyedLimiter(settings.streams_per_tenant, global_limit=settings.streams_global)
        # 交互式运行共用一个 AsyncAgent（它可以被并发复用，第 30 课 2.1）；每个运行传自己的检查点视图
        app.state.agent = rt.new_agent()
        sampler = asyncio.create_task(_sample_backlog(rt))
        log("api_started", instance=settings.instance_id, keys=len(app.state.keys), llm=settings.llm_backend)
        try:
            yield
        finally:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
            await rt.aclose()
            log("api_stopped", instance=settings.instance_id)

    app = FastAPI(title="IT 服务台 Agent 参考服务", version="1.0", lifespan=lifespan)
    app.add_middleware(MetricsMiddleware)

    # ---------------------------------------------------------------- 运维端点

    @app.get("/healthz")
    async def healthz():
        """存活：进程和事件循环还能应答就行。**不要**在这里检查数据库：
        数据库一抖，所有 Pod 的 liveness 一起失败、被一起重启，小故障变成全站故障（级联重启）。"""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        """就绪：依赖都可用才接流量。失败时 K8s 把 Pod 从 Service 里摘掉，但不会重启它。"""
        deps = await rt_of(request).check_dependencies()
        ok = all(v == "ok" for v in deps.values())
        return JSONResponse({"status": "ready" if ok else "not_ready", "dependencies": deps}, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics():
        return Response(telemetry.render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")

    # ---------------------------------------------------------------- 交互式：SSE

    @app.post("/v1/chat/stream", response_class=EventSourceResponse)
    async def chat_stream(body: ChatIn, request: Request, guard: StreamGuard = Depends(stream_slot, scope="request")):
        rt, p = rt_of(request), guard.principal
        run_id, _ = await register_run(rt, new_run_id(), p, "interactive", None)
        carrier = {k: v for k, v in request.headers.items() if k in ("traceparent", "tracestate")}
        metadata = {**p.metadata(), **({"conversation_id": body.conversation_id} if body.conversation_id else {})}
        yield _sse("accepted", f"{run_id}:0", {"run_id": run_id})
        # Agent 在单独的 task 里运行（_relay）：它的 trace 上下文、span 都在那个 task 里进入和退出。
        # 客户端断开时由 stream_slot 的收尾代码取消它（确定性），生成器的 finally 只是第二道保险。
        queue: asyncio.Queue = asyncio.Queue()
        view = rt.ckpt.fenced(0, writer=f"api:{settings.instance_id}")  # 本次运行专用的检查点视图（版本号记录随运行结束回收）
        task = guard.track(run_id, asyncio.ensure_future(
            _relay(request.app.state.agent, rt, body.message, run_id, metadata, view, carrier, queue)))
        seq, t0, first_text = 0, time.perf_counter(), True
        try:
            while True:
                event = await _next_event(queue, task)
                if event is None:
                    break
                seq += 1
                if isinstance(event, TextDelta) and first_text:
                    first_text = False
                    TTFT.observe(time.perf_counter() - t0)
                yield _encode(event, f"{run_id}:{seq}")
                if isinstance(event, RunFinished):
                    await _publish_finish(rt, run_id, event.result)
        except Exception as e:  # noqa: BLE001 —— 运行本身出错（如检查点冲突）：告诉客户端，别让连接无声断掉
            log("chat_stream_error", run_id=run_id, error=f"{type(e).__name__}: {e}")
            yield _sse("error", f"{run_id}:{seq + 1}", {"run_id": run_id, "error": type(e).__name__})
        finally:
            if not task.done():
                task.cancel()

    # ---------------------------------------------------------------- 后台任务

    @app.post("/v1/runs", status_code=202)
    async def create_run(body: RunIn, request: Request, p: Principal = Depends(rate_limited),
                         idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=200)):
        rt = rt_of(request)
        run_id, duplicate = await register_run(rt, new_run_id(), p, "background", idempotency_key)
        # 生产者 span + 把 traceparent 放进 payload：worker 用 continue_trace 接着同一条 trace（第 28 课问题 6 方案 B）
        async with continue_trace({k: v for k, v in request.headers.items() if k in ("traceparent", "tracestate")}):
            with rt.tracer.span("send agent_jobs", **{"otel.kind": "producer", "messaging.system": "postgresql",
                                                       "messaging.destination.name": "agent_jobs",
                                                       "messaging.operation.type": "send", "run_id": run_id}):
                payload = {"op": "run", "run_id": run_id, "input": body.message, "metadata": p.metadata(),
                           "trace": inject_context({})}
                # 幂等键 run:{run_id}：API 在"登记"和"入队"之间崩溃时，客户端带同一个 Idempotency-Key 重试会补上入队
                job_id = await rt.queue.enqueue("agent", payload, tenant_id=p.tenant_id, idempotency_key=f"run:{run_id}",
                                                priority=body.priority)
        async with rt.pool.connection() as c:
            await c.execute("UPDATE service_runs SET job_id = %s WHERE run_id = %s AND job_id IS NULL", (job_id, run_id))
        if not duplicate:
            await rt.bus.publish(run_id, "accepted", job_id=job_id)
        return {"run_id": run_id, "job_id": job_id, "status": "queued", "duplicate": duplicate,
                "links": {"self": f"/v1/runs/{run_id}", "events": f"/v1/runs/{run_id}/events"}}

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request, p: Principal = Depends(principal)):
        rt = rt_of(request)
        reg = await owned_run(rt, run_id, p)
        row = await rt.ckpt.get_run(run_id)  # 只读：不接管、不记版本号
        job = await rt.queue.get(reg["job_id"]) if reg["job_id"] else None
        state = row["state"] if row else {}
        return {
            "run_id": run_id, "mode": reg["mode"], "user_id": reg["user_id"],
            "status": row["status"] if row else ("queued" if job else "created"),
            "stop_reason": row.get("stop_reason") if row else None,
            "output": row.get("output") if row else None,
            "pending": row.get("pending") if row else None,
            "version": row.get("version") if row else None,
            "writer": row.get("writer") if row else None,
            "approval_log": state.get("approval_log", []),
            "job": None if job is None else {"id": job.id, "status": job.status, "attempts": job.attempts, "fence": job.fence,
                                            "worker_id": job.worker_id, "last_error": job.last_error},
        }

    @app.get("/v1/runs/{run_id}/events", response_class=EventSourceResponse)
    async def run_events(run_id: str, request: Request, reg: dict = Depends(visible_run),
                         last_event_id: str | None = Header(None, alias="Last-Event-ID")):
        rt = rt_of(request)
        after = last_event_id or "0-0"
        deadline = time.monotonic() + rt.settings.sse_max_seconds
        SSE_STREAMS.labels("events").inc()
        try:
            while time.monotonic() < deadline:
                block = int(min(5.0, max(0.05, deadline - time.monotonic())) * 1000)
                for entry_id, name, data in await rt.bus.read(run_id, after, block_ms=block):
                    after = entry_id
                    yield _sse(name, entry_id, data)
                    if name in FINAL_EVENTS or name in PAUSE_EVENTS:
                        return  # 终态，或者需要人来做下一步：结束这条流；客户端带 Last-Event-ID 重连可以接着收
            # 到了单条连接的最长时长：主动结束，告诉浏览器 1 秒后重连（带 Last-Event-ID，不丢事件）
            yield ServerSentEvent(comment="max stream duration reached, reconnect with Last-Event-ID", retry=1000)
        finally:
            SSE_STREAMS.labels("events").dec()

    @app.post("/v1/runs/{run_id}/approval", status_code=202)
    async def approve(run_id: str, body: ApprovalIn, request: Request, p: Principal = Depends(rate_limited)):
        rt = rt_of(request)
        if not p.is_approver:
            raise HTTPException(403, "只有 IT 管理员可以审批")
        reg = await owned_run(rt, run_id, p)
        if reg["user_id"] == p.user_id:
            raise HTTPException(403, "不能审批自己发起的操作（职责分离）")
        row = await rt.ckpt.get_run(run_id)
        pending = (row or {}).get("pending")
        if row is None or row["status"] != "paused" or not pending:
            raise HTTPException(409, f"run 当前状态为 {row['status'] if row else '未开始'}，没有等待审批的操作")
        call_id = pending["id"]
        request_id = uuid.uuid4().hex
        with rt.tracer.span("send agent_jobs", **{"otel.kind": "producer", "messaging.destination.name": "agent_jobs",
                                                   "messaging.operation.type": "send", "run_id": run_id}):
            payload = {"op": "resume", "run_id": run_id, "approvals": {call_id: body.approved}, "by": p.user_id,
                       "comment": body.comment, "request_id": request_id, "trace": inject_context({})}
            # 同一个待审批调用只入队一次：两个审批人同时点、同一个人连点两次，都只会有一个 resume 任务
            job_id = await rt.queue.enqueue("agent", payload, tenant_id=p.tenant_id, idempotency_key=f"approve:{run_id}:{call_id}")
        job = await rt.queue.get(job_id)
        duplicate = job.payload.get("request_id") != request_id
        if not duplicate:
            await rt.bus.publish(run_id, "approval_recorded", by=p.user_id, approved=body.approved, call_id=call_id, job_id=job_id)
        return {"run_id": run_id, "job_id": job_id, "duplicate": duplicate,
                "decision": {"approved": job.payload["approvals"][call_id], "by": job.payload["by"]}}

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, request: Request, p: Principal = Depends(rate_limited)):
        rt = rt_of(request)
        await owned_run(rt, run_id, p)
        row = await rt.ckpt.get_run(run_id)
        if row is None or row["status"] not in ("cancelled", "stopped"):
            raise HTTPException(409, f"run 当前状态为 {row['status'] if row else '未开始'}，不需要恢复")
        payload = {"op": "resume", "run_id": run_id, "approvals": {}, "trace": inject_context({})}
        job_id = await rt.queue.enqueue("agent", payload, tenant_id=p.tenant_id, idempotency_key=f"resume:{run_id}:{row['version']}")
        await rt.bus.publish(run_id, "resume_requested", job_id=job_id)
        return {"run_id": run_id, "job_id": job_id}

    @app.get("/v1/approvals")
    async def approvals(request: Request, p: Principal = Depends(principal)):
        if not p.is_approver:
            raise HTTPException(403, "只有 IT 管理员可以查看审批收件箱")
        rows = await rt_of(request).ckpt.list_runs(status="paused", tenant_id=p.tenant_id, limit=50)
        return [{"run_id": r["run_id"], "user_id": r["user_id"], "pending": r["pending"], "since": r["updated_at"]} for r in rows]

    return app


# ------------------------------------------------------------------ 辅助

def _sse(event: str, event_id: str, data: dict) -> ServerSentEvent:
    return ServerSentEvent(event=event, id=event_id, raw_data=json.dumps(data, ensure_ascii=False, default=str))


def _encode(ev, event_id: str) -> ServerSentEvent:
    if isinstance(ev, RunStarted):
        return _sse("run_started", event_id, {"run_id": ev.run_id})
    if isinstance(ev, TextDelta):
        return _sse("delta", event_id, {"text": ev.text})
    if isinstance(ev, ToolStarted):
        return _sse("tool_started", event_id, {"tool": ev.call.name})
    if isinstance(ev, ToolFinished):
        return _sse("tool_finished", event_id, {"tool": ev.call.name, "ok": ev.result.ok})
    if isinstance(ev, ApprovalRequired):
        return _sse("approval_required", event_id, {"tool": ev.call.name, "message": ev.message})
    r = ev.result
    return _sse("done", event_id, {"status": r.status, "stop_reason": r.stop_reason, "output": r.output, "run_id": r.run_id})


async def _relay(agent, rt: Runtime, text: str, run_id: str, metadata: dict, view, carrier: dict, queue: asyncio.Queue) -> None:
    async with continue_trace(carrier):
        with rt.tracer.span("POST /v1/chat/stream", **{"otel.kind": "server", "run_id": run_id}):
            async with contextlib.aclosing(agent.stream(text, run_id=run_id, metadata=metadata, checkpointer=view)) as events:
                async for event in events:
                    queue.put_nowait(event)
    queue.put_nowait(None)


async def _next_event(queue: asyncio.Queue, task: asyncio.Task):
    """等下一个事件；运行任务异常结束时把异常抛出来。"""
    if not queue.empty():
        return queue.get_nowait()
    getter = asyncio.ensure_future(queue.get())
    try:
        done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not getter.done():
            getter.cancel()
    if getter in done:
        return getter.result()
    if not queue.empty():
        return queue.get_nowait()
    task.result()  # 抛出运行任务的异常
    return None


async def _publish_finish(rt: Runtime, run_id: str, result) -> None:
    if result.status == "paused" and result.pending_approval is not None:
        c = result.pending_approval
        await rt.bus.publish(run_id, "awaiting_approval", tool=c.name, call_id=c.id)
    elif result.status in ("completed", "failed", "max_steps") or (result.status == "stopped" and result.stop_reason != "rate_limited"):
        await rt.bus.publish(run_id, "completed" if result.status == "completed" else "failed",
                             status=result.status, stop_reason=result.stop_reason, output=result.output)


async def _sample_backlog(rt: Runtime, every: float = 2.0) -> None:
    """定时把队列积压和待审批数写进指标（多个 API 副本都采样时，查询里用 max() 聚合，而不是 sum()）。"""
    while True:
        try:
            stats = await rt.queue.stats()
            rt.prom.set_queue_stats("agent_jobs", stats["ready"], stats["oldest_queued_age_s"] or 0.0)
            async with rt.pool.connection() as c:
                n = (await (await c.execute("SELECT count(*) FROM agent_runs WHERE status = 'paused'")).fetchone())[0]
            rt.prom.set_pending_approvals(n)
        except Exception as e:  # noqa: BLE001
            log("backlog_sample_error", error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(every)
