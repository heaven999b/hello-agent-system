"""第 17 课 Demo：检索质量 —— 向量检索、混合检索与重排。

    python lessons/17_retrieval_quality/demo.py             # 真实模型（读取 .env，44 次调用，2 路并发，约 3 分钟）
    python lessons/17_retrieval_quality/demo.py --offline   # 离线：回放录制好的真实模型输出，无需 API key
    python lessons/17_retrieval_quality/demo.py --record    # 真实模型，并把模型输出录制到 recorded_llm.json

八个场景（只有 3、5、6 调用模型）：
  1. 评估集：47 条企业制度片段 + 20 条带相关度标注的查询
  2. embedding 的直觉：余弦相似度能看出什么、看不出什么
  3. 主对比：BM25 / 向量 / 混合（RRF）/ 混合 + LLM 重排 —— Recall@5、MRR、nDCG、耗时、成本
  4. 逐题复盘：每种方法在哪类查询上赢、为什么
  5. 重排的两种形态：listwise（一次排整组）vs pointwise（逐条打分）的成本
  6. 查询改写：多查询和 HyDE 能不能救回 BM25 的"零结果"和错误结果
  7. 暴力检索 vs IVF 近似检索：召回率 - 延迟权衡
  8. 切块 × 召回：块大小的"倒 U 形"曲线

融合（rrf_fuse）、指标（ndcg_at_k / mrr）、混合检索（hybrid_search）来自练习：
你完成 exercise.py 之后，Demo 会自动换成你的实现。
如果 .venv 里装了 sentence-transformers，场景 3 会多出"真实 embedding"的对比行（可用 --no-st 关闭）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from agentkit import ResilientLLM, default_llm
from agentkit.types import LLMResponse, Message, Usage

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import retrieval_kit as rk  # noqa: E402

RECORDING = HERE / "recorded_llm.json"
TOP_K = 5  # 最终交给模型的条数
CANDIDATES = 10  # 重排前召回多少条

# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 76 + f"\n  {title}\n" + "═" * 76, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, w: int, right: bool = False) -> str:
    fill = " " * max(0, w - width(s))
    return fill + s if right else s + fill


def table(headers: list[str], rows: list[list[str]], right_from: int = 1) -> None:
    ws = [max(width(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    fmt = lambda r: "  ".join(pad(str(c), ws[i], right=i >= right_from) for i, c in enumerate(r))  # noqa: E731
    info(fmt(headers))
    info("  ".join("─" * w for w in ws))
    for r in rows:
        info(fmt(r))


def load_impl():
    """优先用你在 exercise.py 里的实现；还没写完就用参考答案。"""
    import exercise  # noqa: E402

    try:
        exercise.rrf_fuse([["a"]])
        exercise.ndcg_at_k(["a"], {"a": 1}, 1)
        exercise.mrr([(["a"], {"a": 1})])
        exercise.hybrid_search("q", lambda q, n: [("a", 1.0)], lambda q, n: [], k=1)
        return exercise, "exercise.py（你的实现）"
    except NotImplementedError:
        import solution  # noqa: E402

        return solution, "solution.py（参考答案 —— 完成练习后会自动换成你的实现）"


IMPL, IMPL_NAME = load_impl()

# ---------------------------------------------------------------- 录制与回放（离线模式的"剧本"）


def prompt_key(messages: list[Message]) -> str:
    raw = json.dumps([(m.get("role"), m.get("content")) for m in messages], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class RecordingLLM:
    """真实模型外面再套一层：把每次调用的提示词指纹、输出、用量、耗时记下来，供离线回放。"""

    def __init__(self, inner):
        self.inner = inner
        self.model = inner.model
        self.responses: dict[str, dict] = {}
        self._lock = threading.Lock()

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        t0 = time.perf_counter()
        resp = self.inner.chat(messages, tools, **kwargs)
        u = resp.usage
        with self._lock:
            self.responses[prompt_key(messages)] = {
                "content": resp.content,
                "usage": [u.input_tokens, u.output_tokens, u.cached_input_tokens, u.reasoning_tokens],
                "seconds": round(time.perf_counter() - t0, 2),
            }
        return resp

    def save(self, path: Path) -> None:
        data = {
            "model": self.model,
            "recorded_at": date.today().isoformat(),
            "note": "由 demo.py --record 生成：真实模型的输出，供 --offline 回放。",
            "responses": self.responses,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def fallback_output(messages: list[Message]) -> str:
    """回放时查不到录制（比如你的 hybrid_search 给出的候选和录制时不同）：给一个确定的"什么都不改"的输出。"""
    text = messages[-1].get("content") or ""
    if "个候选片段" in text:  # listwise：原样返回候选顺序 = 不重排
        return json.dumps({"ranking": re.findall(r"^\[(D\d+)\]", text, re.M)})
    if "候选片段：" in text:  # pointwise：一律 1 分 = 保持原顺序
        return json.dumps({"score": 1})
    m = re.search(r"搜索：(.*)", text)
    if m:  # 多查询：只返回原查询
        return json.dumps({"queries": [m.group(1).strip()]}, ensure_ascii=False)
    m = re.search(r"员工问：(.*)", text)
    return m.group(1).strip() if m else ""  # HyDE：用原问题代替假文档


class ReplayLLM:
    """离线模式的模型：按提示词指纹回放录制的真实输出（确定性）；查不到就走 fallback_output。"""

    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.model = data.get("model", "replay")
        self.recorded_at = data.get("recorded_at", "未录制")
        self.responses: dict = data.get("responses", {})
        self.hits = self.misses = 0
        self.recorded_seconds = 0.0  # 回放的这些调用，录制时真实花了多少秒

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        rec = self.responses.get(prompt_key(messages))
        if rec is None:
            self.misses += 1
            return LLMResponse(content=fallback_output(messages), usage=Usage(), model=self.model)
        self.hits += 1
        self.recorded_seconds += rec["seconds"]
        return LLMResponse(content=rec["content"], usage=Usage(*rec["usage"]), model=self.model)


# ---------------------------------------------------------------- 评估框架


@dataclass
class Row:
    name: str
    ranked: dict[str, list[str]]  # 查询 id → 排好序的 doc_id
    ms_per_query: float
    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0


def run_retriever(
    name: str,
    fn: Callable[[rk.Query], list[str]],
    queries: list[rk.Query],
    llm: rk.MeteredLLM | None = None,
    workers: int = 1,
    extra_seconds: Callable[[], float] | None = None,
) -> Row:
    before = llm.snapshot() if llm else None

    def one(q: rk.Query):
        extra0 = extra_seconds() if extra_seconds else 0.0
        t0 = time.perf_counter()
        ranked = fn(q)
        dt = time.perf_counter() - t0 + ((extra_seconds() - extra0) if extra_seconds else 0.0)
        return q.id, ranked, dt

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, queries))
    else:
        results = [one(q) for q in queries]
    row = Row(name, {qid: r for qid, r, _ in results}, 1000 * sum(dt for *_, dt in results) / len(results))
    if llm:
        calls, usage, _ = llm.snapshot()
        row.calls = calls - before[0]
        row.usage = Usage(
            usage.input_tokens - before[1].input_tokens,
            usage.output_tokens - before[1].output_tokens,
            usage.cached_input_tokens - before[1].cached_input_tokens,
            usage.reasoning_tokens - before[1].reasoning_tokens,
        )
        row.cost = llm.cost_usd(row.usage)
    return row


def metrics(row: Row, queries: list[rk.Query]) -> tuple[float, float, float]:
    r5 = sum(rk.recall_at_k(row.ranked[q.id], q.relevance, TOP_K) for q in queries) / len(queries)
    m = IMPL.mrr([(row.ranked[q.id], q.relevance) for q in queries], k=CANDIDATES)
    nd = sum(IMPL.ndcg_at_k(row.ranked[q.id], q.relevance, TOP_K) for q in queries) / len(queries)
    return r5, m, nd


def fmt_ms(ms: float) -> str:
    return f"{ms:.2f} ms" if ms < 100 else f"{ms / 1000:.1f} s"


def marks(ranked: list[str], q: rk.Query, n: int = 3) -> str:
    out = []
    for d in ranked[:n]:
        r = q.relevance.get(d, 0)
        out.append(d + ("✓" if r >= 2 else "½" if r == 1 else ""))
    return "  ".join(out) or "（无结果）"


# ---------------------------------------------------------------- 主流程


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="回放 recorded_llm.json，无需 API key")
    ap.add_argument("--record", action="store_true", help="真实模式下把模型输出录制到 recorded_llm.json")
    ap.add_argument("--model", default=None, help="覆盖 .env 里的 LLM_MODEL")
    ap.add_argument("--no-st", action="store_true", help="即使装了 sentence-transformers 也不用")
    args = ap.parse_args()

    replay: ReplayLLM | None = None
    recorder: RecordingLLM | None = None
    if args.offline:
        replay = ReplayLLM(RECORDING)
        llm = rk.MeteredLLM(replay)
        workers = 1
        mode = f"离线（回放 {RECORDING.name}：{replay.model}，录制于 {replay.recorded_at}）"
    else:
        base = ResilientLLM(default_llm(args.model))
        if args.record:
            recorder = RecordingLLM(base)
            base = recorder
        llm = rk.MeteredLLM(base)
        workers = 2  # 共享网关，并发不超过 2
        mode = f"真实模型（{llm.model}）"
    extra = (lambda: replay.recorded_seconds) if replay else None

    print(f"模式：{mode}")
    print(f"练习实现：{IMPL_NAME}")

    docs = rk.load_corpus()
    queries = rk.load_queries()
    by_id = {d.id: d for d in docs}
    items = [(d.id, d.text) for d in docs]

    # ============================================================ 1
    banner("场景 1：评估集长什么样")
    cats: dict[str, int] = {}
    for q in queries:
        cats[q.category] = cats.get(q.category, 0) + 1
    info(f"语料：{len(docs)} 条制度片段，来自 {len({d.doc for d in docs})} 份文档（IT / 人事 / 财务 / 行政 / 安全，内容均为虚构）")
    info(f"查询：{len(queries)} 条，按难点分类：" + "、".join(f"{c} {n}" for c, n in cats.items()))
    info("一条标注长这样（data/eval_queries.jsonl）：")
    q14 = next(q for q in queries if q.id == "q14")
    info("  " + json.dumps({"id": q14.id, "query": q14.query, "category": q14.category, "relevance": q14.relevance},
                           ensure_ascii=False))
    info(f"  难点：{q14.note}")
    takeaway("相关度分级：2 = 能直接回答，1 = 部分相关，没列出的都算 0。难例（同义、型号、否定）是故意放进去的 —— "
             "只有'好搜'的查询，任何检索方法看起来都一样好。")

    # ============================================================ 2
    banner("场景 2：embedding 的直觉 —— 余弦相似度能看出什么、看不出什么")
    bm25 = rk.BM25().add(items)
    vec, emb = rk.teaching_vector_index(items)
    lex_emb = rk.TeachingEmbedder(concept_weight=0.0).fit([t for _, t in items])
    info(f"教学 embedding：每段文字 → {emb.size} 维、长度为 1 的向量（{emb.dim} 维字面特征 + {len(emb.concepts)} 维概念特征）")
    cases = [
        ("登录密码忘了怎么办", ["it-password-reset", "it-password-policy"]),
        ("X1 Carbon Gen 12 保修几年", ["it-laptop-x1g12", "it-laptop-x1g11"]),
        ("没有发票还能报销吗", ["fin-no-invoice", "fin-invoice"]),
    ]
    rows = []
    for text, ids in cases:
        bs = dict(bm25.search(text, 100))
        for i, d in enumerate(ids):
            rows.append([
                text if i == 0 else "",
                d,
                f"{rk.dot(lex_emb.embed(text), lex_emb.embed(by_id[d].text)):.3f}",
                f"{rk.dot(emb.embed(text), emb.embed(by_id[d].text)):.3f}",
                f"{bs.get(d, 0.0):.2f}",
            ])
    table(["查询", "文档", "余弦(只有字面)", "余弦(+概念)", "BM25"], rows, right_from=2)
    info("")
    info(f"'可以报销' vs '不能报销' 的余弦相似度：{rk.dot(emb.embed('可以报销'), emb.embed('不能报销')):.3f}")
    takeaway("三个现象：① 文档里只有'口令'没有'密码'，BM25 得 0 分，向量靠概念维度把它找回来；"
             "② Gen 12 和 Gen 11 只差一个数字，向量相似度几乎一样，BM25 的分差明显得多；"
             "③ '没有发票'的问题，向量反而更像讲'发票要求'的那篇 —— 向量不懂否定。")

    # ============================================================ 3
    banner("场景 3：主对比 —— BM25 / 向量 / 混合（RRF）/ 混合 + LLM 重排")
    lex_vec = rk.VectorIndex(lex_emb.embed_many, lex_emb.embed).add(items)

    def hybrid_ids(q: rk.Query, vector_search=vec.search) -> list[str]:
        return [d for d, _ in IMPL.hybrid_search(q.query, bm25.search, vector_search, k=CANDIDATES)]

    def rerank_ids(q: rk.Query) -> list[str]:
        cands = hybrid_ids(q)
        return rk.llm_rerank_listwise(llm, q.query, [(d, by_id[d].text) for d in cands])

    step("在 20 条查询上逐一检索（BM25 和向量是毫秒级；LLM 重排每条查询调用一次模型）")
    result_rows = [
        run_retriever("BM25（稀疏）", lambda q: [d for d, _ in bm25.search(q.query, CANDIDATES)], queries),
        run_retriever("向量·只有字面（对照）", lambda q: [d for d, _ in lex_vec.search(q.query, CANDIDATES)], queries),
        run_retriever("向量·教学 embedding", lambda q: [d for d, _ in vec.search(q.query, CANDIDATES)], queries),
        run_retriever("混合 RRF（BM25+向量）", hybrid_ids, queries),
    ]
    st_model = os.environ.get("RETRIEVAL_ST_MODEL", rk.DEFAULT_ST_MODEL)
    st_index, st_failed = None, False
    if not args.no_st:
        try:
            st_index = rk.sentence_transformer_index(items, st_model)
        except Exception as e:  # 装了库但模型下载失败（没网、被墙）等：跳过，不影响其余场景
            st_failed = True
            info(f"（sentence-transformers 已安装，但加载 {st_model} 失败，跳过真实 embedding 对比：{type(e).__name__}: {e}）")
    if st_index is not None:
        result_rows += [
            run_retriever(f"向量·{st_model.split('/')[-1]}", lambda q: [d for d, _ in st_index.search(q.query, CANDIDATES)], queries),
            run_retriever("混合 RRF（BM25+真实向量）", lambda q: hybrid_ids(q, st_index.search), queries),
        ]
    t0 = time.perf_counter()
    rerank_row = run_retriever("混合 + LLM 重排", rerank_ids, queries, llm=llm, workers=workers, extra_seconds=extra)
    wall = time.perf_counter() - t0
    result_rows.append(rerank_row)

    rows = []
    for r in result_rows:
        r5, m, nd = metrics(r, queries)
        rows.append([r.name, f"{r5:.3f}", f"{m:.3f}", f"{nd:.3f}", fmt_ms(r.ms_per_query), str(r.calls),
                     f"{r.usage.total:,}", f"${r.cost:.4f}"])
    table(["方法", "Recall@5", "MRR@10", "nDCG@5", "平均延迟/查询", "模型调用", "token", "估算成本"], rows)
    if st_index is None and not args.no_st and not st_failed:
        info("")
        info("（未检测到 sentence-transformers，跳过真实 embedding 对比。想看的话：.venv/bin/pip install sentence-transformers —— "
             f"会装上 PyTorch，体积较大；首次运行会下载 {st_model}。）")
    if replay:
        info("")
        info(f"（离线回放：命中录制 {replay.hits} 次、未命中 {replay.misses} 次；未命中的查询按原顺序返回，等于没重排。"
             "延迟一列用的是录制时的真实耗时。）")
    else:
        info("")
        info(f"（重排 2 路并发，20 条查询总墙钟时间 {wall:.0f} 秒；成本按 agentkit/pricing.py 的占位价格估算。）")

    step("按查询类别拆开看（Recall@5 / MRR@10）")
    main_rows = [result_rows[0], result_rows[2], result_rows[3], rerank_row]
    rows = []
    for cat in cats:
        qs = [q for q in queries if q.category == cat]
        cells = [f"{cat}（{len(qs)}）"]
        for r in main_rows:
            r5 = sum(rk.recall_at_k(r.ranked[q.id], q.relevance, TOP_K) for q in qs) / len(qs)
            m = IMPL.mrr([(r.ranked[q.id], q.relevance) for q in qs], k=CANDIDATES)
            cells.append(f"{r5:.2f} / {m:.2f}")
        rows.append(cells)
    table(["类别", "BM25", "向量", "混合 RRF", "混合+重排"], rows)
    takeaway("先看 Recall@5（'找没找到'），再看 MRR / nDCG（'排得靠不靠前'）。重排不增加召回的上限 —— "
             "它只能在混合检索给的 10 个候选里重新排序；召回阶段漏掉的，重排救不回来。")

    # ============================================================ 4
    banner("场景 4：逐题复盘（✓ = 能直接回答，½ = 部分相关）")
    by_name = {r.name: r for r in main_rows}
    for qid in ["q02", "q07", "q12", "q14", "q06"]:
        q = next(x for x in queries if x.id == qid)
        step(f"{q.id}［{q.category}］{q.query}")
        info(f"难点：{q.note}")
        for name, r in by_name.items():
            info(f"{pad(name, 24)}{marks(r.ranked[q.id], q)}")
    q06 = next(x for x in queries if x.id == "q06")
    hyb06 = by_name["混合 RRF（BM25+向量）"].ranked["q06"]
    vec06 = by_name["向量·教学 embedding"].ranked["q06"]
    info("")
    info(f"q06 的部分相关文档 hr-remote：向量排第 {vec06.index('hr-remote') + 1} 名；"
         f"融合后{'排第 %d 名' % (hyb06.index('hr-remote') + 1) if 'hr-remote' in hyb06 else '掉出了前 10'}。")
    info(f"BM25 对 q06 返回的是：{'、'.join(d for d, _ in bm25.search(q06.query, 5))} …（只因为都含'公司''系统'之类的字）")
    takeaway("q06 是 RRF 的一个坑：向量检索永远'有结果'（47 篇全都有个相似度），BM25 的噪声文档在向量列表的长尾里也能找到，"
             "两路各拿一点小分，加起来就压过了只在一路排第 2 的好文档。对策：给向量结果设相似度下限、缩小 fetch_k、"
             "调低噪声大的那一路的权重 —— 然后回到评估集上验证。")

    # ============================================================ 5
    banner("场景 5：重排的两种形态 —— listwise vs pointwise")
    sub = [q for q in queries if q.id in ("q13", "q14")]
    n_cand = 8
    step(f"同样 2 条查询、各 {n_cand} 个候选：listwise 每条查询 1 次调用；pointwise 每个候选 1 次调用")
    rows = []
    for name, fn in [
        ("listwise", lambda q: rk.llm_rerank_listwise(llm, q.query, [(d, by_id[d].text) for d in hybrid_ids(q)[:n_cand]])),
        ("pointwise", lambda q: rk.llm_rerank_pointwise(
            llm, q.query, [(d, by_id[d].text) for d in hybrid_ids(q)[:n_cand]], max_workers=workers)),
    ]:
        r = run_retriever(name, fn, sub, llm=llm, extra_seconds=extra)
        _, m, nd = metrics(r, sub)
        rows.append([name, str(r.calls), f"{r.usage.total:,}", fmt_ms(r.ms_per_query), f"{m:.2f}", f"{nd:.2f}",
                     " | ".join(marks(r.ranked[q.id], q, 2) for q in sub)])
    table(["形态", "调用", "token", "延迟/查询", "MRR", "nDCG@5", "前 2 名（q13 | q14）"], rows)
    if replay:
        info("")
        info(f"（离线回放把录制的每次调用耗时串行相加；真实运行时 pointwise 的 {n_cand} 次调用是 2 路并发的，实际延迟更短。）")
    takeaway("pointwise 的每次调用只看一个片段，没法'比较'，只能给 0～3 的绝对分 —— 两个片段都得 3 分时，"
             f"只能退回原来的顺序；调用次数是候选数的 {n_cand} 倍。listwise 一次看全部，便宜，"
             "但候选一多就会受位置偏差和上下文长度影响。")

    # ============================================================ 6
    banner("场景 6：查询改写 —— 多查询与 HyDE 能不能救回 BM25")
    step("这 3 条查询，BM25 要么一条结果都没有，要么第一名是错的（文档里没有查询里的词）")
    rows = []
    for qid in ["q02", "q03", "q05"]:
        q = next(x for x in queries if x.id == qid)
        variants = rk.multi_query(llm, q.query, n=3)
        mq = IMPL.rrf_fuse([[d for d, _ in bm25.search(v, CANDIDATES)] for v in variants])
        fake = rk.hyde(llm, q.query)
        hy = [d for d, _ in bm25.search(fake, CANDIDATES)]
        rows.append([q.query, marks([d for d, _ in bm25.search(q.query, 3)], q, 1),
                     marks([d for d, _ in mq], q, 1), marks(hy, q, 1)])
        info(f"{q.id} 多查询改写：{' / '.join(variants)}")
        info(f"{q.id} HyDE 假文档：{' '.join(fake.split())[:80]}…")
    info("")
    table(["查询", "BM25 原查询 top1", "BM25 + 多查询 top1", "BM25 + HyDE top1"], rows, right_from=99)
    takeaway("改写把口语换成了更像制度文件的说法（'忘了'→'遗失''重置'，'第一天上班'→'入职报到'），BM25 就有词可配了。"
             "但改写模型不知道你们公司的内部叫法（本公司把密码叫'口令'），HyDE 的假文档还会编造细节（天数、流程）——"
             "它们只能用来检索，绝不能当答案。代价是每条查询多 1～2 次模型调用、多几秒延迟，所以常见做法是首轮结果差时才触发。")

    # ============================================================ 7
    banner("场景 7：暴力检索 vs IVF 近似检索 —— 召回率 - 延迟权衡")
    n, dim, n_lists = 3000, 16, 30
    vectors = rk.make_clustered_vectors(n, dim, n_clusters=n_lists, spread=0.35, seed=1)
    qvecs = rk.make_clustered_vectors(50, dim, n_clusters=n_lists, spread=0.35, seed=99)
    ivf = rk.IVFIndex(vectors, n_lists, train_size=1000, seed=1)
    t0 = time.perf_counter()
    truth = [rk.brute_force_knn(vectors, qv, 10) for qv in qvecs]
    brute_ms = 1000 * (time.perf_counter() - t0) / len(qvecs)
    step(f"{n} 个 {dim} 维向量，IVF 分成 {n_lists} 个桶；用暴力检索的结果当标准答案，算 Recall@10")
    rows = [["暴力（精确）", "—", f"{n:,}", "1.000", f"{brute_ms:.2f} ms"]]
    for probe in [1, 2, 4, 8, 16]:
        t0 = time.perf_counter()
        rec, scanned = 0.0, 0
        for qv, tr in zip(qvecs, truth):
            got, cnt = ivf.search(qv, 10, n_probe=probe)
            rec += len(set(got) & set(tr)) / 10
            scanned += cnt
        ms = 1000 * (time.perf_counter() - t0) / len(qvecs)
        rows.append(["IVF", str(probe), f"{scanned // len(qvecs):,}", f"{rec / len(qvecs):.3f}", f"{ms:.2f} ms"])
    table(["方法", "n_probe", "平均比较次数", "Recall@10", "延迟/查询"], rows)
    takeaway("n_probe 是旋钮：搜的桶越多越准、越慢。近似索引的'召回率'指的是'找回了多少个真正的最近邻'，"
             "和场景 3 的'找回了多少个相关文档'是两回事 —— 两种损失会叠加。")

    # ============================================================ 8
    banner("场景 8：切块 × 召回 —— 块大小的倒 U 形曲线")
    units = rk.chunk_units(docs)
    budget = 150
    step(f"把 5 份文档按句子重新切块（块大小从 20 字到 1280 字），用混合检索找答案短语（evidence）。"
         f"上下文预算固定为 {budget} 字（约 3 条制度条目）")

    def chunk_eval(chunks: list, with_heading: bool) -> tuple[float, float, float]:
        chunk_items = [(c.id, c.indexed_text(with_heading)) for c in chunks]
        raw = {c.id: c.text for c in chunks}
        b = rk.BM25().add(chunk_items)
        v, _ = rk.teaching_vector_index(chunk_items)
        r3 = chars = rb = 0.0
        for q in queries:
            ranked = [d for d, _ in IMPL.hybrid_search(q.query, b.search, v.search, k=CANDIDATES)]
            top3 = [raw[d] for d in ranked[:3]]
            r3 += sum(any(e in t for t in top3) for e in q.evidence) / len(q.evidence)
            chars += sum(len(t) for t in top3)
            got, used = [], 0
            for d in ranked:  # 按排名往预算里装，装不下的截断
                if used >= budget:
                    break
                got.append(raw[d][: budget - used])
                used += len(got[-1])
            rb += sum(any(e in t for t in got) for e in q.evidence) / len(q.evidence)
        n_q = len(queries)
        return r3 / n_q, chars / n_q, rb / n_q

    rows = []
    for size in [20, 40, 80, 160, 320, 1280]:
        chunks = rk.chunk_texts(units, size)
        r3, chars, rb = chunk_eval(chunks, False)
        _, _, rb_h = chunk_eval(chunks, True)
        rows.append([f"{size} 字", str(len(chunks)), f"{r3:.2f}", f"{chars:.0f}", f"{rb:.2f}", f"{rb_h:.2f}"])
    natural = [rk.Chunk(d.id, d.doc, [d.section], d.text) for d in docs]
    r3, chars, rb = chunk_eval(natural, False)
    _, _, rb_h = chunk_eval(natural, True)
    rows.append(["按条目（自然边界）", str(len(natural)), f"{r3:.2f}", f"{chars:.0f}", f"{rb:.2f}", f"{rb_h:.2f}"])
    table(["块大小", "块数", "Recall@3", "top3 总字数", f"Recall@{budget}字", "+标题前缀"], rows)
    takeaway("只看 Recall@3 会得出'块越大越好'的错误结论 —— 1280 字的块，前 3 块就是大半个知识库。"
             "固定上下文预算再比，曲线是倒 U 形：块太小，答案被切碎、丢了上下文；块太大，答案被无关内容挤出预算。"
             "按自然边界切（第 15 课）最好；小块加上标题前缀能找回一部分上下文。")

    # ============================================================ 小结
    banner("小结")
    calls, usage, _ = llm.snapshot()
    info(f"本次运行共调用模型 {calls} 次，{usage.total:,} token，估算 ${llm.cost_usd():.4f}（占位价格）。")
    info("检索质量的四个杠杆：召回（稀疏 + 稠密，混合）→ 融合（RRF）→ 精排（cross-encoder / LLM）→ 切块与查询改写。")
    info("每动一个杠杆，都回到评估集上看 Recall@k、MRR、nDCG —— 以及延迟和成本。")
    if recorder:
        recorder.save(RECORDING)
        info(f"已把 {len(recorder.responses)} 条模型输出录制到 {RECORDING.name}，--offline 会回放它们。")


if __name__ == "__main__":
    main()
