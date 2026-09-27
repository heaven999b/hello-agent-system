"""第 31 课练习测试（纯函数，毫秒级）。

运行：make lesson N=31    或    .venv/bin/python -m pytest lessons/31_deployment_and_scaling -v
"""

from __future__ import annotations

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# ------------------------------------------------------------------ (a) desired_replicas


def test_scale_up_by_queue_depth_plus_in_flight():
    # 负载 40，每副本目标 8 × 0.75 = 6 → ceil(40/6) = 7；扩容立刻生效，不受窗口影响
    assert ex.desired_replicas(30, 10, 8, 0.75, 1, 20, current=2) == 7
    assert ex.desired_replicas(30, 10, 8, 0.75, 1, 20, current=2, scale_down_stabilization=[1, 2]) == 7


def test_bounds_and_scale_to_zero():
    assert ex.desired_replicas(10_000, 0, 16, 1.0, 1, 20, current=5) == 20  # 上限
    assert ex.desired_replicas(0, 0, 16, 0.75, 0, 20, current=0) == 0  # KEDA：没活可干时缩到 0
    assert ex.desired_replicas(0, 0, 16, 0.75, 2, 20, current=2) == 2  # 下限
    assert ex.desired_replicas(1, 0, 16, 0.75, 0, 20, current=0) == 1  # 来了一个任务：0 → 1


def test_tolerance_ignores_small_fluctuations():
    # 每副本目标 6，5 个副本的容量 30；负载 31 → 比值 1.033，在 10% 容忍度内：不动（否则会变成 6）
    assert ex.desired_replicas(21, 10, 8, 0.75, 1, 20, current=5) == 5
    assert ex.desired_replicas(21, 10, 8, 0.75, 1, 20, current=5, tolerance=0.0) == 6
    assert ex.desired_replicas(30, 10, 8, 0.75, 1, 20, current=5) == 7  # 比值 1.33：超出容忍度，照常扩


def test_scale_down_waits_for_the_stabilization_window():
    # 负载掉到只需要 3 个，但窗口里最近还推荐过 6 → 先只缩到 6（避免"刚缩完积压又回来"的抖动）
    assert ex.desired_replicas(10, 5, 8, 0.75, 1, 20, current=8, scale_down_stabilization=[6, 4]) == 6
    assert ex.desired_replicas(10, 5, 8, 0.75, 1, 20, current=8, scale_down_stabilization=[]) == 3
    # 窗口里的旧值比当前还高：不会因此扩容，保持当前
    assert ex.desired_replicas(10, 5, 8, 0.75, 1, 20, current=8, scale_down_stabilization=[9]) == 8


def test_invalid_arguments_raise():
    for kwargs in (
        dict(per_worker_concurrency=0), dict(target_utilization=0), dict(target_utilization=1.5),
        dict(min_r=5, max_r=2), dict(current=-1), dict(queue_depth=-3),
    ):
        args = dict(queue_depth=1, in_flight=0, per_worker_concurrency=4, target_utilization=0.5, min_r=0, max_r=5, current=1)
        args.update(kwargs)
        with pytest.raises(ValueError):
            ex.desired_replicas(**args)


# ------------------------------------------------------------------ (b) validate_shutdown_timeline


def codes(issues):
    return [i["code"] for i in issues]


def test_reference_service_timeline_is_clean():
    # deploy/k8s/worker.yaml：grace 35s，无 preStop，排空 20s，租约 30s（心跳 10s），p99 任务 15s
    assert ex.validate_shutdown_timeline(35, 0, 20, 30, 15) == []


def test_sigkill_before_drain_finishes():
    issues = ex.validate_shutdown_timeline(30, 5, 25, 30, 15)  # 5 + 25 + 5 = 35 > 30
    assert codes(issues) == ["sigkill_before_drain"] and issues[0]["severity"] == "error"


def test_grace_shorter_than_longest_job_is_a_warning_not_an_error():
    issues = ex.validate_shutdown_timeline(60, 0, 20, 30, 120)
    assert codes(issues) == ["grace_shorter_than_job"] and issues[0]["severity"] == "warning"


def test_lease_shorter_than_job_without_renewal_means_duplicate_execution():
    issues = ex.validate_shutdown_timeline(60, 0, 40, 30, 45, lease_renewal="none")
    assert codes(issues) == ["grace_shorter_than_job", "lease_expires_mid_job"]
    assert [i["severity"] for i in issues] == ["warning", "error"]
    assert ex.validate_shutdown_timeline(60, 0, 50, 60, 45, lease_renewal="none") == []  # 租约盖住最长任务：没问题


