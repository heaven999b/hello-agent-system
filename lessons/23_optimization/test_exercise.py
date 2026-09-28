"""第 23 课练习测试：离线、确定性（不调用任何模型，全部用纯函数、假程序和剧本模型）。

运行：make lesson N=23    或    .venv/bin/python -m pytest lessons/23_optimization -v

(a)(c) 是纯函数；(b) 是 async 函数（假程序也是 async 的）。最后一组测试 optkit 的并发（不是练习）：
记账在并发下不丢数、评估和 N 次采样真的同时发出、上限守得住、并发不改变结果。
"""

from __future__ import annotations

import asyncio
import copy
import time

import pytest

from agentkit import ScriptedLLM, reply
from agentkit.testing import load_exercise

ex = load_exercise(__file__)
Example = ex.Example
Demo = ex.Demo
optkit = ex.optkit if hasattr(ex, "optkit") else ex._load_sibling("optkit")


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
    """假程序（async，和 optkit.Program 一样要 await）：按字典返回固定输出，记录被调用了哪些输入、同时在途几个。"""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, text: str) -> str:
        self.calls.append(text)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.001)  # 假装在等模型：让出事件循环
            out = self.answers[text]
            if isinstance(out, Exception):
                raise out
            return out
        finally:
            self.in_flight -= 1


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


async def test_bootstrap_collects_only_passing_in_train_order():
    prog = CountingProgram(ANSWERS)
    demos = await ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=10)
    assert [d.input for d in demos] == ["忘记密码", "打印机找不到", "Excel 崩溃"]
    assert all(isinstance(d, Demo) for d in demos)
    # 结果确定：再跑一遍一模一样
    assert await ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, _exact, max_demos=10) == demos


async def test_bootstrap_uses_program_output_not_gold_label():
    demos = await ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, _exact, max_demos=1)
    assert demos == [Demo("忘记密码", "员工本人忘记密码\n类别：password_reset")]  # 带推理过程的完整输出


async def test_bootstrap_respects_cap_and_stops_calling_early():
    prog = CountingProgram(ANSWERS)
    demos = await ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=2)
    assert [d.input for d in demos] == ["忘记密码", "打印机找不到"]
    # 第 3 条（t3）通过后已经收满 2 条：t4、t5 不应该再被调用
    assert prog.calls == ["U 盘用不了", "忘记密码", "打印机找不到"]
    assert prog.max_in_flight == 1  # 一条 await 完再下一条：并发发出去的请求收不回来，"收满就停"就省不下钱


async def test_bootstrap_zero_cap_makes_no_calls():
    prog = CountingProgram(ANSWERS)
    assert await ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=0) == []
    assert await ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=-3) == []
    assert prog.calls == []


async def test_bootstrap_threshold_for_partial_scores():
    scores = {"t1": 0.5, "t2": 1.0, "t3": 0.0, "t4": 0.75, "t5": True}

    def partial(example, output):
        return scores[example.id]

    strict = await ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, partial, max_demos=10)
    assert [d.input for d in strict] == ["忘记密码", "Excel 崩溃"]  # 默认阈值 1.0；True 当作 1.0
    loose = await ex.bootstrap_demos(CountingProgram(ANSWERS), TRAIN, partial, max_demos=10, threshold=0.5)
    assert [d.input for d in loose] == ["U 盘用不了", "忘记密码", "申请显示器", "Excel 崩溃"]


async def test_bootstrap_skips_examples_that_raise():
    answers = dict(ANSWERS)
    answers["忘记密码"] = TimeoutError("模型超时")
    prog = CountingProgram(answers)
    demos = await ex.bootstrap_demos(prog, TRAIN, _exact, max_demos=2)
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


# =====================================================================
# optkit 的并发（不是练习）
# =====================================================================


def _classifier(label: str = "hardware"):
    return ScriptedLLM(responder=lambda m: reply(f"看起来是设备问题。\n类别：{label}", 30, 10), latency=0.02)


async def test_metered_llm_counts_exactly_under_concurrency_without_lock():
    """40 个协程同时调用同一个 MeteredLLM：在途峰值 40（真并发），计数、token 一个不差 —— 没有锁也不丢数，
    因为 await 返回之后的读-改-写中间没有 await，单个事件循环里不会被别的协程插队。"""
    metered = optkit.MeteredLLM(_classifier())
    await asyncio.gather(*(metered.chat([{"role": "user", "content": str(i)}]) for i in range(40)))
    assert metered.max_in_flight == 40 and metered.in_flight == 0
    assert metered.calls == 40
    assert (metered.usage.input_tokens, metered.usage.output_tokens) == (40 * 30, 40 * 10)


