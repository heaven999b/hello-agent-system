"""第 25 课：把主动式 Agent 真的跑起来 —— 真实的进程、真实的墙钟、真实的队列（不是模拟器）。

proactive_kit.py 里的 simulate_day / run_day 是**离散事件模拟器**：时间是整数分钟，一整天在一个 for 循环里瞬间跑完，
用来做确定、可复现的策略对比和练习。它回答"该不该说"，回答不了"系统怎么真的跑起来"：
谁在什么时候醒来？事件从哪个进程来？定时器在两个副本上同时触发怎么办？发通知的进程崩了、任务重跑，会不会发两遍？

本文件用真实的组件回答这几个问题（单机、零外部依赖：一个 SQLite 文件，多个操作系统进程）：

    生产者进程（python proactive_runtime.py produce）
        扮演邮件 / 告警源：按真实的时间间隔往 events 表写事件；其中一条故意投递两次（at-least-once 投递是常态）
    主进程里的 asyncio：
        watch_events()      每 poll 秒查一次 events 表的新行 → 决策器（proactive_kit.should_interrupt）：
                            现在说 → enqueue("notify", 幂等键 = notify:<事件源 ID>)
                            攒着说 → 写进 digest_items 表，等下一次摘要
        interval_trigger()  按真实墙钟每 period 秒触发一次（对齐到整数个周期）→ enqueue("digest", 幂等键 = digest:<周期编号>)
    第二个调度器副本（另一个进程：python proactive_runtime.py scheduler）
        跑同样的 interval_trigger：模拟"两个 Pod 都跑着定时器"（多副本部署、滚动发布时新旧副本并存）
    worker 进程 × 2（WorkerPool → python -m agentkit.distributed.worker --app proactive_runtime.py:make_handler）
        notify：一个事务里 INSERT OR IGNORE 进 notifications 表（幂等键是主键）= "发出通知"
        digest：一个事务里把还没发的 digest_items 打包成一条摘要；用户正在专注 / 开会（user_state 表）就这一轮先不发
        故障注入 crash_once=<事件源 ID>：通知已经写进去（事务已提交）、任务还没确认时，worker 进程 os._exit(1) 当场退出；
        租约过期后另一个 worker 重新领取、重新执行 —— 靠幂等键，不会再发一遍

幂等有两层：
  1. 入队去重：同一个触发（同一个事件源 ID、同一个摘要周期）不管触发几次，队列里只有一个任务
     （SQLiteJobQueue 的 UNIQUE(tenant_id, idempotency_key)：重复入队返回已有任务的 id）；
  2. 执行去重：同一个任务不管执行几次（崩溃后重领、租约过期），通知只发一次（notifications 的主键）。
队列只能保证"至少执行一次"（at-least-once）；"效果只发生一次"要靠副作用本身幂等（第 08、13 课）。

两种时间，别混：事件自带的**业务时间**（minute：几点几分，决策器用它判断勿扰时段，沿用模拟器的约定）；
调度、等待、延迟全部用**真实墙钟**（秒）。局限：单机（SQLite 只能在一台机器上共享）；多机把 SQLite 换成
agentkit.contrib.postgres 里接口相同的实现（第 26 课），定时器可以换成 Temporal Schedules 等托管调度（第 28 课）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
_REPO = HERE.parents[1]
if str(_REPO) not in sys.path:  # 作为脚本直接运行（python proactive_runtime.py produce）时也能 import agentkit
    sys.path.insert(0, str(_REPO))

from agentkit.distributed import PermanentJobError, SQLiteDB, SQLiteJobQueue, WorkerPool  # noqa: E402


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（课程目录名以数字开头，没法写普通的 import）。与 demo.py 的同名函数一致。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


pk = _load_sibling("proactive_kit")

TENANT = "lin"
APP = f"{HERE / 'proactive_runtime.py'}:make_handler"

# 生产者要写的事件：(事件源 ID, 业务时间, 来源, 类型, 标题, 是否紧急)。alert-7731 投递两次。
LIVE_STREAM: list[tuple] = [
    ("act-01", "13:30", "activity", "focus_start", "进入专注模式", False),
    ("mail-481", "13:40", "email", "newsletter", "技术周刊 #129", False),
    ("alert-7731", "13:50", "alert", "prod_alert", "【SEV1】生产 API p99 延迟 3.2s，错误率 5%", True),
    ("alert-7731", "13:50", "alert", "prod_alert", "【SEV1】生产 API p99 延迟 3.2s，错误率 5%", True),  # 重复投递
    ("mail-482", "14:20", "email", "review_request", "同事：有空帮我 review 一下 PR #482 吗？不急", False),
    ("act-02", "14:55", "activity", "focus_end", "专注结束", False),
    ("cal-19", "15:05", "calendar", "calendar_conflict", "15:30 的 1:1 与新加入的跨部门同步会冲突", False),
    ("mail-483", "15:10", "email", "manager_request", "主管：周五前把 Q3 延迟分析报告发我", False),
]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, minute INTEGER NOT NULL, source TEXT NOT NULL,
        kind TEXT NOT NULL, title TEXT NOT NULL, urgent INTEGER NOT NULL, producer_pid INTEGER, written_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS user_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), focus INTEGER NOT NULL, meeting INTEGER NOT NULL, updated_at REAL)""",
    "INSERT OR IGNORE INTO user_state VALUES (1, 0, 0, 0)",
    """CREATE TABLE IF NOT EXISTS digest_items (
        source_id TEXT PRIMARY KEY, title TEXT NOT NULL, added_at REAL NOT NULL, digest_key TEXT)""",
    """CREATE TABLE IF NOT EXISTS notifications (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, job_id INTEGER, attempt INTEGER,
        worker TEXT, pid INTEGER, sent_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS triggers (
        who TEXT NOT NULL, pid INTEGER, kind TEXT NOT NULL, key TEXT NOT NULL, job_id INTEGER, target_at REAL, fired_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS handler_runs (
        job_id INTEGER, key TEXT, attempt INTEGER, worker TEXT, pid INTEGER, outcome TEXT, items INTEGER, t REAL)""",
    """CREATE TABLE IF NOT EXISTS decisions (
        seq INTEGER, source_id TEXT, action TEXT, reason TEXT, score REAL, job_id INTEGER, duplicate INTEGER, t REAL)""",
]


