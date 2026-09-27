"""第 22 课练习测试：离线、确定、毫秒级。

运行：make lesson N=22    或    .venv/bin/python -m pytest lessons/22_eval_methodology -v
"""

from __future__ import annotations

import random

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# =====================================================================
# (a) wilson_interval
# =====================================================================


def test_wilson_known_value():
    low, high = ex.wilson_interval(45, 50)
    assert low == pytest.approx(0.7864, abs=1e-3)
    assert high == pytest.approx(0.9565, abs=1e-3)


def test_wilson_n_zero_means_no_information():
    assert ex.wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_all_success_upper_bound_is_exactly_one():
    low, high = ex.wilson_interval(10, 10)
    assert high == 1.0  # 恰好 1.0，不是 0.9999999
    assert low == pytest.approx(0.7225, abs=1e-3)  # 10/10 也不等于"100% 可靠"


def test_wilson_all_failure_lower_bound_is_exactly_zero():
    low, high = ex.wilson_interval(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.2775, abs=1e-3)


@pytest.mark.parametrize("successes,n,z", [(11, 10, 1.96), (-1, 10, 1.96), (1, -1, 1.96), (3, 10, 0.0), (3, 10, -1.0)])
def test_wilson_rejects_invalid_arguments(successes, n, z):
    with pytest.raises(ValueError):
        ex.wilson_interval(successes, n, z)


def test_wilson_is_symmetric_and_within_unit_interval():
    for k in range(0, 21):
        low, high = ex.wilson_interval(k, 20)
        rlow, rhigh = ex.wilson_interval(20 - k, 20)
        assert 0.0 <= low <= k / 20 <= high <= 1.0
        assert low == pytest.approx(1 - rhigh, abs=1e-12)
        assert high == pytest.approx(1 - rlow, abs=1e-12)


def test_wilson_width_shrinks_with_n_and_grows_with_z():
    w = lambda lo_hi: lo_hi[1] - lo_hi[0]  # noqa: E731
    assert w(ex.wilson_interval(5, 10)) > w(ex.wilson_interval(50, 100)) > w(ex.wilson_interval(500, 1000))
    assert w(ex.wilson_interval(40, 50, z=2.576)) > w(ex.wilson_interval(40, 50, z=1.96))


# =====================================================================
# (b) paired_bootstrap_diff
# =====================================================================


def test_paired_bootstrap_rejects_unpaired_data():
    with pytest.raises(ValueError):
        ex.paired_bootstrap_diff([1, 0, 1], [1, 0], n_boot=100, seed=0)
    with pytest.raises(ValueError):
        ex.paired_bootstrap_diff([], [], n_boot=100, seed=0)


def test_paired_bootstrap_rejects_bad_parameters():
    with pytest.raises(ValueError):
        ex.paired_bootstrap_diff([1, 0], [1, 1], n_boot=0, seed=0)
    with pytest.raises(ValueError):
        ex.paired_bootstrap_diff([1, 0], [1, 1], n_boot=100, seed=0, alpha=1.5)


def test_paired_bootstrap_sign_convention_is_b_minus_a():
    a = [1, 1, 0, 0, 1, 0, 1, 0]
    b = [1, 1, 1, 1, 1, 0, 1, 1]
    est, low, high = ex.paired_bootstrap_diff(a, b, n_boot=500, seed=1)
    assert est == pytest.approx(sum(b) / 8 - sum(a) / 8)  # B 比 A 高 → 正数
    assert low <= est <= high
    est2, _, _ = ex.paired_bootstrap_diff(b, a, n_boot=500, seed=1)
    assert est2 == pytest.approx(-est)


def test_paired_bootstrap_identical_versions_give_zero_width_interval():
    a = [1, 0, 1, 1, 0, 1]
    assert tuple(ex.paired_bootstrap_diff(a, list(a), n_boot=300, seed=0)) == pytest.approx((0.0, 0.0, 0.0))
    # 每个任务都从 0 变 1：差值恒为 1，重采样怎么抽都是 1
    assert tuple(ex.paired_bootstrap_diff([0] * 5, [1] * 5, n_boot=300, seed=0)) == pytest.approx((1.0, 1.0, 1.0))


def test_paired_bootstrap_is_reproducible_and_ignores_global_random_state():
    a = [1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0]
    b = [1, 1, 1, 0, 0, 1, 1, 1, 0, 1, 1, 1]
    random.seed(123)
    r1 = ex.paired_bootstrap_diff(a, b, n_boot=1000, seed=7)
    random.seed(456)
    random.random()
    r2 = ex.paired_bootstrap_diff(a, b, n_boot=1000, seed=7)
    assert tuple(r1) == tuple(r2)


