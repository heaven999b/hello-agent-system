"""第 21 课：Agent 的数据工具箱（只用标准库 + agentkit，零外部依赖）。

一条数据从生产环境走到评估集，要经过这几步：

    traces.jsonl ──load_runs──► RunRecord（一次运行：输入、工具序列、输出、状态、成本）
        ──attach_feedback / mark_implicit_retries──► 带上显式 👍👎 和隐式"重复提问"信号
        ──scrub──► 脱敏（文本再过一遍 redact_pii，用户 ID 换成 HMAC 假名）
        ──dedupe / cluster──► 去重、按"场景"聚类（工具序列签名 + 输入文本相似度）
        ──pick_for_review──► 失败优先 / 稀有路径优先 / 高成本 的待标注样本（带还原权重）

    种子问题 ──synthesize_cases──► 合成候选 ──filter_candidates──► 通过质量过滤的 EvalCase

    人工标签 vs 评委标签 ──agreement_report──► 一致率、kappa、TPR / TNR

练习（exercise.py）里的 cohen_kappa / stratified_sample / split_no_leak 是这条流水线上的另外三块拼图。
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence, TypeVar

from pydantic import BaseModel, Field

from agentkit import redact_pii
from agentkit.evals import EvalCase
from agentkit.llm import LLM, LLMError
from agentkit.workflows import complete_json

T = TypeVar("T")

# =====================================================================
# 1. 从 trace 重建"一次运行"
# =====================================================================

AGENT_SPANS = ("agent.run", "agent.resume")


@dataclass
class RunRecord:
    """从 trace 里还原出来的一次运行。挖数据、抽样、标注都以它为单位，而不是以 span 为单位。"""

    trace_id: str
    run_id: str | None = None
    input: str | None = None  # 用户原话：agentkit 的 trace 默认不记录，需要应用层自己记（见 demo 的 app.request span）
    output: str | None = None  # 最终回答：同上
    tools: list[str] = field(default_factory=list)  # 按调用顺序
    tool_errors: list[str] = field(default_factory=list)  # 失败的工具调用的 error_type
    status: str = "unknown"  # completed / max_steps / stopped / failed / paused / error / unknown
    cost_usd: float = 0.0
    tokens: int = 0
    steps: int = 0
    model: str | None = None
    user: str | None = None
    tenant: str | None = None
    start: float = 0.0
    duration_ms: float = 0.0
    feedback: str | None = None  # 显式反馈："up" / "down"
    feedback_reason: str = ""
    implicit: list[str] = field(default_factory=list)  # 隐式信号，如 "retried"（用户紧接着换个说法又问了一遍）

    @property
    def signature(self) -> str:
        return tool_signature(self.tools)

    def problems(self) -> list[str]:
        """这次运行身上所有的"问题信号"。空列表 = 没发现问题（不等于没问题）。"""
        out = []
        if self.status != "completed":
            out.append(f"status:{self.status}")
        out += [f"tool_error:{e}" for e in dict.fromkeys(self.tool_errors)]
        if self.feedback == "down":
            out.append("thumbs_down" + (f"({self.feedback_reason})" if self.feedback_reason else ""))
        out += self.implicit
        return out

    @property
    def is_problem(self) -> bool:
        return bool(self.problems())


def tool_signature(tools: Sequence[str]) -> str:
    """工具序列签名：把连续重复的同一个工具折叠成 `name+`，例如

        [search_policy, search_policy, get_order] → "search_policy+ → get_order"

    为什么要折叠？"查了 2 次"和"查了 3 次"通常是同一条路径上的不同重试次数，不折叠的话，
    每多重试一次就多出一个"新路径"，稀有路径会被重试噪声淹没。代价是丢掉了次数信息 ——
    它仍然保留在 RunRecord.tools 里，需要时（比如找死循环）直接看原始序列。
    """
    if not tools:
        return "(无工具)"
    parts: list[str] = []
    for name in tools:
        if parts and parts[-1].rstrip("+") == name:
            parts[-1] = name + "+"
        else:
            parts.append(name)
    return " → ".join(parts)


def load_runs(path: str | Path, *, stats: dict | None = None) -> list[RunRecord]:
    """读取 agentkit.tracing.jsonl_exporter 写出的文件，按 trace 重建运行记录，按开始时间排序。

    - 每行一个扁平的 span，靠 trace_id 归组、parent_id 还原成树；
    - 坏行（进程崩溃时只写了半行）跳过但计数，找不到根 span 的 trace（文件被截断）也跳过；
    - 暂停后 resume 会产生一条新的 trace（新的根 span），但 run_id 相同：按 run_id 合并回"一次运行"。
    stats 传一个 dict 进来，会被填上 spans / traces / bad_lines / orphan_traces 计数。
    """
    by_trace: dict[str, list[dict]] = defaultdict(list)
    n_spans = bad = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            span = json.loads(line)
            by_trace[span["trace_id"]].append(span)
            n_spans += 1
        except (json.JSONDecodeError, KeyError, TypeError):
            bad += 1
    records, orphans = [], 0
    for trace_id, spans in by_trace.items():
        rec = _build_record(trace_id, spans)
        if rec is None:
            orphans += 1
        else:
            records.append(rec)
    records = _merge_resumed(sorted(records, key=lambda r: r.start))
    if stats is not None:
        stats.update(spans=n_spans, traces=len(by_trace), bad_lines=bad, orphan_traces=orphans, runs=len(records))
    return records


def _build_record(trace_id: str, spans: list[dict]) -> RunRecord | None:
    root = next((s for s in spans if s.get("parent_id") is None), None)
    if root is None:
        return None
    children: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        if s.get("parent_id"):
            children[s["parent_id"]].append(s)
    for kids in children.values():
        kids.sort(key=lambda s: s.get("start") or 0)

    # 只取"最外层"的 Agent span：子 Agent（agent_as_tool）挂在某个 tool span 下面，它的工具不算主 Agent 的路径
    agents: list[dict] = []

    def find_agents(s: dict) -> None:
        if s.get("name") in AGENT_SPANS:
            agents.append(s)
            return
        for c in children.get(s["span_id"], []):
            find_agents(c)

    find_agents(root)
    attrs = root.get("attrs") or {}
    rec = RunRecord(
        trace_id=trace_id,
        input=attrs.get("app.input"),
        output=attrs.get("app.output"),
        start=root.get("start") or 0.0,
        duration_ms=root.get("duration_ms") or 0.0,
        user=attrs.get("user.id"),
        tenant=attrs.get("tenant.id"),
    )
    for a in agents:
        aa = a.get("attrs") or {}
        rec.run_id = rec.run_id or aa.get("run_id")
        rec.user = rec.user or aa.get("user.id")
        rec.tenant = rec.tenant or aa.get("tenant.id")
        rec.status = aa.get("agent.status", rec.status)
        rec.steps = max(rec.steps, aa.get("agent.steps") or 0)
        rec.cost_usd = max(rec.cost_usd, aa.get("agent.cost_usd") or 0.0)  # 累计值：取最大，而不是相加
        for c in children.get(a["span_id"], []):
            ca = c.get("attrs") or {}
            if c.get("name") == "llm.chat":
                rec.tokens += (ca.get("gen_ai.usage.input_tokens") or 0) + (ca.get("gen_ai.usage.output_tokens") or 0)
                rec.model = rec.model or ca.get("gen_ai.response.model") or ca.get("gen_ai.request.model")
            elif str(c.get("name", "")).startswith("tool."):
                rec.tools.append(ca.get("tool.name") or c["name"][len("tool.") :])
                if ca.get("tool.ok") is False:
                    rec.tool_errors.append(ca.get("tool.error_type") or "unknown")
    if root.get("status") == "error" and rec.status in ("unknown", "completed"):
        rec.status = "error"  # 异常一路冒泡到了根 span：应用层崩了
    return rec


def _merge_resumed(records: list[RunRecord]) -> list[RunRecord]:
    merged: dict[str, RunRecord] = {}
    out: list[RunRecord] = []
    for r in records:
        first = merged.get(r.run_id) if r.run_id else None
        if first is None:
            if r.run_id:
                merged[r.run_id] = r
            out.append(r)
            continue
        first.tools += r.tools
        first.tool_errors += r.tool_errors
        first.status = r.status  # 以最后一段为准
        first.output = r.output if r.output is not None else first.output
        first.cost_usd = max(first.cost_usd, r.cost_usd)
        first.steps = max(first.steps, r.steps)
        first.tokens += r.tokens
        first.duration_ms += r.duration_ms
    return out


# =====================================================================
# 2. 信号：显式反馈、隐式反馈
# =====================================================================


def attach_feedback(records: Iterable[RunRecord], feedback_rows: Iterable[dict]) -> int:
    """把异步到达的反馈（{"run_id", "rating": "up"/"down", "reason"}）按 run_id 关联到运行记录上。

    反馈永远是"事后"来的：用户看完回答、过一会儿才点 👎。所以它不在 trace 里，而在另一张表里，
    只能靠 run_id 关联 —— 这也是第 10 课要求"把 run_id 返回给前端、放进反馈按钮"的原因。
    返回成功关联的条数（关联不上的，多半是 run_id 没传对，值得报警）。
    """
    by_run = {r.run_id: r for r in records if r.run_id}
    n = 0
    for fb in feedback_rows:
        r = by_run.get(fb.get("run_id"))
        if r is None:
            continue
        r.feedback, r.feedback_reason = fb.get("rating"), fb.get("reason", "")
        n += 1
    return n


def mark_implicit_retries(records: Iterable[RunRecord], *, window_s: float = 300, min_similarity: float = 0.25) -> int:
    """隐式信号：同一个用户在 window_s 秒内又问了一个相似的问题 → 上一次的回答大概率没解决问题。

    显式反馈很稀疏（大多数人不点），隐式信号覆盖全量，但噪声大：用户也可能只是想追问细节。
    所以它只是"值得看一眼"的信号，不能直接当成"答错了"的标签。返回标记的条数。
    """
    by_user: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        if r.user and r.input:
            by_user[r.user].append(r)
    n = 0
    for runs in by_user.values():
        runs.sort(key=lambda r: r.start)
        for prev, nxt in zip(runs, runs[1:]):
            if nxt.start - prev.start <= window_s and text_similarity(prev.input, nxt.input) >= min_similarity:
                if "retried" not in prev.implicit:
                    prev.implicit.append("retried")
                    n += 1
    return n


# =====================================================================
# 3. 脱敏：数据离开 trace 系统之前
# =====================================================================


def pseudonymize(value: str | None, key: bytes) -> str | None:
    """用 HMAC 把用户 ID 换成假名：同一个人始终是同一个假名（还能按用户分组、防泄漏划分），
    但没有密钥就反查不回去。不要用普通 SHA-256：ID 空间小的时候（手机号、工号）可以被穷举反查（第 10 课）。"""
    if value is None:
        return None
    return "u#" + hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def scrub(record: RunRecord, key: bytes) -> RunRecord:
    """返回脱敏后的副本。应用层记录时已经脱敏过一次，这里再做一次是纵深防御：
    评估集会进 git、会被很多人看、可能被拿去微调，比 trace 系统的访问面大得多。"""
    return dataclasses.replace(
        record,
        input=redact_pii(record.input) if record.input else record.input,
        output=redact_pii(record.output) if record.output else record.output,
        user=pseudonymize(record.user, key),
        tools=list(record.tools),
        tool_errors=list(record.tool_errors),
        implicit=list(record.implicit),
    )


# =====================================================================
# 4. 文本相似度、去重、聚类
# =====================================================================


def normalize_text(text: str | None) -> str:
    """相似度比较前的归一化：全角转半角、小写、所有数字换成 0、去掉空白和标点。

    数字归一化是一个有意的取舍："订单 A1001 到哪了" 和 "订单 A1004 到哪了" 会被当成同一个问题。
    做评估集时这正是我们想要的（同一个场景）；但如果目的是复现某个具体订单的 bug，就不该这样归一化。
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"\d+", "0", text)
    return "".join(ch for ch in text if ch.isalnum())  # 中文字符的 isalnum() 也是 True


