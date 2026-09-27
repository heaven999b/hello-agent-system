"""编排模式：Workflow（工作流）vs Agent（智能体）。

Anthropic《Building Effective Agents》里最重要的一句话：
**能用简单方案解决的，就不要上 Agent。** 复杂度只在它明显提升效果时才值得。

- Workflow：流程由**代码**预先写死，LLM 只负责其中的步骤。可预测、好测试、便宜。
- Agent：流程由**模型**在运行时自己决定。灵活，但不可预测、难测试、贵。

本文件实现 5 种经典 Workflow 模式 + 多 Agent 的"Agent 即工具"模式：

    1. chain                 提示链：A → 检查 → B → C（步骤固定）
    2. route                 路由：先分类，再交给专门的处理器（客服分流）
    3. parallel              并行：分片并行 / 多次投票
    4. orchestrator_workers  编排者-执行者：LLM 动态拆解子任务，并行执行后汇总
    5. evaluator_optimizer   评估-优化：生成 → 评审 → 按意见修改，循环直到合格
    6. agent_as_tool         多 Agent：把一个 Agent 包装成另一个 Agent 的工具（主管-专家模式）
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Callable, Literal, Sequence, TypeVar

from pydantic import BaseModel, Field, ValidationError, create_model

from .llm import LLM
from .tools import Tool, ToolContext
from .types import Message

M = TypeVar("M", bound=BaseModel)


# ---------------------------------------------------------------- 基础积木

def complete(llm: LLM, prompt: str, system: str | None = None) -> str:
    """单次调用：最简单的 LLM 积木。"""
    messages: list[Message] = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    return llm.chat(messages).content or ""


def extract_json(text: str) -> str:
    """从模型输出里抠出 JSON（模型经常会包一层 ```json ... ``` 或加一句废话）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("输出中没有找到 JSON")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1]


def complete_json(llm: LLM, prompt: str, model_cls: type[M], system: str | None = None, max_repairs: int = 2) -> M:
    """结构化输出 + 自动修复：校验失败就把错误信息发回给模型，让它改，最多 max_repairs 次。

    这是企业里最常用的模式之一：下游代码需要的是**可靠的数据结构**，不是一段自由文本。
    （如果模型/网关支持原生 JSON Schema 结构化输出，优先用原生能力，这个修复循环作为兜底。）
    """
    schema = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
    messages: list[Message] = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": f"{prompt}\n\n只输出一个符合以下 JSON Schema 的 JSON，不要输出任何其他文字：\n{schema}"})
    last_error = ""
    for _ in range(max_repairs + 1):
        text = llm.chat(messages).content or ""
        try:
            return model_cls.model_validate_json(extract_json(text))
        except (ValidationError, ValueError) as e:
            last_error = str(e)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": f"你的输出没有通过校验：\n{last_error}\n请只输出修正后的 JSON。"})
    raise ValueError(f"结构化输出在 {max_repairs} 次修复后仍然失败：{last_error}")


# ---------------------------------------------------------------- 1. 提示链

def chain(steps: Sequence[Callable[[str], str]], text: str, gate: Callable[[int, str], bool] | None = None) -> str:
    """依次执行每一步，上一步输出是下一步输入。gate(i, output) 返回 False 时提前终止并抛错。"""
    for i, step in enumerate(steps):
        text = step(text)
        if gate is not None and not gate(i, text):
            raise ValueError(f"第 {i + 1} 步的输出没有通过检查：{text[:200]}")
    return text


# ---------------------------------------------------------------- 2. 路由

def route(llm: LLM, text: str, routes: dict[str, str]) -> str:
    """让模型从 routes（名称 → 说明）里选一个类别。返回值保证是 routes 的某个 key。"""
    names = tuple(routes)
    Choice = create_model("RouteChoice", route=(Literal[names], ...), reason=(str, ""))  # type: ignore[valid-type]
    options = "\n".join(f"- {k}: {v}" for k, v in routes.items())
    result = complete_json(llm, f"请把下面的请求分到最合适的类别。\n\n可选类别：\n{options}\n\n请求：{text}", Choice)
    return result.route  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 3. 并行

