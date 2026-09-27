"""第 07 课练习：从 trace 数据里算出 Agent 的健康指标。

你要实现 3 个函数（把 raise NotImplementedError 换成你的代码）：
    1. percentile(values, p)            最近秩法百分位数（热身）
    2. compute_metrics(spans)           把一堆 span 汇总成监控看板上的数字
    3. slowest_path(spans, trace_id)    复盘慢请求：沿"最慢的子节点"一路走到底

验证：make lesson N=07   （或 .venv/bin/python -m pytest lessons/07_observability）

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

想看真实数据长什么样？先跑一次 demo：python lessons/07_observability/demo.py --offline，
然后打开 lessons/07_observability/traces/demo.jsonl。
"""

from __future__ import annotations

import math  # noqa: F401  提示：math.ceil
from collections import defaultdict  # noqa: F401  提示：slowest_path 里建"父 → 子"索引很好用


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

    提示：Python 列表下标从 0 开始，别忘了 rank - 1；p == 0 时 ceil 得到 0，要兜底成 1。
    """
    raise NotImplementedError("TODO: 实现最近秩法百分位数")


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

    提示：
    - token 属性名是 "gen_ai.usage.input_tokens" / "gen_ai.usage.output_tokens"（OpenTelemetry 语义约定）；
    - 用 attrs.get(...) 而不是 attrs[...]，真实数据里字段经常缺；
    - error_rate = errors / calls，calls 至少为 1 才会出现在 tools 里，不用担心除零。
    """
    raise NotImplementedError("TODO: 汇总运行次数、成功率、p50/p95、token、工具错误率、平均步数")


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

    提示：先建一个 {parent_id: [子 span, ...]} 的索引，再从根往下走。
    max(kids, key=lambda s: (s["duration_ms"], -s["start"])) 可以一次性处理"最长 + 平局取最早"。
    """
    raise NotImplementedError("TODO: 从根开始，每层选耗时最长的子 span")
