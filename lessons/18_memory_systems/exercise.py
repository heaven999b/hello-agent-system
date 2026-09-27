"""第 18 课练习：记忆系统进阶。

三道题，对应记忆系统的三个动作：
  (a) retrieval_score   读：Generative Agents 式三因子打分（近期性 + 重要性 + 相关性），做好归一化
  (b) apply_memory_ops  写：把 ADD / UPDATE / DELETE / NOOP 应用到记忆库上（不存在的 id、重复 ADD、审计历史）
  (c) consolidate       整理：合并高度相似的记忆，保留最新值和全部来源

开始之前先读 memory_kit.py 里的 MemoryRecord、MemoryOp、OpResult 三个数据类（都很短）。

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=18
    # 或者：.venv/bin/python -m pytest lessons/18_memory_systems -v

测试里有一个"集成测试"：把你写的 apply_memory_ops 塞进 FactMemory，跑一遍 Mem0 式的完整写入流程。
卡住了？先重读 README 第 2 节，再看 solution.py。
"""

from __future__ import annotations

import importlib.util
import sys
import time  # noqa: F401  apply_memory_ops / consolidate 的默认 now 要用
import uuid  # noqa: F401  apply_memory_ops 的默认 new_id 要用
from pathlib import Path
from types import ModuleType
from typing import Callable


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。

    用"目录名__模块名"注册进 sys.modules：只加载一次，也不会和其他课程的同名文件冲突。
    """
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "18_memory_systems__memory_kit"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


memory_kit = _load_sibling("memory_kit")

MemoryRecord = memory_kit.MemoryRecord
MemoryOp = memory_kit.MemoryOp
OpResult = memory_kit.OpResult
normalize_text = memory_kit.normalize_text
DAY = memory_kit.DAY

WEIGHT_NAMES = ("recency", "importance", "relevance")


# =====================================================================
# 练习 (a)：retrieval_score —— Generative Agents 式三因子打分
# =====================================================================


def retrieval_score(
    memory: MemoryRecord,
    query_relevance: float,
    now: float,
    half_life: float,
    weights: dict[str, float] | None = None,
) -> float:
    """给一条记忆打分，返回 [0, 1] 之间的数。分越高越该被放进上下文。

    三个因子，每个都先归一化到 [0, 1]：
      - 近期性 recency   = 0.5 ** (age / half_life)，age = now - memory.last_accessed（秒）。
                           刚访问过 → 1.0；过了一个半衰期 → 0.5；两个 → 0.25。
      - 重要性 importance = (importance - 1) / 9。memory.importance 的取值范围是 1~10：1 → 0.0，10 → 1.0。
      - 相关性 relevance  = query_relevance（调用方算好的相似度，比如向量余弦）。

    然后按权重做**加权平均**：score = Σ w_i · x_i / Σ w_i。
    （Generative Agents 论文里三个权重都是 1，也就是简单相加；除以权重之和是为了让总分落在 [0, 1]，
      这样不同查询之间的分数可以比较，也能设一个"低于 0.3 就不注入"的阈值。）

    weights：
      - None → 三个权重都是 1.0；
      - dict → 只能包含 "recency" / "importance" / "relevance" 这三个名字，缺少的名字当作 0。

    边界（这些情况在真实系统里都会出现）：
      - half_life <= 0 → 抛 ValueError；
      - weights 里有未知的名字、有负数、或者全部为 0 → 抛 ValueError；
      - 时钟偏差：now 比 last_accessed 还早（多台机器时钟不同步）→ age 按 0 算，近期性不能超过 1；
      - importance 超出 1~10、query_relevance 超出 0~1（上游算错了）→ 先截断到合法范围再归一化。
    """
    raise NotImplementedError("TODO: 练习 (a) 实现 retrieval_score")


# =====================================================================
# 练习 (b)：apply_memory_ops —— 执行 ADD / UPDATE / DELETE / NOOP
# =====================================================================


def apply_memory_ops(
    store: dict[str, MemoryRecord],
    ops: list[MemoryOp],
    *,
    now: float | None = None,
    new_id: Callable[[], str] | None = None,
) -> list[OpResult]:
    """按顺序把 ops 应用到 store（记忆 id → MemoryRecord）上，**原地修改** store，返回与 ops 一一对应的结果列表。

    now 为 None 时用 time.time()；new_id 为 None 时用 uuid.uuid4().hex[:8]。测试会传入固定值。
    "活着的记录" = status == "active" 的记录。

    ADD（新增一条）：
      - text 去掉首尾空白后为空 → OpResult("ADD", None, "skipped")
      - 已有一条**活着的**记录，normalize_text 后和它相同 → 不新增。把 op.source（非空且不重复时）追加到那条记录的
        sources 里，返回 OpResult("ADD", 那条记录的 id, "duplicate")
      - 否则用 new_id() 生成 id，新建 MemoryRecord：
            text（去掉首尾空白）、key、importance 取自 op；created_at = updated_at = last_accessed = now；
            expires_at = now + op.ttl_days * DAY（ttl_days 为 None 或 0 时为 None）；
            sources = [op.source]（source 为空时为 []）；
            history = [{"op": "ADD", "old": None, "new": text, "at": now, "reason": op.reason, "source": op.source}]
        返回 OpResult("ADD", 新 id, "applied")

    UPDATE / DELETE 的共同前提：
      - op.id 不在 store 里，或者那条记录已经不是 active → 什么都不改，返回 OpResult(op.op, op.id, "not_found")
        （模型可能编造 id，也可能指向已删除的记录：不要猜它想改哪条。）

    UPDATE（改写一条）：
      - 新 text（去掉首尾空白）为空，或 normalize_text 后和原内容相同 → 不改，返回 "unchanged"（也不写历史）
      - 否则：先往 history 追加 {"op": "UPDATE", "old": 原内容, "new": 新内容, "at": now, "reason": ..., "source": ...}，
        再把 text、key、importance 换成 op 里的值；updated_at = last_accessed = now；
        expires_at 按 op.ttl_days 重新计算（规则同 ADD）；op.source 非空且不重复时追加到 sources。
        返回 "applied"

    DELETE（软删除）：
      - 往 history 追加 {"op": "DELETE", "old": 原内容, "new": None, "at": now, "reason": ..., "source": ...}，
        status 改成 "deleted"，updated_at = now。**不要**从 store 里 del，也不要清空 text —— 审计要看得到删了什么。
        返回 "applied"

    NOOP：什么都不改，返回 OpResult("NOOP", op.id, "noop")

    提示：同一批 ops 里后面的操作要能看到前面操作的结果（比如先 DELETE 再 ADD 同样的内容，应该新建一条）。
    """
    raise NotImplementedError("TODO: 练习 (b) 实现 apply_memory_ops")


# =====================================================================
# 练习 (c)：consolidate —— 合并高度相似的记忆
# =====================================================================


def consolidate(
    memories: list[MemoryRecord],
    similarity_fn: Callable[[str, str], float],
    threshold: float,
    *,
    now: float | None = None,
) -> list[MemoryRecord]:
    """把高度相似的记忆合并成一条，返回新的列表。**不要修改传入的对象**（返回副本，可以用 copy.deepcopy）。

    谁和谁算"相似"：两条记录都是 active、key 相同，并且 similarity_fn(a.text, b.text) >= threshold。
      - key 不同的永远不合并（"用户对花生过敏"和"用户喜欢花生酱"字面很像，但一个是过敏、一个是口味）；
      - 相似关系按**传递**处理：A~B、B~C，那么 A、B、C 是同一组，即使 A 和 C 本身不够相似（单链接聚类，可以用并查集）。
      - status 不是 active 的记录不参与合并，原样（副本）留在结果里。

    每组（至少 2 条）合并成一条，以组内"最新"的记录为底本：updated_at 最大的；一样大就比 created_at；还一样就取 id 较大的。
      - text / key 用底本的（最新的值才是现状）
      - sources：按组内记录的 created_at 从早到晚，把各自的 sources 依次拼起来并去重（保持首次出现的顺序）
      - importance 取组内最大值；created_at 取组内最小值；last_accessed 取组内最大值
      - history：底本自己的 history 后面追加一条
            {"op": "MERGE", "merged": [被合并掉的其他记录 id，按它们在输入中的顺序],
             "old": [它们的 text，同样顺序], "new": 底本的 text, "at": now}

    结果列表的顺序：每组合并后的记录放在该组**最早出现在输入中**的那条的位置上，其他记录保持原顺序。

    threshold 必须在 (0, 1] 之间，否则抛 ValueError。now 为 None 时用 time.time()。
    """
    raise NotImplementedError("TODO: 练习 (c) 实现 consolidate")
