"""第 28 课 Demo 第 2 节的 worker 应用。每个 worker 进程由 WorkerPool 这样拉起（和第 13、26 课是同一条命令）：

    python -m agentkit.distributed.worker --queue sqlite:///.../jobs.db \\
        --app lessons/28_production_observability/worker_app.py:make_handler --opt otlp=http://127.0.0.1:PORT ...

它和 demo（API 进程）之间只有两条通道，都是真实的跨进程通道：
  - 队列里的任务 payload：{"input": ..., "trace": {"traceparent": "00-<trace_id>-<span_id>-03"}}
    worker 取出后用 continue_trace 接着生产者的 trace 继续 —— trace 上下文就是这样"穿过"队列的；
  - OTLP/HTTP：worker 自己的 BatchSpanProcessor 把 span 发给接收端进程（otlp_receiver.py）。
指标走 prometheus_client 的多进程模式：WorkerPool 通过环境变量 PROMETHEUS_MULTIPROC_DIR 告诉每个 worker
把计数写进同一个目录（每个进程各写各的文件），demo 最后用 MultiProcessCollector 汇总。
"""

from __future__ import annotations

import asyncio
import os

from agentkit import Agent, ScriptedLLM, call_tool, default_llm, reply
from agentkit.contrib.otel import OTelTracer, PrometheusHook, continue_trace, setup_tracing
from agentkit.distributed import WorkerContext
from agentkit.testing import load_sibling

demo = load_sibling(__file__, "demo")  # 工具和 system prompt 与 demo 里的同一份


def offline_brain(messages):
    """离线剧本：先查订单，再查物流，最后回答（responder 模式：每个任务按自己的对话决定下一步，互不串台）。"""
    tool_results = [m for m in messages if m["role"] == "tool"]
    if not tool_results:
        return call_tool("lookup_order", order_id="A1001")
    if len(tool_results) == 1:
        return call_tool("track_shipment", tracking_no="SF1001")
    return reply("订单 A1001（降噪耳机）已到达上海浦东转运中心，预计明天送达。")


class TracedHandler:
    """任务 → 一次 Agent 运行，包在"消费者 span"里；返回值（写进队列的 result）带上本进程看到的 trace_id 和 pid。"""

    def __init__(self, wctx: WorkerContext, agent: Agent, tracer: OTelTracer, provider):
        self.wctx, self.agent, self.tracer, self.provider = wctx, agent, tracer, provider

    async def __call__(self, job) -> dict:
        # continue_trace：把 payload 里的 traceparent 设为当前 context 的父级 —— 块里新建的 span 都挂在生产者的 span 下面。
        # 每个任务在 run_worker 里是一个独立的 asyncio task，各自进入、各自退出，并发的任务互不串线。
        async with continue_trace(job.payload.get("trace")):
            with self.tracer.span("process agent-tasks", **{"otel.kind": "consumer",
                                                            "messaging.destination.name": "agent-tasks"}):
                result = await self.agent.run(job.payload["input"], metadata={"tenant_id": job.tenant_id},
                                              run_id=f"job-{job.id}")
        return {"trace_id": result.trace.trace_id, "pid": os.getpid(), "worker": self.wctx.worker_id,
                "status": result.status, "output": result.output}

    async def aclose(self) -> None:
        """worker 收到 SIGTERM、run_worker 退出后调用：关掉模型客户端，再把还没发出去的 span 全部发走。"""
        await self.agent.aclose()
        # shutdown() 会 flush BatchSpanProcessor —— 它是阻塞调用（要等 HTTP 请求返回），放进线程，不卡事件循环
        await asyncio.to_thread(self.provider.shutdown)


async def make_handler(wctx: WorkerContext) -> TracedHandler:
    opts = wctx.options
    provider = setup_tracing("hello-agent-worker", otlp_endpoint=opts["otlp"], set_global=False,
                             resource_attributes={"process.pid": os.getpid()})
    tracer = OTelTracer(provider)
    prom = PrometheusHook()  # 默认 registry；PROMETHEUS_MULTIPROC_DIR 已由 WorkerPool 的 env 设好 → 多进程模式
    if opts.get("offline", "1") == "1":
        llm = ScriptedLLM(responder=offline_brain, latency=float(opts.get("latency", 0.05)))
    else:
        llm = default_llm(max_connections=1)  # 每个 worker 同一时刻最多 1 个模型请求
    agent = Agent(llm, [demo.lookup_order, demo.track_shipment], system_prompt=demo.SYSTEM_PROMPT, name="support",
                  tracer=tracer, hooks=[tracer, prom])
    return TracedHandler(wctx, agent, tracer, provider)
