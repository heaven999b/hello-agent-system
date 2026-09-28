"""第 06 课练习：编排里最常用的三块"胶水代码"。

运行测试：make lesson N=06    （或 .venv/bin/python -m pytest lessons/06_orchestration）

这三个函数都不调用真实模型 —— 模型被抽象成可注入的函数（Callable）。
这正是企业里写编排代码的正确姿势：把"不确定的 LLM"关在一个小接口后面，
其余逻辑全是普通、确定、可单元测试的代码。

async：模型调用要等好几秒，所以"代表模型的函数"是 async 函数（llm_route、run_with_gates 的每个步骤），
调用它们的 hybrid_route 和 run_with_gates 也要写成 `async def`，在里面 await。
vote_with_quorum 只对已经拿到的答案计票，是纯计算，保持普通函数；检查函数（gates）也是普通函数。
测试里直接 `await ex.hybrid_route(...)`（pytest-asyncio），你自己试的时候用 `asyncio.run(...)`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Sequence

# ---------------------------------------------------------------- 任务 1：混合路由


async def hybrid_route(
    text: str,
    rules: dict[str, list[str]],
    llm_route: Callable[[str], Awaitable[str]],
    default: str = "other",
) -> tuple[str, str]:
    """先用关键词规则分流，规则没命中才调用模型；返回 (类别, 来源)。

    这是一个 async 函数：llm_route 背后是一次模型调用（async 函数），要 `await llm_route(text)`；
    关键词匹配是纯计算，照常写。

    为什么这样设计：大部分请求（比如"退款""发票"）一眼就能分对，用规则 = 零成本、零延迟、结果可解释；
    只有规则拿不准的长尾请求才花钱问模型。模型可能出错、超时、胡说，所以要有兜底。

    参数：
        text:      用户请求
        rules:     {类别: [关键词, ...]}，dict 的顺序就是优先级
        llm_route: 调用模型做分类的 async 函数，输入 text，返回类别名（可能抛异常、可能返回乱七八糟的东西）
        default:   兜底类别

    返回 (category, source)，source 只能是：
        "rule"    —— 关键词规则命中
        "llm"     —— 规则没命中，模型给出了合法类别
        "default" —— 规则没命中，模型也没给出可用结果，降级到 default

    规则：
    1. 关键词匹配 = 子串匹配，且忽略大小写（"VPN" 能匹配关键词 "vpn"）。
    2. 按 rules 的顺序逐个类别检查，返回**第一个**命中的类别（一句话同时命中多个类别时，靠顺序定优先级）。
    3. 关键词先去掉首尾空白；空关键词（"" 或 "  "）必须忽略 ——
       否则 '' in text 永远为 True，所有请求都会被分到第一个类别（真实的配置事故）。
    4. 规则命中时**不能**调用 llm_route（省钱，也避免无意义的延迟）。
    5. llm_route 的返回值：去掉首尾空白后，忽略大小写地等于 rules 中的某个类别或 default，
       就返回该类别的**标准写法**（rules 里的 key 或 default 本身）和 "llm"。
       例如模型返回 " Billing\\n"，rules 里是 "billing" → ("billing", "llm")。
    6. llm_route 抛出任何异常（Exception）、返回未知类别、返回非字符串（如 None）→ (default, "default")。
    7. 取消不是"模型出错"：调用方取消了这次请求（用户断开、上游超时）时，await llm_route(...) 会抛出
       asyncio.CancelledError。它是 BaseException，必须原样向外传播，不能降级成 (default, "default") ——
       否则调用方以为请求已经停了，你却还在继续往下执行。

    提示：先把 text 转小写；遍历 rules.items()；用 try/except Exception 包住 await llm_route(text)
         （只写 except Exception，别写 except BaseException 或裸 except）。
    """
    raise NotImplementedError("TODO: 实现 规则优先 → 模型兜底 → default 降级 的混合路由")


# ---------------------------------------------------------------- 任务 2：带法定票数的投票


def vote_with_quorum(answers: Sequence[str | None], quorum: float) -> str | None:
    """多数投票，但只有"多数足够多"时才返回答案；否则返回 None（企业里 None = 转人工处理）。

    场景：同一个问题并行问模型 5 次（或问 5 个不同视角的评审），答案不一致时，
    与其硬选一个，不如承认"模型没把握"，把它交给人。
    这是普通函数（不是 async）：它只对已经拿到的答案计票，是纯计算。并行采样那一步用 agentkit.workflows.parallel。

    规则：
    1. quorum 是比例，必须满足 0 < quorum <= 1，否则抛 ValueError。
    2. 归一化后再计票：去掉首尾空白、忽略大小写（"Yes"、" yes " 算同一个答案）。
    3. None 或空白字符串 = 弃权票：**计入总票数**（它代表一次失败的采样，会拉低把握程度），但不能胜出。
    4. 得票最多的答案唯一，且 得票数 / 总票数 >= quorum 时，返回该答案**第一次出现时的写法（去掉首尾空白）**。
       例：["Yes", " yes", "NO"]，quorum=0.6 → 2/3 ≈ 0.67 >= 0.6 → 返回 "Yes"
    5. 以下情况返回 None：answers 为空；全部弃权；得票最多的答案不唯一（并列）；比例没达到 quorum。

    提示：用 count / total >= quorum 比较（恰好等于也算通过）。
         不要写成 count >= quorum * total —— 例如 0.56 * 25 在浮点数里是 14.000000000000002，
         25 票里恰好 14 票（正好 56%）会被误判为不够。
    """
    raise NotImplementedError("TODO: 实现带 quorum 的多数投票（归一化、弃权票、并列、边界）")


# ---------------------------------------------------------------- 任务 3：带检查点的流水线


@dataclass
class GateResult:
    """流水线的运行结果。（已提供，不需要修改）

    ok:               是否全部成功
    output:           成功时为最后一步的输出；失败时为 None
    failed_step:      失败的步骤名（成功时为 None）
    kind:             失败类型："step_error"（步骤抛异常）或 "gate_rejected"（输出没通过检查）
    reason:           人类可读的失败原因
    completed:        已成功完成（且通过检查）的步骤名，按执行顺序
    last_good_output: 最后一个"可信"的结果 —— 失败时为失败步骤的输入（即上一步通过检查的输出，
                      第一步就失败则为原始输入）；成功时等于 output。方便人工接手或断点重跑。
    """

    ok: bool
    output: str | None = None
    failed_step: str | None = None
    kind: str | None = None
    reason: str | None = None
    completed: list[str] = field(default_factory=list)
    last_good_output: str | None = None


Step = tuple[str, Callable[[str], Awaitable[str]]]  # 步骤函数是 async 的（背后是模型调用）
Gate = Callable[[str], bool]  # 检查函数是普通函数：用代码做的确定性检查


async def run_with_gates(
    steps: Sequence[Step],
    text: str,
    gates: Mapping[str, Gate] | None = None,
) -> GateResult:
    """提示链（prompt chaining）+ 检查点：依次执行步骤，某些步骤后做检查；失败时返回结构化信息，不抛异常。

    agentkit.workflows.chain 在检查失败时直接抛 ValueError —— 在线服务里这意味着一个 500 和一段丢失的上下文。
    企业里更想要的是："哪一步、因为什么、失败了；之前做完了哪些；最后一个可信的中间结果是什么"。

    这是一个 async 函数：每个步骤函数是 async 的，要 `await fn(current)`；检查函数是普通函数，直接调用。

    参数：
        steps: [(步骤名, async 函数), ...]，上一步的输出是下一步的输入
        text:  初始输入
        gates: {步骤名: 检查函数}，检查函数接收该步骤的输出，返回 True 表示通过。不是每一步都需要检查。

    规则：
    1. 配置错误要"大声失败"：在执行任何步骤之前检查，以下情况直接抛 ValueError ——
         - 步骤名有重复；
         - gates 里出现了不存在的步骤名（拼错一个名字 = 一道安全检查被悄悄跳过，比报错可怕得多）。
    2. 运行时错误要"结构化返回"，绝不向外抛异常：
         - 步骤抛异常 → kind="step_error"，reason 形如 "ValueError: 具体信息"（异常类型名: 异常消息）；
         - 检查函数返回假值 → kind="gate_rejected"，reason 里要包含步骤名；
         - 检查函数自己抛异常 → 也按"没通过"处理（fail closed：拿不准时宁可拦下），kind="gate_rejected"。
    3. 一旦失败立即停止，后面的步骤不能再执行。
    4. 全部成功 → GateResult(ok=True, output=最终结果, completed=全部步骤名, last_good_output=最终结果)。
    5. steps 为空 → 成功，output 就是原始 text。gates 为 None 等价于没有任何检查。
    6. "绝不向外抛异常"指的是 Exception。调用方取消时，正在 await 的步骤抛出的 asyncio.CancelledError
       必须原样向外传播，不能变成 kind="step_error" 的结构化结果，后面的步骤也不能再执行。

    提示：先做配置校验；然后维护 current（当前文本）和 completed（已完成步骤）两个变量往下走。
         步骤和检查函数都只用 except Exception 包住。
    """
    raise NotImplementedError("TODO: 实现带检查点、结构化失败信息的提示链")
