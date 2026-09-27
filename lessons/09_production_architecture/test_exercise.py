"""第 09 课练习测试：离线、确定性（用假时钟，不 sleep）。

运行：make lesson N=09
"""

from __future__ import annotations

import math

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)


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
