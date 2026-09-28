"""第 26 课练习测试：真实 Postgres（嵌入式）+ fakeredis（支持 Lua），并发用**真实的多个进程**制造竞争。

运行：make lesson N=26    或    .venv/bin/python -m pytest lessons/26_state_and_queues -v

pg_uri / redis_url 两个 fixture 来自仓库根目录的 conftest.py：每个测试一个全新的数据库、一个共用的 fakeredis 服务
（这里的 aredis fixture 每个测试前清空它）。没装可选依赖时这些测试会被自动跳过（pip install -e ".[prod,prod-local]"）。

两类测试：
- 单连接测试：一个 async 连接，按顺序调用你的函数，检查 SQL 的语义（版本号、租约、死信、令牌数）；
- 并发测试：race.py 同时拉起 8~10 个 python 进程，每个进程用自己的连接调用你的函数，站在同一条起跑线上一起开抢。
  进程之间不共享任何内存，只能靠数据库 / Redis 的原子操作协作 —— 有竞态的实现会在这里暴露出来。
断言的都是确定性的量（计数器最后是多少、每个任务被领了几次、放行了几个令牌）；时间只用作宽松的超时上限。
"""

from __future__ import annotations

import json

import pytest

from agentkit.testing import load_exercise, load_sibling

ex = load_exercise(__file__)
race = load_sibling(__file__, "race")
IMPL = ex.__file__  # 子进程加载同一个实现：默认 exercise.py，AGENTKIT_SOLUTION=1 时是 solution.py


async def _connect(pg_uri: str):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    conn = await psycopg.AsyncConnection.connect(pg_uri, autocommit=True, row_factory=dict_row)
    await conn.execute("SET lock_timeout = '2s'")  # 实现如果在等别人的行锁，2 秒后报错，而不是让测试卡死
    return conn


@pytest.fixture
async def conn(pg_uri):
    c = await _connect(pg_uri)
    await ex.setup(c)
    yield c
    await c.close()


@pytest.fixture
async def aredis(redis_url):
    """redis.asyncio 客户端（和 agentkit.contrib.redis_store 一样只用 async 客户端），每个测试前清空。"""
    import redis.asyncio

    client = redis.asyncio.Redis.from_url(redis_url)
    await client.flushall()
    yield client
    await client.aclose()


async def _one(conn, query: str, params=None) -> dict:
    return await (await conn.execute(query, params)).fetchone()


# =====================================================================
# (a) cas_save
# =====================================================================


async def test_cas_creates_a_new_run_only_once(conn):
    assert await ex.cas_save(conn, "r1", 0, json.dumps({"step": 1})) is True
    row = await ex.load_run(conn, "r1")
    assert (row["version"], row["state"]) == (1, {"step": 1})
    assert await ex.cas_save(conn, "r1", 0, json.dumps({"step": 99})) is False  # 别人已经创建了它
    assert (await ex.load_run(conn, "r1"))["state"] == {"step": 1}


async def test_cas_update_needs_the_current_version(conn):
    await ex.cas_save(conn, "r1", 0, json.dumps({"step": 1}))
    assert await ex.cas_save(conn, "r1", 1, json.dumps({"step": 2})) is True
    assert (await ex.load_run(conn, "r1"))["version"] == 2
    # 僵尸 worker 拿着旧版本号来写：被拒绝，而且什么都没改
    assert await ex.cas_save(conn, "r1", 1, json.dumps({"step": "stale"})) is False
    row = await ex.load_run(conn, "r1")
    assert (row["version"], row["state"]) == (2, {"step": 2})
    assert await ex.cas_save(conn, "missing", 5, "{}") is False  # 不存在的 run 也不会被"更新"出来


async def test_cas_loses_no_update_when_8_processes_race(pg_uri, conn, tmp_path):
    """8 个真实进程，各对同一个 run 做 10 次"读 → 想 1 毫秒 → CAS 写回 +1"，冲突就重读重来。"""
    await ex.cas_save(conn, "counter", 0, json.dumps({"n": 0}))
    results = await race.run_race(8, "cas", target=pg_uri, impl=IMPL, workdir=tmp_path, times=10, think=0.001)
    row = await ex.load_run(conn, "counter")
    assert row["state"]["n"] == 80, f"丢了 {80 - row['state']['n']} 次更新：CAS 条件没有起作用"
    assert row["version"] == 81  # 每次成功写入恰好 +1：没有多写，也没有少写
    assert sum(r["conflicts"] for r in results) > 0, "竞争没有真正发生：一次 CAS 冲突都没有"


# =====================================================================
# (b) claim_one
# =====================================================================


async def test_claim_takes_the_oldest_ready_job_and_writes_a_lease(conn):
    first = await ex.enqueue(conn, {"n": 1})
    later = await ex.enqueue(conn, {"n": 2}, delay_seconds=60)  # 一分钟后才能领
    second = await ex.enqueue(conn, {"n": 3})
    job = await ex.claim_one(conn, "worker-1", 30)
    assert job["id"] == first and job["payload"] == {"n": 1}
    assert (job["status"], job["worker_id"], job["attempts"], job["fence"]) == ("leased", "worker-1", 1, 1)
    lease_left = (await _one(conn, "SELECT EXTRACT(EPOCH FROM lease_until - now())::float AS s FROM jobs WHERE id = %s",
                             (first,)))["s"]
    assert 25 < lease_left <= 30  # 租约用数据库的时钟：now() + 30 秒
    assert (await ex.claim_one(conn, "worker-2", 30))["id"] == second
    assert await ex.claim_one(conn, "worker-3", 30) is None  # 剩下的那个还没到时间
    assert (await _one(conn, "SELECT status FROM jobs WHERE id = %s", (later,)))["status"] == "queued"


