"""第 15 课练习测试：离线、确定性（不调用模型）。

运行：make lesson N=15    或    .venv/bin/python -m pytest lessons/15_enterprise_rag -v
"""

from __future__ import annotations

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
Document, User = ex.Document, ex.User


def doc(doc_id, text, allowed=("all-staff",), tenant="acme", title=None, version=1, **kw):
    return Document(doc_id, tenant, title or doc_id, text, frozenset(allowed), version=version, **kw)


ALICE = User("acme", "alice", {"all-staff", "eng"})  # 研发
HENRY = User("acme", "henry", {"all-staff", "hr"})  # HR


def ids(hits):
    return [h.doc.doc_id for h in hits]


# =====================================================================
# (a) SecureIndex.search
# =====================================================================


def test_search_isolates_tenants_even_with_same_group_names():
    index = ex.SecureIndex(
        [
            doc("A-1", "差旅 住宿 标准 每晚 600 元", tenant="acme"),
            doc("G-1", "差旅 住宿 标准 每晚 900 元", tenant="globex"),  # 另一家公司，同样有 all-staff 组
        ]
    )
    assert ids(index.search("差旅住宿标准", ALICE)) == ["A-1"]
    carol = User("globex", "carol", {"all-staff"})
    assert ids(index.search("差旅住宿标准", carol)) == ["G-1"]


def test_search_prefilters_so_top_k_does_not_shrink():
    # 最相关的 3 篇都是 HR 机密；post-filter 取 top-3 再过滤，alice 会一篇都拿不到
    secret = [doc(f"HR-{i}", "调薪 预算 调薪 预算 调薪 预算 机密", allowed=("hr",)) for i in range(3)]
    public = [doc(f"PUB-{i}", f"调薪 流程 说明 第{i}部分") for i in range(4)]
    index = ex.SecureIndex(secret + public)
    hits = index.search("调薪预算", ALICE, k=3)
    assert len(hits) == 3
    assert all(h.doc.doc_id.startswith("PUB-") for h in hits)
    # HR 同事能看到机密文档，而且它们排在前面
    assert ids(index.search("调薪预算", HENRY, k=3)) == ["HR-0", "HR-1", "HR-2"]
    # 单独分享给某个人：ACL 里写 user:<id>
    index.add(doc("SHARED-1", "调薪 预算 调薪 预算 调薪 预算 草稿", allowed=("user:alice",)))
    assert ids(index.search("调薪预算", ALICE, k=1)) == ["SHARED-1"]
    assert index.search("调薪预算", ALICE, k=0) == []


def test_search_returns_only_the_latest_version():
    index = ex.SecureIndex(
        [
            doc("HR-001", "一线城市 住宿 每晚 500 元", version=1),
            doc("HR-001", "一线城市 住宿 每晚 600 元", version=2),
        ]
    )
    hits = index.search("一线城市住宿", ALICE)
    assert len(hits) == 1
    assert hits[0].doc.version == 2 and "600" in hits[0].doc.text


def test_search_checks_permission_on_the_latest_version():
    index = ex.SecureIndex(
        [
            doc("ENG-7", "生产 数据库 只读 账号 申请", allowed=("eng",), version=1),
            doc("ENG-7", "生产 数据库 只读 账号 申请（已收紧）", allowed=("dba",), version=2),  # 权限被收回
        ]
    )
    assert index.search("生产数据库账号", ALICE) == []  # 不能退回去用 v1
    dba = User("acme", "dan", {"dba"})
    assert [h.doc.version for h in index.search("生产数据库账号", dba)] == [2]


def test_search_hides_deleted_documents_without_falling_back_to_old_versions():
    index = ex.SecureIndex(
        [
            doc("HR-009", "年终奖 发放 时间 一月", version=1),
            doc("HR-009", "年终奖 发放 时间 二月", version=2),
            doc("HR-010", "年终奖 计算 方式"),
        ]
    )
    index.delete("acme", "HR-009", updated_at="2026-09-27")
    assert ids(index.search("年终奖发放", ALICE)) == ["HR-010"]


def test_search_leaks_nothing_about_hidden_documents():
    visible = [
        doc("PUB-1", "报销 报销 报销 流程 在 系统 提交"),
        doc("PUB-2", "差旅 标准 与 报销 说明"),
        doc("PUB-3", "办公 用品 申领"),
    ]
    hidden = [doc(f"SECRET-{i}", f"报销 报销 高管 特殊 报销 通道 {i}", allowed=("execs",)) for i in range(6)]
    hidden.append(doc("GLOBEX-1", "报销 标准", tenant="globex"))
    q = "报销标准"
    alone = [(h.doc.doc_id, h.doc.version, h.score) for h in ex.SecureIndex(visible).search(q, ALICE, k=5)]
    mixed = [(h.doc.doc_id, h.doc.version, h.score) for h in ex.SecureIndex(visible + hidden).search(q, ALICE, k=5)]
    # 无权文档存在与否，结果必须一模一样：条数、顺序、分数都不能变（IDF 等统计量也不能用到它们）
    assert mixed == alone
    assert alone, "至少应该检索到一篇可见文档"


# =====================================================================
# (b) chunk_markdown
# =====================================================================

HANDBOOK = """\
员工手册（2026 版）

# 差旅

出差需提前在 OA 提交申请。

住宿标准见下表。

# 报销

发票抬头必须是公司全称。
"""


