"""SQLite 租约队列（lease queue）：让多个 worker 进程安全地分活干。

为什么用 SQLite 模拟"分布式"？
    多个**独立进程**同时读写**同一个数据库文件**。进程之间不共享任何内存，只能通过数据库协作，
    这和"多台机器上的 worker + 一个共享的 Postgres / Redis"在并发语义上是一回事：
    谁先抢到、谁覆盖了谁、谁崩溃了留下什么，全都是真实发生的竞争，不是用 sleep 演出来的。
    区别只在于：SQLite 同一时刻只允许一个写者，而且只能在同一台机器上用（WAL 模式不支持网络文件系统）。
    上生产时换成 Postgres（SELECT ... FOR UPDATE SKIP LOCKED）或托管队列（SQS 等），思路不变。

任务的一生（详见 README 的状态图）：

    enqueue ─► queued ──claim──► leased ──complete──► succeeded
                 ▲                 │
                 │                 ├──fail（可重试，还有次数）──► queued（退避一段时间后再领）
                 │                 ├──fail（不可重试）─────────► failed
                 │                 └──fail（次数用尽）─────────► dead（死信）
                 └── 租约过期：别人可以重新 claim（fence +1）；次数已用尽则直接进 dead

三个关键设计：
1. 租约（lease）：领取不是"拿走"，而是"借走一段时间"。worker 崩溃后租约到期，任务自动可以被别人领 → 不丢任务。
2. fencing token（fence 列）：每次领取 +1。heartbeat / complete / fail 都必须带上领取时拿到的 fence，
   对不上就拒绝 → 租约过期后"诈尸"的旧 worker 没法覆盖新 worker 的结果。
3. 尝试次数上限 + 死信：一个每次都让 worker 崩溃的"毒消息"，最多害死 max_attempts 个 worker，不会无限循环。
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    tenant_id       TEXT    NOT NULL,
    payload         TEXT    NOT NULL,                  -- JSON
    idempotency_key TEXT    NOT NULL UNIQUE,           -- 入队去重：同一个 key 只会有一个任务
    group_key       TEXT,                              -- 同组任务串行 + 按序处理（如 session_id）；NULL = 不分组
    status          TEXT    NOT NULL DEFAULT 'queued', -- queued / leased / succeeded / failed / dead
    attempts        INTEGER NOT NULL DEFAULT 0,        -- 被领取过几次（崩溃也算一次）
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    fence           INTEGER NOT NULL DEFAULT 0,        -- fencing token：每次领取 +1，只增不减
    worker_id       TEXT,                              -- 当前（或最后一个）持有者
    lease_until     REAL,                              -- 租约到期时间（unix 秒）
    available_at    REAL    NOT NULL,                  -- 早于这个时间不许领取（重试退避 / 延迟任务）
    result          TEXT,
    last_error      TEXT,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs (status, available_at);
CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs (group_key, id);
"""

ACTIVE = ("queued", "leased")
TERMINAL = ("succeeded", "failed", "dead")

# "可领取"的完整定义（表别名 j，命名参数 :now）。三个条件缺一不可：
#   ① 排队中且到了可执行时间，或者租约已过期（持有者大概率崩溃或卡死了）；
#   ② 还没用完尝试次数（用完了的交给死信处理，不能再发出去害人）；
#   ③ 分组任务必须是组里最老的未完成任务 → 同组任务同一时刻最多一个在跑，并且严格按入队顺序处理。
CLAIMABLE_WHERE = """
    ((j.status = 'queued' AND j.available_at <= :now)
      OR (j.status = 'leased' AND j.lease_until < :now))
    AND j.attempts < j.max_attempts
    AND (j.group_key IS NULL OR NOT EXISTS (
          SELECT 1 FROM jobs AS e
          WHERE e.group_key = j.group_key AND e.id < j.id AND e.status IN ('queued', 'leased')))
"""


class LeaseLostError(Exception):
    """租约已丢失：任务已被别人重新领取（你的 fence 过期了），或者任务已经结束。

    收到它的 worker 必须立刻停手：不再提交结果、不再产生副作用。
    这不是"出错了重试一下"的错误 —— 重试只会再被拒绝一次。
    """


@dataclass
class Job:
    id: int
    tenant_id: str
    payload: dict
    idempotency_key: str
    group_key: str | None
    status: str
    attempts: int
    max_attempts: int
    fence: int
    worker_id: str | None
    lease_until: float | None
    available_at: float
    result: str | None
    last_error: str | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return cls(**data)


# ---------------------------------------------------------------------- 连接与事务


