"""第 03 课练习 —— 参考答案。

先自己写 exercise.py，卡住超过 15 分钟再来看。接口与 exercise.py 完全一致。
"""

from __future__ import annotations

from agentkit.context import estimate_tokens
from agentkit.types import Message

DEFAULT_PLACEHOLDER = "[旧的工具结果已清理以节省上下文；如仍需要，请重新调用该工具]"


def has_tool_calls(message: Message) -> bool:
    """这条消息是不是"发起了工具调用的 assistant 消息"。（已提供，可直接使用）"""
    return message.get("role") == "assistant" and bool(message.get("tool_calls"))


# ---------------------------------------------------------------- 任务 1


def trim_to_budget(messages: list[Message], max_tokens: int) -> list[Message]:
    # 第 1 步：拆出开头连续的 system 消息 —— 它们永远保留
    i = 0
    head: list[Message] = []
    while i < len(messages) and messages[i].get("role") == "system":
        head.append(messages[i])
        i += 1

    # 第 2 步：把剩下的消息切成不可分割的"块"
    blocks: list[list[Message]] = []
    for m in messages[i:]:
        if m.get("role") == "tool":
            if blocks and has_tool_calls(blocks[-1][0]):
                blocks[-1].append(m)  # 跟着发起调用的 assistant 走
            # 否则：它前面没有能对应的 assistant(tool_calls)，是孤立消息，丢弃
            continue
        blocks.append([m])

    # 第 3 步：从最新的块往回装，装不下就停（保证保留的是"连续的最近一段"）
    budget = max_tokens - estimate_tokens(head)
    kept: list[list[Message]] = []
    for block in reversed(blocks):
        cost = estimate_tokens(block)
        if kept and cost > budget:  # kept 为空时无条件保留：至少留最后一个块
            break
        kept.append(block)
        budget -= cost
    kept.reverse()
    return list(head) + [m for block in kept for m in block]


# ---------------------------------------------------------------- 任务 2


def clear_old_tool_results(
    messages: list[Message],
    keep_last: int = 2,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> list[Message]:
    if keep_last < 0:
        raise ValueError(f"keep_last 不能是负数：{keep_last}")
    tool_positions = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    n_clear = max(0, len(tool_positions) - keep_last)
    to_clear = set(tool_positions[:n_clear])
    # {**m, "content": ...} 生成一个新 dict：保留 tool_call_id 等所有字段，只换 content，且不碰原对象
    return [{**m, "content": placeholder} if i in to_clear else m for i, m in enumerate(messages)]
