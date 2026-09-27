"""第 18 课练习测试：离线、确定（固定时间 + 固定 id + ScriptedLLM），不调用真实模型。

运行：make lesson N=18    或    .venv/bin/python -m pytest lessons/18_memory_systems -v
"""

from __future__ import annotations

import json

import pytest

from agentkit import ScriptedLLM, reply
from agentkit.testing import load_exercise

ex = load_exercise(__file__)
mk = ex.memory_kit
MemoryRecord, MemoryOp = ex.MemoryRecord, ex.MemoryOp

T0 = 1_000_000.0  # 固定的"现在"：所有时间相关的断言都基于它
DAY = ex.DAY


def rec(rid: str, text: str, *, key: str = "other", importance: float = 5.0, at: float = T0, sources=None, status="active"):
    return MemoryRecord(
        id=rid, text=text, key=key, importance=importance, created_at=at, updated_at=at, last_accessed=at,
        sources=list(sources or []), status=status,
    )


def ids(prefix: str = "n"):
    counter = iter(range(1, 1000))
    return lambda: f"{prefix}{next(counter)}"


# =====================================================================
# (a) retrieval_score
# =====================================================================


def test_score_extremes_and_default_equal_weights():
    fresh = rec("a", "用户对芒果过敏", importance=10, at=T0)
    assert ex.retrieval_score(fresh, 1.0, T0, half_life=30 * DAY) == pytest.approx(1.0)
    stale = rec("b", "用户喜欢蓝色", importance=1, at=T0 - 3650 * DAY)
    assert ex.retrieval_score(stale, 0.0, T0, half_life=30 * DAY) == pytest.approx(0.0, abs=1e-9)
    # 默认等权 = 三个归一化因子的平均：近期性 1、重要性 (5.5-1)/9 = 0.5、相关性 0.2
    mid = rec("c", "x", importance=5.5, at=T0)
    assert ex.retrieval_score(mid, 0.2, T0, half_life=DAY) == pytest.approx((1.0 + 0.5 + 0.2) / 3)


def test_recency_halves_every_half_life():
    only_recency = {"recency": 1.0}
    m = rec("a", "x", at=T0)
    assert ex.retrieval_score(m, 0.0, T0 + 30 * DAY, 30 * DAY, only_recency) == pytest.approx(0.5)
    assert ex.retrieval_score(m, 0.0, T0 + 60 * DAY, 30 * DAY, only_recency) == pytest.approx(0.25)
    # 权重里缺少的名字按 0 算：只看近期性时，重要性和相关性再高也不影响
    vip = rec("b", "x", importance=10, at=T0)
    assert ex.retrieval_score(vip, 1.0, T0 + 30 * DAY, 30 * DAY, only_recency) == pytest.approx(0.5)


def test_clock_skew_and_out_of_range_inputs_are_clamped():
    m = rec("a", "x", importance=5, at=T0 + 3600)  # 另一台机器的时钟快了一小时
    assert ex.retrieval_score(m, 0.0, T0, DAY, {"recency": 1.0}) == pytest.approx(1.0)
    assert ex.retrieval_score(m, 1.7, T0, DAY, {"relevance": 1.0}) == pytest.approx(1.0)
    assert ex.retrieval_score(m, -0.3, T0, DAY, {"relevance": 1.0}) == pytest.approx(0.0)
    assert ex.retrieval_score(rec("b", "x", importance=42), 0.0, T0, DAY, {"importance": 1.0}) == pytest.approx(1.0)
    assert ex.retrieval_score(rec("c", "x", importance=0), 0.0, T0, DAY, {"importance": 1.0}) == pytest.approx(0.0)