async def test_claim_skips_a_row_locked_by_another_worker(pg_uri, conn):
    first = await ex.enqueue(conn)
    second = await ex.enqueue(conn)
    psycopg = pytest.importorskip("psycopg")
    holder = await _connect(pg_uri)  # 另一个数据库会话（另一个 worker 的连接）
    async with holder.transaction():  # 它锁着第一行，迟迟不提交
        await holder.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (first,))
        try:
            job = await ex.claim_one(conn, "worker-2", 30)
        except psycopg.errors.LockNotAvailable:  # conn 设了 lock_timeout = 2s
            pytest.fail("在排队等别人的行锁（等了 2 秒超时）：是不是少了 SKIP LOCKED？")
    await holder.close()
    assert job["id"] == second, "应该跳过被锁住的行，去拿下一个"


async def test_expired_lease_is_reclaimed_with_a_bigger_fence_and_poison_goes_dead(conn):
    jid = await ex.enqueue(conn, max_attempts=2)
    a = await ex.claim_one(conn, "worker-A", 30)
    await conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (jid,))  # A 崩溃了
    b = await ex.claim_one(conn, "worker-B", 30)
    assert b["id"] == jid and (b["worker_id"], b["attempts"], b["fence"]) == ("worker-B", 2, a["fence"] + 1)
    await conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (jid,))  # B 也崩溃了
    assert await ex.claim_one(conn, "worker-C", 30) is None  # 次数用尽：不能再发给第三个 worker 去送死
    row = await _one(conn, "SELECT status, lease_until FROM jobs WHERE id = %s", (jid,))
    assert (row["status"], row["lease_until"]) == ("dead", None)


async def test_8_processes_racing_never_claim_a_job_twice(pg_uri, conn, tmp_path):
    ids = {await ex.enqueue(conn, {"i": i}) for i in range(40)}
    results = await race.run_race(8, "claim", target=pg_uri, impl=IMPL, workdir=tmp_path)
    claimed = [(r["worker"], c["id"], c["fence"]) for r in results for c in r["claims"]]
    claimed_ids = [job_id for _, job_id, _ in claimed]
    assert sorted(claimed_ids) == sorted(ids), (
        f"有任务被领取了两次，或者有任务没被领取：{len(claimed_ids)} 次领取，{len(set(claimed_ids))} 个不同的任务"
    )
    assert sum(1 for r in results if r["claims"]) >= 2, "竞争没有真正发生：只有一个进程领到了任务"
    for worker, job_id, fence in claimed:  # 数据库里记录的持有者，必须就是拿到它的那个进程
        row = await _one(conn, "SELECT worker_id, fence FROM jobs WHERE id = %s", (job_id,))
        assert (row["worker_id"], row["fence"], fence) == (worker, 1, 1)


# =====================================================================
# (c) 令牌桶
# =====================================================================


def test_refill_is_lazy_capped_and_ignores_clock_going_backwards():
    assert ex.refill(0, 100.0, 102.5, rate=1, capacity=10) == pytest.approx(2.5)
    assert ex.refill(9, 100.0, 200.0, rate=1, capacity=10) == 10  # 最多补满
    assert ex.refill(3, 100.0, 100.0, rate=5, capacity=10) == 3  # 时间没走
    assert ex.refill(3, 100.0, 90.0, rate=5, capacity=10) == 3  # 时钟倒退：不补，也不能变少
    assert ex.refill(1, 0.0, 1000.0, rate=0, capacity=10) == 1  # 速率为 0：永远不补


async def test_lua_bucket_allows_a_burst_then_denies(aredis):
    results = [(await ex.take(aredis, "tb:acme", rate=0.001, capacity=3))[0] for _ in range(4)]
    assert results == [True, True, True, False]
    assert (await ex.take(aredis, "tb:globex", rate=0.001, capacity=3))[0]  # 别的 key 有自己的桶
    assert set(await aredis.hgetall("tb:acme")) == {b"tokens", b"ts"}


async def test_lua_bucket_refills_from_the_redis_clock(aredis):
    sec, usec = await aredis.time()
    await aredis.hset("tb:t", mapping={"tokens": 0, "ts": sec + usec / 1e6 - 2})  # 2 秒前桶就空了
    ok, left = await ex.take(aredis, "tb:t", rate=1, capacity=5, requested=2)
    assert ok, "2 秒 × 1 个/秒 = 补回 2 个，应该放行"
    assert 0 <= left < 0.5
    ok, _ = await ex.take(aredis, "tb:t", rate=1, capacity=5, requested=2)
    assert not ok  # 刚用完，还没补回来


async def test_lua_bucket_returns_fractional_tokens_as_a_string(aredis):
    ok, left = await ex.take(aredis, "tb:f", rate=0.001, capacity=2, requested=0.5)
    assert ok and left == pytest.approx(1.5, abs=0.01), "Lua 的小数直接返回会被截断成整数：记得 tostring()"


async def test_lua_bucket_is_exact_when_10_processes_race(aredis, redis_url, tmp_path):
    """10 个真实进程、各 10 次，一共 100 次尝试抢 25 个令牌（速率几乎为 0，不会补充）。"""
    results = await race.run_race(10, "take", target=redis_url, impl=IMPL, workdir=tmp_path, times=10,
                                  key="tb:shared", rate=0.001, capacity=25)
    assert sum(r["granted"] for r in results) == 25, "放行的令牌数不对：读 → 判断 → 写回不是原子的？"
    assert sum(1 for r in results if r["granted"]) >= 2, "竞争没有真正发生：只有一个进程拿到了令牌"
