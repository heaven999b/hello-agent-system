"""第 02 课 Demo（async）：一个进程怎么同时服务很多会话。完全离线，不需要 API key。

    .venv/bin/python lessons/02_agent_loop/demo_async.py
    .venv/bin/python lessons/02_agent_loop/demo_async.py --offline   # 同上（本 demo 本来就是离线的）

模型用 ScriptedLLM(latency=0.2) 扮演：每次调用"想" 0.2 秒。它等待用的是 asyncio.sleep，
和真实的 AsyncOpenAI 客户端等网络回复一样，等待期间会让出事件循环。
ScriptedLLM 会记录 max_in_flight = 同一时刻有多少个模型调用在途 —— 这是"并发真的发生了"的确定性证据，
比看耗时可靠（耗时会随机器负载波动）。

四个实验：
  1. 忘了 await：拿到的是一个协程对象，模型根本没有被调用
  2. 10 个会话：一个接一个 vs asyncio.gather 同时跑 vs Semaphore 限流 —— 看耗时和 max_in_flight
  3. 头号陷阱：在 async 代码里调用阻塞函数（time.sleep）会让所有会话一起停下；以及三种正确写法
  4. 取消：task.cancel() → 正在等待的那个 await 抛出 CancelledError，运行停下并记为 cancelled
"""

from __future__ import annotations

import asyncio
import threading
import time

from agentkit import Agent, InMemoryCheckpointer, ScriptedLLM, call_tool, reply, tool
from agentkit.types import user

N_SESSIONS = 10
LATENCY = 0.2  # 每次模型调用 0.2 秒（真实模型通常 1–10 秒，这里缩小方便演示）


def banner(title: str) -> None:
    print(f"\n{'═' * 72}\n{title}\n{'═' * 72}")


def pad(s: str, width: int) -> str:
    """按终端显示宽度补空格（中文字符占两格），用来对齐表格。"""
    shown = sum(2 if ord(ch) > 127 else 1 for ch in s)
    return s + " " * max(1, width - shown)


def weather_responder(messages):
    """每个会话的"剧本"：第 1 次调用 → 请求查天气；看到工具结果之后 → 给出答案。
    用 responder 而不是 script 列表：很多会话共用一个模型对象时，每个会话都按自己的对话推进，互不串台。"""
    if messages[-1]["role"] == "tool":
        return reply(f"查到了：{messages[-1]['content']}")
    return call_tool("get_weather", city="北京")


@tool
def get_weather(city: str) -> str:
    """查询城市天气（瞬间返回的假数据）。"""
    return f"{city} 31°C 晴"


# ─────────────────────────────────────────────────────────────── 实验 1


async def exp1_forgot_await() -> None:
    banner("实验 1：忘了 await 会怎样")
    llm = ScriptedLLM([reply("你好！")])

    oops = llm.chat([user("hi")])  # ❌ 忘了 await
    print(f"  oops = llm.chat(...)          → 得到 {repr(oops).split(' at ')[0]}>，类型是 {type(oops).__name__}")
    print(f"  这时模型被调用了几次？          → call_count = {llm.call_count}（协程只是一张'待办单'，还没执行）")
    oops.close()  # 不打算 await 的协程要关掉，否则 Python 会警告 "coroutine ... was never awaited"

    right = await llm.chat([user("hi")])  # ✅
    print(f"  right = await llm.chat(...)   → 得到 {type(right).__name__}，content = {right.content!r}，call_count = {llm.call_count}")
    print("\n💡 async def 函数被调用时不会执行，只返回一个协程；await 它，它才真正运行并交回结果。")
    print("   在练习里忘了 await，工具结果会变成 '<coroutine object ...>' 这样一串字，测试会提醒你。")


# ─────────────────────────────────────────────────────────────── 实验 2