def test_chunk_never_crosses_headings_and_records_heading():
    chunks = ex.chunk_markdown(HANDBOOK, max_chars=200, overlap=0)
    assert [(c.heading, c.text) for c in chunks] == [
        ("", "员工手册（2026 版）"),
        ("差旅", "出差需提前在 OA 提交申请。\n\n住宿标准见下表。"),  # 同一小节的两段合并成一块
        ("报销", "发票抬头必须是公司全称。"),
    ]


def test_chunk_merges_paragraphs_greedily_up_to_max_chars():
    text = "\n\n".join(["甲" * 30, "乙" * 30, "丙" * 30])
    chunks = ex.chunk_markdown(text, max_chars=70, overlap=0)
    assert [c.text for c in chunks] == ["甲" * 30 + "\n\n" + "乙" * 30, "丙" * 30]
    assert all(len(c.text) <= 70 for c in chunks)


def test_chunk_splits_long_paragraph_with_overlap():
    long_para = "".join(chr(ord("a") + i % 26) for i in range(95))
    text = f"# 附录\n\n短段落\n\n{long_para}\n\n结尾段落"
    chunks = ex.chunk_markdown(text, max_chars=40, overlap=10)
    assert all(c.heading == "附录" and len(c.text) <= 40 for c in chunks)
    assert chunks[0].text == "短段落"  # 超长段落之前已拼好的块先输出
    assert chunks[-1].text == "结尾段落"  # 切出来的块不和后面的段落合并
    pieces = [c.text for c in chunks[1:-1]]
    assert pieces == [long_para[0:40], long_para[30:70], long_para[60:95]]
    for a, b in zip(pieces, pieces[1:]):
        assert a[-10:] == b[:10]  # 相邻块重叠 10 个字符


def test_chunk_keeps_code_block_intact():
    text = """\
# 安装

先安装依赖：
```bash
# 这是注释，不是标题

pip install agentkit
```
# 验证

运行 make check-env。
"""
    chunks = ex.chunk_markdown(text, max_chars=200, overlap=0)
    assert [c.heading for c in chunks] == ["安装", "验证"]
    code = "```bash\n# 这是注释，不是标题\n\npip install agentkit\n```"
    assert chunks[0].text == "先安装依赖：\n\n" + code  # 代码块自成一段，再和前面的文字合并
    assert chunks[1].text == "运行 make check-env。"


def test_chunk_rejects_invalid_overlap():
    with pytest.raises(ValueError):
        ex.chunk_markdown("随便一段文字", max_chars=10, overlap=10)
    with pytest.raises(ValueError):
        ex.chunk_markdown("随便一段文字", max_chars=10, overlap=-1)


# =====================================================================
# (c) check_citations
# =====================================================================

SOURCES = {
    "HR-001": "[HR-001] 差旅与报销制度（v2，更新于 2026-09-01）\n一线城市住宿标准为每晚不超过 600 元，其他城市每晚不超过 400 元。",
    "HR-010": "[HR-010] 2026 年度调薪流程（全员版）\n调薪结果于 2026 年 3 月生效，由直属经理在 2 月完成一对一沟通。",
}


def statuses(report):
    return [v.status for v in report.verdicts]


def test_citations_supported_answer_passes():
    answer = "一线城市住宿每晚不超过 600 元 [HR-001]。调薪结果于 2026 年 3 月生效 [HR-010]。"
    report = ex.check_citations(answer, SOURCES)
    assert statuses(report) == ["supported", "supported"]
    assert report.ok and report.problems == []
    assert all(v.coverage is not None and v.coverage >= 0.5 for v in report.verdicts)


def test_citations_flags_claim_without_citation():
    answer = "一线城市住宿每晚不超过 600 元 [HR-001]。另外每位员工每年还有 2000 元健身补贴。"
    report = ex.check_citations(answer, SOURCES)
    assert statuses(report) == ["supported", "no_citation"]
    assert not report.ok and [v.claim for v in report.problems] == ["另外每位员工每年还有 2000 元健身补贴。"]


def test_citations_flags_source_not_retrieved_in_this_run():
    # HR-011 可能真实存在，但这次没有检索到（例如用户无权查看）—— 不能算有依据
    answer = "2026 年调薪预算为薪资总额的 6% [HR-011]。"
    report = ex.check_citations(answer, SOURCES)
    assert statuses(report) == ["unknown_source"]


def test_citations_flags_number_not_in_cited_source():
    answer = "一线城市住宿每晚不超过 800 元 [HR-001]。"  # 词几乎全对，只有数字被"改了一下"
    report = ex.check_citations(answer, SOURCES)
    assert statuses(report) == ["number_mismatch"]


def test_citations_flags_low_overlap():
    answer = "年假可以跨年结转五天并在次年三月底前休完 [HR-010]。"
    report = ex.check_citations(answer, SOURCES)
    assert statuses(report) == ["low_overlap"]
    assert report.verdicts[0].coverage is not None and report.verdicts[0].coverage < 0.5


def test_citations_multi_source_uses_union_and_refusal_is_allowed():
    answer = (
        "一线城市住宿每晚不超过 600 元，调薪结果于 2026 年 3 月生效 [HR-001, HR-010]。\n"
        "知识库中没有找到关于宠物托管费用的规定。"
    )
    report = ex.check_citations(answer, SOURCES)
    assert statuses(report) == ["supported", "refusal"]
    assert report.ok
    assert not ex.check_citations("", SOURCES).ok  # 空回答不算通过
