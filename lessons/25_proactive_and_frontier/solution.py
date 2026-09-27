"""第 25 课练习参考答案。先自己做，再来对照。"""

from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


proactive_kit = _load_sibling("proactive_kit")

CONF_FLOOR = proactive_kit.CONF_FLOOR
CONF_CEIL = proactive_kit.CONF_CEIL
DAY = proactive_kit.DAY
Evidence = proactive_kit.Evidence
Belief = proactive_kit.Belief
UserModel = proactive_kit.UserModel
Context = proactive_kit.Context
Limits = proactive_kit.Limits
Decision = proactive_kit.Decision


# =====================================================================
# 练习 (a)：update_belief
# =====================================================================


def _clamp(p: float) -> float:
    return min(max(p, CONF_FLOOR), CONF_CEIL)


def _sigmoid(x: float) -> float:
    # 分两支：x ≥ 0 时 exp(-x) ≤ 1；x < 0 时 exp(x) < 1。两支都不会溢出（x = ±inf 也没问题）
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def update_belief(prior: float, evidence_weight: float, supports: bool) -> float:
    if math.isnan(prior) or math.isnan(evidence_weight):
        raise ValueError("prior / evidence_weight 不能是 NaN")
    if evidence_weight < 0:
        raise ValueError("evidence_weight 必须 ≥ 0；反证请用 supports=False 表达")
    p = _clamp(prior)
    x = math.log(p / (1.0 - p)) + (evidence_weight if supports else -evidence_weight)
    return _clamp(_sigmoid(x))


# =====================================================================
# 练习 (b)：should_interrupt
# =====================================================================


def _in_quiet_hours(now: int, limits: Limits) -> bool:
    m = now % DAY
    start, end = limits.quiet_start, limits.quiet_end
    if start == end:
        return False
    if start < end:
        return start <= m < end
    return m >= start or m < end  # 跨午夜：22:00–08:00


def should_interrupt(
    benefit: float,
    confidence: float,
    cost: float,
    context: Context,
    recent_interrupts: list[int],
    limits: Limits,
) -> Decision:
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence 必须在 [0, 1] 内，收到 {confidence}")
    if benefit < 0 or cost < 0:
        raise ValueError("benefit 和 cost 不能为负")

    base = benefit * confidence - cost
    if context.urgent:
        if confidence < limits.urgent_min_confidence:
            return Decision("defer", "urgent_low_confidence", base)
        if base > limits.threshold:
            return Decision("interrupt", "urgent_override", base)
        return Decision("drop", "not_worth_it", base)

    if base <= limits.threshold:
        return Decision("drop", "not_worth_it", base)
    if _in_quiet_hours(context.now, limits):
        return Decision("defer", "quiet_hours", base)

    multiplier = 1.0
    if context.focus:
        multiplier = max(multiplier, limits.focus_multiplier)
    if context.in_meeting:
        multiplier = max(multiplier, limits.meeting_multiplier)
    score = benefit * confidence - cost * multiplier
    if score <= limits.threshold:
        return Decision("defer", "busy", score)

    recent = [t for t in recent_interrupts if context.now - 60 < t <= context.now]
    if len(recent) >= limits.max_per_hour:
        return Decision("defer", "rate_limited", score)
    return Decision("interrupt", "worth_it", score)


# =====================================================================
# 练习 (c)：forget / explain
# =====================================================================


def forget(model: UserModel, key: str) -> bool:
    existed = model.beliefs.pop(key, None) is not None
    model.blocked.add(key)  # 墓碑：不拉黑的话，同样的信号很快又会把它"学"回来
    return existed


def explain(model: UserModel, key: str) -> dict:
    belief = model.beliefs.get(key)
    if belief is None:
        raise KeyError(key)
    evidence = [asdict(e) for e in sorted(belief.evidence, key=lambda e: e.t)]
    n_support = sum(1 for e in evidence if e["supports"])
    n_refute = len(evidence) - n_support
    basis = f"{n_support} 条支持、{n_refute} 条反对" if evidence else "没有任何证据，只是先验"
    if belief.pinned:
        basis += "（已被你亲口纠正并锁定）"
    return {
        "key": belief.key,
        "statement": belief.statement,
        "confidence": round(belief.confidence, 3),
        "pinned": belief.pinned,
        "n_support": n_support,
        "n_refute": n_refute,
        "evidence": evidence,
        "summary": f"我有 {round(belief.confidence * 100)}% 的把握认为：{belief.statement}。依据：{basis}。",
    }
