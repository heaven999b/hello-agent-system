"""agentkit.contrib.redis_store 的测试：fakeredis TCP 服务（支持 Lua），多线程制造真实竞争。

覆盖：幂等存储的 get/put 与跨"进程"共享、claim 的互斥与过期；令牌桶的突发、补充、并发下的精确计数、
超时与小数；RateLimitHook 按租户限流；锁的互斥、fencing token 递增、比较后删除、存储端拒绝旧 token。
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("redis")

from agentkit import Agent, RunState, ScriptedLLM, ToolContext, ToolRegistry, ToolResult, call_tool, reply, tool  # noqa: E402
from agentkit.contrib.redis_store import (  # noqa: E402
    RateLimitHook,
    RedisIdempotencyStore,
    RedisLock,
    RedisTokenBucket,
)
from agentkit.types import ToolCall  # noqa: E402


def warm_pool(client, n: int) -> None:
    """先把连接一个个建好再并发。fakeredis 的 TCP 服务基于 socketserver，默认监听 backlog 只有 5，
    十几个线程同时建连接会被 reset（agentkit.testing 已调到 128；真 Redis 没有这个问题）。预热让测试不依赖这个设置。"""
    pool = client.connection_pool
    conns = [pool.get_connection() for _ in range(n)]
    for c in conns:
        pool.release(c)


def run_threads(n: int, target) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def wrapper(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            target(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    if errors:
        raise errors[0]


# ============================================================================= 幂等存储


def test_idempotency_store_roundtrip_ttl_and_namespace(redis_client, redis_url):
    store = RedisIdempotencyStore(redis_url, namespace="idem", ttl_seconds=60)
    assert store.get("run1:call1") is None
    store.put("run1:call1", ToolResult(True, "工单 T-1001"))
    got = RedisIdempotencyStore(redis_client).get("run1:call1")  # 另一个 worker（传 client 也行）
    assert got == ToolResult(True, "工单 T-1001")
    assert 0 < redis_client.ttl("idem:{run1:call1}") <= 60
    assert RedisIdempotencyStore(redis_client, namespace="other").get("run1:call1") is None


def test_write_tool_runs_once_across_workers_sharing_the_store(redis_client):
    created = []

    @tool(risk="write")
    def create_ticket(title: str) -> str:
        """建工单"""
        created.append(title)
        return f"T-{len(created)}"

    call = ToolCall(id="call_abc", name="create_ticket", arguments='{"title": "打印机"}')
    ctx = ToolContext(run_id="job-7", call_id="call_abc")
    worker1 = ToolRegistry([create_ticket], idempotency_store=RedisIdempotencyStore(redis_client))
    worker2 = ToolRegistry([create_ticket], idempotency_store=RedisIdempotencyStore(redis_client))
    first = worker1.execute(call, ctx)
    replay = worker2.execute(call, ctx)  # worker1 崩溃后 worker2 接手，重放同一个工具调用
    assert first.content == replay.content == "T-1" and created == ["打印机"]


def test_claim_lets_exactly_one_concurrent_executor_in(redis_client):
    wins = []

    def contender(i: int) -> None:
        store = RedisIdempotencyStore(redis_client)  # 每个线程一个"worker"
        if store.claim("job-1:call_x", ttl_seconds=30):
            wins.append(i)

    warm_pool(redis_client, 16)
    run_threads(16, contender)
    assert len(wins) == 1  # 16 个同时来，只有一个拿到执行权


def test_claim_release_put_and_expiry(redis_client):
    a, b = RedisIdempotencyStore(redis_client), RedisIdempotencyStore(redis_client)
    assert a.claim("k", ttl_seconds=30) and a.in_flight("k")
    assert not b.claim("k") and not b.release("k")  # b 不是持有者，释放不了别人的标记
    assert a.release("k") and not a.in_flight("k")  # 执行失败：释放，让重试不必等过期
    assert b.claim("k")
    b.put("k", ToolResult(True, "done"))  # 成功：写结果并清掉标记
    assert not b.in_flight("k") and not a.claim("k")  # 已经有结果了：谁都不必再执行
    assert a.claim("short", ttl_seconds=0.15)
    time.sleep(0.25)  # 持有者崩溃，标记过期
    assert b.claim("short")


# ============================================================================= 令牌桶


def test_bucket_allows_a_burst_then_denies_and_keys_are_independent(redis_client):
    bucket = RedisTokenBucket(redis_client, rate_per_sec=0.001, capacity=3, overrides={"vip": (0.001, 5)})
    assert [bucket.try_acquire("acme") for _ in range(4)] == [True, True, True, False]
    assert bucket.try_acquire("globex")  # 另一个租户有自己的桶
    assert sum(bucket.try_acquire("vip") for _ in range(6)) == 5  # 按套餐覆盖容量


def test_bucket_refills_over_time_using_the_redis_clock(redis_client):
    bucket = RedisTokenBucket(redis_client, rate_per_sec=20, capacity=1)
    assert bucket.try_acquire("t") and not bucket.try_acquire("t")
    ok, wait, _ = bucket.take("t")
    assert not ok and 0 < wait <= 0.05  # 每 0.05 秒补一个：脚本算出来还要等多久
    assert bucket.acquire("t", timeout=5.0)  # 按算出来的时间睡一会儿，再拿就拿到了


def test_bucket_is_exact_under_concurrency(redis_client):
    bucket = RedisTokenBucket(redis_client, rate_per_sec=0.001, capacity=25)
    granted = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        for _ in range(10):
            if bucket.try_acquire("shared"):
                with lock:
                    granted.append(i)

    warm_pool(redis_client, 10)
    run_threads(10, worker)  # 100 次尝试抢 25 个令牌
    assert len(granted) == 25


def test_acquire_gives_up_early_when_the_wait_exceeds_the_timeout(redis_client, monkeypatch):
    import agentkit.contrib.redis_store as rs

    bucket = RedisTokenBucket(redis_client, rate_per_sec=0.1, capacity=1)  # 10 秒补一个
    assert bucket.acquire("t", timeout=0)
    slept = []
    monkeypatch.setattr(rs.time, "sleep", slept.append)
    assert not bucket.acquire("t", timeout=0.5)
    assert slept == []  # 算得出来 0.5 秒内等不到，就一秒都不白等
    monkeypatch.undo()
    with pytest.raises(ValueError):
        bucket.try_acquire("t", tokens=2)  # 比容量还大：永远不可能放行


def test_fractional_tokens_survive_the_lua_round_trip(redis_client):
    bucket = RedisTokenBucket(redis_client, rate_per_sec=0.001, capacity=2)
    ok, _, left = bucket.take("t", 0.5)
    assert ok and 1.49 < left < 1.51  # Lua 的小数直接返回会被截断成 1；脚本用 tostring 返回
    fields = redis_client.hgetall("tb:t")
    assert set(fields) == {b"tokens", b"ts"} and redis_client.pttl("tb:t") > 0


def test_rate_limit_hook_stops_only_the_tenant_over_quota(redis_client):
    bucket = RedisTokenBucket(redis_client, rate_per_sec=0.001, capacity=1)
    hook = RateLimitHook(bucket, wait_timeout=0.2)

    def agent():
        return Agent(ScriptedLLM([reply("好的")]), hooks=[hook])

    assert agent().run("你好", metadata={"tenant_id": "acme"}).ok
    blocked = agent().run("再来一次", metadata={"tenant_id": "acme"})
    assert (blocked.status, blocked.stop_reason) == ("stopped", "rate_limited")
    assert agent().run("你好", metadata={"tenant_id": "globex"}).ok  # 别的租户不受影响
    assert hook.stats["acme"]["calls"] == 1 and hook.stats["acme"]["rejected"] == 1


def test_rate_limit_hook_counts_tokens_per_call(redis_client):
    bucket = RedisTokenBucket(redis_client, rate_per_sec=0.001, capacity=100)
    hook = RateLimitHook(bucket, tokens_fn=lambda state, messages: 30, wait_timeout=0)

    @tool
    def lookup(q: str) -> str:
        """查资料"""
        return "结果"

    llm = ScriptedLLM([call_tool("lookup", q="x"), call_tool("lookup", q="y"), call_tool("lookup", q="z"), reply("完")])
    r = Agent(llm, [lookup], hooks=[hook]).run("查三次", metadata={"tenant_id": "acme"})
    assert r.stop_reason == "rate_limited" and hook.stats["acme"]["calls"] == 3  # 第 4 次调用时 100 个 token 不够了


# ============================================================================= 锁


def test_lock_is_exclusive_and_fences_increase(redis_client):
    a, b = RedisLock(redis_client, "report", 10), RedisLock(redis_client, "report", 10)
    fa = a.acquire()
    assert fa == 1 and b.acquire(blocking=False) is None
    assert a.owned() and a.release()
    fb = b.acquire(blocking=False)
    assert fb == 2 and b.fence == 2
    assert b.extend(20) and not a.extend()  # 只有持有者能续期
    b.release()


def test_expired_holder_cannot_release_someone_elses_lock(redis_client):
    a, b = RedisLock(redis_client, "job", 0.15), RedisLock(redis_client, "job", 10)
    a.acquire()
    time.sleep(0.25)  # a 卡住了，锁过期
    fb = b.acquire(blocking=False)
    assert fb is not None
    assert not a.owned()
    assert a.release() is False  # 比较后删除：a 删不掉 b 的锁
    assert b.owned()


class FencedStore:
    """被锁保护的存储：记住见过的最大 fencing token，拒绝更小的（真实系统里是一条带条件的 UPDATE）。"""

    def __init__(self):
        self.max_fence = 0
        self.value = 0
        self.rejected = 0
        self._mu = threading.Lock()

    def write(self, fence: int, fn) -> bool:
        with self._mu:
            if fence < self.max_fence:
                self.rejected += 1
                return False
            self.max_fence = fence
            self.value = fn(self.value)
            return True


def test_storage_rejects_a_paused_holders_stale_token(redis_client):
    store = FencedStore()
    a, b = RedisLock(redis_client, "doc", 0.15), RedisLock(redis_client, "doc", 10)
    fa = a.acquire()
    time.sleep(0.25)  # a GC 停顿，锁过期
    fb = b.acquire()
    assert store.write(fb, lambda v: v + 10)
    assert not store.write(fa, lambda v: 999)  # a 醒来，以为自己还持有锁
    assert store.value == 10 and store.rejected == 1


def test_lock_serializes_concurrent_read_modify_write(redis_client):
    store = FencedStore()

    def worker(i: int) -> None:
        lock = RedisLock(redis_client, "counter", 5)  # 每个持有者一个实例
        for _ in range(5):
            with lock as fence:
                current = store.value
                time.sleep(0.001)  # 放大竞态窗口：没有锁的话必然丢更新
                assert store.write(fence, lambda _v, c=current: c + 1)

    warm_pool(redis_client, 6)
    run_threads(6, worker)
    assert store.value == 30 and store.rejected == 0


# ============================================================================= 异步版

import asyncio  # noqa: E402

from agentkit.contrib.redis_store import AsyncRateLimitHook, AsyncRedisIdempotencyStore, AsyncRedisTokenBucket  # noqa: E402


async def async_client(redis_url: str, max_connections: int = 8):
    """redis.asyncio 的默认连接池（redis-py 8.1：上限 100）满了直接抛 MaxConnectionsError，不会等；
    BlockingConnectionPool 满了会排队等。先把连接逐个建好再并发（同上：不依赖 fakeredis 的 backlog 设置）。"""
    import redis.asyncio as aredis

    pool = aredis.BlockingConnectionPool.from_url(redis_url, max_connections=max_connections)
    conns = [await pool.get_connection() for _ in range(max_connections)]
    for c in conns:
        await pool.release(c)
    return aredis.Redis(connection_pool=pool)


def test_async_idempotency_store_and_claim_race(redis_client, redis_url):
    async def main():
        r = await async_client(redis_url)
        store = AsyncRedisIdempotencyStore(r)
        assert await store.get("k") is None
        await store.put("k", ToolResult(True, "T-1"))
        assert await store.get("k") == ToolResult(True, "T-1")
        assert RedisIdempotencyStore(redis_client).get("k") == ToolResult(True, "T-1")  # 同步版和异步版读写同一份数据

        stores = [AsyncRedisIdempotencyStore(r) for _ in range(50)]
        wins = await asyncio.gather(*(s.claim("job-9:call_z", ttl_seconds=30) for s in stores))  # 50 个协程同时抢
        assert sum(wins) == 1
        winner = stores[wins.index(True)]
        assert await winner.in_flight("job-9:call_z") and await winner.release("job-9:call_z")
        await r.aclose()

    asyncio.run(main())


def test_async_token_bucket_is_exact_under_100_coroutines(redis_client, redis_url):
    async def main():
        r = await async_client(redis_url)
        bucket = AsyncRedisTokenBucket(r, rate_per_sec=0.001, capacity=30)
        granted = await asyncio.gather(*(bucket.try_acquire("shared") for _ in range(100)))
        assert sum(granted) == 30
        await r.aclose()

    asyncio.run(main())


def test_async_rate_limit_hook_waits_without_blocking_the_event_loop(redis_client, redis_url):
    async def main():
        r = await async_client(redis_url)
        hook = AsyncRateLimitHook(AsyncRedisTokenBucket(r, rate_per_sec=5, capacity=1), wait_timeout=2)
        state = RunState(metadata={"tenant_id": "acme"})
        ticks = 0

        async def ticker():  # 同一进程里的"其他会话"：每 10 毫秒推进一次
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        await hook.before_llm(state, [])
        t0 = time.monotonic()
        await hook.before_llm(state, [])  # 桶空了，要等约 0.2 秒
        waited = time.monotonic() - t0
        t.cancel()
        assert waited > 0.15  # 桶空了：确实等了（5 个/秒 → 约 0.2 秒）
        assert ticks >= 3, "等令牌时事件循环被卡住了：别的协程一次都没跑"
        assert hook.stats["acme"]["calls"] == 2
        await r.aclose()

    asyncio.run(main())


def test_async_rate_limit_hook_with_async_agent(redis_client, redis_url):
    aio = pytest.importorskip("agentkit.aio")

    async def main():
        r = await async_client(redis_url)
        hook = AsyncRateLimitHook(AsyncRedisTokenBucket(r, rate_per_sec=0.001, capacity=2), wait_timeout=0.1)
        agent = aio.AsyncAgent(aio.AsyncScriptedLLM(responder=lambda m: reply("好")), hooks=[hook])
        results = await asyncio.gather(*(agent.run("hi", metadata={"tenant_id": "acme"}) for _ in range(5)))
        assert sorted(r.status for r in results) == ["completed"] * 2 + ["stopped"] * 3
        assert {r.stop_reason for r in results if r.status == "stopped"} == {"rate_limited"}
        await r.aclose()

    asyncio.run(main())


def test_sync_and_async_hooks_refuse_the_wrong_bucket(redis_client, redis_url):
    import redis.asyncio as aredis

    with pytest.raises(TypeError):
        RateLimitHook(AsyncRedisTokenBucket(aredis.Redis.from_url(redis_url), 1, 1))
    with pytest.raises(TypeError):
        AsyncRateLimitHook(RedisTokenBucket(redis_client, 1, 1))
