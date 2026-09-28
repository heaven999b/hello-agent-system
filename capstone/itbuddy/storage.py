"""ITBuddy 自己的状态：检查点、幂等记录、审计日志 —— 全部放在一个 SQLite 文件里，所有进程共享。

    itbuddy.db（部署时和任务队列是同一个文件，每个进程共用一个连接）
    ├── agent_jobs / agent_jobs_fence   任务队列（SQLiteJobQueue，API 进程写入、worker 进程领取）
    ├── agent_runs                      检查点（SQLiteCheckpointer：版本号 CAS + fence 接管）
    ├── idempotency                     Agent 这一层的幂等记录（SQLiteIdempotencyStore：run_id:call_id → 上次的结果）
    ├── audit_log                       审计日志（本文件的 AuditStore：只追加）
    └── circuit_breakers                所有 worker 共享的熔断器状态（SQLiteCircuitBreaker）

为什么审计放 SQLite 表，而不是每个进程一个 JSONL 文件？
1. 一次审批横跨两个进程：审批决定由 API 进程写，工具执行（approved_by）由 worker 进程写。
   "谁在何时批准了什么、最后执行了没有"要能用**一条查询**回答，而不是去 N 个文件里拼；
2. 审批的仲裁要靠数据库约束：同一个待审批调用只能有一条 approval_decision（部分唯一索引）。
   两个审批人落在两个 API 进程上同时点"批准 / 拒绝"，只有先插进去的那条算数 —— 以前靠进程内的
   threading.Lock，只在一个进程里有效；
3. 自增 id 给出所有进程写入的全序；每条记录带 writer（哪个进程）和 pid。
"只追加"用触发器保证：UPDATE / DELETE 直接报错。它防的是程序 bug 和误操作，防不了能改文件的人 ——
合规要求的不可篡改要写进 WORM 存储或独立的追加式日志服务（DESIGN.md 10.2）。

追踪（traces）没有放进来：每个进程写自己的 traces/<进程名>.jsonl（追踪量大、可以丢、不需要跨进程查询），
`python -m agentkit.viewer <目录>` 可以一次读整个目录。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agentkit.distributed import SQLiteCheckpointer, SQLiteDB, SQLiteIdempotencyStore


class AuditStore:
    """所有进程共享、只追加的审计表。append 返回 False 表示被唯一约束挡下（同一个调用的第二个审批决定）。"""

    def __init__(self, path_or_db: str | Path | SQLiteDB, table: str = "audit_log", *, writer: str | None = None):
        if isinstance(path_or_db, SQLiteDB):
            self.db, self._owns_db = path_or_db, False
        else:
            self.db, self._owns_db = SQLiteDB(path_or_db), True
        if not table.replace("_", "").isalnum():
            raise ValueError(f"非法的表名：{table!r}")
        self.table = table
        self.writer = writer or f"pid-{os.getpid()}"

    async def setup(self) -> None:
        t = self.table

        def ddl(conn):
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {t} (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        REAL NOT NULL,
                    event     TEXT NOT NULL,
                    tenant_id TEXT,
                    run_id    TEXT,
                    call_id   TEXT,
                    writer    TEXT NOT NULL,
                    pid       INTEGER NOT NULL,
                    record    TEXT NOT NULL)"""
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_run_idx ON {t} (run_id, id)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_tenant_idx ON {t} (tenant_id, id)")
            # 一个待审批的调用只能有一个决定：审批的"锁"是这条唯一约束，而不是某个进程里的 threading.Lock
            conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {t}_one_decision ON {t} (run_id, call_id) "
                         f"WHERE event = 'approval_decision'")
            for op in ("UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                             f"BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END")

        await self.db.write(ddl)

    async def close(self) -> None:
        if self._owns_db:
            await self.db.close()

    async def append(self, record: dict) -> bool:
        """写一条记录（自动补上 ts、writer、pid）。返回是否真的写入。"""
        rec = {"ts": time.time(), **record}
        rec.setdefault("writer", self.writer)
        rec.setdefault("pid", os.getpid())
        row = (rec["ts"], rec.get("event", "?"), rec.get("tenant_id"), rec.get("run_id"), rec.get("call_id"),
               rec["writer"], rec["pid"], json.dumps(rec, ensure_ascii=False, default=str))
        inserted = await self.db.write(lambda conn: conn.execute(
            f"INSERT OR IGNORE INTO {self.table} (ts, event, tenant_id, run_id, call_id, writer, pid, record) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row).rowcount)
        return inserted > 0

    async def records(self, *, run_id: str | None = None, event: str | None = None, tenant_id: str | None = None,
                      limit: int | None = None) -> list[dict]:
        """按写入顺序返回记录（每条是写入时的字典，加上 id）。limit：只要最后 N 条。"""
        where, params = [], []
        for col, value in (("run_id", run_id), ("event", event), ("tenant_id", tenant_id)):
            if value is not None:
                where.append(f"{col} = ?")
                params.append(value)
        sql = f"SELECT id, record FROM {self.table}" + (f" WHERE {' AND '.join(where)}" if where else "")
        if limit is not None:
            sql = f"SELECT * FROM ({sql} ORDER BY id DESC LIMIT {int(limit)})"
        rows = await self.db.run(lambda conn: conn.execute(sql + " ORDER BY id", params).fetchall())
        return [{"id": r["id"], **json.loads(r["record"])} for r in rows]

    async def decision(self, run_id: str, call_id: str) -> dict | None:
        """某个待审批调用的审批决定（没有则 None）。"""
        row = await self.db.run(lambda conn: conn.execute(
            f"SELECT id, record FROM {self.table} WHERE run_id = ? AND call_id = ? AND event = 'approval_decision'",
            (run_id, call_id)).fetchone())
        return {"id": row["id"], **json.loads(row["record"])} if row else None


@dataclass
class ITBuddyStores:
    """一个进程里 ITBuddy 用到的共享状态（同一个 SQLite 文件、同一个连接）。

    stores = await ITBuddyStores.open("runs/itbuddy.db")           # 自己打开文件（close 时关闭）
    stores = await ITBuddyStores.open(wctx.db, writer="worker-1")  # worker 进程：借用任务队列的连接
    """

    db: SQLiteDB
    checkpointer: SQLiteCheckpointer
    idempotency: SQLiteIdempotencyStore
    audit: AuditStore
    owns_db: bool = False

    @classmethod
    async def open(cls, path_or_db: str | Path | SQLiteDB, *, writer: str | None = None) -> "ITBuddyStores":
        owns = not isinstance(path_or_db, SQLiteDB)
        db = SQLiteDB(path_or_db) if owns else path_or_db
        writer = writer or f"pid-{os.getpid()}"
        stores = cls(db, SQLiteCheckpointer(db, writer=writer), SQLiteIdempotencyStore(db), AuditStore(db, writer=writer),
                     owns)
        for part in (stores.checkpointer, stores.idempotency, stores.audit):
            await part.setup()
        return stores

    async def close(self) -> None:
        if self.owns_db:
            await self.db.close()

    async def __aenter__(self) -> "ITBuddyStores":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
