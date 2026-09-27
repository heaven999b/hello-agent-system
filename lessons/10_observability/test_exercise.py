"""第 10 课练习测试：离线、确定性。span 数据全部手工构造，最后一个测试用真实的 Agent + jsonl_exporter 做集成验证。

运行：make lesson N=10
"""

from __future__ import annotations

import json
import random

import pytest

from agentkit import Agent, ScriptedLLM, ToolError, Tracer, call_tool, jsonl_exporter, reply, tool
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


def span(name, trace, sid, parent=None, dur=100.0, start=0.0, status="ok", **attrs):
    """构造一个和 Span.to_dict() 同结构的字典。attrs 里的 '__' 会被替换成 '.'，方便写 gen_ai__usage__input_tokens。"""
    return {
        "name": name,
        "trace_id": trace,
        "span_id": sid,
        "parent_id": parent,
        "start": start,
        "end": start + dur / 1000,
        "duration_ms": dur,
        "status": status,
        "attrs": {k.replace("__", "."): v for k, v in attrs.items()},
    }


def run_root(trace, sid, dur, status="completed", steps=2, in_tok=0, out_tok=0):
    return span("agent.run", trace, sid, None, dur, **{
        "agent__status": status, "agent__steps": steps,
        "gen_ai__usage__input_tokens": in_tok, "gen_ai__usage__output_tokens": out_tok,
    })


def llm(trace, sid, parent, in_tok, out_tok, dur=50.0, start=0.0):
    return span("llm.chat", trace, sid, parent, dur, start,
                **{"gen_ai__usage__input_tokens": in_tok, "gen_ai__usage__output_tokens": out_tok})


def tool_span(trace, sid, parent, name, ok=True, dur=20.0, status="ok", start=0.0):
    attrs = {"tool__name": name}
    if ok is not None:
        attrs["tool__ok"] = ok
    return span(f"tool.{name}", trace, sid, parent, dur, start, status, **attrs)


@pytest.fixture
def three_runs():
    """3 次运行：t1 成功（查订单 + 查物流），t2 成功（直接回答），t3 达到步数上限（物流工具两次失败）。"""
    return [
        run_root("t1", "r1", 1000, steps=3, in_tok=300, out_tok=30),
        llm("t1", "a", "r1", 100, 10),
        tool_span("t1", "b", "r1", "lookup_order"),
        llm("t1", "c", "r1", 100, 10),
        tool_span("t1", "d", "r1", "track_shipment"),
        llm("t1", "e", "r1", 100, 10),
        run_root("t2", "r2", 3000, steps=1, in_tok=50, out_tok=5),
        llm("t2", "f", "r2", 50, 5),
        run_root("t3", "r3", 2000, status="max_steps", steps=2, in_tok=400, out_tok=40),
        llm("t3", "g", "r3", 200, 20),
        tool_span("t3", "h", "r3", "track_shipment", ok=False),
        llm("t3", "i", "r3", 200, 20),
        tool_span("t3", "j", "r3", "track_shipment", ok=False),
    ]


# ------------------------------------------------------------------ percentile


def test_percentile_nearest_rank_textbook_example():
    values = [15, 20, 35, 40, 50]
    assert ex.percentile(values, 30) == 20
    assert ex.percentile(values, 40) == 20
    assert ex.percentile(values, 50) == 35
    assert ex.percentile(values, 100) == 50
    assert ex.percentile(values, 0) == 15  # p=0 取最小值


def test_percentile_edge_cases():
    assert ex.percentile([], 50) is None
    assert ex.percentile([7.5], 95) == 7.5
    assert ex.percentile([50, 15, 40, 20, 35], 50) == 35  # 输入无序也要对
    # p95 在小样本下就是最大值：20 个样本时 rank=ceil(19)=19 → 第 19 小的值
    assert ex.percentile(list(range(1, 21)), 95) == 19
    # 浮点陷阱：7 / 100 * 100 == 7.000000000000001，ceil 之后会错成 8
    assert ex.percentile(list(range(1, 101)), 7) == 7
    with pytest.raises(ValueError):
        ex.percentile([1, 2, 3], 101)
    with pytest.raises(ValueError):
        ex.percentile([1, 2, 3], -1)


