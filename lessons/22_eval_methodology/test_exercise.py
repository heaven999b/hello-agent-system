"""第 22 课练习测试：离线、确定、毫秒级。

运行：make lesson N=22    或    .venv/bin/python -m pytest lessons/22_eval_methodology -v

前三组测试练习（纯统计，普通函数）；最后一组测试 refund_bench 的 async harness：
并发真的发生、上限守得住、并发下每次试验的环境仍然互相隔离、基础设施错误单独标记。这组不依赖练习。
"""

from __future__ import annotations

import random

import pytest

from agentkit import LLMError
from agentkit.testing import load_exercise, load_sibling

ex = load_exercise(__file__)
rb = load_sibling(__file__, "refund_bench")


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


# =====================================================================
# refund_bench 的 async harness（不是练习）
# =====================================================================

TASKS = rb.load_tasks()


def _jobs(k: int = 1) -> list:
    return [rb.TrialJob(f"{t.id}|{i}", "A", "你是退款助手。", t, i) for t in TASKS for i in range(k)]


@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_run_trials_concurrency_is_real_and_bounded(limit):
    llm = rb.scripted_oracle_llm(TASKS, latency=0.02)
    jobs = _jobs()
    rows = await rb.run_trials(llm, jobs, concurrency=limit)
    assert llm.max_in_flight == limit  # 上限是几，在途峰值就是几
    assert llm.call_count == len(jobs)  # 作出决定即停：每次试验恰好 1 次模型调用
    assert [r["key"] for r in rows] == [j.key for j in jobs]  # 按 jobs 的顺序返回
    assert all(r["passed"] for r in rows)


async def test_run_trials_environment_stays_isolated_under_concurrency():
    """16 个任务 × 3 次，同时在途 8 个。如果试验之间共用账本，StopAfterDecision 会让后面的试验一次模型都不调，
    评分器也会看到"多于一个决定"—— 全部通过、调用次数恰好等于试验数，说明并发下每次试验仍是干净的环境。"""
    llm = rb.scripted_oracle_llm(TASKS, latency=0.01)
    progress = []
    rows = await rb.run_trials(llm, _jobs(3), concurrency=8, on_progress=lambda i, n: progress.append((i, n)))
    assert llm.max_in_flight == 8
    assert all(r["passed"] and r["decision"].count(",") == 0 for r in rows)
    assert llm.call_count == len(rows) == 48
    assert progress == [(i, 48) for i in range(1, 49)]


async def test_run_trials_infra_error_is_marked_and_does_not_stop_others():
    oracle = rb.scripted_oracle_llm(TASKS, latency=0.0)

    def respond(messages):
        if "订单号：YS-1003" in messages[-1]["content"]:
            raise LLMError("503 Service Unavailable", status_code=503, retryable=False)
        return oracle.responder(messages)

    from agentkit import ScriptedLLM

    llm = ScriptedLLM(responder=respond, latency=0.01)
    rows = await rb.run_trials(llm, _jobs(), concurrency=4)
    bad = [r for r in rows if r["error"]]
    assert [r["task_id"] for r in bad] == ["YS-1003"]
    assert bad[0]["reason"] == "infra_error" and bad[0]["error"].startswith("llm_error")
    assert sum(r["passed"] for r in rows) == len(TASKS) - 1  # 其余试验照常完成，没有被取消
