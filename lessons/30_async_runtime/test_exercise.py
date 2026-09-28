"""第 30 课练习测试：真实并发、确定性断言。

运行：make lesson N=30    或    .venv/bin/python -m pytest lessons/30_async_runtime -v

为什么这些测试不 flaky？断言的都是"计数"和"状态"（同时在跑几个、谁收到了 CancelledError、收尾是否完成），
而不是精确的耗时；少数耗时断言只用来区分"几十毫秒"和"好几秒"，留了很大的余量。
每个 async 测试都在 main() 里先拍下状态快照再返回 —— 因为 asyncio.run 退出前会自动取消并等待残留任务，
如果等它返回后再检查，一个"把子任务留在后台"的错误实现也会碰巧通过。
"""

from __future__ import annotations

import asyncio
import textwrap
import time

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)


def new_log() -> dict:
    return {"started": 0, "running": 0, "cancelled": 0, "cleaned": 0}


def slow_job(log: dict, seconds: float = 5.0):
    """一个会被取消的慢任务：记录启动、取消、收尾（收尾本身也要 20ms，例如回滚事务、归还连接）。"""

    async def job():
        log["started"] += 1
        log["running"] += 1
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            log["cancelled"] += 1
            await asyncio.sleep(0.02)
            log["cleaned"] += 1
            raise
        finally:
            log["running"] -= 1

    return job


# ------------------------------------------------------------------ (a) bounded_gather


def test_bounded_gather_keeps_input_order_and_caps_concurrency():
    state = {"running": 0, "peak": 0}

    async def job(i: int) -> int:
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            await asyncio.sleep(0.01 * (10 - i))  # 越靠前越慢：完成顺序和输入顺序相反
            return i * i
        finally:
            state["running"] -= 1

    results = asyncio.run(ex.bounded_gather([lambda i=i: job(i) for i in range(10)], limit=3))
    assert results == [i * i for i in range(10)], "结果必须按输入顺序排列"
    assert state["peak"] == 3, f"应该真的并发到上限 3 且不超过，实际峰值 {state['peak']}"


def test_bounded_gather_creates_coroutines_lazily():
    """工厂函数只能在拿到名额时调用：'已创建但未完成'的协程数任何时刻都 ≤ limit。"""
    state = {"created": 0, "finished": 0, "max_alive": 0}

    async def job(i: int) -> int:
        await asyncio.sleep(0.005)
        state["finished"] += 1
        return i

    def factory(i: int):
        state["created"] += 1
        state["max_alive"] = max(state["max_alive"], state["created"] - state["finished"])
        return job(i)

    results = asyncio.run(ex.bounded_gather([lambda i=i: factory(i) for i in range(20)], limit=4))
    assert results == list(range(20))
    assert state["max_alive"] <= 4, f"一次性创建了 {state['max_alive']} 个协程：没有背压"


def test_first_failure_cancels_the_rest_and_is_raised_as_is():
    log = new_log()
    boom = ValueError("工单系统返回 500")

    async def failing():
        await asyncio.sleep(0.05)
        raise boom

    factories = [slow_job(log), failing, *[slow_job(log) for _ in range(6)]]

    async def main():
        t0 = time.perf_counter()
        try:
            await ex.bounded_gather(factories, limit=3)
        except ValueError as e:
            return e, time.perf_counter() - t0, dict(log)  # 抛出的那一刻拍快照
        raise AssertionError("应该抛出 ValueError")

    error, elapsed, snap = asyncio.run(main())
    assert error is boom, "要原样抛出第一个异常（不是 ExceptionGroup，也不是新包装的异常）"
    assert elapsed < 2.0, f"失败后应立刻取消其余任务，而不是等它们跑完（耗时 {elapsed:.2f}s）"
    assert snap["running"] == 0, "抛出异常时不能还有子任务在后台跑"
    assert snap["started"] >= 2 and snap["cancelled"] == snap["cleaned"] == snap["started"], snap
    assert snap["started"] < 7, "失败之后不应再把剩下的任务全部启动"


def test_caller_cancellation_cancels_all_children_and_propagates():
    log = new_log()

    async def main():
        task = asyncio.create_task(ex.bounded_gather([slow_job(log) for _ in range(5)], limit=2))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled", dict(log)
        return "returned normally", dict(log)

    outcome, snap = asyncio.run(main())
    assert outcome == "cancelled", "调用方的取消必须继续往外抛"
    assert snap == {"started": 2, "running": 0, "cancelled": 2, "cleaned": 2}, snap


def test_bounded_gather_validates_limit_and_handles_edges():
    async def one():
        return 1

    with pytest.raises(ValueError):
        asyncio.run(ex.bounded_gather([one], limit=0))
    assert asyncio.run(ex.bounded_gather([], limit=3)) == []
    assert asyncio.run(ex.bounded_gather([one, one], limit=10)) == [1, 1]  # limit 比任务多也没问题


# ------------------------------------------------------------------ (b) with_deadline


def test_with_deadline_returns_result_or_raises_inner_error():
    calls = []

    async def ok():
        await asyncio.sleep(0.01)
        return "答案"

    async def bad():
        await asyncio.sleep(0)
        raise KeyError("订单不存在")

    assert asyncio.run(ex.with_deadline(ok(), 1.0, lambda: calls.append(1) or "默认")) == "答案"
    assert calls == [], "没超时就不应该调用 on_timeout"
    with pytest.raises(KeyError):
        asyncio.run(ex.with_deadline(bad(), 1.0, "默认"))


