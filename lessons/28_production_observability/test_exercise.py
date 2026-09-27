"""第 28 课练习测试：离线、确定性，不需要 opentelemetry / prometheus_client。

运行：make lesson N=28
"""

from __future__ import annotations

import pytest

from agentkit import Agent, PermissionPolicy, ScriptedLLM, ToolError, Tracer, call_tool, reply, tool
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# ---------------------------------------------------------------- (a) to_genai_attributes


def test_agent_run_becomes_invoke_agent_with_namespaced_business_attrs():
    name, attrs = ex.to_genai_attributes(
        "agent.run",
        {"agent.name": "support", "run_id": "r-1", "tenant.id": "acme", "agent.status": "completed", "agent.steps": 3,
         "gen_ai.usage.input_tokens": 60},
    )
    assert name == "invoke_agent support"
    assert attrs == {
        "gen_ai.agent.name": "support",
        "agentkit.run_id": "r-1",
        "agentkit.tenant.id": "acme",
        "agentkit.agent.status": "completed",
        "agentkit.agent.steps": 3,
        "gen_ai.usage.input_tokens": 60,
        "gen_ai.operation.name": "invoke_agent",
    }
    assert ex.to_genai_attributes("agent.run", {})[0] == "invoke_agent"  # 没有 agent 名
    for status in ("failed", "max_steps"):
        assert ex.to_genai_attributes("agent.run", {"agent.status": status})[1]["error.type"] == status
    timeout = {"agent.status": "stopped", "agent.stop_reason": "timeout"}
    assert ex.to_genai_attributes("agent.run", timeout)[1]["error.type"] == "timeout"
    not_failures = [("paused", "needs_approval"), ("cancelled", "cancelled"), ("stopped", "budget_exceeded"), ("stopped", "rate_limited")]
    for status, reason in not_failures:  # 等审批、客户端断开、预算中止、限流都不标 ERROR
        _, attrs = ex.to_genai_attributes("agent.run", {"agent.status": status, "agent.stop_reason": reason})
        assert "error.type" not in attrs and attrs["agentkit.agent.stop_reason"] == reason


def test_llm_chat_becomes_client_chat_span_with_provider_and_finish_reasons():
    name, attrs = ex.to_genai_attributes(
        "llm.chat",
        {"gen_ai.request.model": "gpt-5.5", "step": 2, "messages": 5, "finish_reason": "length",
         "gen_ai.usage.reasoning_tokens": 12, "result": "final_answer"},
    )
    assert name == "chat gpt-5.5"
    assert attrs["gen_ai.operation.name"] == "chat" and attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.response.finish_reasons"] == ["length"]
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 12 and "gen_ai.usage.reasoning_tokens" not in attrs
    assert attrs["agentkit.step"] == 2 and attrs["agentkit.messages"] == 5 and attrs["agentkit.result"] == "final_answer"
    name, attrs = ex.to_genai_attributes("llm.chat", {"gen_ai.request.model": "?"}, provider_name="anthropic")
    assert name == "chat" and "gen_ai.request.model" not in attrs and attrs["gen_ai.provider.name"] == "anthropic"


def test_tool_span_error_type_but_policy_denial_is_not_a_failure():
    name, attrs = ex.to_genai_attributes("tool.track", {"tool.name": "track", "tool.ok": False, "tool.error_type": "timeout"})
    assert name == "execute_tool track"
    assert attrs == {
        "gen_ai.tool.name": "track",
        "agentkit.tool.ok": False,
        "agentkit.tool.error_type": "timeout",
        "gen_ai.tool.type": "function",
        "error.type": "timeout",
        "gen_ai.operation.name": "execute_tool",
    }
    _, denied = ex.to_genai_attributes("tool.refund", {"tool.name": "refund", "tool.ok": False, "tool.error_type": "denied"})
    assert "error.type" not in denied
    name, attrs = ex.to_genai_attributes("tool.lookup", {"tool.ok": True, "tool.risk": "read"})  # 工具名从 span 名里取
    assert name == "execute_tool lookup" and attrs["gen_ai.tool.name"] == "lookup" and "error.type" not in attrs
    _, unknown = ex.to_genai_attributes("tool.x", {"tool.ok": False})
    assert unknown["error.type"] == "_OTHER"


