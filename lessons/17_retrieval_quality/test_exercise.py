"""第 17 课练习测试：全部离线、确定，不到 1 秒跑完。

运行：make lesson N=17    或    .venv/bin/python -m pytest lessons/17_retrieval_quality -v
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)


def _kit():
    """按文件路径加载同目录的 retrieval_kit（课程目录名以数字开头，没法普通 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__retrieval_kit"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / "retrieval_kit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def ids(results):
    return [d for d, _ in results]


# =====================================================================
# (a) rrf_fuse
# =====================================================================


def test_rrf_scores_follow_formula():
    out = dict(ex.rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60))
    assert out["a"] == pytest.approx(1 / 61)
    assert out["b"] == pytest.approx(1 / 62 + 1 / 61)  # 名次从 1 开始
    assert out["c"] == pytest.approx(1 / 63)
    assert out["d"] == pytest.approx(1 / 62)
    assert ids(ex.rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60)) == ["b", "a", "d", "c"]


def test_rrf_k_decides_between_consensus_and_single_top_hit():
    # x 只在第一路排第 1；y 在两路都排第 3
    rankings = [["x", "p", "y"], ["q", "r", "y"]]
    at60 = ids(ex.rrf_fuse(rankings, k=60))
    at0 = ids(ex.rrf_fuse(rankings, k=0))
    assert at60.index("y") < at60.index("x")  # k = 60：多路都认可的文档胜出（2/63 > 1/61）
    assert at0.index("x") < at0.index("y")  # k = 0：单路第一名的分量极重（1/1 > 1/3 + 1/3）


def test_rrf_ties_are_deterministic():
    # a、b 分数完全相同（各有一个第 1 名、一个第 2 名），最好名次也一样 → 按 doc_id
    assert ids(ex.rrf_fuse([["b", "a"], ["a", "b"]])) == ["a", "b"]
    assert ids(ex.rrf_fuse([["a", "b"], ["b", "a"]])) == ["a", "b"]
    # 浮点数陷阱：a 依次拿到第 1、7、2 名，b 依次拿到第 1、2、7 名 —— 数学上分数相等，
    # 但按列表顺序累加时，两个浮点数差在最后一位（b 会"大"一点点）。不 round 就会把 b 排在前面。
    rankings = [
        ["a"],
        ["b"],
        ["x1", "b", "x2", "x3", "x4", "x5", "a"],
        ["x6", "a", "x7", "x8", "x9", "x10", "b"],
    ]
    assert ids(ex.rrf_fuse(rankings))[:2] == ["a", "b"]


def test_rrf_tie_break_by_best_rank_before_doc_id():
    # k=1 时：b 两次都是第 2 名 → 1/3 + 1/3；z 一次第 1 名、一次第 5 名 → 1/2 + 1/6。两者相等
    rankings = [["z", "b", "p1"], ["q1", "b", "q2", "q3", "z"]]
    out = ex.rrf_fuse(rankings, k=1)
    scores = dict(out)
    assert scores["b"] == pytest.approx(scores["z"])
    assert ids(out).index("z") < ids(out).index("b")  # z 的最好名次是 1，比 b 的 2 好（尽管 "b" < "z"）


def test_rrf_duplicates_empty_and_zero_weight():
    out = dict(ex.rrf_fuse([["a", "a", "b"]], k=60))
    assert out["a"] == pytest.approx(1 / 61)  # 重复的 a 只算一次
    assert out["b"] == pytest.approx(1 / 62)  # 而且不占名次：b 是第 2 名
    assert ex.rrf_fuse([]) == []
    assert ex.rrf_fuse([[], []]) == []
    # 权重为 0 的列表完全不参与；只在它里面出现的文档不出现在结果里
    fused = ex.rrf_fuse([["a", "b"], ["c", "b"]], weights=[1.0, 0.0])
    assert ids(fused) == ["a", "b"]
    weighted = dict(ex.rrf_fuse([["a"], ["b"]], weights=[2.0, 1.0]))
    assert weighted["a"] == pytest.approx(2 / 61) and weighted["b"] == pytest.approx(1 / 61)


