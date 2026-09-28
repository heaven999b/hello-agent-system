"""SQLite 后端：单机多进程的任务队列、检查点、幂等存储、限流与并发槽位（零依赖）。

为什么 SQLite 能讲清"分布式"？
    多个**独立进程**同时读写**同一个数据库文件**。进程之间不共享任何内存，只能通过数据库协作，
    谁先抢到、谁覆盖了谁、谁崩溃了留下什么，全都是真实发生的竞争，不是用 sleep 演出来的。
    这和"多台机器上的 worker + 一个共享的 Postgres"在并发语义上是一回事。

它做不到的（如实说明，第 13 课 7 节）：
- 只能在**一台机器**上：WAL 模式要求所有进程共享同一块内存映射，不支持网络文件系统；
- 同一时刻只有**一个写者**：写入吞吐有上限（本机实测见第 13 课），适合几个到几十个 worker 进程；
- 用的是各进程的本机时钟（同一台机器上是同一个时钟）；多机时要用数据库服务器的时钟（Postgres 的 now()）；
- synchronous=NORMAL：进程崩溃（kill -9）不丢已提交的数据，但机器断电可能丢最后几个事务。
多机部署换成 agentkit.contrib.postgres 里接口相同的实现（第 26 课）。

实现要点：
- 每个对象一个连接，放在**一个专用线程**里执行（sqlite3 是阻塞 API，不能在事件循环里直接调用）；
- 写操作一律 `BEGIN IMMEDIATE`：事务一开始就拿写锁，"读-判断-写"整个过程原子，不会被别的进程插队；
- busy_timeout：别人正在写时排队等，而不是立刻报 "database is locked"；
- 不用 RETURNING 等较新的语法：Python 3.10 常见的系统 SQLite 版本也能跑。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from ..limits import LimitExceeded
from ..reliability import CircuitOpenError
from ..state import RunState
from ..tools import ToolResult
from .jobs import CheckpointConflict, Job, LeaseLost, explain_rejection, summarize_stats


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"非法的表名：{name!r}")
    return name


class SQLiteDB:
    """一个 SQLite 连接 + 一个专用线程。所有数据库操作都在这个线程里执行，事件循环只 await 结果。

    同一个进程里的多个对象可以共用一个 SQLiteDB（传 db=），省线程也省连接。
    **不能跨进程共享**：每个 worker 进程用文件路径自己创建。
    """

    def __init__(self, path: str | Path, *, busy_timeout: float = 30.0):
        self.path = str(path)
        self.busy_timeout = busy_timeout
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentkit-sqlite")
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None：自己决定事务从哪里开始（见 write）；timeout 就是 busy_timeout
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")  # 读写互不阻塞，只有写和写互斥
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    async def run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """在数据库线程里执行 fn(conn)。调用方被取消时，已经开始的操作仍会在线程里完成（事务要么提交要么回滚）。"""
        if self._closed:
            raise RuntimeError("SQLiteDB 已关闭")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exec, lambda: fn(self._connection()))

    async def write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """在一个写事务（BEGIN IMMEDIATE）里执行 fn(conn)：成功提交，异常回滚。"""

        def txn(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return out

        return await self.run(txn)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()

        def _close():
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        await loop.run_in_executor(self._exec, _close)
        self._exec.shutdown(wait=False)


class _Owner:
    """持有（或借用）一个 SQLiteDB 的对象的公共部分。"""

    def __init__(self, path_or_db: str | Path | SQLiteDB, table: str):
        if isinstance(path_or_db, SQLiteDB):
            self.db, self._owns_db = path_or_db, False
        else:
            self.db, self._owns_db = SQLiteDB(path_or_db), True
        self.table = _ident(table)

    async def close(self) -> None:
        if self._owns_db:
            await self.db.close()

    async def __aenter__(self):
        await self.setup()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def setup(self) -> None:  # pragma: no cover —— 子类实现
        raise NotImplementedError


# =====================================================================================
# 任务队列
# =====================================================================================


class SQLiteJobQueue(_Owner):
    """租约队列。接口与 agentkit.contrib.postgres.PostgresJobQueue 一致，可直接交给 run_worker。

    任务的一生：

        enqueue ─► queued ──claim──► leased ──complete──► succeeded
                     ▲                 │
                     │                 ├──fail（可重试，还有次数）──► queued（退避一段时间后再领）
                     │                 ├──fail（不可重试）─────────► failed
                     │                 └──fail（次数用尽）─────────► dead（死信）
                     └── 租约过期被回收（claim 时顺手做）：还有次数 → queued；次数用尽 → dead

    fence 来自整张表共用的计数器，全局单调递增（不是每个任务各自从 1 数起）：同一个 run 会先后对应多个任务
    （run → 审批后的 resume），检查点的 fence 保护的是整个 run，只有全局递增才能保证"后来者的 fence 一定更大"。
    """

    def __init__(
        self,
        path_or_db: str | Path | SQLiteDB,
        table: str = "agent_jobs",
        *,
        max_attempts: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 300.0,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__(path_or_db, table)
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.clock = clock

    async def setup(self) -> None:
        t = self.table

        def ddl(conn):
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {t} (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind            TEXT    NOT NULL,
                    tenant_id       TEXT    NOT NULL,
                    payload         TEXT    NOT NULL,
                    idempotency_key TEXT,
                    priority        INTEGER NOT NULL DEFAULT 0,
                    status          TEXT    NOT NULL DEFAULT 'queued',
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    max_attempts    INTEGER NOT NULL DEFAULT 5,
                    fence           INTEGER NOT NULL DEFAULT 0,
                    worker_id       TEXT,
                    lease_until     REAL,
                    run_at          REAL    NOT NULL,
                    result          TEXT,
                    last_error      TEXT,
                    created_at      REAL    NOT NULL,
                    updated_at      REAL    NOT NULL,
                    finished_at     REAL,
                    UNIQUE (tenant_id, idempotency_key))"""  # NULL 互不相等：不带幂等键的任务不受约束
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_ready_idx ON {t} (status, priority DESC, id)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_lease_idx ON {t} (status, lease_until)")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {t}_fence (id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL)")
            conn.execute(f"INSERT OR IGNORE INTO {t}_fence (id, value) VALUES (1, 0)")

        await self.db.write(ddl)

    # ------------------------------------------------------------------ 内部

    def _row(self, conn, job_id: int) -> Job | None:
        row = conn.execute(f"SELECT * FROM {self.table} WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d["result"] is not None else None
        return Job.from_row(d)

    def _backoff(self, attempts: int) -> float:
        upper = min(self.max_backoff, self.base_backoff * 2 ** max(attempts - 1, 0))
        return random.uniform(0, upper)  # 全抖动：避免一批同时失败的任务在同一时刻卷土重来

    def _reap(self, conn, now: float) -> list[tuple[int, str]]:
        """回收租约过期的任务：还有次数 → 退避后重新排队；次数用尽 → 死信（毒消息兜底：计数发生在领取时）。"""
        rows = conn.execute(
            f"SELECT id, attempts, max_attempts, worker_id FROM {self.table} "
            f"WHERE status = 'leased' AND lease_until < ? ORDER BY lease_until LIMIT 100", (now,)
        ).fetchall()
        out = []
        for r in rows:
            dead = r["attempts"] >= r["max_attempts"]
            status = "dead" if dead else "queued"
            conn.execute(
                f"UPDATE {self.table} SET status = ?, lease_until = NULL, run_at = ?, finished_at = ?, updated_at = ?, "
                f"last_error = ? WHERE id = ?",
                (status, now + self._backoff(r["attempts"]), now if dead else None, now,
                 f"lease expired: worker {r['worker_id'] or '?'} 没有按时续约（崩溃 / 卡死 / 被暂停？）", r["id"]),
            )
            out.append((r["id"], status))
        return out

    # ------------------------------------------------------------------ 公共 API

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        tenant_id: str,
        idempotency_key: str | None = None,
        priority: int = 0,
        delay_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> int:
        """入队，返回任务 id。同一租户下 idempotency_key 相同的任务只会有一个（重复提交返回已有任务的 id）。"""
        now = self.clock()

        def op(conn):
            cur = conn.execute(
                f"INSERT OR IGNORE INTO {self.table} (kind, tenant_id, payload, idempotency_key, priority, max_attempts, "
                f"run_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, tenant_id, json.dumps(payload, ensure_ascii=False), idempotency_key, priority,
                 max_attempts or self.max_attempts, now + float(delay_seconds), now, now),
            )
            if cur.rowcount:
                return cur.lastrowid
            return conn.execute(
                f"SELECT id FROM {self.table} WHERE tenant_id = ? AND idempotency_key = ?", (tenant_id, idempotency_key)
            ).fetchone()["id"]

        return await self.db.write(op)

    async def get(self, job_id: int) -> Job | None:
        return await self.db.run(lambda conn: self._row(conn, job_id))

    async def find(self, tenant_id: str, idempotency_key: str) -> Job | None:
        def op(conn):
            row = conn.execute(
                f"SELECT id FROM {self.table} WHERE tenant_id = ? AND idempotency_key = ?", (tenant_id, idempotency_key)
            ).fetchone()
            return self._row(conn, row["id"]) if row else None

        return await self.db.run(op)

    async def reap_expired(self) -> list[tuple[int, str]]:
        return await self.db.write(lambda conn: self._reap(conn, self.clock()))

    async def claim(self, worker_id: str, lease_seconds: float = 30, kinds: Iterable[str] | None = None) -> Job | None:
        """领取一个任务：先回收过期租约，再挑优先级最高、最早入队的已就绪任务，发一个新 fence。

        整个过程在一个 BEGIN IMMEDIATE 事务里：两个进程同时 claim，只有一个拿得到写锁，另一个排队，
        轮到它时看到的已经是更新后的表 —— 同一个任务绝不会被领取两次。
        排序用 id（入队顺序）而不是 run_at：被回收 / 重试的任务退避结束后回到原来的位置，而不是排到队尾。
        """
        kinds = list(kinds) if kinds is not None else None

        def op(conn):
            now = self.clock()
            self._reap(conn, now)
            where, params = "status = 'queued' AND run_at <= ?", [now]
            if kinds is not None:
                where += f" AND kind IN ({','.join('?' * len(kinds))})"
                params += kinds
            row = conn.execute(f"SELECT id FROM {self.table} WHERE {where} ORDER BY priority DESC, id LIMIT 1", params).fetchone()
            if row is None:
                return None
            conn.execute(f"UPDATE {self.table}_fence SET value = value + 1 WHERE id = 1")
            fence = conn.execute(f"SELECT value FROM {self.table}_fence WHERE id = 1").fetchone()["value"]
            conn.execute(
                f"UPDATE {self.table} SET status = 'leased', worker_id = ?, lease_until = ?, attempts = attempts + 1, "
                f"fence = ?, updated_at = ? WHERE id = ?",
                (worker_id, now + float(lease_seconds), fence, now, row["id"]),
            )
            return self._row(conn, row["id"])

        return await self.db.write(op)

    async def _fenced_update(self, job: Job, action: str, sql: str, params: tuple) -> None:
        def op(conn):
            cur = conn.execute(f"{sql} WHERE id = ? AND fence = ? AND status = 'leased'", (*params, job.id, job.fence))
            if cur.rowcount == 0:
                raise LeaseLost(explain_rejection(job, self._row(conn, job.id), action))

        await self.db.write(op)

    async def heartbeat(self, job: Job, lease_seconds: float = 30) -> float:
        until = self.clock() + float(lease_seconds)
        await self._fenced_update(job, "续租", f"UPDATE {self.table} SET lease_until = ?, updated_at = ?", (until, self.clock()))
        job.lease_until = until
        return until

    async def complete(self, job: Job, result: Any = None) -> None:
        now = self.clock()
        await self._fenced_update(
            job, "提交",
            f"UPDATE {self.table} SET status = 'succeeded', result = ?, lease_until = NULL, finished_at = ?, updated_at = ?",
            (json.dumps(result, ensure_ascii=False, default=str), now, now),
        )

    async def fail(self, job: Job, error: str, retryable: bool = True) -> str:
        """报告失败，返回任务的新状态：queued（退避后重试）/ failed（不可重试）/ dead（次数用尽）。"""
        def op(conn):
            now = self.clock()
            cur = conn.execute(f"SELECT attempts, max_attempts FROM {self.table} WHERE id = ? AND fence = ? AND status = 'leased'",
                               (job.id, job.fence)).fetchone()
            if cur is None:
                raise LeaseLost(explain_rejection(job, self._row(conn, job.id), "报告失败"))
            if not retryable:
                status = "failed"
            elif cur["attempts"] >= cur["max_attempts"]:
                status = "dead"
            else:
                status = "queued"
            run_at = now + self._backoff(cur["attempts"]) if status == "queued" else now
            conn.execute(
                f"UPDATE {self.table} SET status = ?, last_error = ?, lease_until = NULL, run_at = ?, finished_at = ?, "
                f"updated_at = ? WHERE id = ?",
                (status, str(error)[:4000], run_at, None if status == "queued" else now, now, job.id),
            )
            return status

        return await self.db.write(op)

    async def release(self, job: Job, *, delay_seconds: float = 0.0, reason: str | None = None,
                      count_attempt: bool = False) -> None:
        """把任务放回队列（例如被限流推迟）。默认不消耗尝试次数。"""
        now = self.clock()
        attempts = "attempts" if count_attempt else "MAX(attempts - 1, 0)"
        await self._fenced_update(
            job, "归还",
            f"UPDATE {self.table} SET status = 'queued', lease_until = NULL, run_at = ?, attempts = {attempts}, "
            f"last_error = COALESCE(?, last_error), updated_at = ?",
            (now + float(delay_seconds), reason, now),
        )

    async def redrive(self, job_id: int) -> bool:
        """把死信 / 失败的任务重新放回队列（人工处理后）。注意不重置 fence：fence 必须单调递增。"""
        def op(conn):
            now = self.clock()
            cur = conn.execute(
                f"UPDATE {self.table} SET status = 'queued', attempts = 0, run_at = ?, lease_until = NULL, finished_at = NULL, "
                f"last_error = 'redriven: ' || COALESCE(last_error, ''), updated_at = ? WHERE id = ? AND status IN ('dead', 'failed')",
                (now, now, job_id),
            )
            return cur.rowcount > 0

        return await self.db.write(op)

    async def stats(self) -> dict:
        def op(conn):
            now = self.clock()
            counts = {r["status"]: r["n"] for r in conn.execute(f"SELECT status, count(*) AS n FROM {self.table} GROUP BY status")}
            ready = conn.execute(f"SELECT count(*) FROM {self.table} WHERE status = 'queued' AND run_at <= ?", (now,)).fetchone()[0]
            expired = conn.execute(f"SELECT count(*) FROM {self.table} WHERE status = 'leased' AND lease_until < ?", (now,)).fetchone()[0]
            oldest = conn.execute(f"SELECT min(run_at) FROM {self.table} WHERE status = 'queued' AND run_at <= ?", (now,)).fetchone()[0]
            return summarize_stats(counts, ready, expired, None if oldest is None else now - oldest)

        return await self.db.run(op)

    async def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Job]:
        def op(conn):
            q, params = f"SELECT id FROM {self.table}", []
            if status is not None:
                q, params = q + " WHERE status = ?", [status]
            ids = [r["id"] for r in conn.execute(q + " ORDER BY id LIMIT ?", (*params, int(limit)))]
            return [self._row(conn, i) for i in ids]

        return await self.db.run(op)

    async def purge_finished(self, older_than_seconds: float) -> int:
        def op(conn):
            cur = conn.execute(f"DELETE FROM {self.table} WHERE status IN ('succeeded', 'failed') AND finished_at < ?",
                               (self.clock() - float(older_than_seconds),))
            return cur.rowcount

        return await self.db.write(op)


