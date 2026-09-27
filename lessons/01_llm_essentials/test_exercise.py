"""第 01 课练习测试。离线、确定性：三个函数都是纯函数，不调用模型。

运行：make lesson N=01
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/01_llm_essentials
"""

from __future__ import annotations

import copy
import json

import pytest

from agentkit.context import estimate_tokens
from agentkit.testing import load_exercise
from agentkit.types import ToolCall

ex = load_exercise(__file__)


# ================================================================== (a) accumulate_tool_call_deltas


def tc_chunk(index: int, *, id=None, name=None, args=None) -> dict:
    """构造一个只含一个工具调用分片的 chunk（和 OpenAI 流式返回的结构一致）。"""
    tc: dict = {"index": index}
    if id is not None:
        tc["id"] = id
        tc["type"] = "function"
    fn: dict = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    if fn:
        tc["function"] = fn
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [tc]}}]}


def test_a_single_call_arguments_split_into_fragments():
    chunks = [
        tc_chunk(0, id="call_1", name="get_weather", args=""),
        tc_chunk(0, args='{"ci'),
        tc_chunk(0, args='ty": "北'),
        tc_chunk(0, args='京"}'),
    ]
    calls = ex.accumulate_tool_call_deltas(chunks)
    assert calls == [ToolCall(id="call_1", name="get_weather", arguments='{"city": "北京"}')]
    assert json.loads(calls[0].arguments) == {"city": "北京"}


def test_a_later_fragments_with_none_do_not_overwrite_id_and_name():
    # 真实的 SDK 对象 model_dump() 后，后续分片里 id / name 是显式的 None
    first = tc_chunk(0, id="call_1", name="search", args="")
    later = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": None, "type": None, "function": {"name": None, "arguments": '{"q": "vpn"}'}}
    ]}}]}
    tail = {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": None}}]}}]}
    calls = ex.accumulate_tool_call_deltas([first, later, tail])
    assert calls == [ToolCall(id="call_1", name="search", arguments='{"q": "vpn"}')]


def test_a_parallel_calls_are_separated_by_index():
    chunks = [
        tc_chunk(0, id="call_a", name="get_weather", args=""),
        tc_chunk(0, args='{"city": "北京"}'),
        tc_chunk(1, id="call_b", name="get_weather", args=""),
        tc_chunk(1, args='{"city": '),
        tc_chunk(1, args='"上海"}'),
    ]
    calls = ex.accumulate_tool_call_deltas(chunks)
    assert [c.id for c in calls] == ["call_a", "call_b"]
    assert [json.loads(c.arguments)["city"] for c in calls] == ["北京", "上海"]


def test_a_interleaved_fragments_and_sorted_by_index():
    # 不同 index 的分片交错到达，而且 index 1 先开始：不能假设"先发完 0 再发 1"
    chunks = [
        tc_chunk(1, id="call_b", name="get_time", args='{"tz": '),
        tc_chunk(0, id="call_a", name="get_weather", args='{"city": '),
        tc_chunk(1, args='"UTC"}'),
        tc_chunk(0, args='"广州"}'),
    ]
    calls = ex.accumulate_tool_call_deltas(chunks)
    assert calls == [
        ToolCall(id="call_a", name="get_weather", arguments='{"city": "广州"}'),
        ToolCall(id="call_b", name="get_time", arguments='{"tz": "UTC"}'),
    ]


def test_a_ignores_non_tool_chunks_and_returns_empty_list():
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"index": 0, "delta": {"content": "你好"}}]},
        {"choices": [{"index": 0, "delta": {"content": None, "tool_calls": None}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},  # include_usage 的最后一个 chunk
    ]
    assert ex.accumulate_tool_call_deltas(chunks) == []


def test_a_mixed_content_usage_and_tool_chunks():
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "我先查一下。"}}]},
        tc_chunk(0, id="call_1", name="lookup", args='{"id": 7}'),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9}},
    ]
    assert ex.accumulate_tool_call_deltas(chunks) == [ToolCall("call_1", "lookup", '{"id": 7}')]


