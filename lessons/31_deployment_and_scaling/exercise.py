"""第 31 课练习：扩缩容、停机时间线、压测结果 —— 三个上线前一定要算清楚的问题。

(a) desired_replicas           仿 KEDA / HPA 的副本数计算：按"队列积压 + 在途"算，带容忍度和缩容稳定窗口
(b) validate_shutdown_timeline 检查 terminationGracePeriodSeconds、preStop、worker 宽限期、租约、最长任务之间的关系
(c) evaluate_load_test         从压测样本算 p50/p95/p99、错误率、429 比例，判断是否满足 SLO，并给出最可能的瓶颈

全部是纯函数：不需要 Postgres、Redis、Kubernetes，测试毫秒级完成。
运行测试：make lesson N=31    或    .venv/bin/python -m pytest lessons/31_deployment_and_scaling -v
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------------------------
# (a) desired_replicas
# ---------------------------------------------------------------------------------------------


def desired_replicas(
    queue_depth: int,
    in_flight: int,
    per_worker_concurrency: int,
    target_utilization: float,
    min_r: int,
    max_r: int,
    current: int,
    scale_down_stabilization: Sequence[int] = (),
    *,
    tolerance: float = 0.1,
) -> int:
    """按队列深度算 worker 副本数（KEDA postgresql scaler + HPA 的简化版）。

    参数：
        queue_depth               排队中、可执行的任务数
        in_flight                 正在执行的任务数（它们也占着 worker 的并发名额）
        per_worker_concurrency    每个 worker 同时处理的任务数（run_async_worker 的 concurrency）
        target_utilization        希望 worker 的并发名额平均用到多满（0 < u ≤ 1，例如 0.75 留 25% 余量吸收突发）
        min_r / max_r             副本数上下限（min_r 可以是 0：KEDA 支持缩到 0）
        current                   当前副本数
        scale_down_stabilization  稳定窗口内**之前**算出来的原始推荐值（未经稳定、未经上下限裁剪）
        tolerance                 容忍度：负载与目标的比值在 1±tolerance 以内就不动（HPA 默认 0.1）

    规则（按顺序）：
    1. 参数校验：per_worker_concurrency ≥ 1，0 < target_utilization ≤ 1，0 ≤ min_r ≤ max_r，current ≥ 0，
       queue_depth / in_flight ≥ 0，否则抛 ValueError。
    2. 原始推荐 raw = ceil((queue_depth + in_flight) / (per_worker_concurrency × target_utilization))。
    3. 容忍度：current > 0 时，ratio = 负载 / (每副本目标 × current)；|ratio − 1| ≤ tolerance 则 raw = current。
    4. 稳定（与 Kubernetes HPA 的 stabilizeRecommendationWithBehaviors 相同的做法，扩容窗口为 0）：
         up   = raw
         down = max(raw, 窗口里的所有值)
         rec  = current；rec < up 则 rec = up；rec > down 则 rec = down
       效果：扩容立刻生效；缩容只缩到"窗口里最高的推荐值"，而且不会因为窗口里的旧值反而扩容。
    5. 最后裁剪到 [min_r, max_r]。
    """
    raise NotImplementedError("TODO: 实现 desired_replicas")


# ---------------------------------------------------------------------------------------------
# (b) validate_shutdown_timeline
# ---------------------------------------------------------------------------------------------

LEASE_RENEWAL_MODES = ("heartbeat", "stops_on_sigterm", "none")


def validate_shutdown_timeline(
    termination_grace: float,
    pre_stop: float,
    worker_grace: float,
    lease_seconds: float,
    max_job_seconds: float,
    *,
    heartbeat_seconds: float | None = None,
    lease_renewal: str = "heartbeat",
    cleanup_seconds: float = 5.0,
) -> list[dict]:
    """检查一个 worker 的停机配置，返回问题列表（没有问题返回 []）。

    参数：
        termination_grace   Pod 的 terminationGracePeriodSeconds（从 preStop 开始计时，到点 SIGKILL）
        pre_stop            preStop 钩子耗时（sleep 等）
        worker_grace        worker 收到 SIGTERM 后等在途任务的时间（run_async_worker 的 grace_period）
        lease_seconds       任务租约
        max_job_seconds     最长任务耗时（通常取 p99）
        heartbeat_seconds   续租间隔；None 表示取 lease_seconds / 3
        lease_renewal       "heartbeat"：整个过程持续续租（run_async_worker 的做法，排空期间也续）；
                            "stops_on_sigterm"：收到 SIGTERM 就不再续租；
                            "none"：从不续租（类似没有延长可见性超时的消息队列）
        cleanup_seconds     宽限期结束后的收尾（取消、写检查点、归还任务、flush 追踪、关连接池）

    每个问题是 {"code": ..., "severity": "error" | "warning", "message": 给人看的一句话}，按下面的顺序检查：
      1. sigkill_before_drain（error）：pre_stop + worker_grace + cleanup_seconds > termination_grace
         —— 排空还没结束就被 SIGKILL：被打断的任务既没记 cancelled 也没归还，只能等租约过期。
      2. grace_shorter_than_job（warning）：worker_grace < max_job_seconds
         —— 每次发布都会取消最长的那些任务，由别的 worker 从检查点接手（不是错误，但要求工具可重放）。
      3. heartbeat_too_sparse（error）：lease_renewal 不是 "none"，且 2 × 心跳间隔 > lease_seconds
         —— 丢一次心跳（GC 停顿、数据库抖动）租约就过期，任务被别人领走，同一个任务两处同时执行。
      4. lease_expires_mid_job（error）：lease_renewal == "none" 且 lease_seconds < max_job_seconds
         —— 任务还没做完租约就到期，被重新投递：重复执行。
      5. lease_expires_during_drain（error）：lease_renewal == "stops_on_sigterm"
         且 lease_seconds < min(max_job_seconds, pre_stop + worker_grace)
         —— 停机排空期间租约到期，别的 worker 接手时旧 worker 还在跑：重复执行。
    参数非法（termination_grace、lease_seconds、max_job_seconds ≤ 0，pre_stop、worker_grace、cleanup_seconds < 0，
    heartbeat_seconds ≤ 0，lease_renewal 不在 LEASE_RENEWAL_MODES 里）抛 ValueError。
    """
    raise NotImplementedError("TODO: 实现 validate_shutdown_timeline")


# ---------------------------------------------------------------------------------------------
# (c) evaluate_load_test
# ---------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """最近秩（nearest-rank）百分位：升序排序后取第 ceil(q/100 × n) 个（从 1 开始数）。空序列返回 None。已经写好，直接用。"""
    if not values:
        return None
    s = sorted(values)
    return s[max(1, math.ceil(len(s) * q / 100)) - 1]


def evaluate_load_test(samples: Iterable[dict], slo: dict) -> dict:
    """评估一次压测。

    samples：每个请求一条 dict：
        latency_s (float)  必填：端到端耗时
        ok (bool)          必填：是否成功
        status (int)       可选：HTTP 状态码，缺省时 ok 为 200、否则 500
        可选的耗时拆分（秒）：queue_wait_s（在队列里等 worker）、db_wait_s（等数据库连接）、
                              llm_s（等模型）、loop_lag_s（事件循环调度延迟，CPU 忙的信号）
    slo：{"p95_s": 必填, "p99_s": 可选, "max_error_rate": 必填, "max_rate_limited_ratio": 可选}

    返回 dict：
        n, p50, p95, p99        百分位只统计**成功**请求的延迟（失败的请求往往很快，混进来会美化延迟）
        error_rate              非 429 的失败 / 总数
        rate_limited_ratio      429 / 总数（429 是"按设计拒绝"，和错误分开统计）
        meets_slo, violations   违反的项目，取值 "p95" "p99" "error_rate" "rate_limited" "no_success"
        bottleneck              最可能的瓶颈，按优先级判断：
            "rate_limit"   rate_limited_ratio ≥ 0.05
            "db_pool"      成功请求里 db_wait_s 之和 / latency_s 之和 ≥ 0.2
            "event_loop"   loop_lag_s 占比 ≥ 0.2
            "queue_wait"   queue_wait_s 占比 ≥ 0.3（worker 不够或并发太低：按队列深度扩容）
            "model"        llm_s 占比 ≥ 0.5（瓶颈在模型：优化提示词、换模型、流式、缓存）
            "none"         以上都不满足
    samples 为空抛 ValueError。
    """
    raise NotImplementedError("TODO: 实现 evaluate_load_test")