def connect(path: str | Path, timeout: float = 10.0) -> sqlite3.Connection:
    """打开一个连接。**每个线程、每个进程都要用自己的连接**（sqlite3 连接不能跨线程共享）。

    - isolation_level=None：自动提交模式。Python sqlite3 默认会"偷偷"帮你开事务，
      并发场景下我们要自己决定事务从哪里开始（见 write_txn）；
    - WAL：读和写互不阻塞，只有"写和写"互斥（SQLite 同一时刻只有一个写者）；
    - busy_timeout：遇到别人正在写，先排队等最多 timeout 秒，而不是立刻报 "database is locked"；
    - synchronous=NORMAL：WAL 下的常用搭配，进程崩溃不丢已提交的数据（断电可能丢最后几个事务）。
    """
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE —— 事务一开始就拿到写锁。

    为什么不用默认的 BEGIN（DEFERRED）？"先 SELECT 再 UPDATE"时，DEFERRED 事务要把读锁升级成写锁；
    如果这期间别的连接写过库，升级会**立刻**失败（SQLITE_BUSY_SNAPSHOT，报 database is locked），
    busy_timeout 也救不了。IMMEDIATE 会在 BEGIN 这一步按 busy_timeout 排队等写锁，
    一旦拿到，SQLite 保证到 COMMIT 为止不会再遇到 SQLITE_BUSY：SELECT 和 UPDATE 之间不可能插进别人的写。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def explain_lost(conn: sqlite3.Connection, job_id: int, fence: int) -> str:
    """生成一句人能看懂的"为什么你的租约没了"，方便排查。"""
    job = get_job(conn, job_id)
    if job is None:
        return f"任务 #{job_id} 不存在"
    if job.fence != fence:
        return (f"任务 #{job_id} 已被重新领取：当前 fence={job.fence}（持有者 {job.worker_id}），"
                f"你手里的 fence={fence} 已经过期")
    return f"任务 #{job_id} 当前状态是 {job.status}，不能再由你提交"


# ---------------------------------------------------------------------- 队列


