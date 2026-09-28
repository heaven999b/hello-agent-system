"""第 23 课：优化工具箱 optkit —— 提示词优化、测试时计算与显著性检验。

零外部依赖：只用标准库和 agentkit。每一块都对应讲义里的一节：

    Program / ProgramTask     最小的"LM 程序"：可优化的指令 + 示例 + 固定的输出格式 → 一次模型调用
    MeteredLLM                给 LLM 记账：调用次数、token、成本（每个优化器到底花了多少钱）
    AgentTask                 同一套优化器接到 agentkit.evals.run_eval 上，用来优化 Agent 的 system prompt
    bootstrap_demos           DSPy BootstrapFewShot 的核心：用当前程序跑训练集，收集"评分通过"的输出当示例
    bootstrap_fewshot         再按验证集表现，从示例池里挑一组最好的组合
    select_topk / opro_optimize   OPRO：LLM 当优化器，看着"指令 → 分数"的历史提出新指令
    pareto_front / gepa_optimize  GEPA 式：读失败轨迹 + 文字反馈 → 反思式改写；用帕累托前沿挑父代
    self_consistency / best_of_n  测试时计算：多数投票；采样 N 个，用验证器挑最好的
    paired_bootstrap          配对 bootstrap：提升是真的，还是 20 条样本上的噪声？

设计原则：优化器只和两个接口打交道 —— "给一条指令，在一批样本上跑出 (输出, 分数, 反馈)"
（await Task.run）和"一个会说话的 LLM"（await llm.chat）。换成 Agent、换成别的任务，优化器本身不用改。

并发：要调模型的函数都是 async 的。一次评估里的多条样本、一次测试时计算里的 N 次采样，
都在同一个事件循环里并发发出（agentkit.workflows.parallel，有并发上限），不用线程。
纯计算的部分（select_topk、pareto_front、自一致性投票、配对 bootstrap……）保持普通函数。
"""

from __future__ import annotations

import math
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Protocol, Sequence

from pydantic import BaseModel, Field

from agentkit.llm import LLM
from agentkit.pricing import estimate_cost
from agentkit.types import LLMResponse, Message, Usage
from agentkit.workflows import complete_json, majority_vote, parallel

Log = Callable[[str], None]


def _silent(_msg: str) -> None:
    pass


# =====================================================================
# 数据与记账
# =====================================================================


@dataclass(frozen=True)
class Example:
    """一条带标准答案的样本。

    note：人工标注时顺手写下的理由（"为什么是这个类别"）。它是 GEPA 式反馈的好材料 ——
          标量分数只告诉优化器"错了"，备注告诉它"错在哪、规则是什么"。
    tag ：分析用的标签（比如属于哪条易错规则）。只用于打印报表，优化器看不到。
    """

    input: str
    label: str
    id: str = ""
    note: str = ""
    tag: str = ""


@dataclass(frozen=True)
class Demo:
    """一条少样本示例（few-shot demonstration）：一个输入，和一段"标准的"输出。"""

    input: str
    output: str


Metric = Callable[[Example, str], float]  # (样本, 模型原始输出) -> 0~1 的分数
Feedback = Callable[[Example, str], str]  # (样本, 模型原始输出) -> 给优化器看的文字反馈


