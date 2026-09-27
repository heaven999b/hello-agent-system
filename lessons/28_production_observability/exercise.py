"""第 28 课练习：生产可观测性里最容易做错的三件小事。

你要实现 4 个函数（把 raise NotImplementedError 换成你的代码）：
    (a) to_genai_attributes(agentkit_span_name, attrs)   agentkit span → OTel GenAI 语义约定
    (b) burn_rate(errors, total, slo_target)              错误预算燃烧率
        should_page(short_window, long_window, thresholds) 多窗口多燃烧率告警：该不该半夜叫醒人
    (c) guard_label_cardinality(labels, allowed, max_values, seen)  拒绝高基数指标标签

验证：make lesson N=28   （或 .venv/bin/python -m pytest lessons/28_production_observability）
这些函数都是纯 Python，不需要安装 opentelemetry / prometheus_client。
"""

from __future__ import annotations

from typing import Any, Mapping

# =============================================================================================
# (a) agentkit span → OpenTelemetry GenAI 语义约定
# =============================================================================================


def to_genai_attributes(agentkit_span_name: str, attrs: Mapping[str, Any], provider_name: str = "openai") -> tuple[str, dict]:
    """把一个 agentkit span（名字 + 属性）翻译成 (OTel span 名, OTel 属性)。不修改传入的 attrs。

    agentkit 的三种核心 span（见 agentkit/agent.py）和它们的属性长这样：
        agent.run / agent.resume   {"agent.name": "support", "run_id": "...", "tenant.id": "acme", "user.id": "u-1",
                                    "agent.status": "completed", "agent.steps": 3, "gen_ai.usage.input_tokens": 60, ...}
        llm.chat                   {"gen_ai.request.model": "gpt-5.5", "step": 1, "messages": 2, "finish_reason": "stop",
                                    "gen_ai.usage.input_tokens": 20, "gen_ai.usage.reasoning_tokens": 0, "result": "...", ...}
        tool.<工具名>              {"tool.name": "track", "tool.arguments": "{...}", "tool.risk": "read",
                                    "tool.ok": False, "tool.error_type": "timeout", "tool.result_preview": "..."}

    属性规则（逐个属性处理，按顺序判断，命中一条就不再往下看）：
      1. 值为 None 的丢掉；键 "error"（异常原文）丢掉 —— 它交给 span status，不当属性。
      2. 内容类属性丢掉：tool.arguments、tool.result_preview，以及 gen_ai.input.messages、
         gen_ai.output.messages、gen_ai.system_instructions、gen_ai.tool.call.arguments、gen_ai.tool.call.result。
         （GenAI 约定规定它们是 Opt-In：默认不采集。）
      3. user.id 丢掉（永远不原样导出用户 ID）。
      4. finish_reason → "gen_ai.response.finish_reasons"，值变成单元素列表 [str(值)]。
      5. 改名：agent.name → gen_ai.agent.name；tool.name → gen_ai.tool.name；
               gen_ai.usage.reasoning_tokens → gen_ai.usage.reasoning.output_tokens。
      6. 以 "gen_ai." / "error." / "agentkit." 开头的，原样保留。
      7. agentkit 核心的其余属性加 "agentkit." 前缀：键以 "agent." 或 "tool." 开头，
         或者键是 run_id / step / messages / result / interrupted / tenant.id 之一。
         例：agent.status → agentkit.agent.status；run_id → agentkit.run_id；tool.ok → agentkit.tool.ok
      8. 其余（用户自己加的属性，如 app.feature）原样保留。

    再按 span 名补充（最后设置 gen_ai.operation.name）：
      - "agent.run" / "agent.resume"：operation = "invoke_agent"；span 名 "invoke_agent {gen_ai.agent.name}"，
        没有 agent 名时就是 "invoke_agent"。
          · agent.status 是 "failed" 或 "max_steps" 时，加 error.type = 该状态；否则 agent.stop_reason 是
            "timeout" 时，加 error.type = "timeout"。其余都不算失败：paused（等审批）、cancelled（客户端主动断开）、
            stopped 里的 budget_exceeded / blocked_input / rate_limited。
            （agent.stop_reason 由 OTelTracer 在运行结束时写到根 span 上，已归一化："llm_error: 503 ..." → "llm_error"。）
          · 只对 agent.resume：加 agentkit.resumed = True；并把 gen_ai.usage.input_tokens / output_tokens
            改名为 agentkit.run.cumulative_input_tokens / agentkit.run.cumulative_output_tokens
            （agentkit 在 resume 上记的是整个 run 的累计值，照搬会被按 span 求和的后端重复计数）。
      - "llm.chat"：operation = "chat"；span 名 "chat {gen_ai.request.model}"；模型名缺失、为空或为 "?" 时
        span 名就是 "chat"，并且删掉 gen_ai.request.model。加 gen_ai.provider.name = provider_name。
      - "tool.<名字>"：operation = "execute_tool"；gen_ai.tool.name 缺失时用 span 名里 "tool." 之后的部分；
        span 名 "execute_tool {工具名}"；加 gen_ai.tool.type = "function"。
        tool.ok 恰好是 False 且 tool.error_type 不是 "denied" 时，加 error.type = str(tool.error_type)
        （tool.error_type 缺失时用 "_OTHER"）。权限策略拒绝（denied）是系统按设计工作，不是故障。
      - 其他 span 名：名字不变，只做属性翻译，**不加** gen_ai.operation.name。

    例：
        to_genai_attributes("tool.track", {"tool.name": "track", "tool.ok": False, "tool.error_type": "timeout"})
        == ("execute_tool track", {"gen_ai.tool.name": "track", "agentkit.tool.ok": False,
                                   "agentkit.tool.error_type": "timeout", "gen_ai.tool.type": "function",
                                   "error.type": "timeout", "gen_ai.operation.name": "execute_tool"})

    agentkit/contrib/otel.py 里的 genai_mapping 是完整版（多了内容采集开关、用户 ID 假名化和 span kind），
    测试会拿真实 Agent 产生的 span 检查你的结果和它一致。
    """
    raise NotImplementedError("TODO: (a) 实现 to_genai_attributes")