async def setup_schema(db: SQLiteDB) -> None:
    def ddl(conn):
        for stmt in SCHEMA:
            conn.execute(stmt)

    await db.write(ddl)


# =====================================================================
# 生产者：另一个进程，按真实的时间间隔写事件
# =====================================================================


def produce(db_path: str | Path, gap: float, stream: list[tuple] = LIVE_STREAM) -> int:
    """（在独立的生产者进程里运行）每隔 gap 秒写一条事件。

    这里故意写成普通的同步代码：它扮演的是外部系统（邮件服务器、监控系统），是一个独立的进程，
    time.sleep 只让它自己停一下，不会卡住任何人的事件循环。每条 INSERT 自动提交，主进程随时读得到。"""
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        for sid, hm, source, kind, title, urgent in stream:
            time.sleep(gap)
            conn.execute(
                "INSERT INTO events (source_id, minute, source, kind, title, urgent, producer_pid, written_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, pk.at(hm), source, kind, title, int(urgent), os.getpid(), time.time()),
            )
    finally:
        conn.close()
    return len(stream)


# =====================================================================
# 调度器：真实墙钟上的周期触发
# =====================================================================


async def interval_trigger(queue: SQLiteJobQueue, db: SQLiteDB, *, period: float, until: float, who: str) -> int:
    """每 period 秒醒来一次，对齐到墙钟上的整数个周期（第 k 次在 t = k × period 时刻），入队一个摘要任务。

    幂等键 digest:<周期编号> 只取决于墙钟：几个副本（不管在哪个进程）在同一个周期里触发，算出来的键一样，
    队列里就只有一个任务。这比"选主、只让一个副本跑定时器"简单得多，也不怕选主出错（第 13 课）。
    返回触发次数。until 是墙钟时间（time.time()），到点就停。"""
    fired = 0
    while True:
        now = time.time()
        target = (math.floor(now / period) + 1) * period
        if target > until:
            return fired
        await asyncio.sleep(target - now)  # 真的等：等待期间事件循环去处理别的协程（比如 watch_events）
        woke = time.time()
        window = round(target / period)
        key = f"digest:{window}"
        job_id = await queue.enqueue("digest", {"window": window}, tenant_id=TENANT, idempotency_key=key)
        await db.write(lambda c: c.execute(
            "INSERT INTO triggers VALUES (?, ?, ?, ?, ?, ?, ?)", (who, os.getpid(), "digest", key, job_id, target, woke)))
        fired += 1


