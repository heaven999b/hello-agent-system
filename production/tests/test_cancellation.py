"""取消被第三方库"吞掉"时，CancellationFence 在下一个步骤边界把运行停下来（本课压测抓到的真实问题）。

背景：Python 3.12 之前的 asyncio.wait_for 有竞态（CPython gh-86296）：被等待的东西刚好就绪、外部取消又在同一轮事件循环
到达时，它返回结果、吞掉取消。agentkit.aio 已经换成取消安全的 wait_for，但本服务依赖的两个库内部仍在用它：
  - redis-py 8.1.0：每条命令都经过 AbstractConnection.send_packed_command → asyncio.wait_for
  - psycopg_pool 3.3.3：等连接时 ACondition.wait_timeout → asyncio.wait_for
压测里的表现：交互式运行在"限流 Hook 调 Redis"时被取消，取消被吞掉，客户端早已断开，运行照常跑完、照样建了工单。

前两条测试在本机的 Python 上证明这两个库确实会吞掉取消（3.12+ 上跳过）；后面三条用一个"会吞掉取消"的 Hook 把竞态
确定性地复现出来，证明闸门有效。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import sys
from pathlib import Path

import pytest

for _mod in ("cedarpy", "psycopg_pool", "redis", "prometheus_client"):
    pytest.importorskip(_mod)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentkit import call_tool, reply, tool  # noqa: E402
from agentkit.aio import AsyncAgent, AsyncScriptedLLM, ToolFinished  # noqa: E402
from agentkit.hooks import Hook  # noqa: E402
from agentkit.state import InMemoryCheckpointer  # noqa: E402

from production.service.runtime import CancellationFence  # noqa: E402

needs_old_wait_for = pytest.mark.skipif(sys.version_info >= (3, 12), reason="3.12 起 asyncio.wait_for 已修复 gh-86296")


async def cancel_mid_flight(op, trials: int) -> dict:
    """反复启动 op 并在它进行中取消；统计取消被吞掉（任务正常结束）的次数。"""
    out = {"cancelled": 0, "swallowed": 0}
    for _ in range(trials):
        async def job():
            await op()
            await asyncio.sleep(0.005)  # 如果取消没被吞掉，最迟会在这里抛出

        t = asyncio.create_task(job())
        for _ in range(random.randint(0, 10)):
            await asyncio.sleep(0)
        t.cancel()
        try:
            await t
            out["swallowed"] += 1
        except asyncio.CancelledError:
            out["cancelled"] += 1
    return out


@needs_old_wait_for
def test_redis_py_can_swallow_cancellation(redis_url):
    import redis.asyncio as aredis

    async def main():
        r = aredis.Redis.from_url(redis_url)
        await r.set("x", 1)
        res = await cancel_mid_flight(lambda: r.get("x"), 200)
        await r.aclose()
        return res

    random.seed(3)
    res = asyncio.run(main())
    assert res["swallowed"] >= 1, res  # 本机 3.11.7 实测约 20%–25%


@needs_old_wait_for
def test_psycopg_pool_swallows_cancellation_when_a_connection_arrives_at_the_same_time(pg_uri):
    from psycopg_pool import AsyncConnectionPool

    async def trial() -> str:
        async with AsyncConnectionPool(pg_uri, min_size=1, max_size=1, kwargs={"autocommit": True}, open=False) as pool:
            await pool.open(wait=True)
            holder = await pool.getconn()
            used = []

            async def waiter():
                async with pool.connection() as c:
                    await c.execute("SELECT 1")
                    used.append(True)

            t = asyncio.create_task(waiter())
            await asyncio.sleep(0.05)  # waiter 正在等连接
            await pool.putconn(holder)  # 连接交给它……
            t.cancel()  # ……同一轮事件循环里它的主人取消了它
            try:
                await t
            except asyncio.CancelledError:
                return "cancelled"
            return "swallowed" if used else "?"

    async def main():
        return [await trial() for _ in range(3)]

    assert "swallowed" in asyncio.run(main())  # 本机 3.11.7 实测 20/20


class SwallowsCancellation(Hook):
    """模拟"限流 Hook 调 Redis 时取消被吞掉"：before_llm 里等一会儿，期间的取消当作没发生。"""

    def __init__(self):
        self.swallowed = 0

    async def before_llm(self, state, messages):
        if any(m["role"] == "tool" for m in state.messages):  # 只在第一个工具之后吞，保证断开正好落在这里
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                self.swallowed += 1


def make_agent(fence: CancellationFence | None):
    executed = []

    @tool(risk="write")
    async def create_ticket(title: str) -> str:
        """建工单"""
        executed.append(title)
        return "T-1"

    @tool
    async def search_kb(q: str) -> str:
        """查知识库"""
        return "KB"

    def respond(messages):
        done = sum(1 for m in messages if m["role"] == "tool")
        return call_tool("search_kb", q="vpn") if done == 0 else call_tool("create_ticket", title="VPN") if done == 1 else reply("好了")

    swallow = SwallowsCancellation()
    hooks = ([fence] if fence else []) + [swallow]
    agent = AsyncAgent(AsyncScriptedLLM(responder=respond, latency=0.01), [search_kb, create_ticket],
                       checkpointer=InMemoryCheckpointer(), hooks=hooks)
    return agent, swallow, executed


async def disconnect_after_first_tool(agent, fence, run_id: str, register: bool):
    """模拟 API：客户端读到第一个 tool_finished 就断开；API 登记 abandoned（可选）并取消运行。"""
    async with contextlib.aclosing(agent.stream("VPN 连不上，帮我提工单", run_id=run_id)) as events:
        async for e in events:
            if isinstance(e, ToolFinished):
                if register:
                    fence.abandon(run_id)
                break  # aclosing → 取消运行任务；此刻运行正卡在"会吞掉取消"的 Hook 里
    return agent.checkpointer.load(run_id)


def test_without_the_fence_a_swallowed_cancellation_lets_the_run_finish():
    async def main():
        agent, swallow, executed = make_agent(None)
        return await disconnect_after_first_tool(agent, None, "r-1", False), swallow, executed

    state, swallow, executed = asyncio.run(main())
    assert swallow.swallowed == 1
    assert state.status == "completed" and executed == ["VPN"]  # 用户早走了，工单照样建了


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Task.cancelling() 是 3.11 新增的")
def test_fence_notices_the_pending_cancellation_by_itself():
    async def main():
        fence = CancellationFence()
        agent, swallow, executed = make_agent(fence)
        return await disconnect_after_first_tool(agent, fence, "r-2", register=False), swallow, executed, fence

    state, swallow, executed, fence = asyncio.run(main())
    assert swallow.swallowed == 1
    assert state.status == "cancelled" and executed == []  # 下一个步骤边界（调工具前）就停了，工单没建


def test_fence_stops_a_run_registered_as_abandoned():
    async def main():
        fence = CancellationFence()
        agent, swallow, executed = make_agent(fence)
        return await disconnect_after_first_tool(agent, fence, "r-3", register=True), swallow, executed, fence

    state, swallow, executed, fence = asyncio.run(main())
    assert state.status == "cancelled" and executed == []
    assert fence.ids == {}  # 运行结束时清理，不会无限增长