class JobQueue:
    """一个完整的租约队列。所有方法都可以被多个进程同时调用（每个进程 new 一个自己的 JobQueue）。

    clock 可注入：生产用 time.time（多个进程/机器要比较同一个时间，所以不能用 monotonic），测试用假时钟。
    注意这也意味着租约依赖各机器时钟大致一致 —— 真实系统通常以**数据库服务器的时钟**判断租约是否过期。
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time, timeout: float = 10.0):
        self.path = Path(path)
        self.clock = clock
        self.conn = connect(path, timeout)
        init_db(self.conn)

    def close(self) -> None:
        self.conn.close()

    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else now

    # ------------------------------------------------------------------ 生产者

    def enqueue(
        self,
        tenant_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        group_key: str | None = None,
        max_attempts: int = 3,
        delay_s: float = 0.0,
        now: float | None = None,
    ) -> tuple[int, bool]:
        """入队。返回 (job_id, 是否新建)。

        相同 idempotency_key 重复入队（用户连点两次、上游重试、网关超时后客户端重发）→ 返回已有任务，不会建第二个。
        同一个 key 却带着不同内容 → 多半是 key 生成有 bug，直接报错，而不是悄悄返回旧任务。
        """
        now = self._now(now)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cur = self.conn.execute(
            """INSERT INTO jobs (tenant_id, payload, idempotency_key, group_key, max_attempts,
                                 available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (idempotency_key) DO NOTHING""",
            (tenant_id, body, idempotency_key, group_key, max_attempts, now + delay_s, now, now),
        )
        if cur.rowcount == 1:
            return cur.lastrowid, True
        row = self.conn.execute(
            "SELECT id, tenant_id, payload FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row["tenant_id"] != tenant_id or row["payload"] != body:
            raise ValueError(f"幂等键 {idempotency_key!r} 已被一个内容不同的任务占用：请检查幂等键的生成逻辑")
        return row["id"], False

    # ------------------------------------------------------------------ 消费者

    def claim(self, worker_id: str, lease_seconds: float, *, now: float | None = None) -> Job | None:
        """原子地领取一个任务（没有可领的返回 None）。

        写法：BEGIN IMMEDIATE 拿写锁 → SELECT 找候选 → UPDATE 领取 → COMMIT。
        写锁保证了"找"和"领"之间不会有别人插队，所以 8 个 worker 同时来抢也不会领到同一个任务。
        （练习 exercise.py 里你会用另一种写法：不开事务，用"带条件的 UPDATE + 检查影响行数"。）
        """
        now = self._now(now)
        with write_txn(self.conn) as c:
            # ① 毒消息兜底：租约过期、而且已经用完尝试次数 → 说明 worker 每次处理它都崩溃/卡死，送进死信
            c.execute(
                """UPDATE jobs
                   SET status = 'dead', worker_id = NULL, lease_until = NULL, updated_at = :now,
                       last_error = COALESCE(last_error || ' | ', '') || '租约过期且尝试次数已用完（处理它的 worker 可能每次都崩溃）'
                   WHERE status = 'leased' AND lease_until < :now AND attempts >= max_attempts""",
                {"now": now},
            )
            # ② 找最老的一个可领取任务
            row = c.execute(
                f"SELECT j.id FROM jobs AS j WHERE {CLAIMABLE_WHERE} ORDER BY j.id LIMIT 1", {"now": now}
            ).fetchone()
            if row is None:
                return None
            # ③ 领取：写上持有者和租约，尝试次数 +1，fencing token +1
            c.execute(
                """UPDATE jobs
                   SET status = 'leased', worker_id = :worker, lease_until = :until,
                       attempts = attempts + 1, fence = fence + 1, updated_at = :now
                   WHERE id = :id""",
                {"worker": worker_id, "until": now + lease_seconds, "now": now, "id": row["id"]},
            )
            return get_job(c, row["id"])

    def heartbeat(self, job_id: int, fence: int, lease_seconds: float, *, now: float | None = None) -> float:
        """续约：把租约延长到 now + lease_seconds。返回新的到期时间。

        长任务（一个 Agent 可能跑几分钟）不要设一个超长租约，而是"短租约 + 定期心跳"：
        worker 活着就一直续；worker 一死，心跳停止，最多一个租约周期后任务就能被别人接手。
        fence 对不上（已被别人接手）→ LeaseLostError，worker 应立刻停止处理。
        """
        now = self._now(now)
        until = now + lease_seconds
        cur = self.conn.execute(
            "UPDATE jobs SET lease_until = ?, updated_at = ? WHERE id = ? AND fence = ? AND status = 'leased'",
            (until, now, job_id, fence),
        )
        if cur.rowcount != 1:
            raise LeaseLostError(explain_lost(self.conn, job_id, fence))
        return until

    def complete(self, job_id: int, fence: int, result: str = "", *, now: float | None = None) -> None:
        """提交成功结果。必须带上领取时拿到的 fence。

        只看 fence、不看租约是否过期：租约过期但还没人接手时，迟到的结果依然有效（避免白白重做）；
        一旦有人接手（fence 变了），旧持有者的提交一律拒绝。
        """
        now = self._now(now)
        cur = self.conn.execute(
            """UPDATE jobs SET status = 'succeeded', result = ?, lease_until = NULL, updated_at = ?
               WHERE id = ? AND fence = ? AND status = 'leased'""",
            (result, now, job_id, fence),
        )
        if cur.rowcount != 1:
            raise LeaseLostError(explain_lost(self.conn, job_id, fence))

    def fail(
        self,
        job_id: int,
        fence: int,
        error: str,
        *,
        retryable: bool = True,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> str:
        """报告失败。返回任务的新状态：

        - queued：可重试且还有次数 → 指数退避 + 全抖动后再放回队列（别让所有失败任务同一秒卷土重来）；
        - failed：不可重试（参数非法、权限不足……）→ 重试也没用，直接结束，通知用户；
        - dead：可重试但次数用尽 → 进死信，等人排查（修好后可以 redrive 重新投递）。
        """
        now = self._now(now)
        with write_txn(self.conn) as c:
            row = c.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id = ? AND fence = ? AND status = 'leased'",
                (job_id, fence),
            ).fetchone()
            if row is None:
                raise LeaseLostError(explain_lost(c, job_id, fence))
            available_at = now
            if not retryable:
                status = "failed"
            elif row["attempts"] >= row["max_attempts"]:
                status = "dead"
            else:
                status = "queued"
                upper = min(max_backoff_s, base_backoff_s * 2 ** (row["attempts"] - 1))
                available_at = now + (rng or random).uniform(0, upper)
            c.execute(
                """UPDATE jobs SET status = ?, worker_id = NULL, lease_until = NULL, available_at = ?,
                                   last_error = ?, updated_at = ?
                   WHERE id = ?""",
                (status, available_at, error, now, job_id),
            )
        return status

    # ------------------------------------------------------------------ 运维

    def redrive(self, job_id: int, *, now: float | None = None) -> None:
        """把死信任务重新投递（人工排查、修好 bug 之后）。尝试次数清零，fence 保留继续递增。"""
        now = self._now(now)
        cur = self.conn.execute(
            """UPDATE jobs SET status = 'queued', attempts = 0, available_at = ?, updated_at = ?
               WHERE id = ? AND status = 'dead'""",
            (now, now, job_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"任务 #{job_id} 不在死信里")

    def get(self, job_id: int) -> Job | None:
        return get_job(self.conn, job_id)

    def list_jobs(self, status: str | None = None) -> list[Job]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,)).fetchall()
        return [Job.from_row(r) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def all_done(self) -> bool:
        """所有任务都进入了终态（succeeded / failed / dead）。"""
        row = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'leased')").fetchone()
        return row[0] == 0
