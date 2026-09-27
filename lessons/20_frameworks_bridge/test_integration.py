"""第 20 课集成测试：四个 impl_*.py 用各框架自带的"假模型"离线跑通（不连网、不花钱）。

框架没装时对应测试自动跳过（pytest.importorskip），所以 CI 不需要装这三个框架。
这些测试不属于练习——它们不依赖 exercise.py，在练习没写完时也会通过。
它们的价值是**防版本漂移**：框架升级改了 API，这里会第一个报错。

    .venv/bin/python -m pytest lessons/20_frameworks_bridge/test_integration.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str):
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _approvals(decision: bool):
    seen = []

    def approver(name, args):
        seen.append((name, args))
        return decision

    return approver, seen


def test_agentkit_impl_offline_script():
    impl = _load("impl_agentkit")
    approver, seen = _approvals(True)
    res = impl.run(approver=approver, llm=impl.offline_llm())
    assert res.tool_calls == ["search_kb", "get_account_status", "reset_password"]
    assert res.llm_calls == 3 and [a[0] for a in seen] == ["reset_password"]
    assert "KB-101" in res.answer


@pytest.mark.parametrize("decision", [True, False])
def test_langgraph_impl_with_fake_chat_model(decision):
    pytest.importorskip("langgraph")
    fake_mod = pytest.importorskip("langchain_core.language_models.fake_chat_models")
    from langchain_core.messages import AIMessage

    class ToolCallingFake(fake_mod.GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # 假模型不真的需要工具 Schema
            return self

    model = ToolCallingFake(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_kb", "args": {"query": "VPN 证书过期"}, "id": "c1"},
                        {"name": "reset_password", "args": {"reason": "账号被锁"}, "id": "c2"},
                    ],
                ),
                AIMessage(content="已处理（KB-101）"),
            ]
        )
    )
    impl = _load("impl_langgraph")
    approver, seen = _approvals(decision)
    res = impl.run(approver=approver, model=model)
    assert seen == [("reset_password", {"reason": "账号被锁"})]
    assert res.llm_calls == 2 and res.answer == "已处理（KB-101）"
    # 同一批里的 search_kb 也要等审批节点放行后才执行（整批一起过 approval 节点）
    assert res.tool_calls == (["search_kb", "reset_password"] if decision else ["search_kb"])
    assert res.extra["node_runs"]["approval"] == 2  # 中断 1 次 + 恢复时从头重跑 1 次


@pytest.mark.parametrize("decision", [True, False])
def test_openai_agents_impl_with_scripted_model(decision):
    pytest.importorskip("agents")
    from agents.testing import ScriptedModel, assistant_message, function_call

    model = ScriptedModel(
        [
            [
                function_call("search_kb", {"query": "VPN 证书过期"}, call_id="c1"),
                function_call("reset_password", {"reason": "账号被锁"}, call_id="c2"),
            ],
            [assistant_message("已处理（KB-101）")],
        ]
    )
    impl = _load("impl_openai_agents")
    approver, seen = _approvals(decision)
    res = impl.run(approver=approver, model=model)
    assert seen == [("reset_password", {"reason": "账号被锁"})]
    assert res.answer == "已处理（KB-101）"
    # 和 LangGraph 版不同：不需要审批的 search_kb 在暂停前就执行了
    assert res.tool_calls == (["search_kb", "reset_password"] if decision else ["search_kb"])


def test_openai_agents_tool_flags():
    pytest.importorskip("agents")
    shared = _load("shared_tools")
    impl = _load("impl_openai_agents")
    agent = impl.build_agent(model=None, desk=shared.ITDesk())
    flags = {t.name: t.needs_approval for t in agent.tools}
    assert flags == {"search_kb": False, "get_account_status": False, "reset_password": True}
    reset = next(t for t in agent.tools if t.name == "reset_password")
    assert "username" not in reset.params_json_schema["properties"]  # 身份不交给模型


def test_dspy_impl_with_dummy_lm():
    pytest.importorskip("dspy")
    from dspy.utils import DummyLM

    lm = DummyLM(
        [
            {"next_thought": "先查知识库", "next_tool_name": "search_kb", "next_tool_args": {"query": "VPN 证书过期"}},
            {"next_thought": "用户要求重置", "next_tool_name": "reset_password", "next_tool_args": {"reason": "账号被锁"}},
            {"next_thought": "信息够了", "next_tool_name": "finish", "next_tool_args": {}},
            {"reasoning": "整理答复", "answer": "已处理（KB-101）"},
        ]
    )
    impl = _load("impl_dspy")
    approver, seen = _approvals(True)
    res = impl.run(approver=approver, lm=lm)
    assert seen == [("reset_password", {"reason": "账号被锁"})]
    assert res.tool_calls == ["search_kb", "reset_password"]
    assert res.answer == "已处理（KB-101）"
    assert res.llm_calls == 4  # 3 轮 ReAct + 1 次 ChainOfThought 抽取答案


def test_code_line_counter_skips_docstrings_comments_and_bootstrap():
    shared = _load("shared_tools")
    for name in ("impl_agentkit", "impl_dspy", "impl_langgraph", "impl_openai_agents"):
        n = shared.count_code_lines(HERE / f"{name}.py")
        assert 20 < n < 200, (name, n)