class MeteredLLM:
    """记账装饰器：数一数每个优化器调用了多少次模型、花了多少钱、等了多久。

    为什么要单独记账：优化是"花钱买质量"。只报"准确率 +15 个点"不报"花了 300 次调用"，
    就没法和"换个更大的模型""多采样几次"这些方案公平比较（第 14 课的成本视角）。

    为什么不加锁：评估时很多协程同时调用 chat，但它们都在**同一个事件循环**里。协程只在 await 处让出控制权，
    下面 `await self.llm.chat(...)` 返回之后的几行"读-改-写"中间没有 await，执行时不会被别的协程插队，
    所以计数是准确的。只有"读 → await 别的东西 → 再写回"才需要 asyncio.Lock；
    多线程才需要 threading.Lock（线程可能在任意两条字节码之间被切走）。test_exercise.py 里有一个测试让 40 个
    协程同时调用它，计数一个不差。in_flight / max_in_flight 是"同时在途的调用数"，用来证明并发真的发生了。
    """

    def __init__(self, llm: LLM, name: str | None = None):
        self.llm = llm
        self.model = llm.model
        self.name = name or llm.model
        self.calls = 0
        self.usage = Usage()
        self.cost_usd = 0.0
        self.latency_s = 0.0  # 所有调用的耗时之和（不是墙钟时间：并发时两者不同）
        self.in_flight = 0
        self.max_in_flight = 0

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        t0 = time.perf_counter()
        try:
            resp = await self.llm.chat(messages, tools, **kwargs)
        finally:
            self.in_flight -= 1
        # 以下是同步的读-改-写：没有 await 夹在中间，不会被别的协程打断，不需要锁
        self.latency_s += time.perf_counter() - t0
        self.calls += 1
        self.usage = self.usage + resp.usage
        self.cost_usd += estimate_cost(resp.usage, resp.model or self.model)
        return resp

    def snapshot(self) -> tuple[int, float]:
        """(调用次数, 成本)。两次快照相减 = 这一段花了多少。"""
        return self.calls, self.cost_usd


# =====================================================================
# 被优化的对象：Program 与 Task
# =====================================================================


class Program:
    """最小的"LM 程序"：instruction（可优化）+ demos（可优化）+ output_format（固定的格式契约）。

    为什么把 output_format 从 instruction 里拆出来：优化器会整段改写 instruction，
    如果格式要求也在里面，改着改着就可能把"最后一行写 类别：xxx"改没了，解析全挂。
    DSPy 的做法类似：签名（signature）里的输入输出字段是固定的，优化器只改指令和示例。
    示例按"用户/助手"多轮对话的形式放进消息里（DSPy 的 ChatAdapter 也是这么做的）。
    """

    def __init__(
        self,
        llm: LLM,
        instruction: str,
        demos: Sequence[Demo] = (),
        *,
        output_format: str = "",
        input_prefix: str = "",
        **chat_kwargs,
    ):
        self.llm = llm
        self.instruction = instruction.strip()
        self.demos = list(demos)
        self.output_format = output_format.strip()
        self.input_prefix = input_prefix
        self.chat_kwargs = chat_kwargs

    def messages(self, x: str) -> list[Message]:
        system = self.instruction + (f"\n\n{self.output_format}" if self.output_format else "")
        msgs: list[Message] = [{"role": "system", "content": system}]
        for d in self.demos:
            msgs.append({"role": "user", "content": f"{self.input_prefix}{d.input}"})
            msgs.append({"role": "assistant", "content": d.output})
        msgs.append({"role": "user", "content": f"{self.input_prefix}{x}"})
        return msgs

    async def __call__(self, x: str) -> str:
        """调用一次模型，返回原始输出。是 async 的：`out = await program(x)`。"""
        return (await self.llm.chat(self.messages(x), **self.chat_kwargs)).content or ""

    def with_(self, *, instruction: str | None = None, demos: Sequence[Demo] | None = None, llm: LLM | None = None) -> "Program":
        return Program(
            llm or self.llm,
            self.instruction if instruction is None else instruction,
            self.demos if demos is None else demos,
            output_format=self.output_format,
            input_prefix=self.input_prefix,
            **self.chat_kwargs,
        )


@dataclass
class Record:
    """一次运行的完整记录：优化器能看到的全部"轨迹"。"""

    input: str
    output: str  # 模型的原始输出（含理由）；对 Agent 来说是最终回答 + 工具调用序列
    score: float
    feedback: str = ""


class Task(Protocol):
    """优化器眼中的"被优化系统"：给一条指令，在一批样本上跑，返回每条的记录（async）。"""

    async def run(self, instruction: str, examples: Sequence) -> list[Record]: ...


async def _run_all(fns: Sequence[Callable[[], Awaitable]], max_concurrency: int) -> list:
    """执行一批"返回协程的函数"，按输入顺序返回结果。max_concurrency <= 1 时一个接一个 await。"""
    if max_concurrency <= 1 or len(fns) <= 1:
        return [await f() for f in fns]
    return await parallel(fns, max_concurrency=max_concurrency)


