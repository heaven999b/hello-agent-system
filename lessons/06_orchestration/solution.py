"""第 06 课练习 —— 参考答案。

先自己写 exercise.py，卡住超过 15 分钟再来看。接口与 exercise.py 完全一致：
hybrid_route、run_with_gates 是 async 函数（要 await 模型调用 / async 步骤），vote_with_quorum 是普通函数（纯计算）。
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
    # 1) 关键词规则：便宜、确定、可解释。按 rules 的顺序检查，第一个命中的类别胜出
    lowered = text.lower()
    for category, keywords in rules.items():
        for kw in keywords:
            kw = kw.strip().lower()
            if kw and kw in lowered:  # 空关键词必须跳过：'' in 任何字符串 都是 True
                return category, "rule"

    # 2) 规则没命中才花钱问模型；模型的任何异常都不能让路由崩掉。
    #    只捕获 Exception：CancelledError（调用方取消了请求）是 BaseException，要原样传出去，不能降级成 default
    try:
        answer = await llm_route(text)
    except Exception:  # noqa: BLE001
        return default, "default"

    # 3) 模型的输出不可信：只接受已知类别（忽略首尾空白和大小写）
    if isinstance(answer, str):
        known = {c.lower(): c for c in rules}
        known.setdefault(default.lower(), default)
        hit = known.get(answer.strip().lower())
        if hit is not None:
            return hit, "llm"
    return default, "default"


# ---------------------------------------------------------------- 任务 2：带法定票数的投票


def vote_with_quorum(answers: Sequence[str | None], quorum: float) -> str | None:
    if not 0 < quorum <= 1:
        raise ValueError(f"quorum 必须在 (0, 1] 之间：{quorum}")
    total = len(answers)
    if total == 0:
        return None

    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for a in answers:
        if not isinstance(a, str) or not a.strip():
            continue  # 弃权票：计入总数，但不能胜出
        key = a.strip().lower()
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, a.strip())
    if not counts:
        return None

    top = max(counts.values())
    winners = [k for k, v in counts.items() if v == top]
    if len(winners) > 1:  # 并列第一 = 没有共识
        return None
    if top / total >= quorum:  # 用除法比较，避免 quorum * total 的浮点误差
        return first_seen[winners[0]]
    return None


# ---------------------------------------------------------------- 任务 3：带检查点的流水线


@dataclass
class GateResult:
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
    gates = dict(gates or {})

    # 配置错误：在执行任何步骤之前大声失败
    names = [name for name, _ in steps]
    if len(set(names)) != len(names):
        raise ValueError(f"步骤名重复：{names}")
    unknown = sorted(set(gates) - set(names))
    if unknown:
        raise ValueError(f"gates 引用了不存在的步骤：{unknown}（拼写错误会让检查被悄悄跳过）")

    # 运行时错误：结构化返回，不抛异常（只针对 Exception；取消照常向外传播，后面的步骤不会再执行）
    completed: list[str] = []
    current = text
    for name, fn in steps:
        try:
            output = await fn(current)
        except Exception as e:  # noqa: BLE001
            return GateResult(False, None, name, "step_error", f"{type(e).__name__}: {e}", completed, current)

        gate = gates.get(name)
        if gate is not None:
            try:
                passed = bool(gate(output))
                reason = f"步骤 {name!r} 的输出没有通过检查"
            except Exception as e:  # noqa: BLE001  检查本身出错 → 按不通过处理（fail closed）
                passed = False
                reason = f"步骤 {name!r} 的检查函数出错，按不通过处理：{type(e).__name__}: {e}"
            if not passed:
                return GateResult(False, None, name, "gate_rejected", reason, completed, current)

        completed.append(name)
        current = output

    return GateResult(True, current, None, None, None, completed, current)
