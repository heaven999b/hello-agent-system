"""端到端测试：真实的进程（1 个 uvicorn API + 3 个 worker 子进程）、嵌入式 Postgres、fakeredis TCP 服务。离线、确定。

每条测试都"证明"而不是"跑通"：
- 取消用检查点里的 cancelled + 没有产生工单来证明；
- 接手用任务表里的 fence（全局递增）/ attempts、检查点的 writer、以及"副作用尝试"表里的 deduplicated 记录来证明；
- 跨队列 trace 用各进程导出的 span（JSONL）里的 trace_id 和父子关系来证明。

运行：.venv/bin/python -m pytest production/tests -v   （约 40–60 秒；依赖缺失时整个文件跳过）
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import pytest

for mod in ("psycopg", "psycopg_pool", "pgserver", "fakeredis", "redis", "fastapi", "uvicorn", "httpx",
            "prometheus_client", "opentelemetry.sdk", "cedarpy"):
    pytest.importorskip(mod)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402

from production.loadtest import ApiClient, parse_metrics, metric_sum  # noqa: E402
from production.run_local import LocalStack, http_get  # noqa: E402

# 测试用的时间参数：比生产短得多，故障注入后几秒内就能看到结果
E2E_ENV = {
    "WORKER_LEASE_SECONDS": "3",
    "WORKER_HEARTBEAT_SECONDS": "1",
    "WORKER_GRACE_SECONDS": "0.3",   # 宽限期 < 工具耗时：SIGTERM 时正在建单的任务一定会被取消 → 归还 → 被接手重放
    "TOOL_LATENCY_MS": "800",        # 工单已插入、响应还没返回的窗口
    "DIAGNOSTICS_LATENCY_MS": "1200",
    "STREAMS_PER_TENANT": "2",       # 舱壁：每个租户最多 2 条交互式流
}


@pytest.fixture(scope="module")
def stack():
    s = LocalStack(workers=3, concurrency=4, latency_ms=50, env=E2E_ENV)
    s.start()
    yield s
    s.stop()
    s.cleanup()


def run(coro):
    return asyncio.run(coro)


def db(stack, sql: str, params: tuple = ()):
    with psycopg.connect(stack.database_url, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


async def watch(c: ApiClient, who: str, run_id: str, stop, after: str | None = None, timeout: float = 30.0) -> list[dict]:
    """读 /v1/runs/{id}/events，直到 stop(event) 为真；流被服务端结束（例如暂停）就带 Last-Event-ID 重连。"""
    seen: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        headers = {"Last-Event-ID": after} if after else {}
        res = await asyncio.wait_for(c.sse("GET", f"/v1/runs/{run_id}/events", who, headers=headers, stop=stop),
                                     deadline - time.monotonic())
        seen += res.events
        if res.events:
            after = res.events[-1]["id"]
        if any(stop(e) for e in res.events):
            return seen
    raise TimeoutError(f"{run_id}: {[e['event'] for e in seen]}")


def is_event(name: str, **data):
    return lambda e: e.get("event") == name and all(e.get("data", {}).get(k) == v for k, v in data.items())


# ------------------------------------------------------------------------------------------ 基础


def test_health_readiness_and_metrics(stack):
    assert http_get(f"{stack.api_url}/healthz")[0] == 200
    code, body = http_get(f"{stack.api_url}/readyz")
    assert code == 200 and json.loads(body)["dependencies"] == {"postgres": "ok", "redis": "ok"}
    for w in stack.workers:  # worker 的探针由事件循环自己应答
        assert http_get(f"http://127.0.0.1:{w.health_port}/healthz")[0] == 200
        assert http_get(f"http://127.0.0.1:{w.health_port}/readyz")[0] == 200
        assert "itdesk_worker_job_events_total" in http_get(f"http://127.0.0.1:{w.metrics_port}/metrics")[1]
    assert http_get(f"{stack.api_url}/v1/runs/x")[0] == 401  # 没有 key


# ------------------------------------------------------------------------------------------ 后台任务 + 审批


def test_background_run_with_approval_end_to_end(stack):
    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            r = await c.submit("acme-alice", "我忘记密码了，帮我重置密码", idem="e2e-reset-1")
            assert r.status_code == 202
            run_id = r.json()["run_id"]
            again = await c.submit("acme-alice", "我忘记密码了，帮我重置密码", idem="e2e-reset-1")
            assert again.json()["run_id"] == run_id and again.json()["duplicate"]  # 同一个 Idempotency-Key 只建一个运行

            events = await watch(c, "acme-alice", run_id, is_event("awaiting_approval"))
            assert [e["event"] for e in events][:2] == ["accepted", "claimed"]
            state = await c.get_run("acme-alice", run_id)
            assert state["status"] == "paused" and state["pending"]["name"] == "reset_password"
            assert db(stack, "SELECT count(*) FROM it_password_resets WHERE run_id = %s", (run_id,))[0][0] == 0  # 批准前不执行

            assert (await c.approve("acme-alice", run_id)).status_code == 403  # 普通员工不能审批
            inbox = (await c.http.get("/v1/approvals", headers=c.h("acme-bob"))).json()
            assert run_id in [x["run_id"] for x in inbox]
            a, b = await asyncio.gather(c.approve("acme-bob", run_id), c.approve("acme-bob", run_id))  # 手抖点了两次
            assert a.status_code == b.status_code == 202 and a.json()["job_id"] == b.json()["job_id"]
            assert sorted([a.json()["duplicate"], b.json()["duplicate"]]) == [False, True]

            await watch(c, "acme-alice", run_id, is_event("completed"), after=events[-1]["id"])
            state = await c.get_run("acme-alice", run_id)
            assert state["status"] == "completed" and state["approval_log"][0]["by"] == "bob"
            assert db(stack, "SELECT count(*) FROM it_password_resets WHERE run_id = %s", (run_id,))[0][0] == 1
            jobs = db(stack, "SELECT payload->>'op', status FROM agent_jobs WHERE payload->>'run_id' = %s ORDER BY id", (run_id,))
            assert jobs == [("run", "succeeded"), ("resume", "succeeded")]  # 两次点击只入队了一个 resume
        finally:
            await c.aclose()

    run(body())


def test_approval_resume_is_not_refused_after_the_run_job_was_taken_over(stack):
    """本课压测发现、已在框架修复的问题的回归测试（讲义 3.6 发现 2）。

    修复前：fence 按任务各自从 1 数起，而检查点的 fence 保护整个 run。run 任务被别的 worker 接手过（fence=2）之后，
    审批产生的 resume 任务从 fence=1 开始，被检查点当成"旧持有者"拒绝，要空等一个租约才能继续。
    修复后：claim 从整张队列表共用的序列取 fence（nextval），全局单调 —— resume 任务的 fence 一定更大，第一次领取就接手。
    """
    from agentkit.contrib.postgres import AsyncPostgresJobQueue
    from production.service.identity import DEMO_IDENTITIES

    alice = DEMO_IDENTITIES["acme-alice"]
    run_id = f"r-takeover-{secrets.token_hex(4)}"

    async def crash_while_holding_the_run_job():
        # 先由"测试扮演的 worker"领取 run 任务，然后不提交、不续租（相当于 kill -9）
        db(stack, "INSERT INTO service_runs (run_id, tenant_id, user_id, mode) VALUES (%s, 'acme', 'alice', 'background')", (run_id,))
        q = AsyncPostgresJobQueue(stack.database_url)
        meta = {"user_id": "alice", "roles": alice["roles"], "tenant_plan": alice["plan"], "department": alice["department"]}
        job_id = await q.enqueue("held-by-test", {"op": "run", "run_id": run_id, "input": "我忘记密码了，帮我重置密码",
                                                  "metadata": meta}, tenant_id="acme")
        db(stack, "UPDATE service_runs SET job_id = %s WHERE run_id = %s", (job_id, run_id))
        job = await q.claim("crashed-worker", lease_seconds=60, kinds=["held-by-test"])
        assert job.id == job_id
        await q.close()
        # 租约到期：真正的 worker 回收它，用一个更大的 fence 接手
        db(stack, "UPDATE agent_jobs SET kind = 'agent', lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))
        return job_id, job.fence

    async def body():
        job_id, crashed_fence = await crash_while_holding_the_run_job()
        c = ApiClient(stack.api_url, stack.keys)
        try:
            events = await watch(c, "acme-alice", run_id, is_event("awaiting_approval"))
            claims = [e["data"]["fence"] for e in events if e["event"] == "claimed"]
            assert len(claims) == 1 and claims[0] > crashed_fence  # run 任务被接手（第二次领取）
            t0 = time.perf_counter()
            assert (await c.approve("acme-bob", run_id)).status_code == 202
            rest = await watch(c, "acme-alice", run_id, is_event("completed"), after=events[-1]["id"])
            return job_id, claims[0], rest, time.perf_counter() - t0
        finally:
            await c.aclose()

    job_id, takeover_fence, rest, approve_to_done = run(body())
    assert not any(e["event"] in ("ownership_lost", "fence_rejected", "retrying") for e in rest)
    (op, attempts, fence), = db(stack, "SELECT payload->>'op', attempts, fence FROM agent_jobs "
                                       "WHERE payload->>'run_id' = %s AND id <> %s", (run_id, job_id))
    assert (op, attempts) == ("resume", 1) and fence > takeover_fence  # 第一次领取就接手，没有空等一个租约
    assert db(stack, "SELECT fence FROM agent_runs WHERE run_id = %s", (run_id,))[0][0] == fence  # 检查点归最新的持有者
    assert approve_to_done < 2.5  # 租约是 3 秒：如果被拒绝过，至少要多等一个租约
    print(f"\n接手后的审批 → 完成：{approve_to_done:.2f}s（resume 任务第一次领取即接手，fence {takeover_fence} → {fence}）")
    assert db(stack, "SELECT count(*) FROM it_password_resets WHERE run_id = %s", (run_id,))[0][0] == 1


# ------------------------------------------------------------------------------------------ 交互式：断开即取消


def test_sse_disconnect_cancels_interactive_run_and_resume_finishes_it(stack):
    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            res = await c.sse("POST", "/v1/chat/stream", "acme-alice", json_body={"message": "VPN 老断线，帮我诊断一下"},
                              stop=is_event("tool_started", tool="run_diagnostics"))
            assert res.disconnected
            run_id = res.events[0]["data"]["run_id"]
            t0 = time.perf_counter()
            while (state := await c.get_run("acme-alice", run_id))["status"] != "cancelled":
                assert time.perf_counter() - t0 < 5, state
                await asyncio.sleep(0.02)
            cancel_ms = (time.perf_counter() - t0) * 1000
            await asyncio.sleep(1.5)  # 诊断本来要 1.2 秒：如果运行没被真的取消，接下来就会建单
            assert db(stack, "SELECT count(*) FROM it_tickets WHERE run_id = %s", (run_id,))[0][0] == 0
            assert (await c.get_run("acme-alice", run_id))["status"] == "cancelled"

            # 用户回来了：交给 worker 从检查点接着跑
            r = await c.http.post(f"/v1/runs/{run_id}/resume", headers=c.h("acme-alice"))
            assert r.status_code == 202
            await watch(c, "acme-alice", run_id, is_event("completed"))
            assert db(stack, "SELECT count(*) FROM it_tickets WHERE run_id = %s", (run_id,))[0][0] == 1
            print(f"\n断开 → 检查点记为 cancelled：{cancel_ms:.0f} ms")
        finally:
            await c.aclose()

    run(body())


# ------------------------------------------------------------------------------------------ 租户隔离


def test_tenant_isolation(stack):
    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            run_id = (await c.submit("acme-alice", "打印机卡纸，帮我提个工单")).json()["run_id"]
            await watch(c, "acme-alice", run_id, is_event("completed"))
            for who in ("globex-carol", "globex-erin"):  # 别的租户：连"存在"都不知道
                assert (await c.http.get(f"/v1/runs/{run_id}", headers=c.h(who))).status_code == 404
                assert (await c.http.get(f"/v1/runs/{run_id}/events", headers=c.h(who))).status_code == 404
            assert (await c.approve("globex-erin", run_id)).status_code == 404
            assert (await c.http.get(f"/v1/runs/{run_id}", headers=c.h("acme-bob"))).status_code == 200  # 同租户的 IT 管理员可见
            assert all(x["run_id"] != run_id for x in (await c.http.get("/v1/approvals", headers=c.h("globex-erin"))).json())
        finally:
            await c.aclose()
        return run_id

    run_id = run(body())

    # 纵深防御：就算有人绕过 API 直接往队列里塞一个"globex 恢复 acme 的 run"的任务，worker 也会拒绝（PermanentJobError）
    from agentkit.contrib.postgres import AsyncPostgresJobQueue

    async def forge():
        q = AsyncPostgresJobQueue(stack.database_url)
        job_id = await q.enqueue("agent", {"op": "resume", "run_id": run_id, "approvals": {}}, tenant_id="globex")
        for _ in range(100):
            job = await q.get(job_id)
            if job.status in ("failed", "succeeded", "dead"):
                break
            await asyncio.sleep(0.05)
        await q.close()
        return job

    job = run(forge())
    assert job.status == "failed" and "属于租户 acme" in job.last_error


# ------------------------------------------------------------------------------------------ 限流


def test_rate_limit_429_and_stream_bulkhead(stack):
    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            # 吵闹租户：1 个/秒、突发 2 → 连发 6 个，至少 3 个 429，而且都带 Retry-After
            rs = await asyncio.gather(*(c.submit("noisy-nick", f"VPN 怎么连 {i}") for i in range(6)))
            limited = [r for r in rs if r.status_code == 429]
            assert len(limited) >= 3 and all(int(r.headers["retry-after"]) >= 1 for r in limited)
            assert sum(r.status_code == 202 for r in rs) >= 1

            # 舱壁：globex 在本进程里最多 2 条交互式流，第 3 条立刻 429（而不是排队）
            opened = [asyncio.Event(), asyncio.Event()]
            release = asyncio.Event()

            async def hold(i):
                def on_event(e):
                    if e.get("event") == "accepted":
                        opened[i].set()
                    return release.is_set()

                return await c.sse("POST", "/v1/chat/stream", "globex-carol",
                                   json_body={"message": "ERP 很慢，帮我诊断一下"}, stop=on_event)

            holders = [asyncio.create_task(hold(i)) for i in range(2)]
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in opened)), 10)
            third = await c.sse("POST", "/v1/chat/stream", "globex-carol", json_body={"message": "你好"})
            release.set()
            await asyncio.gather(*holders)
            assert third.status == 429 and "对话流" in third.body and third.headers.get("retry-after") == "1"
            metrics = parse_metrics(await c.metrics())
            assert metric_sum(metrics, "itdesk_rate_limited_total", layer="stream_bulkhead") >= 1
            assert metric_sum(metrics, "itdesk_rate_limited_total", layer="api_bucket") >= 3
        finally:
            await c.aclose()

    run(body())


# ------------------------------------------------------------------------------------------ 跨队列 trace


def test_trace_continues_across_the_queue(stack):
    trace_id, parent = secrets.token_hex(16), secrets.token_hex(8)

    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            r = await c.http.post("/v1/runs", json={"message": "VPN 连不上"},
                                  headers=c.h("acme-alice", traceparent=f"00-{trace_id}-{parent}-01"))
            run_id = r.json()["run_id"]
            await watch(c, "acme-alice", run_id, is_event("completed"))
            return run_id
        finally:
            await c.aclose()

    run(body())
    spans = []
    for f in (stack.run_dir / "spans").glob("*.jsonl"):
        spans += [json.loads(line) for line in f.read_text().splitlines() if trace_id in line]
    by_name = {s["name"]: s for s in spans}
    send, process = by_name["send agent_jobs"], by_name["process agent_jobs"]
    assert send["service"] == "itdesk-api" and send["kind"] == "PRODUCER" and send["parent_id"] == parent
    assert process["service"] == "itdesk-worker" and process["kind"] == "CONSUMER" and process["parent_id"] == send["span_id"]
    assert send["pid"] != process["pid"]  # 真的跨了进程
    agent = next(s for s in spans if s["name"].startswith("invoke_agent"))
    assert agent["parent_id"] == process["span_id"]
    names = {s["name"] for s in spans}
    assert {"execute_tool search_kb", "execute_tool check_system_status"} <= names and any(n.startswith("chat") for n in names)


# ------------------------------------------------------------------------------------------ 故障注入


def test_kill_minus_9_worker_job_is_taken_over_without_duplicate_ticket(stack):
    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            run_id = (await c.submit("acme-alice", "VPN 老断线，帮我诊断一下并提工单")).json()["run_id"]
            events = await watch(c, "acme-alice", run_id, is_event("tool_started", tool="create_ticket"))
            victim_claim = [e for e in events if e["event"] == "claimed"][-1]["data"]
            victim_name = victim_claim["worker"]
            await asyncio.sleep(0.2)  # 工单已经插入，工具还在等下游响应（800ms）
            assert db(stack, "SELECT count(*) FROM it_tickets WHERE run_id = %s", (run_id,))[0][0] == 1
            t_kill = time.perf_counter()
            stack.kill_worker(stack.worker_by_name(victim_name))
            stack.start_worker()  # 相当于 ReplicaSet 补一个 Pod
            rest = await watch(c, "acme-alice", run_id, is_event("completed"), after=events[-1]["id"], timeout=30)
            takeover = next(e for e in rest if e["event"] == "claimed")
            return run_id, victim_claim, takeover, time.perf_counter() - t_kill
        finally:
            await c.aclose()

    run_id, victim, takeover, recovered_s = run(body())
    # fence 来自全局序列：接手者的 fence 一定比被杀的持有者大（具体数值取决于之前领取过多少次）
    assert takeover["data"]["worker"] != victim["worker"] and takeover["data"]["fence"] > victim["fence"]
    (status, fence, attempts, last_error), = db(stack, "SELECT status, fence, attempts, last_error FROM agent_jobs "
                                                       "WHERE payload->>'run_id' = %s", (run_id,))
    assert (status, attempts, fence) == ("succeeded", 2, takeover["data"]["fence"]) and "lease expired" in last_error
    assert db(stack, "SELECT writer FROM agent_runs WHERE run_id = %s", (run_id,))[0][0] == takeover["data"]["worker"]
    # 同一个 call_id 重放 → 同一个幂等键 → 数据库唯一约束挡住了第二张工单
    assert db(stack, "SELECT count(*) FROM it_tickets WHERE run_id = %s", (run_id,))[0][0] == 1
    outcomes = dict(db(stack, "SELECT outcome, count(*) FROM side_effect_attempts WHERE run_id = %s GROUP BY outcome", (run_id,)))
    assert outcomes == {"inserted": 1, "deduplicated": 1}
    stack.wait_ready()
    print(f"\nkill -9 → 另一个 worker 完成：{recovered_s:.1f}s（租约 3s）")


def test_rolling_restart_cancels_releases_and_replays_without_duplicates(stack):
    """滚动发布：新 worker 起来的同时，旧 worker 逐个收到 SIGTERM。

    宽限期（0.3s）比建单工具的下游耗时（0.8s）短，所以"正在建单"的任务一定会被取消：
    AsyncAgent 把检查点记为 cancelled（写工具保持未回答）→ worker 立刻归还任务 → 别的 worker 接手，
    用同一个 call_id 重放 create_ticket → 数据库唯一约束去重。"""
    old = list(stack.workers)

    async def body():
        c = ApiClient(stack.api_url, stack.keys)
        try:
            run_ids = [(await c.submit("globex-carol", f"ERP 很慢，帮我诊断一下并提工单 {i}")).json()["run_id"] for i in range(3)]
            surge = [stack.start_worker() for _ in old]  # 新版本的 worker 同时开始启动
            terminated: dict[str, asyncio.Task] = {}

            async def one(run_id):
                events = await watch(c, "globex-carol", run_id, is_event("tool_started", tool="create_ticket"))
                holder = [e for e in events if e["event"] == "claimed"][-1]["data"]["worker"]
                proc = stack.worker_by_name(holder)
                if proc in old and holder not in terminated:  # 正在建单的旧 worker 收到 SIGTERM
                    terminated[holder] = asyncio.create_task(asyncio.to_thread(stack.terminate_worker, proc, True, 30))
                return await watch(c, "globex-carol", run_id, is_event("completed"), after=events[-1]["id"], timeout=40)

            results = await asyncio.gather(*(one(r) for r in run_ids))
            await asyncio.to_thread(stack.wait_ready, 60, surge)
            for proc in old:  # 剩下的旧 worker 也依次退出，完成滚动
                if proc.name not in terminated:
                    terminated[proc.name] = asyncio.create_task(asyncio.to_thread(stack.terminate_worker, proc, True, 30))
            codes = await asyncio.gather(*terminated.values())
            return run_ids, results, codes
        finally:
            await c.aclose()

    run_ids, results, codes = run(body())
    assert codes == [0] * len(old)  # 全部优雅退出
    released = {e["data"].get("worker") for evs in results for e in evs if e["event"] == "released"}
    n_released = sum(e["event"] == "released" for evs in results for e in evs)
    assert n_released >= 1, "至少一个正在建单的任务在宽限期后被取消并归还"
    for run_id in run_ids:  # 每个运行恰好一张工单
        assert db(stack, "SELECT count(*) FROM it_tickets WHERE run_id = %s", (run_id,))[0][0] == 1
    dedup = db(stack, "SELECT count(*) FROM side_effect_attempts WHERE outcome = 'deduplicated' AND run_id = ANY(%s)", (run_ids,))[0][0]
    assert dedup >= 1  # 被取消的建单在接手后重放，并被唯一约束挡住
    # 主动归还不消耗重试次数：被归还、又被别人领取的任务，attempts 仍是 1，而 fence 换成了接手者更大的那个
    handed = [r for r, evs in zip(run_ids, results) if any(e["event"] == "released" for e in evs)]
    for r, evs in zip(run_ids, results):
        claims = [e["data"]["fence"] for e in evs if e["event"] == "claimed"]
        assert claims == sorted(claims) and len(set(claims)) == len(claims)  # 同一个 run 的每次领取 fence 严格递增
    rows = db(stack, "SELECT attempts FROM agent_jobs WHERE payload->>'run_id' = ANY(%s)", (handed,))
    assert 1 <= len(handed) <= n_released and all(a == 1 for (a,) in rows)  # 同一个 run 可能被归还不止一次
    stats = [json.loads(line)["stats"] for p in old for line in p.log_path.read_text().splitlines() if '"worker_stopped"' in line]
    assert len(stats) == len(old) and sum(s["cancelled"] for s in stats) == n_released
    assert released <= {p.name for p in old}
