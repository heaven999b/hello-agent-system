"""第 12 课测试。

前半部分是练习测试：纯算法、离线、确定性（注入假时钟，不 sleep）。
后半部分验证迷你部署里"跨进程"的说法：起真实的 API 进程（uvicorn）和 worker 进程（WorkerPool），
走真实的 HTTP；断言的是确定性的量（放行数的上下界、fence、领取次数、同时在跑的峰值），时间只作宽松的上限。
这部分不依赖你的练习（路由和限流器用 solution.py），需要 pip install -e ".[server]"，没装就跳过。

运行：make lesson N=12
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import sqlite3
import time
from pathlib import Path

import pytest

from agentkit.testing import load_exercise, load_sibling

ex = load_exercise(__file__)
HERE = Path(__file__).resolve().parent


class FakeClock:
    """可控的时钟：测试里想让时间过去几秒，就 clock.advance(几秒)。"""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ------------------------------------------------------------------ TokenBucket


def test_bucket_starts_full_and_allows_burst_up_to_capacity():
    b = ex.TokenBucket(capacity=5, refill_rate=1, clock=FakeClock())
    assert [b.try_acquire() for _ in range(6)] == [True] * 5 + [False]


def test_bucket_refills_over_time_and_caps_at_capacity():
    clock = FakeClock()
    b = ex.TokenBucket(capacity=5, refill_rate=2, clock=clock)
    for _ in range(5):
        b.try_acquire()
    clock.advance(1)  # 补 2 个
    assert [b.try_acquire() for _ in range(3)] == [True, True, False]
    clock.advance(3600)  # 空闲一小时，也最多补到 capacity，不会"攒"出 7200 个
    assert b.available == 5


def test_bucket_fractional_refill():
    clock = FakeClock()
    b = ex.TokenBucket(capacity=1, refill_rate=0.5, clock=clock)
    assert b.try_acquire()
    clock.advance(1)  # 只补了 0.5 个
    assert not b.try_acquire()
    clock.advance(1)  # 累计 1 个
    assert b.try_acquire()


def test_failed_acquire_consumes_nothing_and_weighted_acquire():
    clock = FakeClock()
    b = ex.TokenBucket(capacity=10, refill_rate=1, clock=clock)
    assert b.try_acquire(7)
    assert not b.try_acquire(4)  # 只剩 3 个，要 4 个 → 拒绝，且不扣
    assert b.available == 3
    assert b.try_acquire(3)


def test_request_larger_than_capacity_never_succeeds():
    clock = FakeClock()
    b = ex.TokenBucket(capacity=10, refill_rate=100, clock=clock)
    clock.advance(1000)
    assert not b.try_acquire(11)
    assert b.retry_after(11) == math.inf
    assert b.available == 10  # 失败的请求没有扣令牌


def test_retry_after():
    clock = FakeClock()
    b = ex.TokenBucket(capacity=10, refill_rate=2, clock=clock)
    assert b.retry_after(3) == 0.0  # 现在就够
    assert b.try_acquire(10)
    assert b.retry_after(3) == pytest.approx(1.5)  # 缺 3 个，每秒补 2 个
    assert b.available == 0  # retry_after 只查询，不扣令牌
    clock.advance(1.5)
    assert b.try_acquire(3)
    frozen = ex.TokenBucket(capacity=5, refill_rate=0, clock=clock)
    frozen.try_acquire(5)
    assert frozen.retry_after(1) == math.inf  # 不补充的桶，空了就永远空


def test_invalid_arguments():
    with pytest.raises(ValueError):
        ex.TokenBucket(capacity=0, refill_rate=1)
    with pytest.raises(ValueError):
        ex.TokenBucket(capacity=5, refill_rate=-1)
    b = ex.TokenBucket(capacity=5, refill_rate=1, clock=FakeClock())
    with pytest.raises(ValueError):
        b.try_acquire(0)
    with pytest.raises(ValueError):
        b.retry_after(-1)


def test_clock_going_backwards_does_not_break_bucket():
    clock = FakeClock()
    b = ex.TokenBucket(capacity=5, refill_rate=1, clock=clock)
    for _ in range(5):
        b.try_acquire()
    clock.advance(-10)  # 时钟回拨（比如用了 time.time() 又碰上 NTP 校时）
    assert b.available == 0  # 不能变成负数，也不能凭空多出令牌
    clock.advance(12)  # 回到比原点晚 2 秒
    assert b.available == pytest.approx(2)


# ------------------------------------------------------------------ TenantRateLimiter


PLANS = {
    "free": ex.Plan("free", capacity=3, refill_rate=1),
    "pro": ex.Plan("pro", capacity=10, refill_rate=5),
}


def make_limiter(clock):
    return ex.TenantRateLimiter(PLANS, {"acme": "pro", "hooli": "free"}, default_plan="free", clock=clock)


def test_noisy_neighbor_is_isolated():
    """hooli 疯狂刷接口，只会把自己的桶抽干，acme 完全不受影响。"""
    limiter = make_limiter(FakeClock())
    hooli = [limiter.try_acquire("hooli") for _ in range(100)]
    assert sum(hooli) == 3
    assert all(limiter.try_acquire("acme") for _ in range(10))


def test_plans_give_different_limits_and_unknown_tenant_gets_default():
    limiter = make_limiter(FakeClock())
    assert sum(limiter.try_acquire("acme") for _ in range(20)) == 10  # pro
    assert sum(limiter.try_acquire("new-startup") for _ in range(20)) == 3  # 未登记 → free
    assert limiter.plan_of("new-startup").name == "free"


def test_all_tenant_buckets_share_the_clock():
    clock = FakeClock()
    limiter = make_limiter(clock)
    for tenant in ("acme", "hooli"):
        while limiter.try_acquire(tenant):
            pass
    assert limiter.retry_after("hooli") == pytest.approx(1.0)
    clock.advance(1)
    assert sum(limiter.try_acquire("acme") for _ in range(10)) == 5  # pro：每秒 5 个
    assert sum(limiter.try_acquire("hooli") for _ in range(10)) == 1  # free：每秒 1 个


def test_limiter_rejects_bad_config():
    with pytest.raises(ValueError):
        ex.TenantRateLimiter(PLANS, {}, default_plan="gold")
    with pytest.raises(ValueError):
        ex.TenantRateLimiter(PLANS, {"acme": "platinum"}, default_plan="free")


# ------------------------------------------------------------------ choose_model

# 虚构的模型目录（名字和价格都是示例，不对应任何真实产品）
LITE = ex.ModelSpec("lite", context_window=32_000, supports_tools=False, strong=False, input_price=0.05, output_price=0.2)
MINI = ex.ModelSpec("mini", context_window=128_000, supports_tools=True, strong=False, input_price=0.15, output_price=0.6)
PRO = ex.ModelSpec("pro", context_window=200_000, supports_tools=True, strong=True, input_price=3.0, output_price=15.0)
LONG = ex.ModelSpec("long", context_window=1_000_000, supports_tools=True, strong=True, input_price=5.0, output_price=20.0)
CATALOG = [LITE, MINI, PRO, LONG]


def test_simple_task_goes_to_cheapest_model():
    assert ex.choose_model({"input_tokens": 800, "output_tokens": 200, "complexity": "low"}, CATALOG) == "lite"


def test_needs_tools_filters_out_models_without_tool_support():
    assert ex.choose_model({"input_tokens": 800, "needs_tools": True}, CATALOG) == "mini"


def test_context_window_counts_output_tokens_too():
    assert ex.choose_model({"input_tokens": 30_000, "output_tokens": 1_000}, CATALOG) == "lite"
    # 31_000 + 2_000 > 32_000 → lite 被排除
    assert ex.choose_model({"input_tokens": 31_000, "output_tokens": 2_000}, CATALOG) == "mini"
    assert ex.choose_model({"input_tokens": 500_000, "needs_tools": True}, CATALOG) == "long"


def test_high_complexity_requires_strong_model():
    assert ex.choose_model({"input_tokens": 5_000, "complexity": "high"}, CATALOG) == "pro"
    assert ex.choose_model({"input_tokens": 300_000, "complexity": "high"}, CATALOG) == "long"


def test_cheapest_depends_on_input_output_mix():
    """A 输入便宜输出贵，B 反过来：读长文档写摘要选 A，短问题长回答选 B。"""
    a = ex.ModelSpec("a", 100_000, True, False, input_price=0.1, output_price=2.0)
    b = ex.ModelSpec("b", 100_000, True, False, input_price=1.0, output_price=0.5)
    assert ex.choose_model({"input_tokens": 50_000, "output_tokens": 500}, [a, b]) == "a"
    assert ex.choose_model({"input_tokens": 200, "output_tokens": 4_000}, [a, b]) == "b"


def test_tie_breaks_by_list_order_and_defaults():
    twin1 = ex.ModelSpec("primary", 8_000, True, False, 1.0, 1.0)
    twin2 = ex.ModelSpec("backup", 8_000, True, False, 1.0, 1.0)
    assert ex.choose_model({"input_tokens": 100}, [twin1, twin2]) == "primary"
    assert ex.choose_model({"input_tokens": 100}, [twin2, twin1]) == "backup"
    assert ex.choose_model({"input_tokens": 0}, CATALOG) == "lite"  # 默认 medium、无工具、无输出


def test_no_model_available_explains_why():
    with pytest.raises(ex.NoModelAvailable, match="工具"):
        ex.choose_model({"input_tokens": 100, "needs_tools": True}, [LITE])
    with pytest.raises(ex.NoModelAvailable, match="上下文"):
        ex.choose_model({"input_tokens": 2_000_000}, CATALOG)
    with pytest.raises(ex.NoModelAvailable, match="强模型"):
        ex.choose_model({"input_tokens": 100, "complexity": "high"}, [LITE, MINI])
    with pytest.raises(ex.NoModelAvailable):
        ex.choose_model({"input_tokens": 100}, [])


def test_invalid_task_raises_value_error():
    for bad in ({"output_tokens": 10}, {"input_tokens": -1}, {"input_tokens": 10, "output_tokens": -5},
                {"input_tokens": 10, "complexity": "extreme"}):
        with pytest.raises(ValueError):
            ex.choose_model(bad, CATALOG)


# ------------------------------------------------------------------ 迷你部署：真实的进程（不是练习）

needs_server = pytest.mark.skipif(
    any(importlib.util.find_spec(m) is None for m in ("fastapi", "uvicorn", "httpx")),
    reason='需要 pip install -e ".[server]"',
)
HOOLI = {"Authorization": "Bearer key-hooli-carl"}
ACME = {"Authorization": "Bearer key-acme-alice"}
GLOBEX = {"Authorization": "Bearer key-globex-bob"}


def _deployment(tmp_path, **kw):
    deployment = load_sibling(__file__, "deployment")
    return deployment.MiniDeployment(tmp_path / "mini", router=HERE / "solution.py", **kw)


def _peak(intervals) -> int:
    points = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
    level = best = 0
    for _, d in points:
        level += d
        best = max(best, level)
    return best


@needs_server
async def test_in_memory_buckets_multiply_the_limit_but_a_shared_bucket_holds_it(tmp_path):
    """hooli 的 free 套餐：桶容量 5、每秒补 2 个。两组各两个 API 进程，各发 20 个请求（组内轮流）：
    进程内的桶 → 每个进程各有一个满桶，至少放行 2 × 5 = 10；共享的 SQLiteTokenBucket → 不超过 5 + 2 × 耗时 + 1。"""
    import httpx

    async with _deployment(tmp_path, apis=[("m1", "memory"), ("m2", "memory"), ("s1", "sqlite"), ("s2", "sqlite")],
                           workers=0) as dep:
        async with httpx.AsyncClient(timeout=10) as http:
            async def fire(names):
                accepted, retry_after, served = 0, set(), set()
                start = time.monotonic()
                for i in range(20):
                    r = await http.post(f"{dep.apis[names[i % 2]].url}/runs", json={"message": f"q{i}", "complexity": "low"},
                                        headers=HOOLI)
                    assert r.status_code in (202, 429), r.text
                    served.add(r.headers["x-served-by"])
                    if r.status_code == 202:
                        accepted += 1
                    else:
                        retry_after.add(int(r.headers["retry-after"]))
                return accepted, time.monotonic() - start, retry_after, served

            memory, shared = await asyncio.gather(fire(["m1", "m2"]), fire(["s1", "s2"]))
    assert memory[3] == {"m1", "m2"} and shared[3] == {"s1", "s2"}  # 请求真的落在了两个不同的进程上
    assert memory[0] >= 10, memory  # 每个进程各自的满桶：配额被放大
    assert 5 <= shared[0] <= 5 + 2 * shared[1] + 1, shared  # 共享的桶守住了"容量 + 速率 × 时间"
    assert shared[2] and min(shared[2]) >= 1  # 429 带着 Retry-After


@needs_server
async def test_202_then_poll_and_another_worker_resumes_after_kill_9(tmp_path):
    """长任务：POST 立刻 202；跑到一半 kill -9 持有它的 worker；另一个 worker 进程从检查点接着跑完同一个 run。"""
    import httpx

    async with _deployment(tmp_path, apis=[("api", "sqlite")], workers=2, lease=1.0,
                           worker_options={"long_latency": 0.8}) as dep:
        async with httpx.AsyncClient(timeout=10) as http:
            base = dep.apis["api"].url
            started = time.monotonic()
            r = await http.post(f"{base}/runs", json={"message": "帮我汇总部门请假情况并生成报告", "complexity": "high"},
                                headers=ACME)
            assert r.status_code == 202 and time.monotonic() - started < 5
            run_id = r.json()["run_id"]
            deadline = time.monotonic() + 60
            killed = first_fence = None
            while time.monotonic() < deadline:
                info = (await http.get(f"{base}/runs/{run_id}", headers=ACME)).json()
                if killed is None and info["job_status"] == "leased" and (info["step"] or 0) >= 1:
                    killed, first_fence = info["worker_id"], info["fence"]
                    dep.pool.kill(next(i for i, w in enumerate(dep.pool.workers) if w.worker_id == killed))
                if info["job_status"] == "succeeded":
                    break
                await asyncio.sleep(0.05)
            other = (await http.get(f"{base}/runs/{run_id}", headers=GLOBEX)).status_code
    assert killed is not None, "没有等到任务跑到一半"
    assert info["job_status"] == "succeeded" and info["run_status"] == "completed", info
    assert info["attempts"] == 2 and info["fence"] > first_fence
    assert info["worker_id"] != killed and info["writer"] == info["worker_id"]  # 检查点最后由接手者写入
    assert info["step"] == 3 and "汇总报告" in info["output"]  # 从检查点接着跑，没有从头再来（一共 3 步）
    assert other == 404  # 别的租户看不到这个 run


@needs_server
async def test_tenant_bulkhead_holds_across_worker_processes(tmp_path):
    """globex 一口气提交 12 个任务，2 个 worker 进程 × 每个 8 并发：
    进程内 KeyedLimiter 每个进程最多 2 个，跨进程 SQLiteSemaphore 加起来最多 3 个；拿不到槽位的任务被推迟而不是失败。"""
    import httpx

    async with _deployment(tmp_path, apis=[("api", "sqlite")], workers=2,
                           worker_options={"latency": 0.2, "tenant_local": 2, "tenant_shared": 3}) as dep:
        async with httpx.AsyncClient(timeout=10) as http:
            base = dep.apis["api"].url
            runs = [(await http.post(f"{base}/runs", json={"message": f"我还剩几天年假？{i}", "complexity": "low"},
                                     headers=GLOBEX)).json()["run_id"] for i in range(12)]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                infos = [(await http.get(f"{base}/runs/{rid}", headers=GLOBEX)).json() for rid in runs]
                if all(i["job_status"] == "succeeded" for i in infos):
                    break
                await asyncio.sleep(0.2)
        deferred = dep.pool.events("deferred")
    assert all(i["job_status"] == "succeeded" for i in infos)
    conn = sqlite3.connect(dep.db)
    rows = conn.execute("SELECT pid, start, end FROM slot_log WHERE tenant = 'globex'").fetchall()
    conn.close()
    assert len(rows) == 12
    assert _peak([(s, e) for _, s, e in rows]) <= 3  # 所有 worker 进程加起来
    for pid in {p for p, _, _ in rows}:
        assert _peak([(s, e) for p, s, e in rows if p == pid]) <= 2  # 每个 worker 进程里
    assert len({p for p, _, _ in rows}) == 2  # 两个进程都干了活
    assert deferred and all(e["job"] for e in deferred)  # 舱壁真的挡过：任务被放回队列而不是失败
