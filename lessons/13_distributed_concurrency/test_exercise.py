"""第 13 课练习测试：离线、确定（断言不变量）。

运行：make lesson N=13    或    .venv/bin/python -m pytest lessons/13_distributed_concurrency -v

两类测试：
- 算法测试：一个连接、注入的假时间 T0，结果与真实时钟无关（例如"租约没过期之前不许接手"）；
- 并发测试：用 race.py 同时拉起 6~10 个**真实的 python 进程**，每个进程用自己的 sqlite3 连接调用你的函数，
  所有进程在同一条起跑线上一起开抢，用的是真实时钟。进程之间不共享任何内存，只能通过数据库协作 ——
  和多台机器上的 worker 一样。为了让有竞态的实现稳定地暴露出来，claim 的并发测试会在每条 SQL 执行前停 1 毫秒
  （sqlite3 的 trace 回调），把本来就存在的竞态窗口放大。
断言的都是确定性的量：每个任务被领取几次、每个 fence 发出去几次、计数器最后是多少；时间只用作宽松的超时上限。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict

import pytest

from agentkit.testing import load_exercise, load_sibling

ex = load_exercise(__file__)
jq = ex.jobqueue
ss = ex.session_store
race = load_sibling(__file__, "race")
IMPL = ex.__file__  # 子进程加载同一个实现：默认 exercise.py，AGENTKIT_SOLUTION=1 时是 solution.py

T0 = 1_000_000.0  # 假时间起点：算法测试里所有涉及时间的断言都用它，结果与真实时钟无关


def _queue(tmp_path, n: int = 1, *, clock=lambda: T0, **enqueue_kwargs):
    """建库并入队 n 个任务，返回 (db 路径, 一个连接, job_ids)。"""
    db = tmp_path / "jobs.db"
    q = jq.JobQueue(db, clock=clock)
    ids = [q.enqueue("acme", {"n": i}, f"key-{i}", **enqueue_kwargs)[0] for i in range(n)]
    q.close()
    return db, jq.connect(db), ids


# =====================================================================
# (a) claim_job
# =====================================================================


def test_claim_takes_oldest_job_and_sets_lease(tmp_path):
    _, conn, ids = _queue(tmp_path, 3)
    job = ex.claim_job(conn, "worker-1", 30, now=T0)
    assert job.id == ids[0] and job.payload == {"n": 0}
    assert (job.status, job.worker_id, job.lease_until) == ("leased", "worker-1", T0 + 30)
    assert (job.attempts, job.fence) == (1, 1)
    stored = jq.get_job(conn, job.id)  # 必须真的写进了数据库，而不只是返回值对
    assert (stored.status, stored.worker_id, stored.fence, stored.attempts) == ("leased", "worker-1", 1, 1)
    assert ex.claim_job(conn, "worker-2", 30, now=T0).id == ids[1]  # 已被领取的不会再发出去
    assert ex.claim_job(conn, "worker-3", 30, now=T0).id == ids[2]
    assert ex.claim_job(conn, "worker-4", 30, now=T0) is None  # 领完了


def test_claim_respects_available_at(tmp_path):
    _, conn, ids = _queue(tmp_path, 1, delay_s=5)  # 延迟任务 / 重试退避：T0+5 之前不许领
    assert ex.claim_job(conn, "worker-1", 30, now=T0 + 4) is None
    assert ex.claim_job(conn, "worker-1", 30, now=T0 + 6).id == ids[0]


def test_expired_lease_is_reclaimed_with_a_bigger_fence(tmp_path):
    _, conn, ids = _queue(tmp_path, 1)
    a = ex.claim_job(conn, "worker-A", 30, now=T0)
    assert ex.claim_job(conn, "worker-B", 30, now=T0 + 29.9) is None  # A 的租约还没到期
    b = ex.claim_job(conn, "worker-B", 30, now=T0 + 30.5)  # A 大概率崩溃了 → B 接手
    assert b.id == a.id == ids[0]
    assert (b.worker_id, b.attempts, b.fence, b.lease_until) == ("worker-B", 2, a.fence + 1, T0 + 60.5)


def test_claim_stops_handing_out_a_job_after_max_attempts(tmp_path):
    _, conn, _ = _queue(tmp_path, 1, max_attempts=2)
    assert ex.claim_job(conn, "worker-A", 10, now=T0).attempts == 1
    assert ex.claim_job(conn, "worker-B", 10, now=T0 + 11).attempts == 2  # A 崩了
    # B 也崩了：两次都没做完，大概率是"毒消息"，不能再发给第三个 worker 去送死
    assert ex.claim_job(conn, "worker-C", 10, now=T0 + 22) is None


def test_concurrent_claims_never_hand_out_a_job_twice(tmp_path):
    """8 个真实进程同时抢 40 个任务（每条 SQL 前停 1 毫秒放大竞态窗口）。"""
    db, conn, ids = _queue(tmp_path, 40, clock=time.time)
    results = race.run_race(8, "claim", db=db, impl=IMPL, lease=30, widen=True)
    claimed = [(r["worker"], c["id"], c["fence"]) for r in results for c in r["claims"]]
    claimed_ids = [job_id for _, job_id, _ in claimed]
    assert sorted(claimed_ids) == sorted(ids), (
        f"有任务被领取了两次，或者有任务没被领取：{len(claimed_ids)} 次领取，{len(set(claimed_ids))} 个不同的任务"
    )
    assert all(fence == 1 for _, _, fence in claimed)
    assert sum(1 for r in results if r["claims"]) >= 2, "竞争没有真正发生：只有一个进程领到了任务"
    for worker, job_id, _ in claimed:  # 数据库里记录的持有者，必须就是拿到它的那个进程
        stored = jq.get_job(conn, job_id)
        assert (stored.worker_id, stored.attempts, stored.fence) == (worker, 1, 1)


def test_fences_are_handed_out_once_each_under_lease_churn(tmp_path):
    """6 个进程反复抢 3 个任务：租约只有 20 毫秒、领了就不管（模拟 worker 一个接一个地卡死），跑 1 秒。

    对每个任务：fence 从 1 开始连续递增、每个值只发出去一次；每次接手都发生在上一个租约过期之后。
    有竞态的领取会把同一个 fence 发给两个进程 —— 那样 fencing 就失效了（两个"最新持有者"）。
    """
    db, conn, ids = _queue(tmp_path, 3, clock=time.time, max_attempts=1_000_000)
    results = race.run_race(6, "claim", db=db, impl=IMPL, lease=0.02, duration=1.0)
    by_job: dict[int, list[dict]] = defaultdict(list)
    for r in results:
        for c in r["claims"]:
            by_job[c["id"]].append({**c, "worker": r["worker"]})
    assert set(by_job) == set(ids)
    for job_id, claims in by_job.items():
        claims.sort(key=lambda c: c["fence"])
        fences = [c["fence"] for c in claims]
        assert fences == list(range(1, len(fences) + 1)), f"任务 #{job_id} 的 fence 有重复或跳号：{fences[:20]}…"
        assert all(c["attempts"] == c["fence"] for c in claims)  # 每次领取 attempts 和 fence 各 +1
        for prev, nxt in zip(claims, claims[1:]):
            assert nxt["now"] > prev["lease_until"], "上一个租约还没过期就被别人接手了"
        assert jq.get_job(conn, job_id).fence == fences[-1]
    takeovers = {c["worker"] for claims in by_job.values() for c in claims[1:]}
    assert len(takeovers) >= 2, "竞争没有真正发生：接手的总是同一个进程"


# =====================================================================
# (b) complete_job
# =====================================================================


def test_complete_with_current_fence(tmp_path):
    _, conn, _ = _queue(tmp_path, 1)
    job = ex.claim_job(conn, "worker-1", 30, now=T0)
    ex.complete_job(conn, job.id, job.fence, "已创建工单 T-1001", now=T0 + 1)
    stored = jq.get_job(conn, job.id)
    assert (stored.status, stored.result, stored.lease_until) == ("succeeded", "已创建工单 T-1001", None)


def test_zombie_worker_cannot_complete_after_takeover(tmp_path):
    _, conn, _ = _queue(tmp_path, 1)
    a = ex.claim_job(conn, "worker-A", 30, now=T0)
    b = ex.claim_job(conn, "worker-B", 30, now=T0 + 31)  # A 卡住（比如 GC 停顿）→ 租约过期 → B 接手
    with pytest.raises(jq.LeaseLostError):  # A 醒过来，拿着旧 fence 来提交
        ex.complete_job(conn, a.id, a.fence, "A 的过期结果", now=T0 + 32)
    stored = jq.get_job(conn, a.id)
    assert (stored.status, stored.worker_id, stored.result) == ("leased", "worker-B", None)  # 什么都没被改
    ex.complete_job(conn, b.id, b.fence, "B 的结果", now=T0 + 33)
    assert jq.get_job(conn, b.id).result == "B 的结果"


def test_complete_rejects_finished_or_unknown_jobs(tmp_path):
    _, conn, _ = _queue(tmp_path, 1)
    job = ex.claim_job(conn, "worker-1", 30, now=T0)
    ex.complete_job(conn, job.id, job.fence, "第一次", now=T0 + 1)
    with pytest.raises(jq.LeaseLostError):  # 重复提交：任务已经结束
        ex.complete_job(conn, job.id, job.fence, "第二次", now=T0 + 2)
    assert jq.get_job(conn, job.id).result == "第一次"
    with pytest.raises(jq.LeaseLostError):
        ex.complete_job(conn, 999, 1, "不存在的任务", now=T0 + 3)


def test_late_complete_is_accepted_if_nobody_took_over(tmp_path):
    """fencing 看的是"你是不是最新的持有者"，而不是"租约有没有过期"。"""
    _, conn, _ = _queue(tmp_path, 1)
    job = ex.claim_job(conn, "worker-1", 30, now=T0)
    ex.complete_job(conn, job.id, job.fence, "迟到但有效", now=T0 + 100)  # 租约早过期了，但没人接手
    assert jq.get_job(conn, job.id).status == "succeeded"


# =====================================================================
# (c) update_session_with_retry
# =====================================================================


def _sneak_in(other, session_id: str, text: str) -> None:
    """另一个 worker 抢在我们前面写了一次（用它确定性地制造冲突）。"""
    cur = other.get(session_id)
    other.compare_and_set(session_id, cur.version, {"messages": cur.data.get("messages", []) + [text]})


def test_conflict_rereads_and_reapplies_update(tmp_path):
    db = tmp_path / "sessions.db"
    store, other = ss.SessionStore(db), ss.SessionStore(db)
    store.compare_and_set("s1", 0, {"messages": ["你好"]})
    seen = []

    def add_my_message(data: dict) -> dict:
        seen.append(list(data["messages"]))
        if len(seen) == 1:  # 我读完之后、写之前，手机端抢先写入了一条
            _sneak_in(other, "s1", "手机端的消息")
        data["messages"].append("电脑端的消息")
        return data

    result = ex.update_session_with_retry(store, "s1", add_my_message, backoff_s=0)
    assert seen == [["你好"], ["你好", "手机端的消息"]], "冲突后必须重新读取最新版本，再应用一次修改"
    assert result.data["messages"] == ["你好", "手机端的消息", "电脑端的消息"]  # 两条都在，谁也没丢
    assert result.version == 3 and store.get("s1").data == result.data


def test_gives_up_after_max_attempts(tmp_path):
    db = tmp_path / "sessions.db"
    store, other = ss.SessionStore(db), ss.SessionStore(db)
    calls, conflicts = [], []

    def always_loses(data: dict) -> dict:
        calls.append(1)
        _sneak_in(other, "s1", f"别人的第 {len(calls)} 次写入")  # 每次都有人抢先
        return {"messages": ["我的写入"]}

    with pytest.raises(ss.ConflictError):
        ex.update_session_with_retry(
            store, "s1", always_loses, max_attempts=4, backoff_s=0,
            on_conflict=lambda attempt, err: conflicts.append((attempt, type(err).__name__)),
        )
    assert len(calls) == 4
    assert conflicts == [(1, "ConflictError"), (2, "ConflictError"), (3, "ConflictError"), (4, "ConflictError")]
    assert "我的写入" not in store.get("s1").data["messages"]  # 放弃就是放弃：没有写入任何东西


def test_backoff_between_retries_but_not_after_giving_up(tmp_path):
    db = tmp_path / "sessions.db"
    store, other = ss.SessionStore(db), ss.SessionStore(db)
    slept: list[float] = []
    calls = []

    def loses_three_times(data: dict) -> dict:
        calls.append(1)
        if len(calls) <= 3:
            _sneak_in(other, "s1", f"抢先 {len(calls)}")
        data.setdefault("messages", []).append("终于写进去了")
        return data

    result = ex.update_session_with_retry(store, "s1", loses_three_times, backoff_s=0.01, sleep=slept.append)
    assert result.data["messages"][-1] == "终于写进去了" and len(calls) == 4
    assert len(slept) == 3  # 冲突 3 次 → 等 3 次（全抖动：每次在 [0, 上限] 里随机）
    for n, s in enumerate(slept, start=1):
        assert 0 <= s <= 0.01 * 2 ** (n - 1)

    def always_loses(data: dict) -> dict:
        _sneak_in(other, "s1", "又被抢先了")
        return data

    slept.clear()
    with pytest.raises(ss.ConflictError):  # 最后一次失败后直接放弃，不再白等
        ex.update_session_with_retry(store, "s1", always_loses, max_attempts=2, backoff_s=0.01, sleep=slept.append)
    assert len(slept) == 1


def test_100_concurrent_increments_lose_nothing(tmp_path):
    """10 个真实进程，各对同一个会话做 10 次"读 → 处理 1 毫秒 → 写回 +1"。"""
    db = tmp_path / "sessions.db"
    ss.SessionStore(db).close()  # 先建好库，再让 10 个进程同时连上来
    results = race.run_race(10, "increment", db=db, impl=IMPL, times=10, think=0.001, max_attempts=200, backoff=0.001)
    final = ss.SessionStore(db).get("counter")
    assert final.data["count"] == 100, f"丢失了 {100 - final.data['count']} 次更新"
    assert final.version == 100  # 每次成功写入恰好 +1：没有多写，也没有少写
    assert sum(r["conflicts"] for r in results) > 0, "竞争没有真正发生：一次 CAS 冲突都没有"


# =====================================================================
# 事件循环里的阻塞调用（不是练习：jobqueue.AsyncJobQueue 已经写好）
# =====================================================================


async def _hold_write_lock(db, seconds: float):
    """另一个真实进程拿到写锁（BEGIN IMMEDIATE）后握住 seconds 秒。返回时锁已经被它拿到了。"""
    script = (
        "import sqlite3, sys, time\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "print('locked', flush=True)\n"
        f"time.sleep({seconds})\n"
        "c.execute('COMMIT')\n"
    )
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(db), stdout=asyncio.subprocess.PIPE)
    assert (await proc.stdout.readline()).strip() == b"locked"
    return proc


async def _ticks_while(db, call) -> tuple[int, object]:
    """另一个进程握着写锁时执行 call()，数一数这段时间里一个每 10 毫秒醒一次的协程跑了几次。"""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    holder = await _hold_write_lock(db, 0.8)
    ticks = 0
    job = await call()
    seen = ticks  # call() 返回后立刻读数，中间没有 await
    task.cancel()
    await holder.wait()
    return seen, job


async def test_async_jobqueue_keeps_the_event_loop_running(tmp_path):
    """同步的 JobQueue.claim 直接在事件循环里调用：等写锁的 0.8 秒里，别的协程（比如心跳）一次都跑不了。
    经过 AsyncJobQueue（专用线程）：事件循环照常运转。这就是 README 3.11 节的决定依据。"""
    db, conn, _ = _queue(tmp_path, 2, clock=time.time)
    conn.close()

    q = jq.JobQueue(db)

    async def blocking():
        return q.claim("w-blocking", 30)  # ❌ 阻塞的 sqlite3 调用跑在事件循环线程里

    blocked_ticks, job1 = await _ticks_while(db, blocking)
    q.close()

    aq = jq.AsyncJobQueue(db)

    async def offloaded():
        return await aq.claim("w-async", 30)  # ✅ 在专用线程里等锁，事件循环不受影响

    free_ticks, job2 = await _ticks_while(db, offloaded)
    await aq.close()

    assert job1 is not None and job2 is not None and job1.id != job2.id
    assert blocked_ticks == 0, "阻塞调用期间事件循环本该一动不动"
    assert free_ticks >= 20, f"专用线程等锁时事件循环应该照常运转，只跑了 {free_ticks} 次"
