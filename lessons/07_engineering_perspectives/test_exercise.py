"""第 07 课练习测试：离线、确定性（不调用任何模型）。

运行：.venv/bin/python -m pytest lessons/07_engineering_perspectives -v

最后 4 个测试是"一致性检查"：用你写的 triggered_considerations / coverage_report
去检查本课的 README（中英文）与 perspectives.py 是否一致。它们也是本课内容的回归测试。
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
P = ex.P
HERE = Path(__file__).resolve().parent

Consideration = P.Consideration
Triggered = P.Triggered
Requirement = P.Requirement
FocusRule = P.FocusRule


# =====================================================================
# (a) evaluate / triggered_considerations
# =====================================================================


def test_bool_fact_must_be_exactly_true():
    cond = {"fact": "can_send_external"}
    assert ex.evaluate(cond, {"can_send_external": True}) == (True, ["can_send_external = True"])
    assert ex.evaluate(cond, {"can_send_external": False}) == (False, [])
    # LLM 抽取的画像里常见的"脏值"：它们都不是 True
    assert ex.evaluate(cond, {"can_send_external": "false"}) == (False, [])
    assert ex.evaluate(cond, {"can_send_external": "yes"}) == (False, [])
    assert ex.evaluate(cond, {"can_send_external": 1}) == (False, [])


def test_numeric_thresholds_and_reasons():
    profile = {"peak_qps": 80, "availability_slo": 99.9, "provider_rpm_limit": 0}
    assert ex.evaluate({"fact": "peak_qps", "op": ">=", "value": 50}, profile) == (True, ["peak_qps = 80 >= 50"])
    assert ex.evaluate({"fact": "peak_qps", "op": ">=", "value": 80}, profile)[0] is True
    assert ex.evaluate({"fact": "peak_qps", "op": ">", "value": 80}, profile) == (False, [])
    assert ex.evaluate({"fact": "peak_qps", "op": "<", "value": 100}, profile)[0] is True
    assert ex.evaluate({"fact": "peak_qps", "op": "<=", "value": 79}, profile)[0] is False
    assert ex.evaluate({"fact": "peak_qps", "op": "!=", "value": 80}, profile)[0] is False
    assert ex.evaluate({"fact": "availability_slo", "op": ">=", "value": 99.9}, profile) == (
        True,
        ["availability_slo = 99.9 >= 99.9"],
    )
    assert ex.evaluate({"fact": "provider_rpm_limit", "op": "==", "value": 0}, profile) == (
        True,
        ["provider_rpm_limit = 0 == 0"],
    )


def test_value_can_reference_another_fact():
    cond = {"fact": "peak_llm_rpm", "op": ">", "value": {"fact": "provider_rpm_limit"}}
    over = {"peak_llm_rpm": 12000, "provider_rpm_limit": 5000}
    assert ex.evaluate(cond, over) == (True, ["peak_llm_rpm = 12000 > provider_rpm_limit = 5000"])
    assert ex.evaluate(cond, {"peak_llm_rpm": 900, "provider_rpm_limit": 1000}) == (False, [])
    with pytest.raises(P.UnknownFactError):
        ex.evaluate(cond, {"peak_llm_rpm": 12000})  # 引用的事实不存在


def test_all_any_not_semantics_and_reasons():
    profile = {"a": True, "b": False, "n": 10}
    a, b = {"fact": "a"}, {"fact": "b"}
    n_big = {"fact": "n", "op": ">=", "value": 5}

    assert ex.evaluate({"all": [a, n_big]}, profile) == (True, ["a = True", "n = 10 >= 5"])
    assert ex.evaluate({"all": [a, b]}, profile) == (False, [])
    # any 只收集"成立的"子条件的原因
    assert ex.evaluate({"any": [b, a, n_big]}, profile) == (True, ["a = True", "n = 10 >= 5"])
    assert ex.evaluate({"any": [b]}, profile) == (False, [])
    assert ex.evaluate({"not": b}, profile) == (True, ["not b"])
    assert ex.evaluate({"not": a}, profile) == (False, [])
    assert ex.evaluate({"not": {"any": [b, {"fact": "n", "op": ">", "value": 99}]}}, profile) == (
        True,
        ["not (b or n > 99)"],
    )
    # 嵌套 + 空列表，与 Python 内置 all/any 一致
    assert ex.evaluate({"all": [a, {"any": [b, n_big]}]}, profile) == (True, ["a = True", "n = 10 >= 5"])
    assert ex.evaluate({"all": []}, profile) == (True, [])
    assert ex.evaluate({"any": []}, profile) == (False, [])


def test_missing_fact_raises_even_if_another_branch_already_decides():
    """不短路：画像漏填的事实必须暴露出来，哪怕另一个分支已经决定了结果。"""
    profile = {"a": True, "b": False}
    with pytest.raises(P.UnknownFactError) as e:
        ex.evaluate({"any": [{"fact": "a"}, {"fact": "can_send_external"}]}, profile)
    assert e.value.fact == "can_send_external"
    with pytest.raises(P.UnknownFactError):
        ex.evaluate({"all": [{"fact": "b"}, {"fact": "peak_qps", "op": ">", "value": 1}]}, profile)
    with pytest.raises(P.UnknownFactError):
        ex.evaluate({"not": {"fact": "missing"}}, profile)


def test_malformed_conditions_raise_value_error():
    profile = {"x": 1}
    bad = [
        {"fact": "x", "op": "=>", "value": 1},  # 不支持的运算符
        {"fact": "x", "value": 1},  # 有 value 没有 op
        {"fact": "x", "op": ">"},  # 有 op 没有 value
        {"every": [{"fact": "x"}]},  # 不认识的形态
        "x > 1",  # 不是 dict
    ]
    for cond in bad:
        with pytest.raises(ValueError):
            ex.evaluate(cond, profile)


def _c(id: str, severity: str = "P1", when: dict | None = None) -> Consideration:
    return Consideration(id, severity, id, id, when)


def test_triggered_keeps_catalog_order_and_reasons():
    catalog = [
        _c("security.lethal", "P0", {"all": [{"fact": "private"}, {"fact": "untrusted"}, {"fact": "send"}]}),
        _c("goals.scope", "P0"),
        _c("ux.voice", "P0", {"fact": "voice"}),
        _c("scale.qps", "P1", {"fact": "qps", "op": ">=", "value": 50}),
    ]
    profile = {"private": True, "untrusted": True, "send": True, "voice": False, "qps": 50}
    got = ex.triggered_considerations(profile, catalog)
    assert [t.item.id for t in got] == ["security.lethal", "goals.scope", "scale.qps"]
    assert got[0].reasons == ("private = True", "untrusted = True", "send = True")
    assert got[1].reasons == (P.GENERAL_REASON,)
    assert got[2].reasons == ("qps = 50 >= 50",)
    assert all(isinstance(t, Triggered) and isinstance(t.reasons, tuple) for t in got)


def test_triggered_rejects_duplicate_ids():
    catalog = [_c("goals.scope"), _c("goals.scope", "P0")]
    with pytest.raises(ValueError):
        ex.triggered_considerations({}, catalog)


def test_example_profile_with_real_catalog():
    """第 4 节的完整小例子：B2B SaaS 的销售邮件助理。"""
    profile = P.derive_facts(P.EXAMPLE_PROFILE)
    got = {t.item.id: t for t in ex.triggered_considerations(profile, P.CATALOG)}

    assert got["security.lethal_trifecta"].reasons == (
        "accesses_private_data = True",
        "reads_untrusted_content = True",
        "can_send_external = True",
    )
    assert got["scale.quota_exceeded"].reasons == (
        "provider_rpm_limit = 5000 > 0",
        "peak_llm_rpm = 12000 > provider_rpm_limit = 5000",
    )
    assert got["ux.streaming"].reasons == ("not batch", "p95_task_seconds = 10 >= 5")
    assert "tenancy.tenant_from_auth" in got and "privacy.cross_border_transfer" in got
    for absent in ("latency.voice_budget", "tools.sandbox", "scale.quota_unknown", "ux.async_tasks"):
        assert absent not in got
    # 通用必查点一条不少
    general = [c.id for c in P.CATALOG if c.when is None]
    assert all(g in got for g in general)


# =====================================================================
# (b) prioritize
# =====================================================================

RULES = [
    FocusRule({"fact": "multi_tenant"}, {"tenancy": 3, "cost": 1}),
    FocusRule({"fact": "voice"}, {"latency": 5}),  # 不成立的规则不能加权
]
PROFILE_B = {"multi_tenant": True, "voice": False}


def test_prioritize_severity_dominates_weight():
    items = [
        Triggered(_c("tenancy.a", "P1"), ("r",)),  # 权重 3，但级别低
        Triggered(_c("docs.b", "P0"), ("r",)),  # 权重 0，但级别高
        Triggered(_c("tenancy.c", "P2"), ("r",)),
    ]
    ranked = ex.prioritize(items, PROFILE_B, RULES)
    assert [r.item.id for r in ranked] == ["docs.b", "tenancy.a", "tenancy.c"]
    assert [r.rank for r in ranked] == [1, 2, 3]
    assert [r.weight for r in ranked] == [0, 3, 3]


def test_prioritize_weight_then_id_and_only_active_rules_count():
    items = [
        Triggered(_c("latency.z", "P1"), ("r",)),  # voice 规则不成立 → 权重 0
        Triggered(_c("cost.b", "P1"), ("r",)),  # 1
        Triggered(_c("tenancy.b", "P1"), ("r",)),  # 3
        Triggered(_c("tenancy.a", "P1"), ("r",)),  # 3，id 更小
        Triggered(_c("goals.a", "P1"), ("r",)),  # 0，id 比 latency.z 小
    ]
    ranked = ex.prioritize(items, PROFILE_B, RULES)
    assert [r.item.id for r in ranked] == ["tenancy.a", "tenancy.b", "cost.b", "goals.a", "latency.z"]
    assert [r.weight for r in ranked] == [3, 3, 1, 0, 0]


def test_prioritize_is_reproducible_under_shuffling():
    profile = P.derive_facts(P.EXAMPLE_PROFILE)
    items = ex.triggered_considerations(profile, P.CATALOG)
    expected = [(r.rank, r.item.id, r.weight) for r in ex.prioritize(items, profile)]
    rng = random.Random(7)
    for _ in range(20):
        shuffled = items[:]
        rng.shuffle(shuffled)
        assert [(r.rank, r.item.id, r.weight) for r in ex.prioritize(shuffled, profile)] == expected


def test_prioritize_uses_default_focus_rules():
    profile = P.derive_facts(P.EXAMPLE_PROFILE)
    ranked = ex.prioritize(ex.triggered_considerations(profile, P.CATALOG), profile)
    # 销售邮件助理：安全 = 对外用户 1 + 致命三要素 3 + 不可信内容 1 + 对外发送 1 = 6，是权重最高的维度
    assert ranked[0].item.id == "security.assume_fooled"
    assert ranked[0].weight == 6
    assert all(r.item.severity == "P0" for r in ranked[:60])
    assert ranked[60].item.severity == "P1"


def test_prioritize_merges_duplicates_and_rejects_bad_severity():
    a = _c("tenancy.a", "P1")
    items = [Triggered(a, ("x", "y")), Triggered(_c("goals.b", "P1"), ("z",)), Triggered(a, ("y", "w"))]
    ranked = ex.prioritize(items, PROFILE_B, RULES)
    assert [r.item.id for r in ranked] == ["tenancy.a", "goals.b"]
    assert ranked[0].reasons == ("x", "y", "w")
    with pytest.raises(ValueError):
        ex.prioritize([Triggered(_c("goals.x", "P3"), ("r",))], PROFILE_B, RULES)


# =====================================================================
# (c) coverage_report
# =====================================================================

REQS = [
    Requirement("cost", "成本", ("预算", "token")),
    Requirement("security", "安全", ("威胁模型", "prompt injection")),
    Requirement("vendor", "供应商依赖与退出", ("供应商", "lock-in")),
]


def test_coverage_heading_match_with_earliest_evidence():
    doc = "# 设计文档\n\n正文里提到了预算。\n\n## 3. 成本估算\n\n## 4. 安全与威胁模型\n"
    report = ex.coverage_report(doc, REQS)
    assert report.covered["cost"] == P.Evidence("heading", 5, "## 3. 成本估算")  # 标题匹配优先于更早的关键词
    assert report.covered["security"] == P.Evidence("heading", 7, "## 4. 安全与威胁模型")
    assert [r.id for r in report.missing] == ["vendor"]
    assert report.ratio == pytest.approx(2 / 3)


def test_coverage_keyword_match_is_normalized():
    doc = "## 风险\n我们评估了 Prompt   Injection 的风险。\n每月ＴＯＫＥＮ用量约 2 亿。\n避免 LOCK-IN。\n"
    report = ex.coverage_report(doc, REQS)
    assert report.covered["security"] == P.Evidence("keyword", 2, "我们评估了 Prompt   Injection 的风险。")
    assert report.covered["cost"].how == "keyword" and report.covered["cost"].line_no == 3
    assert report.covered["vendor"].line_no == 4
    assert report.missing == []
    assert report.ratio == 1.0


def test_coverage_ignores_placeholder_lines():
    doc = "## 成本：TODO\n## 安全\n供应商：待补充\n预算 TBD\n"
    report = ex.coverage_report(doc, REQS)
    assert set(report.covered) == {"security"}
    assert [r.id for r in report.missing] == ["cost", "vendor"]


def test_coverage_hash_inside_code_block_is_not_a_heading():
    doc = "## 部署\n```bash\n# 成本\nmake deploy\n```\n~~~\n# 供应商依赖与退出\n~~~\n"
    report = ex.coverage_report(doc, [Requirement("cost", "成本"), Requirement("vendor", "供应商依赖与退出")])
    assert report.covered == {}
    assert [r.id for r in report.missing] == ["cost", "vendor"]
    # 但代码块里的文字仍然可以作为关键词命中
    report = ex.coverage_report(doc, [Requirement("deploy", "发布", ("make deploy",))])
    assert report.covered["deploy"] == P.Evidence("keyword", 4, "make deploy")


def test_coverage_edge_cases():
    assert [r.id for r in ex.coverage_report("", REQS).missing] == ["cost", "security", "vendor"]
    assert ex.coverage_report("", REQS).ratio == 0.0
    empty = ex.coverage_report("# 任何文档", [])
    assert empty.covered == {} and empty.missing == [] and empty.ratio == 1.0
    # 空标题、空关键词不能匹配一切
    assert ex.coverage_report("## 随便什么\n", [Requirement("x", "", ("",))]).missing[0].id == "x"


# =====================================================================
# 一致性检查：README ⇄ perspectives.py（用你写的函数来检查）
# =====================================================================

README = {"zh": HERE / "README.md", "en": HERE / "README.en.md"}
_FILE_SUFFIXES = {"py", "md", "json", "jsonl", "txt"}


def _ids_in(text: str) -> set[str]:
    dims = "|".join(d.id for d in P.DIMENSIONS)
    found = set(re.findall(rf"`((?:{dims})\.[a-z0-9_]+)`", text))
    return {x for x in found if x.split(".", 1)[1] not in _FILE_SUFFIXES}


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_readme_matches_catalog(lang):
    text = README[lang].read_text(encoding="utf-8")
    # 1) 每个维度都有自己的小节标题，每个考量点都以 `id` 的形式出现
    dim_reqs = [Requirement(f"dim:{d.id}", d.zh if lang == "zh" else d.en) for d in P.DIMENSIONS]
    item_reqs = [Requirement(c.id, c.title(lang), (f"`{c.id}`",)) for c in P.CATALOG]
    report = ex.coverage_report(text, dim_reqs + item_reqs)
    assert [r.id for r in report.missing] == [], f"{README[lang].name} 缺少这些维度或考量点"
    assert all(report.covered[f"dim:{d.id}"].how == "heading" for d in P.DIMENSIONS)
    # 2) README 里没有目录之外的 id
    assert _ids_in(text) == set(P.CATALOG_BY_ID)
    # 3) 每一条：只出现一次，标题、级别、触发条件都与目录一致
    lines = text.splitlines()
    for c in P.CATALOG:
        hits = [line for line in lines if f"`{c.id}`" in line]
        assert len(hits) == 1, f"{c.id} 应该恰好出现在一行里"
        line = hits[0]
        assert c.title(lang) in line, f"{c.id} 的标题与目录不一致"
        tags = {s for s in P.SEVERITIES if f"{P.SEVERITY_ICON[s]} {s}" in line}
        assert tags == {c.severity}, f"{c.id} 的级别应该是 {c.severity}，README 里是 {tags}"
        if c.when is not None:
            assert f"`{P.render_condition(c.when)}`" in line, f"{c.id} 的触发条件与目录不一致"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_readme_scenario_matrix_matches_scenarios(lang):
    text = README[lang].read_text(encoding="utf-8")
    names = [s.zh if lang == "zh" else s.en for s in P.SCENARIOS]
    report = ex.coverage_report(text, [Requirement(s.id, n) for s, n in zip(P.SCENARIOS, names)])
    assert [r.id for r in report.missing] == [], "每个场景都应该有自己的小节标题"
    assert all(e.how == "heading" for e in report.covered.values())

    shorts = [s.short_zh if lang == "zh" else s.short_en for s in P.SCENARIOS]
    lines = text.splitlines()
    header_idx = next(
        i for i, line in enumerate(lines)
        if line.startswith("|") and [c.strip() for c in line.strip().strip("|").split("|")][1:] == shorts
    )
    dim_by_name = {(d.zh if lang == "zh" else d.en): d.id for d in P.DIMENSIONS}
    grid: dict[str, list[str]] = {}
    for line in lines[header_idx + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        grid[dim_by_name[cells[0].replace("*", "")]] = cells[1:]
    assert set(grid) == set(P.DIMENSION_BY_ID), "矩阵应该覆盖全部 20 个维度"
    for j, s in enumerate(P.SCENARIOS):
        column = {dim: row[j] for dim, row in grid.items()}
        assert set(column.values()) <= {"●", "◐", "○"}
        assert {d for d, v in column.items() if v == "●"} == set(s.focus), f"{s.id} 的重点维度与 SCENARIOS 不一致"
        assert {d for d, v in column.items() if v == "○"} == set(s.defer), f"{s.id} 的可缓维度与 SCENARIOS 不一致"


def test_catalog_is_well_formed_and_every_trigger_is_reachable():
    # 各种画像（含两个"极端"画像）合起来，每个情境点都至少被触发一次 —— 条件里没有写错的事实名、没有永远不成立的条件
    extremes = []
    for limit in (0, 1):
        maxed = {f.name: True if f.kind == "bool" else 1_000_000 for f in P.FACTS}
        maxed["provider_rpm_limit"] = limit
        extremes.append(maxed)
    lean = dict(extremes[0], batch=False, self_hosted_model=False)
    profiles = [s.profile for s in P.SCENARIOS] + [P.EXAMPLE_PROFILE] + extremes + [lean]
    reached = set()
    for profile in profiles:
        reached |= {t.item.id for t in ex.triggered_considerations(P.derive_facts(profile), P.CATALOG)}
    assert {c.id for c in P.CATALOG if c.when is not None} <= reached

    # 结构检查
    assert len(P.DIMENSIONS) == 20 and len(P.GROUPS) == 6
    assert len(P.CATALOG_BY_ID) == len(P.CATALOG), "id 不能重复"
    for d in P.DIMENSIONS:
        assert d.group in P.GROUP_BY_ID
        assert 6 <= len(P.items_of(d.id, "general")) <= 10, d.id
        assert 6 <= len(P.items_of(d.id, "situational")) <= 12, d.id
    for c in P.CATALOG:
        assert c.dimension in P.DIMENSION_BY_ID and c.severity in P.SEVERITIES
        assert P.referenced_facts(c.when) <= set(P.FACT_BY_NAME)
    for s in P.SCENARIOS:
        assert P.validate_profile(s.profile) == [], s.id
        assert set(s.focus) | set(s.defer) <= set(P.DIMENSION_BY_ID) and not set(s.focus) & set(s.defer)
    assert len(P.SCENARIOS) >= 8
    assert P.validate_profile(P.EXAMPLE_PROFILE) == []
