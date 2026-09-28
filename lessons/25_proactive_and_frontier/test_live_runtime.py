"""第 25 课：proactive_runtime 的真实运行测试（不是练习）。会真的拉起 5 个进程，跑 5～8 秒。

生产者进程写事件（其中一条重复投递）、两个定时器副本（本进程 A + 另一个进程 B）按真实墙钟触发、
2 个 worker 进程领任务发通知，其中一个在发完 SEV1 通知、确认任务之前 os._exit(1)。
断言只用确定性的量（进程号、任务 id、执行次数、通知条数），时间只作宽松上限。

运行：.venv/bin/python -m pytest lessons/25_proactive_and_frontier/test_live_runtime.py -v
"""

from __future__ import annotations

import os
from collections import Counter

import pytest

from agentkit.testing import load_sibling

rt = load_sibling(__file__, "proactive_runtime")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="WorkerPool 的进程管理按 POSIX 写")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    import asyncio

    return asyncio.run(rt.run_live(tmp_path_factory.mktemp("live"), period=1.0, gap=0.3, lease=1.0))


def test_components_really_run_in_separate_processes(report):
    pids = {report.main_pid, report.producer["pid"], report.scheduler_b["pid"], *report.worker_pids}
    assert len(pids) == 5  # 主进程、生产者、定时器 B、两个 worker：五个不同的操作系统进程
    assert report.producer["returncode"] == 0 and report.scheduler_b["returncode"] == 0
    assert {e["producer_pid"] for e in report.events} == {report.producer["pid"]}  # 事件是另一个进程写进来的
    assert {t["pid"] for t in report.triggers if t["who"] == "B"} == {report.scheduler_b["pid"]}
    assert {t["pid"] for t in report.triggers if t["who"] in ("A", "watcher")} == {report.main_pid}


def test_duplicate_delivery_becomes_one_job(report):
    assert [e["source_id"] for e in report.events].count("alert-7731") == 2
    triggers = [t for t in report.triggers if t["key"] == "notify:alert-7731"]
    assert len(triggers) == 2 and len({t["job_id"] for t in triggers}) == 1  # 两次触发，同一个任务
    assert sum(j.idempotency_key == "notify:alert-7731" for j in report.jobs) == 1
    assert report.watcher["duplicate_trigger"] == 1


def test_two_scheduler_replicas_fire_but_each_window_is_one_job(report):
    windows = report.digest_windows()
    assert windows, "定时器一次都没有触发"
    for key, by_who in windows.items():
        ids = {j for js in by_who.values() for j in js}
        assert len(ids) == 1, f"{key} 对应了多个任务 {ids}"
    assert any(set(by_who) == {"A", "B"} for by_who in windows.values())  # 至少一个周期两个副本都触发了
    digest_jobs = [j for j in report.jobs if j.kind == "digest"]
    assert len(digest_jobs) == len(windows)
    assert sum(1 for t in report.triggers if t["kind"] == "digest") > len(digest_jobs)  # 触发次数 > 任务数：去重真的发生了
    assert report.fired_a + report.scheduler_b["fired"] == sum(1 for t in report.triggers if t["kind"] == "digest")


def test_timer_fires_on_wall_clock_boundaries(report):
    late = report.lateness_ms()
    assert late and all(0 <= x < 500 for x in late)  # 宽松上限：机器负载高时 asyncio.sleep 也会晚醒一点
    for t in report.triggers:
        if t["target_at"] is not None:
            assert f"digest:{round(t['target_at'] / 1.0)}" == t["key"]  # 幂等键只取决于墙钟上的周期编号


def test_crash_after_side_effect_retries_but_notifies_once(report):
    job = report.job_by_key("notify:alert-7731")
    assert job.status == "succeeded" and job.attempts == 2
    runs = report.runs_for("notify:alert-7731")
    assert [(h["attempt"], h["outcome"]) for h in runs] == [(1, "sent"), (2, "duplicate_skipped")]
    assert runs[0]["pid"] != runs[1]["pid"]  # 第二次执行是另一个 worker 进程
    assert sum(n["key"] == "notify:alert-7731" for n in report.notifications) == 1
    assert sorted(report.worker_exit_codes) == [0, 1]  # 一个 worker 被 os._exit(1) 杀掉了
    assert any(e["event"] == "crash_injected" for e in report.worker_events)


def test_each_decision_is_acted_on_exactly_once(report):
    notified = Counter(n["key"] for n in report.notifications if n["kind"] == "notify")
    assert notified == Counter({"notify:alert-7731": 1, "notify:cal-19": 1, "notify:mail-483": 1})
    assert not any("技术周刊" in n["text"] for n in report.notifications)  # 决策器判了"不说"
    digests = [n for n in report.notifications if n["kind"] == "digest"]
    assert len(digests) == 1 and "PR #482" in digests[0]["text"]  # 专注时攒着的那条，恰好进了一次摘要
    focus_end = next(e["written_at"] for e in report.events if e["kind"] == "focus_end")
    assert digests[0]["sent_at"] > focus_end  # 专注期间不推摘要
    assert all(j.status == "succeeded" for j in report.jobs)
