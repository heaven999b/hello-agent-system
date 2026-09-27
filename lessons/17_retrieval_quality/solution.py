"""第 17 课练习参考答案。先自己做，再来对照。"""

from __future__ import annotations

import math
from typing import Callable, Mapping, Sequence

Retriever = Callable[[str, int], Sequence[tuple[str, float]]]


# =====================================================================
# (a) rrf_fuse：倒数排名融合
# =====================================================================


def rrf_fuse(
    rankings: Sequence[Sequence[str]],
    k: float = 60,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    if k < 0:
        raise ValueError("k 不能为负数")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights 的个数必须和 rankings 一致")
    if any(w < 0 for w in weights):
        raise ValueError("weights 不能为负数")

    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for ranking, w in zip(rankings, weights):
        if w == 0:
            continue  # 权重为 0 的列表不贡献分数，也不参与"最好名次"的比较
        seen: set[str] = set()
        rank = 0
        for doc_id in ranking:
            if doc_id in seen:
                continue  # 同一个列表里重复出现：只按第一次出现的名次算
            seen.add(doc_id)
            rank += 1
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
            best_rank[doc_id] = min(best_rank.get(doc_id, rank), rank)

    # 浮点数相加的顺序不同，结果可能差最后一位：比较前先 round，避免"数学上相等"的并列被随机打破
    ordered = sorted(scores, key=lambda d: (-round(scores[d], 12), best_rank[d], d))
    return [(d, scores[d]) for d in ordered]


# =====================================================================
# (b) nDCG@k 与 MRR
# =====================================================================


def ndcg_at_k(ranked_ids: Sequence[str], relevance: Mapping[str, float], k: int) -> float:
    if k <= 0:
        raise ValueError("k 必须是正整数")
    ideal = sorted((r for r in relevance.values() if r > 0), reverse=True)[:k]
    if not ideal:
        return 0.0
    dcg = 0.0
    seen: set[str] = set()
    for i, doc_id in enumerate(ranked_ids[:k]):
        if doc_id in seen:
            continue  # 重复出现的文档不能再拿一次分（但仍然占着这个位置）
        seen.add(doc_id)
        gain = relevance.get(doc_id, 0)
        if gain > 0:
            dcg += gain / math.log2(i + 2)  # 第 1 名（i=0）的折扣是 log2(2) = 1
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg


def mrr(runs: Sequence[tuple[Sequence[str], Mapping[str, float]]], k: int | None = None) -> float:
    if k is not None and k <= 0:
        raise ValueError("k 必须是正整数或 None")
    if not runs:
        raise ValueError("runs 为空：没有查询就没有 MRR（返回 0 会被误读成'检索全错'）")
    total = 0.0
    for ranked_ids, relevance in runs:
        top = ranked_ids if k is None else ranked_ids[:k]
        for i, doc_id in enumerate(top):
            if relevance.get(doc_id, 0) > 0:
                total += 1.0 / (i + 1)
                break
    return total / len(runs)


# =====================================================================
# (c) hybrid_search：BM25 + 向量，用加权 RRF 合并
# =====================================================================


def _as_ranking(results: Sequence[tuple[str, float]]) -> list[str]:
    """把一路检索结果整理成确定的排名：同一 doc 只留最高分；按分数降序、分数相同按 doc_id 升序。"""
    best: dict[str, float] = {}
    for doc_id, score in results:
        if doc_id not in best or score > best[doc_id]:
            best[doc_id] = score
    return sorted(best, key=lambda d: (-best[d], d))


def hybrid_search(
    query: str,
    bm25_fn: Retriever,
    vector_fn: Retriever,
    k: int = 5,
    *,
    rrf_k: float = 60,
    weights: tuple[float, float] = (1.0, 1.0),
    fetch_k: int | None = None,
) -> list[tuple[str, float]]:
    if k <= 0:
        raise ValueError("k 必须是正整数")
    n = fetch_k if fetch_k is not None else max(4 * k, 20)
    rankings = [_as_ranking(bm25_fn(query, n) or []), _as_ranking(vector_fn(query, n) or [])]
    return rrf_fuse(rankings, k=rrf_k, weights=weights)[:k]
