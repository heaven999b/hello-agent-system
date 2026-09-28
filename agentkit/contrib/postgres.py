"""Postgres 适配器：检查点（版本号 CAS + fence 接管）与 SKIP LOCKED 任务队列（第 26 课）。

第 13 课用 SQLite（agentkit.distributed.sqlite）讲清了租约、fencing token、CAS，并且在本机用真进程跑通了；
这里是同一套接口的多机版：换成 Postgres（多台机器共享、行级锁、服务器时钟）。

    PostgresCheckpointer     Checkpointer 协议：save / load；fenced(fence) 返回接管视图
    PostgresJobQueue         JobQueue 协议：enqueue / claim / heartbeat / complete / fail / release ...

worker 循环（run_worker）、AgentJobHandler、异常（LeaseLost、CheckpointConflict……）与 SQLite 版共用，
来自 agentkit.distributed，这里一并导出方便使用；worker 命令行 `python -m agentkit.distributed.worker
--queue postgresql://...` 同样适用。

依赖：psycopg 3 和 psycopg_pool（pip install -e ".[postgres]"，即 psycopg[binary,pool]）。
连接参数可以是连接串（自己建 AsyncConnectionPool），也可以是现成的 AsyncConnectionPool（推荐队列和检查点共用一个），
池的大小要按"同时需要连接的协程数"来定（README 卡片 6、7）。

多进程使用时，每个进程用连接串**自己**创建这些对象：数据库连接不能跨进程共享。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Iterable

from ..distributed.jobs import (
    JOB_STATUSES,
    AgentJobHandler,
    CheckpointConflict,
    Job,
    LeaseLost,
    PermanentJobError,
    RetryLater,
    explain_rejection,
    run_worker,
    stop_on_signals,
)
from ..state import RunState
from . import require

psycopg = require("psycopg", "postgres")
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

__all__ = [
    "AgentJobHandler",
    "CheckpointConflict",
    "Job",
    "LeaseLost",
    "PermanentJobError",
    "PostgresCheckpointer",
    "PostgresJobQueue",
    "RetryLater",
    "TRANSIENT_ERRORS",
    "run_worker",
    "stop_on_signals",
]

try:
    from psycopg_pool import PoolTimeout as _PoolTimeout

    TRANSIENT_ERRORS: tuple = (psycopg.OperationalError, _PoolTimeout)
except ImportError:  # 没装 psycopg_pool（传入的是自己实现的连接池）
    TRANSIENT_ERRORS = (psycopg.OperationalError,)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,50}$")


# =====================================================================================
# 连接管理
# =====================================================================================


class _Db:
    """所有协程共用一个 AsyncConnectionPool，每次操作借一个连接、用完立刻归还。

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
                    # wait=False：不在这里等连接建好。wait=True 时如果数据库此刻不可用，psycopg_pool 会在超时后
                    # 把池关掉，之后每次调用都报 PoolClosed —— 长驻进程（API）在故障期间第一次用队列，就永远恢复不了。
                    # 不等的话，池在后台重连；借连接时拿不到就抛 PoolTimeout（暂时性错误），数据库恢复后自然好转。
                    await self.pool.open(wait=False)
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


async def _setup(db: _Db, lock_name: str, statements: list) -> None:
    """幂等建表。多个进程同时启动时，并发的 CREATE TABLE IF NOT EXISTS 会互相撞车
    （实测 8 个连接同时执行，7 个报 UniqueViolation: pg_class_relname_nsp_index），
    所以用事务级 advisory lock 把它们排成一队。生产中更推荐用迁移工具（Alembic / Flyway）在发布时执行一次。"""
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
    """检查点的公共部分：SQL、版本号记录、参数与冲突信息的构造。"""

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

    pool_or_dsn：连接串（自己建 AsyncConnectionPool）或现成的 AsyncConnectionPool（推荐和队列共用一个）。
    用完 await close()，或者 async with PostgresCheckpointer(...) as ckpt。
    """

    def __init__(
        self,
        pool_or_dsn,
        table: str = "agent_runs",
        *,
        fence: int | None = None,
        writer: str | None = None,
        pool_kwargs: dict | None = None,
        _db: _Db | None = None,
    ):
        super().__init__(table, fence, writer)
        self._db = _db or _Db(pool_or_dsn, pool_kwargs)

    async def __aenter__(self) -> "PostgresCheckpointer":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def setup(self) -> None:
        await _setup(self._db, self.table, _checkpoint_ddl(self.table))

    def fenced(self, fence: int, writer: str | None = None) -> "PostgresCheckpointer":
        w = writer if writer is not None else self.writer
        return PostgresCheckpointer("", self.table, fence=int(fence), writer=w, _db=self._db)

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
    """队列的公共部分：SQL、参数构造、统计结果整理、拒绝原因的解释。"""

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

    _explain = staticmethod(explain_rejection)


class PostgresJobQueue(_QueueBase):
    """基于 `FOR UPDATE SKIP LOCKED` 的任务队列：至少一次投递 + 租约 + fencing token + 退避重试 + 死信。

    状态机（与第 13 课的 SQLiteJobQueue 相同，接口也相同）：
        queued ──claim（attempts+1, fence↑）───► leased ──complete──► succeeded   （fence↑：取全局序列的下一个值）
          ▲  ▲                                     │ ├──fail(retryable=False)──► failed
          │  └──── fail(可重试，退避) / release ───┘ └──fail 且次数用尽──► dead ──redrive──► queued
          └──────── 租约过期被回收（reap）──────────┘（次数用尽则直接 dead：毒消息）

    与 SQLite 版的区别：
      - 多台机器共享：worker 通过网络连接数据库，不再局限于一台机器；
      - 领取用 SKIP LOCKED：多个 worker 并发领取时互相跳过被锁的行，而不是排队等整个库的写锁；
      - 所有时间都用数据库的 now()：租约不受各机器时钟漂移影响；
      - 过期租约由 reap_expired() 先"回收"成 queued，claim 只扫 status='queued' 的部分索引，积压很大时依然是索引扫描。
        回收之后，旧持有者的 complete / heartbeat 会因为 status 不再是 leased 而被拒绝（它的所有权在回收那一刻结束）。

    和 PostgresCheckpointer 共用一个连接池最省连接。transient_errors：数据库暂时不可用的异常类型，
    run_worker 领取时遇到它们会退避重试，而不是让 worker 崩溃。
    """

    transient_errors = TRANSIENT_ERRORS

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
        self._db = _Db(pool_or_dsn, pool_kwargs)

    async def __aenter__(self) -> "PostgresJobQueue":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def setup(self) -> None:
        await _setup(self._db, self.table, _queue_ddl(self.table))

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
        return Job.from_row(rows[0]) if rows else None

    async def find(self, tenant_id: str, idempotency_key: str) -> Job | None:
        rows = await self._db.rows(self._sql["find"], {"tenant_id": tenant_id, "key": idempotency_key})
        return Job.from_row(rows[0]) if rows else None

    async def reap_expired(self) -> list[tuple[int, str]]:
        rows = await self._db.rows(self._sql["reap"], {"cap": self.max_backoff, "base": self.base_backoff})
        return [(r["id"], r["status"]) for r in rows]

    async def claim(self, worker_id: str, lease_seconds: float = 30, kinds: Iterable[str] | None = None) -> Job | None:
        await self.reap_expired()
        rows = await self._db.rows(self._sql["claim"], self._claim_params(worker_id, lease_seconds, kinds))
        return Job.from_row(rows[0]) if rows else None

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
