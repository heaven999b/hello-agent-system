"""第 23 课练习：优化器的三个核心零件。

一共三题：
  (a) select_topk      OPRO 的"记忆"：从"指令 → 分数"的历史里挑出前 k 名（排序、同分、去重）
  (b) bootstrap_demos  BootstrapFewShot 的核心：跑训练集，只把评分通过的 (输入, 程序输出) 收集成示例
                       —— 它要调用模型，所以是 async 函数：写 `async def`，`await program(...)`
  (c) pareto_front     GEPA 的父代池：找出不被任何其他候选支配的候选
（a）（c）是纯计算，写普通函数。

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=23
    # 或者：.venv/bin/python -m pytest lessons/23_optimization -v

同目录的 optkit.py 里有完整的教学实现（Demo 用的就是它）。建议先自己写，写完再对照。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Awaitable, Callable, Sequence


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "23_optimization__optkit"，避免和其他课的同名模块冲突
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


optkit = _load_sibling("optkit")
Example = optkit.Example  # Example(input, label, id="", note="", tag="")
Demo = optkit.Demo  # Demo(input, output)


# =====================================================================
# 练习 (a)：select_topk —— OPRO 元提示词里放哪几条历史指令
# =====================================================================


def select_topk(history: Sequence[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    """从 OPRO 的优化历史里挑出得分最高的 k 条指令。

    history：按出现顺序排列的 (指令, 分数) 列表。同一条指令可能出现多次
             （优化器会"重新发明"已有的指令；或者同一条指令被重新评估过，两次分数不同）。

    要求：
      1. 按分数从高到低排序；
      2. 分数相同时，**先出现的排前面**（结果稳定、可复现 —— 同样的历史永远给出同样的元提示词）；
      3. 去重：指令去掉首尾空白后相同，就算同一条指令。只保留一条：
         分数取它出现过的**最高分**，排序位置按它**第一次出现**的位置算；
      4. 返回值里的指令用去掉首尾空白后的文本；
      5. 最多返回 k 条；k <= 0 或 history 为空时返回 []；不要修改传入的 history。

    例：
      select_topk([("A", 0.6), ("B", 0.8), ("C", 0.6), (" B ", 0.7)], 2)
      == [("B", 0.8), ("A", 0.6)]      # B 去重后保留 0.8；A、C 同分，A 先出现

    为什么要去重：元提示词里如果出现两条一样的指令，等于浪费上下文，还会让优化器以为"这个方向被验证了两次"。
    提示：用 dict 记录 "指令 → (最高分, 第一次出现的下标)"，再用 sorted(key=...) 一次排好。
    """
    raise NotImplementedError("TODO: 实现 select_topk")


# =====================================================================
# 练习 (b)：bootstrap_demos —— 从成功轨迹里收集示例
# =====================================================================


async def bootstrap_demos(
    program: Callable[[str], Awaitable[str]],
    trainset: Sequence,
    metric: Callable[[object, str], float],
    max_demos: int,
    *,
    threshold: float = 1.0,
) -> list:
    """用当前程序跑训练集，把评分通过的 (输入, 程序输出) 收集成示例（DSPy BootstrapFewShot 的核心步骤）。

    这是一个 async 函数：program 要调用模型，是 async 的，写 `output = await program(example.input)`；
    调用方也要 `demos = await bootstrap_demos(...)`。

    参数：
      program(input_text) -> output_text   当前的 LM 程序（比如"指令 + 模型"，optkit.Program 的实例），async
      trainset                             Example 列表（有 .input 和 .label）
      metric(example, output) -> 分数      True/False 或 0~1 的分数
      max_demos                            最多收集几条
      threshold                            分数 >= threshold 才算通过（默认 1.0；True 等于 1.0）

    要求：
      1. **按 trainset 的顺序**逐条运行：一条 await 完再调用下一条（不要用 asyncio.gather 一次全发出去 ——
         发出去的请求收不回来，第 3 条"收满就停"就省不下钱）；结果是确定的（同样的输入永远得到同样的示例列表）；
      2. 只收集 metric 通过的，返回 Demo(input=样本的输入, output=程序的输出)；
         注意 output 是**程序自己的输出**（含推理过程），不是 example.label ——
         这正是 bootstrap 的价值：标注里只有答案，程序跑通的轨迹把"怎么想的"也带上了；
      3. 收集满 max_demos 条就立刻停止，**不要再调用 program**（每次调用都要花钱）；
      4. max_demos <= 0 时直接返回 []，一次都不调用 program；
      5. program 对某条样本抛异常（限流、超时……）时跳过这条，继续下一条。

    提示：一个 for 循环 + try/except + 提前 break 就够了，循环里 `await program(...)`。
    """
    raise NotImplementedError("TODO: 实现 bootstrap_demos")


# =====================================================================
# 练习 (c)：pareto_front —— GEPA 用来挑父代的帕累托前沿
# =====================================================================


def pareto_front(candidates: dict[str, Sequence[float]]) -> list[str]:
    """返回不被任何其他候选支配的候选名。

    candidates：{候选名: [它在每个子任务上的分数]}，所有向量长度相同。
                在 GEPA 里，"子任务"就是验证集里的每一条样本（论文里叫 instance / task）。

    支配的定义：A 支配 B ⇔ A 在**每个**子任务上都 >= B，并且**至少一个**子任务上 > B。

    要求：
      1. 返回所有不被任何其他候选支配的候选名，**顺序和 candidates 的插入顺序一致**；
      2. 分数向量**完全相同**的两个候选互不支配（没有任何一项严格更大），都要留在前沿上；
         但如果有第三个候选支配了它们，它们就一起出局；
      3. candidates 为空时返回 []；
      4. 向量长度不一致时抛出 ValueError（说明数据有 bug，不能默默比较）。

    例：
      pareto_front({"A": [1, 0], "B": [0, 1], "C": [0, 0], "D": [1, 0]})
      == ["A", "B", "D"]     # C 被 A/B/D 支配；A 和 D 分数相同，都保留；A 和 B 各有所长

    为什么不只保留平均分第一名：A 擅长一类样本、B 擅长另一类，两条指令里各有别人没有的"经验"。
    只留第一名，搜索容易卡在局部最优（GEPA 论文的消融实验里，"只选当前最好的候选"比帕累托选择差）。
    提示：写一个 dominates(a, b)，再对每个候选检查"有没有别人支配它"。O(n²) 就够了。
    """
    raise NotImplementedError("TODO: 实现 pareto_front")
