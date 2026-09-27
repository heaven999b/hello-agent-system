"""第 03 课练习测试：离线、确定性。

运行：make lesson N=03
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/03_context_memory
"""

from __future__ import annotations

import copy

import pytest

from agentkit.context import estimate_tokens
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# ------------------------------------------------------------------ 构造测试数据


def sys_msg(text="你是采购助手"):
    return {"role": "system", "content": text}


def user(text):
    return {"role": "user", "content": text}


def say(text):
    return {"role": "assistant", "content": text}


def calls(*ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": i, "type": "function", "function": {"name": "search", "arguments": "{}"}} for i in ids],
    }


def result(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def conversation(turns=6):
    """system + 若干轮「用户提问 → 调工具 → 工具结果 → 回答」，其中第 2 轮是并行调用两个工具。"""
    msgs = [sys_msg()]
    for t in range(turns):
        msgs.append(user(f"第{t}个问题：" + "请帮我查" * 5))
        if t == 2:
            msgs.append(calls(f"c{t}a", f"c{t}b"))
            msgs.append(result(f"c{t}a", "搜索结果" * 40))
            msgs.append(result(f"c{t}b", "搜索结果" * 40))
        else:
            msgs.append(calls(f"c{t}"))
            msgs.append(result(f"c{t}", "搜索结果" * 40))
        msgs.append(say(f"第{t}个回答"))
    return msgs


def assert_protocol_ok(msgs):
    """检查消息协议：每条 tool 消息都能对应上前面的 assistant(tool_calls)，每个调用都有结果。"""
    pending: set[str] = set()
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            assert m["tool_call_id"] in pending, f"第 {i} 条是孤立的 tool 消息：{m}"
            pending.discard(m["tool_call_id"])
        else:
            assert not pending, f"第 {i} 条之前，这些工具调用没有结果：{pending}"
            if m["role"] == "assistant" and m.get("tool_calls"):
                pending = {c["id"] for c in m["tool_calls"]}
    assert not pending, f"结尾处这些工具调用没有结果：{pending}"


# ------------------------------------------------------------------ 任务 1：trim_to_budget


def test_trim_under_budget_keeps_everything():
    msgs = conversation(2)
    assert ex.trim_to_budget(msgs, 100_000) == msgs


def test_trim_keeps_all_leading_system_messages():
    msgs = [sys_msg("规则一"), sys_msg("规则二")] + conversation(5)[1:]
    out = ex.trim_to_budget(msgs, 200)
    assert out[0] == sys_msg("规则一") and out[1] == sys_msg("规则二")
    assert out[2]["role"] != "system"
    assert len(out) < len(msgs)


def test_trim_never_orphans_tool_messages_for_any_budget():
    msgs = conversation(6)
    total = estimate_tokens(msgs)
    for budget in range(0, total + 50, 7):
        out = ex.trim_to_budget(msgs, budget)
        assert_protocol_ok(out)
        assert out[0]["role"] == "system"
        assert out[-1] == msgs[-1], "用户最新的对话必须保留"


def test_trim_respects_budget_when_possible():
    msgs = conversation(6)
    head_and_last = estimate_tokens([msgs[0], msgs[-1]])
    for budget in range(head_and_last, estimate_tokens(msgs) + 1, 13):
        assert estimate_tokens(ex.trim_to_budget(msgs, budget)) <= budget


def test_trim_keeps_last_block_even_if_over_budget():
    msgs = [sys_msg(), user("并行查两个东西"), calls("x", "y"), result("x", "很长" * 100), result("y", "很长" * 100)]
    out = ex.trim_to_budget(msgs, 1)
    # 最后一个块 = assistant(tool_calls) + 两条 tool 结果，必须整体保留，不能只留一半
    assert out == [msgs[0]] + msgs[2:]
    assert_protocol_ok(out)


def test_trim_keeps_contiguous_suffix_only():
    # 每条消息的估算：system "S" = 4；单个汉字 = 1 + 4 = 5；"长"*100 = 104
    msgs = [sys_msg("S"), user("早"), say("好"), user("长" * 100), say("嗯")]
    # 预算 19 = system(4) + 最后一条(5) + 两条早期小消息(5+5)，数值上"装得下"那两条小消息，
    # 但中间隔着一个装不下的大块 —— 必须停下，不能跳过它去保留更早的消息
    out = ex.trim_to_budget(msgs, 19)
    assert out == [sys_msg("S"), say("嗯")]


def test_trim_boundary_equal_budget_fits():
    msgs = conversation(4)
    last_two_blocks = [user("追问"), say("回答")]
    msgs = msgs + last_two_blocks
    exact = estimate_tokens([msgs[0]] + last_two_blocks)
    assert ex.trim_to_budget(msgs, exact) == [msgs[0]] + last_two_blocks  # 恰好等于预算：放得下
    assert ex.trim_to_budget(msgs, exact - 1) == [msgs[0], last_two_blocks[1]]  # 少 1 个 token：放不下


def test_trim_does_not_mutate_input():
    msgs = conversation(5)
    snapshot = copy.deepcopy(msgs)
    ex.trim_to_budget(msgs, 150)
    ex.trim_to_budget(msgs, 100_000)
    assert msgs == snapshot


def test_trim_drops_orphan_tool_messages_already_in_input():
    # 被别的代码截坏的历史：开头就是一条孤立 tool 消息；中间还有一条跟在 user 后面的 tool 消息
    msgs = [sys_msg(), result("lost", "旧结果"), user("你好"), result("lost2", "又一个孤儿"), say("你好！")]
    out = ex.trim_to_budget(msgs, 100_000)
    assert out == [sys_msg(), user("你好"), say("你好！")]


def test_trim_without_system_and_edge_cases():
    assert ex.trim_to_budget([], 100) == []
    only_system = [sys_msg("a"), sys_msg("b")]
    assert ex.trim_to_budget(only_system, 1) == only_system
    msgs = conversation(4)[1:]  # 去掉 system
    out = ex.trim_to_budget(msgs, 120)
    assert out and out[0]["role"] != "tool"
    assert_protocol_ok(out)


# ------------------------------------------------------------------ 任务 2：clear_old_tool_results


def test_clear_keeps_last_n_and_structure():
    msgs = conversation(4)  # 共 5 条 tool 消息（第 2 轮并行调用了两个）
    out = ex.clear_old_tool_results(msgs, keep_last=2, placeholder="[已清理]")
    tools_in = [m for m in msgs if m["role"] == "tool"]
    tools_out = [m for m in out if m["role"] == "tool"]
    assert [m["content"] for m in tools_out] == ["[已清理]"] * 3 + [tools_in[3]["content"], tools_in[4]["content"]]
    # 结构完全不变：条数、顺序、角色、tool_call_id
    assert len(out) == len(msgs)
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    assert [m.get("tool_call_id") for m in out] == [m.get("tool_call_id") for m in msgs]
    assert_protocol_ok(out)
    assert estimate_tokens(out) < estimate_tokens(msgs)


def test_clear_counts_parallel_results_individually():
    msgs = [user("q"), calls("a", "b"), result("a", "A" * 50), result("b", "B" * 50), say("ok")]
    out = ex.clear_old_tool_results(msgs, keep_last=1, placeholder="-")
    assert [m["content"] for m in out if m["role"] == "tool"] == ["-", "B" * 50]


def test_clear_keep_zero_and_keep_more_than_available():
    msgs = conversation(3)
    n_tools = sum(m["role"] == "tool" for m in msgs)
    all_cleared = ex.clear_old_tool_results(msgs, keep_last=0, placeholder="x")
    assert all(m["content"] == "x" for m in all_cleared if m["role"] == "tool")
    assert ex.clear_old_tool_results(msgs, keep_last=n_tools) == msgs
    assert ex.clear_old_tool_results(msgs, keep_last=n_tools + 10) == msgs


def test_clear_does_not_mutate_input_and_preserves_other_fields():
    msgs = [user("q"), calls("a"), {**result("a", "原始结果"), "name": "search"}, say("ok"), calls("b"), result("b", "新")]
    snapshot = copy.deepcopy(msgs)
    out = ex.clear_old_tool_results(msgs, keep_last=1, placeholder="[已清理]")
    assert msgs == snapshot, "不能修改输入"
    assert out[2] == {"role": "tool", "tool_call_id": "a", "content": "[已清理]", "name": "search"}
    assert out[0] == msgs[0] and out[1] == msgs[1] and out[3] == msgs[3]


def test_clear_default_placeholder_and_negative_keep():
    msgs = [calls("a"), result("a", "旧"), calls("b"), result("b", "中"), calls("c"), result("c", "新")]
    out = ex.clear_old_tool_results(msgs)  # 默认 keep_last=2
    assert out[1]["content"] == ex.DEFAULT_PLACEHOLDER
    assert [out[3]["content"], out[5]["content"]] == ["中", "新"]
    with pytest.raises(ValueError):
        ex.clear_old_tool_results(msgs, keep_last=-1)


def test_clear_then_trim_keeps_more_history():
    """组合使用：先清理旧工具结果，同样的预算下能保留更多轮对话。"""
    msgs = conversation(6)
    budget = 400
    plain = ex.trim_to_budget(msgs, budget)
    cleaned = ex.trim_to_budget(ex.clear_old_tool_results(msgs, keep_last=1, placeholder="[已清理]"), budget)
    assert len(cleaned) > len(plain)
    assert_protocol_ok(cleaned)
