"""第 02 课练习：亲手写出 Agent 主循环。

你要实现两个函数：

    execute_tool_call(tools, call)            —— 执行一次工具调用，永远返回字符串（成功结果或错误说明）
    run_agent_loop(llm, tools, user_input)    —— Agent 主循环

写完后运行：
    make lesson N=02
    # 或
    .venv/bin/python -m pytest lessons/02_agent_loop

提示：
- llm 符合 agentkit.llm.LLM 协议：llm.chat(messages, tools=...) -> LLMResponse
  LLMResponse 有 .content（文本）、.tool_calls（list[ToolCall]）和 .to_message()（转成 assistant 消息）。
- ToolCall 有 .id / .name / .arguments（注意 arguments 是 **JSON 字符串**，不是 dict）。
- agentkit.types 里有构造消息的小工具：system(text) / user(text) / tool_message(call_id, content)。
- 卡住了？先读 lessons/02_agent_loop/demo_raw.py，它就是一个不带错误处理的最小版本。
"""

from __future__ import annotations

import inspect
import json  # noqa: F401  实现时会用到
import typing
from typing import Any, Callable

from agentkit.llm import LLM
from agentkit.types import Message, ToolCall, system, tool_message, user  # noqa: F401

SYSTEM_PROMPT = "你是一个有帮助的助手。需要外部信息时调用工具；工具返回错误时，根据错误信息修正参数后重试。"

# ---------------------------------------------------------------------------
# 已实现：把普通 Python 函数转换成 OpenAI function-calling 格式的工具定义。
# （这是一个极简版本，第 03 课会讲用 Pydantic 生成更完整的 Schema。）
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
    """执行模型发起的一次工具调用，**永远返回字符串，永远不抛异常**。

    这是"错误即观察"原则：工具层的任何问题都要变成一段文字反馈给模型，
    让模型自己决定怎么办（换参数重试、换工具、或者如实告诉用户），而不是让整个 Agent 崩溃。

    需要处理的情况（按顺序检查）：
    1. 工具不存在（模型可能"幻想"出一个工具名）
       → 返回以 "错误"（或英文 "Error"）开头的字符串，包含这个未知的工具名，并列出所有可用的工具名。
    2. arguments 不是合法 JSON（模型偶尔会输出 '{a: 1' 这种东西）
       → 返回以 "错误" 开头、包含 "JSON" 字样的字符串。
       注意：arguments 为空字符串 "" 时视为 {}（有些模型对无参工具会给空字符串）。
    3. arguments 是合法 JSON 但不是对象（比如 '[1, 2]'）
       → 同样返回以 "错误" 开头的字符串。
    4. 调用函数 fn(**args) 时抛出任何异常（包括参数名不对导致的 TypeError）
       → 返回以 "错误" 开头的字符串，包含异常类型名（如 "RuntimeError"）和异常消息。
    5. 成功：
       - 结果是 str → 原样返回；
       - 否则 → json.dumps(result, ensure_ascii=False, default=str)（中文不要被转义成 \\uXXXX）。

    提示：用 tools.get(call.name) 查找工具；用 try/except Exception 兜底。
    """
    raise NotImplementedError("TODO 1: 实现 execute_tool_call —— 执行工具，把任何错误都变成以'错误'开头的文字")


# ---------------------------------------------------------------------------
# TODO 2：Agent 主循环
# ---------------------------------------------------------------------------


def run_agent_loop(
    llm: LLM,
    tools: dict[str, Callable[..., Any]],
    user_input: str,
    max_steps: int = 5,
) -> dict:
    """Agent 主循环：让模型在循环里调用工具，直到它给出最终答案或达到步数上限。

    返回 dict：
        {
            "output": str | None,     # 最终答案；达到步数上限时为 None
            "messages": list[dict],   # 完整的消息历史（OpenAI 格式）
            "steps": int,             # 调用模型的次数
            "stop_reason": str,       # "final_answer" 或 "max_steps"
        }

    步骤：
    1. max_steps < 1 时抛 ValueError（这是调用方的编程错误，应该尽早暴露）。
    2. 初始消息：[system(SYSTEM_PROMPT), user(user_input)]
    3. 工具定义：build_tool_schemas(tools)；如果没有任何工具，传 tools=None 而不是空列表
       （空数组对模型没有意义，部分服务端还会因此报 400）。
    4. 循环最多 max_steps 次：
       a. response = llm.chat(messages, tools=...)
       b. 把 response.to_message() 追加到 messages（assistant 消息必须先于它的 tool 结果）
       c. 如果 response.tool_calls 为空 → 这就是最终答案：
          output = response.content or ""，stop_reason = "final_answer"，立即返回。
          注意：判断依据是"有没有 tool_calls"，不是 content 是否为空——
          模型可能一边说"我先查一下"一边发起工具调用，这时还没结束。
       d. 否则，按顺序执行**每一个** tool_call（模型可能一次并行发起多个），
          每个结果追加一条 tool_message(call.id, 结果)。tool_call_id 必须和调用一一对应。
    5. 循环结束仍没有最终答案 → output=None，stop_reason="max_steps"，steps=max_steps。
       即使是最后一步，也要把该步的工具都执行完、结果都追加进 messages ——
       保证消息历史永远"配对完整"，这样之后可以从这里续跑（第 08 课的检查点就依赖这一点）。

    不需要处理的：llm.chat 本身抛出的异常（让它向上抛；重试和降级是第 08 课的内容）。
    """
    raise NotImplementedError("TODO 2: 实现 run_agent_loop —— while 循环：调模型 → 执行工具 → 结果喂回去")
