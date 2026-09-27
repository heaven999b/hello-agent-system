"""第 28 课 Demo：把 agentkit 的追踪接到 OpenTelemetry，把指标接到 Prometheus。

    python lessons/28_production_observability/demo.py            # 真实模型（读取 .env）
    python lessons/28_production_observability/demo.py --offline  # 离线剧本（ScriptedLLM），不调用模型

四个部分：
  1. OTelTracer + 内存导出器：跑 3 个客服任务（正常 / 物流工具失败 / 退款要审批→批准→恢复），
     打印 OTel span 树和 GenAI 语义约定属性；再演示内容采集开关（默认关，打开后仍先脱敏）
  2. 跨队列传播：生产者（本进程）把 traceparent 放进任务 payload，worker（另一个操作系统进程）接着同一条
     trace 执行，打印两边的 trace_id 作为证据；再用 AsyncAgent 写的 asyncio worker 并发处理 20 个任务
     （其中 3 个中途取消），检查有没有串线、取消的运行是否被当成错误、在途数是否归零
  3. PrometheusHook：真的启动 /metrics HTTP 服务，抓取并打印节选
  4. OTLP 导出：设置了 OTEL_EXPORTER_OTLP_ENDPOINT 就真实导出到那里；没设置时启动一个本地迷你 OTLP 接收端，
     让你看到线上传输的到底是什么，并提示如何用 Jaeger all-in-one 或 Langfuse 接收

缺少可选依赖时（例如 CI 只装了 dev 依赖）打印安装命令后正常退出。
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Annotated

from pydantic import Field

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:  # 直接 python lessons/.../demo.py 运行时也能 import agentkit
    sys.path.insert(0, str(REPO))

from agentkit import (  # noqa: E402
    Agent,
    PermissionPolicy,
    ScriptedLLM,
    ToolError,
    call_tool,
    call_tools,
    default_llm,
    render_tree,
    reply,
    tool,
)
from agentkit.aio import AsyncAgent, AsyncScriptedLLM  # noqa: E402

REQUIRED = ("opentelemetry.sdk", "opentelemetry.exporter.otlp.proto.http", "prometheus_client")
SYSTEM_PROMPT = (
    "你是电商客服助手。查物流时先用 lookup_order 拿到物流单号，再用 track_shipment 查询；"
    "物流查询失败时不要重试，直接向用户致歉并说明稍后再查。用户要求退款时调用 refund（需要人工审批）。回答简洁。"
)


def missing_deps() -> list[str]:
    missing = []
    for name in REQUIRED:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except ModuleNotFoundError:
            missing.append(name)
    return missing


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------------------------------------------------------------- 模拟的业务系统

ORDERS = {
    "A1001": {"status": "已发货", "tracking_no": "SF1001", "item": "降噪耳机"},
    "A1002": {"status": "已发货", "tracking_no": "YT2002", "item": "机械键盘"},
    "A1003": {"status": "待发货", "tracking_no": None, "item": "显示器支架"},
}


@tool
def lookup_order(order_id: Annotated[str, Field(description="订单号，例如 A1001")]) -> dict:
    """查询订单状态、商品和物流单号。"""
    order = ORDERS.get(order_id.strip().upper())
    if order is None:
        raise ToolError(f"订单 {order_id} 不存在，请让用户核对订单号。")
    return order


@tool
def track_shipment(tracking_no: Annotated[str, Field(description="物流单号，例如 SF1001")]) -> str:
    """查询物流进度。"""
    if tracking_no.strip().upper().startswith("YT"):
        time.sleep(0.05)
        raise ToolError("物流商接口超时，暂时无法查询。")
    return f"{tracking_no}：已到达【上海浦东转运中心】，预计明天送达。"


@tool(risk="dangerous")
def refund(order_id: Annotated[str, Field(description="订单号")], reason: Annotated[str, Field(description="退款原因")]) -> str:
    """为订单发起退款（高风险操作，需要人工审批）。"""
    return f"订单 {order_id} 已发起退款，原因：{reason}。"


TASKS = [
    ("正常", "帮我查一下订单 A1001 的物流", [
        call_tool("lookup_order", order_id="A1001"),
        call_tool("track_shipment", tracking_no="SF1001"),
        reply("订单 A1001（降噪耳机）已到达上海浦东转运中心，预计明天送达。"),
    ]),
    ("工具失败", "订单 A1002 到哪了？", [
        call_tool("lookup_order", order_id="A1002"),
        call_tool("track_shipment", tracking_no="YT2002"),
        reply("抱歉，物流商接口暂时超时，无法查询 YT2002 的进度，请稍后再试。"),
    ]),
    ("审批", "订单 A1003 我不要了，帮我退款", [
        call_tool("refund", order_id="A1003", reason="用户不想要了"),
        reply("已为订单 A1003 发起退款，审批已通过。"),
    ]),
]


def make_llm(args, script):
    return ScriptedLLM(list(script)) if args.offline else args.llm


# ---------------------------------------------------------------- 打印 OTel span


KEY_ATTRS = (
    "gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.request.model", "gen_ai.provider.name",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.tool.name", "gen_ai.tool.call.id",
    "gen_ai.conversation.id", "error.type", "agentkit.interrupted", "agentkit.resumed",
    "messaging.destination.name",
)


def span_dict(s) -> dict:
    return {
        "name": s.name,
        "kind": s.kind.name,
        "status": s.status.status_code.name,
        "trace_id": format(s.context.trace_id, "032x"),
        "span_id": format(s.context.span_id, "016x"),
        "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
        "start": s.start_time,
        "attrs": {k: (list(v) if isinstance(v, tuple) else v) for k, v in s.attributes.items()},
        "pid": os.getpid(),
    }


def print_otel_tree(spans: list[dict], show_pid: bool = False) -> None:
    ids = {s["span_id"] for s in spans}
    children: dict = {}
    for s in sorted(spans, key=lambda s: s["start"]):
        parent = s["parent_id"] if s["parent_id"] in ids else None
        children.setdefault(parent, []).append(s)

    def walk(node, prefix: str, last: bool, root: bool) -> None:
        a = node["attrs"]
        bits = [node["name"], node["kind"].lower()]
        if node["status"] != "UNSET":
            bits.append(node["status"])
        for key in KEY_ATTRS:
            if key in a and key not in ("gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.tool.name"):
                bits.append(f"{key.replace('gen_ai.', '')}={a[key]}")
        if show_pid:
            bits.append(f"[pid {node['pid']}]")
        print(("  " if root else prefix + ("└─ " if last else "├─ ")) + "  ".join(str(b) for b in bits))
        kids = children.get(node["span_id"], [])
        for i, c in enumerate(kids):
            walk(c, "  " if root else prefix + ("   " if last else "│  "), i == len(kids) - 1, False)

    for top in children.get(None, []):
        tag = "（父 span 在别的进程）" if top["parent_id"] else ""
        print(f"  trace {top['trace_id']}{tag}")
        walk(top, "", True, True)


# ---------------------------------------------------------------- 1. OTelTracer 双写


def part1(args, prom):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agentkit.contrib.otel import OTelTracer, setup_tracing

    banner("1. OTelTracer：agentkit Span 树照常生成，同时实时写 OTel span（GenAI 语义约定）")
    exporter = InMemorySpanExporter()
    provider = setup_tracing("hello-agent-demo", exporter=exporter, set_global=False)
    tracer = OTelTracer(provider)  # 默认不采集内容
    meta = {"tenant_id": "acme", "user_id": "u-1001", "conversation_id": "conv-7f3a"}
    for label, text, script in TASKS:
        exporter.clear()
        agent = Agent(
            make_llm(args, script), [lookup_order, track_shipment, refund], system_prompt=SYSTEM_PROMPT,
            name="support", tracer=tracer, hooks=[tracer, prom, PermissionPolicy()],
        )
        result = agent.run(text, metadata=meta)
        print(f"\n▶ [{label}] 用户：{text}")
        if result.status == "paused":
            pending = prom.registry.get_sample_value("agent_approvals_pending")
            print(f"  ⏸  暂停等审批：{result.pending_approval.name}（此刻 agent_approvals_pending = {pending:.0f}）")
            print_otel_tree([span_dict(s) for s in exporter.get_finished_spans()])
            exporter.clear()
            result = agent.approve(result.run_id, True, by="客服主管")
            print("  ✅ 审批通过后恢复（新的一条 trace，用 agentkit.run_id 关联）：")
        print(f"  助手：{result.output}")
        print_otel_tree([span_dict(s) for s in exporter.get_finished_spans()])
        print(f"  RunResult.trace.trace_id = {result.trace.trace_id}  ← 与上面 OTel 的 trace_id 相同，可直接去后端搜索")

    print("\n  agentkit 这一侧完全不受影响，render_tree 照常可用（最后一次运行）：")
    print("  " + render_tree(result.trace).replace("\n", "\n  "))

    print("\n—— 内容采集开关 ——")
    everything = json.dumps([dict(s.attributes) for s in exporter.get_finished_spans()], ensure_ascii=False)
    print(f"  默认关闭：span 属性里有没有 gen_ai.input.messages？{'有' if 'gen_ai.input.messages' in everything else '没有'}")
    exporter.clear()
    loud = OTelTracer(provider, capture_content=True, max_content_chars=80)
    text = "我手机 13812345678，收货人张伟，查下 A1001"
    Agent(make_llm(args, TASKS[0][2]), [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT,
          name="support", tracer=loud, hooks=[loud]).run(text)
    root = next(s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent"))
    captured = json.loads(root.attributes["gen_ai.input.messages"])[0]["parts"][0]["content"]
    print("  capture_content=True 之后：")
    print(f"    原文 : {text}")
    print(f"    记录 : {captured}")
    print("    手机号在 SDK 端就被替换了；姓名这类自由文本正则抓不住 —— 这就是默认关闭的原因（问题 3）。")
    provider.shutdown()


# ---------------------------------------------------------------- 2. 跨队列传播


def worker_process(payload: str, offline: bool, out: "mp.Queue") -> None:
    """另一个操作系统进程里的 worker：只拿到 payload（JSON 字符串），没有任何共享内存。"""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agentkit.contrib.otel import OTelTracer, continue_trace, setup_tracing

    job = json.loads(payload)
    exporter = InMemorySpanExporter()
    tracer = OTelTracer(setup_tracing("hello-agent-worker", exporter=exporter, set_global=False))
    llm = ScriptedLLM(list(TASKS[0][2])) if offline else default_llm()
    agent = Agent(llm, [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT, name="support", tracer=tracer)
    with continue_trace(job["trace"]):  # 接着生产者的 trace 继续
        with tracer.span("process agent-tasks", **{"otel.kind": "consumer", "messaging.destination.name": "agent-tasks"}):
            result = agent.run(job["input"])
    out.put({"spans": [span_dict(s) for s in exporter.get_finished_spans()], "trace_id": result.trace.trace_id, "pid": os.getpid()})


def part2(args):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from prometheus_client import CollectorRegistry

    from agentkit.contrib.otel import OTelTracer, PrometheusHook, continue_trace, inject_context, setup_tracing

    banner("2. 跨队列传播：traceparent 放进任务 payload，另一个进程里的 worker 接着同一条 trace 执行")
    exporter = InMemorySpanExporter()
    tracer = OTelTracer(setup_tracing("hello-agent-api", exporter=exporter, set_global=False))
    with tracer.span("send agent-tasks", **{"otel.kind": "producer", "messaging.destination.name": "agent-tasks"}) as send:
        payload = json.dumps({"input": TASKS[0][1], "trace": inject_context({})}, ensure_ascii=False)
    print(f"  生产者（pid {os.getpid()}）写进队列的 payload：\n    {payload}")

    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    proc = ctx.Process(target=worker_process, args=(payload, args.offline, out))
    proc.start()
    got = out.get(timeout=120)
    proc.join(timeout=30)
    print(f"\n  生产者 span 的 trace_id      = {send.trace_id}")
    print(f"  worker（pid {got['pid']}）的 trace_id = {got['trace_id']}   {'✅ 同一条 trace' if got['trace_id'] == send.trace_id else '❌ 断链'}")
    print("\n  合并两个进程导出的 span（后端看到的就是这一棵树）：")
    print_otel_tree([span_dict(s) for s in exporter.get_finished_spans()] + got["spans"], show_pid=True)

    print("\n  asyncio worker：一个 AsyncAgent、20 个任务、并发 10，每个任务在自己的 task 里 async with continue_trace；")
    print("  其中 3 个任务中途被取消（模拟 HTTP 客户端断开）。这一节始终用离线剧本，避免并发请求打到共享的模型网关。")
    exporter.clear()
    registry = CollectorRegistry()
    prom = PrometheusHook(registry)

    llm = AsyncScriptedLLM(
        responder=lambda m: reply("已为你查到物流") if m[-1]["role"] == "tool"
        else call_tools(("lookup_order", {"order_id": "A1001"}), ("track_shipment", {"tracking_no": "SF1001"})),
        latency=lambda n: 0.02 + 0.01 * (n % 4),
    )
    agent = AsyncAgent(llm, [lookup_order, track_shipment], name="support", tracer=tracer, hooks=[tracer, prom])

    async def main():
        q: asyncio.Queue = asyncio.Queue()
        expected = {}
        for i in range(20):
            with tracer.span("send agent-tasks", **{"otel.kind": "producer"}) as s:
                await q.put({"id": i, "trace": inject_context({})})
            expected[i] = s.trace_id
        sem = asyncio.Semaphore(10)
        got_ids = {}

        async def handle(job):
            async with sem, continue_trace(job["trace"]):
                result = await agent.run(f"job {job['id']}")
                got_ids[job["id"]] = result.trace.trace_id

        tasks = [asyncio.create_task(handle(await q.get())) for _ in range(20)]
        await asyncio.sleep(0.01)
        peak = registry.get_sample_value("agent_runs_in_flight")
        for t in tasks[:3]:
            t.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        return expected, got_ids, peak, outcomes

    expected, got_ids, peak, outcomes = asyncio.run(main())
    crossed = sum(expected[i] != got_ids[i] for i in got_ids)
    roots = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent support"]
    cancelled = [s for s in roots if s.attributes.get("agentkit.agent.status") == "cancelled"]
    print(f"  完成 {len(got_ids)} 个、取消 {sum(isinstance(o, asyncio.CancelledError) for o in outcomes)} 个；"
          f"模型调用最高并发 {llm.max_in_flight}，运行中途采样的在途数 {peak:.0f}")
    print(f"  {len(roots)} 条 invoke_agent trace，互不相同的 trace_id：{len({s.context.trace_id for s in roots})} 个；"
          f"与各自生产者 trace_id 不一致的：{crossed} 条")
    print(f"  被取消的运行：OTel 状态 {sorted({s.status.status_code.name for s in cancelled})}，"
          f"agentkit.interrupted={cancelled[0].attributes.get('agentkit.interrupted') if cancelled else None}（取消不是错误，不触发告警）")
    print(f"  结束后：agent_runs_in_flight = {registry.get_sample_value('agent_runs_in_flight'):.0f}，"
          f"agent_runs_total{{status=cancelled}} = {registry.get_sample_value('agent_runs_total', {'status': 'cancelled', 'reason': 'cancelled'}):.0f}")


# ---------------------------------------------------------------- 3. Prometheus


def part3(prom, registry):
    from agentkit.contrib.otel import start_metrics_server

    banner("3. PrometheusHook：/metrics（真的启动 HTTP 服务再抓取）")
    # 标签基数防护：一个不在白名单里的租户（比如被人用随机字符串刷接口）只会落进 "__other__"
    Agent(ScriptedLLM([reply("你好")]), [], hooks=[prom]).run("hi", metadata={"tenant_id": "rnd-8f2c91"})
    prom.set_queue_stats("agent-tasks", depth=3, oldest_age_seconds=12.5)
    server, _ = start_metrics_server(0, registry=registry)
    try:
        url = f"http://127.0.0.1:{server.server_port}/metrics"
        body = urllib.request.urlopen(url, timeout=5).read().decode()
    finally:
        server.shutdown()
        server.server_close()
    print(f"  GET {url}（节选，省略了 _created 和直方图的桶）")
    keep = ("agent_runs_total", "agent_run_duration_seconds_count", "agent_run_duration_seconds_sum", "agent_llm_tokens_total",
            "agent_llm_cost_usd_total", "agent_tool_calls_total", "agent_approvals_pending", "agent_runs_in_flight",
            "agent_queue_")
    for line in body.splitlines():
        if line.startswith(keep):
            print("    " + line)
    print("  注意：没有 user_id、run_id、trace_id 标签 —— 它们在 trace 里；tenant 只放行白名单 acme/globex。")


# ---------------------------------------------------------------- 4. OTLP 导出


class MiniOTLPReceiver(http.server.BaseHTTPRequestHandler):
    """只为演示线上格式的迷你接收端：解析 OTLP/HTTP protobuf 请求并记下来。它不是 Collector。"""

    received: list = []

    def do_POST(self):  # noqa: N802
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        self.received.append((self.path, self.headers.get("Content-Type"), len(body), request))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        pass


def part4(args):
    from agentkit.contrib.otel import OTelTracer, setup_tracing

    banner("4. OTLP 导出")
    env_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if env_endpoint:
        provider = setup_tracing("hello-agent-demo", set_global=False)  # 端点、认证头都从 OTEL_EXPORTER_OTLP_* 读取
        tracer = OTelTracer(provider)
        result = Agent(make_llm(args, TASKS[0][2]), [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT,
                       name="support", tracer=tracer).run(TASKS[0][1])
        tracer.force_flush()
        provider.shutdown()
        print(f"  已通过 OTLP/HTTP 导出到 {env_endpoint}")
        print(f"  去后端按 trace_id 搜索：{result.trace.trace_id}（Jaeger UI 默认在 http://localhost:16686）")
        print("  提示：force_flush() 返回 True 只表示队列已清空，不代表对方收下了；导出失败只在日志里告警。")
        return

    MiniOTLPReceiver.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MiniOTLPReceiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        provider = setup_tracing("hello-agent-demo", otlp_endpoint=endpoint, set_global=False,
                                 resource_attributes={"deployment.environment.name": "demo"})
        tracer = OTelTracer(provider)
        Agent(make_llm(args, TASKS[1][2]), [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT,
              name="support", tracer=tracer).run(TASKS[1][1])
        tracer.force_flush()
        provider.shutdown()
    finally:
        server.shutdown()
    print("  没有设置 OTEL_EXPORTER_OTLP_ENDPOINT：先用一个本地迷你接收端看看 BatchSpanProcessor 真正发出了什么")
    for path, content_type, size, request in MiniOTLPReceiver.received:
        for rs in request.resource_spans:
            res = {a.key: a.value.string_value for a in rs.resource.attributes}
            names = [sp.name for ss in rs.scope_spans for sp in ss.spans]
            print(f"    POST {path}  Content-Type: {content_type}  {size} 字节")
            print(f"      resource: service.name={res.get('service.name')}  deployment.environment.name={res.get('deployment.environment.name')}"
                  f"  telemetry.sdk.version={res.get('telemetry.sdk.version')}")
            print(f"      {len(names)} 个 span：{', '.join(names)}")
    print("""
  想在界面里看，二选一（本机没有 Docker，下面的命令未在本机实际运行）：
    • Jaeger all-in-one（v2，原生接收 OTLP）：
        docker run --rm -p 16686:16686 -p 4317:4317 -p 4318:4318 cr.jaegertracing.io/jaegertracing/jaeger:2.21.0
        OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python lessons/28_production_observability/demo.py --offline
        然后打开 http://localhost:16686
    • Langfuse（OTLP/HTTP，不支持 gRPC；认证是 Basic base64(公钥:私钥)）：
        OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
        OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20${LANGFUSE_AUTH}"   # 空格写成 %20
    生产中推荐应用只发给本机 / 集群内的 OTel Collector，由 Collector 做脱敏、尾部采样和扇出（configs/otel-collector.yaml）。""")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="第 28 课 Demo：生产可观测性")
    parser.add_argument("--offline", action="store_true", help="使用离线剧本（ScriptedLLM），不调用真实模型")
    args = parser.parse_args()

    missing = missing_deps()
    if missing:
        print(f"⚠️  缺少可选依赖：{', '.join(missing)}")
        print('   请先安装：pip install -e ".[prod,prod-local]"   （只装本课需要的：pip install -e ".[otel]"）')
        return

    if args.offline:
        print("🔌 离线模式：ScriptedLLM 剧本，不调用模型")
        args.llm = None
    else:
        try:
            args.llm = default_llm()
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"🌐 真实模型：{args.llm.model}（依次调用，不并发）")

    from prometheus_client import CollectorRegistry

    from agentkit.contrib.otel import PrometheusHook

    registry = CollectorRegistry()
    prom = PrometheusHook(registry, tenant_label=True, allowed_tenants={"acme", "globex"})
    started = time.time()
    part1(args, prom)
    part2(args)
    part3(prom, registry)
    part4(args)
    print(f"\n总耗时 {time.time() - started:.1f} 秒。")


if __name__ == "__main__":
    main()
