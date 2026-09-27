"""第 22 课练习：评估统计的三块基石。

    (a) wilson_interval(successes, n, z)           给通过率加"误差棒"
    (b) paired_bootstrap_diff(a, b, n_boot, seed)  B 比 A 好多少、有多确定
    (c) debiased_pairwise(judge, x, y)             交换顺序，去掉 LLM 评委的位置偏差

运行测试：make lesson N=22    或    .venv/bin/python -m pytest lessons/22_eval_methodology -v
写完后重新跑 demo，输出开头会显示"实现来自 exercise.py（你的实现）"。

只允许用标准库（math / random / statistics）。参考答案是 evalstats.py 里的同名函数 —— 先自己写，再对照。
"""

from __future__ import annotations

import math  # noqa: F401  —— (a) 会用到 math.sqrt
import random  # noqa: F401  —— (b) 会用到 random.Random(seed)
from typing import Callable, NamedTuple, Sequence

Z95 = 1.959963984540054  # 95% 双侧置信区间对应的 z 值


class Interval(NamedTuple):
    """点估计 + 置信区间。可以解包：est, low, high = interval。"""

    estimate: float
    low: float
    high: float


# =====================================================================
# (a) Wilson 区间
# =====================================================================


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """返回通过率的 Wilson 得分区间 (low, high)。

    公式（p = successes / n）：
        center = (p + z²/(2n)) / (1 + z²/n)
        half   = z · √(p(1-p)/n + z²/(4n²)) / (1 + z²/n)
        区间   = [center - half, center + half]

    边界（测试会逐条检查）：
    - n == 0：没有任何信息，返回 (0.0, 1.0)；
    - 全部成功（successes == n）：上界必须**恰好**是 1.0；全部失败：下界必须**恰好**是 0.0
      （浮点运算可能算出 0.9999999999，要显式处理）；
    - 结果要夹在 [0, 1] 之内；
    - successes < 0、n < 0、successes > n、z <= 0：抛 ValueError。

    提示：先想清楚为什么教科书上的 p ± z·√(p(1-p)/n) 在 10/10 时会给出 [1.0, 1.0] —— 那就是 Wilson 要修的问题。
    例：wilson_interval(45, 50) ≈ (0.786, 0.957)
    """
    raise NotImplementedError("TODO: (a) 实现 wilson_interval")


# =====================================================================
# (b) 配对 bootstrap
# =====================================================================


def paired_bootstrap_diff(
    a: Sequence[float], b: Sequence[float], n_boot: int = 2000, seed: int = 0, alpha: float = 0.05
) -> Interval:
    """B 相对 A 的平均提升 mean(b) - mean(a)，以及它的 (1-alpha) 百分位 bootstrap 置信区间。

    a[i]、b[i] 是**同一个任务**上两个版本的得分：通过 1 / 失败 0（也可以是 True/False），
    或者多次运行的平均分（如 2/3）。

    步骤：
    1. 逐任务求差 d[i] = b[i] - a[i]；estimate = mean(d)；
    2. 重复 n_boot 次：从 d 里**有放回**地抽 len(d) 个（抽中任务 i 就同时带上它的 a[i] 和 b[i]——
       这就是"配对"），算这次抽样的均值；
    3. 把 n_boot 个均值排序，取 alpha/2 和 1-alpha/2 分位数作为 low / high（用线性插值或最近秩都可以）。

    要求：
    - 返回 Interval(estimate, low, high)，且 low <= estimate <= high 通常成立；
    - len(a) != len(b) 或为空：抛 ValueError（不能配对的数据不能用配对检验）；
    - n_boot < 1 或 alpha 不在 (0, 1)：抛 ValueError；
    - **可复现**：用 random.Random(seed) 建自己的随机数生成器，不要用全局 random ——
      同样的输入和 seed 必须得到完全相同的结果，不受程序里其他 random 调用的影响。
    """
    raise NotImplementedError("TODO: (b) 实现 paired_bootstrap_diff")


# =====================================================================
# (c) 去位置偏差的成对评判
# =====================================================================

# judge(first, second) -> "A"（第一个更好）/ "B"（第二个更好）/ "tie"
Judge = Callable[[str, str], str]


def debiased_pairwise(judge: Judge, x: str, y: str) -> str:
    """把 x、y 按两种顺序各交给评委判一次，返回 'x' / 'y' / 'tie'。

    - 第一次调用 judge(x, y)：返回 "A" 表示 x 赢，"B" 表示 y 赢；
    - 第二次调用 judge(y, x)：返回 "A" 表示 y 赢，"B" 表示 x 赢；
    - 两次都判 x 赢 → 'x'；两次都判 y 赢 → 'y'；其余情况（结论矛盾、任意一次是平局）→ 'tie'。
      这是 Zheng et al. 2023 的"保守做法"：只有换了位置还坚持同一个结论，才算真的赢。

    要求：
    - judge 必须被调用恰好两次，顺序是先 (x, y) 再 (y, x)；
    - 评委的返回值先 strip()，"a"/"b" 不区分大小写，"tie" 也不区分大小写；
      其他任何返回值（如 "C"、""、"x"）抛 ValueError —— 评委输出格式不对要大声报错，不能默默当平局。

    想一想：一个"永远选第一个"的评委，经过这个函数之后会给出什么结论？
    """
    raise NotImplementedError("TODO: (c) 实现 debiased_pairwise")
