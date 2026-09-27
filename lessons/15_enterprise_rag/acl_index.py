"""第 15 课：一个零依赖的"权限感知"迷你检索引擎。

生产里的样子：向量库 / 搜索引擎 + 每个块上的元数据（tenant_id、ACL、doc_id、version……）+ 一条同步管道。
这里把它缩成一个文件，只保留和**权限、版本、隔离、来源可信度**相关的骨架；
打分用关键词（复用 agentkit.memory.tokenize 的中文二元组切分），换成向量检索时，这些骨架一个都不能少。

读这个文件时重点看三件事：
1. `can_read`：权限判断写在检索层的代码里，而不是写在提示词里让模型"自觉"；
2. `post_filter_search` 与 `pre_filter_search`：同样是"过滤"，放在打分之前还是之后，结果完全不同；
3. `ACLIndex.docs` 是只追加（append-only）的：文档的每个版本、每次删除（墓碑）都是一条记录。
   两个朴素的检索方法**不处理多版本和删除** —— 这正是第 15 课练习 (a) 要你补上的。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

from agentkit.guardrails import detect_injection
from agentkit.memory import tokenize

# 检索时忽略的高频二元组（问句里的虚词），否则"是多少""怎么办"会让不相关的文档也得分
STOP_TOKENS = {
    "什么", "怎么", "怎样", "如何", "多少", "是多", "少钱", "么办", "一下", "请问", "可以", "能不", "不能",
    "是不", "不是", "我的", "我们", "你们", "他们", "这个", "那个", "哪些", "有没", "没有", "是否", "吗",
}

# 来源可信度分级（问题卡片 6）：数字越小越可信
TRUST_LEVELS = {"official": 0, "internal": 1, "external": 2}


@dataclass(frozen=True)
class User:
    """一次检索背后的"真人"。tenant_id / user_id 来自认证，groups 来自企业目录（IdP），都不来自模型。"""

    tenant_id: str
    user_id: str
    groups: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "groups", frozenset(self.groups))

    @property
    def principals(self) -> frozenset[str]:
        """用户的全部"主体"（principal）：所属的组 + 代表他本人的 user:<id>。

        ACL 里写 "user:alice" 表示单独分享给 alice（对应网盘里"分享给某个人"）。
        """
        return self.groups | {f"user:{self.user_id}"}


@dataclass(frozen=True)
class Document:
    """索引里的一条记录 = 某篇文档的**某一个版本**。

    生产中一篇文档会被切成很多块（chunk），每个块都要**继承**文档的 tenant_id / allowed / version。
    漏了元数据的块 = 权限失控的块，所以 allowed 为空时一律"谁都不能读"（fail closed），而不是"谁都能读"。
    """

    doc_id: str
    tenant_id: str
    title: str
    text: str
    allowed: frozenset[str] = frozenset()  # ACL：允许读取的组 / 用户（principal）
    version: int = 1
    updated_at: str = ""
    source: str = "wiki"  # 来源系统：hr-portal / wiki / vendor-site ……
    author: str = ""
    trust: str = "internal"  # official / internal / external
    deleted: bool = False  # 墓碑（tombstone）：表示"这篇文档在这个版本被删除了"

    def __post_init__(self):
        object.__setattr__(self, "allowed", frozenset(self.allowed))


@dataclass(frozen=True)
class Hit:
    doc: Document
    score: float


def can_read(user: User, doc: Document) -> bool:
    """权限判断的唯一入口：同租户 + ACL 与用户的主体有交集。

    注意第一个条件：不同租户里完全可能都有一个叫 "all-staff" 的组。
    只比较组名、不比较租户，就会让 A 公司的"全员"读到 B 公司的"全员"文档。
    """
    return doc.tenant_id == user.tenant_id and bool(doc.allowed & user.principals)


def query_tokens(query: str) -> list[str]:
    return [t for t in dict.fromkeys(tokenize(query)) if t not in STOP_TOKENS]


def rank(query: str, docs: Sequence[Document]) -> list[Hit]:
    """对**传进来的这批候选**打分并排序（简化版 TF-IDF，标题权重 ×2）。

    IDF 只在 docs 这批候选上统计 —— 这一点和权限有关：如果把用户无权看的文档也算进统计量，
    它们会改变可见文档的分数和排序，这本身就是一条（很细的）信息泄露通道。
    所以正确的顺序是"先过滤出候选，再调用 rank"。
    """
    q = query_tokens(query)
    if not q or not docs:
        return []
    bags = [Counter(tokenize(d.title) * 2 + tokenize(d.text)) for d in docs]
    n = len(docs)
    df = Counter(t for bag in bags for t in q if bag[t])
    hits = []
    for doc, bag in zip(docs, bags):
        score = sum(math.log(1 + n / df[t]) * (1 + math.log(bag[t])) for t in q if bag[t])
        if score > 0:
            hits.append(Hit(doc, round(score, 4)))
    hits.sort(key=lambda h: (-h.score, h.doc.doc_id, -h.doc.version))  # 分数相同按 doc_id 排，保证结果确定
    return hits


class ACLIndex:
    """只追加的文档索引：更新 = 追加一个新版本，删除 = 追加一条墓碑。"""

    def __init__(self, docs: Iterable[Document] = ()):
        self.docs: list[Document] = list(docs)

    def add(self, doc: Document) -> Document:
        self.docs.append(doc)
        return doc

    def latest(self, tenant_id: str, doc_id: str) -> Document | None:
        versions = [d for d in self.docs if d.tenant_id == tenant_id and d.doc_id == doc_id]
        return max(versions, key=lambda d: d.version) if versions else None

    def update(self, tenant_id: str, doc_id: str, updated_at: str, **changes) -> Document:
        """基于最新版本生成下一个版本（改正文、改标题、改 ACL 都是"更新"）。"""
        cur = self.latest(tenant_id, doc_id)
        if cur is None:
            raise KeyError(doc_id)
        if "allowed" in changes:
            changes["allowed"] = frozenset(changes["allowed"])
        return self.add(replace(cur, version=cur.version + 1, updated_at=updated_at, **changes))

    def delete(self, tenant_id: str, doc_id: str, updated_at: str) -> Document:
        """删除 = 追加一条版本号更大的墓碑，而不是把记录直接抹掉。

        为什么？同步管道里的事件可能乱序到达：如果直接抹掉，一条迟到的"v2 更新"事件
        会让已删除的文档"复活"。有了墓碑，迟到的旧版本比较版本号就知道自己已经过时了。
        （真正释放存储空间的物理删除，由后台任务在确认没有迟到事件之后再做。）
        """
        cur = self.latest(tenant_id, doc_id)
        if cur is None:
            raise KeyError(doc_id)
        return self.add(replace(cur, text="", version=cur.version + 1, updated_at=updated_at, deleted=True))

    # ------------------------------------------------------------------ 两种朴素的过滤方式

    def post_filter_search(self, query: str, user: User, k: int = 3) -> tuple[list[Hit], list[Hit]]:
        """❌ 反面教材：检索后过滤（post-filter）。

        先在本租户**全部**文档上打分取 top-k，再把用户无权看的剔掉。返回 (留下的, 被剔掉的)。
        问题一：结果会"缩水"—— top-k 里有几篇无权文档，用户就少拿几篇，哪怕还有很多他能看的相关文档；
        问题二：打分时用到了无权文档（IDF、排名），调用方还很容易把"被剔掉的"泄露出去（数量、标题）。
        """
        candidates = [d for d in self.docs if d.tenant_id == user.tenant_id and not d.deleted]
        top = rank(query, candidates)[:k]
        return [h for h in top if can_read(user, h.doc)], [h for h in top if not can_read(user, h.doc)]

    def pre_filter_search(self, query: str, user: User, k: int = 3) -> list[Hit]:
        """✅ 检索前过滤（pre-filter）：先按租户 + ACL 圈出候选，再打分取 top-k。

        只要用户有权看的相关文档不少于 k 篇，就一定能拿满 k 篇；无权文档从头到尾不参与计算。
        ⚠️ 仍然不完整：它把索引里的每个版本都当成独立文档，也不认识墓碑之前的旧版本 ——
        文档更新后新旧版本会同时出现，权限收回后旧版本还能被搜到，删除后旧版本也还在。练习 (a) 修好它。
        """
        candidates = [d for d in self.docs if can_read(user, d) and not d.deleted]
        return rank(query, candidates)[:k]


# ---------------------------------------------------------------------- 入库扫描与展示

_HIDDEN_PATTERNS = [
    (re.compile(r"<!--.*?-->", re.S), "HTML 注释（页面上看不见，模型却读得到）"),
    (re.compile(r"[​-‏⁠﻿]"), "零宽字符（常用来藏字）"),
    (re.compile(r"(AI|ai|人工智能|大模型)\s*(助手|assistant|Assistant)?\s*(必须|务必|请注意|注意|请)"), "直接对 AI 喊话"),
]


def screen_document(doc: Document) -> list[str]:
    """入库时扫描（问题卡片 6 方案 A）：返回发现的可疑点，空列表 = 没发现。

    只能拦住"长得像攻击"的内容。一段写得一本正经的错误事实（"报销上限已调整为 5000 元"）
    不含任何指令特征，这里一定查不出来 —— 那要靠来源可信度分级、引用和权限来兜底。
    """
    text = f"{doc.title}\n{doc.text}"
    findings = [f"疑似提示词注入：{hit}" for hit in detect_injection(text)]
    findings += [label for pattern, label in _HIDDEN_PATTERNS if pattern.search(text)]
    return findings


def format_hit(hit: Hit) -> str:
    """把一条检索结果渲染成给模型看的文本：来源编号放最前面，方便模型引用、方便代码校验引用。"""
    d = hit.doc
    meta = f"v{d.version}，更新于 {d.updated_at}，来源 {d.source}，可信度 {d.trust}"
    return f"[{d.doc_id}] {d.title}（{meta}）\n{d.text}"
