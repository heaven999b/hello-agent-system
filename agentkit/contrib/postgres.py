"""Postgres 适配器：检查点（版本号 CAS + fence 接管）、SKIP LOCKED 任务队列、worker 循环（第 26 课）。

第 13 课用 SQLite 讲清了租约、fencing token、CAS 的原理；这里是同一套思想的生产版：
换成 Postgres（多机共享、行级锁、服务器时钟），并且直接实现 agentkit 的接口。

    同步版（agentkit.Agent）                 异步版（agentkit.aio.AsyncAgent，一个进程同时跑几百个会话）
    PostgresCheckpointer                     AsyncPostgresCheckpointer      Checkpointer 协议：save / load
    PostgresJobQueue                         AsyncPostgresJobQueue          enqueue / claim / heartbeat / complete / fail ...
    run_worker（一个进程一次一个任务）       run_async_worker（一个进程同时 concurrency 个任务，带背压）
    AgentJobHandler：把"运行 / 恢复一个 Agent"包装成队列任务（含审批后的 resume）；异步版所有任务共用一个 AsyncAgent

两个版本共用同一套 SQL，语义完全一致。

依赖：psycopg 3 和 psycopg_pool（pip install -e ".[postgres]"，即 psycopg[binary,pool]）。
连接参数可以是连接串，也可以是现成的连接池（同步：psycopg_pool.ConnectionPool；异步：AsyncConnectionPool），
多个对象共用一个连接池时，池的大小要按"同时需要连接的线程 / 协程数"来定（README 卡片 6、7）。

多进程使用时，每个进程用连接串**自己**创建这些对象：数据库连接不能跨进程共享。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import re
import signal
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Iterable, Iterator, Protocol

from ..aio.timeouts import wait_for
from ..hooks import Hook, StopRun
from ..state import RunState
from . import require

psycopg = require("psycopg", "postgres")
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

__all__ = [
    "AgentJobHandler",
    "AsyncPostgresCheckpointer",
    "AsyncPostgresJobQueue",
    "CheckpointConflict",
    "Job",
    "LeaseLost",
    "PermanentJobError",
    "PostgresCheckpointer",
    "PostgresJobQueue",
    "RetryLater",
    "run_async_worker",
    "run_worker",
    "stop_on_signals",
]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,50}$")


# =====================================================================================
# 异常
# =====================================================================================


class CheckpointConflict(RuntimeError):
    """检查点的版本号对不上：在你上次读 / 写之后，另一个 worker 已经写过或接管了这个 run。

    正确的反应是**立刻停手**（不要重试、不要覆盖）：你手里的状态已经过时了。
    """

    def __init__(self, run_id: str, expected: int | None, actual: int | None, writer: str | None = None, detail: str = ""):
        self.run_id, self.expected, self.actual, self.writer = run_id, expected, actual, writer
        msg = f"检查点冲突：run {run_id} 期望版本 {expected}，实际版本 {actual}"
        if writer:
            msg += f"（最后写入者 {writer}）"
        msg += " —— 另一个 worker 已经接管了这个 run，停止处理，不要覆盖。"
        if detail:
            msg += f" {detail}"
        super().__init__(msg)


class LeaseLost(RuntimeError):
    """带 fence 的写入（心跳 / 提交 / 失败 / 归还）被拒绝：任务已经不归你了。"""


class RetryLater(Exception):
    """handler 抛出它表示"现在做不了，过一会儿再来"（如被限流）：任务放回队列，**不消耗**重试次数。"""

    def __init__(self, delay_seconds: float, reason: str = ""):
        super().__init__(reason or f"retry after {delay_seconds}s")
        self.delay_seconds = float(delay_seconds)
        self.reason = reason


class PermanentJobError(Exception):
    """handler 抛出它表示"再试也没用"（参数非法、找不到 run、租户不匹配）：任务直接进入 failed。"""


# =====================================================================================
# 连接管理
# =====================================================================================


class _Db:
    """同步：连接串 → 每个线程一个长连接（autocommit）；连接池 → 每次操作借一个连接。

    为什么不是"整个进程共用一个连接"？psycopg 的连接可以被多个线程使用，但操作会被串行化，
    而且事务是连接级别的：心跳线程和主线程共用连接时，一个线程的事务会把另一个线程的语句卷进去。
    """

    def __init__(self, conninfo, connect_kwargs: dict | None = None):
        self.pool = None if isinstance(conninfo, str) else conninfo
        self.dsn = conninfo if isinstance(conninfo, str) else None
        if self.pool is not None and not hasattr(self.pool, "connection"):
            raise TypeError("conninfo 必须是连接串，或带 .connection() 方法的连接池（如 psycopg_pool.ConnectionPool）")
        self.connect_kwargs = dict(connect_kwargs or {})
        self._local = threading.local()
        self._opened: list = []
        self._lock = threading.Lock()

    @contextmanager
    def conn(self) -> Iterator["psycopg.Connection"]:
        if self.pool is not None:
            with self.pool.connection() as c:  # psycopg_pool：正常退出时提交，异常时回滚，然后归还
                yield c
            return
        c = getattr(self._local, "conn", None)
        if c is None or c.closed or c.broken:  # 连接断了（数据库重启 / 主从切换）：下次调用时重连
            c = psycopg.connect(self.dsn, autocommit=True, **self.connect_kwargs)
            self._local.conn = c
            with self._lock:
                self._opened.append(c)
        yield c

    def rows(self, query, params: dict | None = None) -> list[dict]:
        with self.conn() as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []

    def release_thread(self) -> None:
        """关闭当前线程的连接（线程结束前调用，否则连接会一直占着，直到 close()）。"""
        c = getattr(self._local, "conn", None)
        if c is not None:
            self._local.conn = None
            with self._lock:
                self._opened = [x for x in self._opened if x is not c]
            c.close()

    def close(self) -> None:
        with self._lock:
            opened, self._opened = self._opened, []
        for c in opened:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


class _AsyncDb:
    """异步：所有协程共用一个 AsyncConnectionPool，每次操作借一个连接、用完立刻归还。

    池满时协程在池里排队（默认最多等 30 秒，超时抛 PoolTimeout）—— 所以池的 max_size 要和并发度匹配。
    传入连接串时自己创建池（autocommit，默认 min_size=1、max_size=10），第一次使用时才打开；
    传入现成的池时不负责关闭它。
    """

    def __init__(self, conninfo, pool_kwargs: dict | None = None):
        if isinstance(conninfo, str):
            psycopg_pool = require("psycopg_pool", "postgres")
            kw = {"min_size": 1, "max_size": 10, **(pool_kwargs or {})}
            kw["kwargs"] = {"autocommit": True, **kw.get("kwargs", {})}
            self.pool = psycopg_pool.AsyncConnectionPool(conninfo, open=False, **kw)
            self._owned = True
        else:
            if not hasattr(conninfo, "connection"):
                raise TypeError("conninfo 必须是连接串，或 psycopg_pool.AsyncConnectionPool")
            self.pool = conninfo
            self._owned = False
        self._opened = False
        self._open_lock = asyncio.Lock()

    @asynccontextmanager
    async def conn(self) -> AsyncIterator["psycopg.AsyncConnection"]:
        if self._owned and not self._opened:
            async with self._open_lock:
                if not self._opened:
                    await self.pool.open(wait=True)
                    self._opened = True
        async with self.pool.connection() as c:
            yield c

    async def rows(self, query, params: dict | None = None) -> list[dict]:
        async with self.conn() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return (await cur.fetchall()) if cur.description else []

    async def close(self) -> None:
        if self._owned and self._opened:
            self._opened = False
            await self.pool.close()


def _ident(name: str, what: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"{what} 只能包含字母、数字、下划线，且不以数字开头：{name!r}")
    return name


def _scrub(obj: Any) -> Any:
    """Postgres 的 text / jsonb 不能存 NUL 字符（\\u0000）。工具输出里偶尔会有（二进制文件、奇怪的网页），
    一个 NUL 就会让整个检查点写入失败 → 这里替换成 U+FFFD，保证"存得进去"比"逐字节保真"更重要。"""
    if isinstance(obj, str):
        return obj.replace("\x00", "�") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {(_scrub(k) if isinstance(k, str) else k): _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _jsonb(obj: Any) -> Jsonb:
    return Jsonb(_scrub(obj), dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


_SETUP_LOCK = "SELECT pg_advisory_xact_lock(hashtext(%s))"


def _setup(db: _Db, lock_name: str, statements: list) -> None:
    """幂等建表。多个进程同时启动时，并发的 CREATE TABLE IF NOT EXISTS 会互相撞车
    （实测 8 个连接同时执行，7 个报 UniqueViolation: pg_class_relname_nsp_index），
    所以用事务级 advisory lock 把它们排成一队。生产中更推荐用迁移工具（Alembic / Flyway）在发布时执行一次。"""
    with db.conn() as c, c.transaction():
        c.execute(_SETUP_LOCK, (f"agentkit.setup.{lock_name}",))
        for stmt in statements:
            c.execute(stmt)


async def _asetup(db: _AsyncDb, lock_name: str, statements: list) -> None:
    async with db.conn() as c, c.transaction():
        await c.execute(_SETUP_LOCK, (f"agentkit.setup.{lock_name}",))
        for stmt in statements:
            await c.execute(stmt)


# =====================================================================================
# 检查点
# =====================================================================================


def _checkpoint_ddl(table: str) -> list:
    t = sql.Identifier(table)
    return [
        sql.SQL(
            """CREATE TABLE IF NOT EXISTS {t} (
                run_id     text PRIMARY KEY,
                version    bigint NOT NULL,
                fence      bigint NOT NULL DEFAULT 0,
                status     text NOT NULL,
                tenant_id  text,
                writer     text,
                state      jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now())"""
        ).format(t=t),
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (status, updated_at DESC)").format(
            i=sql.Identifier(f"{table}_status_idx"), t=t
        ),
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (tenant_id, status, updated_at DESC)").format(
            i=sql.Identifier(f"{table}_tenant_idx"), t=t
        ),
    ]


def _checkpoint_sql(table: str) -> dict:
    t = sql.Identifier(table)
    summary = sql.SQL(
        "SELECT run_id, status, tenant_id, version, fence, writer, created_at, updated_at, "
        "state->'metadata'->>'user_id' AS user_id, state->'pending' AS pending, "
        "state->>'stop_reason' AS stop_reason, state->>'output' AS output FROM {t} "
    ).format(t=t)
    return {
        "select": sql.SQL("SELECT version, fence, writer, state FROM {t} WHERE run_id = %(run_id)s").format(t=t),
        # 接管：fence 不比表里小才允许；把表里的 fence 改成自己的，并让 version + 1 —— 旧持有者手里的版本号立刻作废
        "takeover": sql.SQL(
            "UPDATE {t} SET fence = %(fence)s, version = version + 1, writer = %(writer)s, updated_at = now() "
            "WHERE run_id = %(run_id)s AND fence <= %(fence)s RETURNING version, state"
        ).format(t=t),
        "insert": sql.SQL(
            "INSERT INTO {t} (run_id, version, fence, status, tenant_id, writer, state) "
            "VALUES (%(run_id)s, 1, %(fence)s, %(status)s, %(tenant_id)s, %(writer)s, %(state)s) "
            "ON CONFLICT (run_id) DO NOTHING RETURNING version"
        ).format(t=t),
        # CAS：只有版本号还是"我上次看到的那个"才写得进去
        "update": sql.SQL(
            "UPDATE {t} SET state = %(state)s, status = %(status)s, tenant_id = %(tenant_id)s, "
            "writer = %(writer)s, version = version + 1, updated_at = now() "
            "WHERE run_id = %(run_id)s AND version = %(expected)s RETURNING version"
        ).format(t=t),
        "get_run": sql.SQL("{s} WHERE run_id = %(run_id)s").format(s=summary),
        "summary": summary,
    }


class _CheckpointBase:
    """同步版和异步版共用的部分：SQL、版本号记录、参数与冲突信息的构造。"""

    def __init__(self, table: str, fence: int | None, writer: str | None):
        self.table = _ident(table, "table")
        self.fence = fence
        self.writer = writer
        self._versions: dict[str, int] = {}
        self._vlock = threading.Lock()
        self._sql = _checkpoint_sql(self.table)

    def version_of(self, run_id: str) -> int | None:
        """本实例记住的版本号（没读写过则为 None）。"""
        with self._vlock:
            return self._versions.get(run_id)

    def _remember(self, run_id: str, version: int, status: str | None = None) -> None:
        """记住版本号，供下一次 CAS 使用。

        保存了非 running 的状态（完成、暂停、中止……）说明这次运行已经告一段落，版本号随即丢掉：
        恢复和审批都会先 load（重新记住最新版本），而一个共享的检查点实例要服务成千上万个运行，
        只增不减的字典就是内存泄漏（第 31 课压测中发现）。"""
        with self._vlock:
            if status is None or status == "running":
                self._versions[run_id] = version
            else:
                self._versions.pop(run_id, None)

    def _save_params(self, state: RunState) -> tuple[str, dict]:
        expected = self.version_of(state.run_id)
        params = {
            "run_id": state.run_id,
            "state": _jsonb(state.to_dict()),
            "status": state.status,
            "tenant_id": state.metadata.get("tenant_id"),
            "writer": self.writer,
            "fence": self.fence or 0,
            "expected": expected,
        }
        return ("insert" if expected is None else "update"), params

    def _conflict(self, run_id: str, expected: int | None, current: list[dict]) -> CheckpointConflict:
        cur = current[0] if current else {"version": None, "writer": None}
        detail = "（新建时发现它已经存在：另一个 worker 抢先创建了它）" if expected is None else ""
        return CheckpointConflict(run_id, expected, cur["version"], cur["writer"], detail=detail)

    def _stale_fence(self, run_id: str, current: dict) -> CheckpointConflict:
        return CheckpointConflict(
            run_id, None, current["version"], current["writer"],
            detail=f"表里的 fence={current['fence']} 比你的 fence={self.fence} 新：你是被取代的旧持有者。",
        )

    def _list_query(self, status: str | None, tenant_id: str | None, limit: int):
        where, params = [], {"limit": int(limit)}
        if status is not None:
            where.append(sql.SQL("status = %(status)s"))
            params["status"] = status
        if tenant_id is not None:
            where.append(sql.SQL("tenant_id = %(tenant_id)s"))
            params["tenant_id"] = tenant_id
        clause = sql.SQL("WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")
        return sql.SQL("{s} {w} ORDER BY updated_at DESC LIMIT %(limit)s").format(s=self._sql["summary"], w=clause), params


class PostgresCheckpointer(_CheckpointBase):
    """Postgres 检查点：实现 agentkit 的 Checkpointer 协议（save / load），可直接传给 Agent。

    表结构（setup() 创建）：
        run_id PK | version | fence | status | tenant_id | writer | state jsonb | created_at | updated_at
        + (status, updated_at) 和 (tenant_id, status, updated_at) 两个索引：审批收件箱、运维查询用

    **乐观并发（版本号 CAS）**：本实例记住每个 run 最后读到 / 写入的版本号 v，
        save：UPDATE ... SET version = version + 1 WHERE run_id = ? AND version = v
        更新到 0 行 → 在你之后有人写过 → 抛 CheckpointConflict。新 run 用 INSERT（主键冲突同样是冲突）。
    这就是第 13 课 6.7 节说的"检查点写入也要 fencing"：旧 worker 手里的版本号已经过时，写不进去，
    也就不可能用过时的状态覆盖新 worker 的检查点（FileCheckpointer 做不到这一点）。

    **fence 接管**（fenced(fence) 返回的视图）：纯 CAS 是"先写者赢" —— 僵尸 worker 和新 worker 读到同一个
    版本时，谁先写谁赢，输的可能恰恰是新 worker。带 fence 的 load 会把表里的 fence 更新为自己的 fence，
    并把 version 加一（"接管"），于是旧持有者从这一刻起的任何写入都会冲突；fence 比表里小的 load 直接被拒绝。
    队列每次领取都从全局序列拿一个更大的 fence（同一个 run 后续的 resume 任务也一样），所以 AgentJobHandler
    用 job.fence 创建视图：**最新的租约持有者总是赢家**。
    """

    def __init__(
        self,
        conninfo,
        table: str = "agent_runs",
        *,
        fence: int | None = None,
        writer: str | None = None,
        connect_kwargs: dict | None = None,
        _db: _Db | None = None,
    ):
        super().__init__(table, fence, writer)
        self._db = _db or _Db(conninfo, connect_kwargs)

    def setup(self) -> None:
        """建表 + 索引（幂等，可并发调用）。"""
        _setup(self._db, self.table, _checkpoint_ddl(self.table))

    def fenced(self, fence: int, writer: str | None = None) -> "PostgresCheckpointer":
        """返回一个带 fence 的视图（共享连接，版本号记录各自独立）。每领取一次任务创建一个。"""
        w = writer if writer is not None else self.writer
        return PostgresCheckpointer("", self.table, fence=int(fence), writer=w, _db=self._db)

    def close(self) -> None:
        self._db.close()

    def load(self, run_id: str) -> RunState | None:
        """读检查点并记住版本号。带 fence 时同时"接管"：fence 比表里小 → CheckpointConflict。"""
        if self.fence is None:
            rows = self._db.rows(self._sql["select"], {"run_id": run_id})
        else:
            rows = self._db.rows(self._sql["takeover"], {"run_id": run_id, "fence": self.fence, "writer": self.writer})
            if not rows:
                current = self._db.rows(self._sql["select"], {"run_id": run_id})
                if current:
                    raise self._stale_fence(run_id, current[0])
        if not rows:
            return None
        self._remember(run_id, rows[0]["version"])
        return RunState.from_dict(rows[0]["state"])

    def save(self, state: RunState) -> None:
        kind, params = self._save_params(state)
        rows = self._db.rows(self._sql[kind], params)
        if not rows:
            raise self._conflict(state.run_id, params["expected"], self._db.rows(self._sql["select"], {"run_id": state.run_id}))
        self._remember(state.run_id, rows[0]["version"], state.status)

    def get_run(self, run_id: str) -> dict | None:
        """只读地取一条记录（摘要 + 完整 state dict），不"接管"、不记版本号。给 API / 运维看。"""
        rows = self._db.rows(self._sql["get_run"], {"run_id": run_id})
        if not rows:
            return None
        rows[0]["state"] = self._db.rows(self._sql["select"], {"run_id": run_id})[0]["state"]
        return rows[0]

    def list_runs(self, status: str | None = None, tenant_id: str | None = None, limit: int = 50) -> list[dict]:
        """按更新时间倒序列出 run 摘要。审批收件箱：list_runs(status="paused", tenant_id=...)。"""
        return self._db.rows(*self._list_query(status, tenant_id, limit))


class AsyncPostgresCheckpointer(_CheckpointBase):
    """PostgresCheckpointer 的异步版（async save / load），给 agentkit.aio.AsyncAgent 用。语义完全一致。

    pool_or_dsn：连接串（自己建 AsyncConnectionPool）或现成的 AsyncConnectionPool（推荐和队列共用一个）。
    用完 await close()，或者 async with AsyncPostgresCheckpointer(...) as ckpt。
    """

    def __init__(
        self,
        pool_or_dsn,
        table: str = "agent_runs",
        *,
        fence: int | None = None,
        writer: str | None = None,
        pool_kwargs: dict | None = None,
        _db: _AsyncDb | None = None,
    ):
        super().__init__(table, fence, writer)
        self._db = _db or _AsyncDb(pool_or_dsn, pool_kwargs)

    async def __aenter__(self) -> "AsyncPostgresCheckpointer":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def setup(self) -> None:
        await _asetup(self._db, self.table, _checkpoint_ddl(self.table))

    def fenced(self, fence: int, writer: str | None = None) -> "AsyncPostgresCheckpointer":
        w = writer if writer is not None else self.writer
        return AsyncPostgresCheckpointer("", self.table, fence=int(fence), writer=w, _db=self._db)

    async def close(self) -> None:
        await self._db.close()

    async def load(self, run_id: str) -> RunState | None:
        if self.fence is None:
            rows = await self._db.rows(self._sql["select"], {"run_id": run_id})
        else:
            rows = await self._db.rows(
                self._sql["takeover"], {"run_id": run_id, "fence": self.fence, "writer": self.writer}
            )
            if not rows:
                current = await self._db.rows(self._sql["select"], {"run_id": run_id})
                if current:
                    raise self._stale_fence(run_id, current[0])
        if not rows:
            return None
        self._remember(run_id, rows[0]["version"])
        return RunState.from_dict(rows[0]["state"])

    async def save(self, state: RunState) -> None:
        kind, params = self._save_params(state)
        rows = await self._db.rows(self._sql[kind], params)
        if not rows:
            current = await self._db.rows(self._sql["select"], {"run_id": state.run_id})
            raise self._conflict(state.run_id, params["expected"], current)
        self._remember(state.run_id, rows[0]["version"], state.status)

    async def get_run(self, run_id: str) -> dict | None:
        rows = await self._db.rows(self._sql["get_run"], {"run_id": run_id})
        if not rows:
            return None
        rows[0]["state"] = (await self._db.rows(self._sql["select"], {"run_id": run_id}))[0]["state"]
        return rows[0]

    async def list_runs(self, status: str | None = None, tenant_id: str | None = None, limit: int = 50) -> list[dict]:
        return await self._db.rows(*self._list_query(status, tenant_id, limit))


# =====================================================================================
# 任务队列
# =====================================================================================


@dataclass
class Job:
    id: int
    kind: str
    tenant_id: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    fence: int
    priority: int = 0
    worker_id: str | None = None
    lease_until: datetime | None = None
    run_at: datetime | None = None
    idempotency_key: str | None = None
    result: Any = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    # 心跳发现租约丢失时置位；handler 可以据此提前停手（AgentJobHandler 会自动检查）
    lost: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @classmethod
    def _from_row(cls, row: dict) -> "Job":
        names = {f for f in cls.__dataclass_fields__ if f != "lost"}
        return cls(**{k: v for k, v in row.items() if k in names})


JOB_STATUSES = ("queued", "leased", "succeeded", "failed", "dead")

_JOB_COLS = sql.SQL(
    "id, kind, tenant_id, payload, status, attempts, max_attempts, fence, priority, worker_id, lease_until, "
    "run_at, idempotency_key, result, last_error, created_at, updated_at, finished_at"
)


def _fence_seq(table: str) -> str:
    return f"{table}_fence_seq"


def _queue_ddl(table: str) -> list:
    t, seq = sql.Identifier(table), _fence_seq(table)
    return [
        # fence 来自整张队列表共用的序列，全局单调递增。不能按任务各自从 1 数起：检查点的 fence 保护的是
        # 整个 run，而同一个 run 会先后对应多个任务（run → 审批后的 resume）。按任务计数时，
        # resume 任务第一次领取拿到 fence=1，会被检查点当成"比 fence=2 更旧的持有者"拒绝（第 31 课压测中发现）。
        sql.SQL("CREATE SEQUENCE IF NOT EXISTS {s}").format(s=sql.Identifier(seq)),
        sql.SQL(
            """CREATE TABLE IF NOT EXISTS {t} (
                id              bigserial PRIMARY KEY,
                kind            text NOT NULL,
                tenant_id       text NOT NULL,
                payload         jsonb NOT NULL,
                idempotency_key text,
                priority        int NOT NULL DEFAULT 0,
                status          text NOT NULL DEFAULT 'queued',
                attempts        int NOT NULL DEFAULT 0,
                max_attempts    int NOT NULL DEFAULT 5,
                fence           bigint NOT NULL DEFAULT 0,
                worker_id       text,
                lease_until     timestamptz,
                run_at          timestamptz NOT NULL DEFAULT now(),
                result          jsonb,
                last_error      text,
                created_at      timestamptz NOT NULL DEFAULT now(),
                updated_at      timestamptz NOT NULL DEFAULT now(),
                finished_at     timestamptz,
                UNIQUE (tenant_id, idempotency_key))"""  # NULL 互不相等：不带幂等键的任务不受约束
        ).format(t=t),
        # 部分索引：只索引"排队中"的行，顺序与 claim 的 ORDER BY 一致 → 积压再大也是一次索引扫描
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (priority DESC, id) WHERE status = 'queued'").format(
            i=sql.Identifier(f"{table}_ready_idx"), t=t
        ),
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (lease_until) WHERE status = 'leased'").format(
            i=sql.Identifier(f"{table}_lease_idx"), t=t
        ),
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} (status, finished_at)").format(
            i=sql.Identifier(f"{table}_status_idx"), t=t
        ),
        # 从旧版本（按任务计数）升级：序列至少要从表里已有的最大 fence 往后发，否则新 fence 可能比旧的小
        sql.SQL(
            "SELECT setval({seq}::regclass, m) FROM (SELECT max(fence) AS m FROM {t}) x "
            "WHERE m > (SELECT last_value FROM {s})"
        ).format(seq=sql.Literal(seq), t=t, s=sql.Identifier(seq)),
    ]


def _queue_sql(table: str) -> dict:
    t, c = sql.Identifier(table), _JOB_COLS
    return {
        "enqueue": sql.SQL(
            "INSERT INTO {t} (kind, tenant_id, payload, idempotency_key, priority, max_attempts, run_at) "
            "VALUES (%(kind)s, %(tenant_id)s, %(payload)s, %(key)s, %(priority)s, %(max_attempts)s, "
            "COALESCE(%(run_at)s::timestamptz, now()) + make_interval(secs => %(delay)s)) "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id"
        ).format(t=t),
        "find": sql.SQL("SELECT {c} FROM {t} WHERE tenant_id = %(tenant_id)s AND idempotency_key = %(key)s").format(c=c, t=t),
        "get": sql.SQL("SELECT {c} FROM {t} WHERE id = %(id)s").format(c=c, t=t),
        # 回收：租约过期的任务 → 还有次数就退避后重新排队，次数用尽就进死信（毒消息兜底：计数发生在领取时）
        "reap": sql.SQL(
            """UPDATE {t} SET
                   status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                   last_error = 'lease expired: worker ' || COALESCE(worker_id, '?')
                                || ' 没有按时续约（崩溃 / 卡死 / 网络分区？）',
                   lease_until = NULL,
                   run_at = now() + make_interval(secs => random() * LEAST(%(cap)s, %(base)s * power(2, GREATEST(attempts - 1, 0)))),
                   finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                   updated_at = now()
               WHERE id IN (SELECT id FROM {t}
                            WHERE status = 'leased' AND lease_until < now()
                            ORDER BY lease_until LIMIT 100
                            FOR UPDATE SKIP LOCKED)
               RETURNING id, status"""
        ).format(t=t),
        # 领取：子查询挑一个"已到时间"的任务，FOR UPDATE 锁住它，SKIP LOCKED 让并发的 worker 跳过它去拿下一个。
        # 排序用 id（入队顺序）而不是 run_at：被回收 / 重试的任务退避结束后回到原来的位置，而不是排到队尾
        # （它的用户已经等了一轮了；实测按 run_at 排序时，kill -9 之后要等整个积压消化完才有人接手）。
        "claim": sql.SQL(
            """UPDATE {t} SET status = 'leased', worker_id = %(worker)s,
                   lease_until = now() + make_interval(secs => %(lease)s),
                   attempts = attempts + 1, fence = nextval({seq}::regclass), updated_at = now()
               WHERE id = (SELECT id FROM {t}
                           WHERE status = 'queued' AND run_at <= now()
                             AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[]))
                           ORDER BY priority DESC, id
                           LIMIT 1
                           FOR UPDATE SKIP LOCKED)
               RETURNING {c}"""
        ).format(t=t, c=c, seq=sql.Literal(_fence_seq(table))),
        "heartbeat": sql.SQL(
            "UPDATE {t} SET lease_until = now() + make_interval(secs => %(lease)s), updated_at = now() "
            "WHERE id = %(id)s AND fence = %(fence)s AND status = 'leased' RETURNING lease_until"
        ).format(t=t),
        "complete": sql.SQL(
            "UPDATE {t} SET status = 'succeeded', result = %(result)s, lease_until = NULL, "
            "finished_at = now(), updated_at = now() "
            "WHERE id = %(id)s AND fence = %(fence)s AND status = 'leased' RETURNING id"
        ).format(t=t),
        "fail": sql.SQL(
            """UPDATE {t} SET
                   status = CASE WHEN NOT %(retryable)s THEN 'failed'
                                 WHEN attempts >= max_attempts THEN 'dead'
                                 ELSE 'queued' END,
                   last_error = %(error)s, lease_until = NULL,
                   run_at = CASE WHEN %(retryable)s AND attempts < max_attempts
                                 THEN now() + make_interval(secs => %(delay)s) ELSE run_at END,
                   finished_at = CASE WHEN NOT %(retryable)s OR attempts >= max_attempts THEN now() END,
                   updated_at = now()
               WHERE id = %(id)s AND fence = %(fence)s AND status = 'leased'
               RETURNING status"""
        ).format(t=t),
        "release": sql.SQL(
            "UPDATE {t} SET status = 'queued', lease_until = NULL, "
            "run_at = now() + make_interval(secs => %(delay)s), "
            "attempts = CASE WHEN %(count)s THEN attempts ELSE GREATEST(attempts - 1, 0) END, "
            "last_error = COALESCE(%(reason)s, last_error), updated_at = now() "
            "WHERE id = %(id)s AND fence = %(fence)s AND status = 'leased' RETURNING id"
        ).format(t=t),
        # 注意：redrive 不重置 fence —— fence 必须单调递增，否则旧持有者的 fence 可能"复活"
        "redrive": sql.SQL(
            "UPDATE {t} SET status = 'queued', attempts = 0, run_at = now(), lease_until = NULL, "
            "finished_at = NULL, last_error = 'redriven: ' || COALESCE(last_error, ''), updated_at = now() "
            "WHERE id = %(id)s AND status IN ('dead', 'failed') RETURNING id"
        ).format(t=t),
        "stats": sql.SQL(
            """SELECT status, count(*) AS n FROM {t} GROUP BY status
               UNION ALL SELECT '_ready', count(*) FROM {t} WHERE status = 'queued' AND run_at <= now()
               UNION ALL SELECT '_expired', count(*) FROM {t} WHERE status = 'leased' AND lease_until < now()"""
        ).format(t=t),
        "oldest": sql.SQL(
            "SELECT EXTRACT(EPOCH FROM now() - min(run_at))::float8 AS age FROM {t} "
            "WHERE status = 'queued' AND run_at <= now()"
        ).format(t=t),
        "purge": sql.SQL(
            "DELETE FROM {t} WHERE status IN ('succeeded', 'failed') "
            "AND finished_at < now() - make_interval(secs => %(s)s) RETURNING id"
        ).format(t=t),
    }


class _QueueBase:
    """同步版和异步版共用的部分：SQL、参数构造、统计结果整理、拒绝原因的解释。"""

    def __init__(self, table: str, max_attempts: int, base_backoff: float, max_backoff: float):
        self.table = _ident(table, "table")
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._sql = _queue_sql(self.table)

    def _enqueue_params(self, kind, payload, tenant_id, key, priority, run_at, delay_seconds, max_attempts) -> dict:
        return {
            "kind": kind,
            "tenant_id": tenant_id,
            "payload": _jsonb(payload),
            "key": key,
            "priority": priority,
            "max_attempts": max_attempts or self.max_attempts,
            "run_at": run_at,
            "delay": float(delay_seconds),
        }

    @staticmethod
    def _claim_params(worker_id: str, lease_seconds: float, kinds) -> dict:
        return {"worker": worker_id, "lease": float(lease_seconds), "kinds": list(kinds) if kinds is not None else None}

    def _fail_params(self, job: Job, error: str, retryable: bool) -> dict:
        upper = min(self.max_backoff, self.base_backoff * 2 ** max(job.attempts - 1, 0))
        delay = random.uniform(0, upper)  # 全抖动：避免一批同时失败的任务在同一时刻卷土重来
        return {"id": job.id, "fence": job.fence, "error": str(error)[:4000], "retryable": bool(retryable), "delay": delay}

    @staticmethod
    def _stats(rows: list[dict], age: float | None) -> dict:
        counts = {s: 0 for s in JOB_STATUSES}
        extra = {}
        for r in rows:
            (extra if r["status"].startswith("_") else counts)[r["status"]] = r["n"]
        return {"counts": counts, "ready": extra.get("_ready", 0), "oldest_queued_age_s": age,
                "expired_leases": extra.get("_expired", 0)}

    @staticmethod
    def _explain(job: Job, cur: Job | None, action: str) -> str:
        if cur is None:
            return f"任务 #{job.id} 不存在，{action}被拒绝"
        if cur.fence != job.fence:
            return (f"任务 #{job.id} 已被重新领取：当前 fence={cur.fence}（持有者 {cur.worker_id}），"
                    f"你的 fence={job.fence} 已过期，{action}被拒绝")
        return f"任务 #{job.id} 当前状态为 {cur.status}（不再是 leased：已被回收或已结束），{action}被拒绝"


class PostgresJobQueue(_QueueBase):
    """基于 `FOR UPDATE SKIP LOCKED` 的任务队列：至少一次投递 + 租约 + fencing token + 退避重试 + 死信。

    状态机（与第 13 课相同）：
        queued ──claim（attempts+1, fence↑）───► leased ──complete──► succeeded   （fence↑：取全局序列的下一个值）
          ▲  ▲                                     │ ├──fail(retryable=False)──► failed
          │  └──── fail(可重试，退避) / release ───┘ └──fail 且次数用尽──► dead ──redrive──► queued
          └──────── 租约过期被回收（reap）──────────┘（次数用尽则直接 dead：毒消息）

    与第 13 课 SQLite 版的区别：
      - 领取用 SKIP LOCKED：多个 worker 并发领取时互相跳过被锁的行，而不是排队等锁；
      - 所有时间都用数据库的 now()：租约不受各机器时钟漂移影响；
      - 过期租约由 reap_expired() 先"回收"成 queued，claim 只扫 status='queued' 的部分索引，积压很大时依然是索引扫描。
        回收之后，旧持有者的 complete / heartbeat 会因为 status 不再是 leased 而被拒绝（它的所有权在回收那一刻结束）。
    """

    def __init__(
        self,
        conninfo,
        table: str = "agent_jobs",
        *,
        max_attempts: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 300.0,
        connect_kwargs: dict | None = None,
    ):
        super().__init__(table, max_attempts, base_backoff, max_backoff)
        self._db = _Db(conninfo, connect_kwargs)

    def setup(self) -> None:
        """建表 + 索引（幂等，可并发调用）。"""
        _setup(self._db, self.table, _queue_ddl(self.table))

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------ 生产者

    def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        tenant_id: str,
        idempotency_key: str | None = None,
        priority: int = 0,
        run_at: datetime | None = None,
        delay_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> int:
        """入队，返回 job_id。同一租户内相同的 idempotency_key 只入队一次（重复提交返回第一次的 job_id）。"""
        params = self._enqueue_params(kind, payload, tenant_id, idempotency_key, priority, run_at, delay_seconds, max_attempts)
        rows = self._db.rows(self._sql["enqueue"], params)
        return rows[0]["id"] if rows else self.find(tenant_id, idempotency_key).id

    def get(self, job_id: int) -> Job | None:
        rows = self._db.rows(self._sql["get"], {"id": job_id})
        return Job._from_row(rows[0]) if rows else None

    def find(self, tenant_id: str, idempotency_key: str) -> Job | None:
        rows = self._db.rows(self._sql["find"], {"tenant_id": tenant_id, "key": idempotency_key})
        return Job._from_row(rows[0]) if rows else None

    # ------------------------------------------------------------------ 消费者

    def reap_expired(self) -> list[tuple[int, str]]:
        """回收租约过期的任务，返回 [(job_id, 新状态)]。claim() 每次都会先调用它。"""
        rows = self._db.rows(self._sql["reap"], {"cap": self.max_backoff, "base": self.base_backoff})
        return [(r["id"], r["status"]) for r in rows]

    def claim(self, worker_id: str, lease_seconds: float = 30, kinds: Iterable[str] | None = None) -> Job | None:
        """原子领取一个任务：写租约，attempts+1，fence 取全局序列的下一个值。没有可领取的任务返回 None。"""
        self.reap_expired()
        rows = self._db.rows(self._sql["claim"], self._claim_params(worker_id, lease_seconds, kinds))
        return Job._from_row(rows[0]) if rows else None

    def heartbeat(self, job: Job, lease_seconds: float = 30) -> datetime:
        """续租：把租约延长到 now() + lease_seconds。fence 不匹配（已被回收 / 接手）→ LeaseLost。"""
        rows = self._db.rows(self._sql["heartbeat"], {"id": job.id, "fence": job.fence, "lease": float(lease_seconds)})
        if not rows:
            raise LeaseLost(self._explain(job, self.get(job.id), "续租"))
        return rows[0]["lease_until"]

    def complete(self, job: Job, result: Any = None) -> None:
        """提交成功结果。只有 status='leased' 且 fence 匹配才生效，否则 LeaseLost（僵尸 worker 的结果作废）。"""
        rows = self._db.rows(self._sql["complete"], {"id": job.id, "fence": job.fence, "result": _jsonb(result)})
        if not rows:
            raise LeaseLost(self._explain(job, self.get(job.id), "提交"))

    def fail(self, job: Job, error: str, retryable: bool = True) -> str:
        """报告失败，返回新状态：queued（退避后重试）/ dead（次数用尽）/ failed（不可重试）。"""
        rows = self._db.rows(self._sql["fail"], self._fail_params(job, error, retryable))
        if not rows:
            raise LeaseLost(self._explain(job, self.get(job.id), "报告失败"))
        return rows[0]["status"]

    def release(self, job: Job, *, delay_seconds: float = 0.0, reason: str | None = None, count_attempt: bool = False) -> None:
        """主动归还任务（优雅停机、被限流时推迟）。默认不消耗重试次数：这不是任务本身的错。"""
        params = {"id": job.id, "fence": job.fence, "delay": float(delay_seconds), "reason": reason, "count": count_attempt}
        if not self._db.rows(self._sql["release"], params):
            raise LeaseLost(self._explain(job, self.get(job.id), "归还"))

    # ------------------------------------------------------------------ 运维

    def redrive(self, job_id: int) -> bool:
        """把 dead / failed 的任务重新投递（attempts 清零，fence 保持递增）。返回是否成功。"""
        return bool(self._db.rows(self._sql["redrive"], {"id": job_id}))

    def stats(self) -> dict:
        """各状态计数、可执行的排队数、最老可执行任务已等待的秒数、已过期未回收的租约数。
        "最老任务等了多久"比"队列里有多少个"更能说明用户体验，适合做告警（第 28 课）。"""
        return self._stats(self._db.rows(self._sql["stats"]), self._db.rows(self._sql["oldest"])[0]["age"])

    def purge_finished(self, older_than_seconds: float) -> int:
        """删除早已结束（succeeded / failed）的任务。队列表只保留"近期"数据，否则表和索引会一直膨胀。
        dead 不删：它们要等人排查。幂等键随行一起删除 —— 去重窗口 = 保留时长。"""
        return len(self._db.rows(self._sql["purge"], {"s": float(older_than_seconds)}))


class AsyncPostgresJobQueue(_QueueBase):
    """PostgresJobQueue 的异步版：方法相同，全部是 async。和 AsyncPostgresCheckpointer 共用一个连接池最省连接。"""

    def __init__(
        self,
        pool_or_dsn,
        table: str = "agent_jobs",
        *,
        max_attempts: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 300.0,
        pool_kwargs: dict | None = None,
    ):
        super().__init__(table, max_attempts, base_backoff, max_backoff)
        self._db = _AsyncDb(pool_or_dsn, pool_kwargs)

    async def __aenter__(self) -> "AsyncPostgresJobQueue":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def setup(self) -> None:
        await _asetup(self._db, self.table, _queue_ddl(self.table))

    async def close(self) -> None:
        await self._db.close()

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        tenant_id: str,
        idempotency_key: str | None = None,
        priority: int = 0,
        run_at: datetime | None = None,
        delay_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> int:
        params = self._enqueue_params(kind, payload, tenant_id, idempotency_key, priority, run_at, delay_seconds, max_attempts)
        rows = await self._db.rows(self._sql["enqueue"], params)
        return rows[0]["id"] if rows else (await self.find(tenant_id, idempotency_key)).id

    async def get(self, job_id: int) -> Job | None:
        rows = await self._db.rows(self._sql["get"], {"id": job_id})
        return Job._from_row(rows[0]) if rows else None

    async def find(self, tenant_id: str, idempotency_key: str) -> Job | None:
        rows = await self._db.rows(self._sql["find"], {"tenant_id": tenant_id, "key": idempotency_key})
        return Job._from_row(rows[0]) if rows else None

    async def reap_expired(self) -> list[tuple[int, str]]:
        rows = await self._db.rows(self._sql["reap"], {"cap": self.max_backoff, "base": self.base_backoff})
        return [(r["id"], r["status"]) for r in rows]

    async def claim(self, worker_id: str, lease_seconds: float = 30, kinds: Iterable[str] | None = None) -> Job | None:
        await self.reap_expired()
        rows = await self._db.rows(self._sql["claim"], self._claim_params(worker_id, lease_seconds, kinds))
        return Job._from_row(rows[0]) if rows else None

    async def heartbeat(self, job: Job, lease_seconds: float = 30) -> datetime:
        rows = await self._db.rows(self._sql["heartbeat"], {"id": job.id, "fence": job.fence, "lease": float(lease_seconds)})
        if not rows:
            raise LeaseLost(self._explain(job, await self.get(job.id), "续租"))
        return rows[0]["lease_until"]

    async def complete(self, job: Job, result: Any = None) -> None:
        rows = await self._db.rows(self._sql["complete"], {"id": job.id, "fence": job.fence, "result": _jsonb(result)})
        if not rows:
            raise LeaseLost(self._explain(job, await self.get(job.id), "提交"))

    async def fail(self, job: Job, error: str, retryable: bool = True) -> str:
        rows = await self._db.rows(self._sql["fail"], self._fail_params(job, error, retryable))
        if not rows:
            raise LeaseLost(self._explain(job, await self.get(job.id), "报告失败"))
        return rows[0]["status"]

    async def release(self, job: Job, *, delay_seconds: float = 0.0, reason: str | None = None,
                      count_attempt: bool = False) -> None:
        params = {"id": job.id, "fence": job.fence, "delay": float(delay_seconds), "reason": reason, "count": count_attempt}
        if not await self._db.rows(self._sql["release"], params):
            raise LeaseLost(self._explain(job, await self.get(job.id), "归还"))

    async def redrive(self, job_id: int) -> bool:
        return bool(await self._db.rows(self._sql["redrive"], {"id": job_id}))

    async def stats(self) -> dict:
        rows = await self._db.rows(self._sql["stats"])
        return self._stats(rows, (await self._db.rows(self._sql["oldest"]))[0]["age"])

    async def purge_finished(self, older_than_seconds: float) -> int:
        return len(await self._db.rows(self._sql["purge"], {"s": float(older_than_seconds)}))


# =====================================================================================
# worker 循环
# =====================================================================================


class _StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def _new_stats() -> dict:
    return {"claimed": 0, "succeeded": 0, "failed": 0, "deferred": 0, "fence_rejected": 0, "ownership_lost": 0,
            "commit_errors": 0}


def _emitter(worker_id: str, on_event):
    def emit(name: str, **info) -> None:
        if on_event is not None:
            try:
                on_event(name, {"worker_id": worker_id, **info})
            except Exception:  # noqa: BLE001 —— 日志回调出错不能拖垮 worker
                pass

    return emit


class _Heartbeat(threading.Thread):
    """worker 的续租线程（每个 worker 一个，整个生命周期复用同一个数据库连接）。

    进程被 kill -9 时它随之消失，租约自然过期；进程被冻结（GC 停顿、SIGSTOP）时它也一起被冻结 ——
    这正是"心跳停了 = 持有者可能已经死了"的依据。
    续租时持有锁：untrack() 会等正在进行的那次续租结束，保证"提交之后"不会再冒出一次续租。
    """

    def __init__(self, queue: PostgresJobQueue, lease_seconds: float, interval: float, emit):
        super().__init__(daemon=True, name="agentkit-heartbeat")
        self.queue, self.lease, self.interval, self.emit = queue, lease_seconds, interval, emit
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._job: Job | None = None

    def track(self, job: Job) -> None:
        with self._lock:
            self._job = job

    def untrack(self) -> None:
        with self._lock:
            self._job = None

    def run(self) -> None:
        try:
            while not self._halt.wait(self.interval):
                with self._lock:
                    job = self._job
                    if job is None or job.lost.is_set():
                        continue
                    try:
                        self.queue.heartbeat(job, self.lease)
                    except LeaseLost as e:
                        job.lost.set()
                        self.emit("heartbeat_rejected", job=job, error=str(e))
                    except Exception as e:  # noqa: BLE001 —— 数据库暂时不可用：下个周期再试，租约还没到期
                        self.emit("heartbeat_error", job=job, error=f"{type(e).__name__}: {e}")
        finally:
            self.queue._db.release_thread()

    def stop(self) -> None:
        self._halt.set()


def run_worker(
    queue: PostgresJobQueue,
    handler: Callable[[Job], Any],
    *,
    worker_id: str,
    stop_event: _StopEvent,
    lease_seconds: float = 30,
    poll_interval: float = 0.5,
    heartbeat_interval: float | None = None,
    kinds: Iterable[str] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    max_jobs: int | None = None,
) -> dict:
    """同步 worker 主循环（一次一个任务）：领取 → 后台续租 → handler(job) → 带 fence 提交。返回计数统计。

    - handler 正常返回 → complete(job, 返回值)；抛 RetryLater → release（不计次数）；
      抛 PermanentJobError → fail(retryable=False)；抛其他异常 → fail(retryable=True)（退避重试 / 死信）；
      抛 CheckpointConflict / LeaseLost → 所有权已经转移，什么都不提交。
    - 心跳间隔默认是租约的 1/3：连续丢两次心跳也不会失去租约。
    - **优雅停机**：stop_event 被置位后不再领取新任务，但手头的任务会做完并提交，然后返回。
      Kubernetes 删除 Pod 时先发 SIGTERM，等 terminationGracePeriodSeconds（默认 30 秒）后再 SIGKILL；
      用 stop_on_signals(stop_event) 把 SIGTERM 接到 stop_event 上即可。做不完的任务被 SIGKILL 后，
      租约过期、由别的 worker 从检查点接着跑 —— 所以宽限期不必覆盖最长的任务，只要覆盖大多数。
    - on_event(name, info) 用于日志 / 指标（第 28 课可以把它接到 Prometheus）。
    """
    hb_interval = heartbeat_interval or max(lease_seconds / 3, 0.05)
    stats = _new_stats()
    emit = _emitter(worker_id, on_event)
    hb = _Heartbeat(queue, lease_seconds, hb_interval, emit)
    hb.start()
    emit("started")
    try:
        _sync_loop(queue, handler, worker_id, stop_event, lease_seconds, poll_interval, kinds, max_jobs, stats, emit, hb)
    finally:
        hb.stop()
        hb.join(timeout=5)
    emit("stopped", stats=dict(stats))
    return stats


def _sync_loop(queue, handler, worker_id, stop_event, lease_seconds, poll_interval, kinds, max_jobs, stats, emit, hb):
    idle_errors = 0
    while not stop_event.is_set():
        if max_jobs is not None and stats["claimed"] >= max_jobs:
            break
        try:
            job = queue.claim(worker_id, lease_seconds, kinds)
            idle_errors = 0
        except psycopg.OperationalError as e:  # 数据库暂时不可用：退避后重试，而不是让 worker 进程崩溃
            idle_errors += 1
            emit("claim_error", error=str(e))
            stop_event.wait(min(30.0, poll_interval * 2 ** min(idle_errors, 6)))
            continue
        if job is None:
            stop_event.wait(poll_interval * random.uniform(0.5, 1.5))  # 带抖动的轮询，别让所有 worker 同一时刻一起查
            continue
        stats["claimed"] += 1
        emit("claimed", job=job)
        hb.track(job)
        try:
            try:
                result = handler(job)
            except RetryLater as e:
                hb.untrack()
                queue.release(job, delay_seconds=e.delay_seconds, reason=f"deferred: {e.reason}")
                stats["deferred"] += 1
                emit("deferred", job=job, delay_seconds=e.delay_seconds, reason=e.reason)
            except (CheckpointConflict, LeaseLost) as e:
                stats["ownership_lost"] += 1
                emit("ownership_lost", job=job, error=str(e))
            except PermanentJobError as e:
                hb.untrack()
                queue.fail(job, f"PermanentJobError: {e}", retryable=False)
                stats["failed"] += 1
                emit("failed", job=job, error=str(e), status="failed")
            except Exception as e:  # noqa: BLE001
                hb.untrack()
                status = queue.fail(job, f"{type(e).__name__}: {e}", retryable=True)
                stats["failed"] += 1
                emit("failed", job=job, error=f"{type(e).__name__}: {e}", status=status)
            else:
                hb.untrack()
                queue.complete(job, result)  # 存储端是最终裁判：心跳说丢了也照样提交一次，让 fence 来判
                stats["succeeded"] += 1
                emit("completed", job=job, result=result)
        except LeaseLost as e:
            stats["fence_rejected"] += 1
            emit("fence_rejected", job=job, error=str(e))
        except psycopg.Error as e:  # 提交时数据库出错：结果没记上，租约过期后任务会被重做（所以副作用必须幂等）
            stats["commit_errors"] += 1
            emit("commit_error", job=job, error=f"{type(e).__name__}: {e}")
        finally:
            hb.untrack()


def _is_async_handler(handler) -> bool:
    return bool(
        getattr(handler, "is_async", False)
        or inspect.iscoroutinefunction(handler)
        or inspect.iscoroutinefunction(getattr(handler, "__call__", None))
    )


async def _wait_or_timeout(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await wait_for(stop_event.wait(), timeout)  # 取消安全版：3.12 之前的 asyncio.wait_for 可能吞掉停机时的取消
    except asyncio.TimeoutError:
        pass


async def run_async_worker(
    queue: AsyncPostgresJobQueue,
    handler: Callable[[Job], Any],
    *,
    worker_id: str,
    stop_event: asyncio.Event,
    concurrency: int = 16,
    lease_seconds: float = 30,
    poll_interval: float = 0.5,
    heartbeat_interval: float | None = None,
    grace_period: float = 25.0,
    kinds: Iterable[str] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    max_jobs: int | None = None,
) -> dict:
    """异步 worker：**一个进程**用 asyncio 同时处理最多 concurrency 个任务。返回计数统计。

    - **背压**：领取前先拿 asyncio.Semaphore 的一个名额；满载时停在这里，不再 claim ——
      任务留在队列里，别的 worker 还能领走（而不是被这个进程"囤"在内存里）。
    - 每个任务一个异步续租协程；handler 可以是 async 函数（直接 await），也可以是同步函数
      （放进线程池 asyncio.to_thread 执行 —— 否则一个阻塞调用会卡住整个事件循环，所有任务的心跳一起停）。
    - **停机**：stop_event 置位后不再领取；等在途任务最多 grace_period 秒，超时的任务被取消，
      **不提交、不归还**，它们的租约自然过期后由别的 worker 从检查点接手（fence 保证取消前的写入不会覆盖接手者）。
      grace_period 要小于 K8s 的 terminationGracePeriodSeconds（默认 30 秒），给取消和清理留出时间。
    - 结果统计多了 cancelled（停机时被取消的任务数）和 max_in_flight（同时在跑的任务数峰值）。
    """
    if concurrency < 1:
        raise ValueError("concurrency 至少为 1")
    hb_interval = heartbeat_interval or max(lease_seconds / 3, 0.05)
    stats = {**_new_stats(), "cancelled": 0, "max_in_flight": 0}
    emit = _emitter(worker_id, on_event)
    sem = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task] = set()
    is_async = _is_async_handler(handler)
    try:
        from psycopg_pool import PoolTimeout
        transient_errors: tuple = (psycopg.OperationalError, PoolTimeout)
    except ImportError:  # 传入的是自己实现的连接池
        transient_errors = (psycopg.OperationalError,)

    async def heartbeat(job: Job) -> None:
        while True:
            await asyncio.sleep(hb_interval)
            try:
                await queue.heartbeat(job, lease_seconds)
            except LeaseLost as e:
                job.lost.set()
                emit("heartbeat_rejected", job=job, error=str(e))
                return
            except Exception as e:  # noqa: BLE001
                emit("heartbeat_error", job=job, error=f"{type(e).__name__}: {e}")

    async def process(job: Job) -> None:
        hb = asyncio.create_task(heartbeat(job))
        try:
            try:
                result = await handler(job) if is_async else await asyncio.to_thread(handler, job)
            except RetryLater as e:
                await queue.release(job, delay_seconds=e.delay_seconds, reason=f"deferred: {e.reason}")
                stats["deferred"] += 1
                emit("deferred", job=job, delay_seconds=e.delay_seconds, reason=e.reason)
            except (CheckpointConflict, LeaseLost) as e:
                stats["ownership_lost"] += 1
                emit("ownership_lost", job=job, error=str(e))
            except PermanentJobError as e:
                await queue.fail(job, f"PermanentJobError: {e}", retryable=False)
                stats["failed"] += 1
                emit("failed", job=job, error=str(e), status="failed")
            except Exception as e:  # noqa: BLE001  （CancelledError 不是 Exception，会直接穿过去）
                status = await queue.fail(job, f"{type(e).__name__}: {e}", retryable=True)
                stats["failed"] += 1
                emit("failed", job=job, error=f"{type(e).__name__}: {e}", status=status)
            else:
                await queue.complete(job, result)
                stats["succeeded"] += 1
                emit("completed", job=job, result=result)
        except LeaseLost as e:
            stats["fence_rejected"] += 1
            emit("fence_rejected", job=job, error=str(e))
        except psycopg.Error as e:
            stats["commit_errors"] += 1
            emit("commit_error", job=job, error=f"{type(e).__name__}: {e}")
        finally:
            hb.cancel()
            sem.release()

    emit("started", concurrency=concurrency)
    idle_errors = 0
    while not stop_event.is_set():
        if max_jobs is not None and stats["claimed"] >= max_jobs:
            break
        await sem.acquire()  # 背压：满载时停在这里
        if stop_event.is_set():
            sem.release()
            break
        try:
            job = await queue.claim(worker_id, lease_seconds, kinds)
            idle_errors = 0
        except transient_errors as e:  # 数据库暂时不可用 / 连接池借不到连接：退避后重试
            sem.release()
            idle_errors += 1
            emit("claim_error", error=f"{type(e).__name__}: {e}")
            await _wait_or_timeout(stop_event, min(30.0, poll_interval * 2 ** min(idle_errors, 6)))
            continue
        if job is None:
            sem.release()
            await _wait_or_timeout(stop_event, poll_interval * random.uniform(0.5, 1.5))
            continue
        stats["claimed"] += 1
        emit("claimed", job=job)
        task = asyncio.create_task(process(job), name=f"job-{job.id}")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        stats["max_in_flight"] = max(stats["max_in_flight"], len(tasks))

    if tasks:
        emit("draining", in_flight=len(tasks), grace_period=grace_period)
        _, pending = await asyncio.wait(set(tasks), timeout=grace_period)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        stats["cancelled"] = len(pending)
        if pending:
            emit("cancelled", count=len(pending))
    emit("stopped", stats=dict(stats))
    return stats


def stop_on_signals(stop_event, signals: Iterable[int] = (signal.SIGTERM, signal.SIGINT)) -> None:
    """把 SIGTERM / SIGINT 接到 stop_event 上。K8s 删除 Pod 时发送的就是 SIGTERM。

    threading / multiprocessing 的 Event：只能在主线程调用；asyncio.Event：要在事件循环里调用
    （用 loop.add_signal_handler，信号到来时在事件循环里 set，而不是在任意字节码之间打断协程）。
    """
    if isinstance(stop_event, asyncio.Event):
        loop = asyncio.get_running_loop()
        for s in signals:
            loop.add_signal_handler(s, stop_event.set)
        return
    for s in signals:
        signal.signal(s, lambda *_: stop_event.set())


# =====================================================================================
# Agent 任务
# =====================================================================================


_CURRENT_JOB: ContextVar[Job | None] = ContextVar("agentkit_contrib_current_job", default=None)


class _LeaseGuard(Hook):
    """心跳发现租约丢了 → 在下一次模型 / 工具调用前停手，少做无用功（最终的安全仍然靠 fence 和 CAS）。

    一个 Agent 只装一个：当前是哪个任务从 ContextVar 里取。共享的 AsyncAgent 同时跑几十个任务时，
    每个任务是一个独立的 asyncio Task，各自看到自己的 job。
    """

    def _check(self) -> None:
        job = _CURRENT_JOB.get()
        if job is not None and job.lost.is_set():
            raise StopRun("lease_lost", "租约已丢失：任务已被别的 worker 接手，停止执行。")

    def before_llm(self, state, messages) -> None:
        self._check()

    def before_tool(self, state, call, tool) -> str | None:
        self._check()
        return None


def _supports_run_checkpointer(agent) -> bool:
    """agent.run / resume / approve 是否接受单次运行的 checkpointer=（agentkit.aio.AsyncAgent 支持）。"""
    try:
        return all("checkpointer" in inspect.signature(getattr(agent, m)).parameters for m in ("run", "resume", "approve"))
    except (TypeError, ValueError, AttributeError):
        return False


def _looks_like_agent(obj) -> bool:
    return hasattr(obj, "run") and hasattr(obj, "resume") and hasattr(obj, "hooks")


class AgentJobHandler:
    """把 Agent 的运行 / 恢复包装成队列任务。

        同步：run_worker(queue, AgentJobHandler(make_agent, PostgresCheckpointer(dsn)), ...)
        异步：await run_async_worker(aqueue, AgentJobHandler(async_agent, AsyncPostgresCheckpointer(pool)), ...)
        （checkpointer 是 AsyncPostgresCheckpointer 时，handler(job) 返回协程，is_async=True）

    第一个参数三种写法：
      - **一个 AsyncAgent 实例（异步推荐）**：所有任务共用这一个 Agent（也就共用它的工具执行器 / 线程池、
        模型客户端），每个任务通过 run / resume / approve 的 checkpointer= 参数传入带本次 fence 的视图；
      - make_agent(checkpointer)：同步版每个任务调用一次（同步 Agent 的检查点绑在实例上）；
        异步版只调用一次（传入基础 checkpointer），之后同上共用；
      - make_agent(checkpointer, job)：每个任务调用一次，用来按任务定制 Agent。这时 Agent 要么支持
        checkpointer=（AsyncAgent），要么**必须**使用传入的 checkpointer；想共用线程池，就在工厂里给
        每个 AsyncAgent 传同一个 executor=AsyncToolExecutor(registry)。

    payload 格式：
        {"op": "run", "input": "...", "metadata": {...}, "run_id": "可选"}
        {"op": "resume", "run_id": "...", "approvals": {"call_id": true}, "by": "审批人", "comment": "可选"}

    - run_id 默认 f"job-{job.id}"：由任务决定而不是随机生成，接手的 worker 才能找到同一个检查点、算出同样的幂等键。
    - 已有检查点（前任崩溃 / 被取消 / 被接管 / 被限流推迟）→ resume，从断点继续，不从头再来。
    - metadata 里的 tenant_id 一律以 job.tenant_id 为准：payload 是调用方填的，不可信（第 09 课）。
    - 结果为 paused（等待审批）时任务**正常完成**，返回值里 awaiting_approval=True；审批后由 API 入队 resume 任务。
    - stop_reason 在 defer_stop_reasons 里（默认 rate_limited）→ 抛 RetryLater，过一会儿再来，不消耗重试次数；
      status == failed（模型彻底不可用）→ 抛异常，由队列退避重试。
    """

    def __init__(
        self,
        make_agent: Callable[..., Any] | Any,
        checkpointer: PostgresCheckpointer | AsyncPostgresCheckpointer,
        *,
        defer_stop_reasons: Iterable[str] = ("rate_limited",),
        defer_seconds: float = 2.0,
    ):
        self.checkpointer = checkpointer
        self.defer_stop_reasons = set(defer_stop_reasons)
        self.defer_seconds = defer_seconds
        self.is_async = isinstance(checkpointer, AsyncPostgresCheckpointer)
        self.agents_created = 0  # 工厂被调用的次数（共用模式下应该是 0 或 1）
        self._shared = None
        self._per_job = False
        self._pass_job = False
        if _looks_like_agent(make_agent):
            if not _supports_run_checkpointer(make_agent):
                raise TypeError("直接传 Agent 实例时，它的 run/resume/approve 必须支持 checkpointer= 参数"
                                "（agentkit.aio.AsyncAgent 支持）；同步 Agent 请传工厂 make_agent(checkpointer)")
            self._shared = make_agent
            self.make_agent = None
        else:
            self.make_agent = make_agent
            try:
                params = [q for q in inspect.signature(make_agent).parameters.values()
                          if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD)]
                self._pass_job = len(params) >= 2
            except (TypeError, ValueError):  # pragma: no cover
                self._pass_job = False
            self._per_job = self._pass_job or not self.is_async

    def __call__(self, job: Job):
        return self._call_async(job) if self.is_async else self._call_sync(job)

    # ------------------------------------------------------------------ 公共小步骤

    def _build(self, ckpt, job: Job):
        self.agents_created += 1
        return self.make_agent(ckpt, job) if self._pass_job else self.make_agent(ckpt)

    def _adopt(self, agent) -> None:
        """一个 Agent 实例只装一个租约守卫。"""
        if not any(isinstance(h, _LeaseGuard) for h in agent.hooks):
            agent.hooks.append(_LeaseGuard())

    def _call_kwargs(self, agent, ckpt) -> dict:
        if _supports_run_checkpointer(agent):
            return {"checkpointer": ckpt}  # 单次运行的检查点：带本次领取 fence 的视图
        if getattr(agent, "checkpointer", None) is not ckpt:
            raise PermanentJobError("make_agent 必须把传入的 checkpointer 交给 Agent（否则 fence 保护形同虚设）")
        return {}

    @staticmethod
    def _op(job: Job) -> str:
        op = (job.payload or {}).get("op", "run")
        if op not in ("run", "resume"):
            raise PermanentJobError(f"未知的 op：{op!r}（只支持 run / resume）")
        return op

    @staticmethod
    def _check_tenant(state: RunState, job: Job) -> None:
        owner = state.metadata.get("tenant_id")
        if owner is not None and owner != job.tenant_id:
            raise PermanentJobError(f"run {state.run_id} 属于租户 {owner}，不能由租户 {job.tenant_id} 的任务操作")

    @staticmethod
    def _approval_step(state: RunState, payload: dict) -> tuple[dict, dict | None]:
        """返回 (approvals, 需要 approve() 的那次决定)。重试的 resume 任务不会重复写审批记录。"""
        approvals = {str(k): bool(v) for k, v in (payload.get("approvals") or {}).items()}
        pending = state.pending
        logged = pending is not None and any(a.get("call_id") == pending["id"] for a in state.approval_log)
        if pending and pending["id"] in approvals and not logged:
            return approvals, {"approved": approvals[pending["id"]], "by": payload.get("by"),
                               "comment": payload.get("comment", "")}
        return approvals, None

    def _run_args(self, job: Job) -> tuple[str, str | None, dict]:
        p = job.payload
        return (p.get("run_id") or f"job-{job.id}", p.get("input"),
                {**(p.get("metadata") or {}), "tenant_id": job.tenant_id})

    # ------------------------------------------------------------------ 同步

    def _call_sync(self, job: Job) -> dict:
        op = self._op(job)
        ckpt = self.checkpointer.fenced(job.fence, writer=job.worker_id)
        agent = self._shared if self._shared is not None else self._build(ckpt, job)
        if inspect.iscoroutinefunction(getattr(agent, "run", None)):
            raise PermanentJobError("AsyncAgent 请配合 AsyncPostgresCheckpointer 和 run_async_worker 使用")
        self._adopt(agent)
        kw = self._call_kwargs(agent, ckpt)
        token = _CURRENT_JOB.set(job)
        try:
            if op == "run":
                run_id, text, metadata = self._run_args(job)
                existing = ckpt.load(run_id)
                if existing is not None:
                    self._check_tenant(existing, job)
                    result = agent.resume(run_id, **kw)
                elif text is None:
                    raise PermanentJobError("run 任务缺少 input")
                else:
                    result = agent.run(text, metadata=metadata, run_id=run_id, **kw)
            else:
                run_id = job.payload.get("run_id")
                state = ckpt.load(run_id) if run_id else None
                if state is None:
                    raise PermanentJobError(f"找不到 run {run_id!r} 的检查点")
                self._check_tenant(state, job)
                approvals, decision = self._approval_step(state, job.payload)
                if decision is not None:  # approve() 会把"谁、何时、批没批"写进 approval_log，再继续运行
                    result = agent.approve(run_id, decision["approved"], by=decision["by"], comment=decision["comment"], **kw)
                else:
                    result = agent.resume(run_id, approvals, **kw)
        finally:
            _CURRENT_JOB.reset(token)
        return self._outcome(result)

    # ------------------------------------------------------------------ 异步

    async def _async_agent(self, ckpt, job: Job):
        if self._shared is not None:
            return self._shared
        if not self._per_job:  # 异步 + make_agent(checkpointer)：只建一次，之后所有任务共用
            agent = self._build(self.checkpointer, job)
            if inspect.isawaitable(agent):
                agent = await agent
            if _supports_run_checkpointer(agent):
                if self._shared is None:
                    self._shared = agent
                return self._shared
            self._per_job = True  # 不支持单次运行的 checkpointer：退回"每个任务建一个"
        agent = self._build(ckpt, job)
        return (await agent) if inspect.isawaitable(agent) else agent

    async def _call_async(self, job: Job) -> dict:
        op = self._op(job)
        ckpt = self.checkpointer.fenced(job.fence, writer=job.worker_id)
        agent = await self._async_agent(ckpt, job)
        if not inspect.iscoroutinefunction(agent.run):
            raise PermanentJobError("AsyncPostgresCheckpointer 需要配合 AsyncAgent 使用（agent.run 必须是 async）")
        self._adopt(agent)
        kw = self._call_kwargs(agent, ckpt)
        token = _CURRENT_JOB.set(job)
        try:
            if op == "run":
                run_id, text, metadata = self._run_args(job)
                existing = await ckpt.load(run_id)
                if existing is not None:
                    self._check_tenant(existing, job)
                    result = await agent.resume(run_id, **kw)
                elif text is None:
                    raise PermanentJobError("run 任务缺少 input")
                else:
                    result = await agent.run(text, metadata=metadata, run_id=run_id, **kw)
            else:
                run_id = job.payload.get("run_id")
                state = await ckpt.load(run_id) if run_id else None
                if state is None:
                    raise PermanentJobError(f"找不到 run {run_id!r} 的检查点")
                self._check_tenant(state, job)
                approvals, decision = self._approval_step(state, job.payload)
                if decision is not None:
                    result = await agent.approve(run_id, decision["approved"], by=decision["by"],
                                                 comment=decision["comment"], **kw)
                else:
                    result = await agent.resume(run_id, approvals, **kw)
        finally:
            _CURRENT_JOB.reset(token)
        return self._outcome(result)

    # ------------------------------------------------------------------ 结果

    def _outcome(self, result) -> dict:
        if result.status == "stopped" and result.stop_reason == "lease_lost":
            raise LeaseLost(f"run {result.run_id}：心跳发现租约已丢失，停止执行")
        if result.status == "stopped" and result.stop_reason in self.defer_stop_reasons:
            raise RetryLater(self.defer_seconds * random.uniform(1.0, 1.5), f"run {result.run_id} {result.stop_reason}")
        if result.status == "failed":
            raise RuntimeError(f"run {result.run_id} 失败：{result.stop_reason}")
        pending = None
        if result.pending_approval is not None:
            c = result.pending_approval
            pending = {"id": c.id, "name": c.name, "arguments": c.arguments}
        return {
            "run_id": result.run_id,
            "status": result.status,
            "output": result.output,
            "stop_reason": result.stop_reason,
            "awaiting_approval": result.status == "paused",
            "pending": pending,
            "steps": result.steps,
            "cost_usd": round(result.cost_usd, 6),
            "tools_called": result.tools_called(),
        }
