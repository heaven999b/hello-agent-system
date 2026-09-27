"""同一任务 · OpenAI Agents SDK 版（轻量原语：Agent + Runner + 工具 + 护栏 + handoff + Session + 追踪）。

    pip install openai-agents
    python lessons/20_frameworks_bridge/impl_openai_agents.py

对照要点：
  状态      RunState：运行到一半的完整状态，可 to_string() / from_string() 序列化跨进程恢复；
            Session（SQLiteSession 等）另外负责"多轮对话历史"，两者用途不同
  工具      @function_tool：从类型注解 + docstring（Args: 段）生成 strict JSON Schema；needs_approval=True 标记要审批
  循环      Runner.run(agent, input, max_turns=...)：和 agentkit 的 Agent.run 几乎一一对应，超限抛 MaxTurnsExceeded
  审批      result.interruptions 里是待审批的 ToolApprovalItem → state = result.to_state() →
            state.approve(item) / state.reject(item) → Runner.run(agent, state) 继续
  追踪      **默认开启并上传到 OpenAI**。本地网关场景必须关掉（set_tracing_disabled）或替换处理器
            （set_trace_processors）——这里用一个只在内存里计数的处理器替换掉默认的上传处理器

连接本地网关：默认的 OpenAIResponsesModel 走 /responses 接口，很多兼容网关只支持 /chat/completions，
所以显式用 OpenAIChatCompletionsModel(model=..., openai_client=AsyncOpenAI(base_url=...))。
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
import json
import time
from collections import Counter
from importlib.metadata import version

from agents import Agent, OpenAIChatCompletionsModel, RunState, Runner, function_tool, set_trace_processors
from agents.tracing import TracingProcessor
from openai import AsyncOpenAI


class LocalSpanCounter(TracingProcessor):
    """替换默认的"上传到 OpenAI"处理器：只在本地数 Span。真实项目里在这里转发到 OpenTelemetry / Langfuse。"""

    def __init__(self):
        self.spans: Counter = Counter()

    def on_trace_start(self, trace) -> None: ...

    def on_trace_end(self, trace) -> None: ...

    def on_span_start(self, span) -> None: ...

    def on_span_end(self, span) -> None:
        self.spans[type(span.span_data).__name__.removesuffix("SpanData")] += 1

    def shutdown(self) -> None: ...

    def force_flush(self) -> None: ...


def make_model() -> OpenAIChatCompletionsModel:
    cfg = shared.gateway_config()
    client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], max_retries=2)
    return OpenAIChatCompletionsModel(model=cfg["model"], openai_client=client)


def build_agent(model, desk) -> Agent:
    fns = desk.functions()
    return Agent(
        name="it-helpdesk",
        instructions=shared.SYSTEM_PROMPT,
        model=model,
        tools=[
            function_tool(fns["search_kb"]),
            function_tool(fns["get_account_status"]),
            function_tool(fns["reset_password"], needs_approval=True),  # 审批是工具的属性
        ],
    )


async def _run(question: str, approver, model) -> "shared.FrameworkResult":
    desk = shared.ITDesk(user_id="alice")
    agent = build_agent(model or make_model(), desk)
    tracer = LocalSpanCounter()
    set_trace_processors([tracer])  # 替换（而不是追加）处理器：默认的上传处理器被移除
    approvals = []
    t0 = time.perf_counter()
    result = await Runner.run(agent, question, max_turns=8)
    while result.interruptions:
        # 模拟"暂停 → 落盘 → 过一阵子在另一个进程里恢复"：序列化成字符串再读回来
        saved = result.to_state().to_string()
        state = await RunState.from_string(agent, saved)
        for item in result.interruptions:
            args = json.loads(item.arguments or "{}")
            ok = approver(item.name, args)
            approvals.append((item.name, args, ok))
            if ok:
                state.approve(item)
            else:
                state.reject(item)
        result = await Runner.run(agent, state, max_turns=8)
    seconds = time.perf_counter() - t0
    usage = result.context_wrapper.usage  # 恢复后的用量是累计值（RunState 里带着）
    return shared.FrameworkResult(
        framework="OpenAI Agents SDK",
        version=version("openai-agents"),
        answer=str(result.final_output),
        llm_calls=usage.requests,
        tool_calls=desk.tool_names(),
        approvals=approvals,
        seconds=seconds,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        notes=[
            f"本地追踪处理器收到的 Span：{dict(tracer.spans)}（没有任何数据上传到 OpenAI）",
            f"result.raw_responses = {len(result.raw_responses)}（恢复后的结果里包含恢复前的响应，别拿它直接相加）",
        ],
        extra={"spans": dict(tracer.spans)},
    )


def run(question: str = shared.QUESTION, approver=shared.auto_approver, model=None) -> "shared.FrameworkResult":
    return asyncio.run(_run(question, approver, model))


if __name__ == "__main__":
    shared.print_result(run())