def test_paired_bootstrap_detects_consistent_improvement_but_not_noise():
    # 40 个任务：B 修好了 15 个、没弄坏任何一个 → 区间应远离 0
    a = [0] * 15 + [1] * 20 + [0] * 5
    b = [1] * 15 + [1] * 20 + [0] * 5
    _, low, _ = ex.paired_bootstrap_diff(a, b, n_boot=2000, seed=0)
    assert low > 0
    # 40 个任务：B 修好 3 个、弄坏 3 个 → 平均差 0，区间必须包含 0
    a = [0] * 3 + [1] * 3 + [1] * 30 + [0] * 4
    b = [1] * 3 + [0] * 3 + [1] * 30 + [0] * 4
    est, low, high = ex.paired_bootstrap_diff(a, b, n_boot=2000, seed=0)
    assert est == pytest.approx(0.0)
    assert low < 0 < high


def test_paired_bootstrap_accepts_bools_and_fractional_scores():
    a = [True, False, True, True]
    b = [2 / 3, 1.0, 1 / 3, 1.0]  # 多次运行的平均分
    est, low, high = ex.paired_bootstrap_diff(a, b, n_boot=200, seed=3)
    assert est == pytest.approx((2 / 3 + 1 + 1 / 3 + 1) / 4 - 3 / 4)
    assert low <= high


def test_paired_bootstrap_wider_interval_for_higher_confidence():
    a = [1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1]
    b = [1, 1, 1, 0, 0, 1, 1, 1, 0, 1, 1, 1, 1, 1]
    _, lo90, hi90 = ex.paired_bootstrap_diff(a, b, n_boot=2000, seed=0, alpha=0.10)
    _, lo99, hi99 = ex.paired_bootstrap_diff(a, b, n_boot=2000, seed=0, alpha=0.01)
    assert lo99 <= lo90 and hi90 <= hi99
    assert (hi99 - lo99) > (hi90 - lo90)


# =====================================================================
# (c) debiased_pairwise
# =====================================================================

SHORT, LONG = "好的，已退款。", "您好，已按政策为您退款 239.20 元，预计 1-3 个工作日到账。"


def test_always_first_judge_can_only_produce_ties():
    always_a = lambda first, second: "A"  # noqa: E731  —— 极端的位置偏差
    always_b = lambda first, second: "B"  # noqa: E731
    for judge in (always_a, always_b):
        assert ex.debiased_pairwise(judge, SHORT, LONG) == "tie"
        assert ex.debiased_pairwise(judge, LONG, SHORT) == "tie"


def test_consistent_judge_wins_regardless_of_argument_order():
    prefers_longer = lambda first, second: "A" if len(first) > len(second) else "B"  # noqa: E731
    assert ex.debiased_pairwise(prefers_longer, LONG, SHORT) == "x"
    assert ex.debiased_pairwise(prefers_longer, SHORT, LONG) == "y"


def test_judge_called_exactly_twice_in_both_orders():
    calls = []

    def judge(first, second):
        calls.append((first, second))
        return "A"

    ex.debiased_pairwise(judge, "X 回复", "Y 回复")
    assert calls == [("X 回复", "Y 回复"), ("Y 回复", "X 回复")]


def test_one_side_tie_or_both_tie_is_tie():
    # 第一次判 x 赢，第二次判平局 → 不够"坚定"，算平局
    answers = iter(["A", "tie"])
    assert ex.debiased_pairwise(lambda f, s: next(answers), "x 回复", "y 回复") == "tie"
    assert ex.debiased_pairwise(lambda f, s: "tie", "x 回复", "y 回复") == "tie"


def test_verdict_normalization_and_invalid_output():
    answers = iter([" b ", "a"])  # 第一次：第二个(y)赢；第二次：第一个(y)赢 → y
    assert ex.debiased_pairwise(lambda f, s: next(answers), "x 回复", "y 回复") == "y"
    answers = iter(["TIE", "Tie"])
    assert ex.debiased_pairwise(lambda f, s: next(answers), "x 回复", "y 回复") == "tie"
    for bad in ("C", "", "x", "first"):
        with pytest.raises(ValueError):
            ex.debiased_pairwise(lambda f, s, bad=bad: bad, "x 回复", "y 回复")
