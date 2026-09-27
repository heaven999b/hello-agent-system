"""第 07 课练习：把"工程考量"变成可执行的规则。

一共三题：
  (a) evaluate + triggered_considerations   情境触发规则引擎：画像 → 本项目适用的考量点 + 触发原因
  (b) prioritize                             排序：严重级别 → 场景权重 → id，结果稳定、可复现
  (c) coverage_report                        设计文档查漏：用标题 / 关键词匹配，找出没覆盖的项

开始之前，先读 perspectives.py 的开头（数据结构和条件格式都在那里），不长。

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    .venv/bin/python -m pytest lessons/07_engineering_perspectives -v

测试里有 3 个"一致性检查"会用你写的函数去检查本课的 README 与 perspectives.py 是否一致 ——
你的代码写对了，它们才会通过。卡住了？先重读 README 第 6 节，再看 solution.py。
"""

from __future__ import annotations

import importlib.util
import re  # noqa: F401  （练习 c 会用到）
import sys
import unicodedata  # noqa: F401  （练习 c 会用到）
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。

    用带课号前缀的名字注册进 sys.modules：只加载一次，也不会和其他课程的同名文件冲突。
    """
    key = f"lesson07_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


P = _load_sibling("perspectives")

Consideration = P.Consideration
Triggered = P.Triggered
Ranked = P.Ranked
FocusRule = P.FocusRule
Requirement = P.Requirement
Evidence = P.Evidence
CoverageReport = P.CoverageReport
UnknownFactError = P.UnknownFactError


# =====================================================================
# 练习 (a)：情境触发规则引擎
# =====================================================================


def evaluate(cond: dict, profile: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """对画像求值一个条件，返回 (是否成立, 触发原因列表)。

    条件有四种形态（见 perspectives.py 开头）：

    1. 叶子：{"fact": 名字} 或 {"fact": 名字, "op": 运算符, "value": 值}
       - 名字不在 profile 里 → raise UnknownFactError(名字)。
         （为什么不当作 False？画像"忘了填"不等于"不需要考虑"。静默当 False，一整组检查就悄悄消失了。）
       - 没有 "op"：当且仅当 profile[名字] **is True** 时成立。
         注意是 `is True`，不是"真值"：LLM 抽取的画像里可能出现字符串 "false"，而 bool("false") 是 True。
         如果有 "value" 却没有 "op"，条件写错了 → raise ValueError。
       - 有 "op"：op 必须是 P.OPS 的键之一（== != > >= < <=），否则 raise ValueError；
         缺 "value" 也 raise ValueError。
         value 可以是常量，也可以是对另一个事实的引用 {"fact": "另一个名字"}
         （判断方法：value 是 dict 且键恰好只有 "fact"）。引用的事实不存在同样 raise UnknownFactError。
         结果 = P.OPS[op](profile[名字], 解析后的 value)。
       - 成立时，原因列表 = [P.describe_leaf(cond, profile)]；不成立时为 []。

    2. {"all": [子条件...]}：全部成立才成立；成立时原因 = 所有子条件原因按顺序拼接；不成立时为 []。
    3. {"any": [子条件...]}：任一成立即成立；原因 = **成立的**子条件的原因按顺序拼接。
       all 和 any 都**不要短路**：每个子条件都要求值，这样画像里缺的事实一定会被发现。
       （空列表：all([]) 为 True，any([]) 为 False，和 Python 内置一致。）
    4. {"not": 子条件}：取反。成立时原因 = [P.describe_not(子条件)]。

    其他任何形态（不是 dict、没有这四个键）→ raise ValueError。
    """
    raise NotImplementedError("TODO: 练习 (a) —— 实现条件求值")


def triggered_considerations(profile: Mapping[str, Any], catalog: Sequence[Consideration]) -> list[Triggered]:
    """返回适用于这个画像的全部考量点（保持 catalog 原有顺序）。

    - item.when 为 None（通用必查点）：一定适用，原因 = (P.GENERAL_REASON,)
    - 否则用 evaluate 求值；成立则适用，原因 = evaluate 返回的原因（转成 tuple）
    - catalog 里有重复的 id → raise ValueError（目录本身写错了，不能带病运行）

    每一项包装成 Triggered(item, reasons)。
    """
    raise NotImplementedError("TODO: 练习 (a) —— 实现规则引擎")


# =====================================================================
# 练习 (b)：排序
# =====================================================================


def prioritize(
    items: Iterable[Triggered],
    profile: Mapping[str, Any],
    focus_rules: Sequence[FocusRule] | None = None,
) -> list[Ranked]:
    """把 triggered_considerations 的结果排成"先做什么、后做什么"。

    1. 场景权重：focus_rules 为 None 时用 P.FOCUS_RULES。先用 evaluate 找出**成立**的规则，
       一个考量点的权重 = 所有成立规则的 weights 里、它所在维度（item.dimension）的值之和；没有就是 0。
    2. 排序键，依次比较：
         严重级别（P0 < P1 < P2，用 P.SEVERITY_ORDER）→ 权重（大的在前）→ id（字典序）
       级别不是 P0/P1/P2 → raise ValueError。
       注意最后一级用 id 而不是"输入顺序"：同样的一组条目，无论以什么顺序传进来，结果都必须一模一样
       —— 这就是"可复现"。两个人对同一个项目各跑一次，拿到的清单应该逐行相同，才能拿去评审会上讨论。
    3. 同一个 id 出现多次（比如合并了两份目录的结果）：只保留一条，原因按首次出现的顺序合并去重。
    4. 返回 Ranked(rank, item, reasons, weight) 的列表，rank 从 1 开始连续编号。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 实现排序")


# =====================================================================
# 练习 (c)：设计文档覆盖检查
# =====================================================================


def coverage_report(design_doc_text: str, required_items: Sequence[Requirement]) -> CoverageReport:
    """检查一份 Markdown 设计文档是否覆盖了 required_items，返回 CoverageReport。

    规范化（比较之前，文档的每一行和每个标题 / 关键词都要这样处理）：
      unicodedata.normalize("NFKC", s).casefold()，再把连续空白压成一个空格并去掉首尾空白。
      （NFKC 让全角的"ＳＬＯ"和半角的"SLO"一样；casefold 让大小写不敏感。）

    逐行扫描文档（行号从 1 开始）：
      - 以 ``` 或 ~~~ 开头的行是代码块的开关：这一行本身跳过，并切换"在代码块里"的状态；
      - 规范化后为空的行跳过；
      - 规范化后包含 P.PLACEHOLDER_MARKERS 任意一个（todo、tbd、待补充、待填写）的行跳过 ——
        "成本：TODO" 说明还没想，不算覆盖；
      - 去掉首尾空白后以 # 开头、且**不在代码块里**的行是标题（代码块里的 # 是注释）。

    对每个 Requirement（按传入顺序）：
      1. 标题匹配：规范化后的 req.title 非空，且出现在某个标题行里 → 覆盖，how="heading"；
      2. 否则关键词匹配：任意一个（规范化后非空的）关键词出现在任意一行里 → 覆盖，how="keyword"；
      3. 都没有 → 放进 report.missing。
      证据取**最早**匹配的那一行：Evidence(how, 行号, 该行去掉首尾空白后的原文)。

    提醒：关键词命中只说明"提到了"，不说明"想清楚了"。这个工具的价值是快速发现**完全没提**的维度。
    """
    raise NotImplementedError("TODO: 练习 (c) —— 实现覆盖检查")
