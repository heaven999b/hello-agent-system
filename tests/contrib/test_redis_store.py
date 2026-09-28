"""agentkit.contrib.redis_store 的测试：fakeredis TCP 服务（支持 Lua，根 conftest 的 redis_url）。

两种并发都是真的：
- 同一进程里的并发会话 = 同一个事件循环里的很多协程（asyncio.gather）；
- 多个 worker = 多个操作系统进程（asyncio.create_subprocess_exec 启动，各自连同一个 Redis）。
  断言每个 worker 的 pid 互不相同、也不是测试进程本身 —— 不拿线程或同进程的两个对象冒充进程。

覆盖：幂等存储的 get/put 与跨进程重放、claim 的互斥（协程 + 进程）与过期；令牌桶的突发、补充、跨进程下的精确计数、
超时与小数；RateLimitHook 按租户限流、等令牌时不阻塞事件循环；锁的互斥（跨进程）、fencing token 递增、
比较后删除、存储端拒绝旧 token；同步客户端被明确拒绝。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("redis")

import redis.asyncio as aredis  # noqa: E402

from agentkit import Agent, RunState, ScriptedLLM, ToolResult, call_tool, reply, tool, wait_for  # noqa: E402
from agentkit.contrib.redis_store import RateLimitHook, RedisIdempotencyStore, RedisLock, RedisTokenBucket  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


async def async_client(redis_url: str, max_connections: int = 8) -> aredis.Redis:
    """redis.asyncio 的默认连接池（redis-py 8.1：上限 100）满了直接抛 MaxConnectionsError，不会等；
    BlockingConnectionPool 满了会排队等。先把连接逐个建好再并发：fakeredis 的 TCP 服务默认监听 backlog 只有 5，
    很多连接同时建立会被 reset（agentkit.testing 已调到 128；真 Redis 没有这个问题），预热让测试不依赖这个设置。"""
    pool = aredis.BlockingConnectionPool.from_url(redis_url, max_connections=max_connections)
    conns = [await pool.get_connection() for _ in range(max_connections)]
    for c in conns:
        await pool.release(c)
    return aredis.Redis(connection_pool=pool)


@pytest.fixture
async def r(redis_client, redis_url):
    """每个测试一个 async 客户端（redis_client fixture 负责清库）。"""
    client = await async_client(redis_url)
    yield client
    await client.aclose()


# ============================================================================= 多进程 worker

# 每个 worker 是一个独立的 Python 进程：自己的解释器、自己的事件循环、自己的 Redis 连接池，只通过 Redis 交流。
WORKER = textwrap.dedent(
    r'''
    import asyncio, json, os, sys

    import redis.asyncio as aredis

    from agentkit import ToolContext, ToolRegistry, tool
    from agentkit.contrib.redis_store import RedisIdempotencyStore, RedisLock, RedisTokenBucket
    from agentkit.types import ToolCall

    url, scenario, n_workers = sys.argv[1], sys.argv[2], int(sys.argv[3])

    FENCED_WRITE_LUA = """
    -- 被锁保护的"存储"：记住见过的最大 fencing token，拒绝更小的（真实系统里是一条带条件的 UPDATE）
    local seen = tonumber(redis.call('GET', KEYS[2]) or '0')
    if tonumber(ARGV[1]) < seen then
      return 0
    end
    redis.call('SET', KEYS[2], ARGV[1])
    redis.call('SET', KEYS[1], ARGV[2])
    return 1
    """


    async def start_line(r):
        """跨进程的起跑线：每个进程到了就 INCR，全部到齐后一起开始（用 Redis 本身当栅栏）。"""
        await r.incr(f"{scenario}:ready")
        while int(await r.get(f"{scenario}:ready")) < n_workers:
            await asyncio.sleep(0.005)


    async def claim(r):
        stores = [RedisIdempotencyStore(r) for _ in range(10)]  # 每个进程里再开 10 个协程一起抢
        await start_line(r)
        wins = await asyncio.gather(*(s.claim("job-9:call_z", ttl_seconds=30) for s in stores))
        return {"wins": sum(wins)}


    async def bucket(r):
        b = RedisTokenBucket(r, rate_per_sec=0.001, capacity=30)
        await start_line(r)
        granted = await asyncio.gather(*(b.try_acquire("shared") for _ in range(25)))
        return {"granted": sum(granted)}


    async def lock(r):
        fenced_write = r.register_script(FENCED_WRITE_LUA)
        await r.script_load(FENCED_WRITE_LUA)  # 预热脚本缓存（原因见 redis_store._Scripts）
        stats = {"peak": 0, "rejected": 0, "fences": []}

        async def holder():
            lk = RedisLock(r, "counter", 5)  # 每个持有者一个实例
            for _ in range(3):
                async with lk as fence:
                    inside = await r.incr("inside")  # 临界区里此刻有几个持有者（锁正确时永远是 1）
                    stats["peak"] = max(stats["peak"], inside)
                    current = int(await r.get("counter") or 0)
                    await asyncio.sleep(0.002)  # 放大竞态窗口：没有锁的话必然丢更新
                    if not await fenced_write(keys=["counter", "counter:fence"], args=[fence, current + 1]):
                        stats["rejected"] += 1
                    stats["fences"].append(fence)
                    await r.decr("inside")

        await start_line(r)
        await asyncio.gather(holder(), holder())
        return stats


    async def replay(r):
        @tool(risk="write")
        async def create_ticket(title: str) -> str:
            """建工单（副作用：Redis 里的工单计数 +1）"""
            return f"T-{await r.incr('tickets')}"

        registry = ToolRegistry([create_ticket], idempotency_store=RedisIdempotencyStore(r))
        call = ToolCall(id="call_abc", name="create_ticket", arguments='{"title": "打印机"}')
        result = await registry.execute(call, ToolContext(run_id="job-7", call_id="call_abc"))
        return {"content": result.content}


    async def main():
        r = aredis.Redis.from_url(url)
        try:
            out = await globals()[scenario](r)
        finally:
            await r.aclose()
        print(json.dumps({"pid": os.getpid(), **out}))


    asyncio.run(main())
    '''
)


async def run_workers(redis_url: str, scenario: str, n: int, *, timeout: float = 90) -> list[dict]:
    """同时启动 n 个 worker 进程跑同一个场景，等它们全部退出，返回每个进程打印的结果。"""
    procs = [
        await asyncio.create_subprocess_exec(
            sys.executable, "-c", WORKER, redis_url, scenario, str(n),
            cwd=REPO, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(n)
    ]
    try:
        # 时限很宽：8 GB 机器负载高时，启动一个 Python 进程并导入 agentkit 就可能要几秒
        outputs = await wait_for(asyncio.gather(*(p.communicate() for p in procs)), timeout)
    finally:
        for p in procs:
            if p.returncode is None:
                p.kill()
                await p.wait()
    results = []
    for p, (out, err) in zip(procs, outputs):
        assert p.returncode == 0, err.decode()
        results.append(json.loads(out.decode().strip().splitlines()[-1]))
    pids = {res["pid"] for res in results}
    assert len(pids) == n and os.getpid() not in pids  # 真的是 n 个不同的操作系统进程
    return results


# ============================================================================= 幂等存储


async def test_idempotency_store_roundtrip_ttl_and_namespace(r, redis_client, redis_url):
    store = RedisIdempotencyStore(redis_url, namespace="idem", ttl_seconds=60)  # 传 URL 也行
    assert await store.get("run1:call1") is None
    await store.put("run1:call1", ToolResult(True, "工单 T-1001"))
    assert await RedisIdempotencyStore(r).get("run1:call1") == ToolResult(True, "工单 T-1001")  # 另一个实例读到同一份
    assert 0 < redis_client.ttl("idem:{run1:call1}") <= 60
    assert await RedisIdempotencyStore(r, namespace="other").get("run1:call1") is None
    await store.r.aclose()


async def test_write_tool_runs_once_when_another_worker_process_replays_it(redis_client, redis_url):
    """worker 进程 1 执行了写工具后崩溃；worker 进程 2 接手，重放同一个工具调用（同一个幂等键）。"""
    (first,) = await run_workers(redis_url, "replay", 1)
    (again,) = await run_workers(redis_url, "replay", 1)
    assert first["pid"] != again["pid"]
    assert first["content"] == again["content"] == "T-1"
    assert int(redis_client.get("tickets")) == 1  # 副作用只发生了一次


async def test_claim_lets_exactly_one_executor_in_across_processes_and_coroutines(redis_client, redis_url):
    results = await run_workers(redis_url, "claim", 4)  # 4 个进程 × 每个 10 个协程，同时抢同一个执行权
    assert sum(res["wins"] for res in results) == 1
    assert redis_client.exists("idem:{job-9:call_z}:inflight")


async def test_claim_release_put_and_expiry(r):
    a, b = RedisIdempotencyStore(r), RedisIdempotencyStore(r)
    assert await a.claim("k", ttl_seconds=30) and await a.in_flight("k")
    assert not await b.claim("k") and not await b.release("k")  # b 不是持有者，释放不了别人的标记
    assert await a.release("k") and not await a.in_flight("k")  # 执行失败：释放，让重试不必等过期
    assert await b.claim("k")
    await b.put("k", ToolResult(True, "done"))  # 成功：写结果并清掉标记
    assert not await b.in_flight("k") and not await a.claim("k")  # 已经有结果了：谁都不必再执行
    assert await a.claim("short", ttl_seconds=0.15)
    await asyncio.sleep(0.25)  # 持有者崩溃，标记过期
    assert await b.claim("short")


# ============================================================================= 令牌桶


async def test_bucket_allows_a_burst_then_denies_and_keys_are_independent(r):
    bucket = RedisTokenBucket(r, rate_per_sec=0.001, capacity=3, overrides={"vip": (0.001, 5)})
    assert [await bucket.try_acquire("acme") for _ in range(4)] == [True, True, True, False]
    assert await bucket.try_acquire("globex")  # 另一个租户有自己的桶
    assert sum([await bucket.try_acquire("vip") for _ in range(6)]) == 5  # 按套餐覆盖容量


async def test_bucket_refills_over_time_using_the_redis_clock(r):
    bucket = RedisTokenBucket(r, rate_per_sec=20, capacity=1)
    assert await bucket.try_acquire("t") and not await bucket.try_acquire("t")
    ok, wait, _ = await bucket.take("t")
    assert not ok and 0 < wait <= 0.05  # 每 0.05 秒补一个：脚本算出来还要等多久
    assert await bucket.acquire("t", timeout=5.0)  # 按算出来的时间睡一会儿，再拿就拿到了


async def test_bucket_is_exact_when_shared_by_several_worker_processes(redis_client, redis_url):
    results = await run_workers(redis_url, "bucket", 4)  # 4 个进程 × 25 个协程 = 100 次尝试抢 30 个令牌
    assert sum(res["granted"] for res in results) == 30


async def test_acquire_gives_up_early_when_the_wait_exceeds_the_timeout(r):
    slept = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    bucket = RedisTokenBucket(r, rate_per_sec=0.1, capacity=1, sleep=fake_sleep)  # 10 秒补一个
    assert await bucket.acquire("t", timeout=0)
    assert not await bucket.acquire("t", timeout=0.5)
    assert slept == []  # 算得出来 0.5 秒内等不到，就一秒都不白等
    with pytest.raises(ValueError):
        await bucket.try_acquire("t", tokens=2)  # 比容量还大：永远不可能放行


async def test_fractional_tokens_survive_the_lua_round_trip(r, redis_client):
    bucket = RedisTokenBucket(r, rate_per_sec=0.001, capacity=2)
    ok, _, left = await bucket.take("t", 0.5)
    assert ok and 1.49 < left < 1.51  # Lua 的小数直接返回会被截断成 1；脚本用 tostring 返回
    fields = redis_client.hgetall("tb:t")
    assert set(fields) == {b"tokens", b"ts"} and redis_client.pttl("tb:t") > 0


# ============================================================================= RateLimitHook


async def test_rate_limit_hook_stops_only_the_tenant_over_quota(r):
    hook = RateLimitHook(RedisTokenBucket(r, rate_per_sec=0.001, capacity=1), wait_timeout=0.2)

    def agent():
        return Agent(ScriptedLLM([reply("好的")]), hooks=[hook])

    assert (await agent().run("你好", metadata={"tenant_id": "acme"})).ok
    blocked = await agent().run("再来一次", metadata={"tenant_id": "acme"})
    assert (blocked.status, blocked.stop_reason) == ("stopped", "rate_limited")
    assert (await agent().run("你好", metadata={"tenant_id": "globex"})).ok  # 别的租户不受影响
    assert hook.stats["acme"]["calls"] == 1 and hook.stats["acme"]["rejected"] == 1


async def test_rate_limit_hook_counts_tokens_per_call(r):
    hook = RateLimitHook(RedisTokenBucket(r, rate_per_sec=0.001, capacity=100), tokens_fn=lambda state, messages: 30, wait_timeout=0)

    @tool
    def lookup(q: str) -> str:
        """查资料"""
        return "结果"

    llm = ScriptedLLM([call_tool("lookup", q="x"), call_tool("lookup", q="y"), call_tool("lookup", q="z"), reply("完")])
    res = await Agent(llm, [lookup], hooks=[hook]).run("查三次", metadata={"tenant_id": "acme"})
    assert res.stop_reason == "rate_limited" and hook.stats["acme"]["calls"] == 3  # 第 4 次调用时 100 个 token 不够了
    assert len(llm.calls) == 3  # 被拦下的那一步没有调用模型


async def test_rate_limit_hook_waits_without_blocking_the_event_loop(r):
    hook = RateLimitHook(RedisTokenBucket(r, rate_per_sec=5, capacity=1), wait_timeout=2)
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
    assert waited > 0.15  # 确实等了（5 个/秒 → 约 0.2 秒；sleep 不会提前返回）
    assert ticks >= 3, "等令牌时事件循环被卡住了：别的协程一次都没跑"
    assert hook.stats["acme"]["calls"] == 2


async def test_rate_limit_hook_with_concurrent_runs_on_one_agent(r):
    hook = RateLimitHook(RedisTokenBucket(r, rate_per_sec=0.001, capacity=2), wait_timeout=0.1)
    llm = ScriptedLLM(responder=lambda m: reply("好"), latency=0.01)
    agent = Agent(llm, hooks=[hook])
    results = await asyncio.gather(*(agent.run("hi", metadata={"tenant_id": "acme"}) for _ in range(5)))
    assert sorted(res.status for res in results) == ["completed"] * 2 + ["stopped"] * 3
    assert {res.stop_reason for res in results if res.status == "stopped"} == {"rate_limited"}
    assert llm.call_count == 2  # 被限流的 3 个运行一次模型都没调


# ============================================================================= 锁


async def test_lock_is_exclusive_and_fences_increase(r):
    a, b = RedisLock(r, "report", 10), RedisLock(r, "report", 10)
    fa = await a.acquire()
    assert fa == 1 and await b.acquire(blocking=False) is None
    assert await a.owned() and await a.release()
    fb = await b.acquire(blocking=False)
    assert fb == 2 and b.fence == 2
    assert await b.extend(20) and not await a.extend()  # 只有持有者能续期
    await b.release()


async def test_expired_holder_cannot_release_someone_elses_lock(r):
    a, b = RedisLock(r, "job", 0.15), RedisLock(r, "job", 10)
    await a.acquire()
    await asyncio.sleep(0.25)  # a 卡住了，锁过期
    assert await b.acquire(blocking=False) is not None
    assert not await a.owned()
    assert await a.release() is False  # 比较后删除：a 删不掉 b 的锁
    assert await b.owned()


class FencedStore:
    """被锁保护的存储：记住见过的最大 fencing token，拒绝更小的（真实系统里是一条带条件的 UPDATE）。"""

    def __init__(self):
        self.max_fence = 0
        self.value = 0
        self.rejected = 0

    def write(self, fence: int, fn) -> bool:
        if fence < self.max_fence:
            self.rejected += 1
            return False
        self.max_fence = fence
        self.value = fn(self.value)
        return True


async def test_storage_rejects_a_paused_holders_stale_token(r):
    store = FencedStore()
    a, b = RedisLock(r, "doc", 0.15), RedisLock(r, "doc", 10)
    fa = await a.acquire()
    await asyncio.sleep(0.25)  # a 停顿（GC、事件循环被占住……），锁过期
    fb = await b.acquire()
    assert store.write(fb, lambda v: v + 10)
    assert not store.write(fa, lambda v: 999)  # a 醒来，以为自己还持有锁
    assert store.value == 10 and store.rejected == 1


async def test_lock_serializes_read_modify_write_across_processes(redis_client, redis_url):
    """4 个 worker 进程 × 每个 2 个持有者 × 每个 3 次"读-等-写"，共享一个 Redis 里的计数器。"""
    results = await run_workers(redis_url, "lock", 4)
    assert int(redis_client.get("counter")) == 4 * 2 * 3  # 一次更新都没丢
    assert max(res["peak"] for res in results) == 1  # 任何时刻临界区里最多一个持有者
    assert sum(res["rejected"] for res in results) == 0  # 锁一直有效时，fencing 检查不会误杀
    fences = sorted(f for res in results for f in res["fences"])
    assert fences == list(range(1, 25))  # 每次拿锁发放一个新的、全局递增的 fencing token，跨进程也不重复


# ============================================================================= 误用


def test_sync_redis_client_is_refused_with_a_clear_error(redis_client):
    for make in (
        lambda: RedisIdempotencyStore(redis_client),
        lambda: RedisTokenBucket(redis_client, 1, 1),
        lambda: RedisLock(redis_client, "x", 1),
    ):
        with pytest.raises(TypeError, match="redis.asyncio"):
            make()
