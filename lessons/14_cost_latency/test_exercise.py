"""第 14 课练习测试：离线、确定性（ScriptedLLM，不调用任何真实模型）。

运行：make lesson N=14    或    .venv/bin/python -m pytest lessons/14_cost_latency -v
"""

from __future__ import annotations

import json
import os
import string
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit import (
    Agent,
    LLMError,
    PermissionPolicy,
    ScriptedLLM,
    Tracer,
    call_tool,
    jsonl_exporter,
    reply,
    tool,
)
from agentkit.testing import load_exercise

ex = load_exercise(__file__)

ROOT = Path(__file__).resolve().parents[2]

MSGS = [
    {"role": "system", "content": "你是 IT 服务台助手。"},
    {"role": "user", "content": "VPN 连不上怎么办？"},
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "搜索知识库",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    }
]


def reorder(obj):
    """递归地把所有 dict 的键倒序重排（内容不变，只改键的顺序）。"""
    if isinstance(obj, dict):
        return {k: reorder(obj[k]) for k in reversed(list(obj))}
    if isinstance(obj, list):
        return [reorder(x) for x in obj]
    return obj


# =====================================================================
# (a) cache_key
# =====================================================================


def test_cache_key_is_sha256_hex_and_deterministic():
    k1 = ex.cache_key(MSGS, TOOLS, "gpt-x", "acme")
    k2 = ex.cache_key(MSGS, TOOLS, "gpt-x", "acme")
    assert k1 == k2
    assert len(k1) == 64 and set(k1) <= set(string.hexdigits.lower())


def test_cache_key_ignores_dict_key_order():
    k1 = ex.cache_key(MSGS, TOOLS, "gpt-x", "acme")
    k2 = ex.cache_key(reorder(MSGS), reorder(TOOLS), "gpt-x", "acme")
    assert k1 == k2, "dict 的键顺序不同但内容相同，应该得到同一个键（记得 sort_keys=True）"


def test_cache_key_isolates_tenants_models_and_tools():
    base = ex.cache_key(MSGS, TOOLS, "gpt-x", "acme")
    assert ex.cache_key(MSGS, TOOLS, "gpt-x", "globex") != base, "不同租户必须得到不同的键，否则会跨租户泄露"
    assert ex.cache_key(MSGS, TOOLS, "gpt-y", "acme") != base, "换了模型不能命中旧模型的缓存"
    assert ex.cache_key(MSGS, None, "gpt-x", "acme") != base, "可用工具不同，键也要不同"


def test_cache_key_has_no_boundary_ambiguity():
    # 用字符串直接拼接时，"ab"+"c" 和 "a"+"bc" 会撞成同一个键
    assert ex.cache_key(MSGS, None, "c", "ab") != ex.cache_key(MSGS, None, "bc", "a")


def test_cache_key_is_sensitive_to_content_and_order():
    base = ex.cache_key(MSGS, None, "gpt-x", "acme")
    changed = [MSGS[0], {"role": "user", "content": "VPN 连不上怎么办?"}]  # 全角问号 → 半角问号
    assert ex.cache_key(changed, None, "gpt-x", "acme") != base, "精确缓存：内容差一个字符也是不同的请求"
    assert ex.cache_key(list(reversed(MSGS)), None, "gpt-x", "acme") != base, "消息顺序有意义，不能排序"


def test_cache_key_treats_none_tools_as_empty():
    assert ex.cache_key(MSGS, None, "gpt-x", "acme") == ex.cache_key(MSGS, [], "gpt-x", "acme")


def test_cache_key_requires_tenant():
    with pytest.raises(ValueError):
        ex.cache_key(MSGS, None, "gpt-x", "")
    with pytest.raises(ValueError):
        ex.cache_key(MSGS, None, "gpt-x", None)


