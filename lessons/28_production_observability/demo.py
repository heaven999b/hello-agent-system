"""第 28 课 Demo：把 agentkit 的追踪接到 OpenTelemetry，把指标接到 Prometheus。

    python lessons/28_production_observability/demo.py            # 真实模型（读取 .env）
    python lessons/28_production_observability/demo.py --offline  # 离线剧本（ScriptedLLM），不调用模型

第 2 节一共 4 个操作系统进程：
    本进程（API / 生产者）──任务 payload 带 traceparent──► SQLite 队列 ◄──领取── 2 个 worker 进程（WorkerPool）
          │ OTLP/HTTP                                                                  │ OTLP/HTTP
          └──────────────────► 迷你 OTLP 接收端（otlp_receiver.py，独立进程）◄──────────┘

四个部分：
  1. OTelTracer + 内存导出器：跑 3 个客服任务（正常 / 物流工具失败 / 退款要审批→批准→恢复），
     打印 OTel span 树和 GenAI 语义约定属性；再演示内容采集开关（默认关，打开后仍先脱敏）
  2. 跨队列、跨进程传播：本进程在 "send" span 里把 traceparent 写进任务 payload 入队；WorkerPool 拉起的
     2 个 worker 进程（python -m agentkit.distributed.worker）取出任务后 continue_trace 接着同一条 trace 执行。
     所有进程都把 span 用 OTLP 发给接收端进程；打印每个任务"生产者的 trace_id = worker 里 Agent 的 trace_id"，
     以及接收端收到的合并后的 span 树；worker 的指标用 prometheus_client 多进程模式汇总。
     然后在本进程里用一个 Agent 并发处理 20 个任务（其中 3 个中途取消），检查有没有串线、取消是否被当成错误
  3. PrometheusHook：真的启动 /metrics HTTP 服务，抓取并打印节选
  4. OTLP 导出：设置了 OTEL_EXPORTER_OTLP_ENDPOINT 就真实导出到那里；没设置时发给接收端进程，
     让你看到线上传输的到底是什么，并提示如何用 Jaeger all-in-one 或 Langfuse 接收

缺少可选依赖时（例如 CI 只装了 dev 依赖）打印安装命令后正常退出。检查不通过（trace 断链、串线、多进程指标对不上）时退出码为 1。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


async def part1(args, prom):
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
        result = await agent.run(text, metadata=meta)
        print(f"\n▶ [{label}] 用户：{text}")
        if result.status == "paused":
            pending = prom.registry.get_sample_value("agent_approvals_pending")
            print(f"  ⏸  暂停等审批：{result.pending_approval.name}（此刻 agent_approvals_pending = {pending:.0f}）")
            print_otel_tree([span_dict(s) for s in exporter.get_finished_spans()])
            exporter.clear()
            result = await agent.approve(result.run_id, True, by="客服主管")
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
    await Agent(make_llm(args, TASKS[0][2]), [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT,
                name="support", tracer=loud, hooks=[loud]).run(text)
    root = next(s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent"))
    captured = json.loads(root.attributes["gen_ai.input.messages"])[0]["parts"][0]["content"]
    print("  capture_content=True 之后：")
    print(f"    原文 : {text}")
    print(f"    记录 : {captured}")
    print("    手机号在 SDK 端就被替换了；姓名这类自由文本正则抓不住 —— 这就是默认关闭的原因（问题 3）。")
    provider.shutdown()


# ---------------------------------------------------------------- 迷你 OTLP 接收端（独立进程）


class Receiver:
    """otlp_receiver.py 子进程的句柄：启动、读端口、查询它收到了什么。"""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, str(HERE / "otlp_receiver.py")], stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().split()
        if len(line) != 2 or line[0] != "LISTENING":
            self.close()
            raise RuntimeError(f"OTLP 接收端没有启动成功：{line}")
        self.endpoint = f"http://127.0.0.1:{line[1]}"

    def _call(self, path: str, method: str = "GET"):
        req = urllib.request.Request(self.endpoint + path, method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    async def get(self, path: str):  # urllib 是阻塞的：放进线程，不卡本进程的事件循环
        return await asyncio.to_thread(self._call, path)

    async def reset(self) -> None:
        await asyncio.to_thread(self._call, "/reset", "POST")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)


# ---------------------------------------------------------------- 2. 跨队列、跨进程传播


async def wait_until(cond, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError(f"{timeout:.0f} 秒内没有等到{what}")


async def part2(args, receiver: Receiver) -> bool:
    from prometheus_client import CollectorRegistry
    from prometheus_client import multiprocess as prom_mp

    from agentkit.contrib.otel import OTelTracer, inject_context, setup_tracing
    from agentkit.distributed import SQLiteJobQueue, WorkerPool

    banner("2. 跨队列、跨进程传播：traceparent 放进任务 payload，另外两个 worker 进程接着同一条 trace 执行")
    workdir = Path(tempfile.mkdtemp(prefix="agentkit_l28_"))
    prom_dir = workdir / "prom"
    prom_dir.mkdir()
    db = workdir / "jobs.db"
    queue = SQLiteJobQueue(db)
    await queue.setup()
    await receiver.reset()
    api_provider = setup_tracing("hello-agent-api", otlp_endpoint=receiver.endpoint, set_global=False,
                                 resource_attributes={"process.pid": os.getpid()})
    tracer = OTelTracer(api_provider)
    n_jobs = 6 if args.offline else 2  # 真实模式：每个 worker 同一时刻最多 1 个模型请求，总共 2 个任务
    pool = WorkerPool(
        f"sqlite:///{db}", f"{HERE / 'worker_app.py'}:make_handler", n=2, concurrency=2 if args.offline else 1,
        lease=30, poll=0.05, options={"otlp": receiver.endpoint, "offline": "1" if args.offline else "0"},
        # 多进程指标：每个 worker 把计数写进这个目录（必须在进程导入 prometheus_client 之前设置 → 用环境变量传）；
        # span 攒 0.2 秒就发一批（默认 5 秒），demo 不用等太久
        env={"PROMETHEUS_MULTIPROC_DIR": str(prom_dir), "OTEL_BSP_SCHEDULE_DELAY": "200"},
        log_dir=workdir / "logs", name="worker-",
    )
    sent: dict[int, str] = {}
    pool.start()
    try:
        await wait_until(lambda: len(pool.events("started")) == 2, 60, "2 个 worker 进程启动")
        print(f"  API 进程 pid {os.getpid()}；worker 进程 pid {pool.pids}（python -m agentkit.distributed.worker --queue sqlite:///…）")
        for i in range(n_jobs):
            with tracer.span("send agent-tasks", **{"otel.kind": "producer", "messaging.destination.name": "agent-tasks"}) as send:
                payload = {"input": TASKS[0][1], "trace": inject_context({})}  # 当前 span（send）的 traceparent
                jid = await queue.enqueue("agent", payload, tenant_id="acme")
            sent[jid] = send.trace_id
            if i == 0:
                print(f"  写进队列的 payload（任务 #{jid}）：\n    {json.dumps(payload, ensure_ascii=False)}")

        async def all_done():
            jobs = [await queue.get(j) for j in sent]
            return all(j.status in ("succeeded", "failed", "dead") for j in jobs)

        await wait_until(all_done, 120 if args.offline else 300, f"{n_jobs} 个任务全部处理完")
    finally:
        # SIGTERM → run_worker 停止领取、做完在途任务 → handler.aclose() → provider.shutdown() 把剩下的 span 发走
        codes = await asyncio.to_thread(pool.stop)
    await asyncio.to_thread(api_provider.shutdown)
    results = {jid: (await queue.get(jid)) for jid in sent}
    await queue.close()

    print(f"\n  {'任务':<6}{'生产者（API 进程）的 trace_id':<36}{'处理它的 worker':<22}worker 里 Agent 的 trace_id")
    ok = True
    for jid, trace_id in sent.items():
        job = results[jid]
        r = job.result or {}
        same = r.get("trace_id") == trace_id
        ok = ok and same and job.status == "succeeded"
        print(f"  #{jid:<5}{trace_id:<36}{r.get('worker', '?')}（pid {r.get('pid', '?')}）".ljust(66)
              + f"{r.get('trace_id')}  {'✅' if same else '❌ 断链'}")
    pids = {(results[j].result or {}).get("pid") for j in sent}
    print(f"  {len(sent)} 个任务由 {len(pids)} 个不同的 worker 进程处理；worker 退出码 {codes}（0 = 收到 SIGTERM 后正常退出）")

    spans = await receiver.get("/spans")
    first = next(iter(sent))
    tree = [s for s in spans if s["trace_id"] == sent[first]]
    services = sorted({(s["service"], s["pid"]) for s in spans})
    print(f"\n  接收端进程（pid {receiver.proc.pid}）一共收到 {len(spans)} 个 span，来自 {len(services)} 个进程："
          + "、".join(f"{svc}[pid {pid}]" for svc, pid in services))
    print(f"  任务 #{first} 的那条 trace（后端看到的就是这一棵树：生产者和 worker 的 span 分别从两个进程发来）：")
    print_otel_tree(tree, show_pid=True)

    registry = CollectorRegistry()
    for pid in pool.pids:
        prom_mp.mark_process_dead(pid, path=str(prom_dir))  # 进程退出后清掉它的 live* 指标（gunicorn 的 child_exit 同理）
    prom_mp.MultiProcessCollector(registry, path=str(prom_dir))
    done = registry.get_sample_value("agent_runs_total", {"status": "completed", "reason": "final_answer"}) or 0
    files = sorted(f.name for f in prom_dir.iterdir())
    print(f"\n  多进程指标：{len(files)} 个指标文件（每个进程各写各的，例如 {files[0] if files else '-'}），"
          f"MultiProcessCollector 汇总后 agent_runs_total{{status=completed}} = {done:.0f}（{n_jobs} 个任务）")
    shutil.rmtree(workdir, ignore_errors=True)
    return ok and done == n_jobs


async def part2_inprocess(tracer_provider_factory):
    """同一个进程里：一个 Agent、20 个并发任务，每个任务在自己的 task 里 async with continue_trace。"""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from prometheus_client import CollectorRegistry

    from agentkit.contrib.otel import OTelTracer, PrometheusHook, continue_trace, inject_context

    print("\n  进程内的 asyncio worker：一个 Agent、20 个任务、并发 10，每个任务在自己的 task 里 async with continue_trace；")
    print("  其中 3 个任务中途被取消（模拟 HTTP 客户端断开）。这一节始终用离线剧本，避免并发请求打到共享的模型网关。")
    exporter = InMemorySpanExporter()
    provider = tracer_provider_factory(exporter)
    tracer = OTelTracer(provider)
    registry = CollectorRegistry()
    prom = PrometheusHook(registry)
    llm = ScriptedLLM(
        responder=lambda m: reply("已为你查到物流") if m[-1]["role"] == "tool"
        else call_tools(("lookup_order", {"order_id": "A1001"}), ("track_shipment", {"tracking_no": "SF1001"})),
        latency=lambda n: 0.02 + 0.01 * (n % 4),
    )
    agent = Agent(llm, [lookup_order, track_shipment], name="support", tracer=tracer, hooks=[tracer, prom])

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
    await agent.aclose()

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
    provider.shutdown()
    return crossed == 0


# ---------------------------------------------------------------- 3. Prometheus


async def part3(prom, registry):
    from agentkit.contrib.otel import start_metrics_server

    banner("3. PrometheusHook：/metrics（真的启动 HTTP 服务再抓取）")
    # 标签基数防护：一个不在白名单里的租户（比如被人用随机字符串刷接口）只会落进 "__other__"
    await Agent(ScriptedLLM([reply("你好")]), [], hooks=[prom]).run("hi", metadata={"tenant_id": "rnd-8f2c91"})
    prom.set_queue_stats("agent-tasks", depth=3, oldest_age_seconds=12.5)
    server, _ = start_metrics_server(0, registry=registry)
    try:
        url = f"http://127.0.0.1:{server.server_port}/metrics"
        body = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=5).read().decode())
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


async def part4(args, receiver: Receiver):
    from agentkit.contrib.otel import OTelTracer, setup_tracing

    banner("4. OTLP 导出")
    env_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if env_endpoint:
        provider = setup_tracing("hello-agent-demo", set_global=False)  # 端点、认证头都从 OTEL_EXPORTER_OTLP_* 读取
        tracer = OTelTracer(provider)
        result = await Agent(make_llm(args, TASKS[0][2]), [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT,
                             name="support", tracer=tracer).run(TASKS[0][1])
        await asyncio.to_thread(tracer.force_flush)
        await asyncio.to_thread(provider.shutdown)
        print(f"  已通过 OTLP/HTTP 导出到 {env_endpoint}")
        print(f"  去后端按 trace_id 搜索：{result.trace.trace_id}（Jaeger UI 默认在 http://localhost:16686）")
        print("  提示：force_flush() 返回 True 只表示队列已清空，不代表对方收下了；导出失败只在日志里告警。")
        return

    await receiver.reset()
    provider = setup_tracing("hello-agent-demo", otlp_endpoint=receiver.endpoint, set_global=False,
                             resource_attributes={"deployment.environment.name": "demo"})
    tracer = OTelTracer(provider)
    await Agent(make_llm(args, TASKS[1][2]), [lookup_order, track_shipment], system_prompt=SYSTEM_PROMPT,
                name="support", tracer=tracer).run(TASKS[1][1])
    # force_flush / shutdown 是阻塞调用（要等 HTTP 请求返回）：放进线程
    await asyncio.to_thread(tracer.force_flush)
    await asyncio.to_thread(provider.shutdown)
    print(f"  没有设置 OTEL_EXPORTER_OTLP_ENDPOINT：发给本机的迷你接收端进程（pid {receiver.proc.pid}），"
          "看看 BatchSpanProcessor 真正发出了什么")
    for req in await receiver.get("/requests"):
        for res in req["resources"]:
            print(f"    POST {req['path']}  Content-Type: {req['content_type']}  {req['bytes']} 字节")
            print(f"      resource: service.name={res.get('service.name')}  deployment.environment.name={res.get('deployment.environment.name')}"
                  f"  telemetry.sdk.version={res.get('telemetry.sdk.version')}")
        print(f"      {len(req['span_names'])} 个 span：{', '.join(req['span_names'])}")
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


async def amain(args) -> int:
    from prometheus_client import CollectorRegistry

    from agentkit.contrib.otel import PrometheusHook, setup_tracing

    registry = CollectorRegistry()
    prom = PrometheusHook(registry, tenant_label=True, allowed_tenants={"acme", "globex"})
    started = time.time()
    receiver = Receiver()
    try:
        await part1(args, prom)
        ok = await part2(args, receiver)
        ok = await part2_inprocess(lambda exporter: setup_tracing("hello-agent-api", exporter=exporter, set_global=False)) and ok
        await part3(prom, registry)
        await part4(args, receiver)
    finally:
        receiver.close()
        if args.llm is not None:
            await args.llm.aclose()
    print(f"\n总耗时 {time.time() - started:.1f} 秒。")
    if not ok:
        print("❌ 有检查没有通过：trace 断链、串线，或者多进程指标对不上（见上面的输出）")
        return 1
    return 0


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
        print(f"🌐 真实模型：{args.llm.model}（本进程里依次调用；第 2 节的两个 worker 进程各自最多 1 个在途请求）")
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
