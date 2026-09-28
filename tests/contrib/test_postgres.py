"""agentkit.contrib.postgres 的测试：真实 Postgres（嵌入式 pgserver）+ 真实的操作系统进程。

前半部分在一个进程里验证语义（asyncio 协程之间的并发足以制造真实的数据库竞争）：
检查点的 CAS / fence 接管 / 并发写、NUL 清洗；队列的去重、SKIP LOCKED、租约回收、fence 拒绝僵尸、
退避 / 死信 / redrive、全局 fence 序列；run_worker 的背压、连接池大小、优雅停机与取消；AgentJobHandler 的审批 → resume。

后半部分用 WorkerPool 拉起**真正的 worker 进程**（python -m agentkit.distributed.worker --queue postgresql://...），
用真实的信号和真实的网络故障做注入：多进程抢任务不重复领取、心跳保住长任务、kill -9 之后从检查点接手且副作用只发生一次、
SIGSTOP 冻结的僵尸醒来后写不进去、SIGTERM 取消了进行中的写操作后由下一个进程用同一个幂等键重放、
以及网络分区（TcpProxy 断开一个 worker 和数据库之间的 TCP 连接）。
断言的都是确定性的量（次数、fence、谁完成了、副作用执行了几次）；时间只用作宽松的等待上限（8GB 机器负载高）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from psycopg.conninfo import conninfo_to_dict  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from agentkit import Agent, PermissionPolicy, RunState, ScriptedLLM, call_tool, reply, tool, wait_for  # noqa: E402
from agentkit.contrib.postgres import (  # noqa: E402
    AgentJobHandler,
    CheckpointConflict,
    LeaseLost,
    PermanentJobError,
    PostgresCheckpointer,
    PostgresJobQueue,
    RetryLater,
    run_worker,
)
from agentkit.distributed import LeaseGuard, TcpProxy, WorkerPool  # noqa: E402

APPS = str(Path(__file__).with_name("pg_worker_apps.py"))
AGENT_APP = f"{APPS}:make_agent_app"
RECORDING_APP = f"{APPS}:make_recording_app"
REPO_ROOT = Path(__file__).resolve().parents[2]
LEASE = 2.0  # 真进程测试的租约：心跳每 1/3 租约续一次；被杀 / 被冻结 / 断网的持有者最多 2 秒后被接手


# ============================================================================= 辅助


async def sql(uri: str, query: str, params=None) -> list[tuple]:
    """在一个独立连接上执行一条 SQL（测试自己的"运维操作"：让租约过期、查记录表……）。"""
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cur = await conn.execute(query, params)
        return (await cur.fetchall()) if cur.description else []


async def expire_lease(uri: str, job_id: int | None = None) -> None:
    """把租约改成"已经过期"：确定性地模拟 worker 卡住 / 崩溃，不用真的等（真进程版本见后半部分）。"""
    if job_id is None:
        await sql(uri, "UPDATE agent_jobs SET lease_until = now() - interval '1 second' WHERE status = 'leased'")
    else:
        await sql(uri, "UPDATE agent_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))


async def _count(uri: str, query: str, params, expected: int) -> bool:
    return (await sql(uri, query, params))[0][0] == expected


async def succeeded(q: PostgresJobQueue, jid: int):
    job = await q.get(jid)
    return job if job is not None and job.status == "succeeded" else None


def state_with(run_id: str, **meta) -> RunState:
    s = RunState(run_id=run_id, metadata=dict(meta))
    s.messages = [{"role": "user", "content": "你好"}]
    return s


async def eventually(cond, timeout: float = 60.0, interval: float = 0.05, what: str = "条件"):
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
async def ckpt(pg_uri):
    c = PostgresCheckpointer(pg_uri)
    await c.setup()
    yield c
    await c.close()


@pytest.fixture
async def queue(pg_uri):
    q = PostgresJobQueue(pg_uri, base_backoff=0.0)
    await q.setup()
    yield q
    await q.close()


@pytest.fixture(scope="module")
def pg_tcp_server():
    """一个监听 TCP（127.0.0.1）的真实 Postgres。

    会话级的嵌入式 Postgres 只监听 unix socket：pgserver 用命令行参数 -h "" 启动它，
    命令行参数的优先级高于 postgresql.conf 和 ALTER SYSTEM，改配置也打不开 TCP。
    所以这里用 pgserver 自带的同一套 Postgres 程序（initdb / pg_ctl）另起一个实例，只监听 127.0.0.1。"""
    pgserver = pytest.importorskip("pgserver")
    root = Path(tempfile.mkdtemp(prefix="agentkit_pgtcp_"))
    data = root / "data"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pgserver.initdb(["--auth=trust", "--encoding=utf8", "-U", "postgres"], pgdata=data)
    pgserver.pg_ctl(["-w", "-o", f'-h 127.0.0.1 -p {port} -k ""', "-l", str(root / "log"), "start"], pgdata=data, timeout=60)
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        pgserver.pg_ctl(["-w", "-m", "immediate", "stop"], pgdata=data, timeout=60)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def pg_tcp_uri(pg_tcp_server):
    from agentkit.testing import create_database

    return create_database(pg_tcp_server)


# ============================================================================= 检查点


async def test_checkpoint_roundtrip_version_numbers_and_nul_scrubbing(ckpt, pg_uri):
    s = state_with("r1", tenant_id="acme", user_id="u1")
    s.messages.append({"role": "tool", "tool_call_id": "c1", "content": "二进制\x00垃圾"})
    await ckpt.save(s)
    assert ckpt.version_of("r1") == 1
    s.step = 3
    await ckpt.save(s)
    assert ckpt.version_of("r1") == 2

    other = PostgresCheckpointer(pg_uri)  # 另一个实例（另一个 worker 的连接池）
    loaded = await other.load("r1")
    assert loaded.step == 3 and loaded.metadata == {"tenant_id": "acme", "user_id": "u1"}
    assert loaded.messages[1]["content"] == "二进制�垃圾"  # NUL 存不进 jsonb，被替换而不是整个写入失败
    assert other.version_of("r1") == 2
    assert await other.load("nope") is None
    row = await other.get_run("r1")
    assert (row["status"], row["tenant_id"], row["version"], row["user_id"]) == ("running", "acme", 2, "u1")
    await other.close()


async def test_stale_writer_gets_conflict_and_cannot_overwrite(ckpt, pg_uri):
    await ckpt.save(state_with("r1", tenant_id="acme"))
    a, b = PostgresCheckpointer(pg_uri, writer="A"), PostgresCheckpointer(pg_uri, writer="B")
    sa, sb = await a.load("r1"), await b.load("r1")  # 两个 worker 读到同一个版本
    sa.output = "A 的结果"
    await a.save(sa)
    sb.output = "B 基于旧版本的结果"
    with pytest.raises(CheckpointConflict) as e:
        await b.save(sb)
    assert (e.value.expected, e.value.actual, e.value.writer) == (1, 2, "A")
    assert (await ckpt.load("r1")).output == "A 的结果"  # 没有被覆盖
    await a.close()
    await b.close()


async def test_creating_a_run_that_already_exists_is_a_conflict(ckpt, pg_uri):
    await ckpt.save(state_with("r1"))
    other = PostgresCheckpointer(pg_uri)
    with pytest.raises(CheckpointConflict, match="已经存在"):
        await other.save(state_with("r1"))  # 没 load 过就当成新 run 去写：说明别人已经创建了它
    await other.close()


async def test_plain_cas_is_first_writer_wins_but_fenced_takeover_makes_newest_holder_win(ckpt, pg_uri):
    # 纯 CAS：僵尸 Z 和新 worker N 读到同一版本，僵尸先写 → 僵尸赢，新 worker 冲突（没有丢更新，但赢家不对）
    await ckpt.save(state_with("plain"))
    z, n = PostgresCheckpointer(pg_uri), PostgresCheckpointer(pg_uri)
    zs, ns = await z.load("plain"), await n.load("plain")
    await z.save(zs)
    with pytest.raises(CheckpointConflict):
        await n.save(ns)
    await z.close()
    await n.close()

    # 带 fence：新持有者 load 的那一刻就"接管"（version + 1），僵尸之后的写入全部冲突
    old = ckpt.fenced(1, writer="worker-A")
    await old.save(state_with("fenced"))
    old_state = await old.load("fenced")
    new = ckpt.fenced(2, writer="worker-B")
    new_state = await new.load("fenced")
    with pytest.raises(CheckpointConflict):
        await old.save(old_state)  # 僵尸醒来继续写
    await new.save(new_state)  # 新持有者照常写
    with pytest.raises(CheckpointConflict, match="fence"):
        await ckpt.fenced(1).load("fenced")  # 旧 fence 连读（接管）都被拒绝
    assert (await ckpt.get_run("fenced"))["writer"] == "worker-B"


async def test_concurrent_writers_never_lose_an_update(ckpt, pg_uri):
    """8 个写者（各自的连接池 = 各自的数据库连接）同时对一个计数器做"读-改-写"，每人 10 次。"""
    s = state_with("counter")
    s.metadata["n"] = 0
    await ckpt.save(s)
    writers = [PostgresCheckpointer(pg_uri, writer=f"w{i}", pool_kwargs={"min_size": 1, "max_size": 1}) for i in range(8)]
    for w in writers:
        await w.load("counter")  # 先把连接建好：发令之后没有谁因为握手慢而"错峰"
    go = asyncio.Event()
    conflicts = []

    async def writer(i: int, mine: PostgresCheckpointer) -> None:
        await go.wait()
        for _ in range(10):
            while True:  # CAS 重试循环：每次都重新读最新版本
                st = await mine.load("counter")
                st.metadata["n"] += 1
                try:
                    await mine.save(st)
                    break
                except CheckpointConflict:
                    conflicts.append(i)

    tasks = [asyncio.create_task(writer(i, w)) for i, w in enumerate(writers)]
    go.set()
    await asyncio.gather(*tasks)
    fresh = PostgresCheckpointer(pg_uri)
    assert (await fresh.load("counter")).metadata["n"] == 80  # 80 次自增一次不丢
    assert fresh.version_of("counter") == 81
    assert conflicts, "8 个写者同时读到同一个版本，必然有人冲突后重试"
    for c in [*writers, fresh]:
        await c.close()


async def test_list_runs_filters_by_status_and_tenant(ckpt):
    for i, (tenant, status) in enumerate([("acme", "paused"), ("acme", "completed"), ("globex", "paused")]):
        s = state_with(f"r{i}", tenant_id=tenant)
        s.status = status
        if status == "paused":
            s.pending = {"id": f"call_{i}", "name": "reset_password", "arguments": "{}"}
        await ckpt.save(s)
    inbox = await ckpt.list_runs(status="paused", tenant_id="acme")
    assert [r["run_id"] for r in inbox] == ["r0"]
    assert inbox[0]["pending"]["name"] == "reset_password"
    assert {r["run_id"] for r in await ckpt.list_runs(status="paused")} == {"r0", "r2"}
    assert len(await ckpt.list_runs(limit=2)) == 2


async def test_works_with_any_pool_that_has_an_async_connection_method(pg_uri):
    class TinyPool:  # psycopg_pool.AsyncConnectionPool 的最小替身：借出连接，退出时提交 / 回滚
        def __init__(self):
            self.borrowed = 0

        @asynccontextmanager
        async def connection(self):
            self.borrowed += 1
            async with await psycopg.AsyncConnection.connect(pg_uri) as conn:  # 非 autocommit，和池里的连接一样
                yield conn

    pool = TinyPool()
    c = PostgresCheckpointer(pool)
    await c.setup()
    await c.save(state_with("p1"))
    other = PostgresCheckpointer(pg_uri)
    assert (await other.load("p1")).run_id == "p1"
    assert pool.borrowed >= 2
    await c.close()  # 不是自己建的池：close 不去关它
    await other.close()
    with pytest.raises(TypeError, match="AsyncConnectionPool"):
        PostgresCheckpointer(object())


async def test_agent_paused_by_one_instance_is_approved_by_another(ckpt, pg_uri):
    executed = []

    @tool(risk="dangerous")
    def reset_password(user: str) -> str:
        """重置用户密码"""
        executed.append(user)
        return f"{user} 的密码已重置"

    def make(checkpointer, script):
        return Agent(ScriptedLLM(script), [reset_password], checkpointer=checkpointer, hooks=[PermissionPolicy()])

    c1, c2 = PostgresCheckpointer(pg_uri), PostgresCheckpointer(pg_uri)  # 两个互不共享内存的实例，只共享数据库
    first = make(c1, [call_tool("reset_password", user="bob")])
    r = await first.run("帮 bob 重置密码", run_id="r-approve", metadata={"tenant_id": "acme"})
    assert r.status == "paused" and executed == []

    second = make(c2, [reply("已重置")])
    r2 = await second.approve("r-approve", True, by="alice")
    assert r2.status == "completed" and executed == ["bob"]
    assert (await ckpt.get_run("r-approve"))["state"]["approval_log"][0]["by"] == "alice"
    await first.aclose()
    await second.aclose()
    await c1.close()
    await c2.close()


async def test_shared_checkpointer_forgets_versions_of_finished_runs(ckpt):
    """一个共享的检查点实例服务成千上万个运行：只增不减的版本号字典就是内存泄漏。
    运行告一段落（非 running）就忘掉；恢复和审批都会先 load，重新记住最新版本。"""
    for i in range(50):
        s = state_with(f"r{i}")
        await ckpt.save(s)
        s.status = "completed" if i % 2 else "paused"
        await ckpt.save(s)
    assert ckpt._versions == {}
    resumed = await ckpt.load("r0")  # 暂停的运行被审批后恢复：先 load……
    resumed.status = "running"
    await ckpt.save(resumed)  # ……CAS 照常工作
    assert ckpt.version_of("r0") == 3


# ============================================================================= 队列


async def test_enqueue_dedupes_by_idempotency_key_within_a_tenant(queue):
    a = await queue.enqueue("agent", {"x": 1}, tenant_id="acme", idempotency_key="msg-1")
    assert await queue.enqueue("agent", {"x": 2}, tenant_id="acme", idempotency_key="msg-1") == a  # 用户连点两次
    assert await queue.enqueue("agent", {"x": 3}, tenant_id="globex", idempotency_key="msg-1") != a  # 别的租户不受影响
    assert await queue.enqueue("agent", {}, tenant_id="acme") != await queue.enqueue("agent", {}, tenant_id="acme")  # 不带 key 不去重
    assert (await queue.get(a)).payload == {"x": 1}  # 重复提交不会改写第一次的内容
    assert (await queue.stats())["counts"]["queued"] == 4


async def test_claim_respects_priority_run_at_and_kinds(queue):
    low = await queue.enqueue("agent", {}, tenant_id="t")
    high = await queue.enqueue("agent", {}, tenant_id="t", priority=10)
    later = await queue.enqueue("agent", {}, tenant_id="t", priority=99, delay_seconds=60)
    other = await queue.enqueue("report", {}, tenant_id="t", priority=50)
    assert (await queue.claim("w", kinds=["agent"])).id == high
    assert (await queue.claim("w", kinds=["agent"])).id == low
    assert await queue.claim("w", kinds=["agent"]) is None  # later 还没到时间
    job = await queue.claim("w")
    assert job.id == other and (job.status, job.attempts, job.worker_id) == ("leased", 1, "w")
    assert job.fence == 3  # fence 来自整张表共用的序列：这是本表的第 3 次领取，而不是"这个任务的第 1 次"
    assert (await queue.get(later)).status == "queued"


async def test_200_concurrent_claims_in_one_process_never_hand_out_a_job_twice(pg_uri):
    """一个进程里 200 个协程、20 个连接同时抢 150 个任务（多进程版本见后面的 WorkerPool 测试）。"""
    async with PostgresJobQueue(pg_uri, pool_kwargs={"min_size": 20, "max_size": 20}) as q:
        await q.setup()
        ids = await asyncio.gather(*(q.enqueue("agent", {"i": i}, tenant_id="t") for i in range(150)))
        jobs = await asyncio.gather(*(q.claim(f"task-{i}", 30) for i in range(200)))
        claimed = [j.id for j in jobs if j is not None]
        assert sorted(claimed) == sorted(ids), "有任务被领取了两次，或者有任务没被领取"
        assert sum(j is None for j in jobs) == 50
        assert len({j.fence for j in jobs if j is not None}) == 150  # 每次领取一个不同的 fence


async def test_skip_locked_skips_a_row_locked_by_someone_else_instead_of_waiting(queue, pg_uri):
    first = await queue.enqueue("agent", {}, tenant_id="t")
    second = await queue.enqueue("agent", {}, tenant_id="t")
    # lock_timeout：如果实现是在排队等行锁（少了 SKIP LOCKED），1 秒后直接报错，而不是靠计时来判断
    impatient = PostgresJobQueue(pg_uri, pool_kwargs={"kwargs": {"options": "-c lock_timeout=1000"}})
    async with await psycopg.AsyncConnection.connect(pg_uri) as holder:  # 另一个事务锁住了第一行，并且迟迟不提交
        await holder.execute("SELECT id FROM agent_jobs WHERE id = %s FOR UPDATE", (first,))
        assert (await impatient.claim("w", 30)).id == second  # 跳过被锁的行，而不是排队等锁
        await holder.rollback()
    assert (await impatient.claim("w", 30)).id == first
    await impatient.close()


async def test_expired_lease_is_reaped_and_zombie_is_fenced_out(queue, pg_uri):
    await queue.enqueue("agent", {}, tenant_id="t")
    a = await queue.claim("worker-A", 30)
    await expire_lease(pg_uri, a.id)  # A 卡住了（GC / 虚拟机暂停），没来得及续租
    b = await queue.claim("worker-B", 30)
    assert b.id == a.id and (b.fence, b.attempts, b.worker_id) == (a.fence + 1, 2, "worker-B")
    with pytest.raises(LeaseLost, match="fence"):
        await queue.heartbeat(a, 30)
    with pytest.raises(LeaseLost):
        await queue.complete(a, {"by": "A"})  # 僵尸的结果作废
    with pytest.raises(LeaseLost):
        await queue.fail(a, "僵尸报告失败")
    await queue.complete(b, {"by": "B"})
    done = await queue.get(a.id)
    assert (done.status, done.result, done.worker_id) == ("succeeded", {"by": "B"}, "worker-B")
    with pytest.raises(LeaseLost):
        await queue.complete(b, {"by": "B again"})  # 重复提交也被拒绝


async def test_late_completion_is_accepted_if_nobody_reaped_the_job(queue, pg_uri):
    await queue.enqueue("agent", {}, tenant_id="t")
    job = await queue.claim("w", 30)
    await expire_lease(pg_uri, job.id)
    await queue.complete(job, "迟到但有效")  # 租约过期了，但还没人回收 / 接手：fence 仍然是最新的
    assert (await queue.get(job.id)).status == "succeeded"


async def test_reaped_job_loses_ownership_even_before_anyone_reclaims_it(queue, pg_uri):
    await queue.enqueue("agent", {}, tenant_id="t")
    job = await queue.claim("w", 30)
    await expire_lease(pg_uri, job.id)
    assert await queue.reap_expired() == [(job.id, "queued")]
    with pytest.raises(LeaseLost, match="不再是 leased"):
        await queue.complete(job, "太迟了")


async def test_fail_backoff_dead_letter_and_redrive_keeps_fence_monotonic(pg_uri, monkeypatch):
    import agentkit.contrib.postgres as pg

    monkeypatch.setattr(pg.random, "uniform", lambda a, b: b)  # 全抖动固定取上界（60 秒）：断言不靠运气
    q = PostgresJobQueue(pg_uri, max_attempts=2, base_backoff=60)
    await q.setup()
    jid = await q.enqueue("agent", {}, tenant_id="t")
    j1 = await q.claim("w", 30)
    assert await q.fail(j1, "429 Too Many Requests") == "queued"
    assert await q.claim("w", 30) is None  # 退避中（60 秒）
    await sql(pg_uri, "UPDATE agent_jobs SET run_at = now() WHERE id = %s", (jid,))
    j2 = await q.claim("w", 30)
    assert await q.fail(j2, "又失败了") == "dead"  # 次数用尽 → 死信
    stats = await q.stats()
    assert stats["counts"]["dead"] == 1 and stats["ready"] == 0
    assert await q.redrive(jid) and not await q.redrive(jid)  # 只能 redrive dead / failed
    j3 = await q.claim("w", 30)
    assert (j3.attempts, j3.fence) == (1, 3)  # attempts 清零，fence 继续递增（绝不重置）
    await q.close()


async def test_non_retryable_failure_and_poison_message(queue, pg_uri):
    await queue.enqueue("agent", {}, tenant_id="t")
    job = await queue.claim("w", 30)
    assert await queue.fail(job, "参数非法", retryable=False) == "failed"

    poison = await queue.enqueue("agent", {"attachment": "20MB"}, tenant_id="t", max_attempts=1)
    p = await queue.claim("w", 30)
    assert p.id == poison
    await expire_lease(pg_uri, poison)  # worker 每次处理它都崩溃，根本走不到 fail()
    assert await queue.reap_expired() == [(poison, "dead")]  # 计数发生在领取时，所以照样能进死信
    assert "没有按时续约" in (await queue.get(poison)).last_error


async def test_release_does_not_consume_an_attempt(queue):
    jid = await queue.enqueue("agent", {}, tenant_id="t")
    job = await queue.claim("w", 30)
    await queue.release(job, reason="shutting down")
    again = await queue.get(jid)
    assert (again.status, again.attempts, again.fence) == ("queued", 0, 1)
    assert (await queue.claim("w", 30)).fence == 2
    with pytest.raises(LeaseLost):
        await queue.release(job)


async def test_stats_reports_oldest_ready_job_age(queue):
    await queue.enqueue("agent", {}, tenant_id="t", run_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    await queue.enqueue("agent", {}, tenant_id="t", delay_seconds=600)  # 还没到时间的不算"在等"
    s = await queue.stats()
    assert s["counts"]["queued"] == 2 and s["ready"] == 1
    assert 29 <= s["oldest_queued_age_s"] < 60


async def test_fence_is_global_so_a_later_job_for_the_same_run_can_take_over(queue, ckpt, pg_uri):
    """第 31 课压测中发现（已修复）：fence 以前按任务从 1 数起。run 任务被接手过（fence=2）之后，
    审批产生的 resume 任务第一次领取拿到 fence=1，被检查点当成"比 fence=2 更旧的持有者"拒绝。"""
    first = await queue.enqueue("agent", {"op": "run", "run_id": "r"}, tenant_id="t")
    a = await queue.claim("w1", 30)
    await ckpt.fenced(a.fence, "w1").save(state_with("r"))
    await expire_lease(pg_uri, first)  # w1 卡死：租约过期、被回收，由 w2 接手
    assert await queue.reap_expired() == [(first, "queued")]
    b = await queue.claim("w2", 30)
    assert b.id == first and b.fence > a.fence
    view_b = ckpt.fenced(b.fence, "w2")
    s = await view_b.load("r")
    s.status = "paused"
    await view_b.save(s)
    await queue.complete(b)

    await queue.enqueue("agent", {"op": "resume", "run_id": "r"}, tenant_id="t")  # 审批通过：一个新任务，第一次领取
    c = await queue.claim("w3", 30)
    assert c.attempts == 1 and c.fence > b.fence
    assert (await ckpt.fenced(c.fence, "w3").load("r")).status == "paused"  # 以前这里抛 CheckpointConflict


async def test_upgrading_from_per_job_fences_continues_after_the_largest_existing_fence(queue, pg_uri):
    """从旧版本（fence = fence + 1）升级：序列必须从表里已有的最大 fence 往后发，否则新 fence 可能比旧的小。"""
    job = await queue.enqueue("agent", {}, tenant_id="t")
    await sql(pg_uri, "UPDATE agent_jobs SET fence = 41 WHERE id = %s", (job,))  # 模拟旧版本留下的数据
    await sql(pg_uri, "DROP SEQUENCE agent_jobs_fence_seq")
    await queue.setup()  # 发布新版本时执行
    await queue.setup()  # 再执行一次也不会把序列往回拨
    assert (await queue.claim("w", 30)).fence == 42


# ============================================================================= run_worker（一个进程内）


async def test_graceful_stop_finishes_the_current_job_and_claims_no_more(queue):
    for i in range(5):
        await queue.enqueue("agent", {"i": i}, tenant_id="t")
    stop = asyncio.Event()
    events = []

    async def handler(job):
        stop.set()  # 相当于 K8s 发来了 SIGTERM
        await asyncio.sleep(0.2)  # 手头的任务照常做完
        return {"i": job.payload["i"]}

    stats = await run_worker(queue, handler, worker_id="w", stop_event=stop, concurrency=1, lease_seconds=5,
                             on_event=lambda name, info: events.append(name))
    assert stats["claimed"] == 1 and stats["succeeded"] == 1 and stats["cancelled"] == 0
    assert (await queue.stats())["counts"] == {"queued": 4, "leased": 0, "succeeded": 1, "failed": 0, "dead": 0}
    assert events[0] == "started" and events[-1] == "stopped" and "completed" in events


async def test_worker_routes_handler_errors_to_the_right_queue_transition(queue):
    perm = await queue.enqueue("agent", {"mode": "perm"}, tenant_id="t")
    later = await queue.enqueue("agent", {"mode": "later"}, tenant_id="t")
    boom = await queue.enqueue("agent", {"mode": "boom"}, tenant_id="t")

    async def handler(job):
        mode = job.payload["mode"]
        if mode == "boom":
            raise ValueError("下游 500")
        if mode == "perm":
            raise PermanentJobError("参数非法")
        raise RetryLater(60, "rate_limited")

    stats = await run_worker(queue, handler, worker_id="w", stop_event=asyncio.Event(), max_jobs=3, lease_seconds=5)
    assert (stats["failed"], stats["deferred"]) == (2, 1)
    assert (await queue.get(boom)).status == "queued" and "ValueError" in (await queue.get(boom)).last_error
    assert (await queue.get(perm)).status == "failed"
    deferred = await queue.get(later)
    assert (deferred.status, deferred.attempts) == ("queued", 0)  # 推迟不消耗重试次数


async def _drain(pg_uri, n_jobs: int, handler, *, concurrency: int, pool_max: int = 20, **kw) -> dict:
    """入队 n_jobs 个任务，用一个 run_worker 跑完，返回统计。"""
    async with PostgresJobQueue(pg_uri, pool_kwargs={"min_size": 2, "max_size": pool_max}) as q:
        await q.setup()
        for i in range(n_jobs):
            await q.enqueue("agent", {"i": i}, tenant_id="t")
        stats = await run_worker(q, handler, worker_id="w", stop_event=asyncio.Event(), concurrency=concurrency,
                                 max_jobs=n_jobs, poll_interval=0.02, grace_period=30, **kw)
        assert (await q.stats())["counts"]["succeeded"] == n_jobs
        return stats


async def _truncate(pg_uri) -> None:
    """清空队列表再跑下一轮（同一个测试里对比两种配置）。"""
    await sql(pg_uri, "TRUNCATE agent_jobs")


async def test_one_process_runs_16_jobs_at_a_time(pg_uri):
    in_flight = {"now": 0, "max": 0}
    full = asyncio.Event()

    async def handler(job):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        if in_flight["now"] >= 16:
            full.set()
        try:  # "等模型"：等到 16 个任务同时在等（或最多 5 秒），证明它们确实是并发的
            await wait_for(full.wait(), 5)
        except asyncio.TimeoutError:
            pass
        in_flight["now"] -= 1
        return job.payload["i"]

    stats = await _drain(pg_uri, 64, handler, concurrency=16)
    assert stats["succeeded"] == 64
    # 一个进程里同时有 16 个任务在等 IO；背压保证永远不超过 concurrency
    assert in_flight["max"] == stats["max_in_flight"] == 16


async def test_worker_connections_are_bounded_by_its_pool_not_by_jobs_or_heartbeats(pg_uri):
    """8 个在途任务、每个任务每 10ms 续一次租：worker 占用的数据库连接数只取决于连接池的 max_size。"""
    app = "agentkit-bounded-worker"
    count_sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s"
    q = PostgresJobQueue(pg_uri, pool_kwargs={"min_size": 1, "max_size": 3, "kwargs": {"application_name": app}})
    await q.setup()
    for i in range(24):
        await q.enqueue("agent", {"i": i}, tenant_id="t")
    samples: list[int] = []

    async def monitor(stop: asyncio.Event) -> None:
        async with await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as conn:
            while not stop.is_set():
                samples.append((await (await conn.execute(count_sql, (app,))).fetchone())[0])
                await asyncio.sleep(0.01)

    async def handler(job):
        await asyncio.sleep(0.1)  # 让心跳真的跑起来：每个任务续租约 10 次
        return "ok"

    done = asyncio.Event()
    watcher = asyncio.create_task(monitor(done))
    stats = await run_worker(q, handler, worker_id="w", stop_event=asyncio.Event(), concurrency=8, max_jobs=24,
                             heartbeat_interval=0.01, poll_interval=0.01)
    done.set()
    await watcher
    await q.close()
    assert stats["succeeded"] == 24 and stats["max_in_flight"] == 8
    assert samples and max(samples) <= 3, samples  # 不随任务数、心跳数增长
    assert (await sql(pg_uri, count_sql, (app,)))[0][0] == 0  # close() 之后连接全部归还给数据库


async def test_connection_pool_smaller_than_concurrency_becomes_the_bottleneck(pg_uri):
    async def run_with_pool(size: int) -> dict:
        holding = {"now": 0, "max": 0}
        crowd = asyncio.Event()  # 同时持有连接的任务达到 5 个 → 放行（池只有 4 个时永远达不到，只能等超时）
        async with AsyncConnectionPool(pg_uri, min_size=size, max_size=size, kwargs={"autocommit": True}) as pool:
            await pool.wait()  # 连接全部建好再开始，"排队"只可能因为池满

            async def handler(job):  # 任务执行期间占用一个数据库连接（比如一次查询 / 一个事务）
                async with pool.connection() as conn:
                    holding["now"] += 1
                    holding["max"] = max(holding["max"], holding["now"])
                    if holding["now"] >= 5:
                        crowd.set()
                    try:
                        await wait_for(crowd.wait(), 0.3)
                    except asyncio.TimeoutError:
                        pass
                    await conn.execute("SELECT 1")
                    holding["now"] -= 1
                return "ok"

            await _drain(pg_uri, 12, handler, concurrency=16)
            return {**holding, "queued": pool.get_stats().get("requests_queued", 0)}

    small = await run_with_pool(4)
    await _truncate(pg_uri)
    big = await run_with_pool(16)
    # 池 4：并发度 16 被卡成 4，其余任务在池里排队；池 16：同时持有连接的任务超过 4 个，没有任何排队
    assert small["max"] == 4 and small["queued"] > 0, small
    assert big["max"] > 4 and big["queued"] == 0, big


async def test_blocking_call_inside_an_async_handler_stalls_every_other_task(pg_uri):
    sleeping = {"now": 0, "max": 0}
    lock = threading.Lock()

    def enter():
        with lock:
            sleeping["now"] += 1
            sleeping["max"] = max(sleeping["max"], sleeping["now"])

    def leave():
        with lock:
            sleeping["now"] -= 1

    async def blocking(job):
        enter()
        time.sleep(0.05)  # ❌ 在协程里做同步阻塞 IO：整个事件循环停住，别的任务一个都动不了
        leave()
        return "ok"

    def sync_work():
        enter()
        deadline = time.monotonic() + 2
        while sleeping["max"] < 2 and time.monotonic() < deadline:  # 等另一个任务也进来（最多 2 秒）
            time.sleep(0.005)
        time.sleep(0.02)
        leave()

    async def offloaded(job):
        await asyncio.to_thread(sync_work)  # ✅ 阻塞调用放进线程：事件循环照常调度别的任务
        return "ok"

    await _drain(pg_uri, 16, blocking, concurrency=8)
    assert sleeping["max"] == 1, "阻塞调用期间事件循环是停住的，不可能有第二个任务同时在跑"
    sleeping["max"] = 0
    await _truncate(pg_uri)
    await _drain(pg_uri, 16, offloaded, concurrency=8)
    assert sleeping["max"] >= 2, "放进线程的阻塞调用应该并发执行"

    async with PostgresJobQueue(pg_uri) as q:
        with pytest.raises(TypeError, match="async"):  # 同步 handler 直接拒绝：它会在事件循环里阻塞
            await run_worker(q, lambda job: "ok", worker_id="w", stop_event=asyncio.Event())


async def test_shutdown_cancels_stragglers_after_grace_period_and_lets_leases_expire(pg_uri):
    async with PostgresJobQueue(pg_uri, base_backoff=0) as q:
        await q.setup()
        for i in range(4):
            await q.enqueue("agent", {"slow": i >= 2}, tenant_id="t")
        stop = asyncio.Event()
        progress = {"started": 0, "quick_done": 0}

        async def handler(job):
            progress["started"] += 1
            if job.payload["slow"]:
                await asyncio.sleep(30)
            progress["quick_done"] += 1
            return "ok"

        async def sigterm_when_ready():
            await eventually(lambda: progress["started"] == 4 and progress["quick_done"] == 2, interval=0.01)
            await eventually(lambda: _count(pg_uri, "SELECT count(*) FROM agent_jobs WHERE status = 'succeeded'", None, 2),
                             what="两个快任务提交完成")
            stop.set()  # 相当于 K8s 发来 SIGTERM

        stats, _ = await asyncio.gather(
            run_worker(q, handler, worker_id="w", stop_event=stop, concurrency=4, grace_period=0.3,
                       lease_seconds=30, poll_interval=0.02),
            sigterm_when_ready(),
        )
        assert (stats["succeeded"], stats["cancelled"]) == (2, 2)
        counts = (await q.stats())["counts"]
        assert (counts["succeeded"], counts["leased"]) == (2, 2)  # 被取消的任务没有提交、也没有归还
        await expire_lease(pg_uri)  # 租约自然过期后……
        old_max = (await sql(pg_uri, "SELECT max(fence) FROM agent_jobs"))[0][0]
        taken = [await q.claim("another-pod", 30), await q.claim("another-pod", 30)]
        assert all(j is not None and j.attempts == 2 and j.fence > old_max for j in taken)  # ……由别的 worker 以更大的 fence 接手


async def test_worker_rides_out_a_database_outage_instead_of_crashing(pg_tcp_uri):
    """worker 和数据库之间断网（TcpProxy.cut）：领取报 OperationalError / PoolTimeout，
    run_worker 自动用 PostgresJobQueue.transient_errors 识别为"暂时不可用"，退避重试而不是崩溃；恢复后接着干活。"""
    info = conninfo_to_dict(pg_tcp_uri)
    direct = PostgresJobQueue(pg_tcp_uri)  # 数据库那一侧：API 进程照常入队
    await direct.setup()
    events: list[tuple[str, dict]] = []
    done: list[int] = []

    async def handler(job):
        done.append(job.id)
        return "ok"

    async with TcpProxy(info["host"], int(info["port"])) as proxy:
        via_proxy = f"postgresql://{info['user']}@127.0.0.1:{proxy.listen_port}/{info['dbname']}"
        q = PostgresJobQueue(via_proxy, pool_kwargs={"timeout": 1.0})  # 借连接最多等 1 秒，超时抛 PoolTimeout
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(q, handler, worker_id="w", stop_event=stop, poll_interval=0.05,
                                                on_event=lambda name, info: events.append((name, info))))
        try:
            first = await direct.enqueue("agent", {}, tenant_id="t")
            await eventually(lambda: succeeded(direct, first), what="连通时正常处理任务（已提交）")
            assert proxy.cut() >= 1  # 断网：现有连接全部被重置
            await eventually(lambda: sum(n == "claim_error" for n, _ in events) >= 2, what="领取失败并退避")
            second = await direct.enqueue("agent", {}, tenant_id="t")  # 断网期间来了新任务
            await asyncio.sleep(0.5)
            assert not worker.done() and done == [first]  # worker 没崩溃，只是暂时领不到
            proxy.heal()
            await eventually(lambda: done == [first, second], what="恢复后接着处理")
        finally:
            stop.set()
            stats = await worker
            await q.close()
    await direct.close()
    errors = [i["error"] for n, i in events if n == "claim_error"]
    assert any(e.startswith(("OperationalError", "PoolTimeout")) for e in errors), errors
    assert stats["succeeded"] == 2 and stats["claimed"] == 2
    assert proxy.stats["dropped"] >= 1 and proxy.stats["refused"] >= 1  # 断网期间的重连也被拒绝了


# ============================================================================= AgentJobHandler（一个进程内）


@tool(risk="dangerous")
def wire_transfer(amount: int) -> str:
    """转账"""
    TRANSFERS.append(amount)
    return f"已转账 {amount} 元"


TRANSFERS: list[int] = []


async def test_agent_job_pauses_for_approval_then_another_worker_resumes(pg_uri):
    TRANSFERS.clear()

    def responder(messages):
        return reply("转账完成") if messages[-1]["role"] == "tool" else call_tool("wire_transfer", amount=500)

    async with AsyncConnectionPool(pg_uri, min_size=2, max_size=8, kwargs={"autocommit": True}) as pool:
        q, ckpt = PostgresJobQueue(pool), PostgresCheckpointer(pool)  # 队列和检查点共用一个池
        await q.setup()
        await ckpt.setup()

        def pod():  # 每个"Pod"一个共享的 Agent；每个任务通过 checkpointer= 传入带 fence 的视图
            agent = Agent(ScriptedLLM(responder=responder), [wire_transfer], checkpointer=ckpt, hooks=[PermissionPolicy()])
            return AgentJobHandler(agent, ckpt)

        pod1, pod2 = pod(), pod()
        await q.enqueue("agent", {"op": "run", "input": "给供应商转 500", "metadata": {"tenant_id": "evil", "user_id": "u1"}},
                        tenant_id="acme")
        await run_worker(q, pod1, worker_id="pod-1", stop_event=asyncio.Event(), max_jobs=1, poll_interval=0.02)
        first = await q.get(1)
        assert first.status == "succeeded" and first.result["awaiting_approval"] and TRANSFERS == []
        run_id, call_id = first.result["run_id"], first.result["pending"]["id"]
        inbox = await ckpt.list_runs(status="paused", tenant_id="acme")  # tenant_id 以任务为准，payload 里的 "evil" 被覆盖
        assert [r["run_id"] for r in inbox] == [run_id] and inbox[0]["user_id"] == "u1"

        payload = {"op": "resume", "run_id": run_id, "approvals": {call_id: True}, "by": "cfo"}
        r1 = await q.enqueue("agent", payload, tenant_id="acme", idempotency_key=f"approve:{run_id}:{call_id}")
        r2 = await q.enqueue("agent", payload, tenant_id="acme", idempotency_key=f"approve:{run_id}:{call_id}")
        assert r1 == r2  # 审批人手抖点了两次：只入队一次
        await run_worker(q, pod2, worker_id="pod-2", stop_event=asyncio.Event(), max_jobs=1, poll_interval=0.02)
        assert (await q.get(r1)).result["status"] == "completed" and TRANSFERS == [500]
        row = await ckpt.get_run(run_id)
        assert row["writer"] == "pod-2" and row["state"]["approval_log"][0]["by"] == "cfo"


async def test_resume_job_for_another_tenants_run_is_rejected(queue, ckpt):
    s = state_with("r-acme", tenant_id="acme")
    s.status = "paused"
    await ckpt.save(s)
    await queue.enqueue("agent", {"op": "resume", "run_id": "r-acme", "approvals": {}}, tenant_id="globex")
    job = await queue.claim("w", 30)
    handler = AgentJobHandler(Agent(ScriptedLLM([]), checkpointer=ckpt), ckpt)
    with pytest.raises(PermanentJobError, match="属于租户 acme"):
        await handler(job)


async def test_many_agents_share_one_pool_and_really_run_concurrently(pg_uri):
    @tool
    def lookup(q: str) -> str:
        """查知识库"""
        return f"关于 {q} 的答案"

    def responder(messages):  # 按对话决定：先查一次，拿到结果就回答
        return reply("好的") if messages[-1]["role"] == "tool" else call_tool("lookup", q="VPN")

    async with AsyncConnectionPool(pg_uri, min_size=10, max_size=10, kwargs={"autocommit": True}) as pool:
        ckpt = PostgresCheckpointer(pool)  # 所有会话共用一个 10 连接的池
        await ckpt.setup()
        llm = ScriptedLLM(responder=responder, latency=0.3)
        agent = Agent(llm, [lookup], checkpointer=ckpt)  # 一个 Agent 实例被 40 个会话并发复用
        results = await asyncio.gather(*(
            agent.run("VPN 连不上", run_id=f"s{i}", metadata={"tenant_id": "acme"}) for i in range(40)
        ))
        assert all(r.ok for r in results)
        # 同一时刻在途的模型调用数 > 连接数：等模型的时候不占数据库连接，10 个连接撑得起几十个会话
        assert llm.max_in_flight > 10, llm.max_in_flight
        rows = await ckpt.list_runs(status="completed", limit=100)
        assert len(rows) == 40 and all(r["version"] >= 4 for r in rows)  # 每个会话每一步都落盘了
        await agent.aclose()


async def test_one_shared_agent_serves_many_concurrent_jobs(pg_uri):
    def responder(messages):
        return reply("好的") if messages[-1]["role"] == "tool" else call_tool("lookup_kb", q="VPN")

    @tool
    def lookup_kb(q: str) -> str:
        """查知识库"""
        return "重启客户端"

    async with PostgresJobQueue(pg_uri) as q, PostgresCheckpointer(pg_uri) as ckpt:
        await q.setup()
        await ckpt.setup()
        llm = ScriptedLLM(responder=responder, latency=0.05)
        agent = Agent(llm, [lookup_kb], checkpointer=ckpt)
        handler = AgentJobHandler(agent, ckpt)
        AgentJobHandler(agent, ckpt)  # 同一个 Agent 再包一次也只装一个租约守卫
        for i in range(20):
            await q.enqueue("agent", {"op": "run", "input": f"VPN 连不上 #{i}"}, tenant_id="acme")
        stats = await run_worker(q, handler, worker_id="w", stop_event=asyncio.Event(), concurrency=8,
                                 max_jobs=20, poll_interval=0.02)
        assert stats["succeeded"] == 20
        assert handler.agents_created == 0  # 共用模式：一个 Agent（一个线程池、一个模型客户端）服务全部 20 个任务
        assert sum(isinstance(h, LeaseGuard) for h in agent.hooks) == 1
        assert llm.max_in_flight > 1  # 同一个 Agent 实例上真的有多个任务在并发
        rows = await ckpt.list_runs(limit=100)
        # 每个任务用的是自己的 fenced 视图：20 个任务各领取一次，fence 是全局序列发出的 1..20，各不相同
        assert len(rows) == 20 and sorted(r["fence"] for r in rows) == list(range(1, 21))
        assert all(r["writer"] == "w" for r in rows)
        await agent.aclose()


async def test_agent_factory_is_called_per_job_with_that_jobs_fenced_view(pg_uri):
    """工厂写法：每个任务调用一次 make_agent(checkpointer, job)（可以是 async），拿到的是带本次 fence 的检查点视图。"""
    built = []
    llm = ScriptedLLM(responder=lambda m: reply("好的"))

    async with PostgresJobQueue(pg_uri) as q, PostgresCheckpointer(pg_uri) as ckpt:
        await q.setup()
        await ckpt.setup()

        async def make_agent(checkpointer, job):  # 例如按租户选模型、选工具
            built.append((checkpointer.fence, job.fence, job.tenant_id))
            return Agent(llm, checkpointer=checkpointer)

        handler = AgentJobHandler(make_agent, ckpt)
        for tenant in ("acme", "globex", "initech"):
            await q.enqueue("agent", {"op": "run", "input": "你好"}, tenant_id=tenant)
        stats = await run_worker(q, handler, worker_id="w", stop_event=asyncio.Event(), max_jobs=3, poll_interval=0.02)
        assert stats["succeeded"] == 3 and handler.agents_created == 3
        assert sorted(built) == [(1, 1, "acme"), (2, 2, "globex"), (3, 3, "initech")]


async def test_lease_guard_stops_only_the_job_whose_lease_was_lost(pg_uri):
    async with PostgresJobQueue(pg_uri) as q, PostgresCheckpointer(pg_uri) as ckpt:
        await q.setup()
        await ckpt.setup()
        agent = Agent(ScriptedLLM(responder=lambda m: reply("完成"), latency=0.05), checkpointer=ckpt)
        handler = AgentJobHandler(agent, ckpt)
        for i in range(2):
            await q.enqueue("agent", {"op": "run", "input": f"q{i}"}, tenant_id="acme")
        zombie, healthy = await q.claim("w", 30), await q.claim("w", 30)
        zombie.lost.set()  # 心跳发现这个任务的租约已经丢了
        results = await asyncio.gather(handler(zombie), handler(healthy), return_exceptions=True)
        assert isinstance(results[0], LeaseLost)  # 只有它停手（ContextVar：每个任务看到自己的 job）
        assert results[1]["status"] == "completed"
        assert (await ckpt.get_run(f"job-{zombie.id}"))["state"]["stop_reason"] == "lease_lost"
        await agent.aclose()


async def test_cancelled_read_only_run_is_left_to_expire_and_resumed_by_the_next_worker(pg_uri):
    """优雅停机取消了一个卡住的只读工具：检查点落盘为 cancelled，给它补"未执行"，任务既不提交也不归还；
    租约过期后下一个 worker 从检查点继续，由模型重新决定（只读，重做无害）。
    两个 run_worker 在同一个进程里**先后**运行（没有并发）；写操作的版本用真进程做，见后面的 SIGTERM 测试。"""
    executed = []

    @tool
    async def search(q: str) -> str:
        """搜索（第一次执行时卡住 30 秒）"""
        if not executed:
            executed.append("hang")
            await asyncio.sleep(30)
        executed.append("done")
        return "搜索结果"

    def responder(messages):
        return reply("答完了") if messages[-1]["role"] == "tool" and messages[-1]["content"] == "搜索结果" \
            else call_tool("search", q="报销流程")

    async with PostgresJobQueue(pg_uri, base_backoff=0) as q, PostgresCheckpointer(pg_uri) as ckpt:
        await q.setup()
        await ckpt.setup()

        def pod():
            return AgentJobHandler(Agent(ScriptedLLM(responder=responder), [search], checkpointer=ckpt), ckpt)

        await q.enqueue("agent", {"op": "run", "input": "报销流程是什么"}, tenant_id="acme")
        stop = asyncio.Event()

        async def sigterm_while_tool_hangs():
            await eventually(lambda: executed, interval=0.01)
            stop.set()

        stats, _ = await asyncio.gather(
            run_worker(q, pod(), worker_id="pod-1", stop_event=stop, grace_period=0.2, poll_interval=0.02),
            sigterm_while_tool_hangs(),
        )
        assert stats["cancelled"] == 1
        row = await ckpt.get_run("job-1")
        assert row["status"] == "cancelled" and row["writer"] == "pod-1"  # Agent 在取消时把检查点落盘
        assert row["state"]["messages"][-1]["content"].startswith("未执行")  # 只读工具被打断：补上"未执行"
        assert (await q.get(1)).status == "leased"  # 没有 complete，也没有归还

        await expire_lease(pg_uri)
        stats2 = await run_worker(q, pod(), worker_id="pod-2", stop_event=asyncio.Event(), max_jobs=1, poll_interval=0.02)
        assert stats2["succeeded"] == 1
        job = await q.get(1)
        assert (job.fence, job.result["status"], job.result["output"]) == (2, "completed", "答完了")
        assert (await ckpt.get_run("job-1"))["writer"] == "pod-2"
        assert executed == ["hang", "done"]


# ============================================================================= 真实的多进程（WorkerPool）


async def create_record_tables(uri: str) -> None:
    """worker 应用（pg_worker_apps.py）写的记录表：测试进程在拉起 worker 之前建好。"""
    await sql(uri, "CREATE TABLE ticket_calls (key text, title text, worker text, pid int, attempt int, fence bigint, "
                   "t timestamptz NOT NULL DEFAULT clock_timestamp())")
    await sql(uri, "CREATE TABLE tickets (id serial PRIMARY KEY, idempotency_key text UNIQUE NOT NULL, title text)")
    await sql(uri, "CREATE TABLE job_log (job_id bigint, fence bigint, worker text, pid int, start_t float8, end_t float8)")


async def prepare(uri: str) -> tuple[PostgresJobQueue, PostgresCheckpointer]:
    """记录表 + 队列表 + 检查点表，返回测试进程这一侧的队列和检查点（用来入队、观察）。"""
    await create_record_tables(uri)
    q, ckpt = PostgresJobQueue(uri), PostgresCheckpointer(uri)
    await q.setup()
    await ckpt.setup()
    return q, ckpt


@pytest.fixture
async def shared(pg_uri):
    q, ckpt = await prepare(pg_uri)
    yield q, ckpt
    await q.close()
    await ckpt.close()


@asynccontextmanager
async def running(*pools: WorkerPool):
    """启动 worker 进程；退出时优雅停止。停止放进线程里等：事件循环不能被卡住（TcpProxy 还在里面转发）。"""
    for p in pools:
        p.start()
    try:
        yield pools
    finally:
        await asyncio.gather(*(asyncio.to_thread(p.stop) for p in pools))


def started(pool: WorkerPool) -> int:
    return len({e["worker_id"] for e in pool.events("started")})


def at_gate(pool: WorkerPool, jid: int) -> list[dict]:
    """持有者已经执行完工具、写好检查点，正停在第一次领取的第二次模型调用里（gate 上）。"""
    return [e for e in pool.events("llm_call") if e["job"] == jid and e["phase"] == "second" and e["attempt"] == 1]


def events_of(pool: WorkerPool, name: str, worker: str | None = None, job: int | None = None) -> list[dict]:
    return [e for e in pool.events(name)
            if (worker is None or e["worker_id"] == worker) and (job is None or e.get("job") == job)]


def index_of(pool: WorkerPool, worker_id: str) -> int:
    return next(i for i, w in enumerate(pool.workers) if w.worker_id == worker_id)


async def test_setup_is_safe_when_many_processes_start_at_once(pg_uri):
    """6 个真正的进程在同一时刻执行 setup()（不加 advisory lock 时，并发的 CREATE TABLE IF NOT EXISTS
    会撞 UniqueViolation: pg_class_relname_nsp_index）。

    发令枪也是跨进程的：测试进程持有一个 advisory lock 的排他锁，子进程建好连接后申请共享锁、排队等待；
    pg_locks 里看到 6 个进程都在等之后才释放 —— 6 个进程被同一次解锁唤醒。"""
    barrier, n = 7_202_613, 6
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")]))}
    waiting = ("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND objid::bigint = %s "
               "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())")
    procs = []
    try:
        async with await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as starter:
            await starter.execute("SELECT pg_advisory_lock(%s)", (barrier,))
            procs = [subprocess.Popen([sys.executable, APPS, "setup-race", pg_uri, str(barrier)], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for _ in range(n)]
            await eventually(lambda: _count(pg_uri, waiting, (barrier,), n), what=f"{n} 个进程都在发令枪前排队")
            await starter.execute("SELECT pg_advisory_unlock(%s)", (barrier,))  # 开枪
            outs = await asyncio.gather(*(asyncio.to_thread(p.communicate, timeout=60) for p in procs))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    logs = "\n".join(o[0].decode(errors="replace") for o in outs)
    assert [p.returncode for p in procs] == [0] * n, logs
    q = PostgresJobQueue(pg_uri)
    await q.setup()  # 之后再来一次也没事
    await q.enqueue("agent", {}, tenant_id="t")
    assert (await q.claim("w", 30)).fence == 1  # 序列只建了一份，从 1 开始发
    await q.close()


async def test_worker_processes_never_claim_the_same_job(pg_uri, shared, tmp_path):
    q, _ = shared
    pool = WorkerPool(pg_uri, RECORDING_APP, n=3, concurrency=4, poll=0.05, options={"hold": 0.05},
                      log_dir=tmp_path / "logs")
    async with running(pool):
        await eventually(lambda: started(pool) == 3, what="3 个 worker 进程启动")  # 先都就位，再放任务进来抢
        ids = [await q.enqueue("rec", {"i": i}, tenant_id="t") for i in range(60)]
        await eventually(lambda: _count(pg_uri, "SELECT count(*) FROM agent_jobs WHERE status = 'succeeded'", None, 60),
                         what="60 个任务全部完成")
        claimed = pool.events("claimed")
        logs = pool.logs()
    log = await sql(pg_uri, "SELECT job_id, pid FROM job_log")
    assert sorted(r[0] for r in log) == sorted(ids), logs  # 每个任务恰好处理一次
    assert len({e["job"] for e in claimed}) == len(claimed) == 60  # 没有任何任务被领取两次
    assert len({r[1] for r in log}) >= 2  # 真的是多个进程在分活
    attempts = await sql(pg_uri, "SELECT DISTINCT attempts FROM agent_jobs")
    assert attempts == [(1,)]


async def test_heartbeats_keep_a_long_job_while_another_process_is_polling(pg_uri, shared, tmp_path):
    """长任务处理 3 秒 = 2 个租约（1.5 秒）：没有心跳的话，另一个进程早就把它领走了。"""
    q, _ = shared
    pool = WorkerPool(pg_uri, RECORDING_APP, n=2, concurrency=1, lease=1.5, poll=0.05, log_dir=tmp_path / "logs")
    async with running(pool):
        await eventually(lambda: started(pool) == 2, what="2 个 worker 进程启动")
        long_job = await q.enqueue("rec", {"hold": 3.0}, tenant_id="t")
        holder = (await eventually(lambda: events_of(pool, "claimed", job=long_job), what="长任务被领取"))[0]["worker_id"]
        # 持有者 concurrency=1、正忙：短任务只能被另一个进程领走 —— 证明另一个进程一直在轮询
        short_job = await q.enqueue("rec", {"hold": 0.05}, tenant_id="t")
        await eventually(lambda: _count(pg_uri, "SELECT count(*) FROM agent_jobs WHERE status = 'succeeded'", None, 2),
                         what="两个任务都完成")
        claimed = pool.events("claimed")
    assert [e["worker_id"] for e in claimed if e["job"] == long_job] == [holder]  # 只被领取过一次
    poller = [e["worker_id"] for e in claimed if e["job"] == short_job]
    assert len(poller) == 1 and poller[0] != holder
    job = await q.get(long_job)
    assert (job.attempts, job.worker_id) == (1, holder)
    assert await sql(pg_uri, "SELECT worker FROM job_log WHERE job_id = %s", (long_job,)) == [(holder,)]


async def test_kill_9_holder_is_taken_over_from_the_checkpoint_and_the_tool_runs_once(pg_uri, shared, tmp_path):
    """持有者在"工具已执行、检查点已写入、第二次模型调用进行中"时被 kill -9：租约过期后另一个进程接手，
    从检查点继续（不重新调用工具、不从头再来），工单只建了一张。"""
    q, ckpt = shared
    pool = WorkerPool(pg_uri, AGENT_APP, n=2, concurrency=2, lease=LEASE, poll=0.05,
                      options={"gate": tmp_path / "gate"}, log_dir=tmp_path / "logs")  # gate 永远不打开
    async with running(pool):
        await eventually(lambda: started(pool) == 2, what="2 个 worker 进程启动")
        jid = await q.enqueue("run", {"op": "run", "input": "打印机坏了"}, tenant_id="acme")
        await eventually(lambda: at_gate(pool, jid), what="持有者执行完工具、停在第二次模型调用里")
        holder = (await q.get(jid)).worker_id
        run = await ckpt.get_run(f"job-{jid}")
        assert run["writer"] == holder and run["state"]["messages"][-1]["role"] == "tool"  # 工具结果已经在检查点里
        victim = index_of(pool, holder)
        pool.kill(victim)  # SIGKILL：来不及做任何收尾
        assert not pool.workers[victim].alive
        job = await eventually(lambda: succeeded(q, jid), what="另一个进程接手并完成")
        events = pool.events()
        logs = pool.logs()
    taker = job.worker_id
    assert taker != holder and job.attempts == 2, logs
    claims = [(e["worker_id"], e["fence"]) for e in events if e["event"] == "claimed" and e["job"] == jid]
    assert [w for w, _ in claims] == [holder, taker] and claims[1][1] > claims[0][1]
    assert await sql(pg_uri, "SELECT worker, attempt FROM ticket_calls") == [(holder, 1)]  # 工具体只执行过一次
    assert (await sql(pg_uri, "SELECT count(*) FROM tickets"))[0][0] == 1
    taker_llm = [e["phase"] for e in events if e["event"] == "llm_call" and e["worker_id"] == taker]
    assert taker_llm == ["second"]  # 接手者只调了一次模型：从断点继续，没有从头再来
    assert (job.result["status"], job.result["output"]) == ("completed", "已建单：T-1001")
    final = await ckpt.get_run(f"job-{jid}")
    assert (final["writer"], final["status"], final["fence"]) == (taker, "completed", claims[1][1])


async def test_sigstopped_zombie_wakes_up_and_cannot_overwrite_the_result(pg_uri, shared, tmp_path):
    """SIGSTOP 冻结持有者（长 GC / 虚拟机挂起）：租约过期，另一个进程接手并完成。
    SIGCONT 之后僵尸从被冻结的那一行继续，模型调用返回、要把最终答案写进检查点 —— 被版本号 CAS 拒绝，结果不被覆盖。"""
    q, ckpt = shared
    gate = tmp_path / "gate"
    pool = WorkerPool(pg_uri, AGENT_APP, n=2, concurrency=2, lease=LEASE, poll=0.05, options={"gate": gate},
                      log_dir=tmp_path / "logs")
    async with running(pool):
        await eventually(lambda: started(pool) == 2, what="2 个 worker 进程启动")
        jid = await q.enqueue("run", {"op": "run", "input": "重置密码"}, tenant_id="acme")
        await eventually(lambda: at_gate(pool, jid), what="持有者停在第二次模型调用里")
        holder = (await q.get(jid)).worker_id
        zombie = index_of(pool, holder)
        pool.pause(zombie)  # SIGSTOP：心跳停了，它自己不知道
        job = await eventually(lambda: succeeded(q, jid), what="另一个进程接手并完成")
        before = await ckpt.get_run(f"job-{jid}")
        pool.resume(zombie)  # SIGCONT：手里还拿着过期的租约和版本号
        gate.touch()  # 它的模型调用返回，Agent 要把"最终答案"写进检查点
        lost = await eventually(lambda: events_of(pool, "ownership_lost", worker=holder, job=jid),
                                what="僵尸的写入被拒绝")
        assert pool.workers[zombie].alive  # 它没崩溃，只是放手了，接着领别的任务
        events = pool.events()
        logs = pool.logs()
    taker = job.worker_id
    assert taker != holder, logs
    assert "检查点冲突" in lost[0]["error"]  # 被版本号 CAS 拒绝（接手者 load 时把版本号 +1 了）
    assert not [e for e in events if e["event"] == "completed" and e["worker_id"] == holder]
    final = await q.get(jid)
    assert (final.status, final.worker_id, final.fence, final.result) == ("succeeded", taker, job.fence, job.result)
    after = await ckpt.get_run(f"job-{jid}")
    assert (after["version"], after["writer"], after["state"]) == (before["version"], taker, before["state"])
    assert await sql(pg_uri, "SELECT worker FROM ticket_calls") == [(holder,)]
    zombie_llm = [e["phase"] for e in events if e["event"] == "llm_call" and e["worker_id"] == holder]
    assert zombie_llm == ["first", "second"]  # 僵尸的第二次模型调用确实跑完了 —— 只是结果写不进去


async def test_sigterm_cancels_a_hanging_write_and_the_next_process_replays_it_with_the_same_key(pg_uri, shared, tmp_path):
    """SIGTERM 时一个写操作"下游已经执行、响应还没回来"：宽限期过后它被取消，检查点落盘为 cancelled，
    这个写调用**保持未回答**；进程正常退出。下一个进程接手后用同一个 call_id 重放 → 同一个幂等键 → 下游去重，只有一张工单。"""
    q, ckpt = shared
    opts = {"gate": tmp_path / "never", "hang": "tool"}
    jid = await q.enqueue("run", {"op": "run", "input": "3 楼打印机卡纸"}, tenant_id="acme")
    first = WorkerPool(pg_uri, AGENT_APP, n=1, lease=LEASE, poll=0.05, grace=0.5, options=opts, name="a",
                       log_dir=tmp_path / "logs")
    async with running(first):
        await eventually(lambda: _count(pg_uri, "SELECT count(*) FROM ticket_calls", None, 1),
                         what="下游已经建好工单、响应还在路上")
        first.terminate(0)  # SIGTERM：K8s 删除 Pod
        code = await asyncio.to_thread(first.workers[0].wait, 30)
    assert code == 0, first.logs()
    names = [e["event"] for e in first.events()]
    assert names.index("draining") < names.index("cancelled") < names.index("stopped")
    assert first.events("stopped")[0]["stats"]["cancelled"] == 1
    run = await ckpt.get_run(f"job-{jid}")
    last = run["state"]["messages"][-1]
    assert (run["status"], run["writer"]) == ("cancelled", "a0")
    assert last["role"] == "assistant" and last["tool_calls"], "写操作必须保持未回答，不能补“未执行”"
    held = await q.get(jid)
    assert (held.status, held.worker_id) == ("leased", "a0")  # 没有提交，也没有归还：等租约过期

    second = WorkerPool(pg_uri, AGENT_APP, n=1, lease=LEASE, poll=0.05, options=opts, name="b",
                        log_dir=tmp_path / "logs")
    async with running(second):
        job = await eventually(lambda: succeeded(q, jid), what="下一个进程接手并完成")
        events = second.events()
    calls = await sql(pg_uri, "SELECT key, worker, attempt FROM ticket_calls ORDER BY t")
    assert [(w, a) for _, w, a in calls] == [("a0", 1), ("b0", 2)]  # 工具体执行了两次（重放）……
    assert calls[0][0] == calls[1][0]  # ……用的是同一个幂等键
    tickets = await sql(pg_uri, "SELECT id FROM tickets")
    assert len(tickets) == 1  # 下游去重：只有一张工单
    assert job.result["output"] == f"已建单：T-{1000 + tickets[0][0]}"
    assert [e["phase"] for e in events if e["event"] == "llm_call"] == ["second"]  # 从断点继续


# ----------------------------------------------------------------------------- 网络分区（真实 TCP）


async def test_network_partition_isolated_worker_is_taken_over_and_its_late_writes_are_rejected(pg_tcp_uri, tmp_path):
    """两个 worker 进程都通过 TCP 连同一个 Postgres：far0 经过 TcpProxy，near0 直连。
    far0 持有任务时断网（进程本身活得好好的）→ 心跳发不出去 → 租约过期 → near0 从检查点接手并完成；
    网络恢复后 far0 的模型调用返回、要写检查点 —— 被 CAS 拒绝，任务结果、检查点都保持 near0 写的样子。"""
    info = conninfo_to_dict(pg_tcp_uri)
    q, ckpt = await prepare(pg_tcp_uri)
    gate = tmp_path / "gate"
    try:
        async with TcpProxy(info["host"], int(info["port"])) as proxy:
            far_url = f"postgresql://{info['user']}@127.0.0.1:{proxy.listen_port}/{info['dbname']}"
            common = dict(concurrency=1, lease=LEASE, poll=0.1, options={"gate": gate}, log_dir=tmp_path / "logs")
            far = WorkerPool(far_url, AGENT_APP, n=1, name="far", **common)
            near = WorkerPool(pg_tcp_uri, AGENT_APP, n=1, name="near", **common)
            async with running(far):
                await eventually(lambda: started(far) == 1, what="far0 启动")
                jid = await q.enqueue("run", {"op": "run", "input": "VPN 连不上"}, tenant_id="acme")
                await eventually(lambda: at_gate(far, jid), what="far0 执行完工具、停在第二次模型调用里")
                async with running(near):
                    await eventually(lambda: started(near) == 1, what="near0 启动")
                    assert (await q.get(jid)).worker_id == "far0"
                    assert proxy.cut() >= 2  # 分区：far0 的队列连接池、检查点连接池都被掐断
                    job = await eventually(lambda: succeeded(q, jid), what="near0 接手并完成")
                    hb_errors = events_of(far, "heartbeat_error", job=jid)
                    before = await ckpt.get_run(f"job-{jid}")
                    proxy.heal()
                    gate.touch()  # far0 的模型调用返回：它以为任务还归自己
                    lost = await eventually(lambda: events_of(far, "ownership_lost", job=jid),
                                            what="far0 的迟到写入被拒绝")
                    assert far.workers[0].alive  # 分区不是崩溃：它一直活着
                    far_events, near_events = far.events(), near.events()
                    logs = far.logs() + near.logs()
        final = await q.get(jid)
        after = await ckpt.get_run(f"job-{jid}")
    finally:
        await q.close()
        await ckpt.close()
    assert job.worker_id == "near0" and job.attempts == 2, logs
    assert hb_errors, "far0 的心跳应该因为断网失败（租约正是因此过期）"
    assert "检查点冲突" in lost[0]["error"]
    assert not [e for e in far_events if e["event"] == "completed"]
    assert (final.status, final.worker_id, final.fence) == ("succeeded", "near0", job.fence)
    assert final.result == job.result and job.result["output"] == "已建单：T-1001"
    assert (after["version"], after["writer"], after["state"]) == (before["version"], "near0", before["state"])
    assert await sql(pg_tcp_uri, "SELECT worker FROM ticket_calls") == [("far0",)]  # 工具只在断网前执行过一次
    assert [e["phase"] for e in near_events if e["event"] == "llm_call"] == ["second"]  # near0 从检查点继续
    assert [e["phase"] for e in far_events if e["event"] == "llm_call"] == ["first", "second"]
    assert proxy.stats["dropped"] >= 2