@dataclass
class ProgramTask:
    """把 Program + 评分函数包装成 Task。max_concurrency > 1 时，一批样本的模型调用并发发出
    （同时在途最多 max_concurrency 个；共享网关上请 ≤ 2~4）。结果按样本顺序返回。"""

    program: Program
    metric: Metric
    feedback: Feedback | None = None
    max_concurrency: int = 1
    errors: int = 0  # 重试之后仍然失败的调用数：不为 0 时，分数里混进了"网关故障"，要在报告里说明

    async def run(self, instruction: str, examples: Sequence[Example], demos: Sequence[Demo] | None = None) -> list[Record]:
        prog = self.program.with_(instruction=instruction, demos=demos)

        async def one(ex: Example) -> Record:
            try:
                out = await prog(ex.input)
            except Exception as e:  # noqa: BLE001  重试之后仍然失败：记 0 分，不让一条样本拖垮整轮优化
                self.errors += 1  # 同一个事件循环里、中间没有 await：不需要锁
                out = f"[调用失败] {type(e).__name__}: {e}"
            score = float(self.metric(ex, out))
            fb = self.feedback(ex, out) if self.feedback else ""
            return Record(ex.input, out, score, fb)

        return await _run_all([lambda ex=ex: one(ex) for ex in examples], self.max_concurrency)


@dataclass
class AgentTask:
    """把 agentkit 的 Agent + 第 11 课的 run_eval 包装成 Task：同一套优化器可以直接优化 Agent 的 system prompt。

    make_agent(instruction) 每次返回一个新 Agent；cases 是 EvalCase 列表；graders 默认用 rule_grader。
    反馈 = 没通过的 Check 的 detail（例如"没有调用 verify_identity"）—— 这正是 GEPA 想要的文字反馈。
    concurrency：同时在跑的用例数（交给 run_eval）。
    """

    make_agent: Callable[[str], object]
    graders: Sequence[Callable] = ()
    concurrency: int = 4

    async def run(self, instruction: str, examples: Sequence) -> list[Record]:
        from agentkit.evals import rule_grader, run_eval

        report = await run_eval(
            lambda: self.make_agent(instruction), examples, list(self.graders) or [rule_grader], concurrency=self.concurrency
        )
        records = []
        for case, r in zip(examples, report.results):
            failed = [f"{c.name}: {c.detail}" for c in r.checks if not c.passed]
            fb = "通过" if r.passed else "未通过 → " + "；".join(failed)
            out = f"{r.output}\n[工具调用] {r.tools}"
            records.append(Record(case.input, out, 1.0 if r.passed else 0.0, fb))
        return records


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# =====================================================================
# 1. BootstrapFewShot：从成功轨迹里收集示例
# =====================================================================


async def bootstrap_demos(
    program: Callable[[str], Awaitable[str]],
    trainset: Sequence[Example],
    metric: Metric,
    max_demos: int,
    *,
    threshold: float = 1.0,
) -> list[Demo]:
    """按训练集顺序运行 program（async：`await program(x)`），把"评分 ≥ threshold"的 (输入, 程序输出)
    收集为示例，最多 max_demos 条。

    三个细节：
    - 示例的输出用**程序自己的输出**（含推理过程），而不是标准答案 —— 这是 bootstrap 的价值所在：
      标注数据里通常只有答案，没有"怎么想的"，程序跑通的轨迹把中间步骤也带上了；
    - 收集够了就停，不再浪费调用 —— 所以这里**故意一条接一条**地 await，不并发：
      并发发出去的请求收不回来，"够了就停"就省不下钱（想要快，见 bootstrap_fewshot 的做法）；
    - 某条样本调用失败（异常）就跳过，继续下一条。
    """
    demos: list[Demo] = []
    if max_demos <= 0:
        return demos
    for ex in trainset:
        try:
            out = await program(ex.input)
        except Exception:  # noqa: BLE001
            continue
        if float(metric(ex, out)) >= threshold:
            demos.append(Demo(ex.input, out))
            if len(demos) >= max_demos:
                break
    return demos


@dataclass
class OptResult:
    instruction: str
    demos: list[Demo]
    dev_scores: list[float]
    log: list[dict] = field(default_factory=list)

    @property
    def dev(self) -> float:
        return mean(self.dev_scores)