def test_a_no_arguments_becomes_empty_object():
    chunks = [tc_chunk(0, id="call_1", name="list_my_tickets")]  # 无参数工具：一片 arguments 都没有
    assert ex.accumulate_tool_call_deltas(chunks) == [ToolCall("call_1", "list_my_tickets", "{}")]


def test_a_truncated_arguments_are_kept_verbatim():
    # 流在中途断了：arguments 是半截 JSON。拼接函数不负责解析，原样交给工具层去报错
    chunks = [tc_chunk(0, id="call_1", name="create_ticket", args='{"title": "VPN'), tc_chunk(0, args=" 连不上")]
    assert ex.accumulate_tool_call_deltas(chunks)[0].arguments == '{"title": "VPN 连不上'


def test_a_missing_id_raises_value_error():
    chunks = [tc_chunk(0, name="get_weather", args='{"city": "北京"}')]
    with pytest.raises(ValueError):
        ex.accumulate_tool_call_deltas(chunks)


# ================================================================== (b) estimate_request_cost

MESSAGES = [
    {"role": "system", "content": "你是企业 IT 助手。"},
    {"role": "user", "content": "我的 VPN 连不上了，报错 809。"},
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "在 IT 知识库中搜索故障排查文章。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    }
]
PRICE = (2.0, 8.0)  # 示例单价：美元 / 1M tokens


def tool_tokens_of(tools) -> int:
    return estimate_tokens([{"role": "system", "content": json.dumps(tools, ensure_ascii=False)}])


def test_b_without_tools():
    est = ex.estimate_request_cost(MESSAGES, None, 100, PRICE)
    msg = estimate_tokens(MESSAGES)
    assert (est.message_tokens, est.tool_tokens, est.input_tokens, est.output_tokens) == (msg, 0, msg, 100)
    assert est.cost_usd == pytest.approx((msg * 2.0 + 100 * 8.0) / 1e6)


def test_b_tool_definitions_count_as_input_tokens():
    est = ex.estimate_request_cost(MESSAGES, TOOLS, 100, PRICE)
    expected_tools = tool_tokens_of(TOOLS)
    assert est.tool_tokens == expected_tools > 0
    assert est.input_tokens == estimate_tokens(MESSAGES) + expected_tools
    no_tools = ex.estimate_request_cost(MESSAGES, None, 100, PRICE)
    assert est.cost_usd == pytest.approx(no_tools.cost_usd + expected_tools * 2.0 / 1e6)


def test_b_none_and_empty_tools_are_the_same():
    a = ex.estimate_request_cost(MESSAGES, None, 50, PRICE)
    b = ex.estimate_request_cost(MESSAGES, [], 50, PRICE)
    assert a == b and a.tool_tokens == 0


def test_b_cached_input_uses_cached_price_and_is_capped():
    inp = estimate_tokens(MESSAGES) + tool_tokens_of(TOOLS)
    est = ex.estimate_request_cost(MESSAGES, TOOLS, 10, (2.0, 8.0, 0.5), cached_input_tokens=20)
    assert est.cached_input_tokens == 20
    assert est.cost_usd == pytest.approx(((inp - 20) * 2.0 + 20 * 0.5 + 10 * 8.0) / 1e6)
    # 缓存命中不可能超过输入总量
    capped = ex.estimate_request_cost(MESSAGES, TOOLS, 10, (2.0, 8.0, 0.5), cached_input_tokens=10**6)
    assert capped.cached_input_tokens == inp
    assert capped.cost_usd == pytest.approx((inp * 0.5 + 10 * 8.0) / 1e6)


def test_b_two_item_price_charges_cache_at_input_price():
    plain = ex.estimate_request_cost(MESSAGES, TOOLS, 10, PRICE)
    cached = ex.estimate_request_cost(MESSAGES, TOOLS, 10, PRICE, cached_input_tokens=30)
    assert cached.cost_usd == pytest.approx(plain.cost_usd)


