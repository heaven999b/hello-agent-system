"""影子运行（shadow mode）比较器（第 13 课 问题 2）。

同一个输入，同时交给 stable 和 candidate 两个版本跑，比较：
  - 状态：一个完成、一个失败？
  - 工具轨迹：调用了哪些工具、顺序和参数是否一致（Agent 的"行为"主要体现在这里）
  - 输出：文本相似度（这里用 difflib，便宜但只看字面；生产中常配合 LLM 评委判断语义是否等价）
  - 成本、步数

影子运行的铁律：**候选版本不能产生任何副作用**。写操作 / 高危工具必须换成"只记录、不执行"的替身
（shadow_tools），否则影子版本会真的再发一封邮件、再退一次款。
"""

from __future__ import annotations

import copy
import difflib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

from agentkit import RunResult, Tool
from agentkit.types import calls_in


def shadow_tools(tools: Iterable[Tool], log: list[dict] | None = None) -> list[Tool]:
    """返回一份"影子工具"：read 工具照常执行；write / dangerous 工具换成替身，只记录调用意图、不执行。

    替身会告诉模型"操作已受理"，让它能把流程走完 —— 我们要比较的是"它想做什么"，而不是真的去做。
    实现上是复制 Tool 对象、只替换 fn，所以名字、描述、参数 Schema 和原工具完全一致，模型看不出区别。
    """
    out = []
    for t in tools:
        if t.risk not in ("write", "dangerous"):
            out.append(t)
            continue
        twin = copy.copy(t)

        def recorder(_name=t.name, **kwargs):
            kwargs.pop("ctx", None)
            if log is not None:
                log.append({"tool": _name, "args": kwargs})
            return f"（影子模式）{_name} 已受理，未实际执行。"

        twin.fn = recorder
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


def _side(run: Callable[[str], RunResult], text: str) -> ShadowSide:
    t0 = time.time()
    try:
        r = run(text)
    except Exception as e:  # noqa: BLE001 —— 影子版本崩了只是一个"发现"，绝不能影响主流程
        return ShadowSide("error", "", [], 0, 0.0, time.time() - t0, f"{type(e).__name__}: {e}")
    return ShadowSide(r.status, r.output or "", trajectory(r), r.steps, r.cost_usd, time.time() - t0)


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


def run_shadow(
    inputs: Iterable[str],
    *,
    run_stable: Callable[[str], RunResult],
    run_candidate: Callable[[str], RunResult],
    parallel: bool = True,
) -> list[ShadowDiff]:
    """对每个输入同时跑两个版本并比较。parallel=True 时两个版本并发执行（总耗时 ≈ 较慢的那个）。"""
    diffs = []
    with ThreadPoolExecutor(max_workers=2 if parallel else 1) as pool:
        for text in inputs:
            fs = pool.submit(_side, run_stable, text)
            fc = pool.submit(_side, run_candidate, text)
            diffs.append(compare(text, fs.result(), fc.result()))
    return diffs


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
