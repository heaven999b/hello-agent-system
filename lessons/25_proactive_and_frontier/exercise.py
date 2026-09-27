"""第 25 课练习：主动式 Agent 的三个核心零件。

  (a) update_belief       用一条证据更新置信度：对数几率形式的贝叶斯更新，夹在 [0.001, 0.999]，数值稳定
  (b) should_interrupt    该不该现在打扰：净收益阈值、勿扰时段（跨午夜）、专注 / 开会、频率上限、紧急事件越权
  (c) forget / explain    可解释、可删除的推断：explain 返回证据列表；forget 删除推断并拉黑

开始之前，先读 proactive_kit.py 里这些定义（都不长）：
  - CONF_FLOOR / CONF_CEIL、Evidence、Belief、UserModel（add / observe / correct）
  - Context、Limits、Decision
proactive_kit.py 里也有这四个函数的完整版（demo 用它们）。建议先自己写，卡住了再去对照。

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=25
    # 或者：.venv/bin/python -m pytest lessons/25_proactive_and_frontier -v
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。

    用"目录名__模块名"注册进 sys.modules：只加载一次，也不会和其他课程的同名文件冲突。
    """
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


proactive_kit = _load_sibling("proactive_kit")

CONF_FLOOR = proactive_kit.CONF_FLOOR  # 0.001
CONF_CEIL = proactive_kit.CONF_CEIL  # 0.999
DAY = proactive_kit.DAY  # 1440
Evidence = proactive_kit.Evidence
Belief = proactive_kit.Belief
UserModel = proactive_kit.UserModel
Context = proactive_kit.Context
Limits = proactive_kit.Limits
Decision = proactive_kit.Decision


# =====================================================================
# 练习 (a)：update_belief —— 用一条证据更新置信度
# =====================================================================


def update_belief(prior: float, evidence_weight: float, supports: bool) -> float:
    """返回看到一条证据之后的新置信度。

    用对数几率（log-odds）形式的贝叶斯更新：
        logit(p) = ln(p / (1 - p))
        logit(后验) = logit(先验) + evidence_weight    （supports=True，支持证据）
        logit(后验) = logit(先验) - evidence_weight    （supports=False，反证）
        后验 = sigmoid(logit(后验)) = 1 / (1 + e^(-x))
    evidence_weight 的含义是 ln(似然比)：例如先验 0.2（几率 1:4）遇到似然比为 4 的支持证据
    （evidence_weight = ln 4 ≈ 1.386），后验几率 1:1，也就是 0.5。

    要求：
      1. 先把 prior 夹到 [CONF_FLOOR, CONF_CEIL] 再取 logit —— prior 可能正好是 0 或 1，
         而 logit(0) = -∞、logit(1) = +∞：一旦到了 1.0，再多的反证也拉不回来；
      2. 结果也夹到 [CONF_FLOOR, CONF_CEIL]；
      3. evidence_weight = 0 时结果等于（夹过的）prior；
      4. evidence_weight 很大（1e6、1e300，甚至 math.inf）也不能抛异常、不能返回 NaN：
         注意 math.exp(1000) 会 OverflowError，sigmoid 要分正负两支写；
      5. evidence_weight < 0 → ValueError（反证请用 supports=False 表达）；prior 或 evidence_weight 是 NaN → ValueError。

    提示：math.isnan、math.log、math.exp。
    """
    raise NotImplementedError("TODO: 练习 (a) —— 对数几率形式的贝叶斯更新")


# =====================================================================
# 练习 (b)：should_interrupt —— 该不该现在打扰用户
# =====================================================================