# =====================================================================
# 事件监听 + 决策：主进程里的一个协程
# =====================================================================


async def watch_events(
    db: SQLiteDB, queue: SQLiteJobQueue, model, *, poll: float, producer_done: asyncio.Event, limits=None
) -> Counter:
    """轮询 events 表的新行（按 seq 游标），逐条决策。生产者结束后再读一轮，读空才返回。

    这个协程不记"哪些事件处理过"：进程重启、游标丢了、生产者重复投递，同一个事件都可能被决策两次 ——
    那就入队两次，由队列的幂等键去重。入队返回的是已有任务的 id 时，说明这是一次重复触发，不算新的打扰。"""
    limits = limits or pk.Limits()
    stats: Counter = Counter()
    cursor, focus, meeting = 0, False, False
    recent: list[int] = []  # 最近真正打扰过的业务时间（分钟），给频率上限用
    job_ids: set[int] = set()
    while True:
        done_before_read = producer_done.is_set()
        rows = await db.run(lambda c: [dict(r) for r in c.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (cursor,))])
        for r in rows:
            cursor = r["seq"]
            ev = pk.Event(r["source_id"], r["minute"], r["source"], r["kind"], r["title"], bool(r["urgent"]))
            if ev.kind in pk.ACTIVITY_KINDS:
                focus = {"focus_start": True, "focus_end": False}.get(ev.kind, focus)
                meeting = {"meeting_start": True, "meeting_end": False}.get(ev.kind, meeting)
                f, m = int(focus), int(meeting)
                row = (r["seq"], ev.id, "state", ev.kind, None, None, 0, time.time())

                def op(c):  # 用户状态和决策记录在同一个事务里写：worker 发摘要前会读 user_state
                    c.execute("UPDATE user_state SET focus = ?, meeting = ?, updated_at = ? WHERE id = 1", (f, m, row[-1]))
                    c.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)

                await db.write(op)
                stats["state_change"] += 1
                continue
            benefit, conf, _ = pk.estimate(ev, model)
            d = pk.should_interrupt(benefit, conf, limits.base_cost,
                                    pk.Context(now=ev.t, focus=focus, in_meeting=meeting, urgent=ev.urgent), recent, limits)
            stats[d.action] += 1
            decided_at, job_id, duplicate = time.time(), None, False
            if d.action == "interrupt":
                key = f"notify:{ev.id}"
                payload = {"source_id": ev.id, "title": ev.title, "reason": d.reason, "written_at": r["written_at"]}
                job_id = await queue.enqueue("notify", payload, tenant_id=TENANT, idempotency_key=key)
                await db.write(lambda c: c.execute(
                    "INSERT INTO triggers VALUES (?, ?, ?, ?, ?, ?, ?)", ("watcher", os.getpid(), "notify", key, job_id, None, decided_at)))
                duplicate = job_id in job_ids  # 入队返回了已有任务的 id：重复触发，被队列去重了
                if duplicate:
                    stats["duplicate_trigger"] += 1
                else:
                    job_ids.add(job_id)
                    recent.append(ev.t)
            elif d.action == "defer":
                sid, title = ev.id, ev.title
                await db.write(lambda c: c.execute(
                    "INSERT OR IGNORE INTO digest_items VALUES (?, ?, ?, NULL)", (sid, title, time.time())))
            row = (r["seq"], ev.id, d.action, d.reason, round(d.score, 3), job_id, int(duplicate), decided_at)
            await db.write(lambda c: c.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row))
        if done_before_read and not rows:
            return stats
        await asyncio.sleep(poll)


