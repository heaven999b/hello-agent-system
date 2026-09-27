"""第 02 课参考答案。接口与 exercise.py 完全一致。

建议先自己写，卡住超过 10 分钟再来看。对照 agentkit/agent.py 的 _loop()，
你会发现"企业版"只是在这个骨架上加了钩子、检查点和追踪。
"""

from __future__ import annotations

import inspect
import json
import typing
from typing import Any, Callable

from agentkit.llm import LLM
from agentkit.types import Message, ToolCall, system, tool_message, user

SYSTEM_PROMPT = "你是一个有帮助的助手。需要外部信息时调用工具；工具返回错误时，根据错误信息修正参数后重试。"

# ---------------------------------------------------------------------------
# 已实现：把普通 Python 函数转换成 OpenAI function-calling 格式的工具定义。
# ---------------------------------------------------------------------------

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def tool_schema(name: str, fn: Callable[..., Any]) -> dict:
    """根据函数签名生成工具定义。docstring 作为工具描述，没有默认值的参数是必填项。"""
    hints = typing.get_type_hints(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for pname, param in inspect.signature(fn).parameters.items():
        properties[pname] = {"type": _JSON_TYPES.get(hints.get(pname, str), "string")}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": inspect.getdoc(fn) or name,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def build_tool_schemas(tools: dict[str, Callable[..., Any]]) -> list[dict]:
    """把 {工具名: 函数} 转成发给模型的工具定义列表。"""
    return [tool_schema(name, fn) for name, fn in tools.items()]


# ---------------------------------------------------------------------------
# TODO 1：执行一次工具调用
# ---------------------------------------------------------------------------


def execute_tool_call(tools: dict[str, Callable[..., Any]], call: ToolCall) -> str:
    """执行模型发起的一次工具调用，永远返回字符串，永远不抛异常（错误即观察）。"""
    fn = tools.get(call.name)
    if fn is None:
        available = ", ".join(tools) or "（无）"
        return f"错误：不存在名为 {call.name!r} 的工具。可用工具：{available}"

    try:
        args = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as e:
        return f"错误：参数不是合法的 JSON（{e}）。请重新生成参数。"
    if not isinstance(args, dict):
        return '错误：参数必须是 JSON 对象，例如 {"city": "北京"}。请重新生成参数。'

    try:
        result = fn(**args)
    except Exception as e:  # noqa: BLE001 —— 任何异常都要变成观察，不能让 Agent 崩溃
        return f"错误：工具 {call.name} 执行失败：{type(e).__name__}: {e}"

    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# TODO 2：Agent 主循环
# ---------------------------------------------------------------------------


def run_agent_loop(
    llm: LLM,
    tools: dict[str, Callable[..., Any]],
    user_input: str,
    max_steps: int = 5,
) -> dict:
    """Agent 主循环：让模型在循环里调用工具，直到它给出最终答案或达到步数上限。"""
    if max_steps < 1:
        raise ValueError(f"max_steps 必须 >= 1，收到 {max_steps}")

    schemas = build_tool_schemas(tools) or None  # 没有工具就不传 tools
    messages: list[Message] = [system(SYSTEM_PROMPT), user(user_input)]

    for step in range(1, max_steps + 1):
        response = llm.chat(messages, tools=schemas)
        messages.append(response.to_message())  # assistant 消息必须先于它的 tool 结果

        if not response.tool_calls:  # 没有工具调用 = 模型认为任务完成
            return {
                "output": response.content or "",
                "messages": messages,
                "steps": step,
                "stop_reason": "final_answer",
            }

        for call in response.tool_calls:  # 模型可能一次并行发起多个调用，每个都要回应
            messages.append(tool_message(call.id, execute_tool_call(tools, call)))

    return {"output": None, "messages": messages, "steps": max_steps, "stop_reason": "max_steps"}