def test_privacy_content_user_id_error_and_none_never_become_attributes():
    attrs_in = {
        "tool.name": "find_orders", "tool.arguments": '{"phone": "13812345678"}', "tool.result_preview": "张伟...",
        "gen_ai.input.messages": "[...]", "gen_ai.tool.call.result": "{...}", "user.id": "u-42",
        "error": "ValueError: 13812345678", "tool.risk": None,
    }
    before = dict(attrs_in)
    _, attrs = ex.to_genai_attributes("tool.find_orders", attrs_in)
    assert attrs_in == before  # 不修改传入的字典
    flat = repr(attrs)
    assert "13812345678" not in flat and "u-42" not in flat and "张伟" not in flat
    assert set(attrs) == {"gen_ai.tool.name", "gen_ai.tool.type", "gen_ai.operation.name"}


def test_resume_renames_cumulative_tokens_and_unknown_spans_keep_user_attrs():
    name, attrs = ex.to_genai_attributes(
        "agent.resume", {"agent.name": "support", "gen_ai.usage.input_tokens": 40, "gen_ai.usage.output_tokens": 20}
    )
    assert name == "invoke_agent support" and attrs["agentkit.resumed"] is True
    assert attrs["agentkit.run.cumulative_input_tokens"] == 40 and attrs["agentkit.run.cumulative_output_tokens"] == 20
    assert "gen_ai.usage.input_tokens" not in attrs
    name, attrs = ex.to_genai_attributes("request", {"tenant.id": "acme", "app.feature": "faq", "agentkit.x": 1})
    assert name == "request" and attrs == {"agentkit.tenant.id": "acme", "app.feature": "faq", "agentkit.x": 1}


def test_mapping_matches_contrib_on_spans_from_a_real_agent_run():
    """用真实 Agent 跑一遍（查询成功、工具失败、权限拒绝、达到步数上限），逐个 span 与 agentkit.contrib.otel 对比。"""
    from agentkit.contrib.otel import to_genai_attributes as reference  # 纯 Python，不需要 opentelemetry

    @tool
    def lookup(order_id: str) -> dict:
        """查订单"""
        return {"order_id": order_id}

    @tool
    def track(no: str) -> str:
        """查物流"""
        raise ToolError("超时")

    @tool(risk="dangerous")
    def refund(order_id: str) -> str:
        """退款"""
        return "ok"

    tracer = Tracer()
    llm = ScriptedLLM([call_tool("lookup", order_id="A1"), call_tool("track", no="S1"), call_tool("refund", order_id="A1"), reply("好")])
    Agent(llm, [lookup, track, refund], name="support", tracer=tracer, hooks=[PermissionPolicy(deny_tools={"refund"})]).run(
        "x", metadata={"tenant_id": "acme", "user_id": "u-1"}
    )
    Agent(ScriptedLLM([call_tool("nope"), call_tool("nope")]), [], name="looper", tracer=tracer, max_steps=2).run("y")
    spans = [s for root in tracer.traces for s in root.walk()]
    assert len(spans) == 8 + 5
    for s in spans:
        assert ex.to_genai_attributes(s.name, dict(s.attrs)) == reference(s.name, dict(s.attrs)), s.name


# ---------------------------------------------------------------- (b) 燃烧率与多窗口告警


def test_burn_rate_definition_edges_and_validation():
    assert ex.burn_rate(30, 1000, 0.99) == pytest.approx(3.0)
    assert ex.burn_rate(10, 1000, 0.99) == pytest.approx(1.0)  # 刚好在周期末用完预算
    assert ex.burn_rate(0, 0, 0.99) == 0.0  # 没有流量
    assert ex.burn_rate(100, 100, 0.95) == pytest.approx(20.0)  # 95% SLO 的燃烧率最多只有 20
    for bad in [(1, 10, 1.0), (1, 10, 0.0), (1, 10, 1.5), (11, 10, 0.99), (-1, 10, 0.99), (0, -1, 0.99)]:
        with pytest.raises(ValueError):
            ex.burn_rate(*bad)


