"""第 01 课参考答案。接口与 exercise.py 完全一致。

建议先自己写，卡住超过 10 分钟再来看。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Sequence

from agentkit.context import estimate_tokens
from agentkit.types import Message, ToolCall

# ---------------------------------------------------------------------------
# 已实现：成本估算的返回类型
# ---------------------------------------------------------------------------


@dataclass
class CostEstimate:
    """一次请求的成本估算结果（token 数都是估算值，真实用量以 API 返回的 usage 为准）。"""

    message_tokens: int  # 消息部分（system / user / assistant / tool）的估算 token
    tool_tokens: int  # 工具定义（JSON Schema）的估算 token —— 新手最常漏掉的一项
    input_tokens: int  # = message_tokens + tool_tokens
    cached_input_tokens: int  # 其中预计命中提示词缓存的部分
    output_tokens: int  # 预计输出 token
    cost_usd: float


# ---------------------------------------------------------------------------
# TODO (a)：拼接流式返回中分片到达的工具调用
# ---------------------------------------------------------------------------


def accumulate_tool_call_deltas(chunks: Sequence[dict]) -> list[ToolCall]:
    """把 OpenAI Chat Completions 流式返回的 chunk 拼成完整的工具调用列表。"""
    parts: dict[int, dict] = {}
    for chunk in chunks:
        choices = chunk.get("choices") or []
        if not choices:  # 例如最后那个只带 usage 的 chunk
            continue
        delta = choices[0].get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            index = tc.get("index", 0)
            slot = parts.setdefault(index, {"id": None, "name": None, "arguments": []})
            fn = tc.get("function") or {}
            # id / name 只在第一片出现；后面的分片里它们是 None 或干脆没有 —— 不能被覆盖成 None
            if tc.get("id") and not slot["id"]:
                slot["id"] = tc["id"]
            if fn.get("name") and not slot["name"]:
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"].append(fn["arguments"])

    calls: list[ToolCall] = []
    for index in sorted(parts):
        slot = parts[index]
        if not slot["id"] or not slot["name"]:
            raise ValueError(f"index={index} 的工具调用缺少 id 或 name：流可能被截断，或者网关的流式格式不兼容")
        arguments = "".join(slot["arguments"]) or "{}"
        calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=arguments))
    return calls


# ---------------------------------------------------------------------------
# TODO (b)：估算一次请求的成本（别忘了工具定义）
# ---------------------------------------------------------------------------


def estimate_request_cost(
    messages: list[Message],
    tools: list[dict] | None,
    expected_output_tokens: int,
    price: tuple[float, ...],
    *,
    cached_input_tokens: int = 0,
) -> CostEstimate:
    """估算一次模型请求的 token 数和成本。"""
    if expected_output_tokens < 0 or cached_input_tokens < 0:
        raise ValueError("token 数不能为负")
    if len(price) not in (2, 3):
        raise ValueError("price 必须是 (输入单价, 输出单价) 或 (输入单价, 输出单价, 缓存命中单价)")

    message_tokens = estimate_tokens(messages)
    tool_tokens = estimate_tokens([{"role": "system", "content": json.dumps(tools, ensure_ascii=False)}]) if tools else 0
    input_tokens = message_tokens + tool_tokens
    cached = min(cached_input_tokens, input_tokens)

    price_in, price_out = price[0], price[1]
    price_cached = price[2] if len(price) == 3 else price_in
    cost = ((input_tokens - cached) * price_in + cached * price_cached + expected_output_tokens * price_out) / 1_000_000
    return CostEstimate(message_tokens, tool_tokens, input_tokens, cached, expected_output_tokens, cost)


# ---------------------------------------------------------------------------
# TODO (c)：拼装一次请求的消息列表
# ---------------------------------------------------------------------------


def build_messages(system: str, history: list[Message], user_input: str, max_history_turns: int) -> list[Message]:
    """拼装发给模型的 messages：system 在最前，历史按"轮"截断，最后是本次用户输入。"""
    if max_history_turns < 0:
        raise ValueError("max_history_turns 不能为负")
    if not user_input or not user_input.strip():
        raise ValueError("user_input 不能为空")

    # 1) 按轮切分：一轮从一条 user 消息开始，直到下一条 user 消息之前。
    #    assistant(tool_calls) 和它的 tool 结果一定落在同一轮里，所以按轮截断永远不会把它们拆开。
    turns: list[list[Message]] = []
    for m in history:
        role = m.get("role")
        if role == "system":  # system 只能出现在最前面，历史里的旧 system 消息丢掉
            continue
        if role == "user":
            turns.append([m])
        elif turns:
            turns[-1].append(m)
        # else：第一条 user 之前的消息（孤立的 assistant / tool）不属于任何完整的轮，丢弃

    # 2) 只保留最近 N 轮；3) 深拷贝，不修改调用方的 history
    kept = turns[-max_history_turns:] if max_history_turns > 0 else []
    body = [copy.deepcopy(m) for turn in kept for m in turn]
    return [{"role": "system", "content": system}, *body, {"role": "user", "content": user_input}]
