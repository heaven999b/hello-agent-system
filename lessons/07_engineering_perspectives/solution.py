"""第 07 课练习参考答案。接口与 exercise.py 完全一致。"""

from __future__ import annotations

import importlib.util
import re
import sys
import unicodedata
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
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


def _lookup(profile: Mapping[str, Any], fact: str) -> Any:
    if fact not in profile:
        raise UnknownFactError(fact)
    return profile[fact]


def evaluate(cond: dict, profile: Mapping[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(cond, dict):
        raise ValueError(f"条件必须是 dict，实际是 {type(cond).__name__}")

    if "fact" in cond:
        actual = _lookup(profile, cond["fact"])
        if "op" not in cond:
            if "value" in cond:
                raise ValueError(f"条件有 value 却没有 op：{cond}")
            ok = actual is True  # 严格：字符串 "false"、数字 1 都不算 True
        else:
            op = cond["op"]
            if op not in P.OPS:
                raise ValueError(f"不支持的运算符 {op!r}")
            if "value" not in cond:
                raise ValueError(f"条件有 op 却没有 value：{cond}")
            value = cond["value"]
            if isinstance(value, dict) and set(value) == {"fact"}:
                value = _lookup(profile, value["fact"])
            ok = P.OPS[op](actual, value)
        return ok, ([P.describe_leaf(cond, profile)] if ok else [])

    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        # 不短路：每个子条件都求值，这样画像里缺失的事实一定会被发现
        results = [evaluate(child, profile) for child in cond[key]]
        if key == "all":
            ok = all(r for r, _ in results)
            reasons = [x for _, rs in results for x in rs] if ok else []
        else:
            ok = any(r for r, _ in results)
            reasons = [x for r, rs in results if r for x in rs]
        return ok, reasons

    if "not" in cond:
        inner, _ = evaluate(cond["not"], profile)
        ok = not inner
        return ok, ([P.describe_not(cond["not"])] if ok else [])

    raise ValueError(f"无法识别的条件：{cond}")


def triggered_considerations(profile: Mapping[str, Any], catalog: Sequence[Consideration]) -> list[Triggered]:
    seen: set[str] = set()
    out: list[Triggered] = []
    for item in catalog:
        if item.id in seen:
            raise ValueError(f"目录里有重复的 id：{item.id}")
        seen.add(item.id)
        if item.when is None:
            out.append(Triggered(item, (P.GENERAL_REASON,)))
            continue
        ok, reasons = evaluate(item.when, profile)
        if ok:
            out.append(Triggered(item, tuple(reasons)))
    return out


# =====================================================================
# 练习 (b)：排序
# =====================================================================


def prioritize(
    items: Iterable[Triggered],
    profile: Mapping[str, Any],
    focus_rules: Sequence[FocusRule] | None = None,
) -> list[Ranked]:
    rules = P.FOCUS_RULES if focus_rules is None else focus_rules
    active = [r for r in rules if evaluate(r.when, profile)[0]]

    merged: dict[str, tuple[Consideration, list[str]]] = {}
    for t in items:
        if t.item.severity not in P.SEVERITY_ORDER:
            raise ValueError(f"{t.item.id} 的级别 {t.item.severity!r} 不是 P0/P1/P2")
        if t.item.id not in merged:
            merged[t.item.id] = (t.item, [])
        reasons = merged[t.item.id][1]
        for r in t.reasons:
            if r not in reasons:
                reasons.append(r)

    def weight(item: Consideration) -> float:
        return sum(r.weights.get(item.dimension, 0) for r in active)

    rows = [(item, tuple(reasons), weight(item)) for item, reasons in merged.values()]
    rows.sort(key=lambda row: (P.SEVERITY_ORDER[row[0].severity], -row[2], row[0].id))
    return [Ranked(i, item, reasons, w) for i, (item, reasons, w) in enumerate(rows, start=1)]


# =====================================================================
# 练习 (c)：设计文档覆盖检查
# =====================================================================


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def coverage_report(design_doc_text: str, required_items: Sequence[Requirement]) -> CoverageReport:
    lines: list[tuple[int, str, str, bool]] = []  # (行号, 原文, 规范化, 是否标题)
    in_fence = False
    for no, raw in enumerate(design_doc_text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        norm = _normalize(raw)
        if not norm or any(m in norm for m in P.PLACEHOLDER_MARKERS):
            continue
        is_heading = (not in_fence) and stripped.startswith("#")
        lines.append((no, stripped, norm, is_heading))

    report = CoverageReport()
    for req in required_items:
        title = _normalize(req.title)
        evidence = None
        if title:
            for no, raw, norm, is_heading in lines:
                if is_heading and title in norm:
                    evidence = Evidence("heading", no, raw)
                    break
        if evidence is None:
            keywords = [k for k in (_normalize(k) for k in req.keywords) if k]
            for no, raw, norm, _ in lines:
                if any(k in norm for k in keywords):
                    evidence = Evidence("keyword", no, raw)
                    break
        if evidence is None:
            report.missing.append(req)
        else:
            report.covered[req.id] = evidence
    return report
