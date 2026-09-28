"""第 16 课练习参考答案。先自己写，卡住了再看。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Iterable

from agentkit.hooks import Hook, StopRun
from agentkit.tools import Tool

# =====================================================================
# 练习 (a)：bucket —— 稳定分桶
# =====================================================================


def bucket(user_id: str, salt: str) -> int:
    """把 user_id 映射到 0-99 之间的一个整数（"桶"）。

    要求：
      1. 稳定：同一个 (user_id, salt)，无论调用多少次、在哪个进程、哪台机器上，结果都一样。
         ⚠️ 不能用内置 hash()：Python 每次启动进程都会给字符串哈希换一个随机种子（PYTHONHASHSEED），
         服务一重启、或者请求落到另一台机器上，用户就换了桶 —— 今天在新版本，明天又回到旧版本。
      2. 均匀：大量用户应该大致均匀地分布在 100 个桶里（这样"桶 < 10"才真的约等于 10% 的用户）。
      3. 不同 salt 相互独立：salt 通常是"这次发布的名字"。如果不同发布用同一种分法，
         永远是同一批 1% 的用户在当小白鼠，而另一批用户永远拿不到新功能。

    做法：用 hashlib.sha256 对 f"{salt}:{user_id}" 求哈希，取摘要的前 8 个字节转成整数，再对 100 取模。
        digest = hashlib.sha256(f"{salt}:{user_id}".encode("utf-8")).digest()
        int.from_bytes(digest[:8], "big") % 100
    （想一想：为什么要把 salt 和 user_id 用分隔符隔开？提示：salt="a1" + user="23" 和 salt="a" + user="123"。）
    """
    digest = hashlib.sha256(f"{salt}:{user_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


# =====================================================================
# 练习 (b)：pick_version —— 灰度分流
# =====================================================================


@dataclass
class Rollout:
    """一个 prompt（或模型配置）当前的流量分配。"""

    stable: int  # 全量版本号
    candidate: int | None = None  # 灰度中的新版本号；None 表示没有进行中的灰度
    percent: int = 0  # 0-100：多少比例的用户走 candidate
    salt: str = ""  # 这次灰度的分桶盐，一般是 "prompt名:v新版本号"
    force_candidate: frozenset[str] = frozenset()  # 强制走新版本的用户：内部员工、测试账号（先"吃自己的狗粮"）
    force_stable: frozenset[str] = frozenset()  # 强制留在旧版本的用户：合同要求不参与实验、正在做重要演示的客户


def pick_version(user_id: str, rollout: Rollout) -> int:
    """返回这个用户应该使用的版本号。按顺序判断：

      1. 没有进行中的灰度（candidate 是 None）→ stable；
      2. 用户在 force_stable 里 → stable（"不参与实验"的承诺优先级最高，哪怕他也在 force_candidate 里）；
      3. 用户在 force_candidate 里 → candidate（percent 为 0 时也生效：这就是只给内部员工用的"第 0 阶段"）；
      4. bucket(user_id, rollout.salt) < rollout.percent → candidate；
      5. 否则 → stable。

    想一想第 4 条带来的一个好性质：percent 从 1 扩到 10、再扩到 50 时，
    原来在灰度里的用户（桶号 < 1）一定还在灰度里（桶号 < 10）—— 扩量时不会有人被"甩回"旧版本。
    """
    if rollout.candidate is None:
        return rollout.stable
    if user_id in rollout.force_stable:
        return rollout.stable
    if user_id in rollout.force_candidate:
        return rollout.candidate
    return rollout.candidate if bucket(user_id, rollout.salt) < rollout.percent else rollout.stable


# =====================================================================
# 练习 (c)：rollout_decision —— 按指标自动推进 / 暂停 / 回滚
# =====================================================================


@dataclass
class StageMetrics:
    requests: int  # 这个观察窗口里处理了多少请求
    success_rate: float  # 任务成功率（0~1）
    error_rate: float  # 系统错误率（0~1）
    p95_latency_ms: float
    cost_per_task: float  # 平均每个请求的成本（美元）
    safety_incidents: int = 0  # 已确认的安全事件数：越权调用、数据泄露、违规输出……


@dataclass
class Thresholds:
    min_requests: int = 200  # 少于这么多请求，不下结论
    max_error_rate: float = 0.05  # 错误率的绝对上限
    max_success_drop: float = 0.03  # 成功率最多比基线低多少（绝对值，0.03 = 3 个百分点）
    max_latency_ratio: float = 1.25  # p95 延迟最多是基线的多少倍
    max_cost_ratio: float = 1.30  # 单次成本最多是基线的多少倍


def rollout_decision(stage_metrics: StageMetrics, baseline: StageMetrics, thresholds: Thresholds) -> str:
    """看灰度版本（stage_metrics）和基线（baseline，即 stable 版本同一时段的指标），返回
    "advance"（推进到下一阶段）、"hold"（保持当前比例，继续观察或等人决定）或 "rollback"（立刻回滚）。

    按顺序判断，命中一条就返回 —— 口诀：先止血，再看样本，最后才比较。
      1. safety_incidents > 0                                   → "rollback"
         （安全事件零容忍：出现一次越权或泄露，不需要等样本量）
      2. requests < min_requests                                → "hold"
         （样本太少，指标全是噪声：既不能推进，也不应该因为噪声回滚）
      3. error_rate > max_error_rate                            → "rollback"
      4. success_rate < baseline.success_rate - max_success_drop → "rollback"
         （质量变差：自动回滚）
      5. p95_latency_ms > baseline.p95_latency_ms × max_latency_ratio，
         或 cost_per_task > baseline.cost_per_task × max_cost_ratio → "hold"
         （只是更慢 / 更贵，质量没变差：也许是用钱换来了质量，交给人判断，而不是自动回滚）
      6. 其他情况                                               → "advance"

    所有比较都是严格的 > 和 <：恰好等于阈值不算越界。
    """
    s, b, t = stage_metrics, baseline, thresholds
    if s.safety_incidents > 0:
        return "rollback"
    if s.requests < t.min_requests:
        return "hold"
    if s.error_rate > t.max_error_rate:
        return "rollback"
    if s.success_rate < b.success_rate - t.max_success_drop:
        return "rollback"
    if s.p95_latency_ms > b.p95_latency_ms * t.max_latency_ratio or s.cost_per_task > b.cost_per_task * t.max_cost_ratio:
        return "hold"
    return "advance"


# =====================================================================
# 练习 (d)：kill switch —— 紧急开关的生效逻辑
# =====================================================================

WRITE_RISKS = {"write", "dangerous"}


def blocked_reason(flags: dict, tool_name: str, tool_risk: str, tenant_id: str | None) -> str | None:
    """根据当前的开关状态，判断这次工具调用要不要被拦下。返回拒绝理由（给模型看），放行则返回 None。

    flags 来自配置中心，运维改完几秒内生效，结构如下（任何键都可能不存在，不存在就是"没有限制"）：
        {
            "disabled_tools": ["refund"],          # 全局停用的工具（可能是 list，也可能是 set）
            "read_only": False,                    # 全局只读：所有 write / dangerous 工具都停用
            "tenants": {                           # 按租户的开关
                "acme": {"disabled_tools": ["send_email"], "read_only": True},
            },
        }

    按顺序判断，命中一条就返回：
      1. tool_name 在全局 disabled_tools 里 → 返回理由；
      2. tenant_id 不为 None，且 tool_name 在该租户的 disabled_tools 里 → 返回理由；
      3. 全局 read_only 为真，或该租户的 read_only 为真，并且 tool_risk 属于 WRITE_RISKS → 返回理由；
      4. 否则返回 None（read 工具在只读模式下照常可用）。

    理由的要求（测试会检查）：
      - 必须包含工具名；
      - 第 3 条（只读模式）的理由里必须包含"只读"两个字；
      - 建议写清楚"请告诉用户该功能暂时不可用，不要尝试用其他工具绕过"——
        模型很有创造力：发邮件的工具被停了，它可能会"贴心地"建一张工单让别人代发。

    例：
        blocked_reason({"disabled_tools": ["refund"]}, "refund", "dangerous", "acme")  → "工具 refund 已被紧急停用……"
        blocked_reason({"read_only": True}, "search_kb", "read", "acme")                → None
        blocked_reason({"tenants": {"acme": {"read_only": True}}}, "create_ticket", "write", "globex") → None
    """
    tenant_flags = {}
    if tenant_id is not None:
        tenant_flags = (flags.get("tenants") or {}).get(tenant_id) or {}
    if tool_name in set(flags.get("disabled_tools") or ()):
        return f"工具 {tool_name} 已被紧急停用（全局）。请告诉用户该功能暂时不可用，不要尝试用其他工具绕过。"
    if tool_name in set(tenant_flags.get("disabled_tools") or ()):
        return f"工具 {tool_name} 已对租户 {tenant_id} 紧急停用。请告诉用户该功能暂时不可用，不要尝试用其他工具绕过。"
    if (flags.get("read_only") or tenant_flags.get("read_only")) and tool_risk in WRITE_RISKS:
        return f"系统当前处于只读模式，暂时不能执行 {tool_name} 这类修改操作。请告诉用户稍后再试，或转人工处理。"
    return None


class KillSwitch(Hook):
    """紧急开关 Hook（已写好，它会调用你实现的 blocked_reason）。

    和 PermissionPolicy(deny_tools=...) 的区别：deny_tools 是构造时传入的静态集合，改它要重新部署；
    KillSwitch 每次都通过 flags_source() 读取当前的开关，运维改完开关，下一次工具调用就按新开关判断。
    线上 flags_source 是配置中心在本进程的快照（configcenter.ConfigWatcher.snapshot，后台轮询，
    最多滞后一个轮询间隔）；测试里直接传 lambda: 一个 dict。钩子方法都是普通方法：只读内存，不需要 await。

    它在三个地方生效：
      - on_run_start：整个 Agent 被停用（agent_disabled）时，直接结束运行并转人工；
      - visible_tools：被停用的工具不再展示给模型（让它别去尝试）；
      - before_tool：真正的拦截点。只靠"隐藏"不够：模型可能凭历史记忆调用被隐藏的工具，
        而且在开关打开之前就已暂停等审批的运行，恢复时根本不会再经过 visible_tools。
    """

    def __init__(self, flags_source: Callable[[], dict], tools: Iterable[Tool] = ()):
        self.flags_source = flags_source
        self.risks = {t.name: t.risk for t in tools}

    def _flags(self) -> dict:
        return self.flags_source() or {}

    def on_run_start(self, state, user_input):
        flags = self._flags()
        tenant = state.metadata.get("tenant_id")
        if flags.get("agent_disabled") or flags.get("tenants", {}).get(tenant, {}).get("agent_disabled"):
            raise StopRun("kill_switch", "智能助手正在维护，已为您转接人工客服。")
        return None

    def visible_tools(self, state, names):
        flags, tenant = self._flags(), state.metadata.get("tenant_id")
        return [n for n in names if blocked_reason(flags, n, self.risks.get(n, "read"), tenant) is None]

    def before_tool(self, state, call, tool):
        risk = tool.risk if tool is not None else self.risks.get(call.name, "read")
        return blocked_reason(self._flags(), call.name, risk, state.metadata.get("tenant_id"))