# =============================================================================================
# (b) 多窗口多燃烧率告警（Google SRE Workbook《Alerting on SLOs》）
# =============================================================================================

# (长窗口, 短窗口, 燃烧率阈值)：SRE Workbook 表 5-8 里两条"要叫醒人"（page）的规则。
# 30 天的 SLO 周期里，燃烧率 14.4 持续 1 小时会烧掉 2% 的错误预算，燃烧率 6 持续 6 小时会烧掉 5%。
PAGE_RULES: tuple[tuple[str, str, float], ...] = (("1h", "5m", 14.4), ("6h", "30m", 6.0))


def burn_rate(errors: int, total: int, slo_target: float) -> float:
    """燃烧率 = 实际错误率 ÷ SLO 允许的错误率（错误预算）。

    例：SLO 99%（允许 1% 失败），这 1 小时里 1000 次运行失败 30 次 → 错误率 3% → 燃烧率 3.0，
    意思是"照这个速度，30 天的错误预算 10 天就烧完了"。燃烧率 1.0 = 刚好在周期末用完预算。

    规则：
      - slo_target 必须满足 0 < slo_target < 1，否则 ValueError；
      - errors、total 不能为负，errors 不能大于 total，否则 ValueError；
      - total == 0（这个窗口里没有流量）返回 0.0：没有请求就没有烧预算。
    提示：能出现的最大燃烧率是 1 / (1 - slo_target)（全部失败时）。SLO 95% 时最多只有 20 ——
    这时 14.4 的阈值意味着错误率要超过 72% 才会叫人，值得想一想你的 SLO 定得是否合理。
    """
    raise NotImplementedError("TODO: (b) 实现 burn_rate")


def should_page(
    short_window: Mapping[str, float],
    long_window: Mapping[str, float],
    thresholds: tuple[tuple[str, str, float], ...] = PAGE_RULES,
) -> bool:
    """多窗口多燃烧率：任何一条规则的**长窗口和短窗口燃烧率都严格大于阈值**时返回 True。

    short_window / long_window：窗口名 → 该窗口内的燃烧率，例如 {"5m": 20.0, "30m": 8.0} / {"1h": 15.0, "6h": 3.0}。
    thresholds：若干 (长窗口名, 短窗口名, 阈值)，默认 PAGE_RULES。

    为什么要两个窗口：
      - 只看长窗口（1h）：故障修好以后，1 小时的平均值还要很久才降下来，告警迟迟不恢复（reset time 长）；
      - 只看短窗口（5m）：一次几十秒的抖动就会叫醒人（precision 低）；
      - 两个都超过才告警：长窗口保证"确实烧掉了可观的预算"，短窗口保证"现在还在烧"。

    规则：比较用严格大于（与 SRE Workbook 的 PromQL 示例 `> (14.4*0.001)` 一致）；
    某个窗口在字典里没有（没有数据）按"不超过"处理 —— 和 Prometheus 一样，没有数据就不告警。
    """
    raise NotImplementedError("TODO: (b) 实现 should_page")


# =============================================================================================
# (c) 标签基数防护
# =============================================================================================

# 这些字段每个请求、每个用户都不一样。当成 Prometheus 标签，每个取值都会生成一条新的时间序列，
# 指标系统会被撑爆。它们应该放在 trace 和日志里（按 trace_id / run_id 检索）。
FORBIDDEN_LABELS = frozenset(
    {"user_id", "user", "email", "phone", "ip", "run_id", "trace_id", "span_id", "session_id", "conversation_id", "request_id", "call_id"}
)
OVERFLOW = "__other__"


def guard_label_cardinality(
    labels: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    max_values: Mapping[str, int],
    seen: dict[str, set[str]] | None = None,
) -> dict[str, str]:
    """在打点之前检查一组标签，返回可以安全使用的新字典（不修改传入的 labels）。

    labels     ：本次打点要用的标签，如 {"status": "completed", "tenant": "acme"}
    allowed    ：允许使用的标签名
    max_values ：标签名 → 最多允许多少个不同取值；不在这里的标签不限（如 status 这种枚举）
    seen       ：标签名 → 已经放行过的取值集合。多次调用传同一个字典，才能跨调用累计；为 None 时新建一个。

    规则：
      1. 先检查标签名，再动 seen（出错时 seen 保持原样）：
         - 名字在 FORBIDDEN_LABELS 里 → ValueError（即使它也在 allowed 里：防配置失误），
           错误信息里要包含这个标签名；
         - 名字不在 allowed 里 → ValueError，错误信息里包含这个标签名。
      2. 取值规范化：None 或空字符串 → "unknown"，其余 str(值)。
      3. 有上限的标签：取值已在 seen[名字] 里 → 原样通过；seen[名字] 还没满 → 记入 seen 后通过；
         满了 → 换成 OVERFLOW（"__other__"），不记入 seen。
    例：max_values={"tenant": 2}，依次打点 tenant=a、b、c、a → a、b、__other__、a
    """
    raise NotImplementedError("TODO: (c) 实现 guard_label_cardinality")
