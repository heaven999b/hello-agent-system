"""核心数据类型。

我们直接使用 OpenAI Chat Completions 的消息格式（普通 dict），原因：
1. 它是事实上的行业标准，几乎所有模型网关都兼容；
2. 你能直接看到"发给模型的到底是什么"，没有任何框架魔法。

一条消息长这样：
    {"role": "system" | "user" | "assistant" | "tool", "content": "..."}
assistant 消息可能带 tool_calls；tool 消息必须带 tool_call_id，指明它回答的是哪次调用。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field

Message = dict  # 一条 OpenAI 格式消息


@dataclass
class ToolCall:
    """模型发起的一次工具调用。

    注意 arguments 是 **字符串**：模型输出的 JSON 可能不合法，
    解析和校验是工具层的责任（见 tools.py），绝不能假设它一定正确。
    """

    id: str
    name: str
    arguments: str = "{}"

    def parsed_args(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass
class LLMResponse:
    """一次模型调用的结果：要么是最终文本，要么是若干工具调用（也可能两者都有）。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = "stop"

    def to_message(self) -> Message:
        """转成可以追加回对话历史的 assistant 消息。"""
        msg: Message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return msg


# ---------- 消息构造小工具 ----------

def system(text: str) -> Message:
    return {"role": "system", "content": text}


def user(text: str) -> Message:
    return {"role": "user", "content": text}


def assistant(text: str) -> Message:
    return {"role": "assistant", "content": text}


def tool_message(call_id: str, content: str) -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def calls_in(message: Message) -> list[ToolCall]:
    """从 assistant 消息里取出 ToolCall 列表。"""
    return [
        ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=tc["function"].get("arguments") or "{}")
        for tc in message.get("tool_calls") or []
    ]


_ids = itertools.count(1)


def new_call_id() -> str:
    return f"call_{next(_ids)}"