async def bootstrap_fewshot(
    task: ProgramTask,
    trainset: Sequence[Example],
    devset: Sequence[Example],
    *,
    max_demos: int = 4,
    num_candidates: int = 3,
    seed: int = 0,
    log: Log = _silent,
) -> OptResult:
    """BootstrapFewShot + 随机搜索（对应 DSPy 的 BootstrapFewShotWithRandomSearch 的思路）：

    1. 用当前程序（老师）跑一遍训练集，收集所有评分通过的输出，得到示例池；
    2. 候选 0 = 池子里按顺序的前 max_demos 条（朴素 BootstrapFewShot）；
       其余候选 = 用固定种子打乱后取 max_demos 条；
    3. 每个候选在 dev 上评估，取 dev 分数最高的（同分取先出现的）。

    为什么步骤 1 不直接调用 bootstrap_demos：真实模型要并发跑训练集才不慢。
    这里先并发拿到全部输出，再按训练集顺序过滤 —— 结果和顺序调用 bootstrap_demos 完全一样
    （代价：训练集每条都要调用一次，不能"够了就停"；这里要的是整个示例池，本来就要全跑）。
    """
    instruction = task.program.instruction
    records = await task.run(instruction, trainset, demos=[])
    pool = [Demo(ex.input, r.output) for ex, r in zip(trainset, records) if r.score >= 1.0]
    log(f"示例池：训练集 {len(trainset)} 条里有 {len(pool)} 条评分通过，可以当示例")
    if not pool:
        return OptResult(instruction, [], [], [])

    rng = random.Random(seed)
    candidates: list[list[Demo]] = [pool[:max_demos]]
    seen = {tuple(pool[:max_demos])}
    for _ in range(num_candidates * 5):  # 池子太小时随机子集会重复，多试几次
        if len(candidates) >= num_candidates:
            break
        shuffled = pool[:]
        rng.shuffle(shuffled)
        subset = shuffled[:max_demos]
        if tuple(subset) not in seen:
            seen.add(tuple(subset))
            candidates.append(subset)

    best: OptResult | None = None
    history = []
    for i, demos in enumerate(candidates):
        scores = [r.score for r in await task.run(instruction, devset, demos=demos)]
        history.append({"candidate": i, "demos": demos, "dev": mean(scores)})
        log(f"候选 {i}：{len(demos)} 条示例 → dev {mean(scores):.0%}")
        if best is None or mean(scores) > best.dev:
            best = OptResult(instruction, demos, scores)
    assert best is not None
    best.log = history
    return best


# =====================================================================
# 2. OPRO：LLM 当优化器
# =====================================================================