# =====================================================================
# worker：真正"开口"的地方（在 WorkerPool 拉起的独立进程里运行）
# =====================================================================


def _emit(worker_id: str, event: str, **info) -> None:
    """和 run_worker 的事件同样的格式：一行 JSON，WorkerPool.events() 能读到。"""
    line = {"event": event, "worker_id": worker_id, "pid": os.getpid(), "t": round(time.time(), 4), **info}
    print(json.dumps(line, ensure_ascii=False), flush=True)


async def make_handler(ctx):
    """worker 进程的 --app 工厂。ctx.db 是队列所在的 SQLiteDB（同一个文件里也放着事件、摘要、通知表）。"""
    db, worker_id = ctx.db, ctx.worker_id
    crash_once = ctx.options.get("crash_once")

    def record(c, job, outcome: str, items: int = 0) -> None:
        c.execute("INSERT INTO handler_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (job.id, job.idempotency_key, job.attempts, worker_id, os.getpid(), outcome, items, time.time()))

    async def notify(job) -> dict:
        key, title = job.idempotency_key, job.payload["title"]

        def op(c):
            cur = c.execute("INSERT OR IGNORE INTO notifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (key, "notify", f"🔔 {title}", job.id, job.attempts, worker_id, os.getpid(), time.time()))
            outcome = "sent" if cur.rowcount else "duplicate_skipped"  # 主键冲突 = 之前已经发过：这次什么都不做
            record(c, job, outcome)
            return outcome

        outcome = await db.write(op)  # 通知和执行记录在同一个事务里：要么都写进去，要么都没有
        if crash_once and key == f"notify:{crash_once}" and job.attempts == 1:
            _emit(worker_id, "crash_injected", job=job.id, key=key)
            os._exit(1)  # 故障注入：通知已经发出（事务已提交），任务还没确认 —— 进程当场死掉，来不及做任何收尾
        return {"outcome": outcome}

    async def digest(job) -> dict:
        key = job.idempotency_key

        def op(c):
            if c.execute("SELECT 1 FROM notifications WHERE key = ?", (key,)).fetchone():
                outcome, n = "duplicate_skipped", 0
            else:
                state = c.execute("SELECT focus, meeting FROM user_state WHERE id = 1").fetchone()
                items = c.execute("SELECT source_id, title FROM digest_items WHERE digest_key IS NULL ORDER BY added_at").fetchall()
                if state["focus"] or state["meeting"]:
                    outcome, n = "busy_skip", len(items)  # 用户在专注 / 开会：这一轮不推，条目留给下一轮
                elif not items:
                    outcome, n = "nothing_pending", 0
                else:
                    ids = [r["source_id"] for r in items]
                    c.execute(f"UPDATE digest_items SET digest_key = ? WHERE source_id IN ({','.join('?' * len(ids))})", (key, *ids))
                    text = "📬 摘要：" + "；".join(r["title"] for r in items)
                    c.execute("INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (key, "digest", text, job.id, job.attempts, worker_id, os.getpid(), time.time()))
                    outcome, n = "sent", len(items)
            record(c, job, outcome, n)
            return outcome, n

        outcome, n = await db.write(op)  # 一个 BEGIN IMMEDIATE 事务：两个 worker 不会把同一批条目发两次
        return {"outcome": outcome, "items": n}

    async def handle(job) -> dict:
        if job.kind == "notify":
            return await notify(job)
        if job.kind == "digest":
            return await digest(job)
        raise PermanentJobError(f"不认识的任务类型 {job.kind}")

    return handle


# =====================================================================
# 编排：把上面这些真的跑一遍，收集可以断言的事实
# =====================================================================


@dataclass
class LiveReport:
    t0: float
    main_pid: int
    producer: dict
    scheduler_b: dict
    worker_pids: list[int | None]
    worker_exit_codes: list[int | None]
    watcher: Counter
    fired_a: int
    events: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    triggers: list[dict] = field(default_factory=list)
    jobs: list = field(default_factory=list)
    notifications: list[dict] = field(default_factory=list)
    handler_runs: list[dict] = field(default_factory=list)
    worker_events: list[dict] = field(default_factory=list)
    elapsed: float = 0.0

    def job_by_key(self, key: str):
        return next((j for j in self.jobs if j.idempotency_key == key), None)

    def runs_for(self, key: str) -> list[dict]:
        return sorted((h for h in self.handler_runs if h["key"] == key), key=lambda h: h["attempt"])

    def latency_ms(self, source_id: str) -> float | None:
        """事件第一次写入 → 通知写入 notifications 表（毫秒，真实墙钟）。"""
        written = min((e["written_at"] for e in self.events if e["source_id"] == source_id), default=None)
        sent = next((n["sent_at"] for n in self.notifications if n["key"] == f"notify:{source_id}"), None)
        return None if written is None or sent is None else (sent - written) * 1000

    def lateness_ms(self) -> list[float]:
        """定时器每次触发比它的目标时刻（墙钟上的整数个周期）晚了多少毫秒。"""
        return [(t["fired_at"] - t["target_at"]) * 1000 for t in self.triggers if t["target_at"] is not None]

    def digest_windows(self) -> dict[str, dict[str, list[int]]]:
        """{幂等键: {副本名: [拿到的任务 id, ...]}}"""
        out: dict[str, dict[str, list[int]]] = {}
        for t in self.triggers:
            if t["kind"] == "digest":
                out.setdefault(t["key"], {}).setdefault(t["who"], []).append(t["job_id"])
        return out


async def _run_script(*args: str, cwd: Path) -> asyncio.subprocess.Process:
    """用 asyncio 起一个子进程运行本文件的某个子命令（不阻塞事件循环）。"""
    return await asyncio.create_subprocess_exec(
        sys.executable, str(HERE / "proactive_runtime.py"), *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


async def _eventually(check, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        result = await check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待超时：{what}")
        await asyncio.sleep(interval)


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sql)]


async def run_live(
    workdir: str | Path,
    *,
    period: float = 1.0,
    gap: float = 0.3,
    lease: float = 1.0,
    poll: float = 0.05,
    crash_once: str | None = "alert-7731",
    n_workers: int = 2,
    tail: float = 2.5,
    timeout: float = 60.0,
) -> LiveReport:
    """真实地跑一遍：生产者进程 + 两个调度器副本（一个在本进程、一个在另一个进程）+ 事件监听 + 2 个 worker 进程。

    period：摘要触发的周期（秒，真实墙钟）；gap：生产者两条事件之间的间隔（秒）；lease：任务租约（秒），
    决定 worker 崩溃后多久被别人接手；tail：生产者写完之后，调度器再跑多久（留出发摘要的时间）。"""
    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    path = workdir / "proactive.db"
    db = SQLiteDB(path)
    queue = SQLiteJobQueue(db)
    await queue.setup()  # 先建库、切 WAL、建表，再拉起别的进程
    await setup_schema(db)

    pool = WorkerPool(f"sqlite:///{path}", APP, n=n_workers, concurrency=4, lease=lease, poll=poll, grace=5,
                      options={"crash_once": crash_once or ""}, log_dir=workdir / "logs")
    t0 = time.time()
    pool.start()
    producer = scheduler_b = None
    try:
        # worker 都上线了再开始产生事件：量到的延迟不含 Python 进程的启动时间
        async def workers_up():
            return len(pool.events("started")) >= n_workers

        await _eventually(workers_up, 60, "worker 进程上线")
        stream_seconds = gap * (len(LIVE_STREAM) + 1)
        until = time.time() + stream_seconds + tail
        producer = await _run_script("produce", "--db", str(path), "--gap", str(gap), cwd=workdir)
        scheduler_b = await _run_script("scheduler", "--db", str(path), "--period", str(period),
                                        "--until", str(until), "--who", "B", cwd=workdir)
        producer_done = asyncio.Event()

        async def wait_producer():
            await producer.communicate()
            producer_done.set()

        watcher = asyncio.create_task(watch_events(db, queue, pk.initial_user_model(), poll=poll, producer_done=producer_done))
        sched_a = asyncio.create_task(interval_trigger(queue, db, period=period, until=until, who="A"))
        producer_task = asyncio.create_task(wait_producer())
        b_out, b_err = await scheduler_b.communicate()
        watcher_stats, fired_a, _ = await asyncio.gather(watcher, sched_a, producer_task)

        async def drained():
            counts = (await queue.stats())["counts"]
            return counts.get("queued", 0) + counts.get("leased", 0) == 0

        await _eventually(drained, timeout, "队列里的任务全部处理完")
    finally:
        for proc in (producer, scheduler_b):
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.to_thread(pool.stop)  # stop 会等进程退出（阻塞调用）：放进线程，别卡住事件循环
        await queue.close()
        await db.close()

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        report = LiveReport(
            t0=t0,
            main_pid=os.getpid(),
            producer={"pid": producer.pid, "returncode": producer.returncode},
            scheduler_b={"pid": scheduler_b.pid, "returncode": scheduler_b.returncode,
                         "fired": int(b_out.split()[-1]) if b_out and b_out.split() else 0,
                         "stderr": (b_err or b"").decode("utf-8", "replace")[-500:]},
            worker_pids=pool.pids,
            worker_exit_codes=[w.returncode for w in pool.workers],
            watcher=watcher_stats,
            fired_a=fired_a,
            events=_rows(conn, "SELECT * FROM events ORDER BY seq"),
            decisions=_rows(conn, "SELECT * FROM decisions ORDER BY seq"),
            triggers=_rows(conn, "SELECT * FROM triggers ORDER BY fired_at"),
            notifications=_rows(conn, "SELECT * FROM notifications ORDER BY sent_at"),
            handler_runs=_rows(conn, "SELECT * FROM handler_runs ORDER BY t"),
            worker_events=pool.events(),
            elapsed=time.time() - t0,
        )
    finally:
        conn.close()
    q2 = SQLiteJobQueue(path)
    report.jobs = await q2.list_jobs(limit=1000)
    await q2.close()
    return report


# =====================================================================
# 命令行：生产者进程和第二个调度器副本的入口
# =====================================================================


async def _scheduler_main(db_path: str, period: float, until: float, who: str) -> int:
    db = SQLiteDB(db_path)
    queue = SQLiteJobQueue(db)
    try:
        return await interval_trigger(queue, db, period=period, until=until, who=who)
    finally:
        await queue.close()
        await db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="第 25 课：主动式 Agent 运行时的辅助进程")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("produce", help="生产者：按真实的时间间隔往 events 表写事件")
    p1.add_argument("--db", required=True)
    p1.add_argument("--gap", type=float, default=0.3)
    p2 = sub.add_parser("scheduler", help="调度器副本：按真实墙钟周期性地入队摘要任务")
    p2.add_argument("--db", required=True)
    p2.add_argument("--period", type=float, default=1.0)
    p2.add_argument("--until", type=float, required=True, help="墙钟时间（unix 秒），到点停止")
    p2.add_argument("--who", default="B")
    args = ap.parse_args(argv)
    if args.cmd == "produce":
        print(produce(args.db, args.gap), flush=True)
    else:
        print(asyncio.run(_scheduler_main(args.db, args.period, args.until, args.who)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
