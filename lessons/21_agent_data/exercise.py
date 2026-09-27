"""第 21 课练习：数据集构建的三块基石。

一共三题：
  (a) cohen_kappa        标注一致性：扣除"碰巧一致"之后，两个标注者到底有多一致
  (b) stratified_sample  分层抽样：从海量 trace 里抽 n 条，长尾路径一个都不漏，结果可复现
  (c) split_no_leak      按组划分：同一个用户 / 同一个模板生成的变体，不能同时出现在训练集和测试集

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=21
    # 或者：.venv/bin/python -m pytest lessons/21_agent_data -v

同目录的 data_kit.py 里有 Demo 用到的完整工具箱（kappa_2x2 是 (a) 的二分类特例，可以拿来对照）。
做完之后重跑 demo.py，第 2、4 节会显示"来自 exercise.py（你的实现）"。
"""

from __future__ import annotations

import math  # noqa: F401  （练习 b、c 会用到）
import random  # noqa: F401
from collections import Counter  # noqa: F401  （练习 a 会用到）
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

    例子：50 条样本，A 标了 25 个"是"，B 标了 30 个"是"，两人一致 35 条。
        po = 35/50 = 0.7；pe = 0.5×0.6 + 0.5×0.4 = 0.5；kappa = (0.7-0.5)/(1-0.5) = 0.4

    边界情况（测试都会覆盖）：
      - 两组标签不等长、或为空 → ValueError；
      - 标签可以是任意可哈希的值（"pass" / "fail" / 1 / 2 / 元组 ……），不止两类；
      - 类别不全：某个类别只有一方用过，照样参与计算（另一方在该类别上的比例是 0）；
      - 完全一致 → 1.0；比碰巧还差 → 负数；
      - pe == 1：两人都只用过同一个标签 —— 公式是 0/0。此时 po 必然也是 1，约定返回 1.0。
        （这个 1.0 没有信息量：样本里根本没有另一类，现实中应该换一批样本再测。）

    提示：
      - Counter 可以数每个标签出现几次；两方类别的并集是 ca.keys() | cb.keys()；
      - 小心浮点误差：pe 可能算成 0.9999999999 而不是 1。可以全部用整数算：
        分子分母同乘 n²，kappa = (n·一致数 - Σ ca[c]·cb[c]) / (n² - Σ ca[c]·cb[c])。
    """
    raise NotImplementedError("TODO: (a) 实现 cohen_kappa")


# =====================================================================
# 练习 (b)：stratified_sample —— 分层抽样
# =====================================================================


def stratified_sample(records: Sequence[T], key_fn: Callable[[T], Hashable], n: int, seed: int) -> list[T]:
    """按 key_fn 分层（比如按工具路径签名），恰好抽 n 条；每层至少 1 条；结果可复现。

    为什么要分层？线上 1 万条 trace 里，"查政策"占 9000 条，"价保申诉转人工"只有 3 条。
    随机抽 50 条，大概率一条长尾路径都抽不到 —— 而 bug 恰恰爱藏在长尾里。

    分配规则：
      1. 按比例：第 i 层分到 n × 层大小 / 总数，用最大余数法取整（先取整数部分，剩下的名额
         按小数部分从大到小发，小数部分相同则按层第一次出现的顺序）；
      2. 分到 0 条的层补成 1 条，名额从当前分得最多的层里扣（相同则扣先出现的层）。
    例：三层大小 60 / 30 / 10，n=10 → 6 / 3 / 1；三层大小 1 / 1 / 98，n=5 → 1 / 1 / 3。

    要求：
      - 可复现：只用 random.Random(seed) 产生随机数（不要用全局的 random.xxx，也不要用 hash()）；
      - 每层至少 1 条、每层不超过该层大小、总数恰好为 n、同一条记录不会被抽两次；
      - 返回结果按它们在 records 中的原始顺序排列；不修改 records；
      - records 里的元素可能是 dict（不可哈希），记下标而不是记元素本身；
      - n < 0、n > len(records)、或 n 小于层数（没法每层 1 条）→ ValueError；records 为空且 n=0 → []。

    提示：先用一个 dict 按 key 收集下标（dict 保持插入顺序 = 层第一次出现的顺序），再算配额，最后层内 rng.sample。
    """
    raise NotImplementedError("TODO: (b) 实现 stratified_sample")


# =====================================================================
# 练习 (c)：split_no_leak —— 按组划分，杜绝泄漏
# =====================================================================


def split_no_leak(
    records: Sequence[T],
    group_fn: Callable[[T], Hashable],
    ratios: dict[str, float],
    seed: int,
) -> dict[str, list[T]]:
    """把 records 划分成若干集合（如 {"train": 0.6, "dev": 0.2, "test": 0.2}），
    同一组（group_fn 返回值相同）的数据只能整组进入同一个集合。

    为什么？同一个种子问题合成出来的 5 个变体，3 个进了训练集（或 prompt 的 few-shot 示例）、
    2 个进了测试集 —— 测试就成了开卷考试，分数虚高，上线后原形毕露。同一个用户的多条对话、
    同一个会话的多轮、同一个模板批量生成的数据，都要按组划分。

    要求：
      - 不泄漏：任何一组只出现在一个集合里；
      - 不丢不重：每条记录恰好出现在一个集合里；
      - 可复现：同样的 seed 得到同样的结果（只用 random.Random(seed)）；
      - 大小接近比例：各集合的条数接近 比例 × 总条数（组有大有小，不可能精确，误差在一两个组的大小以内）；
      - 返回的 dict 包含 ratios 里的每个名字（比例为 0 的集合是空列表），每个集合内保持 records 的原始顺序；
      - ratios 为空、有负数、或加起来不等于 1（允许 1e-6 的误差）→ ValueError。

    推荐做法（贪心）：把组打乱，再按组的大小从大到小排（sort 是稳定的），然后每次把一个组放进
    "离目标条数还差得最多"的集合（差距相同时按 ratios 的顺序）。
    想一想：为什么先放大组、后放小组，结果更接近目标比例？
    """
    raise NotImplementedError("TODO: (c) 实现 split_no_leak")