def select_topk(history: Sequence[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    """从 "指令 → 分数" 的历史里挑出前 k 条（去重）。

    - 按分数从高到低；同分时先出现的排前面（稳定、可复现）；
    - 指令去掉首尾空白后相同就算重复：只保留它最高的那次分数，位置按它第一次出现算。
    """
    best: dict[str, tuple[float, int]] = {}
    for i, (ins, score) in enumerate(history):
        key = ins.strip()
        if key not in best:
            best[key] = (score, i)
        elif score > best[key][0]:
            best[key] = (score, best[key][1])
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return [(ins, s) for ins, (s, _) in ranked[: max(k, 0)]]


OPRO_HEADER = "你是一个提示词优化器。"


class Proposals(BaseModel):
    instructions: list[str] = Field(description="新的候选指令，每条都是一段完整的系统指令")


def opro_meta_prompt(task_description: str, exemplars: Sequence[str], top: Sequence[tuple[str, float]], n: int) -> str:
    """OPRO 的元提示词（meta-prompt）= 任务说明 + 几条任务样例 + 历史上最好的若干条指令及分数。

    和论文一样，历史按分数**从低到高**排列：最好的指令离"请写新指令"这句话最近。
    注意优化器只看到**总分**，看不到具体哪条错了 —— 这是 OPRO 和 GEPA 最大的区别。
    """
    hist = "\n\n".join(f"<指令>\n{ins}\n</指令>\n得分：{score * 100:.0f}" for ins, score in sorted(top, key=lambda t: t[1]))
    ex = "\n".join(exemplars)
    return (
        f"{OPRO_HEADER}目标：为下面的任务写一段系统指令，让模型在评估集上的准确率尽可能高。\n\n"
        f"## 任务说明\n{task_description}\n\n"
        f"## 任务样例（带标准答案，仅帮助你理解任务）\n{ex}\n\n"
        f"## 历史指令与得分（满分 100，按得分从低到高排列）\n{hist}\n\n"
        f"请写出 {n} 条新的指令：要和上面所有指令都不同，并争取得到比它们更高的分数。"
        f"只写指令本身，不要写输出格式要求（格式由系统统一附加）。"
    )


async def opro_optimize(
    task: Task,
    optimizer_llm: LLM,
    devset: Sequence,
    seed_instructions: Sequence[str],
    *,
    task_description: str,
    exemplars: Sequence[str] = (),
    n_exemplars: int = 3,
    rounds: int = 2,
    per_round: int = 4,
    keep_top: int = 5,
    seed: int = 0,
    seed_scores: dict[str, list[float]] | None = None,
    log: Log = _silent,
) -> OptResult:
    """OPRO（Yang et al., ICLR 2024）的精简版：

        评估种子指令 → 循环 rounds 轮：
            取历史前 keep_top 条 + 随机抽几条任务样例 → 元提示词 → 优化器一次提出 per_round 条新指令
            → 每条在 dev 上评估 → 写回历史
        → 返回 dev 最高的指令

    和论文的差异（为了省调用）：论文每步采样 8 条、每条单独一次调用、跑上百步；
    这里每轮只调用一次优化器、让它一次写 per_round 条。
    seed_scores：已经评估过的种子指令的 dev 分数（比如基线），传进来可以省一轮评估。
    """
    rng = random.Random(seed)
    history: list[tuple[str, float]] = []
    per_case: dict[str, list[float]] = {}
    log_rows: list[dict] = []

    async def score(ins: str, round_no: int) -> None:
        key = ins.strip()
        if key in per_case:  # 同一条指令不重复花钱
            return
        if seed_scores and key in seed_scores:
            s = seed_scores[key]
        else:
            s = [r.score for r in await task.run(key, devset)]
        per_case[key] = s
        history.append((key, mean(s)))
        log_rows.append({"round": round_no, "instruction": key, "dev": mean(s)})
        log(f"[第 {round_no} 轮] dev {mean(s):.0%} ← {_novel_snippet(key, seed_instructions, 64)}")

    # 候选指令一条接一条地评估：每次评估内部已经并发（task 的并发上限），再叠一层并发就会超出网关配额
    for ins in seed_instructions:
        await score(ins, 0)
    for r in range(1, rounds + 1):
        top = select_topk(history, keep_top)
        shown = rng.sample(list(exemplars), min(n_exemplars, len(exemplars))) if exemplars else []
        prompt = opro_meta_prompt(task_description, shown, top, per_round)
        try:
            proposals = (await complete_json(optimizer_llm, prompt, Proposals)).instructions
        except Exception as e:  # noqa: BLE001  优化器输出坏了：这一轮作废，继续
            log(f"[第 {r} 轮] 优化器输出无法解析，跳过：{e}")
            continue
        for ins in [p for p in proposals if p.strip()][:per_round]:
            await score(ins, r)

    best_ins, _ = select_topk(history, 1)[0]
    return OptResult(best_ins, [], per_case[best_ins], log_rows)


# =====================================================================
# 3. GEPA 式：反思式变异 + 帕累托选择
# =====================================================================


def pareto_front(candidates: dict[str, Sequence[float]]) -> list[str]:
    """返回不被任何其他候选支配的候选名（按输入顺序）。

    A 支配 B ⇔ A 在每个子任务上都 ≥ B，并且至少一个子任务上 > B。
    分数向量完全相同的两个候选互不支配，都留在前沿上。
    """
    names = list(candidates)
    vecs = [list(candidates[n]) for n in names]
    if vecs and any(len(v) != len(vecs[0]) for v in vecs):
        raise ValueError("所有候选的分数向量长度必须相同")

    def dominates(a: list[float], b: list[float]) -> bool:
        return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))

    return [n for i, n in enumerate(names) if not any(dominates(vecs[j], vecs[i]) for j in range(len(names)) if j != i)]


