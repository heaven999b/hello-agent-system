"""第 10 课练习（参考答案）：从 trace 数据里算出 Agent 的健康指标。

输入是 agentkit.tracing.jsonl_exporter 导出的"扁平 span 字典"列表，每个字典长这样
（字段来自 agentkit/tracing.py 的 Span.to_dict）：

    {
        "name": "tool.track_shipment",   # agent.run / llm.chat / tool.<工具名> / agent.resume ...
        "trace_id": "9f2c...",           # 同一次运行的所有 span 共享一个 trace_id
        "span_id": "a1b2c3d4",
        "parent_id": "e5f6a7b8",         # 根 span 为 None
        "start": 1727400000.12,          # 秒（Unix 时间戳）
        "end": 1727400000.35,
        "duration_ms": 230.0,
        "status": "ok",                  # ok / error（代码抛异常时为 error）
        "attrs": {"tool.name": "track_shipment", "tool.ok": False, "tool.error_type": "tool_error", ...},
    }
"""

from __future__ import annotations

import math
from collections import defaultdict


def percentile(values: list[float], p: float) -> float | None:
    """最近秩法（nearest-rank）百分位数。

    定义：把 values 升序排序得到 v[1..N]（下标从 1 开始），
        rank = ceil(p / 100 × N)，结果为 v[rank]；p == 0 时取最小值 v[1]。

    特点：结果一定是样本里真实出现过的某个值（不做插值），好解释："p95 = 8.2s"
    意思是"95% 的运行不超过 8.2s，而且确实有一次运行花了 8.2s"。

    例：values = [15, 20, 35, 40, 50]
        p30 → rank=ceil(1.5)=2 → 20；p40 → rank=2 → 20；p50 → rank=ceil(2.5)=3 → 35；p100 → 50

    边界：values 为空返回 None；p 不在 [0, 100] 抛 ValueError；输入顺序任意（函数内部排序）。
    注意浮点误差：7 / 100 * 100 == 7.000000000000001，所以要写成 ceil(p * N / 100)（先乘后除）。
    """
    if not 0 <= p <= 100:
        raise ValueError(f"p 必须在 [0, 100] 之间，收到 {p}")
    if not values:
        return None
    ordered = sorted(values)
    # 先乘后除：p / 100 * N 会有浮点误差（7 / 100 * 100 == 7.000000000000001，ceil 后变成 8）
    rank = max(1, math.ceil(p * len(ordered) / 100))
    return ordered[rank - 1]


def _is_run_root(s: dict) -> bool:
    return s.get("parent_id") is None and s.get("name") == "agent.run"


def compute_metrics(spans: list[dict]) -> dict:
    """把一堆扁平 span 汇总成"监控看板"上的数字。返回：

        {
            "runs": 3,                  # 运行次数 = 根 span 中 name == "agent.run" 的个数
            "success_rate": 0.667,      # attrs["agent.status"] == "completed" 的比例；runs == 0 时为 0.0
            "p50_ms": 1200.0,           # 运行耗时（根 span 的 duration_ms）的 p50，最近秩法；无运行时为 None
            "p95_ms": 3400.0,           # 同上，p95
            "total_tokens": 5230,       # 所有 llm.chat span 的 input_tokens + output_tokens 之和
            "avg_steps": 2.33,          # 根 span attrs["agent.steps"] 的平均值；runs == 0 时为 0.0
            "tools": {                  # 每个工具一行
                "track_shipment": {"calls": 3, "errors": 2, "error_rate": 0.667},
            },
        }

    规则细节（每条都对应一个真实的坑）：
    1. 只有"根" agent.run 才算一次运行：多 Agent 场景下，子 Agent 的 agent.run 挂在父 Agent 的
       工具 span 下面（parent_id 不为 None），它是父运行的一部分，不能重复计数。
       agent.resume（从检查点恢复）是同一次运行的延续，也不计入运行次数。
    2. token 只从 llm.chat span 累加：根 span 上也有 gen_ai.usage.*（那是整次运行的汇总），
       如果把所有带 token 属性的 span 都加起来，会重复统计。
       llm.chat 可能因为模型报错而没有 token 属性，按 0 处理。
    3. 工具 span 的名字以 "tool." 开头；工具名优先取 attrs["tool.name"]，没有就取 "tool." 之后的部分。
       attrs["tool.ok"] 为 False，或 span 的 status 为 "error"（工具执行时抛出了异常），都算一次失败。
    4. spans 的顺序是任意的，可能混有多个 trace。
    """
    runs = [s for s in spans if _is_run_root(s)]
    n = len(runs)
    completed = sum(1 for s in runs if s.get("attrs", {}).get("agent.status") == "completed")
    durations = [float(s["duration_ms"]) for s in runs]

    total_tokens = 0
    tools: dict[str, dict] = {}
    for s in spans:
        name, attrs = s.get("name", ""), s.get("attrs", {})
        if name == "llm.chat":
            total_tokens += int(attrs.get("gen_ai.usage.input_tokens") or 0)
            total_tokens += int(attrs.get("gen_ai.usage.output_tokens") or 0)
        elif name.startswith("tool."):
            tool_name = attrs.get("tool.name") or name[len("tool.") :]
            stat = tools.setdefault(tool_name, {"calls": 0, "errors": 0, "error_rate": 0.0})
            stat["calls"] += 1
            if attrs.get("tool.ok") is False or s.get("status") == "error":
                stat["errors"] += 1
    for stat in tools.values():
        stat["error_rate"] = stat["errors"] / stat["calls"]

    return {
        "runs": n,
        "success_rate": completed / n if n else 0.0,
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
        "total_tokens": total_tokens,
        "avg_steps": sum(int(s.get("attrs", {}).get("agent.steps") or 0) for s in runs) / n if n else 0.0,
        "tools": tools,
    }


def slowest_path(spans: list[dict], trace_id: str) -> list[str]:
    """返回某个 trace 里的"最慢路径"：从根 span 开始，每一层都走向耗时最长的那个子 span，
    直到叶子节点。返回沿途的 span 名称列表。

    例：agent.run(3000ms) 有三个子节点 llm.chat(800ms)、tool.search(1900ms)、llm.chat(300ms)，
        tool.search 没有子节点 → 返回 ["agent.run", "tool.search"]

    规则：
    - 只看 trace_id 匹配的 span；根 = parent_id 为 None 的 span。
    - 找不到这个 trace（或它没有根 span）时返回 []。
    - 同一层耗时相同时，选开始时间（start）更早的那个，保证结果确定。
    - spans 的顺序是任意的（子 span 可能排在父 span 前面）。

    用途：复盘"这次为什么这么慢"时，先沿最慢路径往下看，通常一眼就能看到瓶颈。
    """
    in_trace = [s for s in spans if s.get("trace_id") == trace_id]
    root = next((s for s in in_trace if s.get("parent_id") is None), None)
    if root is None:
        return []
    children: dict[str, list[dict]] = defaultdict(list)
    for s in in_trace:
        if s.get("parent_id") is not None:
            children[s["parent_id"]].append(s)

    path, node = [], root
    while node is not None:
        path.append(node["name"])
        kids = children.get(node["span_id"])
        # 耗时最长优先；耗时相同则 start 更早优先（-start 越大 = start 越早）
        node = max(kids, key=lambda s: (s["duration_ms"], -s["start"])) if kids else None
    return path
