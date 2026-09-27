"""第 13 课练习测试：离线、确定（假时间 + 断言不变量），并用多线程制造真实竞争。

运行：make lesson N=13    或    .venv/bin/python -m pytest lessons/13_distributed_concurrency -v

为什么多线程能测出并发 bug？每个线程用自己的 sqlite3 连接，sqlite3 在执行 SQL 时会释放 GIL，
所以多个线程的 SQL 是真正交错执行的。为了让"有竞态的实现"稳定地暴露出来，
并发测试会给每个连接装一个 trace 回调，在每条 SQL 执行前停 1 毫秒，把竞态窗口放大。
"""

from __future__ import annotations

import threading
import time

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
jq = ex.jobqueue
ss = ex.session_store

T0 = 1_000_000.0  # 假时间起点：所有涉及时间的断言都用它，结果与真实时钟无关


def _widen_race(_sql: str) -> None:
    time.sleep(0.001)


def _queue(tmp_path, n: int = 1, **enqueue_kwargs):
    """建库并入队 n 个任务，返回 (db 路径, 一个连接, job_ids)。"""
    db = tmp_path / "jobs.db"
    q = jq.JobQueue(db, clock=lambda: T0)
    ids = [q.enqueue("acme", {"n": i}, f"key-{i}", **enqueue_kwargs)[0] for i in range(n)]
    q.close()
    return db, jq.connect(db), ids


def _run_threads(n: int, target) -> None:
    """启动 n 个线程同时执行 target(i)；任何线程里的异常都在主线程重新抛出（包括 NotImplementedError）。"""
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def wrapper(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            target(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            barrier.abort()

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    real = [e for e in errors if not isinstance(e, threading.BrokenBarrierError)]
    if real:
        raise real[0]
    assert not errors, errors
    assert not any(t.is_alive() for t in threads), "有线程卡住了（死循环或死锁？）"


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
    db, conn, ids = _queue(tmp_path, 40)
    claimed: list[tuple[str, int, int]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        my_conn = jq.connect(db)  # 每个线程自己的连接
        my_conn.set_trace_callback(_widen_race)
        try:
            while (job := ex.claim_job(my_conn, f"worker-{i}", 30, now=T0)) is not None:
                with lock:
                    claimed.append((job.worker_id, job.id, job.fence))
        finally:
            my_conn.close()

    _run_threads(8, worker)
    claimed_ids = [job_id for _, job_id, _ in claimed]
    assert sorted(claimed_ids) == sorted(ids), "有任务被领取了两次，或者有任务没被领取"
    assert all(fence == 1 for _, _, fence in claimed)
    for worker_id, job_id, _ in claimed:  # 数据库里记录的持有者，必须就是拿到它的那个 worker
        stored = jq.get_job(conn, job_id)
        assert (stored.worker_id, stored.attempts, stored.fence) == (worker_id, 1, 1)


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
    db = tmp_path / "sessions.db"
    ss.SessionStore(db).close()  # 先建好库，再让 10 个线程同时连上来

    def increment(data: dict) -> dict:
        time.sleep(0.001)  # 模拟"读完之后要处理一会儿"，让读和写之间真的有别人插进来
        data["count"] = data.get("count", 0) + 1
        return data

    def worker(_i: int) -> None:
        store = ss.SessionStore(db)
        try:
            for _ in range(10):
                ex.update_session_with_retry(store, "counter", increment, max_attempts=200, backoff_s=0.001)
        finally:
            store.close()

    _run_threads(10, worker)
    final = ss.SessionStore(db).get("counter")
    assert final.data["count"] == 100, f"丢失了 {100 - final.data['count']} 次更新"
    assert final.version == 100  # 每次成功写入恰好 +1：没有多写，也没有少写