def test_lease_shorter_than_drain_when_renewal_stops_on_sigterm():
    # 收到 SIGTERM 就不续租：租约 15s < min(最长任务 40s, 排空窗口 5+20=25s) → 排空期间被别人接手
    issues = ex.validate_shutdown_timeline(40, 5, 20, 15, 40, lease_renewal="stops_on_sigterm")
    assert "lease_expires_during_drain" in codes(issues)
    # 同样的数字，但排空期间持续续租（run_async_worker 的做法）：租约比宽限期短也没关系
    assert "lease_expires_during_drain" not in codes(ex.validate_shutdown_timeline(40, 5, 20, 15, 40))
    # 任务本身都很短（10s < 租约 15s）：就算不续租也来不及重复
    assert ex.validate_shutdown_timeline(40, 5, 20, 15, 10, lease_renewal="stops_on_sigterm") == []


def test_sparse_heartbeat_and_invalid_arguments():
    assert codes(ex.validate_shutdown_timeline(35, 0, 20, 30, 15, heartbeat_seconds=20)) == ["heartbeat_too_sparse"]
    assert ex.validate_shutdown_timeline(35, 0, 20, 30, 15, heartbeat_seconds=20, lease_renewal="none") == []
    for bad in (dict(termination_grace=0), dict(lease_seconds=-1), dict(pre_stop=-1), dict(heartbeat_seconds=0),
                dict(lease_renewal="sometimes")):
        args = dict(termination_grace=35, pre_stop=0, worker_grace=20, lease_seconds=30, max_job_seconds=15)
        args.update(bad)
        with pytest.raises(ValueError):
            ex.validate_shutdown_timeline(**args)


# ------------------------------------------------------------------ (c) evaluate_load_test


def samples_1_to_100(**extra):
    return [{"latency_s": float(i), "ok": True, **extra} for i in range(1, 101)]


def test_percentiles_use_nearest_rank_over_successful_requests():
    data = samples_1_to_100() + [{"latency_s": 0.01, "ok": False, "status": 500}] * 5  # 失败的请求很快：不能拿来美化延迟
    r = ex.evaluate_load_test(data, {"p95_s": 100, "max_error_rate": 0.1})
    assert (r["p50"], r["p95"], r["p99"]) == (50.0, 95.0, 99.0)
    assert r["n"] == 105 and r["error_rate"] == pytest.approx(5 / 105)


def test_slo_verdict_and_violations():
    data = samples_1_to_100()
    assert ex.evaluate_load_test(data, {"p95_s": 95, "p99_s": 99, "max_error_rate": 0.0})["meets_slo"]
    r = ex.evaluate_load_test(data, {"p95_s": 90, "p99_s": 98, "max_error_rate": 0.0})
    assert not r["meets_slo"] and r["violations"] == ["p95", "p99"]
    r = ex.evaluate_load_test([{"latency_s": 1, "ok": False}] * 3, {"p95_s": 5, "max_error_rate": 0.5})
    assert r["violations"] == ["no_success", "error_rate"] and r["p50"] is None


def test_429_is_counted_separately_from_errors():
    data = samples_1_to_100() + [{"latency_s": 0.001, "ok": False, "status": 429}] * 25
    r = ex.evaluate_load_test(data, {"p95_s": 100, "max_error_rate": 0.01, "max_rate_limited_ratio": 0.1})
    assert r["error_rate"] == 0 and r["rate_limited_ratio"] == pytest.approx(0.2)
    assert r["violations"] == ["rate_limited"] and r["bottleneck"] == "rate_limit"


@pytest.mark.parametrize(
    "breakdown, expected",
    [
        ({"db_wait_s": 0.3, "llm_s": 0.5}, "db_pool"),           # 30% 的时间在等连接池
        ({"loop_lag_s": 0.25, "llm_s": 0.6}, "event_loop"),      # 事件循环忙不过来（CPU）
        ({"queue_wait_s": 0.4, "llm_s": 0.5}, "queue_wait"),     # 在队列里等 worker
        ({"llm_s": 0.8, "queue_wait_s": 0.05}, "model"),         # 大头在模型
        ({"llm_s": 0.3}, "none"),
    ],
)
def test_bottleneck_hint(breakdown, expected):
    data = [{"latency_s": 1.0, "ok": True, **breakdown} for _ in range(20)]
    assert ex.evaluate_load_test(data, {"p95_s": 2, "max_error_rate": 0})["bottleneck"] == expected


def test_empty_samples_raise():
    with pytest.raises(ValueError):
        ex.evaluate_load_test([], {"p95_s": 1, "max_error_rate": 0})
