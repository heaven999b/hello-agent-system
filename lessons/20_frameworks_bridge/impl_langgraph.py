"""同一任务 · LangGraph 版（图 / 状态机：你画出节点和边，运行时负责执行、存检查点、暂停与恢复）。

    pip install langgraph langchain-openai
    python lessons/20_frameworks_bridge/impl_langgraph.py

对照要点：
  状态      State(TypedDict)：messages 字段带 reducer（add_messages = 追加而不是覆盖）
  工具      langchain_core.tools.tool 把函数包装成工具；model.bind_tools(...) 把 Schema 交给模型
  循环      没有 while：循环是图里的一条回边 tools → agent；agent 没有工具调用时条件边走向 END。
            recursion_limit（默认 1000）限制的是超步数（super-step），不是模型调用次数
  审批      approval 节点里调用 interrupt(payload) → ainvoke 返回的结果里带 "__interrupt__"；
            审批后 ainvoke(Command(resume=决定), 同一个 thread_id) 继续。
            ⚠️ 恢复时 approval 节点会**从头重新执行**，所以它必须没有副作用——这正是把审批单独做成一个节点的原因
  追踪      每个超步一个检查点：get_state_history(config) 就是一条可回放的执行记录；
            在线追踪用 LangSmith（设置 LANGSMITH_TRACING=true 等环境变量），本 demo 不开启
  async     invoke / ainvoke 两套入口，本文件用 await graph.ainvoke(...)；节点写成 async def，
            await 模型（llm.ainvoke）和工具（tool.ainvoke）。同步节点在 ainvoke 下会被放进线程池执行
"""

from __future__ import annotations

# --- bootstrap（四个 impl 文件相同：按路径加载同目录模块，不计入行数对比）---
import importlib.util
import sys
from pathlib import Path


def _load_sibling(name: str):
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


shared = _load_sibling("shared_tools")
# --- end bootstrap ---

import asyncio
import time
import uuid
from collections import Counter
from importlib.metadata import version
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt


class HelpdeskState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]  # reducer：节点返回的新消息追加到末尾
    decisions: dict[str, bool]  # tool_call_id → 审批结果（没有 reducer：后写覆盖）


def make_model():
    from langchain_openai import ChatOpenAI

    cfg = shared.gateway_config()
    return ChatOpenAI(
        model=cfg["model"],
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        use_responses_api=False,  # 显式走 Chat Completions：很多兼容网关不支持 /responses
        max_retries=2,
    )


def build_graph(model, desk, counter: Counter):
    tools = {t.name: t for t in (tool(f) for f in desk.functions().values())}
    llm = model.bind_tools(list(tools.values()))

    # 节点可以是 async 函数：图用 ainvoke 驱动时，等模型、等工具都不占线程（同步节点会被放进线程池）
    async def agent(state: HelpdeskState) -> dict:
        counter["agent"] += 1  # 每执行一次 agent 节点 = 一次模型调用
        return {"messages": [await llm.ainvoke(state["messages"])]}

    async def approval(state: HelpdeskState) -> dict:
        # 恢复时本节点从头重跑：这里只读状态、调用 interrupt，不产生任何副作用
        counter["approval"] += 1
        decisions = dict(state.get("decisions") or {})
        for call in state["messages"][-1].tool_calls:
            if call["name"] in shared.NEEDS_APPROVAL and call["id"] not in decisions:
                decisions[call["id"]] = bool(interrupt({"tool": call["name"], "args": call["args"]}))
        return {"decisions": decisions}

    async def run_tools(state: HelpdeskState) -> dict:
        counter["tools"] += 1
        decisions = state.get("decisions") or {}
        results = []
        for call in state["messages"][-1].tool_calls:
            if decisions.get(call["id"], True) is False:
                content = "审批人拒绝了该操作。请告诉用户该操作未获批准，不要重试。"
            elif call["name"] not in tools:
                content = f"错误：不存在名为 {call['name']} 的工具"
            else:
                content = str(await tools[call["name"]].ainvoke(call["args"]))  # 同步函数的工具，ainvoke 放进线程池执行
            results.append(ToolMessage(content=content, tool_call_id=call["id"]))
        return {"messages": results}

    def route(state: HelpdeskState) -> Literal["approval", "__end__"]:
        return "approval" if state["messages"][-1].tool_calls else END

    builder = StateGraph(HelpdeskState)
    builder.add_node("agent", agent)
    builder.add_node("approval", approval)
    builder.add_node("tools", run_tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route)
    builder.add_edge("approval", "tools")
    builder.add_edge("tools", "agent")  # 回边：这就是"Agent 循环"
    return builder.compile(checkpointer=InMemorySaver())  # 生产换成 PostgresSaver 等持久化实现


async def run(question: str = shared.QUESTION, approver=shared.auto_approver, model=None) -> "shared.FrameworkResult":
    desk = shared.ITDesk(user_id="alice")
    counter: Counter = Counter()
    graph = build_graph(model or make_model(), desk, counter)
    config = {"configurable": {"thread_id": f"it-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 25}
    approvals = []
    t0 = time.perf_counter()
    inputs = {"messages": [SystemMessage(shared.SYSTEM_PROMPT), HumanMessage(question)], "decisions": {}}
    result = await graph.ainvoke(inputs, config)
    while result.get("__interrupt__"):  # 暂停：状态已在 checkpointer 里，凭 thread_id 随时恢复
        payload = result["__interrupt__"][0].value
        ok = approver(payload["tool"], payload["args"])
        approvals.append((payload["tool"], payload["args"], ok))
        result = await graph.ainvoke(Command(resume=ok), config)
    seconds = time.perf_counter() - t0
    ai = [m for m in result["messages"] if m.type == "ai"]
    usage = [m.usage_metadata or {} for m in ai]
    checkpoints = [cp async for cp in graph.aget_state_history(config)]
    return shared.FrameworkResult(
        framework="LangGraph",
        version=f"{version('langgraph')} (+langchain-openai {version('langchain-openai')})",
        answer=str(result["messages"][-1].content),
        llm_calls=counter["agent"],
        tool_calls=desk.tool_names(),
        approvals=approvals,
        seconds=seconds,
        input_tokens=sum(u.get("input_tokens", 0) for u in usage),
        output_tokens=sum(u.get("output_tokens", 0) for u in usage),
        notes=[
            f"检查点 {len(checkpoints)} 个（每个超步一个，get_state_history 可逐个回放）",
            f"节点执行次数 {dict(counter)}；审批 {len(approvals)} 次——approval 多出来的执行 = 恢复时节点从头重跑",
        ],
        extra={"node_runs": dict(counter), "checkpoints": len(checkpoints)},
    )


if __name__ == "__main__":
    shared.print_result(asyncio.run(run()))
