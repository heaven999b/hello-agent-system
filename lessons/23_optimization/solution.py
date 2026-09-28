"""第 23 课练习参考答案。先自己做，再来对照。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable, Sequence


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "23_optimization__optkit"，避免和其他课的同名模块冲突
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


optkit = _load_sibling("optkit")
Example = optkit.Example
Demo = optkit.Demo


# =====================================================================
# (a) select_topk
# =====================================================================


def select_topk(history: Sequence[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    if k <= 0:
        return []
    # 指令 → (最高分, 第一次出现的下标)。dict 只读 history，不修改它
    best: dict[str, tuple[float, int]] = {}
    for i, (instruction, score) in enumerate(history):
        key = instruction.strip()
        if key not in best:
            best[key] = (score, i)
        elif score > best[key][0]:
            best[key] = (score, best[key][1])  # 分数取最高，位置保持"第一次出现"
    # 一次排序同时满足两个条件：分数降序（取负号），同分按第一次出现的位置升序
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return [(instruction, score) for instruction, (score, _) in ranked[:k]]


# =====================================================================
# (b) bootstrap_demos
# =====================================================================


async def bootstrap_demos(
    program: Callable[[str], Awaitable[str]],
    trainset: Sequence,
    metric: Callable[[object, str], float],
    max_demos: int,
    *,
    threshold: float = 1.0,
) -> list:
    demos: list = []
    if max_demos <= 0:
        return demos  # 一次都不调用
    for ex in trainset:
        try:
            output = await program(ex.input)  # 一条接一条：收满就停，后面的请求根本不发
        except Exception:  # noqa: BLE001  限流、超时……跳过这条，别让一条样本拖垮整个收集过程
            continue
        if float(metric(ex, output)) >= threshold:
            demos.append(Demo(input=ex.input, output=output))  # 用程序自己的输出（带推理过程），不是 ex.label
            if len(demos) >= max_demos:
                break  # 收集够了就停：后面的样本不再花钱
    return demos


# =====================================================================
# (c) pareto_front
# =====================================================================


def _dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    """a 支配 b：每一项都不差，且至少一项严格更好。两个完全相同的向量互不支配。"""
    return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def pareto_front(candidates: dict[str, Sequence[float]]) -> list[str]:
    names = list(candidates)
    vectors = [list(candidates[name]) for name in names]
    if vectors and any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError("所有候选的分数向量长度必须相同")
    front = []
    for i, name in enumerate(names):
        dominated = any(_dominates(vectors[j], vectors[i]) for j in range(len(names)) if j != i)
        if not dominated:
            front.append(name)  # 按插入顺序追加，结果确定
    return front