def shingles(text: str | None) -> set[str]:
    """字符一元组 + 二元组（shingle）。

    中文没有空格，没法按词切；零依赖下最稳妥的是按字符切片。只用二元组的话，短句一改写就几乎不重叠
    （"退货运费谁出" vs "退货的运费是谁承担" 只有 0.18）；把单字也放进来，对短句改写宽容得多（0.33），
    而完全无关的两句话仍然很低。这是在"零依赖"约束下的折中：真正的语义相似度要靠 embedding（第 5 节）。
    """
    t = normalize_text(text)
    return set(t) | {t[i : i + 2] for i in range(len(t) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0  # 没有内容的文本（比如 trace 里没记录输入）不和任何东西相似，免得被错误合并
    return len(a & b) / len(a | b)


def text_similarity(a: str | None, b: str | None) -> float:
    """两段文本 shingle 集合的 Jaccard 相似度（交集 / 并集），0~1。"""
    return _jaccard(shingles(a), shingles(b))


def similarity_groups(
    items: Sequence[T],
    threshold: float,
    text_fn: Callable[[T], str | None],
    bucket_fn: Callable[[T], object] | None = None,
) -> list[list[int]]:
    """把"同一个桶里、相似度 ≥ threshold"的两两连边，返回连通分量（每个分量是一组下标）。

    这就是单链接（single-linkage）聚类：结果和输入顺序无关、完全确定，而且保证
    "任意两条相似度 ≥ threshold 的数据一定在同一组" —— 按组划分数据集时，这正是防泄漏需要的性质。
    代价是链式效应：A 像 B、B 像 C，A 和 C 就被放进同一组，哪怕它们并不像。
    复杂度 O(n²)，几千条以内够用；再大就换 MinHash + LSH（见讲义第 5 节）。
    """
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    sh = [shingles(text_fn(x)) for x in items]
    buckets = [bucket_fn(x) if bucket_fn else None for x in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if buckets[i] == buckets[j] and _jaccard(sh[i], sh[j]) >= threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(i)
    return sorted(groups.values(), key=lambda g: g[0])


@dataclass
class DedupeResult:
    kept: list[RunRecord]
    duplicate_of: dict[str, str]  # 被去掉的 trace_id → 保留下来的那条的 trace_id

    def group_size(self, record: RunRecord) -> int:
        return 1 + sum(1 for v in self.duplicate_of.values() if v == record.trace_id)


def dedupe(records: Sequence[RunRecord], threshold: float = 0.8) -> DedupeResult:
    """近重复去重：工具签名相同 且 输入相似度 ≥ threshold 的运行，只保留一条代表。

    两个关键决策：
    1. 签名不同就不去重。同一个问题走出了两条不同的路径，说明 Agent 行为不稳定 ——
       这恰恰是最值得看的数据，绝不能当"重复"删掉。
    2. 代表优先选"有问题信号"的那条（信息量最大），都没有问题就选最早的一条。
    """
    kept: list[RunRecord] = []
    dup_of: dict[str, str] = {}
    for g in similarity_groups(records, threshold, lambda r: r.input, lambda r: r.signature):
        members = [records[i] for i in g]
        rep = min(members, key=lambda r: (not r.is_problem, r.start))
        kept.append(rep)
        for m in members:
            if m is not rep:
                dup_of[m.trace_id] = rep.trace_id
    kept.sort(key=lambda r: r.start)
    return DedupeResult(kept, dup_of)


@dataclass
class Cluster:
    id: str
    signature: str
    members: list[RunRecord]

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def problem_count(self) -> int:
        return sum(r.is_problem for r in self.members)


def cluster(records: Sequence[RunRecord], threshold: float = 0.25) -> list[Cluster]:
    """按"场景"聚类：工具签名相同 且 输入文本相似（连通分量）。按簇大小降序编号 C01、C02……

    和 dedupe 用的是同一个算法，只是阈值低得多：去重找"几乎一样"的，聚类找"在问同一类事"的。
    """
    groups = similarity_groups(records, threshold, lambda r: r.input, lambda r: r.signature)
    groups.sort(key=lambda g: (-len(g), g[0]))
    return [Cluster(f"C{k + 1:02d}", records[g[0]].signature, [records[i] for i in g]) for k, g in enumerate(groups)]


def group_ids(items: Sequence[T], threshold: float, text_fn: Callable[[T], str | None]) -> list[int]:
    """每条数据所属的"近重复组"编号（不分签名）。拿来当 split_no_leak 的 group_fn：
    相似度 ≥ threshold 的两条数据一定同组，也就一定会被分进同一个集合。"""
    out = [0] * len(items)
    for gid, g in enumerate(similarity_groups(items, threshold, text_fn)):
        for i in g:
            out[i] = gid
    return out


def cross_split_pairs(splits: dict[str, Sequence[T]], text_fn: Callable[[T], str | None], threshold: float) -> list[tuple[T, T]]:
    """找出落在不同集合里、但相似度 ≥ threshold 的数据对 —— 泄漏的直接证据。"""
    tagged = [(name, x, shingles(text_fn(x))) for name, xs in splits.items() for x in xs]
    pairs = []
    for i in range(len(tagged)):
        for j in range(i + 1, len(tagged)):
            if tagged[i][0] != tagged[j][0] and _jaccard(tagged[i][2], tagged[j][2]) >= threshold:
                pairs.append((tagged[i][1], tagged[j][1]))
    return pairs


# =====================================================================
# 5. 待标注抽样：失败优先、稀有路径优先、高成本
# =====================================================================

STRATA = ("failure", "rare_path", "high_cost", "normal")
DEFAULT_QUOTAS = {"failure": 0.4, "rare_path": 0.3, "high_cost": 0.2, "normal": 0.1}


@dataclass
class Pick:
    record: RunRecord
    stratum: str
    weight: float  # 这条样本在全量里"代表"多少条 = 该层总数 / 该层抽中数（逆抽样概率）


def assign_strata(records: Sequence[RunRecord], *, rare_max: int = 2, cost_quantile: float = 0.9) -> list[str]:
    """给每条运行分到唯一的一层，按优先级：有问题信号 > 稀有路径 > 高成本 > 普通。

    - 稀有路径：工具签名在全量里出现次数 ≤ rare_max。长尾路径上的 bug 最难被随机抽样碰到；
    - 高成本：成本 ≥ 全量的 cost_quantile 分位数。贵的运行往往在绕远路（重复调用、上下文过长）。
    """
    sig_counts = Counter(r.signature for r in records)
    costs = sorted(r.cost_usd for r in records)
    cut = costs[min(len(costs) - 1, int(cost_quantile * len(costs)))] if costs else 0.0
    out = []
    for r in records:
        if r.is_problem:
            out.append("failure")
        elif sig_counts[r.signature] <= rare_max:
            out.append("rare_path")
        elif r.cost_usd >= cut and cut > 0:
            out.append("high_cost")
        else:
            out.append("normal")
    return out


def pick_for_review(
    records: Sequence[RunRecord],
    n: int,
    *,
    seed: int = 0,
    quotas: dict[str, float] | None = None,
    rare_max: int = 2,
    cost_quantile: float = 0.9,
) -> list[Pick]:
    """从全量运行里挑 n 条交给人看。按配额分层抽样，某层不够就把名额顺延给后面的层。

    为什么不直接随机抽？失败只占几个百分点，随机抽 50 条可能一条失败都没有，标注预算全花在
    "看起来没问题"的数据上。为什么不只看失败？只看失败会漏掉"用户没抱怨、但走了奇怪路径"的问题，
    也没法估计整体质量。所以：配额保证每类都有，权重让你能把样本上的统计量还原回全量。
    """
    quotas = quotas or DEFAULT_QUOTAS
    if not 0 <= n <= len(records):
        raise ValueError(f"n={n} 超出范围 [0, {len(records)}]")
    labels = assign_strata(records, rare_max=rare_max, cost_quantile=cost_quantile)
    pools: dict[str, list[int]] = {s: [i for i, lab in enumerate(labels) if lab == s] for s in STRATA}

    # 1) 按配额算每层想要多少（最大余数法，保证总和 = n）
    raw = {s: quotas.get(s, 0.0) * n / (sum(quotas.values()) or 1) for s in STRATA}
    want = {s: int(raw[s]) for s in STRATA}
    for s in sorted(STRATA, key=lambda s: -(raw[s] - want[s]))[: n - sum(want.values())]:
        want[s] += 1
    # 2) 某层不够，名额按优先级顺延给还有余量的层
    take = {s: min(want[s], len(pools[s])) for s in STRATA}
    spare = n - sum(take.values())
    for s in STRATA:
        extra = min(spare, len(pools[s]) - take[s])
        take[s] += extra
        spare -= extra
    # 3) 层内随机（固定种子，可复现），记下权重
    rng = random.Random(seed)
    picks = []
    for s in STRATA:
        chosen = sorted(rng.sample(pools[s], take[s]))
        for i in chosen:
            picks.append(Pick(records[i], s, len(pools[s]) / take[s]))
    return picks


def weighted_mean(picks: Sequence[Pick], value_fn: Callable[[RunRecord], float]) -> float:
    """用抽样权重把样本统计量还原到全量（Horvitz-Thompson 思想）。

    不加权的话，"失败优先"抽出来的样本失败率会远高于真实失败率 —— 把它当成线上质量就严重失真了。
    """
    total = sum(p.weight for p in picks)
    return sum(p.weight * value_fn(p.record) for p in picks) / total if total else 0.0


def record_to_case(record: RunRecord, stratum: str | None = None) -> EvalCase:
    """运行记录 → 待标注的评估用例。expect 留空：正确行为是什么，要人来定（第 16 课的标注收件箱）。"""
    tags = ["from_prod", "needs_label", f"path:{record.signature}"] + [f"signal:{p}" for p in record.problems()]
    if stratum:
        tags.append(f"stratum:{stratum}")
    return EvalCase(
        id=f"prod-{record.run_id or record.trace_id}",
        input=record.input or "",
        expect={},
        metadata={"tenant_id": record.tenant} if record.tenant else {},
        tags=tags,
    )


# =====================================================================
# 6. 合成用例：生成
# =====================================================================


@dataclass
class Seed:
    id: str
    input: str
    reference: str  # 种子问题的正确答案要点


class SynthCase(BaseModel):
    input: str = Field(description="用户会怎么问：一句话，像真实用户说的话")
    dimension: str = Field(description="这条用例对应的改写维度名称，照抄给定的维度名")
    answer_type: Literal["answerable", "should_decline"] = Field(
        description="answerable：知识库能回答；should_decline：知识库没有覆盖，正确做法是说明无法确认并转人工"
    )
    reference_answer: str = Field(description="正确回答的要点，一两句话，只能依据知识库")
    must_contain: list[str] = Field(
        default_factory=list,
        description="正确回答这个问题时必须出现的 1-3 个短关键词（每个不超过 8 个字，如数字、时限、专有名词），"
        "从知识库原文逐字复制，不要整句复制；should_decline 时为空列表",
    )
    evidence: str = Field(description="支撑 reference_answer 的知识库原文，逐字复制；should_decline 时写“无”")


class SynthBatch(BaseModel):
    cases: list[SynthCase]


@dataclass
class Candidate:
    """一条合成出来、还没通过质量检查的候选用例。"""

    id: str
    seed_id: str
    dimension: str
    input: str
    answer_type: str
    reference_answer: str
    must_contain: list[str]
    evidence: str

    def to_eval_case(self, decline_expect: dict | None = None) -> EvalCase:
        """转成 agentkit 的 EvalCase。seed / 维度写进 tags：划分数据集时要按 seed 分组（同一模板的变体不能跨集合）。"""
        expect: dict = {"reference_answer": self.reference_answer}
        if self.answer_type == "answerable":
            expect["must_contain"] = list(self.must_contain)
        else:
            expect.update(decline_expect or {})
        return EvalCase(
            id=self.id,
            input=self.input,
            expect=expect,
            tags=["synthetic", f"seed:{self.seed_id}", f"dim:{self.dimension}", self.answer_type],
        )


SYNTH_PROMPT = """你在为一个 AI 助手构造评估用例（测试题），用来检验它能否依据知识库正确回答。

## 知识库（助手能查到的全部信息）
{knowledge}

## 种子问题
{seed}
（参考答案要点：{reference}）

## 任务
围绕种子问题的主题，生成 {k} 条新的测试问题，第 i 条使用下面第 i 个改写维度：
{dimensions}

## 要求
1. 新问题要像真实用户会说的话，不要只是把种子换几个字；
2. answerable 的用例：reference_answer 只能依据知识库；must_contain 和 evidence 必须从知识库原文逐字复制；
   must_contain 只放回答这个问题必需的短关键词，不要把知识库里和这个问题无关的规则也放进去；
3. 知识库没有覆盖的问题，answer_type 填 should_decline，must_contain 为空，evidence 写“无”；
4. 带错误前提的问题（比如把时限说错）仍然是 answerable，正确答案要纠正这个前提。"""


def synthesize_cases(
    llm: LLM,
    seeds: Sequence[Seed],
    dimensions: dict[str, str],
    knowledge: str,
    per_seed: int | Sequence[int] = 3,
) -> list[Candidate]:
    """每个种子调用一次模型，按给定维度生成 per_seed 条变体（用 complete_json 保证结构化输出）。

    为什么要"种子 + 维度"，而不是一句"帮我生成 100 条测试题"？
    不给约束，模型会反复生成它自己觉得典型的问题：干净、完整、单一意图 —— 比真实用户的问题简单得多，
    而且彼此高度相似。种子把主题锚定在真实流量上，维度强制覆盖你关心的变化方向。
    """
    names = list(dimensions)
    counts = [per_seed] * len(seeds) if isinstance(per_seed, int) else list(per_seed)
    out: list[Candidate] = []
    for s_idx, (seed, k) in enumerate(zip(seeds, counts)):
        # 每个种子从不同的维度开始轮换：种子少、每个种子生成的条数也少时，各维度在整体上仍然都能覆盖到
        dims = [names[(s_idx + i) % len(names)] for i in range(k)]
        dim_text = "\n".join(f"{i + 1}. {d}：{dimensions[d]}" for i, d in enumerate(dims))
        prompt = SYNTH_PROMPT.format(knowledge=knowledge, seed=seed.input, reference=seed.reference, k=k, dimensions=dim_text)
        batch = complete_json(llm, prompt, SynthBatch)
        for i, c in enumerate(batch.cases[:k]):
            out.append(
                Candidate(
                    id=f"syn-{seed.id}-{i + 1}",
                    seed_id=seed.id,
                    dimension=c.dimension,
                    input=c.input.strip(),
                    answer_type=c.answer_type,
                    reference_answer=c.reference_answer.strip(),
                    must_contain=[m.strip() for m in c.must_contain if m.strip()],
                    evidence=c.evidence.strip(),
                )
            )
    return out


# =====================================================================
# 7. 合成用例：质量过滤（便宜的检查在前，调模型的检查在后）
# =====================================================================


class AnswerCheck(BaseModel):
    reason: str = Field(description="先写一两句核查理由，引用知识库原文")
    kb_covers_question: bool = Field(description="依据知识库，是否有足够信息回答这个问题")
    reference_supported: bool = Field(description="参考答案的每一个要点，是否都能从知识库推出")
    keywords_necessary: bool = Field(description="必含关键词是否都是正确回答这个问题时必须提到的（而不是知识库里和本题无关的规则）；没有关键词时填 true")


# 第一版写的是"只依据知识库判断，不要使用你自己的常识"。真实运行时，核查员因为"知识库没说海鲜礼盒属于生鲜"
# 拒掉了一道好题 —— 验证者也会过严。改成：允许常识性的归类，但不允许用知识库以外的规则、数字、承诺补全答案。
CHECK_PROMPT = """请核查一条自动生成的测试题是否可靠。依据下面的知识库判断：可以做常识性的归类和推理（比如"海鲜属于生鲜"），
但不能用知识库以外的规则、数字或承诺来补全答案。

## 知识库
{knowledge}

## 测试题
问题：{question}
出题者给的参考答案：{reference}
必含关键词：{keywords}"""


@dataclass
class Rejection:
    candidate: Candidate
    reason: str


def _squash(text: str) -> str:
    """做"是否逐字出现在知识库里"的检查用：全角转半角、去空白和标点，但**保留数字**（数字错一个就是错）。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())


def _cheap_reason(
    c: Candidate, kb: str, seed_text: str | None, kept: list[Candidate], *, min_chars: int, max_chars: int, dup_threshold: float, max_keyword_chars: int
) -> str | None:
    if len(c.input) < min_chars:
        return "too_short"
    if len(c.input) > max_chars:
        return "too_long"
    if not c.reference_answer:
        return "no_reference"
    if seed_text is not None and text_similarity(c.input, seed_text) >= dup_threshold:
        return "near_dup_seed"  # 和种子几乎一样：没带来新信息
    if any(text_similarity(c.input, k.input) >= dup_threshold for k in kept):
        return "near_dup_other"
    if c.answer_type == "answerable":
        if not c.must_contain:
            return "no_must_contain"  # 没有可检查的关键词，规则评分器没法判
        missing = [m for m in c.must_contain if _squash(m) not in kb]
        if missing:
            return f"keyword_not_in_kb:{missing[0]}"
        too_long = [m for m in c.must_contain if len(_squash(m)) > max_keyword_chars]
        if too_long:  # 规则评分要求逐字出现：整句当关键词，正确的回答换个说法就会被判错
            return f"keyword_too_long:{too_long[0]}"
        pieces = [p for p in re.split(r"……|\.\.\.|[；;。]", c.evidence) if _squash(p)]
        if not pieces or any(_squash(p) not in kb for p in pieces):
            return "evidence_not_in_kb"  # 出题者声称的"原文"在知识库里找不到：多半是编的
    return None


def filter_candidates(
    candidates: Sequence[Candidate],
    *,
    knowledge: str,
    seeds: Sequence[Seed] = (),
    llm: LLM | None = None,
    min_chars: int = 6,
    max_chars: int = 200,
    dup_threshold: float = 0.6,
    max_keyword_chars: int = 8,
    max_workers: int = 2,
) -> tuple[list[Candidate], list[Rejection]]:
    """对合成候选做质量过滤，返回 (保留, 拒绝及原因)。

    顺序很重要：先做免费的规则检查（长度、去重、关键词和证据是否逐字出现在知识库里、关键词是否太长），
    只把幸存者交给 LLM 做最贵的检查。LLM 检查的核心问题是：
    **期望答案能不能从给定知识推出来** —— 出题模型最常见的错误，就是把自己的常识当成了知识库内容；
    顺带检查必含关键词是不是都必需（出题模型爱把知识库里的无关规则也塞进期望，导致规则评分误判）。
    """
    kb = _squash(knowledge)
    seed_text = {s.id: s.input for s in seeds}
    survivors: list[Candidate] = []
    rejected: list[Rejection] = []
    for c in candidates:
        reason = _cheap_reason(
            c, kb, seed_text.get(c.seed_id), survivors,
            min_chars=min_chars, max_chars=max_chars, dup_threshold=dup_threshold, max_keyword_chars=max_keyword_chars,
        )
        if reason:
            rejected.append(Rejection(c, reason))
        else:
            survivors.append(c)
    if llm is None:
        return survivors, rejected

    def check(c: Candidate) -> AnswerCheck | None:
        keywords = "、".join(c.must_contain) or "（无）"
        prompt = CHECK_PROMPT.format(knowledge=knowledge, question=c.input, reference=c.reference_answer, keywords=keywords)
        try:
            return complete_json(llm, prompt, AnswerCheck)
        except (ValueError, LLMError):  # 核查失败的题宁可不要，也不能默认放行
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:  # 限制并发：别把共享的模型配额打满
        verdicts = list(pool.map(check, survivors))
    kept = []
    for c, v in zip(survivors, verdicts):
        if v is None:
            rejected.append(Rejection(c, "llm:check_failed"))
            continue
        if c.answer_type == "should_decline":
            reason = "llm:kb_has_answer" if v.kb_covers_question else None  # 标成"该拒答"，其实知识库能答：标签错了
        elif not v.kb_covers_question:
            reason = "llm:kb_missing"
        elif not v.reference_supported:
            reason = "llm:reference_unsupported"
        elif not v.keywords_necessary:
            reason = "llm:keyword_unnecessary"  # 期望写得过细：规则评分会冤枉那些没顺带提到无关规则的正确回答
        else:
            reason = None
        if reason:
            rejected.append(Rejection(c, f"{reason}（{v.reason}）"))
        else:
            kept.append(c)
    return kept, rejected


# =====================================================================
# 8. 一致性：人和人、人和评委
# =====================================================================


def percent_agreement(a: Sequence, b: Sequence) -> float:
    """百分比一致率：两个标注者给出相同标签的比例。直观，但不扣除"碰巧一致"。"""
    if len(a) != len(b) or not a:
        raise ValueError("两组标签必须等长且非空")
    return sum(x == y for x, y in zip(a, b)) / len(a)


def kappa_2x2(tp: int, fp: int, fn: int, tn: int) -> float:
    """二分类特例的 Cohen's kappa（练习 (a) 要实现任意类别的通用版本，可以拿这个对照）。

        po = (tp + tn) / n                          实际一致率
        pe = P(A 阳)·P(B 阳) + P(A 阴)·P(B 阴)      两人各按自己的比例"瞎标"时的期望一致率
        kappa = (po - pe) / (1 - pe)
    """
    n = tp + fp + fn + tn
    if n == 0:
        raise ValueError("空的混淆矩阵")
    po = (tp + tn) / n
    pe = ((tp + fn) * (tp + fp) + (fp + tn) * (fn + tn)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


@dataclass
class AgreementReport:
    n: int
    agreement: float
    kappa: float
    tpr: float  # 人判为问题的里面，评委也抓到了多少（召回）
    tnr: float  # 人判为合格的里面，评委也放行了多少
    tp: int
    fp: int
    fn: int
    tn: int


def agreement_report(human: Sequence[str], judge: Sequence[str], positive: str = "fail") -> AgreementReport:
    """把人的标签当真值，评估评委。positive 是"发现问题"的那个标签。

    只报一致率会被类别不平衡骗：95% 的回答都合格时，一个永远说"合格"的评委也有 95% 一致率。
    所以同时报 kappa（扣除碰巧一致）和 TPR / TNR（分别看"抓问题"和"不冤枉"两种能力）。
    """
    if len(human) != len(judge) or not human:
        raise ValueError("两组标签必须等长且非空")
    tp = sum(h == positive and j == positive for h, j in zip(human, judge))
    fn = sum(h == positive and j != positive for h, j in zip(human, judge))
    fp = sum(h != positive and j == positive for h, j in zip(human, judge))
    tn = sum(h != positive and j != positive for h, j in zip(human, judge))
    return AgreementReport(
        n=len(human),
        agreement=percent_agreement(human, judge),
        kappa=kappa_2x2(tp, fp, fn, tn),
        tpr=tp / (tp + fn) if tp + fn else float("nan"),
        tnr=tn / (tn + fp) if tn + fp else float("nan"),
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
    )