def test_with_deadline_cancels_inner_and_returns_fallback():
    log = new_log()

    async def main():
        t0 = time.perf_counter()
        value = await ex.with_deadline(slow_job(log)(), 0.05, lambda: "（超时，先返回缓存结果）")
        return value, time.perf_counter() - t0, dict(log)

    value, elapsed, snap = asyncio.run(main())
    assert value == "（超时，先返回缓存结果）"
    assert elapsed < 1.0
    assert snap == {"started": 1, "running": 0, "cancelled": 1, "cleaned": 1}, "超时要取消内部协程并等它收尾"
    assert asyncio.run(ex.with_deadline(asyncio.sleep(10), 0.01, "普通值")) == "普通值"


def test_with_deadline_never_swallows_outer_cancellation():
    log = new_log()

    async def main():
        task = asyncio.create_task(ex.with_deadline(slow_job(log)(), 5.0, "默认值"))
        await asyncio.sleep(0.05)
        task.cancel()  # 例如：用户关掉了页面
        try:
            value = await task
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # 给内部协程收尾的时间（即使实现没有等它）
            return "cancelled", dict(log)
        return f"returned {value!r}", dict(log)

    outcome, snap = asyncio.run(main())
    assert outcome == "cancelled", "外部取消被吞掉了：调用方以为拿到了结果，取消链路就此断开"
    assert snap["cancelled"] == 1 and snap["running"] == 0, "外部取消时内部协程也要被取消"


def test_inner_timeout_error_is_not_mistaken_for_the_deadline():
    async def downstream():
        # 下游自己超时了（例如工具内部对某个 API 用了更短的超时）：这是一个错误，要让上层知道，
        # 而不是被当成"我们的截止时间到了"、悄悄换成默认值
        await asyncio.wait_for(asyncio.sleep(10), 0.01)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(ex.with_deadline(downstream(), 5.0, "默认值"))


def test_with_deadline_keeps_cancel_when_inner_finishes_at_the_same_moment():
    """内部结果和外部取消在同一轮事件循环里到达：以取消为准。

    Python 3.10/3.11 的 asyncio.wait_for 在这里有竞态（CPython gh-86296）：它会返回结果、吞掉取消。
    第 30 课在 Agent 的工具执行器里实测到了它：同步工具执行完的那一刻用户断开，运行照样跑完（后来 agentkit 改用 timeouts.wait_for）。
    """

    async def main():
        loop = asyncio.get_running_loop()
        inner = loop.create_future()  # 相当于线程池里正在执行的工具
        task = asyncio.create_task(ex.with_deadline(inner, 5.0, "默认值"))
        for _ in range(3):
            await asyncio.sleep(0)  # 让 with_deadline 进入等待
        inner.set_result("工具结果")  # 工具恰好完成……
        task.cancel()  # ……取消恰好同时到达
        try:
            value = await task
        except asyncio.CancelledError:
            return "cancelled"
        return f"returned {value!r}"

    assert asyncio.run(main()) == "cancelled", "取消被吞掉了：调用方以为拿到了结果，运行会继续跑下去"


# ------------------------------------------------------------------ (c) detect_blocking


def src(code: str) -> str:
    return textwrap.dedent(code).strip("\n")


def test_detects_blocking_calls_only_inside_async_functions():
    code = src(
        """
        import time
        import requests

        async def handler(url):
            time.sleep(1)
            resp = requests.get(url)
            return resp.text

        def sync_worker(url):
            time.sleep(1)
            return requests.post(url)
        """
    )
    assert ex.detect_blocking(code) == ["handler:5:time.sleep", "handler:6:requests.get"]


def test_resolves_import_aliases():
    code = src(
        """
        import time as t
        import urllib.request
        from time import sleep as nap
        from requests import post

        async def f(url):
            t.sleep(0.1)
            nap(0.1)
            post(url, json={})
            return urllib.request.urlopen(url).read()
        """
    )
    assert ex.detect_blocking(code) == [
        "f:7:time.sleep",
        "f:8:time.sleep",
        "f:9:requests.post",
        "f:10:urllib.request.urlopen",
    ]


def test_ignores_awaited_calls_and_function_references():
    code = src(
        """
        import asyncio, time, requests

        async def ok(client, url):
            await asyncio.sleep(1)
            await asyncio.to_thread(time.sleep, 1)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, requests.get, url)
            return await client.get(url)
        """
    )
    assert ex.detect_blocking(code) == []


def test_nested_functions_are_scoped_correctly():
    code = src(
        """
        import asyncio, time

        async def outer():
            def work():
                time.sleep(1)
            callback = lambda: time.sleep(1)

            async def inner():
                time.sleep(2)

            await asyncio.to_thread(work)
            await inner()
        """
    )
    assert ex.detect_blocking(code) == ["inner:9:time.sleep"]


def test_detects_sync_file_io_and_subprocess_but_not_async_libraries():
    code = src(
        """
        import subprocess
        import aiofiles

        class Loader:
            async def load(self, path):
                with open(path) as f:
                    data = f.read()
                subprocess.run(["gzip", path])
                async with aiofiles.open(path) as f:
                    return data + await f.read()
        """
    )
    assert ex.detect_blocking(code) == ["load:6:open", "load:8:subprocess.run"]
    shadowed = src(
        """
        from aiofiles import open

        async def load_async(path):
            async with open(path) as f:
                return await f.read()
        """
    )
    assert ex.detect_blocking(shadowed) == [], "from aiofiles import open 之后，open 指的是 aiofiles.open"
