"""agentkit.contrib.postgres 的测试：真实 Postgres（嵌入式）+ 多线程制造真实竞争。

覆盖：检查点的 CAS / fence 接管 / 并发写；队列的去重、SKIP LOCKED 并发领取、租约过期回收、fence 拒绝僵尸、
退避 / 死信 / redrive；worker 的心跳与优雅停机；AgentJobHandler 的审批 → resume、崩溃后从检查点恢复。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")

from agentkit import Agent, PermissionPolicy, RunState, ScriptedLLM, ToolContext, call_tool, reply, tool  # noqa: E402
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


# ----------------------------------------------------------------------------- 辅助


@pytest.fixture
def ckpt(pg_uri):
    c = PostgresCheckpointer(pg_uri)
    c.setup()
    yield c
    c.close()


@pytest.fixture
def queue(pg_uri):
    q = PostgresJobQueue(pg_uri, base_backoff=0.0)
    q.setup()
    yield q
    q.close()


def run_threads(n: int, target) -> None:
    """n 个线程在同一时刻开始执行 target(i)；任何线程的异常都在主线程重新抛出。"""
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
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "有线程卡住了"
    if errors:
        raise errors[0]


def expire_lease(pg_uri: str, job_id: int) -> None:
    """把租约改成"已经过期"：确定性地模拟 worker 崩溃 / 卡住，不用真的等。"""
    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("UPDATE agent_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))


def state_with(run_id: str, **meta) -> RunState:
    s = RunState(run_id=run_id, metadata=dict(meta))
    s.messages = [{"role": "user", "content": "你好"}]
    return s


# ============================================================================= 检查点


def test_setup_is_idempotent_even_when_many_processes_start_at_once(pg_uri):
    # 不加 advisory lock 时，8 个连接同时 CREATE TABLE IF NOT EXISTS，实测 7 个报 UniqueViolation
    def go(_i):
        q, c = PostgresJobQueue(pg_uri), PostgresCheckpointer(pg_uri)
        q.setup()
        c.setup()
        q.close()
        c.close()

    run_threads(8, go)
    PostgresJobQueue(pg_uri).setup()  # 再来一次也没事


def test_checkpoint_roundtrip_and_version_numbers(ckpt, pg_uri):
    s = state_with("r1", tenant_id="acme", user_id="u1")
    s.messages.append({"role": "tool", "tool_call_id": "c1", "content": "二进制\x00垃圾"})
    ckpt.save(s)
    assert ckpt.version_of("r1") == 1
    s.step = 3
    ckpt.save(s)
    assert ckpt.version_of("r1") == 2

    other = PostgresCheckpointer(pg_uri)  # 另一个进程
    loaded = other.load("r1")
    assert loaded.step == 3 and loaded.metadata == {"tenant_id": "acme", "user_id": "u1"}
    assert loaded.messages[1]["content"] == "二进制�垃圾"  # NUL 存不进 jsonb，被替换而不是整个写入失败
    assert other.version_of("r1") == 2
    assert other.load("nope") is None
    row = other.get_run("r1")
    assert (row["status"], row["tenant_id"], row["version"], row["user_id"]) == ("running", "acme", 2, "u1")
    other.close()


def test_stale_writer_gets_conflict_and_cannot_overwrite(ckpt, pg_uri):
    ckpt.save(state_with("r1", tenant_id="acme"))
    a, b = PostgresCheckpointer(pg_uri, writer="A"), PostgresCheckpointer(pg_uri, writer="B")
    sa, sb = a.load("r1"), b.load("r1")  # 两个 worker 读到同一个版本
    sa.output = "A 的结果"
    a.save(sa)
    sb.output = "B 基于旧版本的结果"
    with pytest.raises(CheckpointConflict) as e:
        b.save(sb)
    assert (e.value.expected, e.value.actual, e.value.writer) == (1, 2, "A")
    assert PostgresCheckpointer(pg_uri).load("r1").output == "A 的结果"  # 没有被覆盖


def test_creating_a_run_that_already_exists_is_a_conflict(ckpt, pg_uri):
    ckpt.save(state_with("r1"))
    other = PostgresCheckpointer(pg_uri)
    with pytest.raises(CheckpointConflict):
        other.save(state_with("r1"))  # 没 load 过就当成新 run 去写：说明别人已经创建了它


def test_plain_cas_is_first_writer_wins_but_fenced_takeover_makes_newest_holder_win(ckpt, pg_uri):
    # 纯 CAS：僵尸 Z 和新 worker N 读到同一版本，僵尸先写 → 僵尸赢，新 worker 冲突（没有丢更新，但赢家不对）
    ckpt.save(state_with("plain"))
    z, n = PostgresCheckpointer(pg_uri), PostgresCheckpointer(pg_uri)
    zs, ns = z.load("plain"), n.load("plain")
    z.save(zs)
    with pytest.raises(CheckpointConflict):
        n.save(ns)

    # 带 fence：新持有者 load 的那一刻就"接管"（version + 1），僵尸之后的写入全部冲突
    base = PostgresCheckpointer(pg_uri)
    old = base.fenced(1, writer="worker-A")
    old.save(state_with("fenced"))
    old_state = old.load("fenced")
    new = base.fenced(2, writer="worker-B")
    new_state = new.load("fenced")
    with pytest.raises(CheckpointConflict):
        old.save(old_state)  # 僵尸醒来继续写
    new.save(new_state)  # 新持有者照常写
    with pytest.raises(CheckpointConflict, match="fence"):
        base.fenced(1).load("fenced")  # 旧 fence 连读（接管）都被拒绝
    assert PostgresCheckpointer(pg_uri).get_run("fenced")["writer"] == "worker-B"


def test_concurrent_writers_never_lose_an_update(ckpt, pg_uri):
    ckpt.save(state_with("counter", n=0))
    conflicts = []

    def worker(i: int) -> None:
        mine = PostgresCheckpointer(pg_uri, writer=f"w{i}")
        for _ in range(10):
            while True:  # CAS 重试循环：每次都重新读最新版本
                s = mine.load("counter")
                s.metadata["n"] += 1
                try:
                    mine.save(s)
                    break
                except CheckpointConflict:
                    conflicts.append(i)
        mine.close()

    run_threads(8, worker)
    final = PostgresCheckpointer(pg_uri)
    assert final.load("counter").metadata["n"] == 80  # 80 次自增一次不丢
    assert final.version_of("counter") == 81


def test_list_runs_filters_by_status_and_tenant(ckpt):
    for i, (tenant, status) in enumerate([("acme", "paused"), ("acme", "completed"), ("globex", "paused")]):
        s = state_with(f"r{i}", tenant_id=tenant)
        s.status = status
        if status == "paused":
            s.pending = {"id": f"call_{i}", "name": "reset_password", "arguments": "{}"}
        ckpt.save(s)
    inbox = ckpt.list_runs(status="paused", tenant_id="acme")
    assert [r["run_id"] for r in inbox] == ["r0"]
    assert inbox[0]["pending"]["name"] == "reset_password"
    assert {r["run_id"] for r in ckpt.list_runs(status="paused")} == {"r0", "r2"}
    assert len(ckpt.list_runs(limit=2)) == 2


def test_works_with_any_pool_that_has_a_connection_method(pg_uri):
    class TinyPool:  # psycopg_pool.ConnectionPool 的最小替身：借出连接，退出时提交 / 回滚
        def __init__(self):
            self.borrowed = 0

        @contextmanager
        def connection(self):
            self.borrowed += 1
            with psycopg.connect(pg_uri) as conn:  # 非 autocommit，和连接池里的连接一样
                yield conn

    pool = TinyPool()
    c = PostgresCheckpointer(pool)
    c.setup()
    c.save(state_with("p1"))
    assert PostgresCheckpointer(pg_uri).load("p1").run_id == "p1"
    assert pool.borrowed >= 2


def test_agent_pauses_in_one_process_and_is_approved_in_another(ckpt, pg_uri):
    executed = []

    @tool(risk="dangerous")
    def reset_password(user: str) -> str:
        """重置用户密码"""
        executed.append(user)
        return f"{user} 的密码已重置"

    def make(checkpointer, script):
        return Agent(ScriptedLLM(script), [reset_password], checkpointer=checkpointer, hooks=[PermissionPolicy()])

    first = make(PostgresCheckpointer(pg_uri), [call_tool("reset_password", user="bob")])
    r = first.run("帮 bob 重置密码", run_id="r-approve", metadata={"tenant_id": "acme"})
    assert r.status == "paused" and executed == []

    second = make(PostgresCheckpointer(pg_uri), [reply("已重置")])  # 另一个进程里的另一个 Agent 实例
    r2 = second.approve("r-approve", True, by="alice")
    assert r2.status == "completed" and executed == ["bob"]
    assert ckpt.get_run("r-approve")["state"]["approval_log"][0]["by"] == "alice"


# ============================================================================= 队列


def test_enqueue_dedupes_by_idempotency_key_within_a_tenant(queue):
    a = queue.enqueue("agent", {"x": 1}, tenant_id="acme", idempotency_key="msg-1")
    assert queue.enqueue("agent", {"x": 2}, tenant_id="acme", idempotency_key="msg-1") == a  # 用户连点两次
    assert queue.enqueue("agent", {"x": 3}, tenant_id="globex", idempotency_key="msg-1") != a  # 别的租户不受影响
    assert queue.enqueue("agent", {}, tenant_id="acme") != queue.enqueue("agent", {}, tenant_id="acme")  # 不带 key 不去重
    assert queue.get(a).payload == {"x": 1}  # 重复提交不会改写第一次的内容
    assert queue.stats()["counts"]["queued"] == 4


def test_claim_respects_priority_run_at_and_kinds(queue):
    low = queue.enqueue("agent", {}, tenant_id="t")
    high = queue.enqueue("agent", {}, tenant_id="t", priority=10)
    later = queue.enqueue("agent", {}, tenant_id="t", priority=99, delay_seconds=60)
    other = queue.enqueue("report", {}, tenant_id="t", priority=50)
    assert queue.claim("w", kinds=["agent"]).id == high
    assert queue.claim("w", kinds=["agent"]).id == low
    assert queue.claim("w", kinds=["agent"]) is None  # later 还没到时间
    job = queue.claim("w")
    assert job.id == other and (job.status, job.attempts, job.fence, job.worker_id) == ("leased", 1, 1, "w")
    assert queue.get(later).status == "queued"


def test_concurrent_claims_never_hand_out_a_job_twice(queue, pg_uri):
    ids = {queue.enqueue("agent", {"i": i}, tenant_id="t") for i in range(60)}
    claimed: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        q = PostgresJobQueue(pg_uri)  # 每个 worker 自己的连接
        while (job := q.claim(f"w{i}", 30)) is not None:
            with lock:
                claimed.append(job.id)
        q.close()

    run_threads(8, worker)
    assert sorted(claimed) == sorted(ids), "有任务被领取了两次，或者有任务没被领取"


def test_skip_locked_skips_a_row_locked_by_someone_else_instead_of_waiting(queue, pg_uri):
    first = queue.enqueue("agent", {}, tenant_id="t")
    second = queue.enqueue("agent", {}, tenant_id="t")
    # lock_timeout：如果实现是在排队等行锁（少了 SKIP LOCKED），1 秒后直接报错，而不是靠计时来判断
    impatient = PostgresJobQueue(pg_uri, connect_kwargs={"options": "-c lock_timeout=1000"})
    with psycopg.connect(pg_uri) as holder:  # 另一个事务锁住了第一行，并且迟迟不提交
        holder.execute("SELECT id FROM agent_jobs WHERE id = %s FOR UPDATE", (first,))
        assert impatient.claim("w", 30).id == second  # 跳过被锁的行，而不是排队等锁
        holder.rollback()
    assert impatient.claim("w", 30).id == first
    impatient.close()


def test_expired_lease_is_reaped_and_zombie_is_fenced_out(queue, pg_uri):
    queue.enqueue("agent", {}, tenant_id="t")
    a = queue.claim("worker-A", 30)
    expire_lease(pg_uri, a.id)  # A 卡住了（GC / 虚拟机暂停），没来得及续租
    b = queue.claim("worker-B", 30)
    assert b.id == a.id and (b.fence, b.attempts, b.worker_id) == (a.fence + 1, 2, "worker-B")
    with pytest.raises(LeaseLost, match="fence"):
        queue.heartbeat(a, 30)
    with pytest.raises(LeaseLost):
        queue.complete(a, {"by": "A"})  # 僵尸的结果作废
    queue.complete(b, {"by": "B"})
    done = queue.get(a.id)
    assert (done.status, done.result, done.worker_id) == ("succeeded", {"by": "B"}, "worker-B")
    with pytest.raises(LeaseLost):
        queue.complete(b, {"by": "B again"})  # 重复提交也被拒绝


def test_late_completion_is_accepted_if_nobody_reaped_the_job(queue, pg_uri):
    queue.enqueue("agent", {}, tenant_id="t")
    job = queue.claim("w", 30)
    expire_lease(pg_uri, job.id)
    queue.complete(job, "迟到但有效")  # 租约过期了，但还没人回收 / 接手：fence 仍然是最新的
    assert queue.get(job.id).status == "succeeded"


def test_reaped_job_loses_ownership_even_before_anyone_reclaims_it(queue, pg_uri):
    queue.enqueue("agent", {}, tenant_id="t")
    job = queue.claim("w", 30)
    expire_lease(pg_uri, job.id)
    assert queue.reap_expired() == [(job.id, "queued")]
    with pytest.raises(LeaseLost, match="不再是 leased"):
        queue.complete(job, "太迟了")


def test_fail_backoff_dead_letter_and_redrive_keeps_fence_monotonic(pg_uri):
    q = PostgresJobQueue(pg_uri, max_attempts=2, base_backoff=60)
    q.setup()
    jid = q.enqueue("agent", {}, tenant_id="t")
    j1 = q.claim("w", 30)
    assert q.fail(j1, "429 Too Many Requests") == "queued"
    assert q.claim("w", 30) is None  # 退避中（最多 60 秒）
    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("UPDATE agent_jobs SET run_at = now() WHERE id = %s", (jid,))
    j2 = q.claim("w", 30)
    assert q.fail(j2, "又失败了") == "dead"  # 次数用尽 → 死信
    stats = q.stats()
    assert stats["counts"]["dead"] == 1 and stats["ready"] == 0
    assert q.redrive(jid) and not q.redrive(jid)  # 只能 redrive dead / failed
    j3 = q.claim("w", 30)
    assert (j3.attempts, j3.fence) == (1, 3)  # attempts 清零，fence 继续递增（绝不重置）


def test_non_retryable_failure_and_poison_message(queue, pg_uri):
    queue.enqueue("agent", {}, tenant_id="t")
    job = queue.claim("w", 30)
    assert queue.fail(job, "参数非法", retryable=False) == "failed"

    poison = queue.enqueue("agent", {"attachment": "20MB"}, tenant_id="t", max_attempts=1)
    p = queue.claim("w", 30)
    assert p.id == poison
    expire_lease(pg_uri, poison)  # worker 每次处理它都崩溃，根本走不到 fail()
    assert queue.reap_expired() == [(poison, "dead")]  # 计数发生在领取时，所以照样能进死信
    assert "没有按时续约" in queue.get(poison).last_error


def test_release_does_not_consume_an_attempt(queue):
    jid = queue.enqueue("agent", {}, tenant_id="t")
    job = queue.claim("w", 30)
    queue.release(job, reason="shutting down")
    again = queue.get(jid)
    assert (again.status, again.attempts, again.fence) == ("queued", 0, 1)
    assert queue.claim("w", 30).fence == 2
    with pytest.raises(LeaseLost):
        queue.release(job)


def test_stats_reports_oldest_ready_job_age(queue):
    from datetime import datetime, timedelta, timezone

    queue.enqueue("agent", {}, tenant_id="t", run_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    queue.enqueue("agent", {}, tenant_id="t", delay_seconds=600)  # 还没到时间的不算"在等"
    s = queue.stats()
    assert s["counts"]["queued"] == 2 and s["ready"] == 1
    assert 29 <= s["oldest_queued_age_s"] < 60


# ============================================================================= worker


def test_heartbeat_keeps_a_long_job_alive_while_another_worker_is_polling(queue, pg_uri):
    queue.enqueue("agent", {}, tenant_id="t")
    calls = []
    stop = threading.Event()

    def slow(job):
        calls.append(job.fence)
        time.sleep(2.0)  # 比租约（1.5 秒）长：没有心跳的话，另一个 worker 早就把它领走了
        return "ok"

    errors = []

    def worker(i: int) -> None:
        q = PostgresJobQueue(pg_uri)
        try:
            run_worker(q, slow, worker_id=f"w{i}", stop_event=stop, lease_seconds=1.5, heartbeat_interval=0.1,
                       poll_interval=0.05)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            q.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]  # 两个 worker 同时在抢
    for t in threads:
        t.start()
    deadline = time.time() + 10
    while queue.stats()["counts"]["succeeded"] == 0 and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(10)
    assert not errors, errors
    assert calls == [1], "任务被执行了不止一次：心跳没能保住租约"
    assert queue.stats()["counts"]["succeeded"] == 1


def test_graceful_stop_finishes_the_current_job_and_claims_no_more(queue):
    for i in range(5):
        queue.enqueue("agent", {"i": i}, tenant_id="t")
    stop = threading.Event()
    events = []

    def handler(job):
        stop.set()  # 相当于 K8s 发来了 SIGTERM
        time.sleep(0.2)  # 手头的任务照常做完
        return {"i": job.payload["i"]}

    stats = run_worker(queue, handler, worker_id="w", stop_event=stop, lease_seconds=5,
                       on_event=lambda name, info: events.append(name))
    assert stats["claimed"] == 1 and stats["succeeded"] == 1
    assert queue.stats()["counts"] == {"queued": 4, "leased": 0, "succeeded": 1, "failed": 0, "dead": 0}
    assert events[0] == "started" and events[-1] == "stopped" and "completed" in events


def test_worker_routes_handler_errors_to_the_right_queue_transition(queue):
    perm = queue.enqueue("agent", {"mode": "perm"}, tenant_id="t")
    later = queue.enqueue("agent", {"mode": "later"}, tenant_id="t")
    boom = queue.enqueue("agent", {"mode": "boom"}, tenant_id="t")

    def handler(job):
        mode = job.payload["mode"]
        if mode == "boom":
            raise ValueError("下游 500")
        if mode == "perm":
            raise PermanentJobError("参数非法")
        raise RetryLater(60, "rate_limited")

    stats = run_worker(queue, handler, worker_id="w", stop_event=threading.Event(), max_jobs=3, lease_seconds=5)
    assert (stats["failed"], stats["deferred"]) == (2, 1)
    assert queue.get(boom).status == "queued" and "ValueError" in queue.get(boom).last_error
    assert queue.get(perm).status == "failed"
    deferred = queue.get(later)
    assert (deferred.status, deferred.attempts) == ("queued", 0)  # 推迟不消耗重试次数


def test_sync_worker_uses_a_fixed_number_of_connections_however_many_jobs(queue, pg_uri):
    for i in range(20):
        queue.enqueue("agent", {"i": i}, tenant_id="t")
    seen = []
    count_sql = "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"

    def handler(job):
        time.sleep(0.03)  # 让心跳线程（间隔 0.01 秒）真的跑起来
        with psycopg.connect(pg_uri) as c:
            seen.append(c.execute(count_sql).fetchone()[0])
        return "ok"

    q = PostgresJobQueue(pg_uri)
    run_worker(q, handler, worker_id="w", stop_event=threading.Event(), max_jobs=20, heartbeat_interval=0.01)
    q.close()
    # 主线程 1 个 + 心跳线程 1 个 + fixture 的 1 个 + handler 自己的 1 个；不会随任务数增长
    assert max(seen) <= 4, seen
    with psycopg.connect(pg_uri) as c:
        assert c.execute(count_sql).fetchone()[0] <= 2  # 结束后心跳线程的连接已关闭（剩 fixture 的和这个）


# ============================================================================= AgentJobHandler


@tool(risk="dangerous")
def refund(order_id: str) -> str:
    """给订单退款"""
    REFUNDS.append(order_id)
    return f"订单 {order_id} 已退款"


REFUNDS: list[str] = []


def test_agent_job_pauses_for_approval_then_another_worker_resumes(queue, ckpt, pg_uri):
    REFUNDS.clear()
    scripts = {
        "w1": [call_tool("refund", order_id="A-1")],
        "w2": [reply("已为订单 A-1 退款")],
    }

    def handler_for(worker):
        def make_agent(checkpointer):
            return Agent(ScriptedLLM(scripts[worker]), [refund], checkpointer=checkpointer, hooks=[PermissionPolicy()])

        return AgentJobHandler(make_agent, PostgresCheckpointer(pg_uri))

    queue.enqueue("agent", {"op": "run", "input": "给 A-1 退款", "metadata": {"tenant_id": "evil", "user_id": "u1"}},
                  tenant_id="acme")
    run_worker(queue, handler_for("w1"), worker_id="w1", stop_event=threading.Event(), max_jobs=1, lease_seconds=5)
    job1 = queue.get(1)
    assert job1.status == "succeeded" and job1.result["awaiting_approval"] is True
    run_id, call_id = job1.result["run_id"], job1.result["pending"]["id"]
    inbox = ckpt.list_runs(status="paused", tenant_id="acme")  # tenant_id 以任务为准，payload 里的 "evil" 被覆盖
    assert [r["run_id"] for r in inbox] == [run_id] and REFUNDS == []

    payload = {"op": "resume", "run_id": run_id, "approvals": {call_id: True}, "by": "alice"}
    r1 = queue.enqueue("agent", payload, tenant_id="acme", idempotency_key=f"approve:{run_id}:{call_id}")
    r2 = queue.enqueue("agent", payload, tenant_id="acme", idempotency_key=f"approve:{run_id}:{call_id}")
    assert r1 == r2  # 审批人手抖点了两次：只入队一次
    run_worker(queue, handler_for("w2"), worker_id="w2", stop_event=threading.Event(), max_jobs=1, lease_seconds=5)
    assert queue.get(r1).result["status"] == "completed" and REFUNDS == ["A-1"]
    row = ckpt.get_run(run_id)
    assert row["writer"] == "w2" and row["state"]["approval_log"][0]["by"] == "alice"


def test_agent_job_resumes_from_checkpoint_after_a_crash(queue, ckpt, pg_uri):
    tickets = []

    @tool(risk="write")
    def create_ticket(title: str) -> str:
        """建工单"""
        tickets.append(title)
        return "T-1"

    class Crash(BaseException):  # 模拟 kill -9：什么异常处理都不走
        pass

    llm_calls = {"attempt1": 0, "attempt2": 0}

    def crash_on_second_call(messages):
        llm_calls["attempt1"] += 1
        raise Crash()

    def make_agent_1(checkpointer):
        return Agent(ScriptedLLM([call_tool("create_ticket", title="打印机卡纸"), crash_on_second_call]),
                     [create_ticket], checkpointer=checkpointer)

    def final(messages):
        llm_calls["attempt2"] += 1
        return reply("已建工单 T-1")

    def make_agent_2(checkpointer):
        return Agent(ScriptedLLM([final]), [create_ticket], checkpointer=checkpointer)

    queue.enqueue("agent", {"op": "run", "input": "打印机卡纸"}, tenant_id="acme")
    j1 = queue.claim("w1", 30)
    with pytest.raises(Crash):
        AgentJobHandler(make_agent_1, PostgresCheckpointer(pg_uri))(j1)
    expire_lease(pg_uri, j1.id)
    j2 = queue.claim("w2", 30)
    out = AgentJobHandler(make_agent_2, PostgresCheckpointer(pg_uri))(j2)
    assert out["status"] == "completed" and out["run_id"] == f"job-{j1.id}"
    assert tickets == ["打印机卡纸"]  # 工具结果已经在检查点里：不会重做
    assert llm_calls == {"attempt1": 1, "attempt2": 1}  # 第二次只调了一次模型，没有从头再来


def test_zombie_worker_checkpoint_write_is_rejected_after_takeover(queue, ckpt, pg_uri):
    frozen, resume_zombie = threading.Event(), threading.Event()

    @tool(risk="write")
    def create_ticket(title: str) -> str:
        """建工单"""
        if not frozen.is_set():
            frozen.set()
            resume_zombie.wait(10)  # 第一个 worker 在这里"卡住"（GC 停顿）
        return "T-1"

    def policy(messages):  # 拿到工具结果就回答，否则调用工具
        return reply("好了") if messages[-1]["role"] == "tool" else call_tool("create_ticket", title="x")

    def make_agent(checkpointer):
        return Agent(ScriptedLLM([policy, policy]), [create_ticket], checkpointer=checkpointer)

    queue.enqueue("agent", {"op": "run", "input": "打印机卡纸"}, tenant_id="acme")
    j1 = queue.claim("zombie", 30)
    outcome = {}

    def zombie():
        try:
            outcome["result"] = AgentJobHandler(make_agent, PostgresCheckpointer(pg_uri))(j1)
        except BaseException as e:  # noqa: BLE001
            outcome["error"] = e

    t = threading.Thread(target=zombie)
    t.start()
    assert frozen.wait(10)
    expire_lease(pg_uri, j1.id)
    j2 = queue.claim("new", 30)
    assert j2.fence == j1.fence + 1
    out = AgentJobHandler(make_agent, PostgresCheckpointer(pg_uri))(j2)
    queue.complete(j2, out)
    resume_zombie.set()  # 僵尸醒来，工具返回，Agent 要把结果写进检查点……
    t.join(10)
    assert isinstance(outcome.get("error"), CheckpointConflict), outcome
    assert ckpt.get_run(out["run_id"])["writer"] == "new"
    assert ckpt.get_run(out["run_id"])["status"] == "completed"


def test_resume_job_for_another_tenants_run_is_rejected(queue, ckpt, pg_uri):
    s = state_with("r-acme", tenant_id="acme")
    s.status = "paused"
    ckpt.save(s)
    queue.enqueue("agent", {"op": "resume", "run_id": "r-acme", "approvals": {}}, tenant_id="globex")
    job = queue.claim("w", 30)
    handler = AgentJobHandler(lambda c: Agent(ScriptedLLM([]), [], checkpointer=c), PostgresCheckpointer(pg_uri))
    with pytest.raises(PermanentJobError, match="属于租户 acme"):
        handler(job)


# ============================================================================= 异步版
#
# 用 asyncio.run 在普通测试函数里跑协程（不依赖 pytest-asyncio 插件）。


import asyncio  # noqa: E402
import importlib.util  # noqa: E402

# 异步版从连接串建池需要 psycopg_pool（pip install "psycopg[pool]"）；没装时只跳过异步部分
requires_pool = pytest.mark.skipif(importlib.util.find_spec("psycopg_pool") is None, reason="需要 psycopg_pool")

from agentkit.contrib.postgres import AsyncPostgresCheckpointer, AsyncPostgresJobQueue, run_async_worker  # noqa: E402


@requires_pool
def test_async_checkpointer_has_the_same_cas_and_fence_semantics(pg_uri):
    async def main():
        async with AsyncPostgresCheckpointer(pg_uri) as ckpt:
            await ckpt.setup()
            await ckpt.save(state_with("r1", tenant_id="acme"))
            a, b = AsyncPostgresCheckpointer(pg_uri, writer="A"), AsyncPostgresCheckpointer(pg_uri, writer="B")
            sa, sb = await a.load("r1"), await b.load("r1")
            await a.save(sa)
            with pytest.raises(CheckpointConflict):
                await b.save(sb)  # 基于旧版本的写入被拒绝

            old, new = ckpt.fenced(1, "w1"), ckpt.fenced(2, "w2")
            await old.save(state_with("f1"))
            stale = await old.load("f1")
            await new.load("f1")  # 新持有者接管
            with pytest.raises(CheckpointConflict):
                await old.save(stale)
            assert [r["run_id"] for r in await ckpt.list_runs(tenant_id="acme")] == ["r1"]
            assert (await ckpt.get_run("f1"))["writer"] == "w2"
            await a.close()
            await b.close()

    asyncio.run(main())


@requires_pool
def test_200_concurrent_async_claims_never_hand_out_a_job_twice(pg_uri):
    async def main():
        async with AsyncPostgresJobQueue(pg_uri, pool_kwargs={"min_size": 20, "max_size": 20}) as q:
            await q.setup()
            ids = await asyncio.gather(*(q.enqueue("agent", {"i": i}, tenant_id="t") for i in range(150)))
            jobs = await asyncio.gather(*(q.claim(f"task-{i}", 30) for i in range(200)))  # 200 个协程同时抢
            claimed = [j.id for j in jobs if j is not None]
            assert sorted(claimed) == sorted(ids), "有任务被领取了两次，或者有任务没被领取"
            assert sum(j is None for j in jobs) == 50

    asyncio.run(main())


async def _drain(pg_uri, n_jobs: int, handler, *, concurrency: int, pool_max: int = 20, **kw) -> tuple[float, dict]:
    """入队 n_jobs 个任务，用一个异步 worker 跑完，返回 (耗时, 统计)。"""
    async with AsyncPostgresJobQueue(pg_uri, pool_kwargs={"min_size": 2, "max_size": pool_max}) as q:
        await q.setup()
        for i in range(n_jobs):
            await q.enqueue("agent", {"i": i}, tenant_id="t")
        t0 = time.perf_counter()
        stats = await run_async_worker(q, handler, worker_id="w", stop_event=asyncio.Event(), concurrency=concurrency,
                                       max_jobs=n_jobs, poll_interval=0.02, grace_period=30, **kw)
        elapsed = time.perf_counter() - t0
        assert (await q.stats())["counts"]["succeeded"] == n_jobs
        return elapsed, stats


@requires_pool
def test_one_async_process_runs_16_jobs_at_a_time(pg_uri):
    in_flight = {"now": 0, "max": 0}
    full = None

    async def handler(job):
        nonlocal full
        full = full or asyncio.Event()
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        if in_flight["now"] >= 16:
            full.set()
        try:  # "等模型"：等到 16 个任务同时在等（或最多 5 秒），证明它们确实是并发的
            await asyncio.wait_for(full.wait(), 5)
        except asyncio.TimeoutError:
            pass
        in_flight["now"] -= 1
        return job.payload["i"]

    _, stats = asyncio.run(_drain(pg_uri, 64, handler, concurrency=16))
    assert stats["succeeded"] == 64
    # 一个进程里同时有 16 个任务在等 IO；背压保证永远不超过 concurrency
    assert in_flight["max"] == stats["max_in_flight"] == 16


@requires_pool
def test_connection_pool_smaller_than_concurrency_becomes_the_bottleneck(pg_uri):
    from psycopg_pool import AsyncConnectionPool

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
                        await asyncio.wait_for(crowd.wait(), 0.3)
                    except asyncio.TimeoutError:
                        pass
                    await conn.execute("SELECT 1")
                    holding["now"] -= 1
                return "ok"

            await _drain(pg_uri, 12, handler, concurrency=16)
            return {**holding, "queued": pool.get_stats().get("requests_queued", 0)}

    small = asyncio.run(run_with_pool(4))
    big = asyncio.run(_reset_and(pg_uri, run_with_pool, 16))
    # 池 4：并发度 16 被卡成 4，其余任务在池里排队；池 16：同时持有连接的任务超过 4 个，没有任何排队
    assert small["max"] == 4 and small["queued"] > 0, small
    assert big["max"] > 4 and big["queued"] == 0, big


async def _reset_and(pg_uri, fn, *args, **kwargs):
    """清空队列表再跑下一轮（同一个测试里对比两种配置）。"""
    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("TRUNCATE agent_jobs")
    return await fn(*args, **kwargs)


@requires_pool
def test_blocking_call_inside_an_async_handler_stalls_every_other_task(pg_uri):
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

    def sync_handler(job):
        enter()
        deadline = time.monotonic() + 2
        while sleeping["max"] < 2 and time.monotonic() < deadline:  # ✅ 放进线程池：别的任务可以同时进来
            time.sleep(0.005)
        time.sleep(0.02)
        leave()
        return "ok"

    asyncio.run(_drain(pg_uri, 16, blocking, concurrency=8))
    assert sleeping["max"] == 1, "阻塞调用期间事件循环是停住的，不可能有第二个任务同时在跑"
    sleeping["max"] = 0
    asyncio.run(_reset_and(pg_uri, _drain, pg_uri, 16, sync_handler, concurrency=8))
    assert sleeping["max"] >= 2, "同步 handler 应该被放进线程池并发执行"


@requires_pool
def test_async_shutdown_cancels_stragglers_after_grace_period_and_lets_leases_expire(pg_uri):
    async def main():
        async with AsyncPostgresJobQueue(pg_uri, base_backoff=0) as q:
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
                while progress["started"] < 4 or progress["quick_done"] < 2:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)  # 让两个快任务的提交完成
                stop.set()  # 相当于 K8s 发来 SIGTERM

            stats, _ = await asyncio.gather(
                run_async_worker(q, handler, worker_id="w", stop_event=stop, concurrency=4, grace_period=0.3,
                                 lease_seconds=30, poll_interval=0.02),
                sigterm_when_ready(),
            )
            assert (stats["succeeded"], stats["cancelled"]) == (2, 2)
            counts = (await q.stats())["counts"]
            assert (counts["succeeded"], counts["leased"]) == (2, 2)  # 被取消的任务没有提交、也没有归还
            with psycopg.connect(pg_uri, autocommit=True) as c:  # 租约自然过期后……
                c.execute("UPDATE agent_jobs SET lease_until = now() - interval '1 second' WHERE status = 'leased'")
            taken = [await q.claim("another-pod", 30), await q.claim("another-pod", 30)]
            assert all(j is not None and j.fence == 2 for j in taken)  # ……由别的 worker 接手

    asyncio.run(main())


@requires_pool
def test_async_heartbeat_keeps_a_long_job_alive_against_a_competing_worker(pg_uri):
    async def main():
        async with AsyncPostgresJobQueue(pg_uri) as q:
            await q.setup()
            await q.enqueue("agent", {}, tenant_id="t")
            calls, stop = [], asyncio.Event()

            async def slow(job):
                calls.append(job.fence)
                await asyncio.sleep(2.0)  # 比租约（1.5 秒）长，靠异步续租撑住
                stop.set()
                return "ok"

            common = dict(stop_event=stop, lease_seconds=1.5, heartbeat_interval=0.1, poll_interval=0.05, concurrency=2)
            await asyncio.gather(run_async_worker(q, slow, worker_id="a", **common),
                                 run_async_worker(q, slow, worker_id="b", **common))
            assert calls == [1] and (await q.stats())["counts"]["succeeded"] == 1

    asyncio.run(main())


# ----------------------------------------------------------------------------- 异步版 + agentkit.aio.AsyncAgent

aio = pytest.importorskip("agentkit.aio")


@requires_pool
def test_many_async_agents_share_one_pool_and_really_run_concurrently(pg_uri):
    from psycopg_pool import AsyncConnectionPool

    @tool
    def lookup(q: str) -> str:
        """查知识库"""
        return f"关于 {q} 的答案"

    def responder(messages):  # 按对话决定：先查一次，拿到结果就回答
        return reply("好的") if messages[-1]["role"] == "tool" else call_tool("lookup", q="VPN")

    async def main():
        async with AsyncConnectionPool(pg_uri, min_size=10, max_size=10, kwargs={"autocommit": True}) as pool:
            ckpt = AsyncPostgresCheckpointer(pool)  # 所有会话共用一个 10 连接的池
            await ckpt.setup()
            llm = aio.AsyncScriptedLLM(responder=responder, latency=0.3)
            agent = aio.AsyncAgent(llm, [lookup], checkpointer=ckpt)  # 一个 AsyncAgent 实例被 40 个会话并发复用
            results = await asyncio.gather(*(
                agent.run("VPN 连不上", run_id=f"s{i}", metadata={"tenant_id": "acme"}) for i in range(40)
            ))
            assert all(r.ok for r in results)
            # 同一时刻在途的模型调用数 > 连接数：等模型的时候不占数据库连接，10 个连接撑得起几十个会话
            assert llm.max_in_flight > 10, llm.max_in_flight
            rows = await ckpt.list_runs(status="completed", limit=100)
            assert len(rows) == 40 and all(r["version"] >= 4 for r in rows)  # 每个会话每一步都落盘了
            await agent.aclose()

    asyncio.run(main())


@tool(risk="dangerous")
def wire_transfer(amount: int) -> str:
    """转账"""
    TRANSFERS.append(amount)
    return f"已转账 {amount} 元"


TRANSFERS: list[int] = []


@requires_pool
def test_async_agent_job_pauses_then_resumes_on_another_async_worker(pg_uri):
    TRANSFERS.clear()

    def responder(messages):
        return reply("转账完成") if messages[-1]["role"] == "tool" else call_tool("wire_transfer", amount=500)

    async def main():
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(pg_uri, min_size=4, max_size=8, kwargs={"autocommit": True}) as pool:
            q = AsyncPostgresJobQueue(pool)  # 队列和检查点共用一个池
            ckpt = AsyncPostgresCheckpointer(pool)
            await q.setup()
            await ckpt.setup()

            def pod():  # 每个"Pod"一个共享的 AsyncAgent；每个任务通过 checkpointer= 传入带 fence 的视图
                agent = aio.AsyncAgent(aio.AsyncScriptedLLM(responder=responder), [wire_transfer],
                                       checkpointer=ckpt, hooks=[PermissionPolicy()])
                return AgentJobHandler(agent, ckpt)

            pod1, pod2 = pod(), pod()
            assert pod1.is_async
            await q.enqueue("agent", {"op": "run", "input": "给供应商转 500"}, tenant_id="acme")
            await run_async_worker(q, pod1, worker_id="pod-1", stop_event=asyncio.Event(), max_jobs=1)
            first = await q.get(1)
            assert first.result["awaiting_approval"] and TRANSFERS == []
            inbox = await ckpt.list_runs(status="paused", tenant_id="acme")
            run_id, call_id = inbox[0]["run_id"], inbox[0]["pending"]["id"]

            await q.enqueue("agent", {"op": "resume", "run_id": run_id, "approvals": {call_id: True}, "by": "cfo"},
                            tenant_id="acme", idempotency_key=f"approve:{run_id}:{call_id}")
            await run_async_worker(q, pod2, worker_id="pod-2", stop_event=asyncio.Event(), max_jobs=1)
            assert (await q.get(2)).result["status"] == "completed" and TRANSFERS == [500]
            row = await ckpt.get_run(run_id)
            assert row["writer"] == "pod-2" and row["state"]["approval_log"][0]["by"] == "cfo"

    asyncio.run(main())


@requires_pool
def test_one_shared_async_agent_serves_many_concurrent_jobs(pg_uri):
    def responder(messages):
        return reply("好的") if messages[-1]["role"] == "tool" else call_tool("lookup_kb", q="VPN")

    @tool
    def lookup_kb(q: str) -> str:
        """查知识库"""
        return "重启客户端"

    async def main():
        q = AsyncPostgresJobQueue(pg_uri)
        ckpt = AsyncPostgresCheckpointer(pg_uri)
        await q.setup()
        await ckpt.setup()
        llm = aio.AsyncScriptedLLM(responder=responder, latency=0.05)
        built = []

        def make_agent(checkpointer):  # 旧写法：异步版只会被调用一次，之后所有任务共用
            built.append(checkpointer)
            return aio.AsyncAgent(llm, [lookup_kb], checkpointer=checkpointer)

        handler = AgentJobHandler(make_agent, ckpt)
        for i in range(20):
            await q.enqueue("agent", {"op": "run", "input": f"VPN 连不上 #{i}"}, tenant_id="acme")
        stats = await run_async_worker(q, handler, worker_id="w", stop_event=asyncio.Event(), concurrency=8,
                                       max_jobs=20, poll_interval=0.02)
        assert stats["succeeded"] == 20
        assert len(built) == 1 and handler.agents_created == 1  # 一个 Agent（一个线程池）服务全部 20 个任务
        agent = handler._shared
        assert sum(type(h).__name__ == "_LeaseGuard" for h in agent.hooks) == 1
        assert llm.max_in_flight > 1  # 同一个 Agent 实例上真的有多个任务在并发
        rows = await ckpt.list_runs(limit=100)
        assert len(rows) == 20 and all(r["fence"] == 1 and r["writer"] == "w" for r in rows)  # 每个任务用的是自己的 fenced 视图
        await q.close()
        await ckpt.close()

    asyncio.run(main())


@requires_pool
def test_lease_guard_stops_only_the_job_whose_lease_was_lost(pg_uri):
    def responder(messages):
        return reply("完成")

    async def main():
        q = AsyncPostgresJobQueue(pg_uri)
        ckpt = AsyncPostgresCheckpointer(pg_uri)
        await q.setup()
        await ckpt.setup()
        agent = aio.AsyncAgent(aio.AsyncScriptedLLM(responder=responder, latency=0.05), checkpointer=ckpt)
        handler = AgentJobHandler(agent, ckpt)
        for i in range(2):
            await q.enqueue("agent", {"op": "run", "input": f"q{i}"}, tenant_id="acme")
        zombie, healthy = await q.claim("w", 30), await q.claim("w", 30)
        zombie.lost.set()  # 心跳发现这个任务的租约已经丢了
        results = await asyncio.gather(handler(zombie), handler(healthy), return_exceptions=True)
        assert isinstance(results[0], LeaseLost)  # 只有它停手（ContextVar：每个任务看到自己的 job）
        assert results[1]["status"] == "completed"
        await q.close()
        await ckpt.close()

    asyncio.run(main())


@requires_pool
def test_cancelled_run_is_left_to_expire_and_resumed_by_another_worker(pg_uri):
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

    async def main():
        q = AsyncPostgresJobQueue(pg_uri, base_backoff=0)
        ckpt = AsyncPostgresCheckpointer(pg_uri)
        await q.setup()
        await ckpt.setup()

        def pod():
            return AgentJobHandler(aio.AsyncAgent(aio.AsyncScriptedLLM(responder=responder), [search], checkpointer=ckpt), ckpt)

        await q.enqueue("agent", {"op": "run", "input": "报销流程是什么"}, tenant_id="acme")
        stop = asyncio.Event()

        async def sigterm_while_tool_hangs():
            while not executed:
                await asyncio.sleep(0.01)
            stop.set()

        stats, _ = await asyncio.gather(
            run_async_worker(q, pod(), worker_id="pod-1", stop_event=stop, grace_period=0.2, poll_interval=0.02),
            sigterm_while_tool_hangs(),
        )
        assert stats["cancelled"] == 1
        row = await ckpt.get_run("job-1")
        assert row["status"] == "cancelled" and row["writer"] == "pod-1"  # AsyncAgent 在取消时把检查点落盘
        # 只读工具被打断：补上"未执行"，resume 时由模型重新决定（只读，重做无害）
        assert row["state"]["messages"][-1]["content"].startswith("未执行")
        assert (await q.get(1)).status == "leased"  # 没有 complete，也没有归还

        with psycopg.connect(pg_uri, autocommit=True) as c:
            c.execute("UPDATE agent_jobs SET lease_until = now() - interval '1 second'")
        stats2 = await run_async_worker(q, pod(), worker_id="pod-2", stop_event=asyncio.Event(), max_jobs=1)
        assert stats2["succeeded"] == 1
        job = await q.get(1)
        assert (job.fence, job.result["status"], job.result["output"]) == (2, "completed", "答完了")
        assert (await ckpt.get_run("job-1"))["writer"] == "pod-2"
        assert executed == ["hang", "done"]
        await q.close()
        await ckpt.close()

    asyncio.run(main())


@requires_pool
def test_write_cancelled_at_shutdown_is_replayed_with_the_same_key_and_not_duplicated(pg_uri):
    """优雅停机取消了一个"下游已经执行、响应还没回来"的写操作：resume 必须用同一个 call_id 重放，由下游去重。"""
    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("CREATE TABLE tickets (id serial PRIMARY KEY, idempotency_key text UNIQUE, title text)")
    keys_seen: list[str] = []

    @tool(risk="write")
    async def create_ticket(title: str, ctx: ToolContext) -> str:
        """建工单（下游用幂等键的唯一约束去重）"""
        keys_seen.append(ctx.idempotency_key)
        async with await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as conn:
            cur = await conn.execute(
                "INSERT INTO tickets (idempotency_key, title) VALUES (%s, %s) ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING id", (ctx.idempotency_key, title))
            row = await cur.fetchone()
            if row is None:
                row = await (await conn.execute("SELECT id FROM tickets WHERE idempotency_key = %s",
                                                (ctx.idempotency_key,))).fetchone()
        if len(keys_seen) == 1:
            await asyncio.sleep(30)  # 第一次：下游已经建好了，响应还在路上时 worker 收到 SIGTERM
        return f"T-{1000 + row[0]}"

    def responder(messages):
        last = messages[-1]
        if last["role"] == "tool" and last["content"].startswith("T-"):
            return reply(f"已建工单 {last['content']}")
        return call_tool("create_ticket", title="打印机卡纸")

    async def main():
        q = AsyncPostgresJobQueue(pg_uri, base_backoff=0)
        ckpt = AsyncPostgresCheckpointer(pg_uri)
        await q.setup()
        await ckpt.setup()

        def pod():
            return AgentJobHandler(aio.AsyncAgent(aio.AsyncScriptedLLM(responder=responder), [create_ticket],
                                                  checkpointer=ckpt), ckpt)

        await q.enqueue("agent", {"op": "run", "input": "3 楼打印机卡纸"}, tenant_id="acme")
        stop = asyncio.Event()

        async def sigterm_after_side_effect():
            while not keys_seen:
                await asyncio.sleep(0.01)
            stop.set()

        stats, _ = await asyncio.gather(
            run_async_worker(q, pod(), worker_id="pod-1", stop_event=stop, grace_period=0.2, poll_interval=0.02),
            sigterm_after_side_effect(),
        )
        assert stats["cancelled"] == 1
        state = (await ckpt.get_run("job-1"))["state"]
        assert state["status"] == "cancelled"
        last = state["messages"][-1]
        assert last["role"] == "assistant" and last["tool_calls"], "写操作必须保持未回答，不能补“未执行”"

        with psycopg.connect(pg_uri, autocommit=True) as c:
            c.execute("UPDATE agent_jobs SET lease_until = now() - interval '1 second'")
        stats2 = await run_async_worker(q, pod(), worker_id="pod-2", stop_event=asyncio.Event(), max_jobs=1)
        assert stats2["succeeded"] == 1
        assert len(keys_seen) == 2 and keys_seen[0] == keys_seen[1]  # 同一个 call_id → 同一个幂等键
        with psycopg.connect(pg_uri) as c:
            assert c.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1  # 下游去重：只有一张工单
        assert (await q.get(1)).result["output"] == "已建工单 T-1001"
        await q.close()
        await ckpt.close()

    asyncio.run(main())
