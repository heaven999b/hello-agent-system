"""影子运行（shadow mode）（第 16 课 问题 2）。

同一个输入，交给 stable 和 candidate 两个版本跑，比较：
  - 状态：一个完成、一个失败？
  - 工具轨迹：调用了哪些工具、顺序和参数是否一致（Agent 的"行为"主要体现在这里）
  - 输出：文本相似度（这里用 difflib，便宜但只看字面；生产中常配合 LLM 评委判断语义是否等价）
  - 成本、步数

两种用法：
  run_shadow     离线回放：一批录下来的输入，两个版本各跑一遍再比较（发布评审前做）；
  ShadowRunner   在线影子：用户的请求照常由 stable 处理并**立刻返回**；candidate 在后台跑同一个输入，
                 结果只进分析，不返回用户。三条纪律（Demo 场景 2 用在途数和计时证明）：
                   1. 不给主路径加延迟：主路径从不等影子，影子满了就丢弃（dropped），而不是排队等；
                   2. 有界：同时在跑的影子不超过 max_concurrency，每个影子最多跑 timeout_s 秒；
                   3. 可取消：停机时 aclose() 取消所有还在跑的影子（它们的模型调用收到 CancelledError）。

影子运行的铁律：**候选版本不能产生任何副作用**。写操作 / 高危工具必须换成"只记录、不执行"的替身
（shadow_tools），否则影子版本会真的再发一封邮件、再退一次款。
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from agentkit import RunResult, Tool, wait_for
from agentkit.types import calls_in


def shadow_tools(tools: Iterable[Tool], log: list[dict] | None = None) -> list[Tool]:
    """返回一份"影子工具"：read 工具照常执行；write / dangerous 工具换成替身，只记录调用意图、不执行。

    替身会告诉模型"操作已受理"，让它能把流程走完 —— 我们要比较的是"它想做什么"，而不是真的去做。
    实现上是复制 Tool 对象、只替换 fn，所以名字、描述、参数 Schema 和原工具完全一致，模型看不出区别。
    替身是 async 函数（只往列表里追加一条记录）：Tool.is_async 按当前的 fn 判断，所以换了 fn 就会被当成 async 工具执行；
    同时去掉进程隔离（原工具可能是 isolation="process" 的同步函数）。
    """
    out = []
    for t in tools:
        if t.risk not in ("write", "dangerous"):
            out.append(t)
            continue
        twin = copy.copy(t)

        async def recorder(_name=t.name, **kwargs):
            kwargs.pop("ctx", None)
            if log is not None:
                log.append({"tool": _name, "args": kwargs})
            return f"（影子模式）{_name} 已受理，未实际执行。"

        twin.fn = recorder
        twin.isolation = None
        out.append(twin)
    return out


def trajectory(result: RunResult) -> list[tuple[str, str]]:
    """按顺序取出一次运行里的工具调用：[(工具名, 规范化后的参数 JSON), ...]。"""
    out = []
    for m in result.messages:
        if m.get("role") != "assistant":
            continue
        for c in calls_in(m):
            out.append((c.name, json.dumps(c.parsed_args(), sort_keys=True, ensure_ascii=False)))
    return out


@dataclass
class ShadowSide:
    status: str
    output: str
    tools: list[tuple[str, str]]
    steps: int
    cost_usd: float
    latency_s: float
    error: str | None = None

    @property
    def tool_names(self) -> list[str]:
        return [n for n, _ in self.tools]


@dataclass
class ShadowDiff:
    input: str
    stable: ShadowSide
    candidate: ShadowSide
    similarity: float  # 输出文本相似度 0~1
    verdict: str  # equivalent / output_changed / args_changed / tools_changed / status_changed / candidate_error
    notes: list[str] = field(default_factory=list)


Runner = Callable[[str], Awaitable[RunResult]]


def side_from(result: RunResult, latency_s: float) -> ShadowSide:
    return ShadowSide(result.status, result.output or "", trajectory(result), result.steps, result.cost_usd, latency_s)


async def _side(run: Runner, text: str) -> ShadowSide:
    t0 = time.perf_counter()
    try:
        r = await run(text)
    except Exception as e:  # noqa: BLE001 —— 影子版本崩了只是一个"发现"，绝不能影响主流程
        return ShadowSide("error", "", [], 0, 0.0, time.perf_counter() - t0, f"{type(e).__name__}: {e}")
    return side_from(r, time.perf_counter() - t0)


def compare(text: str, stable: ShadowSide, candidate: ShadowSide, *, similarity_threshold: float = 0.6) -> ShadowDiff:
    sim = difflib.SequenceMatcher(None, stable.output, candidate.output).ratio()
    notes = []
    if candidate.error:
        verdict = "candidate_error"
        notes.append(candidate.error)
    elif stable.status != candidate.status:
        verdict = "status_changed"
        notes.append(f"{stable.status} → {candidate.status}")
    elif stable.tool_names != candidate.tool_names:
        verdict = "tools_changed"
        notes.append(f"{stable.tool_names} → {candidate.tool_names}")
    elif stable.tools != candidate.tools:
        verdict = "args_changed"
        notes += [f"{a[0]}: {a[1]} → {b[1]}" for a, b in zip(stable.tools, candidate.tools) if a != b]
    elif sim < similarity_threshold:
        verdict = "output_changed"
    else:
        verdict = "equivalent"
    return ShadowDiff(text, stable, candidate, round(sim, 3), verdict, notes)


async def run_shadow(inputs: Iterable[str], *, run_stable: Runner, run_candidate: Runner,
                     max_concurrency: int = 4) -> list[ShadowDiff]:
    """离线回放：每个输入两个版本都跑一遍并比较。两个版本并发执行，不同输入之间最多 max_concurrency 个同时进行。
    结果按输入的顺序返回。"""
    sem = asyncio.Semaphore(max_concurrency)

    async def one(text: str) -> ShadowDiff:
        async with sem:
            stable, candidate = await asyncio.gather(_side(run_stable, text), _side(run_candidate, text))
            return compare(text, stable, candidate)

    return list(await asyncio.gather(*(one(t) for t in inputs)))


class ShadowRunner:
    """在线影子：主路径照常返回，candidate 在后台跑，有并发上限、有超时、停机时取消。

        shadow = ShadowRunner(run_candidate, max_concurrency=4, timeout_s=10)
        result = await shadow.serve(text, run_stable)      # 只等 stable；影子在后台
        ...
        await shadow.aclose()                               # 停机：取消还在跑的影子
        shadow.diffs / shadow.stats                         # 比较结果与计数

    为什么不用"两个版本都跑完再返回"？那样主路径的延迟 = max(stable, candidate)：候选版本慢一倍，
    所有用户就一起慢一倍；候选版本卡住，用户也跟着卡住。影子的收益（发现行为差异）不值得用户买单。
    """

    def __init__(self, run_candidate: Runner, *, max_concurrency: int = 4, timeout_s: float = 30.0):
        self.run_candidate = run_candidate
        self.max_concurrency = max_concurrency
        self.timeout_s = timeout_s
        self.diffs: list[ShadowDiff] = []
        self.stats = {"started": 0, "compared": 0, "dropped": 0, "timeout": 0, "cancelled": 0, "max_in_flight": 0}
        self._tasks: set[asyncio.Task] = set()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def serve(self, text: str, run_stable: Runner) -> RunResult:
        """主路径：影子先"尝试"启动（满了就丢弃，从不等待），然后只 await stable。返回 stable 的结果。"""
        candidate = self._launch(text)
        t0 = time.perf_counter()
        result = await run_stable(text)
        if candidate is not None:  # 比较要等影子跑完：挂一个回调，主路径不等
            stable = side_from(result, time.perf_counter() - t0)
            candidate.add_done_callback(lambda t: self._collect(text, stable, t))
        return result

    def _launch(self, text: str) -> asyncio.Task | None:
        if len(self._tasks) >= self.max_concurrency:
            self.stats["dropped"] += 1  # 影子是"尽力而为"的：满了就不做，不能让主路径排队
            return None
        task = asyncio.create_task(self._shadow(text), name="shadow")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.stats["started"] += 1
        self.stats["max_in_flight"] = max(self.stats["max_in_flight"], len(self._tasks))
        return task

    async def _shadow(self, text: str) -> ShadowSide:
        t0 = time.perf_counter()
        try:
            r = await wait_for(self.run_candidate(text), self.timeout_s)  # 取消安全的超时：到点真的停下来
        except asyncio.TimeoutError:
            self.stats["timeout"] += 1
            return ShadowSide("timeout", "", [], 0, 0.0, time.perf_counter() - t0, f"影子超过 {self.timeout_s}s，已取消")
        except Exception as e:  # noqa: BLE001 —— 影子出错只是一个"发现"
            return ShadowSide("error", "", [], 0, 0.0, time.perf_counter() - t0, f"{type(e).__name__}: {e}")
        return side_from(r, time.perf_counter() - t0)

    def _collect(self, text: str, stable: ShadowSide, task: asyncio.Task) -> None:
        if task.cancelled():
            self.stats["cancelled"] += 1
            return
        self.diffs.append(compare(text, stable, task.result()))
        self.stats["compared"] += 1

    async def drain(self, timeout: float | None = None) -> None:
        """等还在跑的影子跑完（最多 timeout 秒，到点的留给 aclose 取消）。"""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def aclose(self) -> int:
        """停机：取消所有还在跑的影子，等它们真正结束。返回被取消的个数。"""
        tasks = set(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks)
        return sum(t.cancelled() for t in tasks)


def summarize(diffs: list[ShadowDiff]) -> dict:
    """汇总成可以贴进发布评审单的数字。"""
    n = len(diffs) or 1
    verdicts: dict[str, int] = {}
    for d in diffs:
        verdicts[d.verdict] = verdicts.get(d.verdict, 0) + 1
    return {
        "total": len(diffs),
        "verdicts": verdicts,
        "equivalent_rate": verdicts.get("equivalent", 0) / n,
        "avg_similarity": sum(d.similarity for d in diffs) / n,
        "cost_ratio": (sum(d.candidate.cost_usd for d in diffs) / max(sum(d.stable.cost_usd for d in diffs), 1e-12)),
        "avg_steps": (sum(d.stable.steps for d in diffs) / n, sum(d.candidate.steps for d in diffs) / n),
    }