def test_rrf_validates_arguments():
    with pytest.raises(ValueError):
        ex.rrf_fuse([["a"]], k=-1)
    with pytest.raises(ValueError):
        ex.rrf_fuse([["a"], ["b"]], weights=[1.0])
    with pytest.raises(ValueError):
        ex.rrf_fuse([["a"]], weights=[-0.5])


# =====================================================================
# (b) nDCG@k 与 MRR
# =====================================================================


def test_ndcg_perfect_worst_and_exact_value():
    rel = {"a": 2, "b": 1}
    assert ex.ndcg_at_k(["a", "b", "x"], rel, 3) == pytest.approx(1.0)
    assert ex.ndcg_at_k(["x", "y", "z"], rel, 3) == 0.0
    # 顺序颠倒：DCG = 1/log2(2) + 2/log2(3)；IDCG = 2/log2(2) + 1/log2(3)
    expected = (1 + 2 / math.log2(3)) / (2 + 1 / math.log2(3))
    assert ex.ndcg_at_k(["b", "a"], rel, 2) == pytest.approx(expected)


def test_ndcg_counts_missing_relevant_docs_and_cutoff():
    rel = {"a": 1, "b": 1, "c": 1}
    # 只找到 a 且排第 1：IDCG 要按 3 个相关文档算，所以不是 1.0
    idcg = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert ex.ndcg_at_k(["a"], rel, 3) == pytest.approx(1 / idcg)
    # k=1 时理想结果也只有 1 个位置
    assert ex.ndcg_at_k(["a", "x"], rel, 1) == pytest.approx(1.0)
    assert ex.ndcg_at_k(["x", "a"], rel, 1) == 0.0


def test_ndcg_edge_cases():
    assert ex.ndcg_at_k(["a", "b"], {}, 5) == 0.0
    assert ex.ndcg_at_k(["a", "b"], {"a": 0, "b": -1}, 5) == 0.0
    # 重复出现的文档不能拿两次分
    assert ex.ndcg_at_k(["a", "a"], {"a": 1, "b": 1}, 2) == pytest.approx(1 / (1 + 1 / math.log2(3)))
    with pytest.raises(ValueError):
        ex.ndcg_at_k(["a"], {"a": 1}, 0)


def test_mrr_basic_and_cutoff():
    runs = [
        (["a", "b"], {"a": 1}),  # RR = 1
        (["x", "y", "b"], {"b": 2}),  # RR = 1/3
        (["x", "y"], {"b": 1}),  # RR = 0
    ]
    assert ex.mrr(runs) == pytest.approx((1 + 1 / 3 + 0) / 3)
    assert ex.mrr(runs, k=2) == pytest.approx((1 + 0 + 0) / 3)  # 第二个查询的相关文档在第 3 名，被截掉
    # 相关度为 0 的不算相关
    assert ex.mrr([(["a", "b"], {"a": 0, "b": 1})]) == pytest.approx(0.5)


def test_mrr_validates_arguments():
    with pytest.raises(ValueError):
        ex.mrr([])
    with pytest.raises(ValueError):
        ex.mrr([(["a"], {"a": 1})], k=0)


# =====================================================================
# (c) hybrid_search
# =====================================================================


def fixed(results):
    """造一个假检索器，并记录它被调用时收到的参数。"""
    calls = []

    def fn(query, n):
        calls.append((query, n))
        return list(results)[:n]

    fn.calls = calls
    return fn


def test_hybrid_overlap_is_merged_and_ranked_first():
    bm25 = fixed([("a", 9.0), ("b", 5.0), ("c", 1.0)])
    vec = fixed([("d", 0.9), ("b", 0.8), ("e", 0.1)])
    out = ex.hybrid_search("q", bm25, vec, k=5)
    assert ids(out)[0] == "b"  # 两路都排第 2，胜过只在一路排第 1 的
    assert ids(out).count("b") == 1
    assert dict(out)["b"] == pytest.approx(2 / 62)
    assert len(out) == 5


