"""第 02 课练习测试。离线、确定性：用 ScriptedLLM 按剧本扮演模型。

运行：make lesson N=02
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/02_agent_loop
"""

from __future__ import annotations

import json

import pytest

from agentkit import ScriptedLLM, call_tool, call_tools, reply
from agentkit.testing import load_exercise
from agentkit.types import LLMResponse, ToolCall

ex = load_exercise(__file__)


# ------------------------------------------------------------------ 测试用的工具


def add(a: int, b: int) -> int:
    """两数相加"""
    return a + b


def get_weather(city: str) -> dict:
    """查询城市当前气温"""
    data = {"北京": 31, "上海": 27}
    if city not in data:
        raise KeyError(f"没有 {city} 的数据")
    return {"city": city, "temp_c": data[city]}


def boom() -> str:
    """总是失败的工具"""
    raise RuntimeError("数据库连接超时")


def now() -> str:
    """当前时间"""
    return "12:00"


TOOLS = {"add": add, "get_weather": get_weather, "boom": boom, "now": now}


# ------------------------------------------------------------------ 辅助断言


def tool_msgs(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m["role"] == "tool"]


def assert_protocol_ok(messages: list[dict]) -> None:
    """OpenAI 消息协议的不变量：每个 tool_call 都恰好有一条 tool 消息回应，且紧跟在它的 assistant 消息之后。"""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "tool":
            raise AssertionError(f"第 {i} 条 tool 消息前面没有对应的 assistant(tool_calls) 消息")
        if m["role"] == "assistant" and m.get("tool_calls"):
            expected = [c["id"] for c in m["tool_calls"]]
            answered = []
            j = i + 1
            while j < len(messages) and messages[j]["role"] == "tool":
                answered.append(messages[j]["tool_call_id"])
                j += 1
            assert answered == expected, f"tool_call_id 没有一一配对：调用 {expected}，回应 {answered}"
            i = j
            continue
        i += 1


# ------------------------------------------------------------------ execute_tool_call


def test_execute_success_returns_string():
    assert ex.execute_tool_call(TOOLS, ToolCall("c1", "add", '{"a": 2, "b": 3}')) == "5"
    out = ex.execute_tool_call(TOOLS, ToolCall("c2", "get_weather", '{"city": "北京"}'))
    assert json.loads(out) == {"city": "北京", "temp_c": 31}
    assert "北京" in out, "非字符串结果要用 json.dumps(..., ensure_ascii=False)，中文不能被转义"


def test_execute_empty_arguments_means_no_args():
    assert ex.execute_tool_call(TOOLS, ToolCall("c1", "now", "")) == "12:00"


def test_execute_unknown_tool_lists_available_tools():
    out = ex.execute_tool_call(TOOLS, ToolCall("c1", "delete_everything", "{}"))
    assert out.startswith(("错误", "Error"))
    assert "delete_everything" in out
    assert "add" in out and "get_weather" in out, "要告诉模型有哪些工具可用，它才能自我纠正"


def test_execute_invalid_json():
    out = ex.execute_tool_call(TOOLS, ToolCall("c1", "add", "{a: 1, b: 2"))
    assert out.startswith(("错误", "Error")) and "JSON" in out


def test_execute_arguments_must_be_object():
    assert ex.execute_tool_call(TOOLS, ToolCall("c1", "add", "[1, 2]")).startswith(("错误", "Error"))


def test_execute_exception_becomes_observation():
    out = ex.execute_tool_call(TOOLS, ToolCall("c1", "boom", "{}"))
    assert out.startswith(("错误", "Error")) and "RuntimeError" in out and "数据库连接超时" in out


def test_execute_wrong_argument_names():
    out = ex.execute_tool_call(TOOLS, ToolCall("c1", "add", '{"x": 1, "y": 2}'))
    assert out.startswith(("错误", "Error")) and "TypeError" in out


# ------------------------------------------------------------------ run_agent_loop：正常路径


def test_direct_answer_without_tool_calls():
    llm = ScriptedLLM([reply("你好！")])
    r = ex.run_agent_loop(llm, TOOLS, "hi")
    assert r["output"] == "你好！"
    assert r["stop_reason"] == "final_answer" and r["steps"] == 1
    assert [m["role"] for m in r["messages"]] == ["system", "user", "assistant"]
    assert r["messages"][0]["content"] == ex.SYSTEM_PROMPT
    assert r["messages"][1] == {"role": "user", "content": "hi"}
    assert len(llm.calls) == 1


def test_tool_schemas_are_sent_and_empty_tools_become_none():
    llm = ScriptedLLM([reply("ok")])
    ex.run_agent_loop(llm, TOOLS, "hi")
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == list(TOOLS)

    llm = ScriptedLLM([reply("ok")])
    ex.run_agent_loop(llm, {}, "hi")
    assert llm.calls[0]["tools"] is None, "没有工具时应传 tools=None，而不是空列表"


