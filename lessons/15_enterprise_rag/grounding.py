"""第 15 课：简单的引用校验（grounding check）。

要求模型"每句话后面标注来源编号"只是第一步 —— 模型同样会编造引用：
引一个根本没检索到的编号，或者引了真实的文档、说的却是文档里没有的内容。
所以引用必须由**代码**来核对。本文件提供：

    split_claims       把回答拆成一条条"论断"，并取出每条后面的引用编号
    content_tokens     论断 / 片段的内容词（复用 agentkit.memory.tokenize，去掉虚词）
    coverage           论断的内容词有多大比例能在被引片段里找到（词重叠）
    extract_numbers    取出文本里的数字（金额、天数、比例最容易被模型"改一下"）
    is_refusal         这句话是不是"没找到 / 不知道"式的拒答
    verify_citations   简单版校验：只检查"引了的"——编号存不存在、词重叠够不够

verify_citations 有一个大盲区：**没带引用的句子它根本不看**，而编造最常藏在这种句子里。
练习 (c) 的 check_citations 要补上这个盲区，并加上数字核对。

局限（务必知道）：词重叠是很粗的信号。换个说法（同义改写）会被误判为"不支持"；
"可以结转"和"不可以结转"的词重叠却几乎是 100%。生产中会再叠加 NLI 模型或 LLM 评审（第 11 课）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from agentkit.memory import tokenize

# 引用标记：[HR-001]、[HR-001, HR-002]、【HR-001】都认。编号必须以字母或数字开头。
_ID = r"[A-Za-z0-9][A-Za-z0-9_.:#-]*"
CITATION_RE = re.compile(rf"[\[【]\s*({_ID}(?:\s*[,，;；、]\s*{_ID})*)\s*[\]】]")
# 一句话 = 若干非句末字符 + 句末标点（或行尾）+ 紧跟在句末标点后面的引用标记
_SENTENCE_RE = re.compile(r"[^。！？!?；;]+(?:[。！？!?；;]+|$)(?:\s*[\[【][^\]】]*[\]】])*")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)]|[（(]\d+[)）])\s*")

# 论断里常见、但不承载事实的词（二元组）
_FILLER = {
    "根据", "可以", "以及", "如果", "需要", "我们", "您的", "你的", "目前", "一个", "这个", "已经", "进行",
    "相关", "具体", "情况", "文档", "制度", "规定", "显示", "提到", "注明", "说明", "另外", "此外", "其中",
}

_REFUSAL_PHRASES = (
    "没有找到", "未找到", "找不到", "没有检索到", "未检索到", "无法确定", "无法回答", "不知道",
    "没有相关", "未提及", "没有提到", "没有说明", "暂无相关",
)


@dataclass
class Claim:
    text: str  # 去掉引用标记之后的句子
    citations: list[str] = field(default_factory=list)  # 这句话引用的来源编号（按出现顺序去重）


def _is_structural(line: str) -> bool:
    """标题、表格分隔线、代码围栏这类"排版行"不是论断。"""
    s = line.strip()
    return not s or s.startswith("#") or s.startswith("```") or bool(re.fullmatch(r"[|:\-\s]+", s))


def split_claims(answer: str) -> list[Claim]:
    """把回答拆成论断。规则：
    - 按行处理，去掉列表符号（"- ""1. "）；跳过标题、表格分隔线、代码围栏；
    - 行内按句末标点（。！？!?；;）切句，句末标点后面紧跟的引用标记归属于这一句；
    - 以冒号结尾且没有引用的句子（"报销标准如下："）只是引导语，跳过；
    - 只有引用标记、没有内容的片段（模型把引用单独放一行），并入上一条论断。
    """
    claims: list[Claim] = []
    for line in (answer or "").splitlines():
        if _is_structural(line):
            continue
        line = _BULLET_RE.sub("", line)
        for m in _SENTENCE_RE.finditer(line):
            sentence = m.group(0).strip()
            ids: list[str] = []
            for group in CITATION_RE.findall(sentence):
                ids += [i.strip() for i in re.split(r"[,，;；、]", group) if i.strip()]
            ids = list(dict.fromkeys(ids))
            text = re.sub(r"\s+(?=[。！？!?；;，,])", "", CITATION_RE.sub("", sentence)).strip()
            if not re.search(r"[\w一-鿿]", text):  # 没有实际内容
                if ids and claims:
                    claims[-1].citations = list(dict.fromkeys(claims[-1].citations + ids))
                continue
            if not ids and text.rstrip().endswith((":", "：")):
                continue
            claims.append(Claim(text, ids))
    return claims


def content_tokens(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in _FILLER}


def coverage(claim: str, source_texts: Iterable[str]) -> float:
    """论断的内容词里，有多少比例出现在（所有）被引片段中。论断没有内容词时返回 1.0。"""
    claim_tokens = content_tokens(claim)
    if not claim_tokens:
        return 1.0
    pool: set[str] = set()
    for s in source_texts:
        pool |= content_tokens(s)
    return len(claim_tokens & pool) / len(claim_tokens)


def _norm_number(n: str) -> str:
    if "." in n:
        return n.rstrip("0").rstrip(".") or "0"
    return n.lstrip("0") or "0"


# 紧跟在字母后面的数字（HR-010、v2、Q4）是编号，不是数量，不参与数字核对
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])(?<![A-Za-z]-)\d+(?:\.\d+)?")


def extract_numbers(text: str) -> set[str]:
    """取出表示数量的数字并归一化："1,200" → "1200"，"09" → "9"，"600.00" → "600"。

    HR-010、v2、Q4 这类"字母 + 数字"的编号会被跳过 —— 否则片段编号 [HR-010] 里的 10
    会让"预算为 10%"这种编造的数字蒙混过关。
    """
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    return {_norm_number(n) for n in _NUMBER_RE.findall(text)}


def is_refusal(text: str) -> bool:
    return any(p in text for p in _REFUSAL_PHRASES)


def verify_citations(answer: str, sources: dict[str, str], min_coverage: float = 0.5) -> list[str]:
    """简单版引用校验：对**带了引用**的每条论断，检查
    1. 引用的编号是否都在 sources 里（sources = 本次运行实际检索到的片段，{编号: 片段文本}）；
    2. 论断与被引片段的词重叠是否 >= min_coverage。
    返回问题列表（空列表 = 没发现问题）。没带引用的句子不检查 —— 这是它的盲区。
    """
    problems = []
    for c in split_claims(answer):
        if not c.citations:
            continue
        unknown = [i for i in c.citations if i not in sources]
        if unknown:
            problems.append(f"引用了本次没有检索到的来源 {unknown}：{c.text}")
            continue
        cov = coverage(c.text, [sources[i] for i in c.citations])
        if cov < min_coverage:
            problems.append(f"与被引片段的词重叠只有 {cov:.0%}：{c.text}")
    return problems
