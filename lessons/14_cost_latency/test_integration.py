"""第 14 课集成测试：costkit.py 教学实现里"声称做到了"的能力，逐条用确定性的量证明。

这些测试不属于练习——它们不依赖 exercise.py，在练习没写完时也会通过：
  - 共享缓存：进程 A 写入的答案，**另一个操作系统进程** B 直接命中（进程内 LRU 做不到）；
    Demo 场景 1b 用的 worker 应用（cache_app.py，WorkerPool 拉起的真 worker 进程）也跑一遍；
  - 对冲请求：胜者出现后输家被真正取消（CancelledError 送进它 await 的地方）、在途归零、没有拿到响应；
    调用方取消时所有在途请求一起取消；
  - 并发：同一个 CachingLLM / CascadeLLM 被很多协程同时使用，计数准确；没有 single-flight 时同时未命中会各调一次模型。
断言的都是次数、在途峰值、谁写入的；时间只作宽松上限（机器负载高时也不会误报）。

    .venv/bin/python -m pytest lessons/14_cost_latency/test_integration.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agentkit import LLMError, ScriptedLLM, reply
from agentkit.distributed import SQLiteJobQueue, WorkerPool
from agentkit.testing import load_sibling

ck = load_sibling(__file__, "costkit")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MSGS = [{"role": "user", "content": "VPN 连不上怎么办？"}]

# 在一个独立的 python 进程里问一次：返回这个进程看到的命中情况、调用模型的次数、缓存表里的写入者
ASK_SCRIPT = r"""
import asyncio, json, os, sys
from agentkit import ScriptedLLM, reply
from agentkit.testing import load_sibling
ck = load_sibling(sys.argv[1], "costkit")

async def main():
    db, mode, tenant = sys.argv[2], sys.argv[3], sys.argv[4]
    base = ck.MeteredLLM(ScriptedLLM(responder=lambda m: reply("先确认没连访客 Wi-Fi", input_tokens=100, output_tokens=10),
                                     model="m"))
    store = ck.SQLiteResponseCache(db, writer=f"pid {os.getpid()}") if mode == "shared" else ck.ResponseCache()
    llm = ck.CachingLLM(base, store, scope={"tenant_id": tenant})
    r = await llm.chat([{"role": "user", "content": "VPN 连不上怎么办？"}])
    out = {"pid": os.getpid(), "hits": llm.hits, "model_calls": base.completed, "content": r.content}
    if mode == "shared":
        out["writers"] = [e["writer"] for e in await store.entries()]
        await store.close()
    print(json.dumps(out, ensure_ascii=False))