def gepa_select_parent(pool_scores: Sequence[Sequence[float]], rng: random.Random) -> tuple[int, list[int], dict[int, int]]:
    """GEPA 论文里的父代选择：

    1. 对每个 dev 样本（论文里叫 instance / task），找出得分最高的候选（可能并列多个）；
    2. 至少在一个样本上"最好"的候选进入候选池，再去掉被支配的 → 帕累托前沿；
    3. 按"在多少个样本上是最好的"加权随机抽一个当父代。

    为什么不直接选平均分最高的：平均分最高的那条指令可能在某类样本上很差，而另一条指令恰好擅长这类样本。
    只盯着第一名，搜索容易卡在局部最优；保留"各有所长"的候选，反思时才有更多样的起点。
    """
    n_items = len(pool_scores[0]) if pool_scores else 0
    freq: dict[int, int] = {}
    for j in range(n_items):
        top = max(s[j] for s in pool_scores)
        for i, s in enumerate(pool_scores):
            if s[j] == top:
                freq[i] = freq.get(i, 0) + 1
    if not freq:  # 没有 dev 样本：只能选第一个
        return 0, [0], {0: 1}
    front = [int(n) for n in pareto_front({str(i): pool_scores[i] for i in sorted(freq)})]
    weights = [freq[i] for i in front]
    parent = rng.choices(front, weights=weights, k=1)[0]
    return parent, front, {i: freq[i] for i in front}


REFLECT_HEADER = "你在帮我改进一个 LM 程序的系统指令。"


class Reflection(BaseModel):
    diagnosis: str = Field(description="失败的共同原因，以及你归纳出的可推广规则")
    instruction: str = Field(description="改进后的完整系统指令")


def reflection_prompt(instruction: str, records: Sequence[Record], task_description: str = "") -> str:
    """GEPA 的反思输入 = 当前指令 + 这一小批样本的完整轨迹（输入、模型输出含理由、分数、文字反馈）。"""
    blocks = []
    for i, r in enumerate(records, 1):
        mark = "✅" if r.score >= 1.0 else "❌"
        blocks.append(f"### 样本 {i}（{mark} 得分 {r.score:.2f}）\n输入：{r.input}\n模型输出：{r.output}\n评分反馈：{r.feedback}")
    desc = f"## 任务说明\n{task_description}\n\n" if task_description else ""
    return (
        f"{REFLECT_HEADER}\n\n{desc}## 当前指令\n<指令>\n{instruction}\n</指令>\n\n"
        f"## 这条指令在一小批训练样本上的运行记录\n" + "\n\n".join(blocks) + "\n\n"
        "请你：\n1. 对照失败样本的输出和评分反馈，诊断当前指令缺了什么、哪里有误导；\n"
        "2. 把诊断归纳成**可推广的规则**（写类别层面的规则，不要把某条样本原文抄进指令，避免过拟合）；\n"
        "3. 保留当前指令里有效的内容，写出改进后的完整指令。不要写输出格式要求（格式由系统统一附加）。"
    )


@dataclass
class Candidate:
    instruction: str
    dev_scores: list[float]
    parent: int | None
    iteration: int

    @property
    def dev(self) -> float:
        return mean(self.dev_scores)