def test_b_invalid_arguments_raise():
    with pytest.raises(ValueError):
        ex.estimate_request_cost(MESSAGES, None, -1, PRICE)
    with pytest.raises(ValueError):
        ex.estimate_request_cost(MESSAGES, None, 10, (1.0,))


# ================================================================== (c) build_messages

SYSTEM = "你是企业 IT 助手。"


def u(text):
    return {"role": "user", "content": text}


def a(text):
    return {"role": "assistant", "content": text}


def a_calls(*ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": i, "type": "function", "function": {"name": "get_weather", "arguments": "{}"}} for i in ids],
    }


def t(call_id, text="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


HISTORY = [
    u("你好"),
    a("你好！有什么可以帮你？"),
    u("北京和上海哪个热？"),
    a_calls("c1", "c2"),
    t("c1", "31"),
    t("c2", "27"),
    a("北京更热。"),
    u("谢谢"),
    a("不客气。"),
]


def assert_protocol_ok(messages):
    """每个 tool_call 都恰好有一条 tool 消息回应、紧跟其后；没有孤立的 tool 消息。"""
    i = 0
    while i < len(messages):
        m = messages[i]
        assert m["role"] != "tool", f"第 {i} 条是孤立的 tool 消息"
        if m["role"] == "assistant" and m.get("tool_calls"):
            expected = [c["id"] for c in m["tool_calls"]]
            j = i + 1
            answered = []
            while j < len(messages) and messages[j]["role"] == "tool":
                answered.append(messages[j]["tool_call_id"])
                j += 1
            assert answered == expected, f"tool_call_id 没有配对：{expected} vs {answered}"
            i = j
            continue
        i += 1


def test_c_system_first_and_user_input_last():
    msgs = ex.build_messages(SYSTEM, HISTORY, "再见", max_history_turns=10)
    assert msgs[0] == {"role": "system", "content": SYSTEM}
    assert msgs[-1] == {"role": "user", "content": "再见"}
    assert msgs[1:-1] == HISTORY
    assert_protocol_ok(msgs)


def test_c_truncates_by_turns_not_by_messages():
    msgs = ex.build_messages(SYSTEM, HISTORY, "再见", max_history_turns=1)
    assert msgs == [{"role": "system", "content": SYSTEM}, u("谢谢"), a("不客气。"), u("再见")]


def test_c_never_splits_tool_calls_from_results():
    msgs = ex.build_messages(SYSTEM, HISTORY, "再见", max_history_turns=2)
    # 第 2 轮（含 1 条 assistant(tool_calls) + 2 条 tool）必须整轮保留
    assert msgs[1:-1] == HISTORY[2:]
    assert_protocol_ok(msgs)


def test_c_zero_turns_means_no_history():
    assert ex.build_messages(SYSTEM, HISTORY, "你好", max_history_turns=0) == [
        {"role": "system", "content": SYSTEM},
        u("你好"),
    ]


def test_c_drops_leading_orphans_and_stray_system_messages():
    history = [
        t("c0", "孤立的工具结果"),  # 上一次错误截断留下的
        a("孤立的 assistant 消息"),
        {"role": "system", "content": "旧的 system prompt"},
        u("你好"),
        {"role": "system", "content": "另一条混进来的 system"},
        a("你好！"),
    ]
    msgs = ex.build_messages(SYSTEM, history, "在吗", max_history_turns=5)
    assert msgs == [{"role": "system", "content": SYSTEM}, u("你好"), a("你好！"), u("在吗")]
    assert [m["role"] for m in msgs].count("system") == 1
    assert_protocol_ok(msgs)


def test_c_does_not_mutate_history():
    history = copy.deepcopy(HISTORY)
    msgs = ex.build_messages(SYSTEM, history, "再见", max_history_turns=3)
    msgs[1]["content"] = "被改掉了"
    msgs.append(u("多加一条"))
    assert history == HISTORY


def test_c_invalid_arguments_raise():
    with pytest.raises(ValueError):
        ex.build_messages(SYSTEM, HISTORY, "你好", max_history_turns=-1)
    with pytest.raises(ValueError):
        ex.build_messages(SYSTEM, HISTORY, "   ", max_history_turns=1)
