"""第 12 课练习 —— 参考答案。

    (a) SecureIndex.search  —— 租户隔离 + ACL 预过滤 + 同一文档只取最新版本 + 不泄露任何无权文档的信息
    (b) chunk_markdown      —— 先按标题、段落切块，过长再按长度切，切开的地方保留重叠
    (c) check_citations     —— 严格的引用校验：该引的有没有引、引的存不存在、数字对不对、词重叠够不够

先自己做 exercise.py，卡住超过 15 分钟再来看。对照时重点看 docstring 里的"为什么"。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))  # 让同目录的 acl_index.py / grounding.py 可以直接 import

from acl_index import ACLIndex, Document, Hit, User, can_read, rank  # noqa: E402,F401
from grounding import coverage, extract_numbers, is_refusal, split_claims  # noqa: E402,F401

# =====================================================================
# 练习 (a)：SecureIndex.search —— 不越权、不过期、不泄露
# =====================================================================


class SecureIndex(ACLIndex):
    """在 ACLIndex（只追加的索引：每个版本、每条删除墓碑都是 self.docs 里的一条记录）上实现安全检索。"""

    def search(self, query: str, user: User, k: int = 3) -> list[Hit]:
        """返回 user 有权查看的、最相关的至多 k 篇文档（每篇只出现一次，且是最新版本）。

        必须满足：
          1. 租户隔离：只看 user.tenant_id 的文档（别的租户里即使有同名的组，也不相干）；
          2. 只取最新版本：同一个 doc_id 在 self.docs 里可能有多个版本，只保留 version 最大的那个；
             如果最新版本是墓碑（deleted=True），这篇文档就当作不存在 —— **不能退回去用旧版本**；
          3. 权限看最新版本：先确定最新版本，再用 can_read(user, 最新版本) 判断。
             顺序反过来（先按权限过滤再取最新）会出事：v2 收回了你的权限，你却还能搜到 v1；
          4. 检索前过滤（pre-filter）：先圈出"可见的最新版本"作为候选，再调用 rank(query, 候选)，取前 k 个。
             这样只要可见的相关文档不少于 k 篇，就一定能拿满 k 篇；
          5. 不泄露：返回值里只有可见文档。更严格地说，**无权文档存在与否，不能让结果有任何不同**
             （条数、顺序、分数都一样）—— 所以打分用的统计量也只能来自可见候选，rank 已经保证了这一点，
             前提是你只把可见候选传给它。
          6. k <= 0 时返回空列表。

        提示：用一个 dict 记录 {doc_id: 该租户下版本号最大的那条记录}，遍历一遍 self.docs 即可。
        """
        if k <= 0:
            return []
        latest: dict[str, Document] = {}
        for d in self.docs:
            if d.tenant_id != user.tenant_id:
                continue
            cur = latest.get(d.doc_id)
            if cur is None or d.version > cur.version:
                latest[d.doc_id] = d
        visible = [d for d in latest.values() if not d.deleted and can_read(user, d)]
        return rank(query, visible)[:k]


# =====================================================================
# 练习 (b)：chunk_markdown —— 按结构切块
# =====================================================================


@dataclass
class Chunk:
    text: str
    heading: str = ""  # 这个块所属小节的标题文字（不含 #）；文档开头、第一个标题之前的内容为 ""


def fixed_size_chunks(text: str, size: int, overlap: int = 0) -> list[str]:
    """（已写好）固定长度切块：每块 size 个字符，相邻两块重叠 overlap 个字符。

    例：fixed_size_chunks("abcdefgh", 4, 1) == ["abcd", "defg", "gh"]
    最后一块到达文本末尾就停，不会产生一个完全被上一块包含的"尾巴"。
    """
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError(f"需要 size > 0 且 0 <= overlap < size，收到 size={size}, overlap={overlap}")
    if not text:
        return []
    pieces, start = [], 0
    while True:
        pieces.append(text[start : start + size])
        if start + size >= len(text):
            return pieces
        start += size - overlap


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def chunk_markdown(text: str, max_chars: int = 500, overlap: int = 50) -> list[Chunk]:
    """把 Markdown 文档切成块：结构优先，长度兜底。

    规则（按顺序）：
      1. 小节：Markdown 标题行（1~6 个 # 加空格开头，见 HEADING_RE）开启一个新小节。
         标题文字存进 Chunk.heading，标题行本身不放进 text。**一个块永远不跨两个小节。**
      2. 段落：小节内以空行分隔段落。每个段落 strip() 后使用，空段落丢弃。
      3. 代码块：``` 开头的行到下一个 ``` 行（含两端围栏行）是一个完整的段落 ——
         里面的空行不分段，里面以 # 开头的行（比如 bash / Python 注释）也**不是**标题。
         代码块前后即使没有空行，也和相邻文字分成不同的段落。
      4. 合并：小节内按顺序把相邻段落用 "\\n\\n" 拼起来，只要拼完长度 <= max_chars 就继续拼，
         否则先输出当前块，再从这个段落开始新块。
      5. 超长段落：单个段落长度 > max_chars 时，先输出手上已经拼好的块，然后用
         fixed_size_chunks(段落, max_chars, overlap) 把它切成若干块（相邻块重叠 overlap 个字符），
         每一块单独输出，不再和后面的段落合并。
      6. overlap 必须满足 0 <= overlap < max_chars，否则抛 ValueError（在处理任何内容之前就检查）。

    为什么重叠只用在第 5 步？在标题、段落这些"自然边界"切开时，语义本来就是完整的，
    重叠只会制造冗余；只有被迫在句子中间切开时，重叠才能把被切断的上下文补回来。
    """
    if not 0 <= overlap < max_chars:
        raise ValueError(f"需要 0 <= overlap < max_chars，收到 overlap={overlap}, max_chars={max_chars}")

    # 第一遍：把文档解析成 [(小节标题, [段落, ...]), ...]
    sections: list[tuple[str, list[str]]] = []
    heading, paragraphs, buf, in_code = "", [], [], False

    def end_paragraph() -> None:
        para = "\n".join(buf).strip()
        if para:
            paragraphs.append(para)
        buf.clear()

    for line in text.splitlines():
        if line.strip().startswith("```"):
            if not in_code:
                end_paragraph()  # 代码块开始：前面的文字自成一段
            buf.append(line)
            in_code = not in_code
            if not in_code:
                end_paragraph()  # 代码块结束：整个代码块是一段
            continue
        if in_code:
            buf.append(line)
            continue
        m = HEADING_RE.match(line)
        if m:
            end_paragraph()
            sections.append((heading, paragraphs))
            heading, paragraphs = m.group(2), []
            continue
        if not line.strip():
            end_paragraph()
            continue
        buf.append(line)
    end_paragraph()
    sections.append((heading, paragraphs))

    # 第二遍：小节内贪心合并，超长段落按长度切
    chunks: list[Chunk] = []
    for heading, paras in sections:
        current = ""
        for p in paras:
            if len(p) > max_chars:
                if current:
                    chunks.append(Chunk(current, heading))
                    current = ""
                chunks += [Chunk(piece, heading) for piece in fixed_size_chunks(p, max_chars, overlap)]
            elif not current:
                current = p
            elif len(current) + 2 + len(p) <= max_chars:
                current += "\n\n" + p
            else:
                chunks.append(Chunk(current, heading))
                current = p
        if current:
            chunks.append(Chunk(current, heading))
    return chunks


# =====================================================================
# 练习 (c)：check_citations —— 严格的引用校验
# =====================================================================

SUPPORTED = "supported"  # 有引用、编号存在、数字对得上、词重叠够
REFUSAL = "refusal"  # 没有引用，但这句话本身就是"没找到 / 不知道"式的拒答 —— 允许
NO_CITATION = "no_citation"  # 陈述了事实却没有引用
UNKNOWN_SOURCE = "unknown_source"  # 引用了本次没有检索到的编号（编造的，或者是别人的文档）
NUMBER_MISMATCH = "number_mismatch"  # 论断里的数字在被引片段里找不到
LOW_OVERLAP = "low_overlap"  # 与被引片段的词重叠低于阈值


@dataclass
class ClaimVerdict:
    claim: str
    citations: list[str]
    status: str
    coverage: float | None = None  # 只有走到"词重叠"那一步才有值
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (SUPPORTED, REFUSAL)


@dataclass
class CitationReport:
    verdicts: list[ClaimVerdict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """至少有一条论断，并且每一条都通过。空回答不算通过。"""
        return bool(self.verdicts) and all(v.ok for v in self.verdicts)

    @property
    def problems(self) -> list[ClaimVerdict]:
        return [v for v in self.verdicts if not v.ok]


def check_citations(answer: str, sources: dict[str, str], min_coverage: float = 0.5) -> CitationReport:
    """逐条校验回答里的论断。sources = 本次运行实际检索到的片段 {编号: 片段文本}。

    用 split_claims(answer) 拆出论断，对每条论断按下面的顺序判断，命中第一条就定案：
      1. 没有引用：如果 is_refusal(论断) → REFUSAL，否则 → NO_CITATION；
      2. 引用的编号里有不在 sources 中的 → UNKNOWN_SOURCE
         （注意：sources 是"本次检索到的"，不是"整个知识库"。引一篇用户这次没检索到的文档，
          要么是编造，要么是模型从别处"记得"的内容 —— 都不能算有依据）；
      3. extract_numbers(论断) 中有任何一个数字不在"所有被引片段的数字并集"里 → NUMBER_MISMATCH；
      4. coverage(论断, 所有被引片段) < min_coverage → LOW_OVERLAP，并把覆盖率记进 coverage 字段；
      5. 否则 → SUPPORTED（同样记录 coverage）。
    多个引用时，用的是所有被引片段的**并集**（一句话综合了两个来源是正常的）。
    """
    verdicts: list[ClaimVerdict] = []
    for c in split_claims(answer):
        if not c.citations:
            verdicts.append(ClaimVerdict(c.text, [], REFUSAL if is_refusal(c.text) else NO_CITATION))
            continue
        unknown = [i for i in c.citations if i not in sources]
        if unknown:
            verdicts.append(ClaimVerdict(c.text, c.citations, UNKNOWN_SOURCE, detail=f"本次没有检索到：{', '.join(unknown)}"))
            continue
        cited = [sources[i] for i in c.citations]
        source_numbers: set[str] = set().union(*(extract_numbers(s) for s in cited))
        missing = extract_numbers(c.text) - source_numbers
        if missing:
            verdicts.append(
                ClaimVerdict(c.text, c.citations, NUMBER_MISMATCH, detail=f"被引片段里没有：{', '.join(sorted(missing))}")
            )
            continue
        cov = coverage(c.text, cited)
        status = SUPPORTED if cov >= min_coverage else LOW_OVERLAP
        verdicts.append(ClaimVerdict(c.text, c.citations, status, round(cov, 3)))
    return CitationReport(verdicts)