def test_weights_are_normalized_and_validated():
    m = rec("a", "x", importance=7, at=T0 - 10 * DAY)
    w = {"recency": 1.0, "importance": 2.0, "relevance": 3.0}
    s1 = ex.retrieval_score(m, 0.4, T0, 30 * DAY, w)
    s10 = ex.retrieval_score(m, 0.4, T0, 30 * DAY, {k: v * 10 for k, v in w.items()})
    assert s1 == pytest.approx(s10), "权重整体放大不应改变分数（要除以权重之和）"
    assert 0.0 <= s1 <= 1.0
    for bad in ({"recency": -1.0, "relevance": 2.0}, {"recency": 0.0, "importance": 0.0}, {"freshness": 1.0}):
        with pytest.raises(ValueError):
            ex.retrieval_score(m, 0.4, T0, 30 * DAY, bad)
    with pytest.raises(ValueError):
        ex.retrieval_score(m, 0.4, T0, 0.0)


def test_importance_rescues_relevant_but_wordless_memory():
    """第 04 课的漏召回：问题里没有"过敏"两个字。纯相关性会漏掉过敏；加上重要性就不会。"""
    allergy = rec("a", "用户对芒果过敏", importance=9, at=T0 - 30 * DAY)
    chit = rec("b", "用户上周看了一部电影", importance=2, at=T0 - 1 * DAY)
    rel = {"a": 0.0, "b": 0.1}  # 问题"推荐一家餐厅"和两条记忆的字面相似度

    def ranking(weights):
        scored = {m.id: ex.retrieval_score(m, rel[m.id], T0, 30 * DAY, weights) for m in (allergy, chit)}
        return sorted(scored, key=scored.get, reverse=True)

    assert ranking({"relevance": 1.0}) == ["b", "a"]
    assert ranking(None) == ["a", "b"]


# =====================================================================
# (b) apply_memory_ops
# =====================================================================


def test_add_creates_record_with_audit_history_and_ttl():
    store: dict = {}
    res = ex.apply_memory_ops(
        store,
        [MemoryOp("ADD", None, "  用户这周在北京出差 ", "plan", 3, 7, "临时安排", "session-3"),
         MemoryOp("ADD", None, "   ", "other")],
        now=T0, new_id=ids(),
    )
    assert [(r.op, r.id, r.status) for r in res] == [("ADD", "n1", "applied"), ("ADD", None, "skipped")]
    r = store["n1"]
    assert (r.text, r.key, r.importance, r.status) == ("用户这周在北京出差", "plan", 3, "active")
    assert r.created_at == r.updated_at == r.last_accessed == T0
    assert r.expires_at == pytest.approx(T0 + 7 * DAY)
    assert r.sources == ["session-3"]
    assert r.history == [{"op": "ADD", "old": None, "new": "用户这周在北京出差", "at": T0, "reason": "临时安排", "source": "session-3"}]


def test_duplicate_add_merges_source_instead_of_creating():
    store = {"m1": rec("m1", "用户对芒果过敏", key="allergy", sources=["session-1"])}
    res = ex.apply_memory_ops(
        store,
        [MemoryOp("ADD", None, "用户对芒果过敏。", "allergy", 9, source="session-5"),
         MemoryOp("ADD", None, "用户 对芒果过敏", "allergy", 9, source="session-5")],
        now=T0 + DAY, new_id=ids(),
    )
    assert [(r.id, r.status) for r in res] == [("m1", "duplicate"), ("m1", "duplicate")]
    assert list(store) == ["m1"]
    assert store["m1"].sources == ["session-1", "session-5"], "新出处要并进来，但不能重复"


def test_update_keeps_old_value_in_history():
    store = {"m1": rec("m1", "用户吃素", key="diet", importance=7, sources=["session-1"])}
    now = T0 + 30 * DAY
    res = ex.apply_memory_ops(
        store,
        [MemoryOp("UPDATE", "m1", "用户不再吃素，现在吃鱼和鸡肉", "diet", 8, None, "饮食习惯变了", "session-2"),
         MemoryOp("UPDATE", "m1", " 用户不再吃素，现在吃鱼和鸡肉 ", "diet", 8)],
        now=now, new_id=ids(),
    )
    assert [r.status for r in res] == ["applied", "unchanged"]
    r = store["m1"]
    assert (r.text, r.importance, r.updated_at, r.last_accessed, r.created_at) == (
        "用户不再吃素，现在吃鱼和鸡肉", 8, now, now, T0)
    assert r.sources == ["session-1", "session-2"]
    assert r.history == [{"op": "UPDATE", "old": "用户吃素", "new": "用户不再吃素，现在吃鱼和鸡肉", "at": now,
                          "reason": "饮食习惯变了", "source": "session-2"}], "unchanged 不应该写历史"