asyncio.run(main())
"""


async def ask_in_new_process(db: Path, mode: str, tenant: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", ASK_SCRIPT, str(Path(__file__).resolve()), str(db), mode, tenant,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))},
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return json.loads(out.decode().strip().splitlines()[-1])


# ------------------------------------------------------------------ 共享缓存：真的跨进程


async def test_shared_cache_is_hit_by_another_os_process(tmp_path):
    db = tmp_path / "cache.db"
    a = await ask_in_new_process(db, "shared", "acme")
    b = await ask_in_new_process(db, "shared", "acme")
    c = await ask_in_new_process(db, "shared", "globex")
    assert a["pid"] != b["pid"]
    assert (a["hits"], a["model_calls"]) == (0, 1)  # A：未命中，调用了模型，把答案写进共享表
    assert (b["hits"], b["model_calls"]) == (1, 0), "B 是另一个进程，应该直接命中 A 写入的答案"
    assert b["content"] == a["content"]
    assert f"pid {a['pid']}" in b["writers"], "B 命中的那条是 A 写入的"
    assert (c["hits"], c["model_calls"]) == (0, 1), "换了租户，跨进程也不能命中"


async def test_in_process_cache_is_not_shared_between_processes(tmp_path):
    """对照组：进程内 LRU 活在各自的内存里，B 进程看不到 A 进程的缓存。"""
    a = await ask_in_new_process(tmp_path / "unused.db", "local", "acme")
    b = await ask_in_new_process(tmp_path / "unused.db", "local", "acme")
    assert (a["model_calls"], b["model_calls"]) == (1, 1) and b["hits"] == 0


async def test_demo_worker_app_shares_answers_between_worker_processes(tmp_path):
    """Demo 场景 1b 的 worker 应用：两个 WorkerPool 各一个真 worker 进程，用 kinds 把任务分给指定进程。"""
    db = tmp_path / "jobs.db"
    queue = SQLiteJobQueue(db)
    await queue.setup()
    store = ck.SQLiteResponseCache(db)
    await store.setup()
    app = f"{HERE / 'cache_app.py'}:make_handler"
    common = dict(concurrency=2, lease=30, poll=0.02, grace=10, options={"cache": "shared", "latency": 0.05},
                  log_dir=tmp_path / "logs")
    pool_a = WorkerPool(f"sqlite:///{db}", app, n=1, kinds=["ask-a"], name="A", **common)
    pool_b = WorkerPool(f"sqlite:///{db}", app, n=1, kinds=["ask-b"], name="B", **common)
    try:
        with pool_a, pool_b:
            ra = await _ask(queue, "ask-a", "acme")
            rb = await _ask(queue, "ask-b", "acme")
            pids = (pool_a.workers[0].pid, pool_b.workers[0].pid)
            logs = pool_a.logs() + pool_b.logs()
        assert (ra["worker"], ra["hit"], ra["model_calls"]) == ("A0", False, 1), logs
        assert (rb["worker"], rb["hit"], rb["model_calls"]) == ("B0", True, 0), logs
        assert (ra["pid"], rb["pid"]) == pids and pids[0] != pids[1]
        writers = [e["writer"] for e in await store.entries()]
        assert writers == [f"A0/pid {pids[0]}"]
    finally:
        await store.close()
        await queue.close()


async def _ask(queue, kind: str, tenant: str, timeout: float = 60) -> dict:
    jid = await queue.enqueue(kind, {"tenant": tenant, "question": "VPN 连不上怎么办？"}, tenant_id=tenant)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await queue.get(jid)
        if job.status == "succeeded":
            return job.result
        assert job.status in ("queued", "leased"), job.last_error
        await asyncio.sleep(0.02)
    raise AssertionError(f"{timeout}s 内任务 {jid} 没有完成")


async def test_sqlite_cache_ttl_lru_and_isolation(tmp_path):
    now = [1000.0]
    db = tmp_path / "c.db"
    # 两个实例各用自己的连接，和两个进程一样只通过文件共享
    a = ck.SQLiteResponseCache(db, ttl_s=10, max_entries=2, clock=lambda: now[0])
    b = ck.SQLiteResponseCache(db, ttl_s=10, max_entries=2, clock=lambda: now[0])
    r = reply("x", input_tokens=7, output_tokens=3)
    await a.put("k1", r)
    now[0] += 1
    await a.put("k2", r)
    now[0] += 1
    assert (await b.get("k1")).usage.input_tokens == 7  # 命中 → k1 变成最近使用
    now[0] += 1
    await b.put("k3", r)  # 超过容量 2：淘汰最久没用的 k2
    assert b.stats.evicted == 1
    assert await a.get("k2") is None and await a.get("k1") is not None
    now[0] += 11  # 过期
    assert await b.get("k1") is None and b.stats.expired == 1
    await a.close()
    await b.close()


# ------------------------------------------------------------------ 并发下的缓存与级联


async def test_concurrent_identical_misses_each_call_the_model_without_single_flight():
    """没有 single-flight：同一个键的 10 个请求同时未命中，模型被调了 10 次（缓存击穿，README 5.2）。"""
    base = ScriptedLLM(responder=lambda m: reply("答"), latency=0.05, model="m")
    store = ck.ResponseCache()
    llms = [ck.CachingLLM(base, store, scope={"tenant_id": "acme"}) for _ in range(10)]
    await asyncio.gather(*(llm.chat(MSGS) for llm in llms))
    assert base.call_count == 10 and base.max_in_flight == 10
    after = ck.CachingLLM(base, store, scope={"tenant_id": "acme"})
    await after.chat(MSGS)
    assert (after.hits, base.call_count) == (1, 10)  # 之后的请求命中


async def test_costkit_cascade_counts_are_exact_under_concurrency():
    small = ck.MeteredLLM(ScriptedLLM(responder=lambda m: reply("ok" if int(m[-1]["content"]) % 4 else "bad",
                                                                input_tokens=10, output_tokens=1),
                                      latency=0.02, model="s"))
    large = ck.MeteredLLM(ScriptedLLM(responder=lambda m: reply("ok", input_tokens=50, output_tokens=5),
                                      latency=0.02, model="l"))
    cascade = ck.CascadeLLM(small, large, lambda m, r: r.content == "ok")
    await asyncio.gather(*(cascade.chat([{"role": "user", "content": str(i)}]) for i in range(40)))
    assert small.max_in_flight > 1 and large.max_in_flight > 1
    assert (cascade.calls, cascade.escalations) == (40, 10)
    assert cascade.small_usage.input_tokens == 400 and cascade.large_usage.input_tokens == 500
    assert cascade.wasted.input_tokens == 100


# ------------------------------------------------------------------ 对冲请求：输家真的被取消


def two_speed_llm(slow: float, fast: float) -> "ck.MeteredLLM":
    """第 1 次调用 slow 秒，之后每次 fast 秒。"""
    return ck.MeteredLLM(ScriptedLLM(responder=lambda m: reply("ok", input_tokens=100, output_tokens=10),
                                     latency=lambda n: slow if n == 1 else fast, model="m"))


async def test_hedge_cancels_the_losing_request():
    llm = two_speed_llm(slow=5.0, fast=0.01)
    t0 = time.monotonic()
    out = await ck.hedged_call(lambda: llm.chat(MSGS), hedge_after_s=0.05)
    assert (out.winner, out.launched, out.cancelled) == (1, 2, 1)
    assert (llm.started, llm.completed, llm.cancelled) == (2, 1, 1), "输家收到了 CancelledError，没有完成"
    assert llm.in_flight == 0, "返回时没有任何请求还在后台跑"
    assert llm.billed.input_tokens == 100, "只有胜者产生了 usage"
    assert time.monotonic() - t0 < 4.0  # 宽松上限：没有等那个 5 秒的慢请求


async def test_fast_request_is_not_hedged():
    llm = two_speed_llm(slow=0.01, fast=0.01)
    out = await ck.hedged_call(lambda: llm.chat(MSGS), hedge_after_s=1.0)
    assert (out.winner, out.launched, out.cancelled, llm.started) == (0, 1, 0, 1)


async def test_cancelling_the_caller_cancels_every_in_flight_request():
    llm = two_speed_llm(slow=30.0, fast=30.0)
    task = asyncio.create_task(ck.hedged_call(lambda: llm.chat(MSGS), hedge_after_s=0.01))
    while llm.in_flight < 2:  # 等到原始请求和对冲请求都在飞
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (llm.started, llm.cancelled, llm.completed, llm.in_flight) == (2, 2, 0, 0)


async def test_all_failed_retryable_relaunches_immediately_then_gives_up_on_permanent_errors():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise LLMError("503", retryable=True)
        return "ok"

    out = await ck.hedged_call(flaky, hedge_after_s=10.0, max_requests=2)
    assert (out.value, out.winner, out.launched) == ("ok", 1, 2)  # 没有干等 10 秒，失败后立刻补发

    async def broken():
        raise LLMError("401 unauthorized", retryable=False)

    with pytest.raises(LLMError, match="401"):
        await ck.hedged_call(broken, hedge_after_s=10.0, max_requests=3)
