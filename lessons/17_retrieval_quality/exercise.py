"""第 17 课练习：融合、评估指标、混合检索。

三道题，全部是纯函数，不需要模型、不需要网络：

    (a) rrf_fuse(rankings, k=60, weights=None)         倒数排名融合（RRF）
    (b) ndcg_at_k(ranked_ids, relevance, k)            nDCG@k
        mrr(runs, k=None)                              MRR（平均倒数排名）
    (c) hybrid_search(query, bm25_fn, vector_fn, k=5)  BM25 + 向量的混合检索

验证：
    make lesson N=17
    AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/17_retrieval_quality -v   # 对照参考答案

做完之后重新运行 demo.py，输出开头会显示"练习实现：exercise.py（你的实现）"。
"""

from __future__ import annotations

import math  # noqa: F401  （(b) 会用到 math.log2）
from typing import Callable, Mapping, Sequence

# 一路检索器：输入 (查询, 最多返回几条)，输出 [(doc_id, 分数), ...]
Retriever = Callable[[str, int], Sequence[tuple[str, float]]]


# =====================================================================
# (a) rrf_fuse：倒数排名融合（Reciprocal Rank Fusion）
# =====================================================================


def rrf_fuse(
    rankings: Sequence[Sequence[str]],
    k: float = 60,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """把几路检索的排名列表合并成一个，返回 [(doc_id, 融合分数), ...]，分数从高到低。

    公式（Cormack 等，SIGIR 2009）：
        score(d) = Σ_i  w_i / (k + rank_i(d))
    - rank_i(d) 是 d 在第 i 个列表里的名次，**从 1 开始**；d 不在第 i 个列表里就不加分；
    - w_i 是第 i 个列表的权重，weights=None 表示全部为 1.0；
    - 只用名次、不用原始分数：BM25 的 12.7 分和余弦的 0.83 根本不是一个量纲，没法直接相加。

    边界与确定性（测试会逐条检查）：
    1. 同一个列表里 doc_id 重复出现：只按**第一次**出现的名次算，后面的忽略（也不占名次）；
    2. 权重为 0 的列表完全不参与：不加分、不参与第 3 条的"最好名次"；只出现在这种列表里的文档不出现在结果里；
    3. 并列时的顺序：先比融合分数（大的在前），再比"在各个参与列表里的最好名次"（小的在前），最后比 doc_id（字典序小的在前）；
    4. rankings 为空、或者全是空列表：返回 []；
    5. k < 0、weights 个数和 rankings 不一致、weights 里有负数：抛 ValueError。

    提示：
    - 用两个 dict 记录每个文档的"累计分数"和"最好名次"，最后一次 sorted 搞定；
    - 浮点数相加的顺序不同，结果可能差在最后一位（1/61 + 1/63 + 1/65 换个顺序加就可能不相等）。
      排序时用 round(score, 12) 比较，才能让数学上相等的分数真正并列。
    """
    raise NotImplementedError("TODO (a): 实现 RRF 融合")


# =====================================================================
# (b) nDCG@k 与 MRR
# =====================================================================


def ndcg_at_k(ranked_ids: Sequence[str], relevance: Mapping[str, float], k: int) -> float:
    """nDCG@k（归一化折损累计增益）：既看"找没找到"，也看"排得靠不靠前"和"找到的有多相关"。

        DCG@k  = Σ_{i=1..k}  gain(第 i 名) / log2(i + 1)
        IDCG@k = 把所有相关文档按相关度从高到低"理想地"排好之后的 DCG@k
        nDCG@k = DCG@k / IDCG@k            （结果在 0 到 1 之间）

    - gain 用线性增益：gain = relevance.get(doc_id, 0)，相关度 <= 0 的算 0（和 trec_eval、sklearn 一致）；
    - 第 1 名的折扣是 log2(2) = 1，不打折；第 3 名的折扣是 log2(4) = 2，只算一半；
    - IDCG 用 relevance 里**所有**相关度 > 0 的文档来算（不只是检索到的那些）——
      漏掉的相关文档要体现为扣分。

    边界：
    1. relevance 里没有任何相关度 > 0 的文档：返回 0.0（约定；有的工具会跳过这种查询）；
    2. ranked_ids 里同一个 doc_id 重复出现：只有第一次能拿分，后面的占着位置但 gain 为 0；
    3. ranked_ids 比 k 短：照常计算；
    4. k <= 0：抛 ValueError。
    """
    raise NotImplementedError("TODO (b): 实现 nDCG@k")


def mrr(runs: Sequence[tuple[Sequence[str], Mapping[str, float]]], k: int | None = None) -> float:
    """MRR（Mean Reciprocal Rank，平均倒数排名）："第一个相关结果排在第几"的倒数，再对所有查询取平均。

    runs 是 [(ranked_ids, relevance), ...]，每个元素对应一个查询。
    - 一个查询的倒数排名 RR = 1 / 第一个相关文档（相关度 > 0）的名次（名次从 1 开始）；
    - k 不为 None 时只看前 k 个结果，前 k 个里没有相关文档则 RR = 0；k 为 None 表示看全部；
    - MRR = 所有查询的 RR 的平均值。

    边界：runs 为空抛 ValueError（没有查询就没有 MRR，返回 0 会被误读成"检索全错"）；
    k 不为 None 且 k <= 0 也抛 ValueError。
    """
    raise NotImplementedError("TODO (b): 实现 MRR")


# =====================================================================
# (c) hybrid_search：BM25 + 向量，用加权 RRF 合并
# =====================================================================


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
    """混合检索：两路各取 fetch_k 个候选 → 各自整理成确定的排名 → 加权 RRF 融合 → 取前 k 个。

    参数：
    - bm25_fn(query, n) / vector_fn(query, n)：各返回最多 n 个 (doc_id, 分数)；
    - fetch_k：每一路取多少个候选，None 时取 max(4 * k, 20)。
      为什么要多取？融合后排进前 k 的文档，在单路里可能只排第 12 名；只取 k 个就永远见不到它；
    - weights：(BM25 的权重, 向量的权重)，直接传给 rrf_fuse；
    - 返回 [(doc_id, 融合分数), ...]，最多 k 个。

    必须处理的情况（测试会逐条检查）：
    1. **重叠**：同一个文档两路都找到了，结果里只出现一次，分数是两路之和；
    2. **空结果**：某一路返回 [] 或 None，结果就是另一路的排名；两路都空返回 []；
    3. **并列分数**：检索器返回的列表不一定排好序，也可能有分数完全相同的文档。
       先把每一路整理成确定的排名：同一个 doc_id 出现多次只保留分数最高的一次；
       按分数从高到低排，分数相同按 doc_id 字典序。这样同样的输入永远得到同样的输出；
    4. k <= 0：抛 ValueError。

    提示：直接复用 (a) 的 rrf_fuse，本题主要是把"脏"的检索结果整理干净。
    """
    raise NotImplementedError("TODO (c): 实现混合检索")