async def exp2_many_sessions() -> None:
    banner(f"实验 2：{N_SESSIONS} 个会话，每个会话 = 2 次模型调用（各 {LATENCY}s）+ 1 次工具调用")

    def make() -> tuple[Agent, ScriptedLLM]:
        llm = ScriptedLLM(responder=weather_responder, latency=LATENCY)
        return Agent(llm, [get_weather]), llm

    rows = []

    # A. 一个接一个：await 完一个会话，再开始下一个
    agent, llm = make()
    t0 = time.perf_counter()
    for i in range(N_SESSIONS):
        await agent.run(f"会话 {i}：北京天气？")
    rows.append(("一个接一个（for + await）", time.perf_counter() - t0, llm.max_in_flight, llm.call_count))

    # B. 同时跑：gather 把 10 个协程同时交给事件循环
    agent, llm = make()
    t0 = time.perf_counter()
    results = await asyncio.gather(*(agent.run(f"会话 {i}：北京天气？") for i in range(N_SESSIONS)))
    assert all(r.ok for r in results)
    rows.append(("asyncio.gather 同时跑", time.perf_counter() - t0, llm.max_in_flight, llm.call_count))

    # C. 同时跑，但最多 3 个在途：Semaphore 就是一个"最多 N 个人同时进"的闸门
    agent, llm = make()
    sem = asyncio.Semaphore(3)

    async def limited(i: int):
        async with sem:  # 拿不到名额就在这里等（等待期间同样让出事件循环）
            return await agent.run(f"会话 {i}：北京天气？")

    t0 = time.perf_counter()
    await asyncio.gather(*(limited(i) for i in range(N_SESSIONS)))
    rows.append(("gather + Semaphore(3)", time.perf_counter() - t0, llm.max_in_flight, llm.call_count))

    print(pad("写法", 30) + pad("耗时", 10) + pad("max_in_flight", 16) + "模型调用次数")
    print("─" * 72)
    for label, secs, peak, calls in rows:
        print(pad(label, 30) + pad(f"{secs:.2f}s", 10) + pad(str(peak), 16) + str(calls))
    print(f"\n💡 理论值：一个接一个 = {N_SESSIONS} × 2 × {LATENCY}s = {N_SESSIONS * 2 * LATENCY:.1f}s；"
          f"同时跑 ≈ 2 × {LATENCY}s = {2 * LATENCY:.1f}s；Semaphore(3) ≈ 4 批 × {2 * LATENCY:.1f}s = {4 * 2 * LATENCY:.1f}s。")
    print("   同一个 Agent 实例、同一个线程、同一个进程：等模型的时候，事件循环去推进别的会话。")
    print("   max_in_flight 是确定性的证据：一个接一个时永远是 1，gather 时是 10，Semaphore(3) 时封顶 3。")


# ─────────────────────────────────────────────────────────────── 实验 3


class Gauge:
    """数"同一时刻有几个工具在执行"。同步工具在线程池里跑，所以要加锁。"""

    def __init__(self):
        self.now = 0
        self.peak = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def exit(self) -> None:
        with self._lock:
            self.now -= 1


async def heartbeat(stop: asyncio.Event, lags: list[float], interval: float = 0.01) -> None:
    """每 10ms 醒一次，记录"比预定时间晚了多久"。事件循环被卡住时，心跳就会迟到。"""
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lags.append(time.perf_counter() - t0 - interval)


def make_inventory_tools(gauge: Gauge) -> dict[str, object]:
    """同一个"查库存"工具（要等外部系统 0.2 秒）的四种写法。"""

    @tool
    async def async_sleep(sku: str) -> str:
        """查询库存。"""
        gauge.enter()
        try:
            await asyncio.sleep(LATENCY)  # ✅ 等待时让出事件循环
        finally:
            gauge.exit()
        return f"{sku}: 7 件"

    @tool
    async def blocking_in_async(sku: str) -> str:
        """查询库存。"""
        gauge.enter()
        try:
            time.sleep(LATENCY)  # ❌ 阻塞调用：整个事件循环（所有会话）跟着停 0.2 秒
        finally:
            gauge.exit()
        return f"{sku}: 7 件"

    @tool
    async def to_thread(sku: str) -> str:
        """查询库存。"""
        gauge.enter()
        try:
            await asyncio.to_thread(time.sleep, LATENCY)  # ✅ 改不掉的阻塞调用：挪到线程里，await 它
        finally:
            gauge.exit()
        return f"{sku}: 7 件"

    @tool
    def plain_sync(sku: str) -> str:
        """查询库存。"""
        gauge.enter()
        try:
            time.sleep(LATENCY)  # 普通 def 工具：agentkit 自动放进线程池执行，不卡事件循环
        finally:
            gauge.exit()
        return f"{sku}: 7 件"

    return {
        "async def + await asyncio.sleep": async_sleep,
        "async def + time.sleep  ❌": blocking_in_async,
        "async def + asyncio.to_thread": to_thread,
        "普通 def + time.sleep（线程池）": plain_sync,
    }


