"""第 03 课练习：亲手实现两种上下文裁剪策略。

运行测试：make lesson N=03    （或 .venv/bin/python -m pytest lessons/03_context_memory）

背景回顾（详见 README 第 2 节）：
    发给模型的 messages 是 OpenAI 格式的 dict 列表。一次工具调用由两部分组成：
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", ...}, {"id": "c2", ...}]}
        {"role": "tool", "tool_call_id": "c1", "content": "..."}
        {"role": "tool", "tool_call_id": "c2", "content": "..."}
    这 3 条消息必须"同生共死"：
      - 只留下 tool 消息、删掉前面的 assistant → 孤立的 tool 消息 → 很多 API 直接返回 400；
      - 只留下 assistant、删掉 tool 结果     → "有调用没结果"     → 同样 400。

你可以使用：
    agentkit.context.estimate_tokens(messages) -> int   估算一组消息的 token 数（逐条相加）
    下面已经提供的 has_tool_calls(message)

⚠️ 为了真正学会，请不要直接调用 agentkit.context.split_blocks / SlidingWindow，自己把"切块"写一遍。
"""

from __future__ import annotations

from agentkit.context import estimate_tokens  # noqa: F401  （实现时会用到）
from agentkit.types import Message

DEFAULT_PLACEHOLDER = "[旧的工具结果已清理以节省上下文；如仍需要，请重新调用该工具]"


def has_tool_calls(message: Message) -> bool:
    """这条消息是不是"发起了工具调用的 assistant 消息"。（已提供，可直接使用）"""
    return message.get("role") == "assistant" and bool(message.get("tool_calls"))


# ---------------------------------------------------------------- 任务 1


def trim_to_budget(messages: list[Message], max_tokens: int) -> list[Message]:
    """按"块"把对话历史截断到 max_tokens 以内（滑动窗口），绝不产生孤立的 tool 消息。

    规则（每一条都有对应的测试）：
    1. 开头连续的 system 消息永远保留（它们也占预算）。
    2. 其余消息切成不可分割的"块"：
         - 一条带 tool_calls 的 assistant 消息 + 紧跟其后的所有 tool 消息 = 一个块；
         - 其他每条消息（user / 普通 assistant / 中途出现的 system）各自单独成块。
    3. 从**最新**的块开始往回保留，直到下一个（更早的）块放不下为止。
       保留下来的必须是"连续的最近一段"：一旦某个块放不下就停，不能跳过它去保留更早的小块
       （否则对话会出现断层，模型看到的上下文是拼接出来的"假历史"）。
    4. 至少保留最后一个块 —— 哪怕它本身就超预算（宁可超一点，也不能把用户最新的问题删掉）。
    5. 输入里本来就孤立的 tool 消息（前面没有能对应的 assistant(tool_calls)，
       例如被别的代码截坏的历史）一律丢弃 —— 即使预算充足。
    6. 不要修改传入的 messages 列表或其中的 dict；返回一个新列表。
       （保留下来的消息可以直接复用原来的 dict 对象，不必深拷贝。）

    判断"放得下"：估算 token 数 <= max_tokens 即可（等于也算放得下）。

    提示（三步走）：
      第 1 步：用 while 循环取出开头连续的 system 消息作为 head；
      第 2 步：遍历剩余消息建 blocks（list[list[Message]]）。遇到 tool 消息时，
              如果 blocks[-1] 的第一条是 has_tool_calls 的 assistant，就 append 进去，否则丢弃；
      第 3 步：budget = max_tokens - estimate_tokens(head)，然后 reversed(blocks) 往回装。

    边界情况：空列表 → []；只有 system 消息 → 原样返回这些 system；没有 system 消息也要能工作。
    """
    raise NotImplementedError("TODO: 实现按块截断（保留 system、至少保留最后一个块、不产生孤立 tool 消息）")


# ---------------------------------------------------------------- 任务 2


def clear_old_tool_results(
    messages: list[Message],
    keep_last: int = 2,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> list[Message]:
    """工具结果清理：只保留最近 keep_last 条 tool 消息的原文，更早的 tool 消息内容替换为 placeholder。

    为什么：Agent 跑久了，上下文里大部分 token 是早就用过的工具结果（一次搜索可能返回几千字）。
    模型通常只需要最近几次的原始结果；更早的，留下"这里曾经调用过工具"的痕迹就够了，真要用可以再调一次。

    规则：
    1. 按 tool 消息的**条数**计数（一轮里并行调用了 2 个工具 = 2 条 tool 消息）。
    2. 被清理的消息只替换 content，其余字段（role、tool_call_id 以及任何其他键）原样保留；
       消息总条数和顺序完全不变 —— 这样绝不会破坏 assistant(tool_calls) 与 tool 结果的配对。
       （对比：直接把大的 tool 消息删掉，就会变成"有调用没结果"，API 返回 400。）
    3. 非 tool 消息一律不动。
    4. keep_last=0 表示清理全部 tool 结果；keep_last 大于 tool 消息总数表示什么都不清理。
    5. keep_last 为负数 → 抛 ValueError。
    6. 不要修改传入的列表和其中的 dict：需要替换的消息请生成新 dict（提示：{**m, "content": placeholder}）。

    提示：先找出所有 tool 消息的下标，算出"前面多少条需要清理"，再用一个列表推导式生成结果。
    """
    raise NotImplementedError("TODO: 实现旧工具结果清理（只换 content，不删消息，不修改输入）")