@pytest.mark.parametrize("limit", [1, 3])
async def test_program_task_evaluates_concurrently_within_limit(limit):
    metered = optkit.MeteredLLM(_classifier("hardware"))
    prog = optkit.Program(metered, "分类工单", output_format="最后一行写 类别：xxx")
    task = optkit.ProgramTask(prog, _exact, max_concurrency=limit)
    recs = await task.run("分类工单", TRAIN)
    assert metered.max_in_flight == limit  # 上限是几，在途峰值就是几
    assert [r.input for r in recs] == [e.input for e in TRAIN]  # 按样本顺序返回
    assert [r.score for r in recs] == [0.0, 0.0, 1.0, 0.0, 0.0]  # 全答 hardware：只有 t3 对


async def test_program_task_counts_failed_calls_as_zero_and_keeps_going():
    def responder(messages):
        if "忘记密码" in messages[-1]["content"]:
            raise TimeoutError("模型超时")
        return reply("类别：hardware")

    task = optkit.ProgramTask(optkit.Program(ScriptedLLM(responder=responder, latency=0.01), "分类"), _exact, max_concurrency=5)
    recs = await task.run("分类", TRAIN)
    assert task.errors == 1 and recs[1].output.startswith("[调用失败] TimeoutError")
    assert [r.score for r in recs] == [0.0, 0.0, 1.0, 0.0, 0.0]


async def test_sample_n_sends_all_samples_at_once_by_default():
    """测试时计算：n 次采样同时发出 → 用户等的是"一次调用"的时间，不是 n 次之和（调用次数照样是 n）。"""
    llm = _classifier()
    prog = optkit.Program(llm, "分类")
    outs = await optkit.sample_n(lambda: prog("打印机找不到"), 5)
    assert len(outs) == 5 and llm.call_count == 5
    assert llm.max_in_flight == 5

    capped = _classifier()
    prog2 = optkit.Program(capped, "分类")
    await optkit.sample_n(lambda: prog2("打印机找不到"), 5, max_concurrency=2)
    assert capped.max_in_flight == 2  # 网关配额只有 2 时：延迟约为 ceil(5/2)=3 次调用


async def test_sample_n_concurrent_latency_is_about_one_call():
    llm = ScriptedLLM(responder=lambda m: reply("类别：hardware"), latency=0.05)
    prog = optkit.Program(llm, "分类")
    t0 = time.perf_counter()
    await optkit.sample_n(lambda: prog("x"), 5, max_concurrency=1)
    sequential = time.perf_counter() - t0
    t0 = time.perf_counter()
    await optkit.sample_n(lambda: prog("x"), 5)
    concurrent = time.perf_counter() - t0
    assert sequential >= 5 * 0.05 * 0.95  # 一个接一个：至少 5 次延迟之和
    assert concurrent < sequential  # 宽松：同时发出只等最慢的那一次（机器负载高时也成立）


def _det_llm():
    """确定性的假模型：t2、t3、t5 答对，其余答错；每次调用的延迟不同，完成顺序和发出顺序不一样。"""
    right = {"忘记密码": "password_reset", "打印机找不到": "hardware", "Excel 崩溃": "software"}

    def respond(messages):
        text = messages[-1]["content"]
        for k, v in right.items():
            if k in text:
                return reply(f"理由：{k}\n类别：{v}")
        return reply("理由：不确定\n类别：network")

    return ScriptedLLM(responder=respond, latency=lambda n: 0.03 if n % 2 else 0.005)


async def test_bootstrap_fewshot_concurrent_matches_sequential_bootstrap_demos():
    """bootstrap_fewshot 并发跑训练集再按顺序过滤：结果必须和"一条接一条的 bootstrap_demos"完全一样。"""
    results = []
    for limit in (1, 4):
        llm = _det_llm()
        task = optkit.ProgramTask(optkit.Program(llm, "分类"), _exact, max_concurrency=limit)
        res = await optkit.bootstrap_fewshot(task, TRAIN, TRAIN, max_demos=2, num_candidates=2, seed=0)
        results.append(res)
        assert llm.max_in_flight == limit
    assert (results[0].demos, results[0].dev_scores, results[0].log) == (results[1].demos, results[1].dev_scores, results[1].log)
    # 候选 0（朴素 BootstrapFewShot）= 一条接一条调用 bootstrap_demos 的结果
    seq = await optkit.bootstrap_demos(optkit.Program(_det_llm(), "分类"), TRAIN, _exact, max_demos=2)
    assert [d.input for d in seq] == ["忘记密码", "打印机找不到"]
    assert results[1].log[0]["demos"] == seq
