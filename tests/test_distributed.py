"""agentkit.distributed 的测试。

前半部分在一个进程里验证队列 / 检查点的语义（fence、CAS、重试、死信）；
后半部分用 WorkerPool 拉起**真正的操作系统进程**，用真实的信号做故障注入：
kill -9 之后别的进程接手且副作用不重复、SIGSTOP 冻结的"僵尸"醒来后写不进去、SIGTERM 优雅停机、
多进程抢任务不重复领取、跨进程的并发槽位和令牌桶真的被所有进程共同遵守。
断言的都是确定性的量（次数、fence、谁领了什么）；时间只用作宽松的等待上限（8GB 机器负载高）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from agentkit import Agent, ScriptedLLM, call_tool, reply, tool
from agentkit.distributed import (
    AgentJobHandler,
    CheckpointConflict,
    LeaseLost,
    SQLiteCheckpointer,
    SQLiteDB,
    SQLiteIdempotencyStore,
    SQLiteJobQueue,
    SQLiteSemaphore,
    SQLiteTokenBucket,
    WorkerPool,
    run_worker,
)
from agentkit.limits import LimitExceeded
from agentkit.state import RunState

APPS = str(Path(__file__).with_name("worker_apps.py"))


def rows(path, sql: str, params=()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:  # 表还没被 worker 建出来
        if "no such table" in str(e):
            return []
        raise
    finally:
        conn.close()


async def eventually(cond, timeout: float = 30.0, interval: float = 0.05, what: str = "条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"{timeout}s 内没有等到{what}")


@pytest.fixture
async def queue(tmp_path):
    q = SQLiteJobQueue(tmp_path / "jobs.db", base_backoff=0.01, max_backoff=0.02)
    await q.setup()
    yield q
    await q.close()


# ------------------------------------------------------------------ 队列语义（单进程）


async def test_enqueue_is_idempotent_per_tenant(queue):
    a = await queue.enqueue("run", {"input": "x"}, tenant_id="acme", idempotency_key="req-1")
    b = await queue.enqueue("run", {"input": "x"}, tenant_id="acme", idempotency_key="req-1")
    c = await queue.enqueue("run", {"input": "x"}, tenant_id="globex", idempotency_key="req-1")
    assert a == b and c != a  # 同一租户重复提交 → 同一个任务；不同租户互不影响


async def test_claim_order_and_global_fence(queue):
    low = await queue.enqueue("run", {}, tenant_id="t")
    high = await queue.enqueue("run", {}, tenant_id="t", priority=5)
    j1 = await queue.claim("w1", lease_seconds=10)
    j2 = await queue.claim("w2", lease_seconds=10)
    assert (j1.id, j2.id) == (high, low)  # 优先级高的先领
    assert j2.fence > j1.fence  # fence 全局递增，不是每个任务从 1 数起
    assert await queue.claim("w3") is None


async def test_stale_fence_is_rejected_after_takeover(queue):
    await queue.enqueue("run", {}, tenant_id="t")
    old = await queue.claim("zombie", lease_seconds=0.05)
    await asyncio.sleep(0.1)  # 租约过期
    new = await eventually(lambda: queue.claim("fresh", lease_seconds=10), what="重新领取")
    assert new.id == old.id and new.fence > old.fence and new.attempts == 2
    for action in (queue.heartbeat(old), queue.complete(old, "僵尸的结果"), queue.fail(old, "x")):
        with pytest.raises(LeaseLost, match="已被重新领取"):
            await action
    await queue.complete(new, "新持有者的结果")
    assert (await queue.get(new.id)).result == "新持有者的结果"


async def test_retry_backoff_then_dead_letter(queue):
    jid = await queue.enqueue("run", {}, tenant_id="t", max_attempts=2)
    job = await queue.claim("w")
    assert await queue.fail(job, "boom") == "queued"
    job = await eventually(lambda: queue.claim("w"), what="退避结束后重新领取")
    assert await queue.fail(job, "boom again") == "dead"
    stats = await queue.stats()
    assert stats["counts"]["dead"] == 1
    assert await queue.redrive(jid)
    assert (await queue.claim("w")).attempts == 1  # redrive 重置次数，但 fence 不回退


async def test_release_does_not_consume_attempt(queue):
    await queue.enqueue("run", {}, tenant_id="t", max_attempts=1)
    job = await queue.claim("w")
    await queue.release(job, reason="限流，稍后再来")
    again = await queue.claim("w")
    assert again.attempts == 1 and again.fence > job.fence


async def test_expired_lease_with_no_attempts_left_goes_dead(queue):
    await queue.enqueue("poison", {}, tenant_id="t", max_attempts=1)
    await queue.claim("w", lease_seconds=0.05)  # 领了就"崩溃"（不续约、不提交）
    await asyncio.sleep(0.1)
    assert await queue.claim("w") is None  # 毒消息不会再发出去害人
    assert (await queue.stats())["counts"]["dead"] == 1


# ------------------------------------------------------------------ 检查点（fence + CAS）


async def test_checkpoint_fence_takeover_rejects_zombie(tmp_path):
    base = SQLiteCheckpointer(tmp_path / "c.db")
    await base.setup()
    zombie, fresh = base.fenced(5, "zombie"), base.fenced(6, "fresh")
    state = RunState(run_id="r1", metadata={"tenant_id": "acme"})
    await zombie.save(state)
    assert (await zombie.load("r1")) is not None
    assert (await fresh.load("r1")) is not None  # 接管：fence 6 ≥ 5，version + 1
    with pytest.raises(CheckpointConflict):
        await zombie.save(state)  # 僵尸手里的版本号作废了
    with pytest.raises(CheckpointConflict, match="旧持有者"):
        await base.fenced(4, "older").load("r1")
    await fresh.save(state)
    assert (await base.get_run("r1"))["writer"] == "fresh"
    await base.close()


async def test_agent_job_handler_runs_and_resumes_after_approval(tmp_path):
    """同一进程里跑一遍完整流程：run 任务 → 暂停等审批 → resume 任务带着审批结果继续。"""
    from agentkit import PermissionPolicy

    db = SQLiteDB(tmp_path / "a.db")
    q, ckpt = SQLiteJobQueue(db), SQLiteCheckpointer(db)
    await q.setup()
    await ckpt.setup()

    @tool(risk="dangerous")
    def wire(amount: int) -> str:
        """转账"""
        return f"已转 {amount}"

    def respond(messages):
        return reply("完成") if messages[-1]["role"] == "tool" else call_tool("wire", amount=100)

    agent = Agent(ScriptedLLM(responder=respond), [wire], hooks=[PermissionPolicy()], checkpointer=ckpt)
    handler = AgentJobHandler(agent, ckpt)
    await q.enqueue("run", {"op": "run", "input": "转账"}, tenant_id="acme")
    stop = asyncio.Event()
    stats = await run_worker(q, handler, worker_id="w", stop_event=stop, poll_interval=0.01, max_jobs=1)
    first = (await q.list_jobs())[0]
    assert stats["succeeded"] == 1 and first.result["awaiting_approval"]
    call_id = first.result["pending"]["id"]
    await q.enqueue("resume", {"op": "resume", "run_id": first.result["run_id"], "approvals": {call_id: True},
                               "by": "经理"}, tenant_id="acme")
    await run_worker(q, handler, worker_id="w", stop_event=stop, poll_interval=0.01, max_jobs=1)
    second = (await q.list_jobs())[1]
    assert second.result["status"] == "completed" and second.result["output"] == "完成"
    run = await ckpt.get_run(first.result["run_id"])
    assert run["state"]["approval_log"][0]["by"] == "经理"
    # 别的租户不能操作这个 run
    await q.enqueue("resume", {"op": "resume", "run_id": first.result["run_id"]}, tenant_id="globex")
    await run_worker(q, handler, worker_id="w", stop_event=stop, poll_interval=0.01, max_jobs=1)
    third = (await q.list_jobs())[2]
    assert third.status == "failed" and "不能由租户 globex" in third.last_error
    await db.close()


async def test_idempotency_store_is_shared_and_first_result_wins(tmp_path):
    from agentkit import ToolResult

    a, b = SQLiteIdempotencyStore(tmp_path / "i.db"), SQLiteIdempotencyStore(tmp_path / "i.db")
    await a.setup()
    await a.put("run:call", ToolResult(True, "T-1"))
    await b.put("run:call", ToolResult(True, "T-2"))
    assert (await b.get("run:call")).content == "T-1"
    await a.close()
    await b.close()


async def test_semaphore_times_out_when_full(tmp_path):
    sem = SQLiteSemaphore(tmp_path / "s.db", "gw", limit=1)
    await sem.setup()
    async with sem.slot():
        assert await sem.in_use() == 1
        with pytest.raises(LimitExceeded):
            async with sem.slot(timeout=0.1):
                pass
    assert await sem.in_use() == 0
    await sem.close()


# ------------------------------------------------------------------ 真实的多进程


async def test_many_processes_never_claim_the_same_job(tmp_path):
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    for i in range(60):
        await q.enqueue("rec", {"i": i}, tenant_id="t")
    with WorkerPool(f"sqlite:///{db}", f"{APPS}:make_recording_app", n=4, concurrency=4, poll=0.02,
                    options={"hold": 0.02}, log_dir=tmp_path / "logs") as pool:
        await eventually(lambda: len(rows(db, "SELECT * FROM job_log")) == 60, what="60 个任务全部完成")
        await eventually(lambda: _all_succeeded(q, 60), what="全部提交")
        log = rows(db, "SELECT job_id, pid FROM job_log")
        claimed = pool.events("claimed")
    assert sorted(r["job_id"] for r in log) == list(range(1, 61))  # 每个任务恰好处理一次
    assert len({r["pid"] for r in log}) >= 2  # 真的是多个进程在分活
    assert len({e["job"] for e in claimed}) == len(claimed) == 60  # 没有任何任务被领取两次
    assert (await q.stats())["counts"]["succeeded"] == 60
    await q.close()


async def test_kill_9_worker_is_taken_over_without_duplicate_side_effects(tmp_path):
    """worker 在"工具已执行、第二次模型调用进行中"时被 kill -9：租约过期后别的进程接手，
    从检查点继续（不重新调用工具），工单只建了一张。"""
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    jid = await q.enqueue("run", {"op": "run", "input": "打印机坏了"}, tenant_id="acme")
    with WorkerPool(f"sqlite:///{db}", f"{APPS}:make_agent_app", n=2, concurrency=2, lease=1.0, poll=0.05,
                    options={"second_latency": 3.0}, log_dir=tmp_path / "logs") as pool:
        await eventually(lambda: rows(db, "SELECT * FROM ticket_calls"), what="工具被执行")
        holder = (await q.get(jid)).worker_id
        victim = next(i for i, w in enumerate(pool.workers) if w.worker_id == holder)
        pool.kill(victim)
        assert not pool.workers[victim].alive
        job = await eventually(lambda: _succeeded(q, jid), timeout=40, what="接手者完成任务")
        claims = [e for e in pool.events("claimed") if e["job"] == jid]
        logs = pool.logs()
    assert job.worker_id != holder and job.attempts == 2, logs
    assert [c["worker_id"] for c in claims] == [holder, job.worker_id]
    assert claims[1]["fence"] > claims[0]["fence"]
    calls = rows(db, "SELECT * FROM ticket_calls")
    assert len(calls) == 1, "接手者从检查点继续，工具不会被再执行一次"
    assert job.result["status"] == "completed" and job.result["output"].startswith("已建单")


async def _all_succeeded(q, n):
    return (await q.stats())["counts"]["succeeded"] == n


async def _succeeded(q, jid):
    job = await q.get(jid)
    return job if job and job.status == "succeeded" else None


async def test_paused_zombie_cannot_overwrite_after_waking_up(tmp_path):
    """SIGSTOP 冻结持有任务的进程（模拟长 GC / 虚拟机挂起）：它的租约过期，别人接手并完成；
    解冻后它还以为自己持有任务，但心跳、检查点写入、提交全部被 fence 拒绝。"""
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    jid = await q.enqueue("run", {"op": "run", "input": "重置密码"}, tenant_id="acme")
    with WorkerPool(f"sqlite:///{db}", f"{APPS}:make_agent_app", n=2, concurrency=2, lease=1.0, poll=0.05,
                    options={"second_latency": 2.0}, log_dir=tmp_path / "logs") as pool:
        await eventually(lambda: rows(db, "SELECT * FROM ticket_calls"), what="工具被执行")
        holder = (await q.get(jid)).worker_id
        zombie = next(i for i, w in enumerate(pool.workers) if w.worker_id == holder)
        pool.pause(zombie)
        job = await eventually(lambda: _succeeded(q, jid), timeout=40, what="另一个进程接手并完成")
        pool.resume(zombie)
        rejected = await eventually(
            lambda: [e for e in pool.events() if e["worker_id"] == holder and e["event"] in
                     ("heartbeat_rejected", "ownership_lost", "fence_rejected")],
            what="僵尸醒来后被拒绝", timeout=20,
        )
        assert pool.workers[zombie].alive  # 它没崩溃，只是被拒绝了，接着领别的任务
        logs = pool.logs()
    assert job.worker_id != holder, logs
    assert rejected
    assert not [e for e in pool.events("completed") if e["worker_id"] == holder]
    final = await q.get(jid)
    assert final.worker_id == job.worker_id and final.result == job.result  # 结果没有被僵尸覆盖
    assert len(rows(db, "SELECT * FROM ticket_calls")) == 1


async def test_sigterm_drains_in_flight_job_then_exits(tmp_path):
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    jid = await q.enqueue("run", {"op": "run", "input": "开通 VPN"}, tenant_id="acme")
    pool = WorkerPool(f"sqlite:///{db}", f"{APPS}:make_agent_app", n=1, lease=5, poll=0.05, grace=15,
                      options={"second_latency": 1.5}, log_dir=tmp_path / "logs").start()
    try:
        await eventually(lambda: pool.events("claimed"), what="任务被领取")
        pool.terminate(0)
        code = await asyncio.to_thread(pool.workers[0].wait, 30)
        assert code == 0, pool.logs()
        job = await q.get(jid)
        assert job.status == "succeeded"  # 在途任务做完才退出，不需要别人接手
        names = [e["event"] for e in pool.events()]
        assert names.index("draining") < names.index("completed") < names.index("stopped")
    finally:
        pool.stop()


async def test_semaphore_limits_concurrency_across_processes(tmp_path):
    """4 个进程 × 每个 4 个并发 = 最多 16 个同时在跑；跨进程槽位 limit=3 把它压到 3。"""
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    for i in range(24):
        await q.enqueue("rec", {"i": i}, tenant_id="t")
    with WorkerPool(f"sqlite:///{db}", f"{APPS}:make_recording_app", n=4, concurrency=4, poll=0.02,
                    options={"mode": "semaphore", "limit": 3, "hold": 0.15}, log_dir=tmp_path / "logs"):
        await eventually(lambda: len(rows(db, "SELECT * FROM job_log")) == 24, timeout=60, what="全部完成")
    log = rows(db, "SELECT start, end, pid FROM job_log")
    points = sorted([(r["start"], 1) for r in log] + [(r["end"], -1) for r in log])
    level = peak = 0
    for _, d in points:
        level += d
        peak = max(peak, level)
    assert peak <= 3
    assert peak >= 2 and len({r["pid"] for r in log}) >= 2  # 确实发生了跨进程并发


async def test_killed_holder_releases_semaphore_slot_after_lease(tmp_path):
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    await q.enqueue("rec", {}, tenant_id="t")
    pool = WorkerPool(f"sqlite:///{db}", f"{APPS}:make_recording_app", n=1, lease=30, poll=0.02,
                      options={"mode": "semaphore", "limit": 1, "hold": 30, "slot_lease": 1.0}, log_dir=tmp_path / "logs").start()
    sem = SQLiteSemaphore(db, "gateway", limit=1)
    await sem.setup()
    try:
        await eventually(lambda: sem.in_use(), what="槽位被占用")
        pool.kill(0)
        async with sem.slot(timeout=10):  # 持有者死了，租约到期后槽位自动释放，不会永久丢一个名额
            pass
    finally:
        pool.stop()
        await sem.close()


async def test_token_bucket_is_shared_by_all_processes(tmp_path):
    """3 个进程共用一个桶：总处理数不超过 capacity + rate × 时间（进程内的桶会让速率变成 3 倍）。"""
    db = str(tmp_path / "jobs.db")
    q = SQLiteJobQueue(db)
    await q.setup()
    for i in range(200):
        await q.enqueue("rec", {"i": i}, tenant_id="t")
    rate, capacity = 20.0, 5.0
    with WorkerPool(f"sqlite:///{db}", f"{APPS}:make_recording_app", n=3, concurrency=8, poll=0.02,
                    options={"mode": "bucket", "rate": rate, "capacity": capacity, "hold": 0.0},
                    log_dir=tmp_path / "logs"):
        await eventually(lambda: len(rows(db, "SELECT * FROM job_log")) >= 40, timeout=60, what="处理了一批")
    log = rows(db, "SELECT start, pid FROM job_log ORDER BY start")
    span = log[-1]["start"] - log[0]["start"]
    assert len(log) <= capacity + rate * span + 2  # +2：边界上的取整余量
    assert len({r["pid"] for r in log}) >= 2


# ------------------------------------------------------------------ 跨进程熔断器


async def test_breaker_opened_by_another_process_is_seen_here(tmp_path):
    """另一个真实的进程把下游打到熔断；本进程第一次调用就快速失败，而不是自己再失败 5 次。"""
    import sys

    from agentkit import LLMError, ResilientLLM
    from agentkit.distributed import SQLiteCircuitBreaker
    from agentkit.reliability import CircuitOpenError

    db = str(tmp_path / "b.db")
    script = (
        "import asyncio\n"
        "from agentkit import LLMError\n"
        "from agentkit.distributed import SQLiteCircuitBreaker\n"
        "async def main():\n"
        f"    b = SQLiteCircuitBreaker({db!r}, 'gpt', failure_threshold=3, reset_timeout=60)\n"
        "    async def boom():\n"
        "        raise LLMError('503', retryable=True)\n"
        "    for _ in range(3):\n"
        "        try:\n"
        "            await b.call(boom)\n"
        "        except LLMError:\n"
        "            pass\n"
        "    print(await b.current_state())\n"
        "    await b.close()\n"
        "asyncio.run(main())\n"
    )
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    assert proc.returncode == 0 and out.decode().strip().endswith("open"), out.decode()

    here = SQLiteCircuitBreaker(db, "gpt", failure_threshold=3, reset_timeout=60)
    assert await here.current_state() == "open"
    primary = ScriptedLLM([reply("不该被调用")], model="gpt")
    backup = ScriptedLLM([reply("备用模型的回答")], model="backup")
    llm = ResilientLLM(primary, [backup], breaker_factory=lambda m: SQLiteCircuitBreaker(db, m, failure_threshold=3,
                                                                                           reset_timeout=60))
    res = await Agent(llm, []).run("hi")
    assert res.output == "备用模型的回答" and primary.call_count == 0  # 主模型一次都没被打
    with pytest.raises(CircuitOpenError):
        await here.call(lambda: None)
    await here.close()


async def test_shared_breaker_lets_only_one_probe_through(tmp_path):
    from agentkit import LLMError
    from agentkit.distributed import SQLiteCircuitBreaker
    from agentkit.reliability import CircuitOpenError

    now = [0.0]
    db = str(tmp_path / "b.db")
    # 两个实例各用自己的连接（和两个进程一样只通过文件共享状态）
    a = SQLiteCircuitBreaker(db, "m", failure_threshold=1, reset_timeout=10, clock=lambda: now[0])
    b = SQLiteCircuitBreaker(db, "m", failure_threshold=1, reset_timeout=10, clock=lambda: now[0])

    async def boom():
        raise LLMError("503", retryable=True)

    with pytest.raises(LLMError):
        await a.call(boom)
    assert await b.current_state() == "open"
    now[0] = 11  # 半开
    release = asyncio.Event()

    async def slow_ok():
        await release.wait()
        return "ok"

    probe = asyncio.create_task(a.call(slow_ok))
    await eventually(lambda: _probing(db), what="试探租约被占用")
    with pytest.raises(CircuitOpenError):
        await b.call(slow_ok)  # 另一个"进程"在试探期间快速失败
    release.set()
    assert await probe == "ok"
    assert await b.current_state() == "closed"
    await a.close()
    await b.close()


def _probing(db) -> bool:
    return bool(rows(db, "SELECT 1 FROM circuit_breakers WHERE probe_until IS NOT NULL"))


# ------------------------------------------------------------------ 回归（第 12/13 课在真实部署中发现）


async def test_stop_signal_is_seen_even_when_every_slot_is_busy(tmp_path):
    """满载时主循环停在"等名额"上；停机信号必须能叫醒它，grace_period 才生效。"""
    q = SQLiteJobQueue(tmp_path / "jobs.db")
    await q.setup()
    await q.enqueue("slow", {}, tenant_id="t")
    started = asyncio.Event()

    async def slow(job):
        started.set()
        await asyncio.sleep(30)

    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(q, slow, worker_id="w", stop_event=stop, concurrency=1,
                                            poll_interval=0.01, grace_period=0.3))
    await asyncio.wait_for(started.wait(), 10)
    t0 = time.monotonic()
    stop.set()
    stats = await asyncio.wait_for(worker, 10)
    assert stats["cancelled"] == 1 and time.monotonic() - t0 < 5  # 以前要等 30 秒的任务自己跑完
    assert (await q.get(1)).status == "leased"  # 被取消的任务不提交、不归还，租约过期后别人接手
    await q.close()


async def test_many_processes_can_create_the_same_new_database_at_once(tmp_path):
    """几个进程同时新建同一个库（同时切换 WAL）时不能报 database is locked。"""
    import sys

    script = (
        "import asyncio, os, sys, time\n"
        "from agentkit.distributed import SQLiteCheckpointer, SQLiteJobQueue\n"
        "gate, db = sys.argv[1], sys.argv[2]\n"
        "while not os.path.exists(gate):\n"
        "    time.sleep(0.001)\n"
        "async def main():\n"
        "    for obj in (SQLiteCheckpointer(db), SQLiteJobQueue(db)):\n"
        "        await obj.setup()\n"
        "        await obj.close()\n"
        "asyncio.run(main())\n"
    )
    for round_ in range(4):
        gate, db = tmp_path / f"gate{round_}", tmp_path / f"fresh{round_}.db"
        procs = [await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(gate), str(db),
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                 for _ in range(6)]
        await asyncio.sleep(0.5)  # 让 6 个进程都停在起跑线上
        gate.touch()
        outs = [await p.communicate() for p in procs]
        assert all(p.returncode == 0 for p in procs), [o[0].decode()[-300:] for o in outs]


async def test_release_on_cancel_hands_the_job_back_immediately(tmp_path):
    """停机时被取消的任务：默认等租约过期；release_on_cancel=True 时立刻归还，别人马上能领（不消耗尝试次数）。"""
    q = SQLiteJobQueue(tmp_path / "jobs.db")
    await q.setup()
    jid = await q.enqueue("slow", {}, tenant_id="t")
    started = asyncio.Event()

    async def slow(job):
        started.set()
        await asyncio.sleep(30)

    stop = asyncio.Event()
    events = []
    worker = asyncio.create_task(run_worker(q, slow, worker_id="w", stop_event=stop, concurrency=1, lease_seconds=60,
                                            poll_interval=0.01, grace_period=0.2, release_on_cancel=True,
                                            on_event=lambda n, i: events.append(n)))
    await asyncio.wait_for(started.wait(), 10)
    stop.set()
    await asyncio.wait_for(worker, 10)
    job = await q.get(jid)
    assert job.status == "queued" and "released_on_cancel" in events  # 租约 60 秒，但不用等
    again = await q.claim("w2", lease_seconds=5)
    assert again.id == jid and again.attempts == 1 and again.fence > job.fence
    await q.close()