async def gepa_optimize(
    task: Task,
    reflect_llm: LLM,
    trainset: Sequence,
    devset: Sequence,
    seed_instruction: str,
    *,
    iterations: int = 4,
    minibatch_size: int = 4,
    task_description: str = "",
    seed: int = 0,
    seed_dev_scores: list[float] | None = None,
    log: Log = _silent,
) -> tuple[OptResult, list[Candidate]]:
    """GEPA（Agrawal et al., ICLR 2026）核心循环的精简版：

        候选池 = [种子指令]，在 dev（论文里的 D_pareto）上逐条打分
        循环 iterations 次：
            1. 用帕累托前沿按频率抽一个父代（gepa_select_parent）
            2. 从训练集（论文里的 D_feedback）取一小批，跑父代，拿到轨迹 + 文字反馈
               —— 如果这一批全对，没东西可学，跳过（论文实现里也有这个选项）
            3. 把轨迹和反馈交给反思模型，让它写出改进后的指令（反思式变异）
            4. 新指令在同一小批上**严格变好**才接受，再到 dev 上完整评估、加入候选池
        → 返回 dev 平均分最高的候选

    省略了论文里的"系统感知合并"（System Aware Merge，把不同谱系里各模块的最佳版本拼起来）：
    本课的程序只有一个模块，合并没有意义。
    """
    rng = random.Random(seed)
    dev0 = seed_dev_scores if seed_dev_scores is not None else [r.score for r in await task.run(seed_instruction, devset)]
    pool = [Candidate(seed_instruction.strip(), list(dev0), None, 0)]
    order = list(range(len(trainset)))
    rng.shuffle(order)
    cursor = 0
    rows: list[dict] = []

    for it in range(1, iterations + 1):
        parent_idx, front, freq = gepa_select_parent([c.dev_scores for c in pool], rng)
        parent = pool[parent_idx]
        batch = [trainset[order[(cursor + k) % len(order)]] for k in range(minibatch_size)]
        cursor += minibatch_size
        recs = await task.run(parent.instruction, batch)
        row = {"iteration": it, "parent": parent_idx, "front": front, "freq": freq, "parent_batch": sum(r.score for r in recs)}
        if all(r.score >= 1.0 for r in recs):
            row["status"] = "skip"
            log(f"[迭代 {it}] 父代 #{parent_idx} 在这批 {len(batch)} 条上全对 → 没有可学的失败，跳过")
            rows.append(row)
            continue
        try:
            refl = await complete_json(reflect_llm, reflection_prompt(parent.instruction, recs, task_description), Reflection)
        except Exception as e:  # noqa: BLE001
            row["status"] = "reflect_error"
            log(f"[迭代 {it}] 反思模型输出无法解析，跳过：{e}")
            rows.append(row)
            continue
        child_recs = await task.run(refl.instruction, batch)
        child_batch = sum(r.score for r in child_recs)
        row.update(diagnosis=refl.diagnosis, child_batch=child_batch, instruction=refl.instruction.strip())
        fails = [r for r in recs if r.score < 1.0]
        log(f"[迭代 {it}] 父代 #{parent_idx}（前沿 {front}）在小批上 {row['parent_batch']:.0f}/{len(batch)}，失败 {len(fails)} 条")
        log(f"          反思诊断：{_one_line(refl.diagnosis, 110)}")
        if child_batch <= row["parent_batch"]:
            row["status"] = "reject"
            log(f"          新指令在小批上 {child_batch:.0f}/{len(batch)}，没有严格变好 → 拒绝（省下一次 dev 评估）")
            rows.append(row)
            continue
        dev_scores = [r.score for r in await task.run(refl.instruction, devset)]
        pool.append(Candidate(refl.instruction.strip(), dev_scores, parent_idx, it))
        row.update(status="accept", child=len(pool) - 1, dev=mean(dev_scores))
        log(f"          新指令在小批上 {child_batch:.0f}/{len(batch)} → 接受为候选 #{len(pool) - 1}，dev {mean(dev_scores):.0%}")
        rows.append(row)

    best_idx = max(range(len(pool)), key=lambda i: (pool[i].dev, -i))
    best = pool[best_idx]
    return OptResult(best.instruction, [], best.dev_scores, rows), pool


def _novel_snippet(text: str, references: Sequence[str], n: int) -> str:
    """日志里展示一条指令"新在哪"：取第一行不在参考指令里的内容（找不到就取第一行）。"""
    ref_lines = {line.strip() for r in references for line in r.splitlines()}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    novel = [line for line in lines if line not in ref_lines]
    shown = novel[0] if novel else (lines[0] if lines else "")
    prefix = "…" if novel and lines and novel[0] != lines[0] else ""
    return prefix + _one_line(shown, n)


