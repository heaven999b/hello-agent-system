"""第 22 课：评估统计与评委工具 —— 零外部依赖，只用 Python 标准库。

回答四个问题：
1. 这个通过率有多准？            → wilson_interval / bootstrap_ci（置信区间）
2. B 真的比 A 好吗？              → paired_bootstrap_diff / mcnemar（配对检验）
3. 要多少个任务才够？            → n_for_margin / min_sample_size / min_sample_size_paired（样本量）
4. LLM 评委靠得住吗？            → debiased_pairwise（交换顺序去位置偏差）/ judge_agreement（与人工的一致率、kappa）
另附 stratified_sample（分层抽样），用于"小而准"的评估子集。

为什么不用 numpy / scipy？这些公式都只有几行，自己写一遍才知道每个数字从哪来；
而且评估脚本常常要跑在 CI 里，少一个依赖就少一个坑。
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Callable, Hashable, NamedTuple, Sequence, TypeVar

T = TypeVar("T")

Z95 = NormalDist().inv_cdf(0.975)  # ≈ 1.95996：95% 双侧置信区间对应的 z 值


class Interval(NamedTuple):
    """点估计 + 置信区间。是 NamedTuple，所以既能 `est, lo, hi = ...` 解包，也能 `.low` 取值。"""

    estimate: float
    low: float
    high: float

    def excludes(self, value: float = 0.0) -> bool:
        """区间是否不包含 value（比如差值区间不包含 0 = 差异在该置信水平下显著）。"""
        return value < self.low or value > self.high

    def fmt(self, pct: bool = True, signed: bool = False) -> str:
        f = (lambda v: f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%") if pct else (lambda v: f"{v:+.3f}" if signed else f"{v:.3f}")
        return f"{f(self.estimate)}  [{f(self.low)}, {f(self.high)}]"


def _check_rate_args(successes: int, n: int, z: float) -> None:
    if n < 0 or successes < 0:
        raise ValueError(f"successes 和 n 不能为负（successes={successes}, n={n}）")
    if successes > n:
        raise ValueError(f"successes={successes} 大于 n={n}")
    if z <= 0:
        raise ValueError(f"z 必须为正数，收到 {z}")


# =====================================================================
# 1. 单个通过率的置信区间
# =====================================================================


def wald_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """教科书上的"正态近似"区间 p ± z·√(p(1-p)/n)。只用来做反面教材：
    p=0 或 p=1 时宽度为 0（10/10 通过 → "100% ± 0"，显然过度自信），小样本时覆盖率也不足。"""
    _check_rate_args(successes, n, z)
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    half = z * math.sqrt(p * (1 - p) / n)
    return (max(0.0, p - half), min(1.0, p + half))


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson 得分区间（Wilson score interval）：小样本、极端通过率下都靠谱的二项比例区间。

    直觉：Wald 区间用"观测到的 p"估计标准误，p=1 时标准误算成 0；
    Wilson 反过来问"真实通过率为多少时，观测到这个结果不算意外"，所以 10/10 也会给出 [0.72, 1.00]。

    边界约定：
    - n == 0：没有任何信息，返回 (0.0, 1.0)；
    - 全部成功：上界精确为 1.0；全部失败：下界精确为 0.0（避免浮点误差得到 0.9999999）。
    """
    _check_rate_args(successes, n, z)
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == n else min(1.0, center + half)
    return (low, high)


