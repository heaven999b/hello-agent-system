"""第 21 课练习参考答案。先自己写，卡住了再看。"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Callable, Hashable, Sequence, TypeVar

T = TypeVar("T")

# =====================================================================
# 练习 (a)：cohen_kappa —— 扣除"碰巧一致"之后的一致性
# =====================================================================


def cohen_kappa(labels_a: Sequence[Hashable], labels_b: Sequence[Hashable]) -> float:
    """两个标注者（人和人，或人和 LLM 评委）对同一批样本的 Cohen's kappa。

        po = 实际一致的比例
        pe = Σ_c  P_A(c) × P_B(c)      —— 两人各自按自己的标签分布"随机乱标"时，期望有多少会碰巧一致
        kappa = (po - pe) / (1 - pe)

    边界情况：
      - 两组标签不等长、或为空 → ValueError；
      - 类别不全：某个类别只有一方用过，照样参与计算（另一方在该类别上的比例是 0）；
      - 完全一致 → 1.0；
      - pe == 1：两人都只用过同一个标签 —— 公式是 0/0。此时 po 必然也是 1，我们约定返回 1.0，
        但要知道这个 1.0 没有信息量：样本里根本没有另一类，换一批样本再测。
    """
    if len(labels_a) != len(labels_b):
        raise ValueError(f"两组标签长度不同：{len(labels_a)} vs {len(labels_b)}")
    n = len(labels_a)
    if n == 0:
        raise ValueError("标签为空，kappa 没有定义")
    agree = sum(a == b for a, b in zip(labels_a, labels_b))
    ca, cb = Counter(labels_a), Counter(labels_b)
    # 用整数算，避免 pe 因为浮点误差变成 0.9999999 而不等于 1
    chance = sum(ca[c] * cb[c] for c in ca.keys() | cb.keys())  # = pe × n²
    if chance == n * n:
        return 1.0
    return (n * agree - chance) / (n * n - chance)  # 分子分母同乘 n²


# =====================================================================
# 练习 (b)：stratified_sample —— 分层抽样
# =====================================================================


def stratified_sample(records: Sequence[T], key_fn: Callable[[T], Hashable], n: int, seed: int) -> list[T]:
    """按 key_fn 分层，恰好抽 n 条；每层至少 1 条；结果可复现。

    分配规则：
      1. 按比例：第 i 层分到 n × 层大小 / 总数，用最大余数法取整（先取整数部分，剩下的名额
         按小数部分从大到小发，小数部分相同则按层第一次出现的顺序）；
      2. 分到 0 条的层补成 1 条，名额从当前分得最多的层里扣（相同则扣先出现的层）。
    层内用 random.Random(seed) 无放回抽样；返回结果按它们在 records 中的原始顺序排列。
    """
    if n < 0 or n > len(records):
        raise ValueError(f"n={n} 超出范围 [0, {len(records)}]")
    strata: dict[Hashable, list[int]] = {}
    for i, r in enumerate(records):
        strata.setdefault(key_fn(r), []).append(i)  # dict 保持插入顺序 = 层第一次出现的顺序
    keys = list(strata)
    if n < len(keys):
        raise ValueError(f"n={n} 小于层数 {len(keys)}，无法保证每层至少 1 条")
    if n == 0:
        return []

    total = len(records)
    ideal = [n * len(strata[k]) / total for k in keys]
    quota = [math.floor(x) for x in ideal]
    order = sorted(range(len(keys)), key=lambda i: (-(ideal[i] - quota[i]), i))
    for i in order[: n - sum(quota)]:
        quota[i] += 1
    for i in range(len(keys)):
        if quota[i] == 0:
            donor = max(range(len(keys)), key=lambda j: (quota[j], -j))
            quota[donor] -= 1
            quota[i] = 1

    rng = random.Random(seed)
    chosen: list[int] = []
    for k, q in zip(keys, quota):
        chosen += rng.sample(strata[k], q)
    return [records[i] for i in sorted(chosen)]


# =====================================================================
# 练习 (c)：split_no_leak —— 按组划分，杜绝泄漏
# =====================================================================


def split_no_leak(
    records: Sequence[T],
    group_fn: Callable[[T], Hashable],
    ratios: dict[str, float],
    seed: int,
) -> dict[str, list[T]]:
    """把 records 划分成若干集合（如 train / dev / test），同一组的数据只能整组进入同一个集合。

    做法：把组打乱（random.Random(seed)），再按组的大小从大到小（大小相同保持打乱后的顺序），
    每次把一个组放进"离目标条数还差得最多"的集合（目标条数 = 比例 × 总条数；差距相同时按 ratios 的顺序）。
    先放大组、后放小组，小组像沙子一样填缝，各集合的大小更接近目标比例。
    """
    if not ratios:
        raise ValueError("ratios 不能为空")
    if any(v < 0 for v in ratios.values()):
        raise ValueError(f"比例不能为负：{ratios}")
    if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-6):
        raise ValueError(f"比例之和必须为 1，实际是 {sum(ratios.values())}")

    groups: dict[Hashable, list[int]] = {}
    for i, r in enumerate(records):
        groups.setdefault(group_fn(r), []).append(i)
    members = list(groups.values())
    random.Random(seed).shuffle(members)
    members.sort(key=len, reverse=True)  # sort 是稳定的：大小相同的组保持打乱后的顺序

    names = list(ratios)
    target = {s: ratios[s] * len(records) for s in names}
    assigned: dict[str, list[int]] = {s: [] for s in names}
    for g in members:
        best = max(names, key=lambda s: (target[s] - len(assigned[s]), -names.index(s)))
        assigned[best] += g
    return {s: [records[i] for i in sorted(assigned[s])] for s in names}
