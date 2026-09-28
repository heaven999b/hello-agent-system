"""第 26 课 Demo：第 13 课的队列 / 检查点接口，搬到多机后端（Postgres + Redis）上 —— 真实进程、真实故障、真实网络分区。

    python lessons/26_state_and_queues/demo.py --offline        # 离线：剧本模型（asyncio.sleep 模拟模型耗时），约 1 分钟
    python lessons/26_state_and_queues/demo.py                  # 真实模型：第 1、2 部分 7 个任务；第 3–5 部分仍用剧本模型
    python lessons/26_state_and_queues/demo.py --offline --only 3

所有 worker 都是真正的操作系统进程：WorkerPool 拉起 `python -m agentkit.distributed.worker --queue postgresql://... --app worker_app.py:make_handler`
—— 和第 13 课是同一条命令、同一个 run_worker、同一个 AgentJobHandler，只是 --queue 从 sqlite:/// 换成了 postgresql://。
基础设施全部在本机拉起，不需要 Docker：
    - Postgres：用 pgserver 自带的 initdb / pg_ctl 另起一个**监听 TCP（127.0.0.1）**的 Postgres 16 进程 ——
      worker 通过网络连接它，第 5 部分才能在 worker 和数据库之间插一个"可以拔网线"的 TCP 代理；
    - Redis：fakeredis 的 TCP 服务，单独跑在一个子进程里（Redis 协议 + Lua；不模拟持久化、主从切换、集群分片）。
缺少可选依赖时打印安装命令并以退出码 0 结束；某一部分的检查没通过时退出码为 1。

五个部分：
  1. 3 个 worker 进程 × 3 个租户 × 31 个任务：按租户限流（Redis Lua 令牌桶）、建工单（下游唯一约束做幂等）；
     中途 kill -9 一个 worker、SIGSTOP 冻结两个 worker 制造"僵尸"，最后统计：没有重复工单、fence 拒绝了僵尸的提交、
     检查点冲突被检测到；然后 SIGTERM 优雅停机。
  2. 审批：收件箱里查出等待审批的 run → 批准 → 入队 resume（重复点击只入队一次）→ 一个全新的 worker 进程恢复执行。
  3. 同一个 worker 应用、同一批任务：SQLite vs Postgres，1 个进程 × 并发 1 / 16、3 个进程 × 并发 16，以及连接池大小的影响。
  4. 优雅停机：SIGTERM 后宽限期内没做完的写操作被取消、保持"未回答"；另一个 worker 进程接手时用同一个 call_id 重放，
     幂等键不变，下游唯一约束去重 —— 工单不会建两张。
  5. 网络分区：两个 worker 进程，一个经过 TcpProxy 连数据库、一个直连。持有任务的那个断网（进程活得好好的）→
     心跳发不出去 → 租约过期 → 另一个接手并完成 → 网络恢复后，旧持有者的迟到写入被检查点的 CAS 拒绝。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
APP = f"{HERE / 'worker_app.py'}:make_handler"
INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'
REQUIRED = ("psycopg", "psycopg_pool", "redis", "pgserver", "fakeredis", "lupa")
LEASE = 2.0  # 第 1、4、5 部分的租约（秒）：心跳每 1/3 租约续一次；被杀 / 被冻结 / 断网的持有者最多 2 秒后可以被接手

MESSAGES = {
    "ticket": [
        "3 楼东区的打印机一直卡纸",
        "会议室 B 的投影仪没有信号",
        "工位网口插上网线没反应",
        "显示器上有一条竖线越来越明显",
        "笔记本电池鼓包了",
    ],
    "question": [
        "VPN 证书过期了怎么更新？",
        "怎么申请 Git 仓库权限？",
        "邮箱空间满了如何清理？",
        "如何连接 5 楼的访客 Wi-Fi？",
    ],
    "password": ["帮我重置一下 Jira 的密码，账号 zhang.san"],
}


def load_worker_app():
    from agentkit.testing import load_sibling

    return load_sibling(__file__, "worker_app")


# =====================================================================================
# 打印小工具
# =====================================================================================


def banner(title: str) -> None:
    print("\n" + "═" * 78 + f"\n  {title}\n" + "═" * 78, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, w: int) -> str:
    return text + " " * max(0, w - width(text))


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    info("".join(pad(h, w) for h, w in zip(headers, widths)))
    info("─" * sum(widths))
    for row in rows:
        info("".join(pad(str(c), w) for c, w in zip(row, widths)))


def short(text, n: int = 36) -> str:
    text = " ".join(str(text or "").split())
    return text if width(text) <= n else text[: n - 1] + "…"


async def wait_until(cond, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError(f"{timeout:.0f} 秒内没有等到{what}")


# =====================================================================================
# 基础设施：监听 TCP 的 Postgres、单独进程里的 fakeredis
# =====================================================================================


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def tcp_postgres(root: Path):
    """一个监听 127.0.0.1 的真实 Postgres。pgserver.get_server() 启动的实例只监听 unix socket（命令行参数 -h ""，
    改配置也打不开 TCP），所以这里用 pgserver 自带的同一套 initdb / pg_ctl 另起一个（和 tests/contrib/test_postgres.py 的 fixture 相同）。"""
    import pgserver

    data, port = root / "pgdata", free_port()
    pgserver.initdb(["--auth=trust", "--encoding=utf8", "-U", "postgres"], pgdata=data)
    opts = f'-h 127.0.0.1 -p {port} -k "" -c max_connections=500'
    pgserver.pg_ctl(["-w", "-o", opts, "-l", str(root / "postgres.log"), "start"], pgdata=data, timeout=60)
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        pgserver.pg_ctl(["-w", "-m", "fast", "stop"], pgdata=data, timeout=60)


@contextlib.contextmanager
def redis_process():
    """fakeredis 的 TCP 服务跑在单独的进程里：worker 进程和 demo 都通过 TCP 连它，和连一台真 Redis 一样。"""
    code = ("import time\nfrom agentkit.testing import fake_redis_server\n"
            "with fake_redis_server() as url:\n    print(url, flush=True)\n    while True:\n        time.sleep(3600)\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")]))}
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
    try:
        url = proc.stdout.readline().strip()
        if not url.startswith("redis://"):
            raise RuntimeError("fakeredis 服务没有启动成功")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


async def sql(dsn: str, query: str, params=None) -> list[tuple]:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(query, params)
        return (await cur.fetchall()) if cur.description else []


async def prepare(dsn: str):
    """建好队列表、检查点表、下游工单表（在拉起 worker 之前：很多进程同时建表会撞车），返回 demo 这一侧的队列和检查点。"""
    from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue

    wa = load_worker_app()
    queue, ckpt = PostgresJobQueue(dsn, base_backoff=0.2, max_backoff=2), PostgresCheckpointer(dsn)
    await queue.setup()
    await ckpt.setup()
    for ddl in wa.TICKETS_DDL:
        await sql(dsn, ddl)
    return queue, ckpt


# =====================================================================================
# 时间线：worker 进程打到标准输出的 JSON 事件
# =====================================================================================


class Timeline:
    """轮询 WorkerPool.events()，按时间打印新事件。事件通道是每个进程自己的日志文件，没有任何共享锁：
    一个 worker 被 SIGSTOP 冻结时，不会连累别的进程写事件（multiprocessing.Queue 的写锁就会）。"""

    def __init__(self, t0: float, verbose: bool = False):
        self.t0, self.verbose = t0, verbose
        self.seen: dict[str, int] = {}
        self.all: list[dict] = []

    def log(self, who: str, msg: str, ts: float | None = None) -> None:
        print(f"   [+{(ts or time.time()) - self.t0:5.1f}s] {pad(who, 10)}│ {msg}", flush=True)

    def poll(self, *pools) -> list[dict]:
        new = []
        for pool in pools:
            for w in pool.workers:
                evs = [e for e in w.events() if e.get("event") != "_log"]
                n = self.seen.get(str(w.log_path), 0)
                new.extend(evs[n:])
                self.seen[str(w.log_path)] = len(evs)
        new.sort(key=lambda e: e.get("t", 0))
        for e in new:
            self.all.append(e)
            self.show(e)
        return new

    def show(self, e: dict) -> None:
        name, who, ts = e["event"], e.get("worker_id", "?"), e.get("t")
        if name == "takeover" and (self.verbose or e["prev_status"] != "stopped"):
            # prev_status=stopped：前任因为限流把任务推迟了（RetryLater），这是正常的续跑，不是接手崩溃的任务
            self.log(who, f"接手任务 #{e['job']}（第 {e['attempts']} 次领取，fence={e['fence']}）：发现前任 {e['prev_writer']} "
                          f"的检查点（status={e['prev_status']}，第 {e['prev_step']} 步）→ 从断点继续", ts)
        elif name == "ticket":
            if e["created"]:
                self.log(who, f"🧾 建工单 {e['ticket']}（幂等键 {e['key']}）", ts)
            else:
                self.log(who, f"♻️  下游唯一约束命中：返回已有工单 {e['ticket']}，没有重复创建", ts)
        elif name == "chaos_window":
            msg = ("工单建好了，但结果还没写进检查点……" if e["stage"] == "after_ticket"
                   else "😵 Agent 跑完了，提交结果之前停顿（模拟 GC 停顿）")
            self.log(who, msg, ts)
        elif name == "woke":
            msg = ("醒了！工具返回，Agent 继续往检查点里写……" if e["stage"] == "after_ticket"
                   else "醒了！以为自己还持有租约，继续提交结果……")
            self.log(who, msg, ts)
        elif name == "fence_rejected":
            self.log(who, f"❌ 提交被拒绝（LeaseLost）：{short(e.get('error'), 60)}", ts)
        elif name == "heartbeat_rejected":
            self.log(who, f"💔 心跳被拒绝：任务 #{e['job']} 的 fence 已经过期，租约早就不是我的了", ts)
        elif name == "heartbeat_error":
            self.log(who, f"📡 心跳发不出去（任务 #{e['job']}）：{short(e.get('error'), 56)}", ts)
        elif name == "ownership_lost":
            kind = "检查点冲突（CheckpointConflict）" if "检查点冲突" in (e.get("error") or "") else "租约已丢失（LeaseLost）"
            self.log(who, f"❌ {kind}：{short(e.get('error'), 56)}", ts)
        elif name == "completed" and (self.verbose or (e.get("result") or {}).get("status") == "paused"):
            r = e.get("result") or {}
            tag = "⏸  等待审批" if r.get("status") == "paused" else "✅"
            self.log(who, f"{tag} 完成 #{e['job']}：{short(r.get('output'), 40)}", ts)
        elif self.verbose and name == "claimed":
            self.log(who, f"领取任务 #{e['job']}（第 {e.get('attempts')} 次尝试，fence={e.get('fence')}）", ts)
        elif self.verbose and name in ("deferred", "failed"):
            self.log(who, f"{name} #{e['job']} {short(e.get('error') or e.get('reason') or '', 50)}", ts)

    def count(self, name: str, **match) -> int:
        return sum(1 for e in self.all if e["event"] == name and all(e.get(k) == v for k, v in match.items()))


def index_of(pool, worker_id: str) -> int:
    return next(i for i, w in enumerate(pool.workers) if w.worker_id == worker_id)


# =====================================================================================
# 第 1 部分：3 个 worker 进程 × 3 个租户 —— 限流、幂等、kill -9、僵尸、优雅停机
# =====================================================================================


async def enqueue_batch(queue, offline: bool) -> dict[int, dict]:
    """入队：3 个租户，每个租户 10 个（真实模式 2 个）任务；再加一个需要审批的重置密码。返回 {job_id: 描述}。"""
    wa = load_worker_app()
    per_tenant = 10 if offline else 2
    chaos_plan = {("acme", 1): "kill_after_ticket", ("globex", 1): "freeze_mid_run", ("initech", 1): "freeze_before_complete"}
    jobs: dict[int, dict] = {}
    for i in range(per_tenant):
        for tenant in wa.TENANTS:
            kind = "ticket" if i % 2 == 0 else "question"
            pool = MESSAGES[kind]
            text = pool[(i // 2) % len(pool)]
            payload = {"op": "run", "input": text, "metadata": {"user_id": f"{tenant}-u{i}", "roles": ["employee"]}}
            if (tenant, i) in chaos_plan:
                payload["chaos"] = chaos_plan[(tenant, i)]
                payload["input"] = MESSAGES["ticket"][i % len(MESSAGES["ticket"])]
            jid = await queue.enqueue("agent", payload, tenant_id=tenant, idempotency_key=f"msg-{tenant}-{i}")
            jobs[jid] = {"tenant": tenant, "text": payload["input"], "chaos": payload.get("chaos")}
            if (tenant, i) == ("acme", 0):  # 需要审批的那个
                payload = {"op": "run", "input": MESSAGES["password"][0],
                           "metadata": {"user_id": "acme-zhang", "roles": ["employee"]}}
                jid = await queue.enqueue("agent", payload, tenant_id="acme", idempotency_key="msg-acme-pw")
                jobs[jid] = {"tenant": "acme", "text": payload["input"], "chaos": None}
    return jobs


def worker_options(args, redis_url: str, workdir: Path) -> dict:
    opts = {"redis": redis_url, "rl_wait": 1.0, "latency": 0.15}
    if not args.offline:  # 真实模型：本机所有 worker 共用 2 个模型并发名额（第 13 课的 SQLiteSemaphore，带租约）
        opts.update(llm="real", slots=2, slots_db=str(workdir / "model_slots.db"))
    return opts


async def part1(args, dsn: str, redis_url: str, workdir: Path) -> bool:
    import redis.asyncio as aredis

    from agentkit.distributed import WorkerPool

    wa = load_worker_app()
    banner("第 1 部分：3 个 worker 进程 × 3 个租户 —— 限流、幂等、kill -9、僵尸、优雅停机")
    queue, ckpt = await prepare(dsn)
    jobs = await enqueue_batch(queue, args.offline)
    n = len(jobs)
    step(f"入队 {n} 个任务（{len(wa.TENANTS)} 个租户；幂等键 msg-<租户>-<序号>，重复提交只入队一次）")
    dup = await queue.enqueue("agent", {"op": "run", "input": "重复提交"}, tenant_id="acme", idempotency_key="msg-acme-0")
    info(f"再用同一个幂等键 msg-acme-0 提交一次 → 返回已有的 job #{dup}，队列里仍然是 {(await queue.stats())['counts']['queued']} 个")
    for t, (plan, rate, cap) in wa.TENANTS.items():
        info(f"租户 {pad(t, 8)} {plan}套餐：模型调用 {rate:g} 次/秒，突发 {cap} 次（Redis 令牌桶，所有 worker 共享）")
    labels = {"kill_after_ticket": "建完工单后 kill -9", "freeze_mid_run": "建完工单后冻结（跑到一半的僵尸）",
              "freeze_before_complete": "跑完后、提交前冻结（提交阶段的僵尸）"}
    for jid, d in jobs.items():
        if d["chaos"]:
            info(f"故障注入：任务 #{jid}（{d['tenant']}）—— {labels[d['chaos']]}")

    concurrency = 2 if args.offline else 1
    if not args.offline:  # 先建好模型名额表：几个进程同时新建 SQLite 文件、切换 WAL 模式时会撞锁（第 13 课）
        from agentkit.distributed import SQLiteSemaphore

        slots = SQLiteSemaphore(workdir / "model_slots.db", "model", limit=2)
        await slots.setup()
        await slots.close()
    step(f"启动 3 个 worker 进程：python -m agentkit.distributed.worker --queue postgresql://… --concurrency {concurrency} "
         f"--lease {LEASE:g}（彼此不共享内存，只通过 Postgres 和 Redis 协作）")
    pool = WorkerPool(dsn, APP, n=3, concurrency=concurrency, lease=LEASE, poll=0.1, grace=10,
                      options=worker_options(args, redis_url, workdir), log_dir=workdir / "logs-part1", name="worker-")
    t0 = time.time()
    tl = Timeline(t0, args.verbose)
    chaos_log: list[str] = []
    handled: set[tuple] = set()
    thaws: set[asyncio.Task] = set()
    frozen: dict[int, set[int]] = {}  # 被冻结的进程 → 它在等别人做完的故障任务

    async def thaw_later(e: dict, idx: int) -> None:
        # 等别的 worker 接手并做完，再让僵尸醒来。fence 取自全局序列：只要被重新领取过，新 fence 一定比僵尸手里的大
        async def done_by_someone_else():
            job = await queue.get(e["job"])
            return job.fence > e["fence"] and job.status in ("succeeded", "failed", "dead")

        await wait_until(done_by_someone_else, 120, f"任务 #{e['job']} 被别人完成", interval=0.1)
        frozen[idx].discard(e["job"])
        if not frozen[idx]:  # 同一个进程可能因为两个任务被冻结：都被别人做完了才解冻
            pool.resume(idx)
            tl.log("调度器", f"▶️  SIGCONT {e['worker_id']}：任务 #{e['job']} 早已被别人完成，僵尸醒来")

    pool.start()
    try:
        deadline = time.monotonic() + (240 if args.offline else 600)
        last_report = time.monotonic()
        while time.monotonic() < deadline:
            for e in tl.poll(pool):
                if e["event"] != "chaos_window" or (e["job"], e["stage"]) in handled:
                    continue
                handled.add((e["job"], e["stage"]))
                idx = index_of(pool, e["worker_id"])
                if e["action"] == "kill":
                    await asyncio.to_thread(pool.kill, idx)  # SIGKILL：不释放租约、不写检查点、不留遗言
                    tl.log("调度器", f"💥 kill -9 {e['worker_id']}（pid {pool.workers[idx].pid}，退出码 {pool.workers[idx].returncode}）："
                                    "租约没还、检查点没写、不留遗言")
                    new = pool.add()  # 相当于 K8s 发现 Pod 挂了，拉起一个新的
                    tl.log("调度器", f"🔁 启动替补 {new.worker_id}（pid {new.pid}）")
                    chaos_log.append(f"kill -9 {e['worker_id']}（任务 #{e['job']}）")
                else:
                    pool.pause(idx)  # SIGSTOP：整个进程被冻结，它的心跳协程也停了，它自己不知道
                    frozen.setdefault(idx, set()).add(e["job"])
                    tl.log("调度器", f"🧊 SIGSTOP {e['worker_id']}：整个进程被冻结（心跳协程也停了），租约 {LEASE:g} 秒后过期")
                    chaos_log.append(f"SIGSTOP {e['worker_id']}（任务 #{e['job']}）")
                    task = asyncio.create_task(thaw_later(e, idx))
                    thaws.add(task)
                    task.add_done_callback(thaws.discard)
                    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # 取回异常，不留"没人取的异常"
            s = (await queue.stats())["counts"]
            done = s["succeeded"] + s["failed"] + s["dead"]
            if done >= n and not thaws:
                break
            if time.monotonic() - last_report > 3:
                tl.log("调度器", f"进度：完成 {done}/{n}，排队 {s['queued']}，执行中 {s['leased']}")
                last_report = time.monotonic()
            await asyncio.sleep(0.05)
        elapsed = time.time() - t0
        await asyncio.sleep(1.5)  # 让刚醒来的僵尸把它的遭遇（被拒绝的提交 / 检查点冲突）写完
        tl.poll(pool)
    finally:
        for t in thaws:
            t.cancel()
        await asyncio.gather(*thaws, return_exceptions=True)
        step("所有任务结束 → 给每个 worker 发 SIGTERM（优雅停机：不再领取，手头的做完再退出）")
        codes = await asyncio.to_thread(pool.stop)
    tl.poll(pool)
    info("退出码：" + "，".join(f"{w.worker_id}={c}" for w, c in zip(pool.workers, codes))
         + "（-9 = 被 kill -9；0 = 收到 SIGTERM 后正常退出）")

    r = aredis.Redis.from_url(redis_url)
    try:
        ok = await report_part1(queue, dsn, r, tl, jobs, chaos_log, elapsed)
    finally:
        await r.aclose()
        await queue.close()
        await ckpt.close()
    if not ok:
        print(pool.logs()[-4000:])
    return ok


async def report_part1(queue, dsn, r, tl: Timeline, jobs, chaos_log, elapsed) -> bool:
    wa = load_worker_app()
    step(f"📊 结果（{elapsed:.1f} 秒）")
    s = (await queue.stats())["counts"]
    (tickets, keys), = await sql(dsn, "SELECT count(*), count(DISTINCT idempotency_key) FROM tickets")
    multi = await sql(dsn, "SELECT id, attempts, fence FROM agent_jobs WHERE attempts > 1 ORDER BY id")
    per_tenant_done = dict(await sql(dsn, "SELECT tenant_id, EXTRACT(EPOCH FROM max(finished_at) - min(created_at))::float "
                                          "FROM agent_jobs GROUP BY tenant_id"))
    dedups = tl.count("ticket", created=False)
    info(f"任务：{s['succeeded']}/{len(jobs)} 成功，failed {s['failed']}，dead {s['dead']}；"
         f"被重新领取的：{'、'.join(f'#{i}（第 {a} 次尝试完成，fence={f}）' for i, a, f in multi) or '无'}；"
         f"因限流被推迟 {tl.count('deferred')} 次（不计入尝试次数）")
    info(f"工单：{tickets} 张，幂等键 {keys} 个 → 重复 {tickets - keys} 张 {'✅' if tickets == keys else '❌'}；"
         f"下游唯一约束挡下了 {dedups} 次重放")
    info(f"fence：拒绝了僵尸 worker 的 {tl.count('fence_rejected')} 次提交、{tl.count('heartbeat_rejected')} 次心跳 ✅")
    info(f"检查点 / 租约：{tl.count('ownership_lost')} 次\"所有权已转移\"（僵尸醒来后想写检查点被 CAS 拒绝，或心跳发现租约丢了主动停手）✅")
    info(f"故障注入：{'；'.join(chaos_log) or '无'}")
    step("按租户看限流效果（数据来自 Redis：限流 Hook 在每个进程里记账，进程被 kill 了数据也还在）")
    rows = []
    for t, (plan, rate, cap) in wa.TENANTS.items():
        h = {k.decode(): float(v) for k, v in (await r.hgetall(f"demo:rl:{t}")).items()}
        rows.append([t, f"{plan}（{rate:g}/s）", f"{int(h.get('calls', 0))}", f"{int(h.get('rejected', 0))}",
                     f"{h.get('waited_s', 0):.1f}s", f"{per_tenant_done.get(t, 0):.1f}s"])
    table(["租户", "套餐", "模型调用", "等不到→推迟", "等令牌总时长", "全部完成用时"], rows, [10, 16, 10, 14, 14, 14])
    info("推迟 = RateLimitHook 等了 1 秒还拿不到令牌 → StopRun(rate_limited) → AgentJobHandler 抛 RetryLater →")
    info("任务回到队列（不消耗重试次数），worker 去做别的租户的任务，而不是在这里干等。")
    return s["succeeded"] == len(jobs) and tickets == keys


# =====================================================================================
# 第 2 部分：审批 → 入队 resume → 一个全新的 worker 进程恢复执行
# =====================================================================================


async def part2(args, dsn: str, redis_url: str, workdir: Path) -> bool:
    from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue
    from agentkit.distributed import WorkerPool

    banner("第 2 部分：审批 → 入队 resume → 一个全新的 worker 进程恢复执行")
    queue, ckpt = PostgresJobQueue(dsn), PostgresCheckpointer(dsn)
    try:
        inbox = await ckpt.list_runs(status="paused")
        step("审批收件箱：ckpt.list_runs(status='paused')（按 (status, updated_at) 索引查询）")
        if not inbox:
            info("收件箱是空的：这次运行里模型没有发起需要审批的操作（真实模型不一定每次都调用 reset_password），跳过。")
            return True
        for row in inbox:
            p = row["pending"]
            info(f"run {row['run_id']}（租户 {row['tenant_id']}，用户 {row['user_id']}）等待审批：{p['name']}({p['arguments']})，"
                 f"最后写入者 {row['writer']}")
        row = inbox[0]
        run_id, call_id = row["run_id"], row["pending"]["id"]
        step("审批人 alice 点了“批准”——手抖点了两次；API 用 approve:<run_id>:<call_id> 作为幂等键入队 resume 任务")
        payload = {"op": "resume", "run_id": run_id, "approvals": {call_id: True}, "by": "alice", "comment": "已电话核实本人"}
        a = await queue.enqueue("agent", payload, tenant_id=row["tenant_id"], idempotency_key=f"approve:{run_id}:{call_id}")
        b = await queue.enqueue("agent", payload, tenant_id=row["tenant_id"], idempotency_key=f"approve:{run_id}:{call_id}")
        info(f"两次入队返回的 job_id：#{a}、#{b} → {'同一个任务 ✅' if a == b else '❌ 重复了'}")
        step("启动一个之前从没出现过的 worker 进程 fresh-0（状态全在 Postgres 里，任何进程都能接着跑）")
        pool = WorkerPool(dsn, APP, n=1, concurrency=2, lease=LEASE, poll=0.1, options=worker_options(args, redis_url, workdir),
                          log_dir=workdir / "logs-part2", name="fresh-")
        tl = Timeline(time.time(), verbose=True)
        pool.start()
        try:
            async def finished():
                tl.poll(pool)
                return (await queue.get(a)).status in ("succeeded", "failed", "dead")

            await wait_until(finished, 120 if args.offline else 300, f"resume 任务 #{a} 结束", interval=0.1)
        finally:
            await asyncio.to_thread(pool.stop)
        tl.poll(pool)
        job = await queue.get(a)
        final = await ckpt.get_run(run_id)
        info(f"resume 任务 #{a}：{job.status}；run {run_id} 现在是 {final['status']}，最后写入者 {final['writer']}")
        info(f"检查点的 fence：{row['fence']} → {final['fence']}（resume 是一个新任务，第一次领取就从全局序列拿到了更大的 fence，"
             f"fenced load 接管成功）")
        info(f"回复：{short(final['state'].get('output'), 60)}")
        for entry in final["state"]["approval_log"]:
            at = time.strftime("%H:%M:%S", time.localtime(entry["at"]))
            info(f"审批记录：{entry['by']} 于 {at} {'批准' if entry['approved'] else '拒绝'} {entry['tool']}（{entry['comment']}）")
        return a == b and job.status == "succeeded" and final["status"] == "completed"
    finally:
        await queue.close()
        await ckpt.close()


# =====================================================================================
# 第 3 部分：同一个 worker 应用，SQLite vs Postgres，1 × 1 / 1 × 16 / 3 × 16
# =====================================================================================

BENCH_LATENCY = 0.15  # 每次模型调用的模拟耗时（秒）：ScriptedLLM(latency=...)，asyncio.sleep，显式的延迟模型


async def measure(server_uri: str, workdir: Path, key: str, backend: str, procs: int, conc: int, n: int, *,
                  pool_size: int = 16, hold: bool = False) -> dict:
    from agentkit.distributed import SQLiteCheckpointer, SQLiteJobQueue, WorkerPool
    from agentkit.testing import create_database

    if backend == "postgres":
        from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue

        url = create_database(server_uri, f"bench_{key}")
        queue, ckpt = PostgresJobQueue(url), PostgresCheckpointer(url)
    else:
        path = workdir / f"bench_{key}.db"
        url = f"sqlite:///{path}"
        queue, ckpt = SQLiteJobQueue(path), SQLiteCheckpointer(path)
    await queue.setup()
    await ckpt.setup()  # 调度器先建好所有表（几个进程同时建表 / 同时把 SQLite 切到 WAL 模式会撞锁）
    await ckpt.close()
    for i in range(n):  # 先关着闸：任务带着很长的延迟入队，等所有 worker 进程都启动完毕再一起放开
        await queue.enqueue("agent", {"op": "run", "input": f"VPN 证书过期了怎么更新？#{i}"}, tenant_id="acme",
                            delay_seconds=3600)
    options = {"latency": BENCH_LATENCY, "llm_events": "1", "pool": pool_size}
    if hold:
        options["hold"] = "1"
    pool = WorkerPool(url, APP, n=procs, concurrency=conc, lease=30, poll=0.02, grace=10, options=options,
                      log_dir=workdir / f"logs-bench-{key}", name=f"{backend[:2]}-")
    peak_conn = 0
    sampling = asyncio.Event()

    async def sample_connections(dbname: str) -> None:  # 每 20 毫秒数一次这个库上的连接数（不含采样用的这一个）
        nonlocal peak_conn
        import psycopg

        q = "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()"
        async with await psycopg.AsyncConnection.connect(server_uri, autocommit=True) as conn:
            while not sampling.is_set():
                peak_conn = max(peak_conn, (await (await conn.execute(q, (dbname,))).fetchone())[0])
                await asyncio.sleep(0.02)

    pool.start()
    sampler = asyncio.create_task(sample_connections(url.rsplit("/", 1)[1].split("?")[0])) if backend == "postgres" else None
    try:
        await wait_until(lambda: len(pool.events("started")) == procs, 60, f"{procs} 个 worker 进程启动")
        if backend == "postgres":
            (gate,), = await sql(url, "WITH u AS (UPDATE agent_jobs SET run_at = now() RETURNING run_at) "
                                      "SELECT min(run_at) FROM u")  # 开闸：所有任务同时变成可领取
        else:
            import sqlite3

            gate = time.time()
            conn = sqlite3.connect(path, timeout=30)
            conn.execute("UPDATE agent_jobs SET run_at = ?", (gate,))
            conn.commit()
            conn.close()

        async def all_done():
            return (await queue.stats())["counts"]["succeeded"] >= n

        await wait_until(all_done, 300, f"{n} 个任务全部完成", interval=0.02)
    finally:
        sampling.set()
        if sampler is not None:
            await asyncio.gather(sampler, return_exceptions=True)
        await asyncio.to_thread(pool.stop)
    if backend == "postgres":
        (elapsed,), = await sql(url, "SELECT EXTRACT(EPOCH FROM max(finished_at) - %s)::float FROM agent_jobs", (gate,))
    else:
        conn = sqlite3.connect(path, timeout=30)
        elapsed = conn.execute("SELECT max(finished_at) FROM agent_jobs").fetchone()[0] - gate
        conn.close()
    await queue.close()
    # 所有进程的模型调用区间（同一台机器、同一个时钟）→ 扫描线求"同一时刻在途的模型调用数"的峰值
    marks = sorted([(e["t0"], 1) for e in pool.events("llm")] + [(e["t1"], -1) for e in pool.events("llm")])
    in_flight = peak_llm = 0
    for _, d in marks:
        in_flight += d
        peak_llm = max(peak_llm, in_flight)
    return {"elapsed": elapsed, "rate": n / elapsed, "llm_peak": peak_llm, "conn_peak": peak_conn, "n": n,
            "calls": len(pool.events("llm"))}


async def part3(args, server_uri: str, workdir: Path) -> bool:
    banner("第 3 部分：同一个 worker 应用、同一条命令 —— 只换 --queue：SQLite vs Postgres，加并发、加进程")
    info(f"每个任务调 2 次模型（ScriptedLLM(latency={BENCH_LATENCY})：每次 asyncio.sleep {BENCH_LATENCY} 秒，显式的延迟模型）"
         "+ 1 次只读工具 + 约 5 次检查点写入。")
    if not args.offline:
        info("（真实模式下这一部分也用剧本模型：它测的是 worker 架构，不是模型速度。）")
    info("每组都是真实的 worker 进程（WorkerPool）；计时从\"开闸\"算起（任务先带着很长的延迟入队，进程都启动完再一起放开）。")
    variants = [
        ("SQLite", "sqlite", 1, 1, 16, {}),
        ("Postgres", "postgres", 1, 1, 16, {}),
        ("Postgres", "postgres", 1, 16, 64, {}),
        ("Postgres", "postgres", 3, 16, 96, {}),
        ("SQLite", "sqlite", 3, 16, 96, {}),
        ("SQLite", "sqlite", 8, 64, 512, {}),
        ("Postgres", "postgres", 8, 64, 512, {}),
        ("Postgres · 检查点池 4", "postgres", 1, 16, 64, {"pool_size": 4}),
        ("Postgres · 等模型时占着连接", "postgres", 1, 16, 64, {"hold": True}),
    ]
    rows, results = [], []
    for i, (label, backend, procs, conc, n, extra) in enumerate(variants):
        r = await measure(server_uri, workdir, str(i), backend, procs, conc, n, **extra)
        results.append(r)
        ideal = procs * conc / (2 * BENCH_LATENCY)
        pool_label = "-" if backend == "sqlite" else ("4（业务库）" if extra.get("hold") else str(extra.get("pool_size", 16)))
        rows.append([label, f"{procs} × {conc}", str(n), pool_label, str(r["conn_peak"]) if backend == "postgres" else "-",
                     f"{r['elapsed']:.2f}s", f"{r['rate']:.1f}", f"{ideal:.0f}", str(r["llm_peak"])])
        info(f"{pad(label, 30)} {procs} × {conc:<3} {n} 个任务 → {r['elapsed']:.2f}s，{r['rate']:.1f} 任务/秒")
    step("对比")
    table(["后端", "进程×并发", "任务数", "检查点池", "连接峰值", "耗时", "任务/秒", "理论上限", "在途模型调用峰值"],
          rows, [30, 11, 8, 12, 10, 9, 9, 10, 16])
    info("理论上限 = 同时在跑的任务数 ÷ 每个任务等模型的 0.3 秒。在途模型调用峰值：把所有进程的模型调用区间放在一起数出来的。")
    info("连接峰值 = 这个库上同时打开的连接数（含 worker 的队列池、检查点池、工单库池，不含采样用的那一个）。")
    info("观察：① 1 × 1 时时间几乎全花在等模型上，换哪种后端都一样；")
    info("② 同一个进程里并发 16（asyncio），吞吐跟着涨，在途模型调用峰值就是 16；再加进程（3 × 16）乘上去；")
    info("③ 同一台机器上，SQLite 和 Postgres 一直到 8 × 64 都差不多：这时的瓶颈是这台机器本身（8 个 worker 进程 + 数据库抢 CPU），")
    info("   不是哪个数据库的锁。Postgres 换来的不是单机更快，而是多台机器能连同一个队列 —— 而连接数随进程数一起涨（看连接峰值）；")
    info("④ 检查点池只有 4 个连接也够 16 路并发：检查点写入只借连接几毫秒；")
    info("⑤ 但如果每个任务在等模型时一直占着连接（比如开着事务调模型），并发就被卡成连接数（这里是 4）。")
    return all(r["calls"] == 2 * r["n"] for r in results)


# =====================================================================================
# 第 4 部分：优雅停机 —— 被取消的写操作，另一个进程 resume 后不重复
# =====================================================================================

SHUTDOWN_TITLES = ["3 楼打印机卡纸", "投影仪没信号", "网口没反应", "显示器有竖线", "笔记本电池鼓包", "耳机麦克风没声音"]


async def part4(args, server_uri: str, workdir: Path) -> bool:
    from agentkit.distributed import WorkerPool
    from agentkit.testing import create_database

    banner("第 4 部分：优雅停机 —— SIGTERM 时被取消的写操作，另一个进程 resume 后不重复")
    info("场景：pod-a 进程里同时在跑 6 个建工单任务。其中一个任务的下游已经把工单建好了，响应还在路上，")
    info("这时 pod-a 收到 SIGTERM。宽限期（0.5 秒）到了它还没回来，只能取消。")
    if not args.offline:
        info("（真实模式下这一部分也用剧本模型：要确定地卡在“下游已执行、响应未返回”这一刻。）")
    dsn = create_database(server_uri, "shutdown")
    queue, ckpt = await prepare(dsn)
    hang_jid = None
    for i, title in enumerate(SHUTDOWN_TITLES):
        payload = {"op": "run", "input": title}
        if title.startswith("笔记本"):
            payload["hang"] = True
        jid = await queue.enqueue("agent", payload, tenant_id="acme", idempotency_key=f"shutdown-{i}")
        hang_jid = jid if payload.get("hang") else hang_jid
    opts = {"latency": 0.1}
    pod_a = WorkerPool(dsn, APP, n=1, concurrency=8, lease=LEASE, poll=0.05, grace=0.5, options=opts,
                       log_dir=workdir / "logs-part4", name="pod-a")
    pod_b = WorkerPool(dsn, APP, n=1, concurrency=8, lease=LEASE, poll=0.05, grace=0.5, options=opts,
                       log_dir=workdir / "logs-part4", name="pod-b")
    try:
        step("pod-a 启动：一个进程、并发 8，租约 2 秒，宽限期 0.5 秒（--grace 0.5）")
        pod_a.start()
        await wait_until(lambda: pod_a.events("downstream_done"), 60, "下游建好工单、响应还在路上")
        info("K8s 发来 SIGTERM：pod-a 不再领取新任务，等在途任务最多 0.5 秒")
        pod_a.terminate(0)
        code = await asyncio.to_thread(pod_a.workers[0].wait, 30)
        stats = pod_a.events("stopped")[0]["stats"]
        names = [e["event"] for e in pod_a.events()]
        order_ok = names.index("draining") < names.index("cancelled") < names.index("stopped")
        info(f"pod-a 退出（退出码 {code}）：完成 {stats['succeeded']} 个，宽限期后取消 {stats['cancelled']} 个（不提交、不归还）；"
             f"事件顺序 draining → cancelled → stopped {'✅' if order_ok else '❌'}")
        run = await ckpt.get_run(f"job-{hang_jid}")
        last = run["state"]["messages"][-1]
        pending = [tc["id"] for tc in last.get("tool_calls") or []]
        held = await queue.get(hang_jid)
        info(f"run job-{hang_jid} 的检查点：status={run['status']}，最后一条是 assistant 的写工具调用 {pending}，"
             "没有补“未执行”（保持未回答）")
        info(f"任务 #{hang_jid} 在队列里：status={held.status}，持有者 {held.worker_id}（没提交也没归还，等租约自然过期）")

        step(f"另一个进程 pod-b 启动；{LEASE:g} 秒租约过期后它领到任务 #{hang_jid}，从检查点接着跑")
        pod_b.start()

        async def taken_over():
            return (await queue.get(hang_jid)).status == "succeeded"

        await wait_until(taken_over, 60, f"pod-b 接手并完成任务 #{hang_jid}")
    finally:
        await asyncio.gather(asyncio.to_thread(pod_a.stop), asyncio.to_thread(pod_b.stop))
    job = await queue.get(hang_jid)
    calls = await sql(dsn, "SELECT worker, key, created, attempt FROM ticket_calls WHERE run_id = %s ORDER BY t",
                      (f"job-{hang_jid}",))
    step("📊 结果")
    for worker, key, created, attempt in calls:
        info(f"{worker}（第 {attempt} 次领取）执行 create_ticket，幂等键 {key} → {'新建' if created else '唯一约束命中，返回已有工单'}")
    same = len({k for _, k, _, _ in calls}) == 1
    info(f"两次执行用的是{'同一个' if same else '不同的'}幂等键 {'✅' if same else '❌'}")
    (n, distinct), = await sql(dsn, "SELECT count(*), count(DISTINCT run_id) FROM tickets")
    info(f"工单 {n} 张，对应 {distinct} 个 run → {'没有重复 ✅' if n == distinct else '有重复 ❌'}；"
         f"任务 #{hang_jid} 最终由 {job.worker_id} 完成：{short(job.result.get('output'), 40)}")
    info("原理：写工具在取消时保持“未回答”，resume 重放的是检查点里的同一个 tool_call（同一个 call_id），")
    info("幂等键 = run_id:call_id 不变，下游唯一约束把第二次执行变成“返回已有结果”。")
    info("如果取消时给它补上“未执行”，模型在 resume 后会重新发起一个 call_id 不同的调用，幂等键变了，工单就会建两张。")
    await queue.close()
    await ckpt.close()
    return code == 0 and order_ok and same and len(calls) == 2 and n == distinct == len(SHUTDOWN_TITLES)


# =====================================================================================
# 第 5 部分：网络分区（真实 TCP）
# =====================================================================================


async def part5(args, server_uri: str, workdir: Path) -> bool:
    from psycopg.conninfo import conninfo_to_dict

    from agentkit.distributed import TcpProxy, WorkerPool
    from agentkit.testing import create_database

    banner("第 5 部分：网络分区 —— worker 活着，但连不上数据库")
    info("两个 worker 进程都通过 TCP 连同一个 Postgres：far-0 经过 TcpProxy（可以随时“拔网线”），near-0 直连。")
    info("没有 mock 任何网络调用：TcpProxy 真实地转发 TCP 字节流，cut() 真实地重置连接。")
    if not args.offline:
        info("（真实模式下这一部分也用剧本模型：要确定地卡在“工具已执行、模型调用进行中”这一刻再断网。）")
    dsn = create_database(server_uri, "partition")
    queue, ckpt = await prepare(dsn)
    info_ = conninfo_to_dict(dsn)
    gate = workdir / "partition.gate"
    opts = {"latency": 0.1, "gate": str(gate)}
    t0 = time.time()
    tl = Timeline(t0, verbose=True)
    async with TcpProxy(info_["host"], int(info_["port"])) as proxy:
        far_url = f"postgresql://{info_['user']}@127.0.0.1:{proxy.listen_port}/{info_['dbname']}"
        far = WorkerPool(far_url, APP, n=1, concurrency=1, lease=LEASE, poll=0.1, options=opts,
                         log_dir=workdir / "logs-part5", name="far-")
        near = WorkerPool(dsn, APP, n=1, concurrency=1, lease=LEASE, poll=0.1, options=opts,
                          log_dir=workdir / "logs-part5", name="near-")
        try:
            step(f"far-0 启动（--queue postgresql://…@127.0.0.1:{proxy.listen_port}，即代理的端口），入队一个报修任务")
            far.start()
            await wait_until(lambda: far.events("started"), 60, "far-0 启动")
            jid = await queue.enqueue("agent", {"op": "run", "input": "VPN 连不上，提示证书过期"}, tenant_id="acme")

            def at_gate():
                tl.poll(far, near)
                return [e for e in far.events("llm_call") if e["job"] == jid and e["phase"] == "second" and e["attempt"] == 1]

            await wait_until(at_gate, 60, "far-0 执行完工具、停在第二次模型调用里")
            tl.log("调度器", "far-0 已经建好工单、检查点里有工具结果，正在等第二次模型调用返回")
            near.start()
            await wait_until(lambda: near.events("started"), 60, "near-0 启动")
            dropped = proxy.cut()
            t_cut = time.time()
            tl.log("调度器", f"✂️  断网：TcpProxy.cut() 掐断 far-0 的 {dropped} 条数据库连接（队列池、检查点池、工单库池），"
                            "之后的新连接一接上就被重置；far-0 进程本身活得好好的")

            async def near_done():
                tl.poll(far, near)
                return (await queue.get(jid)).status == "succeeded"

            await wait_until(near_done, 90, "near-0 接手并完成")
            job = await queue.get(jid)
            before = await ckpt.get_run(f"job-{jid}")
            tl.log("调度器", f"任务 #{jid} 已由 {job.worker_id} 完成（fence={job.fence}）；far-0 仍然存活：{far.workers[0].alive}")
            proxy.heal()
            gate.touch()
            tl.log("调度器", "🔌 网络恢复 + far-0 的模型调用返回：它以为任务还归自己，要把最终答案写进检查点")

            def far_lost():
                tl.poll(far, near)
                return [e for e in far.events("ownership_lost") if e.get("job") == jid]

            await wait_until(far_lost, 60, "far-0 的迟到写入被拒绝")
            await asyncio.sleep(0.5)
            tl.poll(far, near)
            far_alive = far.workers[0].alive
        finally:
            # 停止放进线程里等：事件循环不能被卡住（TcpProxy 就在这个事件循环里转发字节）
            await asyncio.gather(asyncio.to_thread(far.stop), asyncio.to_thread(near.stop))
    final = await queue.get(jid)
    after = await ckpt.get_run(f"job-{jid}")
    calls = await sql(dsn, "SELECT worker FROM ticket_calls")
    (tickets,), = await sql(dsn, "SELECT count(*) FROM tickets")
    hb_errors = [e for e in tl.all if e["event"] == "heartbeat_error" and e.get("worker_id") == "far-0"]
    claims = [(e["worker_id"], e["fence"], e["t"]) for e in tl.all if e["event"] == "claimed" and e["job"] == jid]
    near_claim_t = next((t for w, _, t in claims if w == "near-0"), None)
    step("📊 结果")
    info(f"领取记录：{'，'.join(f'{w} fence={f}' for w, f, _ in claims)}（接手者的 fence 更大）")
    info(f"far-0 断网期间的心跳：{len(hb_errors)} 次失败（heartbeat_error），租约因此没有续上；"
         f"断网后 {near_claim_t - t_cut:.1f} 秒 near-0 领到它（租约 {LEASE:g} 秒 + 回收后的退避）" if near_claim_t else "")
    info(f"任务最终：{final.status}，完成者 {final.worker_id}，fence={final.fence}；far-0 一次提交都没有发生"
         f"（completed 事件 {sum(1 for e in tl.all if e['event'] == 'completed' and e.get('worker_id') == 'far-0')} 次）")
    unchanged = (after["version"], after["writer"], after["state"]) == (before["version"], "near-0", before["state"])
    info(f"检查点：far-0 恢复后写入被 CAS 拒绝，版本号仍是 {after['version']}、最后写入者 {after['writer']} —— "
         f"{'和 near-0 写完时一模一样 ✅' if unchanged else '被改过 ❌'}")
    info(f"下游：工具体只执行过 {len(calls)} 次（{calls[0][0] if calls else '-'}，断网前），工单 {tickets} 张；"
         "near-0 从检查点继续，没有重新调用工具")
    info(f"far-0 分区期间和之后一直活着（{'✅' if far_alive else '❌'}）：分区不是崩溃。TcpProxy 统计：{proxy.stats}")
    step("如实说明这个实验的局限")
    info("① 所有进程都在同一台机器上：没有跨机器的时钟漂移、没有真实的网络延迟分布；")
    info("② TcpProxy.cut() 用 RST 重置连接，客户端立刻报错；真实的分区更常见的是包被静默丢弃，要等 TCP 超时才发现 ——")
    info("   生产环境要给数据库连接配 connect_timeout、TCP keepalive、statement_timeout，否则一个断掉的连接可能让协程挂很久；")
    info("③ Redis 是 fakeredis（Redis 协议的 Python 实现），不模拟持久化、主从切换、集群分片：\"Redis 切换时丢写入\"本课没有实测。")
    await queue.close()
    await ckpt.close()
    return (final.status == "succeeded" and final.worker_id == "near-0" and bool(hb_errors) and unchanged
            and len(calls) == 1 and tickets == 1 and far_alive)


# =====================================================================================
# 入口
# =====================================================================================


async def amain(args, server_uri: str, redis_url: str, workdir: Path) -> int:
    from agentkit.testing import create_database

    parts = {int(x) for x in args.only.split(",") if x.strip()}
    results: dict[int, bool] = {}
    if 1 in parts or 2 in parts:
        dsn = create_database(server_uri, "helpdesk")
        results[1] = await part1(args, dsn, redis_url, workdir)
        if 2 in parts:
            results[2] = await part2(args, dsn, redis_url, workdir)
    if 3 in parts:
        results[3] = await part3(args, server_uri, workdir)
    if 4 in parts:
        results[4] = await part4(args, server_uri, workdir)
    if 5 in parts:
        results[5] = await part5(args, server_uri, workdir)
    failed = [k for k, ok in results.items() if not ok]
    if failed:
        print(f"\n❌ 第 {', '.join(map(str, failed))} 部分的检查没有通过（见上面的输出；worker 日志在 {workdir}）")
        return 1
    print("\n完成。讲义：lessons/26_state_and_queues/README.md")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用剧本模型（ScriptedLLM），不调用真实模型")
    parser.add_argument("--only", default="1,2,3,4,5", help="只跑某几个部分，例如 --only 1,2")
    parser.add_argument("--verbose", action="store_true", help="打印每一个 worker 事件")
    parser.add_argument("--keep", action="store_true", help="保留运行目录（worker 日志、SQLite 文件），不删除")
    args = parser.parse_args()

    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        print(f"本 Demo 需要可选依赖：{', '.join(missing)} 未安装。\n请运行：{INSTALL_HINT}")
        return 0
    if not args.offline:
        from agentkit.config import env

        if not env("LLM_API_KEY"):
            print("没有配置 LLM_API_KEY（.env），改用 --offline 模式运行。")
            args.offline = True

    workdir = Path(tempfile.mkdtemp(prefix="agentkit_l26_"))
    print(f"模式：{'离线（剧本模型）' if args.offline else '真实模型（' + (os.environ.get('LLM_MODEL') or '默认模型') + '）'}；"
          f"运行目录 {workdir}")
    try:
        with contextlib.ExitStack() as stack:
            try:  # 只有"拉起基础设施"失败才降级退出；后面的逻辑出错就让它报错
                server_uri = stack.enter_context(tcp_postgres(workdir))
                redis_url = stack.enter_context(redis_process())
            except (ImportError, OSError, RuntimeError) as e:
                print(f"本机基础设施启动失败：{type(e).__name__}: {e}\n请确认已安装：{INSTALL_HINT}")
                return 0
            print(f"Postgres：{server_uri}（TCP，独立进程）；fakeredis：{redis_url}（独立进程）")
            return asyncio.run(amain(args, server_uri, redis_url, workdir))
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