def should_interrupt(
    benefit: float,
    confidence: float,
    cost: float,
    context: Context,
    recent_interrupts: list[int],
    limits: Limits,
) -> Decision:
    """决定对一条建议：现在说（interrupt）、攒进摘要等用户有空再说（defer）、还是不说（drop）。

    参数：
      benefit            帮上忙时用户得到的收益（≥ 0）
      confidence         用户确实需要它的置信度，[0, 1]
      cost               一次打扰的基础成本（≥ 0）
      context            Context(now, focus, in_meeting, urgent)；now 是分钟数，≥ 1440 表示第二天
      recent_interrupts  之前每次打扰（含摘要推送）发生的时刻（分钟数）
      limits             Limits(threshold, quiet_start, quiet_end, max_per_hour, focus_multiplier, meeting_multiplier, urgent_min_confidence)

    按下面的顺序判断（返回 Decision(action, reason, score)）：
      0. confidence 不在 [0, 1]、或 benefit / cost 为负 → ValueError
      1. base = benefit × confidence − cost
      2. 紧急事件（context.urgent）：越过勿扰时段、专注 / 开会、频率上限，但——
           confidence < limits.urgent_min_confidence → Decision("defer", "urgent_low_confidence", base)
           base > threshold                          → Decision("interrupt", "urgent_override", base)
           否则                                       → Decision("drop", "not_worth_it", base)
      3. 非紧急：base ≤ threshold → Decision("drop", "not_worth_it", base)   （什么时候说都不值得）
      4. 处于勿扰时段 → Decision("defer", "quiet_hours", base)
           勿扰时段是 [quiet_start, quiet_end)，用 now % 1440 判断；quiet_start > quiet_end 表示跨午夜
           （默认 22:00–08:00：23:30 和 07:59 在勿扰时段内，08:00 不在）；quiet_start == quiet_end 表示没有勿扰时段
      5. 情境倍数 multiplier：默认 1；专注时取 focus_multiplier，开会时取 meeting_multiplier，两者都有取较大的那个
         score = benefit × confidence − cost × multiplier
         score ≤ threshold → Decision("defer", "busy", score)   （值得说，但不是现在）
      6. 频率上限：recent_interrupts 里满足 now − 60 < t ≤ now 的个数 ≥ max_per_hour → Decision("defer", "rate_limited", score)
      7. 否则 → Decision("interrupt", "worth_it", score)

    所有比较都是"严格大于阈值才算值得"。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 打扰决策器")


# =====================================================================
# 练习 (c)：forget / explain —— 可删除、可解释的推断
# =====================================================================


def forget(model: UserModel, key: str) -> bool:
    """遗忘一条推断。

    要求：
      1. 从 model.beliefs 删除这条推断（它的证据列表也就一起没了）；
      2. 把 key 加入 model.blocked —— UserModel.add / observe 看到 blocked 里的 key 会直接忽略，
         否则同样的信号流很快又会把它"学"回来，用户会觉得"说好的忘了呢？"；
      3. 返回 True 表示确实删掉了一条推断；本来就不存在返回 False（但仍然要拉黑）；
      4. 不能影响其他推断。
    """
    raise NotImplementedError("TODO: 练习 (c) —— forget")


def explain(model: UserModel, key: str) -> dict:
    """解释一条推断："我有多大把握、凭什么"。推断不存在（或已被遗忘）时抛 KeyError。

    返回一个 dict，包含这些键：
      "key"        推断的 key
      "statement"  推断的自然语言描述
      "confidence" 置信度，round(..., 3)
      "pinned"     是否被用户纠正并锁定
      "n_support"  支持证据的条数
      "n_refute"   反对证据的条数
      "evidence"   证据列表：每条是一个 dict，含 t / source / note / weight / supports 五个键，按 t 从早到晚排序；
                   必须是**拷贝**：调用方修改返回值，不能改到模型里的数据
      "summary"    一句给用户看的话，要包含置信度百分比（如 "77%"）和 statement，例如：
                   "我有 77% 的把握认为：你关心 staging 环境的告警。依据：2 条支持、1 条反对。"

    提示：dataclasses.asdict 会递归地复制 dataclass。
    """
    raise NotImplementedError("TODO: 练习 (c) —— explain")
