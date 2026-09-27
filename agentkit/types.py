"""核心数据类型。

我们直接使用 OpenAI Chat Completions 的消息格式（普通 dict），原因：
1. 它是事实上的行业标准，几乎所有模型网关都兼容；
2. 你能直接看到"发给模型的到底是什么"，没有任何框架魔法。

一条消息长这样：
    {"role": "system" | "user" | "assistant" | "tool", "content": "..."}
assistant 消息可能带 tool_calls；tool 消息必须带 tool_call_id，指明它回答的是哪次调用。
"""

from __future__ import annotations

import json
import uuid
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
    cached_input_tokens: int = 0  # 输入中命中提示词缓存的部分（通常更便宜、更快；网关不支持时为 0）
    reasoning_tokens: int = 0  # 输出中推理模型"思考"用掉的部分（已包含在 output_tokens 里，按输出价计费）

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )


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


def new_call_id() -> str:
    """随机的工具调用 ID。不要用进程内自增计数器：多进程 / 多 worker 时各自从 call_1 开始，
    会让 idempotency_key = run_id:call_id 在崩溃恢复时撞车。"""
    return f"call_{uuid.uuid4().hex[:12]}"
