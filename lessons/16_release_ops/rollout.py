"""基于指标的灰度推进 / 回滚（第 16 课 问题 2、4）。

    metrics_from_runs    从**真实的运行记录**算出一个阶段的指标（成功率、错误率、p95、单次成本）
    evaluate_stage       看一眼当前灰度阶段的指标，给出 advance / hold / rollback 和理由
    RolloutController    把决策落到 PromptRegistry 上：1% → 10% → 50% → 100%，出事自动回滚；
                         每次变更通过 publish_release 写进配置中心，所有 worker 进程在一个轮询间隔内切换
    two_proportion_test  A/B 实验里"成功率差异是不是真的"（双比例 z 检验）
    min_sample_size      想检测出 X 个百分点的差异，每组至少要多少样本

决策的核心思想：**先止血，再看样本，最后才比较。**
  - 安全事件零容忍：一次越权 / 泄露就回滚，不等样本量；
  - 样本不够不下结论：灰度 1% 的前 50 个请求，成功率 94% 还是 96% 都只是噪声；
  - 质量变差（错误率、成功率）→ 自动回滚；成本、延迟变差 → 暂停等人判断（也许是用钱换来了质量）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Iterable

from registry import PromptRegistry


@dataclass
class StageMetrics:
    requests: int  # 这个阶段 candidate 处理了多少请求
    success_rate: float  # 任务成功率：status == completed 且没有被点踩（或在线评估通过）
    error_rate: float  # 系统错误率：status == failed、异常、超时
    p95_latency_ms: float
    cost_per_task: float  # 平均每个请求的成本（美元）
    safety_incidents: int = 0  # 已确认的安全事件：越权调用、数据泄露、违规输出……


@dataclass
class Thresholds:
    min_requests: int = 200
    max_error_rate: float = 0.05  # 绝对上限
    max_success_drop: float = 0.03  # 成功率最多比基线低 3 个百分点（绝对值）
    max_latency_ratio: float = 1.25  # p95 最多是基线的 1.25 倍
    max_cost_ratio: float = 1.30  # 单次成本最多是基线的 1.3 倍


@dataclass
class Decision:
    action: str  # advance / hold / rollback
    reasons: list[str] = field(default_factory=list)


def evaluate_stage(stage: StageMetrics, baseline: StageMetrics, t: Thresholds = Thresholds()) -> Decision:
    if stage.safety_incidents > 0:
        return Decision("rollback", [f"安全事件 {stage.safety_incidents} 起：零容忍，不等样本量"])
    if stage.requests < t.min_requests:
        return Decision("hold", [f"样本不足：{stage.requests} < {t.min_requests}，继续观察"])

    bad = []
    if stage.error_rate > t.max_error_rate:
        bad.append(f"错误率 {stage.error_rate:.1%} > 上限 {t.max_error_rate:.1%}")
    if stage.success_rate < baseline.success_rate - t.max_success_drop:
        bad.append(f"成功率 {stage.success_rate:.1%}，比基线 {baseline.success_rate:.1%} 低了超过 {t.max_success_drop:.0%}")
    if bad:
        return Decision("rollback", bad)

    slow = []
    if stage.p95_latency_ms > baseline.p95_latency_ms * t.max_latency_ratio:
        slow.append(f"p95 {stage.p95_latency_ms:.0f}ms 是基线的 {stage.p95_latency_ms / max(baseline.p95_latency_ms, 1e-9):.2f} 倍")
    if stage.cost_per_task > baseline.cost_per_task * t.max_cost_ratio:
        slow.append(f"单次成本 ${stage.cost_per_task:.4f} 是基线的 {stage.cost_per_task / max(baseline.cost_per_task, 1e-12):.2f} 倍")
    if slow:
        return Decision("hold", slow + ["质量没变差，但更慢 / 更贵：需要人来判断值不值"])
    return Decision("advance", ["各项指标都在阈值内"])


def metrics_from_runs(runs: Iterable[dict]) -> StageMetrics:
    """从真实的运行记录算出一个阶段（或基线）的指标。每条记录至少有：
        status       RunResult.status
        errors       这次运行里出错的工具调用数（异常 / 超时；worker 用一个 after_tool 钩子数出来）
        latency_ms   这次运行的耗时
        cost_usd     这次运行的成本
        safety       （可选）已确认的安全事件数
    错误 = 没有正常完成，或者中途有工具出错；成功 = 正常完成且没有出错（生产中还会叠加点踩、在线评估）。
    """
    runs = list(runs)
    n = len(runs)
    if n == 0:
        return StageMetrics(0, 0.0, 0.0, 0.0, 0.0)
    errors = sum(1 for r in runs if r["status"] != "completed" or r["errors"] > 0)
    latencies = sorted(r["latency_ms"] for r in runs)
    return StageMetrics(
        requests=n,
        success_rate=(n - errors) / n,
        error_rate=errors / n,
        p95_latency_ms=latencies[math.ceil(0.95 * n) - 1],
        cost_per_task=sum(r["cost_usd"] for r in runs) / n,
        safety_incidents=sum(int(r.get("safety", 0)) for r in runs),
    )


STAGES = (1, 10, 50, 100)


async def publish_release(center, registry: PromptRegistry, name: str, *, actor: str, reason: str) -> int:
    """把"全部版本 + 当前流量分配"写进配置中心（configcenter.ConfigCenter），返回新的配置版本号。
    所有 worker 进程的 ConfigWatcher 会在一个轮询间隔内看到它。"""
    return await center.set(f"release:{name}", registry.release_doc(name), actor=actor, reason=reason)


class RolloutController:
    """灰度控制器：每个观察窗口结束时调用一次 step()，它根据指标推进、暂停或回滚。

    center（可选）：配置中心。给了它，每次推进 / 推全 / 回滚都会立刻 publish_release，
    published 记下最近一次下发的配置版本号 —— 调用方可以据此确认"所有 worker 都已经切过去了"。
    """

    def __init__(self, registry: PromptRegistry, name: str, *, center=None, stages: tuple[int, ...] = STAGES,
                 thresholds: Thresholds = Thresholds(), actor: str = "rollout-bot"):
        self.registry, self.name, self.stages, self.thresholds, self.actor = registry, name, stages, thresholds, actor
        self.center = center
        self.published: int | None = None
        self.log: list[tuple[int, Decision]] = []

    async def _publish(self, reason: str) -> None:
        if self.center is not None:
            self.published = await publish_release(self.center, self.registry, self.name, actor=self.actor, reason=reason)

    async def start(self, candidate: int, **kwargs) -> None:
        self.registry.start_rollout(self.name, candidate, self.stages[0], actor=self.actor, **kwargs)
        await self._publish(f"开始灰度 v{candidate}：{self.stages[0]}%")

    async def step(self, stage: StageMetrics, baseline: StageMetrics) -> Decision:
        r = self.registry.rollout(self.name)
        if r.candidate is None:
            raise RuntimeError("没有进行中的灰度")
        d = evaluate_stage(stage, baseline, self.thresholds)
        self.log.append((r.percent, d))
        if d.action == "rollback":
            self.registry.rollback(self.name, actor=self.actor, reason="；".join(d.reasons))
            await self._publish("自动回滚：" + "；".join(d.reasons))
        elif d.action == "advance":
            later = [p for p in self.stages if p > r.percent]
            if later and later[0] < 100:
                self.registry.set_percent(self.name, later[0], actor=self.actor, reason="指标达标，扩量")
                await self._publish(f"指标达标，扩量到 {later[0]}%")
            else:
                self.registry.promote(self.name, actor=self.actor)
                await self._publish(f"指标达标，v{r.candidate} 推全")
        return d


# ------------------------------------------------------------------ A/B 实验的统计学


def two_proportion_test(success_a: int, n_a: int, success_b: int, n_b: int) -> tuple[float, float, float]:
    """双比例 z 检验：B 组成功率减 A 组成功率的差、z 值、双侧 p 值。

    p 值 < 0.05 才能说"差异显著"（大约是：如果两组其实没差别，看到这么大差异的概率不到 5%）。
    """
    pa, pb = success_a / n_a, success_b / n_b
    pooled = (success_a + success_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return pb - pa, 0.0, 1.0
    z = (pb - pa) / se
    p = 2 * (1 - NormalDist().cdf(abs(z)))
    return pb - pa, z, p


def min_sample_size(p_base: float, mde: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """想以 power 的把握检测出 mde（绝对值，如 0.02 = 2 个百分点）的成功率变化，每组至少要多少样本。"""
    p1, p2 = p_base, p_base - mde
    pbar = (p1 + p2) / 2
    za, zb = NormalDist().inv_cdf(1 - alpha / 2), NormalDist().inv_cdf(power)
    n = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2 / mde**2
    return math.ceil(n)