def test_cache_key_is_stable_across_processes():
    """多个服务实例共享一个 Redis 缓存：同一个请求在不同进程里必须算出同一个键（所以不能用内置 hash()）。"""
    here = ex.cache_key(MSGS, TOOLS, "gpt-x", "acme")
    code = (
        "import json, sys\n"
        "from agentkit.testing import load_exercise\n"
        f"ex = load_exercise({str(Path(__file__).resolve())!r})\n"
        "msgs, tools = json.loads(sys.argv[1]), json.loads(sys.argv[2])\n"
        "print(ex.cache_key(msgs, tools, 'gpt-x', 'acme'))\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": "12345"}  # 换一个哈希种子，模拟"另一台机器上的进程"
    out = subprocess.run(
        [sys.executable, "-c", code, json.dumps(MSGS, ensure_ascii=False), json.dumps(TOOLS)],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == here


# =====================================================================
# (b) CascadeLLM
# =====================================================================


def is_json_object(messages, response) -> bool:
    try:
        return isinstance(json.loads(response.content or ""), dict)
    except json.JSONDecodeError:
        return False


def test_cascade_uses_small_model_when_valid():
    small = ScriptedLLM([reply('{"category": "network"}')], model="small")
    large = ScriptedLLM([], model="large")  # 剧本为空：一旦被调用就会报错
    llm = ex.CascadeLLM(small, large, is_json_object)
    r = llm.chat(MSGS)
    assert r.content == '{"category": "network"}'
    assert len(large.calls) == 0
    assert (llm.calls, llm.escalations, llm.escalation_rate) == (1, 0, 0.0)
    assert llm.wasted.total == 0


def test_cascade_escalates_when_rejected_and_counts_wasted_tokens():
    small = ScriptedLLM([reply("我觉得大概是网络问题吧", input_tokens=100, output_tokens=30)], model="small")
    large = ScriptedLLM([reply('{"category": "network"}')], model="large")
    llm = ex.CascadeLLM(small, large, is_json_object)
    r = llm.chat(MSGS)
    assert r.content == '{"category": "network"}'
    assert len(small.calls) == 1 and len(large.calls) == 1
    assert large.calls[0]["messages"] == MSGS, "升级时要把同样的 messages 原样发给大模型"
    assert llm.escalations == 1 and llm.reasons == {"rejected": 1}
    assert (llm.wasted.input_tokens, llm.wasted.output_tokens) == (100, 30), "被丢弃的小模型回答也花了钱"


def test_cascade_escalates_when_small_model_errors():
    small = ScriptedLLM([LLMError("429 rate limited", status_code=429, retryable=True)], model="small")
    large = ScriptedLLM([reply('{"ok": true}')], model="large")
    llm = ex.CascadeLLM(small, large, is_json_object)
    assert llm.chat(MSGS).content == '{"ok": true}'
    assert llm.reasons == {"error": 1}
    assert llm.wasted.total == 0, "小模型没返回任何东西，没有'白花的 token'"


def test_cascade_treats_validator_crash_as_rejection():
    def buggy_validator(messages, response):
        raise KeyError("confidence")  # 校验器自己有 bug

    small = ScriptedLLM([reply("x", input_tokens=5, output_tokens=5)], model="small")
    large = ScriptedLLM([reply("from large")], model="large")
    llm = ex.CascadeLLM(small, large, buggy_validator)
    assert llm.chat(MSGS).content == "from large"
    assert llm.reasons == {"validator_error": 1}
    assert llm.wasted.total == 10


def test_cascade_escalation_rate_and_large_failure_propagates():
    small = ScriptedLLM([reply("{}"), reply("not json"), reply("{}"), reply("{}"), reply("also not json")], model="small")
    large = ScriptedLLM([reply('{"fixed": 1}'), LLMError("503 unavailable", status_code=503, retryable=True)], model="large")
    llm = ex.CascadeLLM(small, large, is_json_object)
    for _ in range(4):
        llm.chat(MSGS)
    assert llm.calls == 4 and llm.escalations == 1
    assert llm.escalation_rate == pytest.approx(0.25)
    with pytest.raises(LLMError):  # 大模型也失败：不能吞掉异常，更不能偷偷返回不合格的小模型答案
        llm.chat(MSGS)
    assert llm.calls == 5 and llm.escalations == 2


@tool
def get_weather(city: str) -> str:
    """查询城市天气"""
    return f"{city}：晴，25°C"


def test_cascade_inside_agent_fixes_hallucinated_tool():
    """级联对 Agent 透明：小模型编造了不存在的工具 → 校验不过 → 这一步升级到大模型；下一步小模型又能胜任。"""
    allowed = {"get_weather"}

    def tool_names_exist(messages, response) -> bool:
        return all(c.name in allowed for c in response.tool_calls)

    small = ScriptedLLM([call_tool("delete_everything"), reply("北京今天晴，25°C。")], model="small")
    large = ScriptedLLM([call_tool("get_weather", city="北京")], model="large")
    llm = ex.CascadeLLM(small, large, tool_names_exist)
    res = Agent(llm, [get_weather]).run("北京天气怎么样？")
    assert res.status == "completed" and res.output == "北京今天晴，25°C。"
    assert res.tools_called() == ["get_weather"]
    assert llm.calls == 2 and llm.escalations == 1 and llm.escalation_rate == pytest.approx(0.5)


# =====================================================================
# (c) cost_by_tenant
# =====================================================================


def span(name, trace, sid, parent=None, **attrs):
    return {"name": name, "trace_id": trace, "span_id": sid, "parent_id": parent, "attrs": attrs}


def test_cost_by_tenant_joins_root_tenant_by_trace_id():
    spans = [
        span("request", "t1", "r1", **{"tenant.id": "acme"}),
        span("agent.run", "t1", "a1", "r1", run_id="run1", **{"agent.cost_usd": 0.001}),
        span("llm.chat", "t1", "l1", "a1", **{"gen_ai.usage.input_tokens": 100}),
        span("request", "t2", "r2", **{"tenant.id": "acme"}),
        span("agent.run", "t2", "a2", "r2", run_id="run2", **{"agent.cost_usd": 0.002}),
        span("request", "t3", "r3", **{"tenant.id": "globex"}),
        span("agent.run", "t3", "a3", "r3", run_id="run3", **{"agent.cost_usd": 0.0005}),
    ]
    assert ex.cost_by_tenant(spans) == {"acme": 0.003, "globex": 0.0005}
    assert ex.cost_by_tenant([]) == {}


def test_cost_by_tenant_puts_untagged_cost_under_unknown():
    spans = [
        span("request", "t1", "r1", **{"app.feature": "faq"}),  # 根 span 忘了打租户标签
        span("agent.run", "t1", "a1", "r1", run_id="run1", **{"agent.cost_usd": 0.004}),
        span("agent.run", "t2", "a2", "zzz", run_id="run2", **{"agent.cost_usd": 0.001}),  # 找不到根 span
        span("request", "t3", "r3", **{"tenant.id": "acme"}),
        span("agent.run", "t3", "a3", "r3", run_id="run3", **{"agent.cost_usd": 0.002}),
    ]
    assert ex.cost_by_tenant(spans) == {"unknown": 0.005, "acme": 0.002}


def _run_for_tenant(tracer, agent, tenant, text):
    with tracer.span("request", **{"tenant.id": tenant, "app.feature": "faq"}):
        return agent.run(text, metadata={"tenant_id": tenant})


def test_cost_by_tenant_on_real_exported_traces(tmp_path):
    path = tmp_path / "traces.jsonl"
    tracer = Tracer(exporter=jsonl_exporter(path))
    expected: dict[str, float] = {}
    for tenant, tokens in [("acme", 1000), ("globex", 3000), ("acme", 2000)]:
        llm = ScriptedLLM([reply("好的", input_tokens=tokens, output_tokens=tokens // 10)])
        res = _run_for_tenant(tracer, Agent(llm, [], tracer=tracer), tenant, "你好")
        expected[tenant] = expected.get(tenant, 0.0) + res.cost_usd
    spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    got = ex.cost_by_tenant(spans)
    assert set(got) == {"acme", "globex"}
    for tenant, cost in expected.items():
        assert got[tenant] == pytest.approx(cost, abs=1e-6)


@tool(risk="dangerous")
def refund(order_id: str) -> str:
    """给订单退款"""
    return f"订单 {order_id} 已退款"


def test_cost_by_tenant_does_not_double_count_resumed_runs(tmp_path):
    """暂停审批 → resume：agent.resume span 上的 cost 是累计值，相加会把 run 阶段的钱算两遍。"""
    path = tmp_path / "traces.jsonl"
    tracer = Tracer(exporter=jsonl_exporter(path))
    llm = ScriptedLLM([
        call_tool("refund", order_id="A1", input_tokens=4000, output_tokens=100),
        reply("已为您退款。", input_tokens=5000, output_tokens=100),
    ])
    agent = Agent(llm, [refund], hooks=[PermissionPolicy()], tracer=tracer)
    res = _run_for_tenant(tracer, agent, "acme", "订单 A1 退款")
    assert res.status == "paused"
    with tracer.span("request", **{"tenant.id": "acme", "app.feature": "approval"}):
        final = agent.approve(res.run_id, approved=True)
    assert final.status == "completed"

    spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {s["name"] for s in spans if "agent.cost_usd" in s["attrs"]} == {"agent.run", "agent.resume"}
    assert ex.cost_by_tenant(spans) == {"acme": pytest.approx(final.cost_usd, abs=1e-6)}