def test_hybrid_handles_empty_results():
    vec = fixed([("d", 0.9), ("b", 0.8)])
    assert ids(ex.hybrid_search("q", fixed([]), vec, k=5)) == ["d", "b"]
    assert ids(ex.hybrid_search("q", lambda q, n: None, vec, k=5)) == ["d", "b"]
    assert ex.hybrid_search("q", fixed([]), fixed([]), k=5) == []


def test_hybrid_is_deterministic_with_unsorted_and_tied_scores():
    # 检索器返回没排序、有并列、有重复的"脏"结果
    bm25 = fixed([("c", 1.0), ("b", 3.0), ("a", 3.0), ("c", 2.0)])
    vec = fixed([])
    out = ex.hybrid_search("q", bm25, vec, k=3)
    # 整理后的 BM25 排名：a(3.0) = b(3.0) 并列按 id → a, b；c 只保留最高分 2.0 → 第 3
    assert ids(out) == ["a", "b", "c"]
    assert dict(out)["c"] == pytest.approx(1 / 63)
    shuffled = fixed([("a", 3.0), ("c", 2.0), ("b", 3.0), ("c", 1.0)])
    assert ex.hybrid_search("q", shuffled, vec, k=3) == out


def test_hybrid_fetch_k_weights_and_validation():
    bm25 = fixed([(f"b{i:02d}", 30.0 - i) for i in range(30)])
    vec = fixed([(f"v{i:02d}", 1.0 - i / 100) for i in range(30)])
    out = ex.hybrid_search("q", bm25, vec, k=3)
    assert bm25.calls == [("q", 20)] and vec.calls == [("q", 20)]  # 默认 fetch_k = max(4k, 20)
    assert len(out) == 3
    ex.hybrid_search("q", bm25, vec, k=10, fetch_k=7)
    assert bm25.calls[-1] == ("q", 7)
    only_bm25 = ex.hybrid_search("q", bm25, vec, k=4, weights=(1.0, 0.0))
    assert ids(only_bm25) == ["b00", "b01", "b02", "b03"]
    with pytest.raises(ValueError):
        ex.hybrid_search("q", bm25, vec, k=0)


def test_hybrid_on_the_lesson_corpus_combines_both_strengths():
    kit = _kit()
    docs = kit.load_corpus()
    items = [(d.id, d.text) for d in docs]
    bm25 = kit.BM25().add(items)
    vec, _ = kit.teaching_vector_index(items)
    # 同义改写：BM25 一条都搜不到（文档里只有"口令"没有"密码"），向量能搜到
    assert bm25.search("登录密码忘了怎么办", 5) == []
    assert "it-password-reset" in ids(ex.hybrid_search("登录密码忘了怎么办", bm25.search, vec.search, k=3))
    # 型号精确匹配：Gen 12 必须排在几乎一模一样的 Gen 11 前面
    top = ids(ex.hybrid_search("X1 Carbon Gen 12 保修几年", bm25.search, vec.search, k=3))
    assert top.index("it-laptop-x1g12") < top.index("it-laptop-x1g11")


# =====================================================================
# 课程工具（不是练习）：pointwise 重排的并发是真的，而且有上限
# =====================================================================


async def test_pointwise_rerank_runs_judgments_concurrently_up_to_the_cap():
    from agentkit import ScriptedLLM, reply

    kit = _kit()

    def judge(messages):  # 候选片段里写着自己该得几分
        text = messages[-1]["content"]
        return reply('{"score": %s}' % text.split("候选片段：分数")[1][0])

    cands = [(f"d{i}", f"分数{s} 片段 {i}") for i, s in enumerate([1, 3, 0, 3, 2, 1])]
    for cap in (1, 2, 4):
        inner = ScriptedLLM(responder=judge, latency=0.02)  # 每次调用都真的要等：并发的调用才会重叠
        llm = kit.MeteredLLM(inner)
        order = await kit.llm_rerank_pointwise(llm, "q", cands, max_concurrency=cap)
        assert order == ["d1", "d3", "d4", "d0", "d5", "d2"]  # 按分数降序，同分保持原顺序
        assert llm.calls == inner.call_count == 6  # 每个候选恰好一次调用
        assert inner.max_in_flight == llm.max_in_flight == cap  # 同时在路上的调用数 = 上限，不多也不少
        assert llm.in_flight == 0
