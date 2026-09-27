"""第 23 课练习测试：离线、确定性（不调用任何模型，全部用纯函数和假程序）。

运行：make lesson N=23    或    .venv/bin/python -m pytest lessons/23_optimization -v
"""

from __future__ import annotations

import copy

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
Example = ex.Example
Demo = ex.Demo


# =====================================================================
# (a) select_topk
# =====================================================================


def test_topk_sorted_by_score_desc():
    history = [("A", 0.5), ("B", 0.9), ("C", 0.7), ("D", 0.1)]
    assert ex.select_topk(history, 3) == [("B", 0.9), ("C", 0.7), ("A", 0.5)]


def test_topk_ties_keep_first_occurrence_order():
    history = [("早", 0.8), ("中", 0.6), ("晚", 0.8), ("末", 0.8)]
    assert ex.select_topk(history, 4) == [("早", 0.8), ("晚", 0.8), ("末", 0.8), ("中", 0.6)]


def test_topk_dedup_keeps_best_score_and_first_position():
    # "A" 第一次出现在下标 0，分数 0.5；后来被重新评估得到 0.9 → 保留 0.9，但同分比较时位置仍算下标 0
    history = [("A", 0.5), ("B", 0.9), ("C", 0.3), ("A", 0.9), ("A", 0.2)]
    assert ex.select_topk(history, 5) == [("A", 0.9), ("B", 0.9), ("C", 0.3)]


def test_topk_whitespace_variants_are_duplicates():
    history = [("先想清楚由哪个团队处理。\n", 0.6), ("  先想清楚由哪个团队处理。", 0.7), ("另一条", 0.65)]
    assert ex.select_topk(history, 3) == [("先想清楚由哪个团队处理。", 0.7), ("另一条", 0.65)]


def test_topk_edge_cases_and_no_mutation():
    history = [("A", 0.5), ("B", 0.9), ("A", 0.7)]
    snapshot = copy.deepcopy(history)
    assert ex.select_topk(history, 10) == [("B", 0.9), ("A", 0.7)]  # k 大于去重后的条数：全部返回
    assert ex.select_topk(history, 0) == []
    assert ex.select_topk(history, -1) == []
    assert ex.select_topk([], 3) == []
    assert history == snapshot  # 不能修改传入的历史


# =====================================================================
# (b) bootstrap_demos
# =====================================================================


def _exact(example, output: str) -> bool:
    return output.strip().endswith(example.label)


class CountingProgram:
    """假程序：按字典返回固定输出，并记录被调用了哪些输入。"""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        out = self.answers[text]
        if isinstance(out, Exception):
            raise out
        return out


TRAIN = [
    Example("U 盘用不了", "security", id="t1"),
    Example("忘记密码", "password_reset", id="t2"),
    Example("打印机找不到", "hardware", id="t3"),
    Example("申请显示器", "access_request", id="t4"),
    Example("Excel 崩溃", "software", id="t5"),
]
ANSWERS = {
    "U 盘用不了": "看起来是硬件问题\n类别：hardware",  # 错
    "忘记密码": "员工本人忘记密码\n类别：password_reset",  # 对
    "打印机找不到": "打印机一律归硬件组\n类别：hardware",  # 对
    "申请显示器": "显示器坏了？\n类别：hardware",  # 错
    "Excel 崩溃": "应用程序崩溃\n类别：software",  # 对
}


def test_bootstrap_collects_only_passing_in_train_order():
    prog = CountingProgram(ANSWERS)
    demos = ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=10)
    assert [d.input for d in demos] == ["忘记密码", "打印机找不到", "Excel 崩溃"]
    assert all(isinstance(d, Demo) for d in demos)
    # 结果确定：再跑一遍一模一样
    assert ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, _exact, max_demos=10) == demos


def test_bootstrap_uses_program_output_not_gold_label():
    demos = ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, _exact, max_demos=1)
    assert demos == [Demo("忘记密码", "员工本人忘记密码\n类别：password_reset")]  # 带推理过程的完整输出


def test_bootstrap_respects_cap_and_stops_calling_early():
    prog = CountingProgram(ANSWERS)
    demos = ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=2)
    assert [d.input for d in demos] == ["忘记密码", "打印机找不到"]
    # 第 3 条（t3）通过后已经收满 2 条：t4、t5 不应该再被调用
    assert prog.calls == ["U 盘用不了", "忘记密码", "打印机找不到"]


def test_bootstrap_zero_cap_makes_no_calls():
    prog = CountingProgram(ANSWERS)
    assert ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=0) == []
    assert ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=-3) == []
    assert prog.calls == []


def test_bootstrap_threshold_for_partial_scores():
    scores = {"t1": 0.5, "t2": 1.0, "t3": 0.0, "t4": 0.75, "t5": True}

    def partial(example, output):
        return scores[example.id]

    strict = ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, partial, max_demos=10)
    assert [d.input for d in strict] == ["忘记密码", "Excel 崩溃"]  # 默认阈值 1.0；True 当作 1.0
    loose = ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, partial, max_demos=10, threshold=0.5)
    assert [d.input for d in loose] == ["U 盘用不了", "忘记密码", "申请显示器", "Excel 崩溃"]


def test_bootstrap_skips_examples_that_raise():
    answers = dict(ANSWERS)
    answers["忘记密码"] = TimeoutError("模型超时")
    prog = CountingProgram(answers)
    demos = ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=2)
    assert [d.input for d in demos] == ["打印机找不到", "Excel 崩溃"]
    assert prog.calls == [e.input for e in TRAIN]


# =====================================================================
# (c) pareto_front
# =====================================================================


def test_front_removes_dominated():
    cands = {"base": [1, 0, 0, 1], "better": [1, 1, 0, 1], "worse": [0, 0, 0, 1]}
    assert ex.pareto_front(cands) == ["better"]


def test_front_keeps_tradeoffs():
    cands = {"擅长安全": [1, 1, 0, 0], "擅长网络": [0, 0, 1, 1], "平庸": [1, 0, 0, 0]}
    assert ex.pareto_front(cands) == ["擅长安全", "擅长网络"]


def test_front_identical_vectors_both_kept():
    cands = {"A": [1, 0], "B": [0, 1], "C": [0, 0], "D": [1, 0]}
    assert ex.pareto_front(cands) == ["A", "B", "D"]


def test_front_identical_vectors_dominated_by_a_third_are_both_removed():
    cands = {"x": [0.5, 0.5], "y": [0.5, 0.5], "z": [0.5, 0.75]}
    assert ex.pareto_front(cands) == ["z"]


def test_front_edge_cases():
    assert ex.pareto_front({}) == []
    assert ex.pareto_front({"only": [0.2, 0.4]}) == ["only"]
    assert ex.pareto_front({"a": [1, 1], "b": [1, 1], "c": [1, 1]}) == ["a", "b", "c"]
    assert ex.pareto_front({"a": [], "b": []}) == ["a", "b"]  # 没有子任务：谁也不支配谁


def test_front_preserves_insertion_order():
    cands = {"z": [0, 1, 0], "a": [1, 0, 0], "m": [0, 0, 1], "dominated": [0, 0, 0]}
    assert ex.pareto_front(cands) == ["z", "a", "m"]


def test_front_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        ex.pareto_front({"a": [1, 0], "b": [1]})
