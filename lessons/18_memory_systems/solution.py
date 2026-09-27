"""第 18 课练习参考答案。先自己做，再来对照。"""

from __future__ import annotations

import copy
import importlib.util
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Callable


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "18_memory_systems__memory_kit"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


memory_kit = _load_sibling("memory_kit")

MemoryRecord = memory_kit.MemoryRecord
MemoryOp = memory_kit.MemoryOp
OpResult = memory_kit.OpResult
normalize_text = memory_kit.normalize_text
DAY = memory_kit.DAY

WEIGHT_NAMES = ("recency", "importance", "relevance")


# =====================================================================
# (a) retrieval_score
# =====================================================================


def retrieval_score(
    memory: MemoryRecord,
    query_relevance: float,
    now: float,
    half_life: float,
    weights: dict[str, float] | None = None,
) -> float:
    if half_life <= 0:
        raise ValueError("half_life 必须大于 0")
    if weights is None:
        w = {name: 1.0 for name in WEIGHT_NAMES}
    else:
        unknown = set(weights) - set(WEIGHT_NAMES)
        if unknown:
            raise ValueError(f"未知的权重名：{sorted(unknown)}")
        w = {name: float(weights.get(name, 0.0)) for name in WEIGHT_NAMES}
    if any(v < 0 for v in w.values()) or sum(w.values()) == 0:
        raise ValueError(f"权重必须非负且不全为 0：{w}")

    age = max(0.0, now - memory.last_accessed)  # 时钟偏差：别让近期性超过 1
    factors = {
        "recency": 0.5 ** (age / half_life),
        "importance": (min(10.0, max(1.0, memory.importance)) - 1.0) / 9.0,
        "relevance": min(1.0, max(0.0, query_relevance)),
    }
    return sum(w[k] * factors[k] for k in WEIGHT_NAMES) / sum(w.values())


# =====================================================================
# (b) apply_memory_ops
# =====================================================================


def _expires(now: float, ttl_days: float | None) -> float | None:
    return now + ttl_days * DAY if ttl_days else None


def apply_memory_ops(
    store: dict[str, MemoryRecord],
    ops: list[MemoryOp],
    *,
    now: float | None = None,
    new_id: Callable[[], str] | None = None,
) -> list[OpResult]:
    now = time.time() if now is None else now
    new_id = new_id or (lambda: uuid.uuid4().hex[:8])
    results: list[OpResult] = []

    for op in ops:
        if op.op == "NOOP":
            results.append(OpResult("NOOP", op.id, "noop"))
            continue

        if op.op == "ADD":
            text = (op.text or "").strip()
            if not text:
                results.append(OpResult("ADD", None, "skipped"))
                continue
            # 每次都现查：同一批里前面的操作（比如刚 DELETE 掉的）要能影响后面的判断
            dup = next(
                (r for r in store.values() if r.status == "active" and normalize_text(r.text) == normalize_text(text)),
                None,
            )
            if dup is not None:
                if op.source and op.source not in dup.sources:
                    dup.sources.append(op.source)
                results.append(OpResult("ADD", dup.id, "duplicate"))
                continue
            rid = new_id()
            store[rid] = MemoryRecord(
                id=rid,
                text=text,
                key=op.key,
                importance=op.importance,
                created_at=now,
                updated_at=now,
                last_accessed=now,
                expires_at=_expires(now, op.ttl_days),
                sources=[op.source] if op.source else [],
                history=[{"op": "ADD", "old": None, "new": text, "at": now, "reason": op.reason, "source": op.source}],
            )
            results.append(OpResult("ADD", rid, "applied"))
            continue

        # UPDATE / DELETE：先确认目标存在且还活着。不猜模型"本来想改哪条"。
        record = store.get(op.id) if op.id is not None else None
        if record is None or record.status != "active":
            results.append(OpResult(op.op, op.id, "not_found"))
            continue

        if op.op == "UPDATE":
            text = (op.text or "").strip()
            if not text or normalize_text(text) == normalize_text(record.text):
                results.append(OpResult("UPDATE", record.id, "unchanged"))
                continue
            record.history.append(
                {"op": "UPDATE", "old": record.text, "new": text, "at": now, "reason": op.reason, "source": op.source}
            )
            record.text = text
            record.key = op.key
            record.importance = op.importance
            record.updated_at = record.last_accessed = now
            record.expires_at = _expires(now, op.ttl_days)
            if op.source and op.source not in record.sources:
                record.sources.append(op.source)
            results.append(OpResult("UPDATE", record.id, "applied"))
        else:  # DELETE：软删除，保留原文给审计
            record.history.append(
                {"op": "DELETE", "old": record.text, "new": None, "at": now, "reason": op.reason, "source": op.source}
            )
            record.status = "deleted"
            record.updated_at = now
            results.append(OpResult("DELETE", record.id, "applied"))
    return results


# =====================================================================
# (c) consolidate
# =====================================================================


def consolidate(
    memories: list[MemoryRecord],
    similarity_fn: Callable[[str, str], float],
    threshold: float,
    *,
    now: float | None = None,
) -> list[MemoryRecord]:
    if not 0 < threshold <= 1:
        raise ValueError("threshold 必须在 (0, 1] 之间")
    now = time.time() if now is None else now
    n = len(memories)

    # 并查集：相似关系按传递处理
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    active = [i for i, m in enumerate(memories) if m.status == "active"]
    for x in range(len(active)):
        for y in range(x + 1, len(active)):
            i, j = active[x], active[y]
            a, b = memories[i], memories[j]
            if a.key == b.key and similarity_fn(a.text, b.text) >= threshold:
                parent[find(j)] = find(i)

    groups: dict[int, list[int]] = {}
    for i in active:
        groups.setdefault(find(i), []).append(i)  # 组内按输入顺序

    merged_at: dict[int, MemoryRecord] = {}  # 组内最早出现的位置 → 合并后的记录
    absorbed: set[int] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        base_idx = max(members, key=lambda i: (memories[i].updated_at, memories[i].created_at, memories[i].id))
        base = copy.deepcopy(memories[base_idx])
        sources: list[str] = []
        for i in sorted(members, key=lambda i: memories[i].created_at):
            for s in memories[i].sources:
                if s not in sources:
                    sources.append(s)
        others = [i for i in members if i != base_idx]
        base.sources = sources
        base.importance = max(memories[i].importance for i in members)
        base.created_at = min(memories[i].created_at for i in members)
        base.last_accessed = max(memories[i].last_accessed for i in members)
        base.history.append(
            {
                "op": "MERGE",
                "merged": [memories[i].id for i in others],
                "old": [memories[i].text for i in others],
                "new": base.text,
                "at": now,
            }
        )
        merged_at[members[0]] = base
        absorbed.update(members[1:])

    out: list[MemoryRecord] = []
    for i, m in enumerate(memories):
        if i in merged_at:
            out.append(merged_at[i])
        elif i not in absorbed:
            out.append(copy.deepcopy(m))
    return out
