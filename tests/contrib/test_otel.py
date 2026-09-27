"""agentkit.contrib.otel 的测试（第 28 课）。

覆盖：GenAI 属性映射（纯函数）、OTel 双写与 ID 对齐、错误 / 暂停语义、内容采集开关、
W3C traceparent 跨线程与跨进程传播、真实的 OTLP/HTTP 导出（本地接收端）、导出端故障不影响 Agent、
Prometheus 指标（并发、重复注册、标签基数、多进程模式）。

需要可选依赖 opentelemetry-sdk / prometheus-client（pip install -e ".[otel]"），缺失时相关测试自动跳过。
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import urllib.request
from pathlib import Path

import pytest

from agentkit import Agent, LLMError, PermissionPolicy, ScriptedLLM, ToolError, call_tool, render_tree, reply, tool
from agentkit.contrib.otel import LabelGuard, genai_mapping, to_genai_attributes

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 公共工具与 fixture


@tool
def lookup_order(order_id: str) -> dict:
    """查询订单"""
    return {"order_id": order_id, "status": "已发货"}


@tool
def track_shipment(tracking_no: str) -> str:
    """查询物流"""
    raise ToolError("物流接口超时")


@tool(risk="dangerous")
def refund(order_id: str) -> str:
    """退款（高风险，需要审批）"""
    return f"{order_id} 已退款"


@pytest.fixture
def otel():
    """每个测试一个独立的 TracerProvider + 内存导出器（不设全局 provider，测试互不干扰）。"""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agentkit.contrib.otel import setup_tracing

    exporter = InMemorySpanExporter()
    provider = setup_tracing("test-agent", exporter=exporter, set_global=False)
    yield provider, exporter
    provider.shutdown()


@pytest.fixture
def registry():
    pc = pytest.importorskip("prometheus_client")
    return pc.CollectorRegistry()


def by_name(spans):
    out = {}
    for s in spans:
        out.setdefault(s.name, []).append(s)
    return out


def hexid(n: int, width: int) -> str:
    return format(n, f"0{width}x")


def sample(registry, name: str, **labels) -> float:
    value = registry.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# ---------------------------------------------------------------- 1. 映射（纯函数，不需要 opentelemetry）


def test_mapping_follows_genai_conventions_for_the_three_core_spans():
    name, attrs, kind = genai_mapping("agent.run", {"agent.name": "support", "run_id": "r1", "agent.status": "completed"})
    assert (name, kind) == ("invoke_agent support", "internal")
    assert attrs["gen_ai.operation.name"] == "invoke_agent" and attrs["gen_ai.agent.name"] == "support"
    assert attrs["agentkit.run_id"] == "r1" and "error.type" not in attrs

    name, attrs, kind = genai_mapping("llm.chat", {"gen_ai.request.model": "gpt-5.5", "finish_reason": "length", "step": 2})
    assert (name, kind) == ("chat gpt-5.5", "client")
    assert attrs["gen_ai.provider.name"] == "openai" and attrs["gen_ai.response.finish_reasons"] == ["length"]
    assert attrs["agentkit.step"] == 2

    name, attrs, _ = genai_mapping("tool.track", {"tool.name": "track", "tool.ok": False, "tool.error_type": "timeout"})
    assert name == "execute_tool track"
    assert attrs["gen_ai.tool.name"] == "track" and attrs["gen_ai.tool.type"] == "function"
    assert attrs["error.type"] == "timeout"


def test_mapping_failure_semantics_denied_paused_and_run_failures():
    _, denied, _ = genai_mapping("tool.refund", {"tool.name": "refund", "tool.ok": False, "tool.error_type": "denied"})
    assert "error.type" not in denied  # 策略拒绝不是故障
    _, paused, _ = genai_mapping("agent.run", {"agent.name": "a", "agent.status": "paused", "interrupted": "PauseRun"})
    assert "error.type" not in paused and paused["agentkit.interrupted"] == "PauseRun"
    for status in ("failed", "max_steps"):
        assert genai_mapping("agent.run", {"agent.status": status})[1]["error.type"] == status
    # 没有模型名（agentkit 用 "?" 占位）时 span 名退化为 "chat"，不能写成 "chat ?"
    name, attrs, _ = genai_mapping("llm.chat", {"gen_ai.request.model": "?"})
    assert name == "chat" and "gen_ai.request.model" not in attrs


def test_mapping_privacy_content_is_opt_in_and_user_id_is_never_raw():
    attrs = {"tool.name": "find", "tool.arguments": '{"phone": "[手机号已脱敏]"}', "tool.result_preview": "..."}
    _, off, _ = genai_mapping("tool.find", attrs)
    assert "gen_ai.tool.call.arguments" not in off and "gen_ai.tool.call.result" not in off
    assert not any("arguments" in k or "preview" in k for k in off)
    _, on, _ = genai_mapping("tool.find", attrs, capture_content=True)
    assert on["gen_ai.tool.call.arguments"] == attrs["tool.arguments"]

    _, root, _ = genai_mapping("agent.run", {"user.id": "u-42", "gen_ai.input.messages": "[...]"})
    assert "user.id" not in root and "user.hash" not in root and "gen_ai.input.messages" not in root
    _, hashed, _ = genai_mapping("agent.run", {"user.id": "u-42"}, pseudonymize=lambda v: "h(" + v + ")")
    assert hashed["user.hash"] == "h(u-42)" and "user.id" not in hashed


def test_mapping_resume_does_not_double_count_cumulative_tokens_and_passes_user_attrs():
    _, attrs, _ = genai_mapping("agent.resume", {"agent.name": "a", "gen_ai.usage.input_tokens": 40, "gen_ai.usage.output_tokens": 20})
    assert attrs["agentkit.resumed"] is True
    assert "gen_ai.usage.input_tokens" not in attrs and attrs["agentkit.run.cumulative_input_tokens"] == 40
    name, attrs = to_genai_attributes("request", {"tenant.id": "acme", "app.feature": "faq", "x": None})
    assert name == "request" and attrs == {"agentkit.tenant.id": "acme", "app.feature": "faq"}


def test_label_guard_caps_distinct_values_under_thread_contention():
    guard = LabelGuard(max_values=5)
    barrier = threading.Barrier(16)
    results: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(50):
            v = guard(f"tenant-{(i * 50 + j) % 40}")
            with lock:
                results.append(v)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    admitted = set(results) - {LabelGuard.OVERFLOW}
    assert len(admitted) == 5 and LabelGuard.OVERFLOW in results
    assert LabelGuard(allowed={"acme"})("evil") == LabelGuard.OVERFLOW and LabelGuard()(None) == "unknown"


# ---------------------------------------------------------------- 2. OTelTracer 双写


def test_dual_write_keeps_agentkit_tree_and_aligns_ids(otel):
    from agentkit.contrib.otel import OTelTracer

    provider, exporter = otel
    tracer = OTelTracer(provider)
    agent = Agent(ScriptedLLM([call_tool("lookup_order", order_id="A1"), reply("已发货")]), [lookup_order], name="support", tracer=tracer)
    result = agent.run("A1 到哪了")

    assert result.ok and "tool.lookup_order" in render_tree(result.trace)  # agentkit 这一侧完全不受影响
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["chat scripted", "execute_tool lookup_order", "chat scripted", "invoke_agent support"]
    root = by_name(spans)["invoke_agent support"][0]
    # RunResult.trace 的 ID 就是 OTel 的 ID：拿它能直接去 Jaeger / Tempo 里搜
    assert result.trace.trace_id == hexid(root.context.trace_id, 32)
    assert result.trace.span_id == hexid(root.context.span_id, 16)
    for child, s in zip(result.trace.children, spans[:3]):
        assert child.span_id == hexid(s.context.span_id, 16) and s.parent.span_id == root.context.span_id


def test_genai_attributes_kinds_and_hook_enrichment(otel):
    from opentelemetry.trace import SpanKind

    from agentkit.contrib.otel import OTelTracer

    provider, exporter = otel
    tracer = OTelTracer(provider)
    llm = ScriptedLLM([call_tool("lookup_order", order_id="A1"), reply("ok")])
    agent = Agent(llm, [lookup_order], name="support", tracer=tracer, hooks=[tracer])
    agent.run("hi", metadata={"conversation_id": "conv-9", "tenant_id": "acme", "user_id": "u-1"})

    spans = by_name(exporter.get_finished_spans())
    root, tool_span, chat = spans["invoke_agent support"][0], spans["execute_tool lookup_order"][0], spans["chat scripted"][0]
    assert (root.kind, tool_span.kind, chat.kind) == (SpanKind.INTERNAL, SpanKind.INTERNAL, SpanKind.CLIENT)
    assert root.attributes["gen_ai.conversation.id"] == "conv-9"
    assert root.attributes["agentkit.tenant.id"] == "acme" and "user.id" not in root.attributes
    assert tool_span.attributes["gen_ai.tool.call.id"] == llm_first_call_id(agent)
    assert chat.attributes["gen_ai.usage.input_tokens"] == 20 and chat.attributes["gen_ai.response.finish_reasons"] == ("tool_calls",)
    assert chat.attributes["gen_ai.provider.name"] == "openai"


def llm_first_call_id(agent: Agent) -> str:
    return agent.llm.calls[1]["messages"][-2]["tool_calls"][0]["id"]


def test_tool_failure_is_error_but_policy_denial_is_not(otel):
    from opentelemetry.trace import StatusCode

    from agentkit.contrib.otel import OTelTracer

    provider, exporter = otel
    tracer = OTelTracer(provider)
    llm = ScriptedLLM([call_tool("track_shipment", tracking_no="SF1"), call_tool("refund", order_id="A1"), reply("抱歉")])
    agent = Agent(llm, [track_shipment, refund], name="support", tracer=tracer, hooks=[PermissionPolicy(deny_tools={"refund"})])
    assert agent.run("x").ok

    spans = by_name(exporter.get_finished_spans())
    failed, denied = spans["execute_tool track_shipment"][0], spans["execute_tool refund"][0]
    assert failed.status.status_code == StatusCode.ERROR and failed.attributes["error.type"] == "tool_error"
    assert denied.status.status_code == StatusCode.UNSET and "error.type" not in denied.attributes
    assert spans["invoke_agent support"][0].status.status_code == StatusCode.UNSET  # 运行本身完成了


def test_llm_error_marks_span_error_with_redacted_exception(otel):
    from opentelemetry.trace import StatusCode

    from agentkit.contrib.otel import OTelTracer

    provider, exporter = otel
    agent = Agent(ScriptedLLM([LLMError("gateway 503, user 13812345678")]), [], name="support", tracer=OTelTracer(provider))
    assert agent.run("x").status == "failed"

    spans = by_name(exporter.get_finished_spans())
    chat, root = spans["chat scripted"][0], spans["invoke_agent support"][0]
    assert chat.status.status_code == StatusCode.ERROR and chat.attributes["error.type"] == "LLMError"
    event = chat.events[0]
    assert event.name == "exception" and "13812345678" not in event.attributes["exception.message"]
    assert "13812345678" not in chat.status.description
    assert root.status.status_code == StatusCode.ERROR and root.attributes["error.type"] == "failed"


def test_pause_is_not_an_error_and_resume_is_correlated_by_run_id(otel, registry):
    from opentelemetry.trace import StatusCode

    from agentkit.contrib.otel import OTelTracer, PrometheusHook

    provider, exporter = otel
    tracer = OTelTracer(provider)
    prom = PrometheusHook(registry)
    agent = Agent(ScriptedLLM([call_tool("refund", order_id="A1"), reply("已退款")]), [refund], name="support", tracer=tracer, hooks=[tracer, prom, PermissionPolicy()])

    paused = agent.run("退款 A1")
    assert paused.status == "paused" and sample(registry, "agent_approvals_pending") == 1
    pause_spans = by_name(exporter.get_finished_spans())
    tool_span = pause_spans["execute_tool refund"][0]
    assert tool_span.status.status_code == StatusCode.UNSET and tool_span.attributes["agentkit.interrupted"] == "PauseRun"
    assert pause_spans["invoke_agent support"][0].status.status_code == StatusCode.UNSET

    exporter.clear()
    done = agent.approve(paused.run_id, True, by="lead")
    assert done.ok and sample(registry, "agent_approvals_pending") == 0
    roots = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent support"]
    assert roots[0].attributes["agentkit.resumed"] is True and roots[0].attributes["agentkit.run_id"] == paused.run_id
    assert done.trace.trace_id != paused.trace.trace_id  # 暂停几小时后恢复：新 trace，用 run_id 关联（问题 6）
    assert sample(registry, "agent_runs_total", status="paused", reason="needs_approval") == 1
    assert sample(registry, "agent_runs_total", status="completed", reason="final_answer") == 1


def test_content_capture_is_off_by_default_and_env_switch_turns_it_on_redacted(otel, monkeypatch):
    from agentkit.contrib.otel import CAPTURE_CONTENT_ENV, OTelTracer

    provider, exporter = otel

    def run_once():
        exporter.clear()
        tracer = OTelTracer(provider)
        llm = ScriptedLLM([call_tool("lookup_order", order_id="A1"), reply("电话 13900001111 的订单已发货")])
        Agent(llm, [lookup_order], name="s", tracer=tracer, hooks=[tracer]).run("我的手机 13812345678，查 A1")
        return by_name(exporter.get_finished_spans())

    monkeypatch.delenv(CAPTURE_CONTENT_ENV, raising=False)
    spans = run_once()
    everything = json.dumps([dict(s.attributes) for group in spans.values() for s in group], ensure_ascii=False)
    assert "gen_ai.input.messages" not in everything and "gen_ai.tool.call.arguments" not in everything

    monkeypatch.setenv(CAPTURE_CONTENT_ENV, "SPAN_ONLY")
    spans = run_once()
    root = spans["invoke_agent s"][0].attributes
    assert "[手机号已脱敏]" in root["gen_ai.input.messages"] and "13812345678" not in root["gen_ai.input.messages"]
    assert "13900001111" not in root["gen_ai.output.messages"]
    assert json.loads(root["gen_ai.input.messages"])[0]["parts"][0]["type"] == "text"
    assert "A1" in spans["execute_tool lookup_order"][0].attributes["gen_ai.tool.call.arguments"]


def test_concurrent_runs_on_shared_tracer_and_hook_do_not_mix(otel, registry):
    """8 个线程共用一个 OTelTracer 和一个 PrometheusHook 同时跑 Agent：span 不串台，计数不丢。"""
    from agentkit.contrib.otel import OTelTracer, PrometheusHook

    provider, exporter = otel
    tracer = OTelTracer(provider)
    prom = PrometheusHook(registry, tenant_label=True)
    barrier = threading.Barrier(8)
    trace_ids: dict[str, str] = {}
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(5):
            llm = ScriptedLLM([call_tool("lookup_order", order_id=f"A{i}{j}"), reply("ok")])
            r = Agent(llm, [lookup_order], name="support", tracer=tracer, hooks=[prom]).run("x", metadata={"tenant_id": f"t{i % 2}"})
            with lock:
                trace_ids[r.run_id] = r.trace.trace_id

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spans = exporter.get_finished_spans()
    assert len(spans) == 40 * 4 and len(set(trace_ids.values())) == 40
    by_trace: dict[int, list] = {}
    for s in spans:
        by_trace.setdefault(s.context.trace_id, []).append(s)
    for group in by_trace.values():
        root = next(s for s in group if s.name == "invoke_agent support")
        assert len(group) == 4 and all(s.parent.span_id == root.context.span_id for s in group if s is not root)
    assert sample(registry, "agent_runs_total", status="completed", reason="final_answer", tenant="t0") == 20
    assert sample(registry, "agent_runs_total", status="completed", reason="final_answer", tenant="t1") == 20
    assert sample(registry, "agent_tool_calls_total", tool="lookup_order", error_type="none") == 40
    assert sample(registry, "agent_llm_tokens_total", direction="input", tenant="t0") == 20 * 2 * 20


# ---------------------------------------------------------------- 2b. asyncio：真实的 AsyncAgent 高并发


@pytest.fixture
def aio():
    return pytest.importorskip("agentkit.aio")


def _consistency_probe(tracer, mismatches: list):
    """在工具内部检查：agentkit span 栈顶必须就是 OTel 的当前 span（两套 contextvars 必须同步）。"""
    from opentelemetry import trace

    ak = tracer.current_span()
    otel_id = format(trace.get_current_span().get_span_context().span_id, "016x")
    if ak is None or not ak.name.startswith("tool.") or ak.span_id != otel_id:
        mismatches.append((ak.name if ak else None, otel_id))


def test_async_agent_50_concurrent_runs_with_parallel_tools(otel, registry, aio):
    """50 个并发运行共用一个 AsyncAgent；每个运行一轮里并行调用 3 个只读工具（async、async、同步线程池各一个）。"""
    import asyncio

    from agentkit import call_tools
    from agentkit.contrib.otel import OTelTracer, PrometheusHook

    provider, exporter = otel
    tracer = OTelTracer(provider)
    prom = PrometheusHook(registry, tenant_label=True)
    mismatches: list = []
    observed: dict = {}
    state = {"arrived": 0, "all_in": None}

    @tool
    async def lookup_order(order_id: str) -> dict:
        """查询订单"""
        _consistency_probe(tracer, mismatches)
        state["arrived"] += 1
        if state["arrived"] == 50:  # 50 个运行都卡在工具里：此刻在途数必须是 50
            observed["in_flight"] = sample(registry, "agent_runs_in_flight")
            state["all_in"].set()
        await state["all_in"].wait()
        _consistency_probe(tracer, mismatches)
        return {"order_id": order_id}

    @tool
    async def get_policy(topic: str) -> str:
        """查询政策"""
        await asyncio.sleep(0.001)
        _consistency_probe(tracer, mismatches)
        return "7 天无理由"

    @tool
    def track_shipment(tracking_no: str) -> str:  # 同步工具：在线程池里执行，context 被复制进线程
        """查询物流"""
        _consistency_probe(tracer, mismatches)
        return "运输中"

    def responder(messages):
        if messages[-1]["role"] == "tool":
            return reply("好的")
        return call_tools(("lookup_order", {"order_id": "A1"}), ("track_shipment", {"tracking_no": "S1"}), ("get_policy", {"topic": "退货"}))

    llm = aio.AsyncScriptedLLM(responder=responder, latency=lambda n: 0.001 * (n % 7))
    agent = aio.AsyncAgent(llm, [lookup_order, get_policy, track_shipment], name="support", tracer=tracer, hooks=[tracer, prom])

    async def main():
        state["all_in"] = asyncio.Event()
        return await asyncio.gather(*(agent.run(f"q{i}", metadata={"tenant_id": f"t{i % 3}"}) for i in range(50)))

    results = asyncio.run(main())
    assert all(r.ok for r in results) and llm.max_in_flight > 1
    assert observed["in_flight"] == 50 and sample(registry, "agent_runs_in_flight") == 0
    assert mismatches == []

    spans = exporter.get_finished_spans()
    roots = {s.context.trace_id: s for s in spans if s.name == "invoke_agent support"}
    assert len(roots) == 50 and len(spans) == 50 * 6  # 50 条独立 trace，每条：根 + 2 次 chat + 3 个工具
    per_trace: dict = {}
    for s in spans:
        root = roots[s.context.trace_id]  # 每个 span 都能找到自己 trace 的根：没有孤儿，没有串线
        if s is not root:
            assert s.parent.span_id == root.context.span_id, s.name
            per_trace.setdefault(s.context.trace_id, []).append(s)
    for tid, group in per_trace.items():
        tools = [s for s in group if s.name.startswith("execute_tool")]
        assert sorted(t.name for t in tools) == ["execute_tool get_policy", "execute_tool lookup_order", "execute_tool track_shipment"]
        assert max(t.start_time for t in tools) < min(t.end_time for t in tools)  # 三个工具的时间区间有重叠：真的并行
        assert roots[tid].attributes["agentkit.agent.runtime"] == "asyncio"
    for r in results:  # agentkit 这一侧：每个 tool.* 的父就是本次运行的 agent.run，ID 与 OTel 一致
        tool_spans = [c for c in r.trace.children if c.name.startswith("tool.")]
        assert len(tool_spans) == 3 and all(c.parent_id == r.trace.span_id and c.trace_id == r.trace.trace_id for c in tool_spans)
        assert int(r.trace.trace_id, 16) in roots
    assert sample(registry, "agent_tool_calls_total", tool="track_shipment", error_type="none") == 50
    assert sum(sample(registry, "agent_runs_total", status="completed", reason="final_answer", tenant=f"t{i}") for i in range(3)) == 50


def test_async_agent_cancelled_and_timed_out_runs(otel, registry, aio):
    """取消 = 客户端主动断开：不是错误，但在途数必须归零；超时 = 失败：根 span 标 ERROR。"""
    import asyncio

    from opentelemetry.trace import StatusCode

    from agentkit.contrib.otel import OTelTracer, PrometheusHook

    provider, exporter = otel
    tracer = OTelTracer(provider)
    prom = PrometheusHook(registry)
    llm = aio.AsyncScriptedLLM(responder=lambda m: reply("ok"), latency=lambda n: 0.3)
    agent = aio.AsyncAgent(llm, [], name="support", tracer=tracer, hooks=[tracer, prom])
    slow = aio.AsyncAgent(llm, [], name="slow", tracer=tracer, hooks=[tracer, prom], run_timeout=0.05)

    async def main():
        tasks = [asyncio.create_task(agent.run(f"q{i}")) for i in range(20)]
        timed_out = asyncio.create_task(slow.run("x"))
        await asyncio.sleep(0.1)  # 20 个运行都卡在 0.3 秒的模型调用里；超时的那个在 0.05 秒时已经结束
        observed = sample(registry, "agent_runs_in_flight")
        for t in tasks[:10]:
            t.cancel()  # 模拟 10 个 HTTP 客户端断开
        done = await asyncio.gather(*tasks, return_exceptions=True)
        return observed, done, await timed_out

    in_flight_peak, done, timeout_result = asyncio.run(main())
    assert in_flight_peak == 20
    assert sum(isinstance(d, asyncio.CancelledError) for d in done) == 10 and sum(getattr(d, "ok", False) for d in done) == 10
    assert timeout_result.status == "stopped" and timeout_result.stop_reason == "timeout"
    assert sample(registry, "agent_runs_in_flight") == 0  # 被取消的运行也递减了
    assert sample(registry, "agent_runs_total", status="cancelled", reason="cancelled") == 10
    assert sample(registry, "agent_runs_total", status="stopped", reason="timeout") == 1

    roots = [s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent")]
    assert len(roots) == 21 and len({s.context.trace_id for s in roots}) == 21
    cancelled = [s for s in roots if s.attributes["agentkit.agent.status"] == "cancelled"]
    assert len(cancelled) == 10
    for s in cancelled:
        assert s.status.status_code == StatusCode.UNSET and "error.type" not in s.attributes
        assert s.attributes["agentkit.interrupted"] == "CancelledError" and s.attributes["agentkit.agent.stop_reason"] == "cancelled"
    [timeout_root] = [s for s in roots if s.name == "invoke_agent slow"]
    assert timeout_root.status.status_code == StatusCode.ERROR and timeout_root.attributes["error.type"] == "timeout"
    chats = [s for s in exporter.get_finished_spans() if s.name.startswith("chat") and s.attributes.get("agentkit.interrupted")]
    assert len(chats) == 11  # 被取消 / 超时打断的模型调用：标记 interrupted，但不是 ERROR
    assert all(s.status.status_code == StatusCode.UNSET for s in chats)


def test_async_agent_concurrent_approvals_gauge(otel, registry, aio):
    """10 个并发运行都停在高风险工具上等审批 → approvals_pending=10；并发批准后归零，在途数也归零。"""
    import asyncio

    from agentkit.contrib.otel import OTelTracer, PrometheusHook

    provider, exporter = otel
    tracer = OTelTracer(provider)
    prom = PrometheusHook(registry)
    llm = aio.AsyncScriptedLLM(
        responder=lambda m: reply("已退款") if m[-1]["role"] == "tool" else call_tool("refund", order_id="A1"), latency=0.001
    )
    agent = aio.AsyncAgent(llm, [refund], name="support", tracer=tracer, hooks=[tracer, prom, PermissionPolicy()])

    async def main():
        paused = await asyncio.gather(*(agent.run(f"退款 {i}") for i in range(10)))
        mid = sample(registry, "agent_approvals_pending")
        done = await asyncio.gather(*(agent.approve(r.run_id, True, by="lead") for r in paused))
        return paused, mid, done

    paused, mid, done = asyncio.run(main())
    assert all(r.status == "paused" for r in paused) and mid == 10
    assert all(r.ok for r in done)
    assert sample(registry, "agent_approvals_pending") == 0 and sample(registry, "agent_runs_in_flight") == 0
    resumed = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent support" and s.attributes.get("agentkit.resumed")]
    assert {s.attributes["agentkit.run_id"] for s in resumed} == {r.run_id for r in paused}


def test_async_worker_continues_each_job_in_its_own_producer_trace(otel, aio):
    """asyncio worker：50 个任务、并发 10，共用一个 AsyncAgent；每个任务在自己的 task 里 async with continue_trace。"""
    import asyncio

    from agentkit.contrib.otel import OTelTracer, continue_trace, inject_context

    provider, exporter = otel
    tracer = OTelTracer(provider)
    llm = aio.AsyncScriptedLLM(
        responder=lambda m: reply("ok") if m[-1]["role"] == "tool" else call_tool("lookup_order", order_id="A1"),
        latency=lambda n: 0.001 * (n % 5),
    )
    agent = aio.AsyncAgent(llm, [lookup_order], name="worker", tracer=tracer)

    async def main():
        q: asyncio.Queue = asyncio.Queue()
        for i in range(50):
            with tracer.span("send agent-tasks", **{"otel.kind": "producer", "job": i}):
                await q.put({"id": i, "trace": inject_context({})})
        sem = asyncio.Semaphore(10)
        results = {}

        async def handle(job):
            async with sem, continue_trace(job["trace"]):
                results[job["id"]] = await agent.run(f"job {job['id']}")

        await asyncio.gather(*[asyncio.create_task(handle(await q.get())) for _ in range(50)])
        return results

    results = asyncio.run(main())
    spans = exporter.get_finished_spans()
    sends = {s.attributes["job"]: s for s in spans if s.name == "send agent-tasks"}
    roots = {s.context.trace_id: s for s in spans if s.name == "invoke_agent worker"}
    assert len(sends) == len(roots) == 50
    for job, result in results.items():
        send = sends[job]
        root = roots[int(result.trace.trace_id, 16)]
        assert root.context.trace_id == send.context.trace_id and root.parent.span_id == send.context.span_id


# ---------------------------------------------------------------- 3. 传播：线程、队列、进程


def test_inject_extract_roundtrip_and_garbage_carrier_starts_new_trace(otel):
    from opentelemetry import trace

    from agentkit.contrib.otel import continue_trace, current_trace_id, extract_context, inject_context

    provider, _ = otel
    t = provider.get_tracer("t")
    assert inject_context({}) == {} and current_trace_id() is None  # 没有活动 span：什么都不写
    with t.start_as_current_span("producer") as span:
        carrier = inject_context({"other": "kept"})
        tid = hexid(span.get_span_context().trace_id, 32)
        assert current_trace_id() == tid
    version, trace_id, parent_id, flags = carrier["traceparent"].split("-")
    assert (version, trace_id) == ("00", tid) and len(parent_id) == 16 and carrier["other"] == "kept"
    # 最低位 = sampled；OTel Python 1.45 还会置上 W3C Trace Context Level 2 的 random 位（0x02），所以常见的是 "03"
    assert int(flags, 16) & 0x01 == 1
    assert trace.get_current_span(extract_context(carrier)).get_span_context().trace_id == int(tid, 16)

    for garbage in ({"traceparent": "not-a-traceparent"}, {"traceparent": "00-" + "0" * 32 + "-" + "1" * 16 + "-01"}, None):
        with continue_trace(garbage):
            with t.start_as_current_span("worker") as span:
                assert hexid(span.get_span_context().trace_id, 32) != tid  # 格式非法 / 全零 trace-id：开新 trace，不抛异常


def test_continue_trace_across_threads_through_a_queue(otel):
    from agentkit.contrib.otel import OTelTracer, continue_trace, inject_context

    provider, exporter = otel
    tracer = OTelTracer(provider)
    jobs: queue.Queue = queue.Queue()
    results: list = []

    def producer() -> None:
        for i in range(3):
            with tracer.span("send agent-tasks", **{"otel.kind": "producer", "messaging.destination.name": "agent-tasks"}):
                jobs.put(json.dumps({"input": f"A{i}", "trace": inject_context({})}))
        jobs.put(None)

    def worker() -> None:
        while (raw := jobs.get()) is not None:
            job = json.loads(raw)
            with continue_trace(job["trace"]):
                llm = ScriptedLLM([call_tool("lookup_order", order_id=job["input"]), reply("ok")])
                results.append(Agent(llm, [lookup_order], name="worker", tracer=tracer).run(job["input"]))

    threads = [threading.Thread(target=producer), threading.Thread(target=worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spans = exporter.get_finished_spans()
    sends = [s for s in spans if s.name == "send agent-tasks"]
    roots = [s for s in spans if s.name == "invoke_agent worker"]
    assert len(sends) == len(roots) == 3
    for send, root, result in zip(sends, roots, results):
        assert root.context.trace_id == send.context.trace_id and root.parent.span_id == send.context.span_id
        assert root.parent.is_remote and result.trace.trace_id == hexid(send.context.trace_id, 32)
    assert {s.kind.name for s in sends} == {"PRODUCER"}


def test_continue_trace_across_processes(otel):
    """traceparent 只是一个字符串：放进 payload 交给另一个操作系统进程，那边接着同一条 trace 继续。"""
    from agentkit.contrib.otel import OTelTracer, inject_context

    provider, exporter = otel
    tracer = OTelTracer(provider)
    with tracer.span("send agent-tasks", **{"otel.kind": "producer"}) as send:
        payload = json.dumps({"input": "A1", "trace": inject_context({})})

    child = textwrap.dedent(
        """
        import json, sys
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from agentkit import Agent, ScriptedLLM, reply
        from agentkit.contrib.otel import OTelTracer, continue_trace, setup_tracing
        job = json.loads(sys.stdin.read())
        exporter = InMemorySpanExporter()
        tracer = OTelTracer(setup_tracing("worker", exporter=exporter, set_global=False))
        with continue_trace(job["trace"]):
            result = Agent(ScriptedLLM([reply("ok")]), [], name="worker", tracer=tracer).run(job["input"])
        root = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent worker"][0]
        print(json.dumps({"trace_id": format(root.context.trace_id, "032x"),
                          "parent": format(root.parent.span_id, "016x"), "result_trace": result.trace.trace_id}))
        """
    )
    proc = subprocess.run([sys.executable, "-c", child], input=payload, capture_output=True, text=True, cwd=REPO, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["trace_id"] == send.trace_id == out["result_trace"] and out["parent"] == send.span_id


def test_sampling_ratio_zero_still_follows_a_sampled_upstream():
    """头部采样 0%：本服务自己发起的 trace 一条都不导出（agentkit 树照常生成）；但上游已采样的 trace 会被跟随。"""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from agentkit.contrib.otel import OTelTracer, continue_trace, setup_tracing

    exporter = InMemorySpanExporter()
    tracer = OTelTracer(setup_tracing("svc", sample_ratio=0.0, exporter=exporter, set_global=False))
    agent = Agent(ScriptedLLM([reply("a"), reply("b")]), [], name="s", tracer=tracer)
    local = agent.run("x")
    assert exporter.get_finished_spans() == () and local.trace is not None and len(local.trace.trace_id) == 32

    upstream = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}  # W3C 规范里的示例值
    with continue_trace(upstream):
        agent.run("y")
    assert {hexid(s.context.trace_id, 32) for s in exporter.get_finished_spans()} == {"4bf92f3577b34da6a3ce929d0e0e4736"}
    with pytest.raises(ValueError):
        setup_tracing("svc", sample_ratio=1.5, set_global=False)


# ---------------------------------------------------------------- 4. 真实的 OTLP/HTTP 导出与故障


class _OTLPReceiver(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        request = ExportTraceServiceRequest()
        request.ParseFromString(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append((self.path, self.headers.get("Content-Type"), request))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_otlp_http_export_reaches_a_real_receiver():
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    from agentkit.contrib.otel import OTelTracer, setup_tracing

    _OTLPReceiver.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OTLPReceiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        provider = setup_tracing("otlp-test", otlp_endpoint=f"http://127.0.0.1:{server.server_port}/", set_global=False)
        tracer = OTelTracer(provider)
        Agent(ScriptedLLM([reply("ok")]), [], name="support", tracer=tracer).run("hi")
        assert tracer.force_flush()
        provider.shutdown()
    finally:
        server.shutdown()
    path, content_type, request = _OTLPReceiver.received[0]
    assert path == "/v1/traces" and content_type == "application/x-protobuf"
    resource = request.resource_spans[0].resource
    assert {a.key: a.value.string_value for a in resource.attributes}["service.name"] == "otlp-test"
    names = [s.name for rs in request.resource_spans for ss in rs.scope_spans for s in ss.spans]
    assert sorted(names) == ["chat scripted", "invoke_agent support"]


def test_dead_collector_never_breaks_the_agent(monkeypatch):
    """导出端挂了：Agent 照常完成（批量异步导出，失败只丢 span）。注意 force_flush 返回 True 也不代表导出成功。"""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    from agentkit.contrib.otel import OTelTracer, setup_tracing

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1")  # 默认 10 秒，测试里缩短
    provider = setup_tracing("dead", otlp_endpoint="http://127.0.0.1:9", set_global=False)
    tracer = OTelTracer(provider)
    result = Agent(ScriptedLLM([reply("ok")]), [], name="s", tracer=tracer).run("hi")
    assert result.ok and result.trace is not None
    tracer.force_flush(1000)
    provider.shutdown()


# ---------------------------------------------------------------- 5. Prometheus


def test_prometheus_metrics_cover_runs_tokens_cost_tools_and_unknown_tools(registry):
    from agentkit.contrib.otel import PrometheusHook

    prom = PrometheusHook(registry)
    llm = ScriptedLLM([call_tool("lookup_order", order_id="A1"), call_tool("hallucinated_tool_7f3a"), call_tool("track_shipment", tracking_no="x"), reply("ok")])
    Agent(llm, [lookup_order, track_shipment], hooks=[prom]).run("x", metadata={"user_id": "u-1", "tenant_id": "acme"})

    assert sample(registry, "agent_runs_total", status="completed", reason="final_answer") == 1
    assert sample(registry, "agent_run_duration_seconds_count", status="completed") == 1
    assert sample(registry, "agent_llm_tokens_total", direction="input") == 80
    assert sample(registry, "agent_llm_cost_usd_total") == pytest.approx((80 * 1.0 + 40 * 4.0) / 1e6)
    assert sample(registry, "agent_tool_calls_total", tool="__unknown__", error_type="not_found") == 1
    assert sample(registry, "agent_tool_calls_total", tool="track_shipment", error_type="tool_error") == 1
    text = prom.render()
    assert "hallucinated_tool_7f3a" not in text and "u-1" not in text and "acme" not in text  # 未开 tenant_label


def test_tenant_label_is_bounded_and_hook_creation_is_idempotent(registry):
    from agentkit.contrib.otel import PrometheusHook

    for i in range(5):  # 模拟"每个请求 new 一个 Hook"：不能抛 Duplicated timeseries
        prom = PrometheusHook(registry, tenant_label=True, max_tenants=2)
        Agent(ScriptedLLM([reply("ok")]), [], hooks=[prom]).run("x", metadata={"tenant_id": f"t{i}"})
    tenants = {labels["tenant"] for m in registry.collect() if m.name == "agent_runs" for *_, labels in [(0, s.labels) for s in m.samples] if "tenant" in labels}
    assert "__other__" in tenants and len(tenants - {"__other__"}) <= 2
    with pytest.raises(ValueError):
        PrometheusHook(registry, tenant_label=False)  # 同一 namespace 的标签集合不能变
    prom.set_queue_stats("agent-tasks", depth=7, oldest_age_seconds=42.0)
    assert sample(registry, "agent_queue_depth", queue="agent-tasks") == 7
    assert sample(registry, "agent_queue_oldest_job_age_seconds", queue="agent-tasks") == 42.0


def test_start_metrics_server_serves_the_registry(registry):
    from agentkit.contrib.otel import PrometheusHook, start_metrics_server

    prom = PrometheusHook(registry)
    Agent(ScriptedLLM([reply("ok")]), [], hooks=[prom]).run("x")
    server, thread = start_metrics_server(0, registry=registry)
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/metrics", timeout=5).read().decode()
    finally:
        server.shutdown()
        server.server_close()
    assert 'agent_runs_total{reason="final_answer",status="completed"} 1.0' in body and server.server_address[0] == "127.0.0.1"


def test_multiprocess_mode_aggregates_counters_from_concurrent_workers(tmp_path):
    """两个 worker 进程同时跑 Agent、各写各的指标文件；抓取进程用 MultiProcessCollector 汇总。"""
    pytest.importorskip("prometheus_client")
    mp_dir = tmp_path / "prom"
    mp_dir.mkdir()
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(mp_dir)}  # 必须在导入 prometheus_client 之前设置
    worker = textwrap.dedent(
        """
        from agentkit import Agent, ScriptedLLM, reply
        from agentkit.contrib.otel import PrometheusHook
        hook = PrometheusHook()
        for _ in range(25):
            Agent(ScriptedLLM([reply("ok")]), [], hooks=[hook]).run("x")
        hook.set_queue_stats("agent-tasks", depth=3)
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", worker], cwd=REPO, env=env, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for p in procs:
        assert p.wait(timeout=60) == 0, p.stderr.read()

    scraper = textwrap.dedent(
        """
        import urllib.request
        from agentkit.contrib.otel import start_metrics_server
        server, _ = start_metrics_server(0)
        print(urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/metrics", timeout=5).read().decode())
        server.shutdown()
        """
    )
    out = subprocess.run([sys.executable, "-c", scraper], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert 'agent_runs_total{reason="final_answer",status="completed"} 50.0' in out.stdout
    assert "agent_queue_depth" in out.stdout


# ---------------------------------------------------------------- 6. 第 28 课的配置文件：语法 + 与代码一致


LESSON_CONFIGS = REPO / "lessons" / "28_production_observability" / "configs"


def _exposed_metric_names(registry) -> set[str]:
    from agentkit.contrib.otel import PrometheusHook

    prom = PrometheusHook(registry, tenant_label=True)
    llm = ScriptedLLM([call_tool("lookup_order", order_id="A1"), reply("ok")])
    Agent(llm, [lookup_order], hooks=[prom]).run("x", metadata={"tenant_id": "acme"})
    prom.set_queue_stats("q", 1, 1.0)
    return {sample.name for metric in registry.collect() for sample in metric.samples}


def test_prometheus_rules_parse_and_only_use_metrics_the_hook_exposes(registry):
    import re

    yaml = pytest.importorskip("yaml")
    rules = yaml.safe_load((LESSON_CONFIGS / "prometheus-rules.yaml").read_text(encoding="utf-8"))
    exposed = _exposed_metric_names(registry)
    recorded = {r["record"] for g in rules["groups"] for r in g["rules"] if "record" in r}
    alerts = [r for g in rules["groups"] for r in g["rules"] if "alert" in r]
    assert {"AgentErrorBudgetBurn", "AgentErrorBudgetSlowBurn", "AgentLatencyP95High", "AgentCostPerRunSpike",
            "AgentToolErrorRateHigh", "AgentQueueBacklog"} <= {a["alert"] for a in alerts}
    for group in rules["groups"]:
        for rule in group["rules"]:
            expr = rule["expr"]
            assert expr.count("(") == expr.count(")"), rule.get("record") or rule.get("alert")
            for name in re.findall(r"\bagent_[a-z_]+\b", expr):
                assert name in exposed, f"{name} 不是 PrometheusHook 暴露的指标"
            for name in re.findall(r"\bagent:[a-z_:0-9]+", expr):
                assert name in recorded, f"{name} 没有对应的记录规则"
            assert 'le="60"' not in expr  # Prometheus 3 把 le 规范化成浮点写法
            if "alert" in rule:
                assert rule["labels"]["severity"] in {"page", "ticket"}


def test_grafana_dashboard_is_importable_json_and_queries_known_series(registry):
    import re

    yaml = pytest.importorskip("yaml")
    dashboard = json.loads((LESSON_CONFIGS / "grafana-dashboard.json").read_text(encoding="utf-8"))
    rules = yaml.safe_load((LESSON_CONFIGS / "prometheus-rules.yaml").read_text(encoding="utf-8"))
    recorded = {r["record"] for g in rules["groups"] for r in g["rules"] if "record" in r}
    exposed = _exposed_metric_names(registry)
    assert dashboard["id"] is None and dashboard["uid"] and dashboard["schemaVersion"] >= 36
    assert [v["name"] for v in dashboard["templating"]["list"]] == ["datasource"]
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    assert len(exprs) >= 15
    for p in dashboard["panels"]:
        pos = p["gridPos"]
        assert pos["x"] + pos["w"] <= 24
        if p["type"] != "row":
            assert p["datasource"] == {"type": "prometheus", "uid": "${datasource}"}
    for expr in exprs:
        assert expr.count("(") == expr.count(")")
        for name in re.findall(r"\bagent_[a-z_]+\b", expr):
            assert name in exposed, name
        for name in re.findall(r"\bagent:[a-z_:0-9]+", expr):
            assert name in recorded, name


def test_collector_config_pipelines_reference_defined_components():
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load((LESSON_CONFIGS / "otel-collector.yaml").read_text(encoding="utf-8"))
    pipelines = cfg["service"]["pipelines"]
    connectors = set(cfg.get("connectors", {}))
    for name, p in pipelines.items():
        for r in p["receivers"]:
            assert r in cfg["receivers"] or r in connectors, (name, r)
        for proc in p.get("processors", []):
            assert proc in cfg["processors"], (name, proc)
        for e in p["exporters"]:
            assert e in cfg["exporters"] or e in connectors, (name, e)
    # 顺序：memory_limiter 第一；尾部采样在脱敏之后；batch 在采样之后
    first = pipelines["traces/in"]["processors"]
    assert first[0] == "memory_limiter" and first.index("redaction") < first.index("tail_sampling")
    assert "attributes/strip-content" in pipelines["traces/ops"]["processors"]
    policies = {p["type"] for p in cfg["processors"]["tail_sampling"]["policies"]}
    assert {"status_code", "latency", "probabilistic"} <= policies
    # 新名字：otlp_grpc / otlp_http（旧名 otlp / otlphttp 已弃用）；密钥只能来自环境变量
    assert set(cfg["exporters"]) == {"otlp_grpc/tempo", "otlp_http/langfuse", "debug"}
    text = (LESSON_CONFIGS / "otel-collector.yaml").read_text(encoding="utf-8")
    assert "${env:LANGFUSE_AUTH}" in text and "${env:REDACTION_HMAC_KEY}" in text and "sk-lf-" not in text.replace("sk-lf-...", "")
    blocked_keys = cfg["processors"]["redaction"]["blocked_key_patterns"]
    assert not any("token" in pattern for pattern in blocked_keys)  # 会误伤 gen_ai.usage.*_tokens