def test_single_tool_call_round_trip():
    llm = ScriptedLLM([call_tool("add", a=2, b=3), reply("结果是 5")])
    r = ex.run_agent_loop(llm, TOOLS, "2+3=?")
    msgs = r["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "assistant"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "add"
    assert msgs[3]["tool_call_id"] == msgs[2]["tool_calls"][0]["id"]
    assert msgs[3]["content"] == "5"
    # 第二次调用模型时，模型必须"看到"工具结果
    assert llm.calls[1]["messages"][-1] == msgs[3]
    assert r["output"] == "结果是 5" and r["steps"] == 2 and r["stop_reason"] == "final_answer"
    assert_protocol_ok(msgs)


def test_multiple_sequential_steps():
    llm = ScriptedLLM([call_tool("get_weather", city="北京"), call_tool("add", a=31, b=1), reply("32")])
    r = ex.run_agent_loop(llm, TOOLS, "北京气温加 1")
    assert r["steps"] == 3 and r["output"] == "32"
    contents = [m["content"] for m in tool_msgs(r["messages"])]
    assert json.loads(contents[0]) == {"city": "北京", "temp_c": 31}
    assert contents[1] == "32"
    assert_protocol_ok(r["messages"])


def test_parallel_tool_calls_all_answered_in_order():
    llm = ScriptedLLM([
        call_tools(("get_weather", {"city": "北京"}), ("get_weather", {"city": "上海"})),
        reply("北京更热"),
    ])
    r = ex.run_agent_loop(llm, TOOLS, "北京和上海哪个热？")
    assistant = r["messages"][2]
    tms = tool_msgs(r["messages"])
    assert len(assistant["tool_calls"]) == 2 and len(tms) == 2, "一次并行发起的多个调用都要执行"
    assert [m["tool_call_id"] for m in tms] == [c["id"] for c in assistant["tool_calls"]]
    assert [json.loads(m["content"])["temp_c"] for m in tms] == [31, 27]
    assert len(llm.calls) == 2, "所有并行调用执行完之后，才进入下一轮"
    assert_protocol_ok(r["messages"])


def test_content_with_tool_calls_is_not_final():
    thinking_aloud = LLMResponse(content="我先算一下", tool_calls=[ToolCall("c9", "add", '{"a": 1, "b": 1}')])
    llm = ScriptedLLM([thinking_aloud, reply("等于 2")])
    r = ex.run_agent_loop(llm, TOOLS, "1+1")
    assert r["output"] == "等于 2" and r["steps"] == 2, "有 tool_calls 就不是最终答案，即使 content 不为空"


def test_none_content_final_answer_becomes_empty_string():
    r = ex.run_agent_loop(ScriptedLLM([LLMResponse(content=None)]), TOOLS, "hi")
    assert r["output"] == "" and r["stop_reason"] == "final_answer"


# ------------------------------------------------------------------ run_agent_loop：错误即观察


def test_unknown_tool_does_not_crash_loop():
    llm = ScriptedLLM([call_tool("delete_everything"), reply("抱歉，我没有这个能力")])
    r = ex.run_agent_loop(llm, TOOLS, "删掉一切")
    assert tool_msgs(r["messages"])[0]["content"].startswith(("错误", "Error"))
    assert r["output"] == "抱歉，我没有这个能力"
    assert_protocol_ok(r["messages"])


def test_tool_exception_does_not_crash_loop():
    llm = ScriptedLLM([call_tool("boom"), reply("工具出错了")])
    r = ex.run_agent_loop(llm, TOOLS, "x")
    obs = tool_msgs(r["messages"])[0]["content"]
    assert obs.startswith(("错误", "Error")) and "数据库连接超时" in obs
    assert r["stop_reason"] == "final_answer"


def test_model_self_corrects_after_invalid_json():
    bad = LLMResponse(tool_calls=[ToolCall("c1", "add", "{a: 1, b: 2")])
    llm = ScriptedLLM([bad, call_tool("add", a=1, b=2), reply("3")])
    r = ex.run_agent_loop(llm, TOOLS, "1+2")
    contents = [m["content"] for m in tool_msgs(r["messages"])]
    assert contents[0].startswith(("错误", "Error")) and contents[1] == "3"
    assert r["output"] == "3" and r["steps"] == 3
    # 模型第二次被调用时看到了错误说明，这才有机会自我纠正
    assert llm.calls[1]["messages"][-1]["content"].startswith(("错误", "Error"))


# ------------------------------------------------------------------ run_agent_loop：步数上限


def test_max_steps_stops_runaway_loop():
    llm = ScriptedLLM([call_tool("add", a=1, b=1) for _ in range(3)])
    r = ex.run_agent_loop(llm, TOOLS, "一直算", max_steps=3)
    assert r["stop_reason"] == "max_steps" and r["output"] is None and r["steps"] == 3
    assert len(llm.calls) == 3, "达到上限后不能再调用模型"
    assert r["messages"][-1]["role"] == "tool", "最后一步的工具也要执行完，保证历史配对完整"
    assert_protocol_ok(r["messages"])


def test_default_max_steps_is_5():
    llm = ScriptedLLM([call_tool("now") for _ in range(5)])
    r = ex.run_agent_loop(llm, TOOLS, "x")
    assert r["steps"] == 5 and r["stop_reason"] == "max_steps"


def test_invalid_max_steps_raises():
    with pytest.raises(ValueError):
        ex.run_agent_loop(ScriptedLLM([]), TOOLS, "x", max_steps=0)
