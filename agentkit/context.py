"""上下文工程：决定每次调用模型时"让它看到什么"。

上下文窗口是 Agent 最稀缺的资源：
- 太长 → 贵、慢、超限报错，而且模型会"迷失在中间"（lost in the middle），效果反而变差；
- 太短 → 模型忘了用户目标、重复调用工具。

两种基础策略：
1. SlidingWindow   滑动窗口：只保留最近的消息（简单、便宜、会丢信息）
2. SummarizingCompactor 摘要压缩：把早期对话压成摘要（保留要点、多一次模型调用）

⚠️ 最常见的坑：截断时把 assistant 的 tool_calls 和对应的 tool 结果拆开了。
孤立的 tool 消息会让 API 直接报 400 错误。所以我们按"块"截断：
一个 assistant(tool_calls) + 它的全部 tool 结果 = 一个不可分割的块。
"""

from __future__ import annotations

import json
import re

from .types import Message

_CJK = re.compile(r"[一-鿿　-〿＀-￯]")


def estimate_tokens(messages: list[Message]) -> int:
    """粗略估算 token 数：中文约 1 字 ≈ 1 token，其他约 4 字符 ≈ 1 token，每条消息再加 4 的格式开销。
    生产中请用模型对应的 tokenizer（如 tiktoken）或 API 返回的真实用量。"""
    total = 0
    for m in messages:
        text = m.get("content") or ""
        if m.get("tool_calls"):
            text += json.dumps(m["tool_calls"], ensure_ascii=False)
        cjk = len(_CJK.findall(text))
        total += cjk + (len(text) - cjk) // 4 + 4
    return total


def split_blocks(messages: list[Message]) -> tuple[list[Message], list[list[Message]]]:
    """拆成 (开头的 system 消息, 不可分割的消息块列表)。"""
    i = 0
    head: list[Message] = []
    while i < len(messages) and messages[i]["role"] == "system":
        head.append(messages[i])
        i += 1
    blocks: list[list[Message]] = []
    for m in messages[i:]:
        if m["role"] == "tool" and blocks:
            blocks[-1].append(m)  # tool 结果跟着它前面的 assistant 走
        else:
            blocks.append([m])
    return head, blocks


class SlidingWindow:
    """保留 system + 尽量多的最近消息块，总量不超过 max_tokens。至少保留最后一个块。"""

    def __init__(self, max_tokens: int = 6000):
        self.max_tokens = max_tokens

    def apply(self, messages: list[Message]) -> list[Message]:
        if estimate_tokens(messages) <= self.max_tokens:
            return messages
        head, blocks = split_blocks(messages)
        budget = self.max_tokens - estimate_tokens(head)
        kept: list[list[Message]] = []
        for block in reversed(blocks):
            cost = estimate_tokens(block)
            if kept and cost > budget:
                break
            kept.insert(0, block)
            budget -= cost
        return head + [m for b in kept for m in b]


SUMMARY_MARKER = "\n\n## 早前对话摘要（系统自动生成）\n"

SUMMARY_PROMPT = """请把下面这段 Agent 对话历史压缩成简洁的要点摘要，供后续继续工作使用。必须保留：
1. 用户的最终目标和明确提出的约束/偏好；
2. 已经确认的关键事实和数据（包括工具返回的关键数字、ID）；
3. 已经做出的决定和已完成的操作（避免重复执行）；
4. 尚未完成的事项。
不要编造，不要写客套话。

对话历史：
{history}"""


class SummarizingCompactor:
    """超过 max_tokens 时，把较早的消息块交给模型做摘要，拼进 system 消息；最近的块原样保留。"""

    def __init__(self, llm, max_tokens: int = 6000, keep_recent_tokens: int = 2000):
        self.llm = llm
        self.max_tokens = max_tokens
        self.keep_recent_tokens = keep_recent_tokens
        self.compactions = 0

    def apply(self, messages: list[Message]) -> list[Message]:
        if estimate_tokens(messages) <= self.max_tokens:
            return messages
        head, blocks = split_blocks(messages)
        recent: list[list[Message]] = []
        budget = self.keep_recent_tokens
        for block in reversed(blocks):
            cost = estimate_tokens(block)
            if recent and cost > budget:
                break
            recent.insert(0, block)
            budget -= cost
        old = blocks[: len(blocks) - len(recent)]
        if not old:
            return messages

        base_system = head[0]["content"] if head else ""
        previous_summary = ""
        if SUMMARY_MARKER in base_system:
            base_system, previous_summary = base_system.split(SUMMARY_MARKER, 1)
        history = "\n".join(_render(m) for b in old for m in b)
        if previous_summary:
            history = f"[更早的摘要]\n{previous_summary}\n\n[后续对话]\n{history}"
        summary = self.llm.chat([{"role": "user", "content": SUMMARY_PROMPT.format(history=history)}]).content or ""
        self.compactions += 1
        new_head = [{"role": "system", "content": base_system + SUMMARY_MARKER + summary.strip()}] + head[1:]
        return new_head + [m for b in recent for m in b]


def _render(m: Message) -> str:
    if m.get("tool_calls"):
        calls = ", ".join(f"{c['function']['name']}({c['function']['arguments']})" for c in m["tool_calls"])
        return f"assistant 调用工具: {calls}"
    return f"{m['role']}: {m.get('content') or ''}"
