"""第 26 课练习测试：真实 Postgres（嵌入式）+ fakeredis（支持 Lua），多线程制造真实竞争。

运行：make lesson N=26    或    .venv/bin/python -m pytest lessons/26_state_and_queues -v

pg_uri / redis_client 两个 fixture 来自仓库根目录的 conftest.py：每个测试一个全新的数据库、一个清空的 Redis。
没装可选依赖时这些测试会被自动跳过（pip install -e ".[prod,prod-local]"）。
"""

from __future__ import annotations

import json
import threading

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)


def _connect(pg_uri: str):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    conn = psycopg.connect(pg_uri, autocommit=True, row_factory=dict_row)
    conn.execute("SET lock_timeout = '2s'")  # 实现如果在等别人的行锁，2 秒后报错，而不是让测试卡死
    return conn


@pytest.fixture
def conn(pg_uri):
    c = _connect(pg_uri)
    ex.setup(c)
    yield c
    c.close()


def run_threads(n: int, target) -> None:
    """n 个线程同时开始执行 target(i)；任何线程里的异常（包括 NotImplementedError）都在主线程重新抛出。"""
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
    assert not any(t.is_alive() for t in threads), "有线程卡住了（死循环或死锁？）"


# =====================================================================
# (a) cas_save
# =====================================================================


def test_cas_creates_a_new_run_only_once(conn):
    assert ex.cas_save(conn, "r1", 0, json.dumps({"step": 1})) is True
    row = ex.load_run(conn, "r1")
    assert (row["version"], row["state"]) == (1, {"step": 1})
    assert ex.cas_save(conn, "r1", 0, json.dumps({"step": 99})) is False  # 别人已经创建了它
    assert ex.load_run(conn, "r1")["state"] == {"step": 1}


def test_cas_update_needs_the_current_version(conn):
    ex.cas_save(conn, "r1", 0, json.dumps({"step": 1}))
    assert ex.cas_save(conn, "r1", 1, json.dumps({"step": 2})) is True
    assert ex.load_run(conn, "r1")["version"] == 2
    # 僵尸 worker 拿着旧版本号来写：被拒绝，而且什么都没改
    assert ex.cas_save(conn, "r1", 1, json.dumps({"step": "stale"})) is False
    row = ex.load_run(conn, "r1")
    assert (row["version"], row["state"]) == (2, {"step": 2})
    assert ex.cas_save(conn, "missing", 5, "{}") is False  # 不存在的 run 也不会被"更新"出来


def test_cas_loses_no_update_under_concurrency(pg_uri, conn):
    ex.cas_save(conn, "counter", 0, json.dumps({"n": 0}))

    def worker(i: int) -> None:
        mine = _connect(pg_uri)
        for _ in range(10):
            while True:  # 读最新版本 → 改 → CAS；冲突就重来
                row = ex.load_run(mine, "counter")
                if ex.cas_save(mine, "counter", row["version"], json.dumps({"n": row["state"]["n"] + 1})):
                    break
        mine.close()

    run_threads(8, worker)
    row = ex.load_run(conn, "counter")
    assert row["state"]["n"] == 80, "有更新丢了：CAS 条件没有起作用"
    assert row["version"] == 81


# =====================================================================
# (b) claim_one
# =====================================================================


def test_claim_takes_the_oldest_ready_job_and_writes_a_lease(conn):
    first = ex.enqueue(conn, {"n": 1})
    later = ex.enqueue(conn, {"n": 2}, delay_seconds=60)  # 一分钟后才能领
    second = ex.enqueue(conn, {"n": 3})
    job = ex.claim_one(conn, "worker-1", 30)
    assert job["id"] == first and job["payload"] == {"n": 1}
    assert (job["status"], job["worker_id"], job["attempts"], job["fence"]) == ("leased", "worker-1", 1, 1)
    lease_left = conn.execute("SELECT EXTRACT(EPOCH FROM lease_until - now())::float AS s FROM jobs WHERE id = %s",
                              (first,)).fetchone()["s"]
    assert 25 < lease_left <= 30  # 租约用数据库的时钟：now() + 30 秒
    assert ex.claim_one(conn, "worker-2", 30)["id"] == second
    assert ex.claim_one(conn, "worker-3", 30) is None  # 剩下的那个还没到时间
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (later,)).fetchone()["status"] == "queued"


def test_claim_skips_a_row_locked_by_another_worker(pg_uri, conn):
    first = ex.enqueue(conn)
    second = ex.enqueue(conn)
    psycopg = pytest.importorskip("psycopg")
    holder = _connect(pg_uri)
    with holder.transaction():  # 另一个 worker 锁着第一行，迟迟不提交
        holder.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (first,))
        try:
            job = ex.claim_one(conn, "worker-2", 30)
        except psycopg.errors.LockNotAvailable:  # conn 设了 lock_timeout = 2s
            pytest.fail("在排队等别人的行锁（等了 2 秒超时）：是不是少了 SKIP LOCKED？")
    holder.close()
    assert job["id"] == second, "应该跳过被锁住的行，去拿下一个"