def test_page_needs_both_long_and_short_window_above_threshold():
    # 真故障：过去 1 小时和最近 5 分钟都在高速烧预算
    assert ex.should_page({"5m": 20.0, "30m": 1.0}, {"1h": 15.0, "6h": 1.0}) is True
    # 已经修好：1 小时平均值还很高，但最近 5 分钟恢复了 → 不再叫人（缩短 reset time）
    assert ex.should_page({"5m": 0.5, "30m": 1.0}, {"1h": 15.0, "6h": 1.0}) is False
    # 一次短暂抖动：5 分钟很高，但 1 小时平均不高 → 不叫人（提高 precision）
    assert ex.should_page({"5m": 40.0, "30m": 2.0}, {"1h": 3.0, "6h": 1.0}) is False
    # 慢速燃烧：第二条规则（6h / 30m，阈值 6）
    assert ex.should_page({"5m": 7.0, "30m": 7.0}, {"1h": 7.0, "6h": 6.5}) is True


def test_page_uses_strict_comparison_missing_data_and_custom_thresholds():
    assert ex.should_page({"5m": 14.4}, {"1h": 14.4}) is False  # 严格大于
    assert ex.should_page({}, {"1h": 99.0, "6h": 99.0}) is False  # 短窗口没有数据 → 不告警
    ticket = (("3d", "6h", 1.0),)  # SRE Workbook 里开工单（ticket）的那条规则
    assert ex.should_page({"6h": 1.2}, {"3d": 1.1}, ticket) is True
    assert ex.should_page({"6h": 1.2}, {"3d": 1.1}) is False  # 默认规则只包含 page


def test_page_threshold_means_a_fixed_fraction_of_a_30_day_budget():
    """把燃烧率换算成"烧掉了多少预算"：14.4 持续 1h = 2%，6 持续 6h = 5%（SRE Workbook 表 5-8）。"""
    slo, per_hour = 0.999, 100_000
    period_hours = 30 * 24
    rate = ex.burn_rate(int(per_hour * 0.001 * 14.4), per_hour, slo)
    assert rate == pytest.approx(14.4)
    assert rate * 1 / period_hours == pytest.approx(0.02)
    assert ex.burn_rate(600, per_hour, slo) * 6 / period_hours == pytest.approx(0.05)
    # 燃烧率 7：快速规则（>14.4）不触发，慢速规则（>6）触发 —— 同一次事故，由哪条规则叫人取决于它烧得多快
    slow = ex.burn_rate(700, per_hour, slo)
    assert ex.should_page({"5m": slow, "30m": slow}, {"1h": slow, "6h": slow}) is True
    assert ex.should_page({"5m": slow}, {"1h": slow}) is False


# ---------------------------------------------------------------- (c) 标签基数


def test_guard_rejects_high_cardinality_and_unknown_label_names_atomically():
    seen: dict = {}
    for name in ("user_id", "run_id", "trace_id"):
        with pytest.raises(ValueError, match=name):
            ex.guard_label_cardinality({"status": "ok", name: "x"}, {"status", name}, {}, seen)
    with pytest.raises(ValueError, match="region"):
        ex.guard_label_cardinality({"tenant": "acme", "region": "cn"}, {"tenant"}, {"tenant": 5}, seen)
    assert seen == {}  # 出错时不留下半截状态


def test_guard_caps_distinct_values_across_calls_with_overflow_bucket():
    seen: dict = {}
    allowed, caps = {"tenant", "status"}, {"tenant": 2}
    got = [ex.guard_label_cardinality({"tenant": t, "status": "completed"}, allowed, caps, seen)["tenant"] for t in "abca"]
    assert got == ["a", "b", ex.OVERFLOW, "a"]
    assert seen == {"tenant": {"a", "b"}}
    # status 没有上限：多少个取值都放行
    statuses = {ex.guard_label_cardinality({"status": s}, allowed, caps, seen)["status"] for s in ("completed", "failed", "paused")}
    assert statuses == {"completed", "failed", "paused"}


def test_guard_normalizes_values_and_does_not_mutate_input():
    labels = {"tenant": None, "status": 200}
    out = ex.guard_label_cardinality(labels, {"tenant", "status"}, {"tenant": 1})
    assert out == {"tenant": "unknown", "status": "200"} and labels == {"tenant": None, "status": 200}
    assert ex.guard_label_cardinality({"tenant": ""}, {"tenant"}, {"tenant": 1}) == {"tenant": "unknown"}