# ------------------------------------------------------------------ compute_metrics


def test_runs_and_success_rate(three_runs):
    m = ex.compute_metrics(three_runs)
    assert m["runs"] == 3
    assert m["success_rate"] == pytest.approx(2 / 3)


def test_latency_percentiles(three_runs):
    m = ex.compute_metrics(three_runs)
    assert m["p50_ms"] == 2000  # [1000, 2000, 3000] → rank=ceil(1.5)=2
    assert m["p95_ms"] == 3000


def test_tokens_counted_from_llm_spans_only(three_runs):
    """根 span 上的 gen_ai.usage.* 是汇总值，再加一遍就重复统计了。"""
    m = ex.compute_metrics(three_runs)
    assert m["total_tokens"] == 3 * 110 + 55 + 2 * 220  # = 825，而不是 1650


def test_tool_stats_and_error_rate(three_runs):
    tools = ex.compute_metrics(three_runs)["tools"]
    assert tools["lookup_order"] == {"calls": 1, "errors": 0, "error_rate": 0.0}
    assert tools["track_shipment"]["calls"] == 3
    assert tools["track_shipment"]["errors"] == 2
    assert tools["track_shipment"]["error_rate"] == pytest.approx(2 / 3)


def test_average_steps(three_runs):
    assert ex.compute_metrics(three_runs)["avg_steps"] == pytest.approx((3 + 1 + 2) / 3)


def test_empty_input():
    m = ex.compute_metrics([])
    assert m["runs"] == 0 and m["success_rate"] == 0.0 and m["avg_steps"] == 0.0
    assert m["p50_ms"] is None and m["p95_ms"] is None
    assert m["total_tokens"] == 0 and m["tools"] == {}


def test_nested_agent_run_and_resume_are_not_counted_as_runs():
    """多 Agent：子 Agent 的 agent.run 挂在父 Agent 的工具 span 下面；agent.resume 是同一次运行的延续。"""
    spans = [
        run_root("t1", "r1", 5000, steps=2),
        tool_span("t1", "x", "r1", "ask_expert"),
        span("agent.run", "t1", "sub", "x", 3000, **{"agent__status": "completed", "agent__steps": 4}),
        span("agent.resume", "t9", "rs", None, 800, **{"agent__status": "completed", "agent__steps": 3}),
    ]
    m = ex.compute_metrics(spans)
    assert m["runs"] == 1 and m["avg_steps"] == 2 and m["p50_ms"] == 5000


def test_tool_crash_without_ok_attr_counts_as_error_and_name_fallback():
    """工具抛异常时 span.status == 'error'，可能根本来不及写 tool.ok；attrs 里没有 tool.name 时从 span 名推断。"""
    spans = [
        run_root("t1", "r1", 100),
        tool_span("t1", "a", "r1", "flaky", ok=None, status="error"),
        span("tool.legacy_api", "t1", "b", "r1", 10),  # 没有任何 attrs
        llm("t1", "c", "r1", 0, 0) | {"attrs": {}},  # 模型报错时 llm.chat 可能没有 token 属性
    ]
    m = ex.compute_metrics(spans)
    assert m["tools"]["flaky"] == {"calls": 1, "errors": 1, "error_rate": 1.0}
    assert m["tools"]["legacy_api"]["calls"] == 1 and m["tools"]["legacy_api"]["errors"] == 0
    assert m["total_tokens"] == 0


def test_order_does_not_matter(three_runs):
    shuffled = three_runs[:]
    random.Random(7).shuffle(shuffled)
    assert ex.compute_metrics(shuffled) == ex.compute_metrics(three_runs)


# ------------------------------------------------------------------ slowest_path


