"""长期记忆：跨会话记住用户的信息。

短期记忆 = 当前对话的消息历史（context.py 管理）。
长期记忆 = 存在外部的、跨会话的信息，需要时检索出来放进上下文（本质上就是 RAG）。

企业级长期记忆的三个硬要求：
1. 隔离：记忆必须按 (tenant_id, user_id) 严格隔离。A 公司的数据出现在 B 公司的回答里 = 重大事故；
2. 可删除：用户有权要求删除（GDPR / 个人信息保护法的"被遗忘权"）；
3. 防投毒：写入的内容可能来自恶意输入，检索出来时仍要当作"不可信数据"对待。

检索这里用零依赖的关键词打分（中文用字的二元组）；生产中换成
向量检索 + 关键词（BM25）的混合检索，再加重排序（rerank）。
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import Field

from .tools import Tool, ToolContext, ToolError, tool


@dataclass
class MemoryItem:
    text: str
    tenant_id: str
    user_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


def tokenize(text: str) -> list[str]:
    """英文按单词，中文按相邻两字（bigram）切分。"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    for run in re.findall(r"[一-鿿]+", text):
        tokens += list(run) if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
    return tokens


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.items: list[MemoryItem] = []
        if self.path and self.path.exists():
            self.items = [MemoryItem(**d) for d in json.loads(self.path.read_text(encoding="utf-8"))]

    def _persist(self) -> None:
        if self.path:
            self.path.write_text(json.dumps([asdict(i) for i in self.items], ensure_ascii=False, indent=2), encoding="utf-8")

    def _scope(self, tenant_id: str, user_id: str) -> list[MemoryItem]:
        return [i for i in self.items if i.tenant_id == tenant_id and i.user_id == user_id]

    def add(self, tenant_id: str, user_id: str, text: str, tags: list[str] | None = None) -> MemoryItem:
        item = MemoryItem(text=text, tenant_id=tenant_id, user_id=user_id, tags=tags or [])
        self.items.append(item)
        self._persist()
        return item

    def search(self, tenant_id: str, user_id: str, query: str, k: int = 3) -> list[MemoryItem]:
        """在"本租户本用户"的范围内按关键词相关度检索（简化版 TF-IDF）。"""
        scope = self._scope(tenant_id, user_id)
        q = set(tokenize(query))
        if not scope or not q:
            return []
        docs = [Counter(tokenize(i.text)) for i in scope]
        df = Counter(t for d in docs for t in set(d))
        scored = []
        for item, d in zip(scope, docs):
            score = sum(math.log(1 + len(docs) / df[t]) * (1 + math.log(d[t])) for t in q if t in d)
            if score > 0:
                scored.append((score, item.created_at, item))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item for _, _, item in scored[:k]]

    def forget(self, tenant_id: str, user_id: str, item_id: str) -> bool:
        before = len(self.items)
        self.items = [
            i for i in self.items if not (i.id == item_id and i.tenant_id == tenant_id and i.user_id == user_id)
        ]
        self._persist()
        return len(self.items) < before


def memory_tools(store: MemoryStore) -> list[Tool]:
    """把记忆库暴露为两个工具。注意 tenant_id / user_id 来自 ctx，模型无法伪造。"""

    def _who(ctx: ToolContext) -> tuple[str, str]:
        if not ctx.tenant_id or not ctx.user_id:
            raise ToolError("当前会话没有用户身份，无法使用长期记忆")
        return ctx.tenant_id, ctx.user_id

    @tool(risk="write")
    def remember(
        fact: Annotated[str, Field(description="要长期记住的一条关于用户的事实或偏好，写成完整的一句话")],
        ctx: ToolContext,
    ) -> str:
        """把关于当前用户的重要事实/偏好存入长期记忆（跨会话有效）。只存用户明确表达的、以后有用的信息。"""
        tenant, uid = _who(ctx)
        item = store.add(tenant, uid, fact)
        return f"已记住（id={item.id}）"

    @tool
    def recall(
        query: Annotated[str, Field(description="要检索的主题关键词")],
        ctx: ToolContext,
    ) -> str:
        """从当前用户的长期记忆中检索相关信息。"""
        tenant, uid = _who(ctx)
        hits = store.search(tenant, uid, query)
        return "\n".join(f"- {h.text}" for h in hits) or "没有相关记忆"

    return [remember, recall]