def parallel(fns: Sequence[Callable[[], str]], max_workers: int = 4) -> list[str]:
    """并行执行多个独立任务，按输入顺序返回结果。LLM 调用是 I/O 密集型，线程池就够用。"""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda f: f(), fns))


def majority_vote(answers: Sequence[str]) -> str:
    """投票：取出现次数最多的答案（并列时取最先出现的）。只做首尾空白归一化。"""
    if not answers:
        raise ValueError("没有可投票的答案")
    counts: dict[str, int] = {}
    for a in answers:
        key = a.strip()
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------- 4. 编排者-执行者

class Plan(BaseModel):
    subtasks: list[str] = Field(description="拆解出的相互独立的子任务，每个子任务是一句完整的指令")


def orchestrator_workers(llm: LLM, task: str, worker: Callable[[str], str], max_subtasks: int = 5) -> str:
    """编排者动态拆解任务 → 执行者并行处理 → 编排者汇总。
    和 parallel 的区别：子任务不是写死的，而是模型根据具体输入决定的。"""
    plan = complete_json(llm, f"把下面的任务拆解成最多 {max_subtasks} 个可以并行完成的独立子任务。\n\n任务：{task}", Plan)
    subtasks = plan.subtasks[:max_subtasks]
    results = parallel([lambda s=s: worker(s) for s in subtasks])
    parts = "\n\n".join(f"### 子任务 {i + 1}：{s}\n{r}" for i, (s, r) in enumerate(zip(subtasks, results)))
    return complete(llm, f"原始任务：{task}\n\n以下是各子任务的结果，请整合成一份完整、连贯、不重复的最终答复：\n\n{parts}")


# ---------------------------------------------------------------- 5. 评估-优化

class Review(BaseModel):
    passed: bool = Field(description="是否已经达到要求")
    feedback: str = Field(description="如果没通过，具体说明要怎么改")


def evaluator_optimizer(
    generate: Callable[[str, str | None], str],
    evaluate: Callable[[str], Review],
    task: str,
    max_rounds: int = 3,
) -> tuple[str, list[Review]]:
    """generate(task, 上一轮反馈) 生成候选；evaluate(候选) 给出评审；直到通过或达到轮数上限。
    返回 (最终候选, 每轮评审记录)。"""
    feedback: str | None = None
    reviews: list[Review] = []
    candidate = ""
    for _ in range(max_rounds):
        candidate = generate(task, feedback)
        review = evaluate(candidate)
        reviews.append(review)
        if review.passed:
            break
        feedback = review.feedback
    return candidate, reviews


# ---------------------------------------------------------------- 6. 多 Agent：Agent 即工具

def agent_as_tool(agent, name: str, description: str) -> Tool:
    """把一个专家 Agent 包装成工具，交给主管 Agent 调用（supervisor 模式）。

    好处：专家有自己独立的 system prompt、工具集和上下文窗口，主管的上下文不会被专家的中间步骤污染。
    注意：身份信息（ctx）要透传给子 Agent，否则子 Agent 会"失去身份"，或被迫让模型传身份（危险）。
    """

    def delegate(
        task: Annotated[str, Field(description="交给该专家的完整任务描述，要包含所有必要的上下文")],
        ctx: ToolContext,
    ) -> str:
        meta = {"tenant_id": ctx.tenant_id, "user_id": ctx.user_id, "roles": list(ctx.roles), "parent_run": ctx.run_id}
        result = agent.run(task, metadata=meta)
        if not result.ok:
            return f"专家 {name} 未能完成任务（{result.status}）：{result.output}"
        return result.output or ""

    return Tool(delegate, name=name, description=description)