@pytest.fixture
def deep_trace():
    #  agent.run (3000)
    #  ├─ llm.chat (800)
    #  ├─ tool.ask_expert (1900)
    #  │   └─ agent.run[expert] (1850)
    #  │       ├─ llm.chat (400)
    #  │       └─ tool.search_kb (1400)
    #  └─ llm.chat (300)
    return [
        span("tool.search_kb", "T", "s5", "s4", 1400, start=1.5),  # 故意把子节点放在前面
        span("agent.run", "T", "root", None, 3000, start=0.0),
        span("llm.chat", "T", "s1", "root", 800, start=0.0),
        span("tool.ask_expert", "T", "s2", "root", 1900, start=0.8),
        span("agent.run", "T", "s4", "s2", 1850, start=0.82),
        span("llm.chat", "T", "s6", "s4", 400, start=0.85),
        span("llm.chat", "T", "s3", "root", 300, start=2.7),
        span("agent.run", "OTHER", "o1", None, 99999),  # 另一个 trace，不能混进来
    ]


def test_slowest_path_follows_longest_child(deep_trace):
    assert ex.slowest_path(deep_trace, "T") == ["agent.run", "tool.ask_expert", "agent.run", "tool.search_kb"]


def test_slowest_path_single_span_and_unknown_trace(deep_trace):
    assert ex.slowest_path(deep_trace, "OTHER") == ["agent.run"]
    assert ex.slowest_path(deep_trace, "nope") == []
    assert ex.slowest_path([], "T") == []


def test_slowest_path_tie_breaks_by_earliest_start():
    spans = [
        span("agent.run", "T", "r", None, 1000, start=0.0),
        span("tool.b_later", "T", "b", "r", 400, start=0.5),
        span("tool.a_earlier", "T", "a", "r", 400, start=0.1),
    ]
    assert ex.slowest_path(spans, "T") == ["agent.run", "tool.a_earlier"]


# ------------------------------------------------------------------ 集成：真实 Agent 导出的数据


def test_works_on_real_jsonl_export(tmp_path):
    @tool
    def lookup_order(order_id: str) -> str:
        """查询订单"""
        return "已发货"

    @tool
    def track_shipment(tracking_no: str) -> str:
        """查询物流"""
        raise ToolError("物流接口超时")

    path = tmp_path / "traces.jsonl"
    tracer = Tracer(exporter=jsonl_exporter(path))
    scripts = [
        [call_tool("lookup_order", order_id="A1", input_tokens=100, output_tokens=10),
         call_tool("track_shipment", tracking_no="SF1", input_tokens=150, output_tokens=10),
         reply("物流暂时查不到", input_tokens=200, output_tokens=20)],
        [reply("你好", input_tokens=30, output_tokens=5)],
    ]
    for script in scripts:
        Agent(ScriptedLLM(script), [lookup_order, track_shipment], tracer=tracer).run("x")

    spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    m = ex.compute_metrics(spans)
    assert m["runs"] == 2 and m["success_rate"] == 1.0
    assert m["total_tokens"] == 110 + 160 + 220 + 35
    assert m["avg_steps"] == 2.0
    assert m["tools"]["track_shipment"] == {"calls": 1, "errors": 1, "error_rate": 1.0}
    first_trace = spans[0]["trace_id"]
    assert ex.slowest_path(spans, first_trace)[0] == "agent.run"


def test_nested_sub_agent_from_real_export(tmp_path):
    """多 Agent：主管通过 agent_as_tool 调用专家，两者共用一个 Tracer。
    专家的 agent.run 嵌套在主管的 tool span 下 —— 只算 1 次运行，但专家的 token 要算进总数。"""
    from agentkit.workflows import agent_as_tool

    path = tmp_path / "traces.jsonl"
    tracer = Tracer(exporter=jsonl_exporter(path))
    expert = Agent(ScriptedLLM([reply("专家答复", input_tokens=40, output_tokens=4)]), [], name="expert", tracer=tracer)
    boss = Agent(ScriptedLLM([call_tool("ask_expert", task="x", input_tokens=100, output_tokens=10),
                              reply("完成", input_tokens=120, output_tokens=12)]),
                 [agent_as_tool(expert, "ask_expert", "专家")], name="boss", tracer=tracer)
    boss.run("hi")

    spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    m = ex.compute_metrics(spans)
    assert m["runs"] == 1
    assert m["total_tokens"] == 110 + 132 + 44
    assert m["tools"]["ask_expert"]["calls"] == 1
    path_names = ex.slowest_path(spans, spans[0]["trace_id"])
    assert path_names[0] == "agent.run" and len(path_names) >= 2