async def exp3_blocking_pitfall() -> None:
    banner(f"实验 3：头号陷阱 —— {N_SESSIONS} 个会话同时跑，每个会话调一次要等 {LATENCY}s 的工具")

    def responder_for(tool_name: str):
        def responder(messages):
            if messages[-1]["role"] == "tool":
                return reply("好的")
            return call_tool(tool_name, sku="A-1")

        return responder

    print(pad("工具的写法", 36) + pad("耗时", 9) + pad("工具同时在跑", 14) + "事件循环最长卡顿")
    print("─" * 76)
    gauge = Gauge()
    for label, t in make_inventory_tools(gauge).items():
        gauge.peak = 0
        agent = Agent(ScriptedLLM(responder=responder_for(t.name), latency=LATENCY), [t])
        stop, lags = asyncio.Event(), []
        beat = asyncio.ensure_future(heartbeat(stop, lags))
        t0 = time.perf_counter()
        results = await asyncio.gather(*(agent.run(f"会话 {i}") for i in range(N_SESSIONS)))
        elapsed = time.perf_counter() - t0
        stop.set()
        await beat
        assert all(r.ok for r in results)
        print(pad(label, 36) + pad(f"{elapsed:.2f}s", 9) + pad(f"{gauge.peak} 个", 14) + f"{max(lags) * 1000:.0f}ms")

    print(f"\n💡 time.sleep / requests.get / 同步数据库驱动 写在 async def 里：它不 await、不让出，")
    print(f"   {N_SESSIONS} 个会话的工具只能一个接一个跑（工具同时在跑 = 1），事件循环被整段卡住 ——")
    print("   这段时间里别的会话收不到模型回复、心跳发不出去、HTTP 请求没人接。")
    print("   修法：换成 async 客户端（asyncio.sleep、httpx.AsyncClient、asyncpg）；实在换不掉，就 await asyncio.to_thread(...)；")
    print("   或者干脆把工具写成普通 def —— agentkit 的 ToolExecutor 会把同步工具放进线程池（第 03 课）。")



# ─────────────────────────────────────────────────────────────── 实验 4


async def exp4_cancellation() -> None:
    banner("实验 4：取消 —— 用户关掉了页面，这次运行要立刻停下，别再花钱")
    llm = ScriptedLLM([reply("一份很长的报告……")], latency=5.0)  # 这次模型要"想" 5 秒
    agent = Agent(llm, [], checkpointer=InMemoryCheckpointer())

    task = asyncio.ensure_future(agent.run("写一份很长的报告", run_id="demo-cancel"))
    await asyncio.sleep(0.1)  # 让它跑起来，停在 await llm.chat(...) 上
    print(f"  0.1s 后：模型调用在途 {llm.in_flight} 个，调用 task.cancel()")
    t0 = time.perf_counter()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print(f"  await task → 抛出 CancelledError，用时 {(time.perf_counter() - t0) * 1000:.0f}ms（不用等满 5 秒）")
    state = agent.checkpointer.load("demo-cancel")
    print(f"  检查点里的状态：status={state.status!r}  stop_reason={state.stop_reason!r}  模型在途 {llm.in_flight} 个")
    print("\n💡 取消 = 在被取消的任务下一次 await 的地方抛出 CancelledError。这里它停在 ScriptedLLM 的 asyncio.sleep 上；")
    print("   换成真实模型，就是停在 AsyncOpenAI 等网络回复的地方，HTTP 请求随之断开。")
    print("   CancelledError 是 BaseException，不是 Exception：`except Exception` 不会吞掉它，这是故意的。")


async def main() -> None:
    print("🎬 离线演示：ScriptedLLM(latency=0.2) 扮演模型，每次调用等 0.2 秒")
    await exp1_forgot_await()
    await exp2_many_sessions()
    await exp3_blocking_pitfall()
    await exp4_cancellation()


if __name__ == "__main__":
    asyncio.run(main())