# =====================================================================================
# 检查点
# =====================================================================================


class SQLiteCheckpointer(_Owner):
    """带版本号 CAS 与 fence 接管的检查点。接口与 PostgresCheckpointer 一致，可直接传给 Agent。

    **乐观并发（版本号 CAS）**：本实例记住每个 run 最后读到 / 写入的版本号 v，
        save：UPDATE ... SET version = version + 1 WHERE run_id = ? AND version = v
        更新到 0 行 → 在你之后有人写过 → 抛 CheckpointConflict。新 run 用 INSERT（主键冲突同样是冲突）。
    **fence 接管**（fenced(fence) 返回的视图）：纯 CAS 是"先写者赢" —— 僵尸 worker 和新 worker 读到同一个版本时，
    谁先写谁赢，输的可能恰恰是新 worker。带 fence 的 load 会把表里的 fence 改成自己的并让 version 加一（"接管"），
    于是旧持有者从这一刻起的任何写入都会冲突；fence 比表里小的 load 直接被拒绝。**最新的租约持有者总是赢家**。

    这正是 FileCheckpointer 做不到的：两个进程各自读文件、各自写文件，后写的覆盖先写的，没人知道发生过冲突。
    """

    def __init__(
        self,
        path_or_db: str | Path | SQLiteDB,
        table: str = "agent_runs",
        *,
        fence: int | None = None,
        writer: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__(path_or_db, table)
        self.fence = fence
        self.writer = writer
        self.clock = clock
        self._versions: dict[str, int] = {}
        self._vlock = threading.Lock()

    async def setup(self) -> None:
        t = self.table

        def ddl(conn):
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {t} (
                    run_id     TEXT PRIMARY KEY,
                    version    INTEGER NOT NULL,
                    fence      INTEGER NOT NULL DEFAULT 0,
                    status     TEXT NOT NULL,
                    tenant_id  TEXT,
                    writer     TEXT,
                    state      TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL)"""
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_status_idx ON {t} (tenant_id, status, updated_at DESC)")

        await self.db.write(ddl)

    def fenced(self, fence: int, writer: str | None = None) -> "SQLiteCheckpointer":
        """返回一个带 fence 的视图（共用连接，版本号记录各自独立）。每领取一次任务创建一个。"""
        return SQLiteCheckpointer(self.db, self.table, fence=int(fence), writer=writer if writer is not None else self.writer,
                                  clock=self.clock)

    def version_of(self, run_id: str) -> int | None:
        with self._vlock:
            return self._versions.get(run_id)

    def _remember(self, run_id: str, version: int, status: str | None = None) -> None:
        """记住版本号，供下一次 CAS 使用。保存了非 running 的状态（完成、暂停……）说明这次运行告一段落，
        版本号随即丢掉：恢复和审批都会先 load；一个共享的实例要服务成千上万个运行，只增不减的字典就是内存泄漏。"""
        with self._vlock:
            if status is None or status == "running":
                self._versions[run_id] = version
            else:
                self._versions.pop(run_id, None)

    async def load(self, run_id: str) -> RunState | None:
        """读检查点并记住版本号。带 fence 时同时"接管"：fence 比表里小 → CheckpointConflict。"""
        t = self.table

        def plain(conn):
            return conn.execute(f"SELECT version, state FROM {t} WHERE run_id = ?", (run_id,)).fetchone()

        def takeover(conn):
            cur = conn.execute(
                f"UPDATE {t} SET fence = ?, version = version + 1, writer = ?, updated_at = ? WHERE run_id = ? AND fence <= ?",
                (self.fence, self.writer, self.clock(), run_id, self.fence),
            )
            row = conn.execute(f"SELECT version, fence, writer, state FROM {t} WHERE run_id = ?", (run_id,)).fetchone()
            if cur.rowcount == 0 and row is not None:
                raise CheckpointConflict(
                    run_id, None, row["version"], row["writer"],
                    detail=f"表里的 fence={row['fence']} 比你的 fence={self.fence} 新：你是被取代的旧持有者。",
                )
            return row

        row = await (self.db.run(plain) if self.fence is None else self.db.write(takeover))
        if row is None:
            return None
        self._remember(run_id, row["version"])
        return RunState.from_dict(json.loads(row["state"]))

    async def save(self, state: RunState) -> None:
        t = self.table
        expected = self.version_of(state.run_id)
        payload = state.to_json()
        tenant = state.metadata.get("tenant_id")

        def op(conn):
            now = self.clock()
            if expected is None:
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO {t} (run_id, version, fence, status, tenant_id, writer, state, created_at, updated_at) "
                    f"VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (state.run_id, self.fence or 0, state.status, tenant, self.writer, payload, now, now),
                )
                new_version = 1
            else:
                cur = conn.execute(
                    f"UPDATE {t} SET state = ?, status = ?, tenant_id = ?, writer = ?, version = version + 1, updated_at = ? "
                    f"WHERE run_id = ? AND version = ?",
                    (payload, state.status, tenant, self.writer, now, state.run_id, expected),
                )
                new_version = expected + 1
            if cur.rowcount == 0:
                row = conn.execute(f"SELECT version, writer FROM {t} WHERE run_id = ?", (state.run_id,)).fetchone()
                detail = "（新建时发现它已经存在：另一个 worker 抢先创建了它）" if expected is None else ""
                raise CheckpointConflict(state.run_id, expected, row["version"] if row else None,
                                         row["writer"] if row else None, detail=detail)
            return new_version

        version = await self.db.write(op)
        self._remember(state.run_id, version, state.status)

    async def get_run(self, run_id: str) -> dict | None:
        """只读地取一条记录（摘要 + 完整 state），不"接管"、不记版本号。给 API / 运维看。"""
        def op(conn):
            row = conn.execute(f"SELECT * FROM {self.table} WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["state"] = json.loads(d["state"])
            return d

        return await self.db.run(op)

    async def list_runs(self, status: str | None = None, tenant_id: str | None = None, limit: int = 50) -> list[dict]:
        """按更新时间倒序列出 run 摘要。审批收件箱：list_runs(status="paused", tenant_id=...)。"""
        def op(conn):
            where, params = [], []
            if status is not None:
                where.append("status = ?")
                params.append(status)
            if tenant_id is not None:
                where.append("tenant_id = ?")
                params.append(tenant_id)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            rows = conn.execute(
                f"SELECT run_id, version, fence, status, tenant_id, writer, created_at, updated_at, state FROM {self.table} "
                f"{clause} ORDER BY updated_at DESC LIMIT ?", (*params, int(limit)),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                s = json.loads(d.pop("state"))
                d.update(user_id=(s.get("metadata") or {}).get("user_id"), pending=s.get("pending"),
                         stop_reason=s.get("stop_reason"), output=s.get("output"))
                out.append(d)
            return out

        return await self.db.run(op)


# =====================================================================================
# 幂等存储
# =====================================================================================


class SQLiteIdempotencyStore(_Owner):
    """跨进程的幂等存储：记住"某个 idempotency_key（run_id:call_id）已经成功执行过、结果是什么"。

    接管崩溃运行的 worker 进程重放同一个工具调用时，从这里拿到上次的结果，不会重复建单 / 扣款。
    ttl_seconds：记录保留多久（要长于"一个运行从开始到最后一次可能被重放"的时间）。

    局限：它只记"成功之后"的结果。两个进程**同时**执行同一个调用（僵尸 worker 和接手者撞在一起）时，
    两边都查不到记录、都会执行 —— 所以真正有副作用的下游还要自己认 Idempotency-Key（第 08、13 课）。
    """

    def __init__(self, path_or_db: str | Path | SQLiteDB, table: str = "idempotency", *, ttl_seconds: float = 7 * 86400,
                 clock: Callable[[], float] = time.time):
        super().__init__(path_or_db, table)
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    async def setup(self) -> None:
        await self.db.write(lambda conn: conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} (key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at REAL NOT NULL)"
        ))

    async def get(self, key: str) -> ToolResult | None:
        def op(conn):
            return conn.execute(f"SELECT result FROM {self.table} WHERE key = ? AND created_at >= ?",
                                (key, self.clock() - self.ttl_seconds)).fetchone()

        row = await self.db.run(op)
        return ToolResult(**json.loads(row["result"])) if row else None

    async def put(self, key: str, result: ToolResult) -> None:
        data = json.dumps(asdict(result), ensure_ascii=False)
        # INSERT OR IGNORE：先写入的结果为准（两个进程几乎同时成功时，不让后来者改写"第一次的结果"）
        await self.db.write(lambda conn: conn.execute(
            f"INSERT OR IGNORE INTO {self.table} (key, result, created_at) VALUES (?, ?, ?)", (key, data, self.clock())
        ))


# =====================================================================================
# 跨进程限流：令牌桶与并发槽位
# =====================================================================================


class SQLiteTokenBucket(_Owner):
    """跨进程的令牌桶：所有 worker 进程共用同一个桶（按 key，通常是租户或模型）。

    limits.TokenBucket 的桶在进程内存里：3 个 worker 进程各有一个桶，实际速率是配置的 3 倍。
    这里每次取令牌都在一个写事务里"补充 + 扣减"，多个进程看到的是同一个桶。
    """

    def __init__(self, path_or_db: str | Path | SQLiteDB, rate: float, capacity: float, table: str = "rate_buckets",
                 *, clock: Callable[[], float] = time.time):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate 和 capacity 必须大于 0")
        super().__init__(path_or_db, table)
        self.rate, self.capacity, self.clock = rate, capacity, clock

    async def setup(self) -> None:
        await self.db.write(lambda conn: conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} (key TEXT PRIMARY KEY, tokens REAL NOT NULL, updated_at REAL NOT NULL)"
        ))

    async def _take(self, key: str, tokens: float) -> float:
        """尝试取令牌：成功返回 0，否则返回还需等待的秒数。"""
        def op(conn):
            now = self.clock()
            row = conn.execute(f"SELECT tokens, updated_at FROM {self.table} WHERE key = ?", (key,)).fetchone()
            level = self.capacity if row is None else min(self.capacity, row["tokens"] + (now - row["updated_at"]) * self.rate)
            wait = 0.0
            if level >= tokens:
                level -= tokens
            else:
                wait = (tokens - level) / self.rate
            conn.execute(f"INSERT OR REPLACE INTO {self.table} (key, tokens, updated_at) VALUES (?, ?, ?)", (key, level, now))
            return wait

        return await self.db.write(op)

    async def try_acquire(self, key: str = "default", tokens: float = 1) -> bool:
        if tokens > self.capacity:
            raise ValueError("一次请求的令牌数不能超过桶容量")
        return await self._take(key, tokens) == 0.0

    async def acquire(self, key: str = "default", tokens: float = 1, timeout: float | None = None) -> bool:
        """等到拿到令牌为止；超过 timeout 仍拿不到则返回 False。等待时加一点抖动，避免多个进程同时醒来抢同一个令牌。"""
        if tokens > self.capacity:
            raise ValueError("一次请求的令牌数不能超过桶容量")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = await self._take(key, tokens)
            if wait == 0.0:
                return True
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(wait * random.uniform(1.0, 1.2))


class SQLiteSemaphore(_Owner):
    """跨进程的并发槽位（分布式信号量）：所有 worker 进程加起来，同一个 name 最多 limit 个同时持有者。

    典型用途：模型网关只给了 20 个并发配额，而你有 4 个 worker 进程、每个进程 16 个并发任务。
    进程内的 asyncio.Semaphore 管不住"4 × 16 = 64"；这里的槽位存在数据库里，四个进程共用。

    **崩溃安全**：槽位是带租约的（lease_seconds），持有期间后台自动续约。持有者进程被 kill -9，
    续约停止，租约到期后槽位自动释放 —— 否则一次崩溃就会永久吃掉一个名额（multiprocessing.Semaphore 就有这个问题）。
    """

    def __init__(self, path_or_db: str | Path | SQLiteDB, name: str, limit: int, table: str = "semaphore_slots", *,
                 lease_seconds: float = 30.0, poll_interval: float = 0.05, clock: Callable[[], float] = time.time):
        if limit < 1:
            raise ValueError("limit 至少为 1")
        super().__init__(path_or_db, table)
        self.name, self.limit, self.lease_seconds, self.poll_interval, self.clock = name, limit, lease_seconds, poll_interval, clock

    async def setup(self) -> None:
        def ddl(conn):
            conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (holder TEXT PRIMARY KEY, name TEXT NOT NULL, "
                         f"expires_at REAL NOT NULL)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_name_idx ON {self.table} (name, expires_at)")

        await self.db.write(ddl)

    async def _try(self, holder: str) -> bool:
        def op(conn):
            now = self.clock()
            conn.execute(f"DELETE FROM {self.table} WHERE name = ? AND expires_at < ?", (self.name, now))
            n = conn.execute(f"SELECT count(*) FROM {self.table} WHERE name = ?", (self.name,)).fetchone()[0]
            if n >= self.limit:
                return False
            conn.execute(f"INSERT INTO {self.table} (holder, name, expires_at) VALUES (?, ?, ?)",
                         (holder, self.name, now + self.lease_seconds))
            return True

        return await self.db.write(op)

    async def in_use(self) -> int:
        return await self.db.run(lambda conn: conn.execute(
            f"SELECT count(*) FROM {self.table} WHERE name = ? AND expires_at >= ?", (self.name, self.clock())
        ).fetchone()[0])

    @contextlib.asynccontextmanager
    async def slot(self, timeout: float | None = None) -> AsyncIterator[str]:
        """占用一个槽位；timeout 秒内拿不到就抛 LimitExceeded。yield 持有者 id。"""
        holder = f"{uuid.uuid4().hex}"
        deadline = None if timeout is None else time.monotonic() + timeout
        while not await self._try(holder):
            if deadline is not None and time.monotonic() >= deadline:
                raise LimitExceeded(f"{self.name} 的 {self.limit} 个跨进程槽位已满，等待 {timeout:.2f}s 后仍未获得")
            await asyncio.sleep(self.poll_interval * random.uniform(0.5, 1.5))

        async def renew():
            while True:
                await asyncio.sleep(self.lease_seconds / 3)
                await self.db.write(lambda conn: conn.execute(
                    f"UPDATE {self.table} SET expires_at = ? WHERE holder = ?", (self.clock() + self.lease_seconds, holder)
                ))

        renewer = asyncio.create_task(renew())
        try:
            yield holder
        finally:
            renewer.cancel()
            # 释放放进 shield：调用方被取消时也要把槽位还回去，否则要等租约过期
            await asyncio.shield(self.db.write(lambda conn: conn.execute(f"DELETE FROM {self.table} WHERE holder = ?", (holder,))))


# =====================================================================================
# 跨进程熔断器
# =====================================================================================


class SQLiteCircuitBreaker(_Owner):
    """所有 worker 进程共享的熔断器（与 reliability.CircuitBreaker 同一个状态机，状态存在数据库里）。

    进程内的熔断器：worker A 连续失败 5 次已经熔断，worker B、C 还各自要再失败 5 次才会熔断 ——
    下游已经挂了，你还要再往它身上打 10 个请求；而且每个新扩容出来的进程都从 closed 开始，重新试错。
    这里的失败计数、熔断时间、"谁在试探"都在数据库里，一个进程熔断，所有进程立刻都知道。

    半开时的**单个试探**用带过期时间的"试探租约"（probe_until）实现：拿到租约的进程去试探，
    其余进程继续快速失败；试探者如果中途崩溃，租约过期后别的进程可以接着试探，不会永远卡在半开。

    用法：ResilientLLM(primary, breaker_factory=lambda model: SQLiteCircuitBreaker(db, model))
    """

    def __init__(
        self,
        path_or_db: str | Path | SQLiteDB,
        name: str = "llm",
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        *,
        record_if: Callable[[Exception], bool] | None = None,
        probe_timeout: float = 60.0,
        table: str = "circuit_breakers",
        clock: Callable[[], float] = time.time,
    ):
        super().__init__(path_or_db, table)
        self.name, self.failure_threshold, self.reset_timeout = name, failure_threshold, reset_timeout
        self.record_if, self.probe_timeout, self.clock = record_if, probe_timeout, clock
        self._ready = False

    async def setup(self) -> None:
        await self.db.write(lambda conn: conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} (name TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, "
            f"opened_at REAL, probe_until REAL)"
        ))
        self._ready = True

    async def _ensure(self) -> None:
        if not self._ready:
            await self.setup()

    def _state_of(self, row, now: float) -> str:
        if row is None or row["opened_at"] is None:
            return "closed"
        return "half_open" if now - row["opened_at"] >= self.reset_timeout else "open"

    async def current_state(self) -> str:
        await self._ensure()
        row = await self.db.run(lambda conn: conn.execute(
            f"SELECT failures, opened_at, probe_until FROM {self.table} WHERE name = ?", (self.name,)
        ).fetchone())
        return self._state_of(row, self.clock())

    async def _enter(self) -> bool:
        """放行检查。返回 True 表示本次是半开状态下的试探请求。熔断中 / 别人正在试探 → CircuitOpenError。"""
        def op(conn):
            now = self.clock()
            row = conn.execute(f"SELECT failures, opened_at, probe_until FROM {self.table} WHERE name = ?", (self.name,)).fetchone()
            state = self._state_of(row, now)
            if state == "open":
                raise CircuitOpenError(self.name)
            if state == "half_open":
                if row["probe_until"] is not None and row["probe_until"] > now:
                    raise CircuitOpenError(self.name)  # 已经有一个进程在试探
                conn.execute(f"UPDATE {self.table} SET probe_until = ? WHERE name = ?", (now + self.probe_timeout, self.name))
                return True
            return False

        return await self.db.write(op)

    async def record_failure(self, probe: bool = False) -> None:
        await self._ensure()

        def op(conn):
            now = self.clock()
            conn.execute(f"INSERT OR IGNORE INTO {self.table} (name, failures) VALUES (?, 0)", (self.name,))
            row = conn.execute(f"SELECT failures, opened_at FROM {self.table} WHERE name = ?", (self.name,)).fetchone()
            failures = row["failures"] + 1
            reopen = probe or self._state_of(row, now) == "half_open" or failures >= self.failure_threshold
            conn.execute(
                f"UPDATE {self.table} SET failures = ?, opened_at = CASE WHEN ? THEN ? ELSE opened_at END, probe_until = NULL "
                f"WHERE name = ?", (failures, reopen, now, self.name),
            )

        await self.db.write(op)

    async def record_success(self) -> None:
        await self._ensure()
        await self.db.write(lambda conn: conn.execute(
            f"INSERT OR REPLACE INTO {self.table} (name, failures, opened_at, probe_until) VALUES (?, 0, NULL, NULL)", (self.name,)
        ))

    async def call(self, fn: Callable[[], Any]) -> Any:
        await self._ensure()
        probe = await self._enter()
        try:
            result = await fn()
        except Exception as e:
            if self.record_if is not None and not self.record_if(e):
                if probe:  # 试探请求因为"请求自身的问题"失败：不算下游不健康，但要把试探租约还回去
                    await self.db.write(lambda conn: conn.execute(
                        f"UPDATE {self.table} SET probe_until = NULL WHERE name = ?", (self.name,)))
                raise
            await self.record_failure(probe=probe)
            raise
        await self.record_success()
        return result

