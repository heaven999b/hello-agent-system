"""第 31 课练习参考答案（接口与 exercise.py 完全一致）。"""

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
    if per_worker_concurrency < 1:
        raise ValueError("per_worker_concurrency 至少为 1")
    if not 0 < target_utilization <= 1:
        raise ValueError("target_utilization 必须在 (0, 1] 之间")
    if min_r < 0 or max_r < min_r:
        raise ValueError("需要 0 ≤ min_r ≤ max_r")
    if current < 0 or queue_depth < 0 or in_flight < 0:
        raise ValueError("current、queue_depth、in_flight 不能为负")

    load = queue_depth + in_flight
    per_replica = per_worker_concurrency * target_utilization
    raw = math.ceil(load / per_replica)
    if current > 0 and abs(load / (per_replica * current) - 1) <= tolerance:
        raw = current  # 在容忍范围内：不为一点点波动来回扩缩

    up = raw  # 扩容窗口为 0：窗口里只有本次推荐
    down = max([raw, *scale_down_stabilization])
    rec = current
    if rec < up:
        rec = up
    if rec > down:
        rec = down
    return max(min_r, min(max_r, rec))


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
    if termination_grace <= 0 or lease_seconds <= 0 or max_job_seconds <= 0:
        raise ValueError("termination_grace、lease_seconds、max_job_seconds 必须大于 0")
    if pre_stop < 0 or worker_grace < 0 or cleanup_seconds < 0:
        raise ValueError("pre_stop、worker_grace、cleanup_seconds 不能为负")
    if heartbeat_seconds is not None and heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds 必须大于 0")
    if lease_renewal not in LEASE_RENEWAL_MODES:
        raise ValueError(f"lease_renewal 只能是 {LEASE_RENEWAL_MODES}")

    issues: list[dict] = []

    def add(code: str, severity: str, message: str) -> None:
        issues.append({"code": code, "severity": severity, "message": message})

    needed = pre_stop + worker_grace + cleanup_seconds
    if needed > termination_grace:
        add("sigkill_before_drain", "error",
            f"preStop {pre_stop}s + 排空 {worker_grace}s + 收尾 {cleanup_seconds}s = {needed}s，"
            f"超过 terminationGracePeriodSeconds {termination_grace}s：排空没结束就会被 SIGKILL")
    if worker_grace < max_job_seconds:
        add("grace_shorter_than_job", "warning",
            f"宽限期 {worker_grace}s 短于最长任务 {max_job_seconds}s：每次发布都会取消长任务，由别的 worker 从检查点接手")
    heartbeat = heartbeat_seconds if heartbeat_seconds is not None else lease_seconds / 3
    if lease_renewal != "none" and 2 * heartbeat > lease_seconds:
        add("heartbeat_too_sparse", "error",
            f"心跳间隔 {heartbeat}s 的两倍超过租约 {lease_seconds}s：丢一次心跳就失去租约，任务会被两处同时执行")
    if lease_renewal == "none" and lease_seconds < max_job_seconds:
        add("lease_expires_mid_job", "error",
            f"租约 {lease_seconds}s 短于最长任务 {max_job_seconds}s 且不续租：任务没做完就被重新投递，重复执行")
    if lease_renewal == "stops_on_sigterm":
        window = min(max_job_seconds, pre_stop + worker_grace)
        if lease_seconds < window:
            add("lease_expires_during_drain", "error",
                f"收到 SIGTERM 后不再续租，而租约 {lease_seconds}s 短于排空窗口 {window}s：别的 worker 接手时旧 worker 还在跑")
    return issues


# ---------------------------------------------------------------------------------------------
# (c) evaluate_load_test
# ---------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(1, math.ceil(len(s) * q / 100)) - 1]


BOTTLENECK_RULES = (  # (字段, 占比阈值, 瓶颈)：按优先级排列
    ("db_wait_s", 0.2, "db_pool"),
    ("loop_lag_s", 0.2, "event_loop"),
    ("queue_wait_s", 0.3, "queue_wait"),
    ("llm_s", 0.5, "model"),
)


def evaluate_load_test(samples: Iterable[dict], slo: dict) -> dict:
    samples = list(samples)
    if not samples:
        raise ValueError("没有样本")
    n = len(samples)

    def status(s: dict) -> int:
        return s.get("status", 200 if s["ok"] else 500)

    ok = [s for s in samples if s["ok"]]
    latencies = [s["latency_s"] for s in ok]
    limited = sum(status(s) == 429 for s in samples)
    errors = sum((not s["ok"]) and status(s) != 429 for s in samples)
    result = {
        "n": n,
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "error_rate": errors / n,
        "rate_limited_ratio": limited / n,
    }

    violations = []
    if not ok:
        violations.append("no_success")
    else:
        if result["p95"] > slo["p95_s"]:
            violations.append("p95")
        if slo.get("p99_s") is not None and result["p99"] > slo["p99_s"]:
            violations.append("p99")
    if result["error_rate"] > slo["max_error_rate"]:
        violations.append("error_rate")
    if slo.get("max_rate_limited_ratio") is not None and result["rate_limited_ratio"] > slo["max_rate_limited_ratio"]:
        violations.append("rate_limited")

    bottleneck = "none"
    if result["rate_limited_ratio"] >= 0.05:
        bottleneck = "rate_limit"
    else:
        total = sum(latencies)
        for field, threshold, name in BOTTLENECK_RULES:
            if total > 0 and sum(s.get(field, 0.0) for s in ok) / total >= threshold:
                bottleneck = name
                break

    result.update(meets_slo=not violations, violations=violations, bottleneck=bottleneck)
    return result
