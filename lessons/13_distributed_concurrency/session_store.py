"""会话状态存储：用"版本号 + CAS"做乐观并发控制（optimistic concurrency control, OCC）。

问题：同一个会话被两个 worker 同时"读 → 改 → 写"（用户连发两条消息、手机和电脑同时在聊）：

    worker A: 读到 v7（3 条消息）─────── 调模型 5 秒 ─────── 写回 4 条消息
    worker B:      读到 v7（3 条消息）─── 调模型 5 秒 ─── 写回 4 条消息   ← 覆盖了 A 的写入
    结果：两条新消息只剩一条 —— 这就是"丢失更新"（lost update），而且不会有任何报错。

乐观并发控制：每行带一个 version。写的时候说"只有版本还是我读到的那个，才写入"：

    UPDATE sessions SET data = ?, version = version + 1
    WHERE session_id = ? AND version = ?      -- ← 就是这个条件
    影响行数 = 0 → 在我读完之后有人写过了 → ConflictError → 重新读最新版本，再应用一次修改

这是一条原子的"比较并交换"（compare-and-set, CAS）。它不加锁、不阻塞别人，冲突少时几乎零成本；
冲突多时会反复重试 —— 对 Agent 来说每次重试可能意味着**重新调用一次模型**，这就是它的代价（见 README 问题 4）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,      -- 每次成功写入 +1；0 表示"还不存在"
    data       TEXT    NOT NULL,      -- JSON
    updated_at REAL    NOT NULL
);
"""


class ConflictError(Exception):
    """CAS 失败：你读到的版本已经不是最新的了（在你读之后、写之前，别人先写了）。"""

    def __init__(self, session_id: str, expected_version: int, actual_version: int):
        super().__init__(f"会话 {session_id} 版本冲突：你基于 v{expected_version} 修改，但当前已是 v{actual_version}")
        self.session_id = session_id
        self.expected_version = expected_version
        self.actual_version = actual_version


@dataclass
class Session:
    session_id: str
    version: int  # 0 = 还不存在
    data: dict = field(default_factory=dict)


class SessionStore:
    """每个线程 / 进程各建一个自己的 SessionStore（内部持有一个 sqlite3 连接）。"""

    def __init__(self, path: str | Path, timeout: float = 10.0):
        self.conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
        self.conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def get(self, session_id: str) -> Session:
        """读取最新版本。不存在时返回 version=0 的空会话。每次返回的 data 都是新解析出来的独立副本。"""
        row = self.conn.execute("SELECT version, data FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return Session(session_id, 0, {})
        return Session(session_id, row[0], json.loads(row[1]))

    def compare_and_set(self, session_id: str, expected_version: int, data: dict) -> Session:
        """只有当前版本 == expected_version 时才写入，并把版本 +1；否则抛 ConflictError。

        expected_version=0 表示"我认为它还不存在"：用 INSERT 创建，主键冲突说明别人抢先创建了。
        """
        body = json.dumps(data, ensure_ascii=False)
        now = time.time()
        if expected_version == 0:
            try:
                self.conn.execute(
                    "INSERT INTO sessions (session_id, version, data, updated_at) VALUES (?, 1, ?, ?)",
                    (session_id, body, now),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(session_id, 0, self._version(session_id)) from None
            return Session(session_id, 1, json.loads(body))
        cur = self.conn.execute(
            "UPDATE sessions SET data = ?, version = version + 1, updated_at = ? WHERE session_id = ? AND version = ?",
            (body, now, session_id, expected_version),
        )
        if cur.rowcount != 1:
            raise ConflictError(session_id, expected_version, self._version(session_id))
        return Session(session_id, expected_version + 1, json.loads(body))

    def put_unsafe(self, session_id: str, data: dict) -> None:
        """❌ 反面教材：不看版本号，直接覆盖（"最后写入者胜"，last-write-wins）。Demo 用它复现丢失更新。"""
        self.conn.execute(
            """INSERT INTO sessions (session_id, version, data, updated_at) VALUES (?, 1, ?, ?)
               ON CONFLICT (session_id) DO UPDATE
               SET data = excluded.data, version = sessions.version + 1, updated_at = excluded.updated_at""",
            (session_id, json.dumps(data, ensure_ascii=False), time.time()),
        )

    def _version(self, session_id: str) -> int:
        row = self.conn.execute("SELECT version FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else 0
