"""第 01 课练习：三个"写 Agent 之前就该会"的基本功。

你要实现三个函数：

    accumulate_tool_call_deltas(chunks)                      —— (a) 把流式返回里分片到达的工具调用拼完整
    estimate_request_cost(messages, tools, out_tokens, price) —— (b) 估算一次请求的成本，别漏了工具定义
    build_messages(system, history, user_input, max_turns)    —— (c) 拼装消息：system 在前、按轮截断、不拆开工具调用

写完后运行：
    make lesson N=01
    # 或
    .venv/bin/python -m pytest lessons/01_llm_essentials

提示：
- 这三个函数都是纯函数，不调用模型，所以测试离线、结果确定。
- ToolCall 在 agentkit/types.py：ToolCall(id=..., name=..., arguments="JSON 字符串")。
- estimate_tokens 在 agentkit/context.py：粗略估算，中文约 1 字 1 token，其他约 4 字符 1 token，每条消息 +4。
- 卡住了？先跑一遍 demo.py，第 4 节会把真实的流式分片一片一片打印出来。
"""

from __future__ import annotations

import copy  # noqa: F401  实现时会用到
import json  # noqa: F401  实现时会用到
from dataclasses import dataclass
from typing import Sequence

from agentkit.context import estimate_tokens  # noqa: F401  实现时会用到
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
    """把 OpenAI Chat Completions 流式返回的 chunk 拼成完整的工具调用列表。

    每个 chunk 是一个 dict（即 SDK 对象 chunk.model_dump() 的结果），工具调用的分片长这样：

        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_abc", "type": "function",
             "function": {"name": "get_weather", "arguments": ""}}      ← 第一片：带 id 和 name
        ]}}]}
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\\"ci"}}           ← 后续分片：只有 arguments 的一小段
        ]}}]}
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "ty\\": \\"北京\\"}"}}
        ]}}]}

    规则：
    1. 用 tool_calls 里的 "index" 区分不同的调用（并行调用时会有 index 0、1、2……）。
       不同 index 的分片可能**交错**到达，不能假设"先把 0 发完再发 1"。
    2. id 和 function.name 只在某个调用的第一片出现；后续分片里它们是 None 或者干脆没有这个键。
       以第一次出现的非空值为准，不要被后面的 None 覆盖。
    3. function.arguments 是一段一段的字符串，按到达顺序拼接（None 或缺失视为空串）。
       拼完后如果是空串，用 "{}"（无参数的工具，有的服务端一片 arguments 都不发）。
    4. **不要**在这里解析或校验 JSON：流被截断时 arguments 本来就可能不完整，
       解析和校验是工具层的责任（第 03 课）。原样保留拼接结果。
    5. 忽略与工具调用无关的 chunk：choices 为空（最后那个只带 usage 的 chunk）、
       delta 里只有 content、只有 role、只有 finish_reason 等。只看 choices[0]。
    6. 返回的列表按 index 从小到大排序。没有任何工具调用时返回 []。
    7. 某个 index 直到最后都没收到 id 或 name → 抛 ValueError
       （没有 id 就无法用 tool_call_id 回应这次调用，继续下去下一轮请求会 400）。

    写完后对照生产版 agentkit.llm.ToolCallAccumulator（OpenAICompatLLM.stream() 用它拼工具调用）：
    它直接处理 SDK 的分片对象，缺 id 时补一个本地 id 而不是报错 —— 同一个问题的另一种取舍。
    """
    raise NotImplementedError("TODO (a): 按 index 分组，id/name 取第一个非空值，arguments 按顺序拼接")


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
    """估算一次模型请求的 token 数和成本（美元）。

    参数：
        messages               本次要发送的全部消息（每次都是完整历史，因为 API 是无状态的）
        tools                  工具定义列表（OpenAI function-calling 格式），可以是 None 或 []
        expected_output_tokens 预计的输出 token 数（推理模型的"思考"也算输出）
        price                  单价，单位：美元 / 100 万 token，和 agentkit/pricing.py 的 PRICES 格式一致：
                               (输入单价, 输出单价) 或 (输入单价, 输出单价, 缓存命中的输入单价)
        cached_input_tokens    预计命中提示词缓存的输入 token 数

    计算规则（测试按这个规则算期望值）：
    1. message_tokens = estimate_tokens(messages)
    2. tool_tokens：工具定义在服务端会被拼进提示词，**按输入计费、也占上下文窗口**。
       把整个 tools 列表 json.dumps(tools, ensure_ascii=False) 当成一条 system 消息来估算：
           estimate_tokens([{"role": "system", "content": <上面的 JSON 字符串>}])
       tools 为 None 或空列表时为 0。
    3. input_tokens = message_tokens + tool_tokens
    4. 缓存命中的部分不能超过 input_tokens（超过就截到 input_tokens）。
    5. price 只有两项时，缓存命中部分按普通输入单价计算（保守估计）。
    6. cost_usd = (未命中缓存的输入 × 输入单价 + 命中缓存的输入 × 缓存单价 + 输出 × 输出单价) / 1_000_000
    7. expected_output_tokens 或 cached_input_tokens 为负数 → ValueError；
       price 的长度不是 2 或 3 → ValueError。

    返回 CostEstimate（上面已定义好）。
    """
    raise NotImplementedError("TODO (b): 消息 token + 工具定义 token = 输入 token，再按三种单价算钱")


# ---------------------------------------------------------------------------
# TODO (c)：拼装一次请求的消息列表
# ---------------------------------------------------------------------------


def build_messages(system: str, history: list[Message], user_input: str, max_history_turns: int) -> list[Message]:
    """拼装发给模型的 messages：[system, ...最近 N 轮历史..., 本次 user 输入]。

    什么是"一轮"（turn）：从一条 user 消息开始，到下一条 user 消息之前的所有消息。例如

        user 你好                                          ┐ 第 1 轮
        assistant 你好！有什么可以帮你？                   ┘
        user 北京天气？                                    ┐
        assistant (tool_calls: get_weather)                │ 第 2 轮：assistant(tool_calls) 和它的
        tool {"temp": 31}                                  │ tool 结果一定在同一轮里，所以按轮截断
        assistant 北京 31 度。                             ┘ 永远不会把它们拆开

    规则：
    1. 返回的第一条永远是 {"role": "system", "content": system}，而且只有这一条 system 消息：
       history 里如果混进了 system 消息，丢掉。
    2. 历史只保留最近 max_history_turns 轮；为 0 时不带任何历史。
    3. history 开头、第一条 user 消息之前的消息（例如被错误截断后留下的孤立 tool 消息）
       不属于任何完整的轮，丢掉 —— 孤立的 tool 消息会让 API 直接报 400。
    4. 最后一条是 {"role": "user", "content": user_input}。
    5. 不要修改调用方传进来的 history（包括其中的 dict），需要的话深拷贝。
    6. max_history_turns 为负数 → ValueError；user_input 为空或全是空白 → ValueError
       （空请求发给模型只会浪费钱）。
    """
    raise NotImplementedError("TODO (c): 按 user 消息切分成轮，保留最近 N 轮，system 放最前，user_input 放最后")