# =====================================================================
# 2. bootstrap：不依赖分布假设的置信区间
# =====================================================================


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认的 'linear' 方法一致）。"""
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _check_boot_args(n_boot: int, alpha: float) -> None:
    if n_boot < 1:
        raise ValueError(f"n_boot 至少为 1，收到 {n_boot}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha 必须在 (0, 1) 之间，收到 {alpha}")


def _bootstrap_means(values: Sequence[float], n_boot: int, seed: int) -> list[float]:
    """有放回地重采样 n_boot 次，返回排好序的均值列表。

    用 random.Random(seed) 而不是全局 random：同一个 seed 永远得到同一组结果（可复现、可写进测试），
    也不会被程序里别处的 random 调用"偷走"随机数。"""
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += values[int(rng.random() * n)]
        means.append(s / n)
    means.sort()
    return means


def bootstrap_ci(values: Sequence[float], n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> Interval:
    """均值的百分位 bootstrap 置信区间。

    values 的每个元素应该是**一个独立单元**的得分。同一个任务跑了 k 次时，
    先在任务内求平均（得到 0、1/3、2/3、1 这样的分数），再把"任务"作为单元传进来——
    同一任务的 k 次运行彼此相关，把它们当成 k 个独立样本会让区间窄得虚假（见讲义 1.6）。
    """
    if len(values) == 0:
        raise ValueError("values 不能为空")
    _check_boot_args(n_boot, alpha)
    vals = [float(v) for v in values]
    means = _bootstrap_means(vals, n_boot, seed)
    return Interval(_mean(vals), _percentile(means, alpha / 2), _percentile(means, 1 - alpha / 2))


def paired_bootstrap_diff(
    a: Sequence[float], b: Sequence[float], n_boot: int = 2000, seed: int = 0, alpha: float = 0.05
) -> Interval:
    """配对 bootstrap：B 相对 A 的平均提升 mean(b) - mean(a) 及其置信区间。

    a[i] 和 b[i] 必须是**同一个任务**上两个版本的得分（通过=1/失败=0，或多次运行的平均分）。
    关键：重采样的是"任务"，每次抽中任务 i 就同时带上 a[i] 和 b[i]。
    这样"任务本身难不难"这部分方差在相减时被抵消了，只剩下"版本差异"的噪声 ——
    所以配对比较比"各算各的区间、看重不重叠"灵敏得多。

    返回 Interval(estimate=差值, low, high)。区间不包含 0 → 在 1-alpha 置信水平下差异显著。
    """
    if len(a) != len(b):
        raise ValueError(f"a 和 b 必须一一配对（同一批任务），长度却是 {len(a)} 和 {len(b)}")
    if len(a) == 0:
        raise ValueError("a、b 不能为空")
    _check_boot_args(n_boot, alpha)
    diffs = [float(y) - float(x) for x, y in zip(a, b)]
    means = _bootstrap_means(diffs, n_boot, seed)
    return Interval(_mean(diffs), _percentile(means, alpha / 2), _percentile(means, 1 - alpha / 2))


# =====================================================================
# 3. McNemar 检验：同一批任务上两个版本的通过/失败是否有差异
# =====================================================================


@dataclass
class McNemarResult:
    a_only: int  # 只有 A 通过（A 赢）的任务数
    b_only: int  # 只有 B 通过（B 赢）的任务数
    statistic: float  # 精确检验时是较小的那个不一致计数；卡方检验时是卡方统计量
    p_value: float
    method: str  # "exact" 或 "chi2"

    @property
    def discordant(self) -> int:
        return self.a_only + self.b_only


def mcnemar(a: Sequence[bool], b: Sequence[bool], exact: bool | None = None) -> McNemarResult:
    """McNemar 检验。a[i]、b[i] 是同一任务上 A、B 是否通过。

    只有"结论不一致"的任务（一个过、一个没过）携带比较信息；两个都过或都没过的任务对"谁更好"没有贡献。
    原假设"两版本一样好"下，每个不一致任务偏向 A 或 B 的概率各 1/2 —— 就是抛硬币。

    - exact=True：精确二项检验，p = 2 × P(X ≤ min(a_only, b_only))，X ~ Binomial(不一致数, 0.5)。
      任何样本量都成立，不一致任务少（< 25）时必须用它。
    - exact=False：卡方近似（带连续性校正），χ² = (|a_only - b_only| - 1)² / (a_only + b_only)，自由度 1。
      不一致任务多时和精确检验几乎一样；它是"手算年代"的产物，样本少时不准。
    - exact=None（默认）：不一致任务数 < 25 用精确检验，否则用卡方（常用经验规则）。
    """
    if len(a) != len(b):
        raise ValueError(f"a 和 b 必须一一配对，长度却是 {len(a)} 和 {len(b)}")
    a_only = sum(1 for x, y in zip(a, b) if x and not y)
    b_only = sum(1 for x, y in zip(a, b) if y and not x)
    n = a_only + b_only
    if exact is None:
        exact = n < 25
    if n == 0:
        return McNemarResult(a_only, b_only, 0.0, 1.0, "exact" if exact else "chi2")
    if exact:
        k = min(a_only, b_only)
        tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n  # Python 大整数，n 再大也不会下溢
        return McNemarResult(a_only, b_only, float(k), min(1.0, 2 * tail), "exact")
    stat = max(0, abs(a_only - b_only) - 1) ** 2 / n
    p = math.erfc(math.sqrt(stat / 2))  # 自由度为 1 的卡方分布的右尾概率
    return McNemarResult(a_only, b_only, stat, p, "chi2")


# =====================================================================
# 4. 样本量估算：评估之前先算"要多少任务才看得出差别"
# =====================================================================


def n_for_margin(p: float, margin: float, z: float = Z95) -> int:
    """想把通过率的置信区间半宽控制在 ±margin 以内，大约需要多少个独立任务（正态近似）。
    例：p≈0.8、margin=0.05 → 246 个。p 未知时用 0.5（最保守）。"""
    if not 0 <= p <= 1 or margin <= 0:
        raise ValueError("要求 0 ≤ p ≤ 1 且 margin > 0")
    return math.ceil(z * z * p * (1 - p) / margin**2)


def _z_pair(alpha: float, power: float) -> tuple[float, float]:
    if not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("alpha 和 power 必须在 (0, 1) 之间")
    nd = NormalDist()
    return nd.inv_cdf(1 - alpha / 2), nd.inv_cdf(power)


def min_sample_size(p_a: float, p_b: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """两个**独立**样本比较通过率（双侧检验）时，每个版本至少需要多少任务。
    n = (z_{1-α/2}·√(2·p̄·(1-p̄)) + z_{power}·√(p_a(1-p_a) + p_b(1-p_b)))² / (p_a - p_b)²"""
    if p_a == p_b:
        raise ValueError("p_a 与 p_b 相等：没有要检测的差异")
    za, zb = _z_pair(alpha, power)
    pbar = (p_a + p_b) / 2
    num = za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p_a * (1 - p_a) + p_b * (1 - p_b))
    return math.ceil(num**2 / (p_a - p_b) ** 2)


def min_sample_size_paired(p_discordant: float, delta: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """配对设计（同一批任务、McNemar 检验）至少需要多少任务。Connor (1987) 的近似公式：
    n = (z_{1-α/2}·√ψ + z_{power}·√(ψ - δ²))² / δ²
    ψ = p_discordant：两个版本结论不一致的任务比例；δ = delta：通过率之差（= B 独赢比例 - A 独赢比例）。
    两个版本越"同进同退"（ψ 越小），需要的任务越少 —— 这正是配对设计省钱的原因。"""
    if delta == 0:
        raise ValueError("delta 为 0：没有要检测的差异")
    if not 0 < p_discordant <= 1 or abs(delta) > p_discordant:
        raise ValueError("要求 0 < p_discordant ≤ 1 且 |delta| ≤ p_discordant")
    za, zb = _z_pair(alpha, power)
    num = za * math.sqrt(p_discordant) + zb * math.sqrt(p_discordant - delta**2)
    return math.ceil(num**2 / delta**2)


# =====================================================================
# 5. 成对 LLM 评委：交换顺序，去掉位置偏差
# =====================================================================

# judge(first, second) -> "A"（第一个更好）| "B"（第二个更好）| "tie"
Judge = Callable[[str, str], str]


def _normalize_verdict(v: object) -> str:
    s = str(v).strip()
    if s.upper() in ("A", "B"):
        return s.upper()
    if s.lower() == "tie":
        return "tie"
    raise ValueError(f"评委只能返回 'A'、'B' 或 'tie'，收到 {v!r}")


def judge_both_orders(judge: Judge, x: str, y: str) -> tuple[str, str]:
    """按 (x, y) 和 (y, x) 两种顺序各问一次评委，返回两次判决各自认为的赢家（'x' / 'y' / 'tie'）。
    注意返回的是"哪个回答赢了"，而不是"哪个位置赢了"——这样两次结果才能直接比较。"""
    first = _normalize_verdict(judge(x, y))  # x 在前
    second = _normalize_verdict(judge(y, x))  # y 在前
    w1 = {"A": "x", "B": "y", "tie": "tie"}[first]
    w2 = {"A": "y", "B": "x", "tie": "tie"}[second]
    return w1, w2


def debiased_pairwise(judge: Judge, x: str, y: str) -> str:
    """保守的去位置偏差成对评判（Zheng et al. 2023 的做法）：两种顺序都判同一方赢才算赢，否则算平局。
    返回 'x' / 'y' / 'tie'。代价：评委调用次数翻倍；好处：一个"永远选第一个"的评委只能得出平局，没法制造假赢家。"""
    w1, w2 = judge_both_orders(judge, x, y)
    return w1 if w1 == w2 else "tie"


# =====================================================================
# 6. 评委与人工的一致率
# =====================================================================


@dataclass
class Agreement:
    n: int
    observed: float  # 原始一致率：两者给出相同标签的比例
    expected: float  # 随机一致率：两者各自按自己的标签分布"瞎猜"也会撞上的比例
    kappa: float  # Cohen's kappa = (observed - expected) / (1 - expected)
    confusion: dict[tuple[Hashable, Hashable], int] = field(default_factory=dict)  # (评委标签, 人工标签) -> 次数


def judge_agreement(judge_labels: Sequence[Hashable], human_labels: Sequence[Hashable]) -> Agreement:
    """评委与人工标注的一致率和 Cohen's kappa。

    为什么不只看一致率？如果 90% 的样本人工都标"通过"，一个永远说"通过"的评委一致率就有 90%，
    但它什么都没判断。kappa 扣掉了这种"碰巧一致"：永远说"通过"的评委 kappa = 0。
    经验上 kappa > 0.8 算很好、0.6-0.8 算不错、< 0.4 说明评委基本不可用（Landis & Koch 1977 的常用分档，只是粗略参考）。
    两边都只用了同一个标签（expected = 1）时 kappa 无定义，这里约定：完全一致返回 1.0，否则 0.0。
    """
    if len(judge_labels) != len(human_labels):
        raise ValueError(f"标签数量不一致：评委 {len(judge_labels)}，人工 {len(human_labels)}")
    n = len(judge_labels)
    if n == 0:
        raise ValueError("没有可比较的标签")
    confusion = Counter(zip(judge_labels, human_labels))
    observed = sum(c for (j, h), c in confusion.items() if j == h) / n
    pj, ph = Counter(judge_labels), Counter(human_labels)
    expected = sum(pj[k] * ph.get(k, 0) for k in pj) / (n * n)
    if expected >= 1.0:
        kappa = 1.0 if observed >= 1.0 else 0.0
    else:
        kappa = (observed - expected) / (1 - expected)
    return Agreement(n, observed, expected, kappa, dict(confusion))


def kappa_bootstrap_ci(
    judge_labels: Sequence[Hashable], human_labels: Sequence[Hashable], n_boot: int = 2000, seed: int = 0, alpha: float = 0.05
) -> Interval:
    """kappa 的百分位 bootstrap 区间：按"条"有放回地重采样，每次重算 kappa。
    一致率本身是个比例，直接用 wilson_interval(一致条数, n) 就行；kappa 不是简单比例，所以用 bootstrap。
    样本很少（比如 8 条）时区间会宽得吓人 —— 这正是它要告诉你的：这点校准数据还说明不了什么。"""
    if len(judge_labels) != len(human_labels) or not judge_labels:
        raise ValueError("标签数量必须一致且不为空")
    _check_boot_args(n_boot, alpha)
    rng = random.Random(seed)
    n = len(judge_labels)
    stats = []
    for _ in range(n_boot):
        idx = [int(rng.random() * n) for _ in range(n)]
        stats.append(judge_agreement([judge_labels[i] for i in idx], [human_labels[i] for i in idx]).kappa)
    stats.sort()
    return Interval(judge_agreement(judge_labels, human_labels).kappa, _percentile(stats, alpha / 2), _percentile(stats, 1 - alpha / 2))


# =====================================================================
# 7. 分层抽样：小而准的评估子集
# =====================================================================


def stratified_sample(items: Sequence[T], key: Callable[[T], Hashable], n: int, seed: int = 0) -> list[T]:
    """按 key 分层、按比例分配名额（最大余数法），每层至少 1 个（只要 n 够分）。

    为什么不直接随机抽？随机抽 50 个时，只占 10% 的"高风险"类别可能只抽到 2 个甚至 0 个；
    分层抽样保证每一类都有代表，估计出的总分也更稳（去掉了"各类别抽到多少个"这部分随机性）。"""
    if n <= 0:
        raise ValueError("n 必须为正")
    groups: dict[Hashable, list[T]] = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    total = len(items)
    if n >= total:
        return list(items)
    keys = list(groups)
    quotas = {k: n * len(groups[k]) / total for k in keys}
    alloc = {k: int(quotas[k]) for k in keys}
    if n >= len(keys):  # 每层至少 1 个
        for k in keys:
            alloc[k] = max(alloc[k], 1)
    # 最大余数法补齐 / 削减到恰好 n 个
    while sum(alloc.values()) < n:
        k = max((k for k in keys if alloc[k] < len(groups[k])), key=lambda k: quotas[k] - alloc[k])
        alloc[k] += 1
    while sum(alloc.values()) > n:
        k = min((k for k in keys if alloc[k] > 1), key=lambda k: quotas[k] - alloc[k])
        alloc[k] -= 1
    rng = random.Random(seed)
    out: list[T] = []
    for k in keys:
        out.extend(rng.sample(groups[k], alloc[k]))
    return out
