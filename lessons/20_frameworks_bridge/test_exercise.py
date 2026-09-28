"""第 20 课练习测试：离线、确定、零框架依赖（不需要安装 dspy / langgraph / openai-agents）。
MiniGraph 的 invoke / resume 和 Predict 都是 async 的，所以这些测试写成 async def、用 await 调用。

运行：make lesson N=20    或    .venv/bin/python -m pytest lessons/20_frameworks_bridge/test_exercise.py -v
"""

from __future__ import annotations

import json
import operator

import pytest

from agentkit import ScriptedLLM, ToolRegistry, Tracer, call_tool, reply, tool
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# ============================================================ (a) MiniGraph


def _counter_graph(limit: int = 3, max_steps: int = 25):
    """inc 节点把 n 加 1；n < limit 时回到 inc，否则走到 done。"""
    g = ex.MiniGraph()
    g.add_node("inc", lambda s: {"n": s["n"] + 1})
    g.add_node("done", lambda s: {"finished": True})
    g.add_edge(ex.START, "inc")
    g.add_conditional_edges("inc", lambda s: "again" if s["n"] < limit else "stop", {"again": "inc", "stop": "done"})
    g.add_edge("done", ex.END)
    return g.compile(max_steps=max_steps)


async def test_graph_linear_run_to_end():
    g = ex.MiniGraph()
    g.add_node("a", lambda s: {"trail": s["trail"] + ["a"]})
    g.add_node("b", lambda s: {"trail": s["trail"] + ["b"], "done": True})
    g.add_edge(ex.START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", ex.END)
    out = await g.compile().invoke({"trail": []}, thread_id="t")
    assert out == {"trail": ["a", "b"], "done": True}


async def test_graph_conditional_loop_and_path_map():
    out = await _counter_graph(limit=3).invoke({"n": 0}, thread_id="t")
    assert out["n"] == 3 and out["finished"] is True


async def test_graph_router_without_path_map_and_unknown_target():
    g = ex.MiniGraph()
    g.add_node("a", lambda s: None)  # 返回 None = 不改状态
    g.add_node("b", lambda s: {"went": "b"})
    g.add_edge(ex.START, "a")
    g.add_conditional_edges("a", lambda s: s["go"])  # 没有 path_map：路由函数直接返回节点名
    g.add_edge("b", ex.END)
    app = g.compile()
    assert (await app.invoke({"go": "b"}, thread_id="t1"))["went"] == "b"
    assert await app.invoke({"go": ex.END}, thread_id="t2") == {"go": ex.END}
    with pytest.raises(ValueError):
        await app.invoke({"go": "nowhere"}, thread_id="t3")


def test_graph_validate_rejects_bad_structure():
    g = ex.MiniGraph()
    g.add_node("a", lambda s: {})
    with pytest.raises(ValueError):  # 没有入口
        g.compile()
    g.add_edge(ex.START, "a")
    with pytest.raises(ValueError):  # a 没有出边
        g.compile()
    g.add_edge("a", "ghost")
    with pytest.raises(ValueError):  # 终点不存在
        g.compile()
    g.add_edge("a", ex.END)
    g.add_conditional_edges("a", lambda s: ex.END)
    with pytest.raises(ValueError):  # 同时有普通边和条件边
        g.compile()


async def test_graph_reducer_appends_instead_of_overwrite():
    g = ex.MiniGraph(reducers={"log": operator.add})
    g.add_node("a", lambda s: {"log": ["a"], "last": "a"})
    g.add_node("b", lambda s: {"log": ["b"], "last": "b"})
    g.add_edge(ex.START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", ex.END)
    out = await g.compile().invoke({"log": ["start"], "last": None}, thread_id="t")
    assert out["log"] == ["start", "a", "b"]  # 有 reducer：追加
    assert out["last"] == "b"  # 没有 reducer：后写覆盖


async def test_graph_max_steps_raises_recursion_error():
    app = _counter_graph(limit=100, max_steps=5)
    with pytest.raises(ex.GraphRecursionError):
        await app.invoke({"n": 0}, thread_id="t")


async def test_graph_checkpoint_after_every_step_and_snapshots_are_immutable():
    app = _counter_graph(limit=2)
    out = await app.invoke({"n": 0}, thread_id="t")
    history = app.get_state_history("t")  # 最新在前
    # 输入 1 个 + 每执行一个节点 1 个：inc, inc, done → 共 4 个
    assert [cp.step for cp in history] == [3, 2, 1, 0]
    assert [cp.next for cp in history] == [ex.END, "done", "inc", "inc"]
    assert history[0].status == "done" and history[-1].status == "running"
    assert history[2].state == {"n": 1}
    out["n"] = 999  # 改返回值不能影响已存的检查点
    assert app.get_state("t").state["n"] == 2


async def test_graph_interrupt_pauses_then_resume_reruns_node():
    runs = {"approval": 0}

    def approval(s):
        runs["approval"] += 1  # 恢复时节点从头重跑：这一行会执行两次
        ok = ex.interrupt({"question": "批准吗？", "amount": s["amount"]})
        return {"approved": ok}

    g = ex.MiniGraph()
    g.add_node("approval", approval)
    g.add_node("pay", lambda s: {"paid": True})
    g.add_node("cancel", lambda s: {"paid": False})
    g.add_edge(ex.START, "approval")
    g.add_conditional_edges("approval", lambda s: "pay" if s["approved"] else "cancel")
    g.add_edge("pay", ex.END)
    g.add_edge("cancel", ex.END)
    app = g.compile()

    first = await app.invoke({"amount": 42}, thread_id="t")
    assert first["__interrupt__"] == {"question": "批准吗？", "amount": 42}
    cp = app.get_state("t")
    assert cp.status == "interrupted" and cp.next == "approval" and cp.step == 0
    with pytest.raises(ValueError):
        await app.resume("no-such-thread", True)

    final = await app.resume("t", True)
    assert final["paid"] is True and "__interrupt__" not in final
    assert runs["approval"] == 2
    assert app.get_state("t").status == "done"
    with pytest.raises(ValueError):  # 已经结束的 thread 不能再 resume
        await app.resume("t", True)

    rejected = await app.invoke({"amount": 7}, thread_id="t2")
    assert "__interrupt__" in rejected
    assert (await app.resume("t2", False))["paid"] is False


async def test_graph_node_with_two_interrupts_consumes_resume_values_in_order():
    def ask_twice(s):
        a = ex.interrupt("第一个问题")
        b = ex.interrupt("第二个问题")
        return {"answers": [a, b]}

    g = ex.MiniGraph()
    g.add_node("ask", ask_twice)
    g.add_edge(ex.START, "ask")
    g.add_edge("ask", ex.END)
    app = g.compile()
    assert (await app.invoke({}, thread_id="t"))["__interrupt__"] == "第一个问题"
    assert (await app.resume("t", "A"))["__interrupt__"] == "第二个问题"
    assert (await app.resume("t", "B"))["answers"] == ["A", "B"]


def _helpdesk_registry(resets: list):
    @tool
    def search_kb(query: str) -> str:
        """搜索 IT 知识库。"""
        return "[KB-101] 更新 VPN 证书：设置 → 证书 → 更新"

    @tool(risk="dangerous")
    def reset_password(reason: str) -> str:
        """为当前用户重置密码。"""
        resets.append(reason)
        return "已发送重置链接"

    return ToolRegistry([search_kb, reset_password])


@pytest.mark.parametrize("decision", [True, False])
async def test_agent_graph_built_from_agentkit_blocks_waits_for_approval(decision):
    """用你的 MiniGraph + agentkit 的 LLM / ToolRegistry 搭出和 impl_langgraph.py 同构的审批 Agent。"""
    resets: list = []
    llm = ScriptedLLM(
        [
            call_tool("search_kb", query="VPN 证书过期"),
            call_tool("reset_password", reason="账号被锁"),
            reply("处理完毕"),
        ]
    )
    tracer = Tracer()
    app = ex.build_agent_graph(llm, _helpdesk_registry(resets), {"reset_password"}, tracer=tracer)
    inputs = {"messages": [{"role": "system", "content": "你是 IT 助手"}, {"role": "user", "content": "VPN 连不上，顺便重置密码"}], "decisions": {}}

    paused = await app.invoke(inputs, thread_id="it-1")
    assert paused["__interrupt__"] == {"tool": "reset_password", "arguments": {"reason": "账号被锁"}}
    assert resets == [] and len(llm.calls) == 2  # 审批前：危险工具没执行，模型只被调用了 2 次

    final = await app.resume("it-1", decision)
    assert final["messages"][-1]["content"] == "处理完毕"
    assert resets == (["账号被锁"] if decision else [])
    tool_results = [m["content"] for m in final["messages"] if m["role"] == "tool"]
    assert tool_results[0].startswith("[KB-101]")
    assert (tool_results[1] == "已发送重置链接") if decision else ("拒绝" in tool_results[1])
    # 每个节点一个 Span；approval 节点被中断的那次标记为 interrupted 而不是 error
    spans = [s for root in tracer.traces for s in root.walk()]
    assert [s.name for s in spans].count("node.approval") == 3  # 第 1 轮 1 次 + 中断 1 次 + 恢复重跑 1 次
    assert all(s.status == "ok" for s in spans)


# ============================================================ (b) Signature


def test_parse_signature_types_and_errors():
    ins, outs = ex.parse_signature("question, context: str -> answer, confidence: float, tags: list")
    assert [(f.name, f.type) for f in ins] == [("question", str), ("context", str)]
    assert [(f.name, f.type) for f in outs] == [("answer", str), ("confidence", float), ("tags", list)]
    for bad in ["question answer", "a -> b -> c", "q -> ", "q, -> a", "q -> a: decimal", "q -> q", "my-q -> a"]:
        with pytest.raises(ValueError):
            ex.parse_signature(bad)


def test_signature_to_messages_renders_instructions_fields_and_inputs():
    sig = ex.Signature(
        "question, context -> answer, confidence: float",
        instructions="只根据 context 回答。",
        desc={"question": "员工的问题", "confidence": "0 到 1 之间"},
    )
    msgs = sig.to_messages(question="VPN 连不上怎么办？", context="[KB-101] 更新证书")
    assert [m["role"] for m in msgs] == ["system", "user"]
    system = msgs[0]["content"]
    for needle in ["只根据 context 回答。", "question", "员工的问题", "context", "answer", "confidence", "float", "0 到 1 之间", "JSON"]:
        assert needle in system, needle
    assert msgs[1]["content"] == "question: VPN 连不上怎么办？\ncontext: [KB-101] 更新证书"
    with pytest.raises(ValueError):
        sig.to_messages(question="缺了 context")
    with pytest.raises(ValueError):
        sig.to_messages(question="q", context="c", extra="多余的字段")


def test_signature_demos_become_few_shot_turns():
    sig = ex.Signature("question -> answer, confidence: float").with_demos(
        [{"question": "打印机卡纸", "answer": "关电源取纸", "confidence": 0.9}]
    )
    msgs = sig.to_messages(question="邮箱满了")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "question: 打印机卡纸"
    assert json.loads(msgs[2]["content"]) == {"answer": "关电源取纸", "confidence": 0.9}
    assert msgs[3]["content"] == "question: 邮箱满了"
    # 优化器改的是新版本，旧版本不变
    better = sig.with_instructions("回答要简短。")
    assert "回答要简短。" in better.to_messages(question="x")[0]["content"]
    assert "回答要简短。" not in sig.to_messages(question="x")[0]["content"]


def test_signature_parse_output_extracts_and_coerces():
    sig = ex.Signature("question -> answer, confidence: float, urgent: bool, steps: list, count: int")
    text = '好的，结果如下：\n```json\n{"answer": "更新证书", "confidence": "0.8", "urgent": "true", "steps": ["a", "b"], "count": 2, "extra": 1}\n```'
    assert sig.parse_output(text) == {"answer": "更新证书", "confidence": 0.8, "urgent": True, "steps": ["a", "b"], "count": 2}
    with pytest.raises(ValueError, match="confidence"):
        sig.parse_output('{"answer": "x", "urgent": false, "steps": [], "count": 1}')
    with pytest.raises(ValueError):
        sig.parse_output('{"answer": "x", "confidence": "很高", "urgent": false, "steps": [], "count": 1}')
    with pytest.raises(ValueError):
        sig.parse_output("抱歉，我不知道。")


async def test_predict_module_repairs_invalid_output_once():
    sig = ex.Signature("question -> answer, confidence: float")
    llm = ScriptedLLM([reply("答案是更新证书"), reply('{"answer": "更新证书", "confidence": 0.7}')])
    pred = await ex.Predict(sig, llm, max_repairs=1)(question="VPN 证书过期")  # 调用模型：async
    assert pred.answer == "更新证书" and pred["confidence"] == 0.7
    assert len(llm.calls) == 2
    assert "没有通过校验" in llm.calls[1]["messages"][-1]["content"]  # 第二次调用带上了错误反馈


# ============================================================ (c) 概念对照


SMALL_TABLE = """
一些说明文字

<!-- concept-map:start -->
| agentkit | DSPy | LangGraph | OpenAI Agents SDK |
|---|---|---|---|
| `PauseRun` / `approve` | 无 | `interrupt()` + `Command(resume=...)` | `needs_approval=True` |
| `@tool` | `dspy.Tool` | `@tool` \\| `ToolNode` | `@function_tool` |
| 预算 | — | — | — |
<!-- concept-map:end -->

| 另一张表 | x |
|---|---|
| `PauseRun` | 不应该被解析 |
"""


def test_parse_concept_table_between_markers():
    table = ex.parse_concept_table(SMALL_TABLE)
    assert set(table) == {"PauseRun", "approve", "@tool", "预算"}
    assert table["PauseRun"] is table["approve"] or table["PauseRun"] == table["approve"]
    assert table["PauseRun"]["LangGraph"] == "`interrupt()` + `Command(resume=...)`"
    assert table["@tool"]["LangGraph"] == "`@tool` | `ToolNode`"  # 转义的竖线还原
    assert list(table["预算"]) == ["DSPy", "LangGraph", "OpenAI Agents SDK"]


def test_map_concept_normalizes_names_and_aliases():
    table = ex.parse_concept_table(SMALL_TABLE)
    assert ex.map_concept("PauseRun", "LangGraph", table) == "`interrupt()` + `Command(resume=...)`"
    assert ex.map_concept(" pauserun ", "openai", table) == "`needs_approval=True`"
    assert ex.map_concept("tool", "openai-agents-sdk", table) == "`@function_tool`"
    assert ex.map_concept("`@tool`", "dspy", table) == "`dspy.Tool`"
    assert ex.map_concept("approve()", "lg", table).startswith("`interrupt()`")


def test_map_concept_unknown_raises_keyerror_with_suggestion():
    table = ex.parse_concept_table(SMALL_TABLE)
    with pytest.raises(KeyError, match="PauseRun"):
        ex.map_concept("PauseRn", "LangGraph", table)  # 拼错了：提示相近的概念
    with pytest.raises(KeyError, match="LangGraph"):
        ex.map_concept("PauseRun", "CrewAI", table)  # 表里没有这个框架：列出可选框架


def test_map_concept_reads_lesson_readme_by_default():
    assert "interrupt" in ex.map_concept("PauseRun", "LangGraph")
    assert "needs_approval" in ex.map_concept("PauseRun", "openai")
    assert "function_tool" in ex.map_concept("@tool", "OpenAI Agents SDK")
    assert "max_iters" in ex.map_concept("max_steps", "dspy")
    assert "set_trace_processors" in ex.map_concept("Tracer", "openai")
