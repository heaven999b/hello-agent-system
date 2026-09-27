"""第 21 课练习测试：离线、确定、毫秒级。

运行：make lesson N=21    或    .venv/bin/python -m pytest lessons/21_agent_data -v
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from agentkit.evals import EvalCase
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# =====================================================================
# (a) cohen_kappa
# =====================================================================


def test_kappa_textbook_example():
    """50 条：两人都说"是" 20、A是B否 5、A否B是 10、都说"否" 15 → po=0.7，pe=0.5，kappa=0.4。"""
    a = ["yes"] * 20 + ["yes"] * 5 + ["no"] * 10 + ["no"] * 15
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    assert ex.cohen_kappa(a, b) == pytest.approx(0.4)
    assert ex.cohen_kappa(b, a) == pytest.approx(0.4)  # 对称


def test_kappa_perfect_and_worse_than_chance():
    labels = ["pass", "fail", "pass", "fail", "pass"]
    assert ex.cohen_kappa(labels, list(labels)) == pytest.approx(1.0)
    flipped = ["fail" if x == "pass" else "pass" for x in labels[:4]]
    assert ex.cohen_kappa(labels[:4], flipped) == pytest.approx(-1.0)  # 系统性唱反调：比瞎猜还差


def test_kappa_high_agreement_but_low_kappa():
    """kappa 悖论：90% 的样本一致，kappa 却是负的 —— 因为两人都几乎只说 pass，碰巧一致的概率本来就有 90.5%。"""
    a = ["pass"] * 90 + ["pass"] * 5 + ["fail"] * 5
    b = ["pass"] * 90 + ["fail"] * 5 + ["pass"] * 5
    assert sum(x == y for x, y in zip(a, b)) / 100 == 0.9
    assert ex.cohen_kappa(a, b) == pytest.approx((0.90 - 0.905) / (1 - 0.905))
    assert ex.cohen_kappa(a, b) < 0


def test_kappa_multiclass_with_missing_category():
    """三类标签，B 从来没用过 "partial"。手算：n=6，一致 3 条；
    A: pass 2 / fail 2 / partial 2，B: pass 4 / fail 2 → pe = (2·4 + 2·2 + 2·0)/36 = 1/3，kappa = (0.5-1/3)/(2/3) = 0.25"""
    a = ["pass", "pass", "fail", "fail", "partial", "partial"]
    b = ["pass", "pass", "fail", "pass", "pass", "fail"]
    assert ex.cohen_kappa(a, b) == pytest.approx(0.25)
    assert ex.cohen_kappa([1, 2, 3, 3], [1, 2, 3, 3]) == pytest.approx(1.0)  # 标签不一定是字符串


def test_kappa_expected_agreement_is_one():
    """两人都只用过同一个标签：pe = 1，公式是 0/0，约定返回 1.0（而且不能抛 ZeroDivisionError）。"""
    assert ex.cohen_kappa(["pass"] * 7, ["pass"] * 7) == 1.0


def test_kappa_rejects_bad_input():
    with pytest.raises(ValueError):
        ex.cohen_kappa(["pass", "fail"], ["pass"])
    with pytest.raises(ValueError):
        ex.cohen_kappa([], [])


# =====================================================================
# (b) stratified_sample
# =====================================================================


def _records(sizes: dict[str, int]) -> list[dict]:
    """按层大小造数据，并打乱顺序（层在原始数据里是交错出现的）。"""
    recs = [{"id": f"{k}-{i}", "path": k} for k, s in sizes.items() for i in range(s)]
    random.Random(42).shuffle(recs)
    return recs


def test_stratified_exact_total_and_proportional():
    recs = _records({"search": 60, "order": 30, "refund": 10})
    out = ex.stratified_sample(recs, lambda r: r["path"], 10, seed=1)
    assert len(out) == 10
    assert Counter(r["path"] for r in out) == {"search": 6, "order": 3, "refund": 1}
    assert len({r["id"] for r in out}) == 10  # 没有重复
    assert all(r in recs for r in out)


def test_stratified_every_stratum_at_least_one():
    """两条长尾路径各只有 1 条：按比例它们都该分到 0.05 条，但每层至少 1 条，名额从大层里扣。"""
    recs = _records({"search": 98, "appeal": 1, "loop": 1})
    out = ex.stratified_sample(recs, lambda r: r["path"], 5, seed=0)
    assert Counter(r["path"] for r in out) == {"search": 3, "appeal": 1, "loop": 1}


def test_stratified_reproducible_and_seed_matters():
    recs = _records({"search": 50, "order": 20, "refund": 5, "human": 2})
    before = list(recs)
    a = ex.stratified_sample(recs, lambda r: r["path"], 12, seed=7)
    b = ex.stratified_sample(recs, lambda r: r["path"], 12, seed=7)
    assert a == b
    assert recs == before  # 不修改输入
    others = [ex.stratified_sample(recs, lambda r: r["path"], 12, seed=s) for s in range(8)]
    assert any(o != a for o in others)  # 换种子，结果会变


def test_stratified_keeps_original_order():
    recs = _records({"a": 9, "b": 6, "c": 3})
    out = ex.stratified_sample(recs, lambda r: r["path"], 9, seed=3)
    positions = [recs.index(r) for r in out]
    assert positions == sorted(positions)


def test_stratified_take_all_and_edges():
    recs = _records({"a": 3, "b": 2})
    out = ex.stratified_sample(recs, lambda r: r["path"], 5, seed=0)
    assert out == recs  # n = 全部：原样返回（原始顺序）
    assert ex.stratified_sample([], lambda r: r["path"], 0, seed=0) == []


def test_stratified_rejects_impossible_requests():
    recs = _records({"a": 5, "b": 5, "c": 5})
    with pytest.raises(ValueError):
        ex.stratified_sample(recs, lambda r: r["path"], 2, seed=0)  # 3 层却只要 2 条
    with pytest.raises(ValueError):
        ex.stratified_sample(recs, lambda r: r["path"], 16, seed=0)  # 比总数还多
    with pytest.raises(ValueError):
        ex.stratified_sample(recs, lambda r: r["path"], -1, seed=0)


# =====================================================================
# (c) split_no_leak
# =====================================================================

RATIOS = {"train": 0.6, "dev": 0.2, "test": 0.2}


def _grouped(sizes: list[int]) -> list[dict]:
    recs = [{"id": f"g{g}-{i}", "group": f"g{g}"} for g, s in enumerate(sizes) for i in range(s)]
    random.Random(0).shuffle(recs)
    return recs


def _check_partition(recs, splits):
    flat = [r for part in splits.values() for r in part]
    assert sorted(r["id"] for r in flat) == sorted(r["id"] for r in recs)  # 不丢不重
    owner = {}
    for name, part in splits.items():
        for r in part:
            assert owner.setdefault(r["group"], name) == name, f"组 {r['group']} 同时出现在 {owner[r['group']]} 和 {name}"


def test_split_no_group_crosses_sets():
    recs = _grouped([random.Random(i).randint(1, 5) for i in range(40)])
    splits = ex.split_no_leak(recs, lambda r: r["group"], RATIOS, seed=0)
    assert set(splits) == set(RATIOS)
    _check_partition(recs, splits)


def test_split_hits_ratios_with_equal_groups():
    """20 个组、每组 3 条：目标 36 / 12 / 12 恰好能凑整，贪心应该一条不差。"""
    recs = _grouped([3] * 20)
    splits = ex.split_no_leak(recs, lambda r: r["group"], RATIOS, seed=5)
    assert {k: len(v) for k, v in splits.items()} == {"train": 36, "dev": 12, "test": 12}


def test_split_close_to_ratios_with_uneven_groups():
    sizes = [random.Random(100 + i).randint(1, 5) for i in range(60)]
    recs = _grouped(sizes)
    splits = ex.split_no_leak(recs, lambda r: r["group"], RATIOS, seed=3)
    for name, ratio in RATIOS.items():
        assert abs(len(splits[name]) - ratio * len(recs)) <= 2 * max(sizes), (name, len(splits[name]))


def test_split_reproducible_order_and_seed():
    recs = _grouped([2, 1, 4, 3, 2, 2, 1, 5, 3, 1, 2, 2])
    a = ex.split_no_leak(recs, lambda r: r["group"], RATIOS, seed=11)
    b = ex.split_no_leak(recs, lambda r: r["group"], RATIOS, seed=11)
    assert a == b
    for part in a.values():  # 集合内保持原始顺序
        positions = [recs.index(r) for r in part]
        assert positions == sorted(positions)
    others = [ex.split_no_leak(recs, lambda r: r["group"], RATIOS, seed=s) for s in range(10)]
    assert any(o != a for o in others)


def test_split_synthetic_variants_stay_together():
    """真实用法：合成用例的 tags 里带着 seed:S1，同一个种子的变体必须整组进同一个集合。"""
    cases = [EvalCase(id=f"syn-S{s}-{v}", input=f"问题 {s}-{v}", tags=["synthetic", f"seed:S{s}"]) for s in range(10) for v in range(4)]
    seed_of = lambda c: next(t for t in c.tags if t.startswith("seed:"))  # noqa: E731
    splits = ex.split_no_leak(cases, seed_of, {"train": 0.5, "test": 0.5}, seed=0)
    train_seeds = {seed_of(c) for c in splits["train"]}
    test_seeds = {seed_of(c) for c in splits["test"]}
    assert train_seeds and test_seeds and not (train_seeds & test_seeds)
    assert len(splits["train"]) == len(splits["test"]) == 20


def test_split_zero_ratio_and_giant_group():
    recs = _grouped([30, 1, 1, 1, 1])  # 一个"大客户"占了 30/34：只能整组进某一个集合
    splits = ex.split_no_leak(recs, lambda r: r["group"], {"train": 0.8, "dev": 0.0, "test": 0.2}, seed=0)
    assert splits["dev"] == []
    _check_partition(recs, splits)


def test_split_rejects_bad_ratios():
    recs = _grouped([1, 1, 1])
    for bad in ({}, {"train": 0.7, "test": 0.2}, {"train": 1.2, "test": -0.2}):
        with pytest.raises(ValueError):
            ex.split_no_leak(recs, lambda r: r["group"], bad, seed=0)