def _one_line(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# =====================================================================
# 4. 测试时计算：自一致性与 best-of-N
# =====================================================================


async def sample_n(fn: Callable[[], Awaitable[str]], n: int, max_concurrency: int | None = None) -> list[str]:
    """把同一个请求采样 n 次（依赖模型本身的随机性；温度为 0 时 n 次可能一模一样）。

    fn 每次调用返回一个新协程（例如 lambda: program(x)）。n 次采样**同时发出**（默认全部并发；
    max_concurrency 限制同时在途的个数）：用户等待的时间约等于"最慢的那一次"，而不是 n 次之和 ——
    但调用次数（成本）仍然是 n 倍。并发上限小于 n 时，延迟约为 ceil(n / 上限) 次调用。
    """
    return await _run_all([fn] * n, max_concurrency or n)


def self_consistency(answers: Sequence[str]) -> tuple[str, float]:
    """自一致性（Wang et al., ICLR 2023）：多次采样，取出现最多的**最终答案**（并列取先出现的）。

    返回 (多数答案, 一致率)。一致率低 = 模型自己都拿不准，是很好的"该转人工 / 该升级"的信号。
    注意投票的是解析后的答案（类别名），不是整段输出 —— 推理过程各不相同，答案才可能一致。
    """
    winner = majority_vote(list(answers))
    return winner, Counter(a.strip() for a in answers)[winner] / len(answers)


def best_of_n(candidates: Sequence[str], verifier: Callable[[str], float]) -> tuple[str, list[float]]:
    """best-of-N：N 个候选各自让验证器打分，取最高分（并列取先出现的）。

    效果的上限由验证器决定：验证器只会检查格式，就只能挑出"格式对的"；
    验证器是单元测试 / 可执行检查，才能真正挑出"答案对的"。
    """
    scores = [float(verifier(c)) for c in candidates]
    best = max(range(len(candidates)), key=lambda i: (scores[i], -i))
    return candidates[best], scores


# =====================================================================
# 5. 显著性：配对 bootstrap（思路见第 22 课）
# =====================================================================


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], *, n_boot: int = 10000, seed: int = 0, alpha: float = 0.05
) -> dict:
    """比较同一批样本上的两个系统：b 相对 a 的平均提升，及其 bootstrap 置信区间。

    "配对"的意思：每次重采样抽的是**样本编号**，a 和 b 用同一组编号 —— 两个系统在同一条样本上的
    表现是相关的（难题两边都容易错），配对比较能把"题目难度"这部分方差消掉，比各自算置信区间灵敏得多。

    返回 {diff, lo, hi, p_le_0, wins, losses}：
      p_le_0 = 重采样中"提升 ≤ 0"的比例（单侧）；wins / losses = b 对 a 错 / a 对 b 错 的样本数。
    """
    if len(a) != len(b) or not a:
        raise ValueError("a 和 b 必须是同一批样本上的逐条分数，长度相同且非空")
    n = len(a)
    d = [y - x for x, y in zip(a, b)]
    rng = random.Random(seed)
    boots = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo = boots[int(math.floor(alpha / 2 * n_boot))]
    hi = boots[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]
    return {
        "diff": sum(d) / n,
        "lo": lo,
        "hi": hi,
        "p_le_0": sum(1 for x in boots if x <= 0) / n_boot,
        "wins": sum(1 for x in d if x > 0),
        "losses": sum(1 for x in d if x < 0),
    }


def verbatim_overlap(text: str, examples: Iterable[Example], n: int = 10) -> list[Example]:
    """找出"输入里有连续 n 个字符原样出现在 text 里"的样本。

    用途：检查优化出来的指令有没有"背题" —— 把训练样本原文抄进指令（过拟合），
    或者更糟，抄进了 dev / test 的原文（泄漏：分数不再可信）。n 取 10 个字符左右，
    太短会把"浏览器插件"这类普通词组误报成抄袭。
    """
    squeeze = lambda s: "".join(s.split())  # noqa: E731  忽略空白差异
    t = squeeze(text)
    hits = []
    for ex in examples:
        src = squeeze(ex.input)
        if any(src[i : i + n] in t for i in range(0, max(1, len(src) - n + 1))) and len(src) >= n:
            hits.append(ex)
    return hits


def per_tag_accuracy(examples: Iterable[Example], scores: Sequence[float]) -> dict[str, tuple[int, int]]:
    """按 tag 统计 (答对数, 总数)，用来看"错在哪一类"。"""
    out: dict[str, list[int]] = {}
    for ex, s in zip(examples, scores):
        k = ex.tag or "-"
        out.setdefault(k, [0, 0])
        out[k][0] += int(s >= 1.0)
        out[k][1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}