def test_expired_lease_is_reclaimed_with_a_bigger_fence_and_poison_goes_dead(conn):
    jid = ex.enqueue(conn, max_attempts=2)
    a = ex.claim_one(conn, "worker-A", 30)
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (jid,))  # A 崩溃了
    b = ex.claim_one(conn, "worker-B", 30)
    assert b["id"] == jid and (b["worker_id"], b["attempts"], b["fence"]) == ("worker-B", 2, a["fence"] + 1)
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (jid,))  # B 也崩溃了
    assert ex.claim_one(conn, "worker-C", 30) is None  # 次数用尽：不能再发给第三个 worker 去送死
    row = conn.execute("SELECT status, lease_until FROM jobs WHERE id = %s", (jid,)).fetchone()
    assert (row["status"], row["lease_until"]) == ("dead", None)


def test_concurrent_claims_never_hand_out_a_job_twice(pg_uri, conn):
    ids = {ex.enqueue(conn, {"i": i}) for i in range(40)}
    claimed: list[tuple[str, int]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        mine = _connect(pg_uri)  # 每个 worker 自己的连接
        while (job := ex.claim_one(mine, f"worker-{i}", 30)) is not None:
            with lock:
                claimed.append((job["worker_id"], job["id"]))
        mine.close()

    run_threads(8, worker)
    assert sorted(j for _, j in claimed) == sorted(ids), "有任务被领取了两次，或者有任务没被领取"
    for worker_id, job_id in claimed:  # 数据库里记录的持有者，必须就是拿到它的那个 worker
        row = conn.execute("SELECT worker_id, fence FROM jobs WHERE id = %s", (job_id,)).fetchone()
        assert (row["worker_id"], row["fence"]) == (worker_id, 1)


# =====================================================================
# (c) 令牌桶
# =====================================================================


def test_refill_is_lazy_capped_and_ignores_clock_going_backwards():
    assert ex.refill(0, 100.0, 102.5, rate=1, capacity=10) == pytest.approx(2.5)
    assert ex.refill(9, 100.0, 200.0, rate=1, capacity=10) == 10  # 最多补满
    assert ex.refill(3, 100.0, 100.0, rate=5, capacity=10) == 3  # 时间没走
    assert ex.refill(3, 100.0, 90.0, rate=5, capacity=10) == 3  # 时钟倒退：不补，也不能变少
    assert ex.refill(1, 0.0, 1000.0, rate=0, capacity=10) == 1  # 速率为 0：永远不补


def test_lua_bucket_allows_a_burst_then_denies(redis_client):
    results = [ex.take(redis_client, "tb:acme", rate=0.001, capacity=3)[0] for _ in range(4)]
    assert results == [True, True, True, False]
    assert ex.take(redis_client, "tb:globex", rate=0.001, capacity=3)[0]  # 别的 key 有自己的桶
    assert set(redis_client.hgetall("tb:acme")) == {b"tokens", b"ts"}


def test_lua_bucket_refills_from_the_redis_clock(redis_client):
    sec, usec = redis_client.time()
    redis_client.hset("tb:t", mapping={"tokens": 0, "ts": sec + usec / 1e6 - 2})  # 2 秒前桶就空了
    ok, left = ex.take(redis_client, "tb:t", rate=1, capacity=5, requested=2)
    assert ok, "2 秒 × 1 个/秒 = 补回 2 个，应该放行"
    assert 0 <= left < 0.5
    ok, _ = ex.take(redis_client, "tb:t", rate=1, capacity=5, requested=2)
    assert not ok  # 刚用完，还没补回来


def test_lua_bucket_returns_fractional_tokens_as_a_string(redis_client):
    ok, left = ex.take(redis_client, "tb:f", rate=0.001, capacity=2, requested=0.5)
    assert ok and left == pytest.approx(1.5, abs=0.01), "Lua 的小数直接返回会被截断成整数：记得 tostring()"


def test_lua_bucket_is_exact_under_concurrency(redis_client):
    pool = redis_client.connection_pool  # 先把连接逐个建好再并发（不依赖 fakeredis TCP 服务的 backlog 设置）
    conns = [pool.get_connection() for _ in range(10)]
    for c in conns:
        pool.release(c)
    granted = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        for _ in range(10):
            if ex.take(redis_client, "tb:shared", rate=0.001, capacity=25)[0]:
                with lock:
                    granted.append(i)

    run_threads(10, worker)  # 100 次尝试抢 25 个令牌
    assert len(granted) == 25