def test_unknown_or_deleted_ids_are_not_found_and_nothing_changes():
    store = {"m1": rec("m1", "用户对花生过敏", key="allergy"), "m2": rec("m2", "旧的", status="deleted")}
    before = json.dumps({k: vars(v) for k, v in store.items()}, ensure_ascii=False, sort_keys=True)
    res = ex.apply_memory_ops(
        store,
        [MemoryOp("UPDATE", "m9", "编造的 id"), MemoryOp("DELETE", "m9"), MemoryOp("UPDATE", "m2", "改一条已删除的"),
         MemoryOp("DELETE", None), MemoryOp("NOOP", "m1", reason="已经记过")],
        now=T0 + DAY, new_id=ids(),
    )
    assert [(r.op, r.id, r.status) for r in res] == [
        ("UPDATE", "m9", "not_found"), ("DELETE", "m9", "not_found"), ("UPDATE", "m2", "not_found"),
        ("DELETE", None, "not_found"), ("NOOP", "m1", "noop")]
    assert json.dumps({k: vars(v) for k, v in store.items()}, ensure_ascii=False, sort_keys=True) == before


def test_delete_is_soft_and_later_ops_see_it():
    store = {"m1": rec("m1", "用户对花生过敏", key="allergy")}
    now = T0 + 120 * DAY
    res = ex.apply_memory_ops(
        store,
        [MemoryOp("DELETE", "m1", reason="用户说记错了", source="session-5"),
         MemoryOp("DELETE", "m1"),  # 已经删了：not_found
         MemoryOp("ADD", None, "用户对花生过敏", "allergy", 9)],  # 死掉的记录不算重复：新建一条
        now=now, new_id=ids(),
    )
    assert [(r.id, r.status) for r in res] == [("m1", "applied"), ("m1", "not_found"), ("n1", "applied")]
    old = store["m1"]
    assert old.status == "deleted" and old.text == "用户对花生过敏", "软删除：记录和原文都要留着给审计"
    assert old.updated_at == now
    assert old.history[-1] == {"op": "DELETE", "old": "用户对花生过敏", "new": None, "at": now,
                               "reason": "用户说记错了", "source": "session-5"}


def test_fact_memory_end_to_end_with_your_apply_function():
    """集成：把你的 apply_memory_ops 塞进 Mem0 式 FactMemory，跑两次会话（剧本代替模型）。"""

    def extracted(*facts):
        return reply(json.dumps({"facts": [{"text": t, "key": k, "importance": i, "ttl_days": None} for t, k, i in facts]},
                                ensure_ascii=False))

    def decide_update_city(messages):
        existing = json.loads(messages[-1]["content"].split("<existing_memories>")[1].split("</existing_memories>")[0])
        target = next(e["id"] for e in existing if "上海" in e["text"])  # 模型看到的是短编号 "0""1"…
        ops = [{"op": "UPDATE", "id": target, "text": "用户住在深圳", "key": "city", "importance": 6, "reason": "搬家"},
               {"op": "NOOP", "id": next(e["id"] for e in existing if "Alice" in e["text"]), "reason": "已知"}]
        return reply(json.dumps({"ops": ops}, ensure_ascii=False))

    llm = ScriptedLLM([
        extracted(("用户住在上海", "city", 5), ("用户名叫 Alice", "name", 3)),
        extracted(("用户已搬到深圳", "city", 6), ("用户名叫 Alice", "name", 3)),
        decide_update_city,
    ])
    mem = mk.FactMemory(llm, apply_fn=ex.apply_memory_ops, new_id=ids("m"))
    mem.observe("acme", "alice", "我叫 Alice，住在上海", now=T0, source="s1")
    pairs = mem.observe("acme", "alice", "我搬到深圳了", now=T0 + 30 * DAY, source="s2")
    assert [(op.op, op.id, res.status) for op, res in pairs] == [("UPDATE", "m1", "applied"), ("NOOP", "m2", "noop")]
    live = {r.text for r in mem.live("acme", "alice", T0 + 31 * DAY)}
    assert live == {"用户住在深圳", "用户名叫 Alice"}
    assert mem.store("acme", "alice")["m1"].history[-1]["old"] == "用户住在上海"
    assert mem.live("globex", "alice", T0 + 31 * DAY) == [], "另一个租户的同名用户看不到"


# =====================================================================
# (c) consolidate
# =====================================================================


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def test_consolidate_keeps_latest_text_and_all_sources():
    mems = [
        rec("m1", "用户住在深圳南山", key="city", importance=5, at=T0, sources=["s1"]),
        rec("m2", "用户喜欢咖啡", key="hobby", at=T0 + DAY, sources=["s2"]),
        rec("m3", "用户住在深圳南山区", key="city", importance=7, at=T0 + 2 * DAY, sources=["s3", "s1"]),
    ]
    mems[0].last_accessed = T0 + 5 * DAY
    out = ex.consolidate(mems, jaccard, 0.8, now=T0 + 10 * DAY)
    assert [m.id for m in out] == ["m3", "m2"], "合并后的记录放在该组最早出现的位置"
    merged = out[0]
    assert merged.text == "用户住在深圳南山区", "保留最新的值"
    assert merged.sources == ["s1", "s3"]
    assert (merged.importance, merged.created_at, merged.last_accessed) == (7, T0, T0 + 5 * DAY)
    assert merged.history[-1] == {"op": "MERGE", "merged": ["m1"], "old": ["用户住在深圳南山"],
                                  "new": "用户住在深圳南山区", "at": T0 + 10 * DAY}


def test_consolidate_never_merges_across_keys_or_inactive_records():
    mems = [
        rec("a", "用户对花生过敏", key="allergy", at=T0),
        rec("b", "用户对花生过敏。", key="diet", at=T0 + DAY),  # 字面几乎一样，但槽位不同
        rec("c", "用户对花生过敏!", key="allergy", at=T0 + 2 * DAY, status="deleted"),
    ]
    out = ex.consolidate(mems, jaccard, 0.5, now=T0)
    assert [(m.id, m.text, m.status) for m in out] == [(m.id, m.text, m.status) for m in mems]
    assert all(len(m.history) == 0 for m in out)


def test_consolidate_is_transitive_and_does_not_mutate_input():
    mems = [
        rec("a", "AAAAB", key="k", at=T0, sources=["s1"]),
        rec("b", "AAABB", key="k", at=T0 + DAY, sources=["s2"]),
        rec("c", "AABBB", key="k", at=T0 + 2 * DAY, sources=["s3"]),
    ]
    sim = lambda x, y: sum(p == q for p, q in zip(x, y)) / 5  # a~b 0.8，b~c 0.8，a~c 只有 0.6
    snapshot = [(m.text, list(m.sources), list(m.history)) for m in mems]
    out = ex.consolidate(mems, sim, 0.8, now=T0)
    assert len(out) == 1 and out[0].id == "c" and out[0].sources == ["s1", "s2", "s3"]
    assert out[0].history[-1]["merged"] == ["a", "b"]
    assert [(m.text, m.sources, m.history) for m in mems] == snapshot, "不要修改传入的对象"
    assert out[0] is not mems[2]
    for bad in (0.0, 1.5, -0.1):
        with pytest.raises(ValueError):
            ex.consolidate(mems, sim, bad)
