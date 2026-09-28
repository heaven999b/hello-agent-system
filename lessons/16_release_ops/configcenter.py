"""配置中心：紧急开关、灰度流量配置、prompt 版本 —— 所有 worker 进程读同一份带版本号的配置（第 16 课 问题 1、2、4）。

    ConfigCenter   写入端（控制面：值班工程师、发布机器人）。SQLite 里一张"文档表"：每个配置（如 "flags"、
                   "release:itbuddy.system"）是一个 JSON 文档 + 一个单调递增的版本号；每次修改都在同一个事务里
                   写一行审计（谁、为什么、改之前、改之后、新版本号）。
    ConfigWatcher  读取端（每个 worker 进程一个）。后台每 poll_interval 秒查一次版本号（一条很便宜的 SELECT），
                   变了才读全文、换掉本地快照；请求路径上只读快照（snapshot()：纯内存，不 await、不查库）。

为什么是"轮询版本号 + 本地快照"？
    - 请求路径不碰数据库：KillSwitch 每个工具调用都要看一眼开关，如果每次都查库，配置中心一抖，所有请求一起变慢；
    - 生效时间有明确上界：一次修改最多 poll_interval 秒后被每个进程看到（本课 Demo 场景 5 在 3 个真 worker 进程上实测）；
    - 配置中心暂时读不到（数据库被锁、网络抖动）：保留最后一次成功读到的配置继续服务（fail-static），下一轮再试。
      进程**启动时**读不到则直接失败 —— 没有任何已知配置时，宁可不接流量。

它就是 etcd / Consul / Apollo / Nacos / LaunchDarkly 这类系统在单机上的最小版本：它们用 watch / 长轮询 / 推送
代替固定间隔的轮询（配置一变就通知客户端），并且多机可用。局限：SQLite 只能在一台机器上共享。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from pathlib import Path
from typing import Callable, Iterable

from agentkit.distributed import SQLiteDB

logger = logging.getLogger("lesson16.config")


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise ValueError(f"非法的表名：{name!r}")
    return name


class ConfigCenter:
    """带版本号的配置文档 + 审计日志，存在一个 SQLite 文件里，同一台机器上的所有进程共享。"""

    def __init__(self, path_or_db: str | Path | SQLiteDB, table: str = "config_docs", *,
                 clock: Callable[[], float] = time.time):
        if isinstance(path_or_db, SQLiteDB):
            self.db, self._owns_db = path_or_db, False
        else:
            self.db, self._owns_db = SQLiteDB(path_or_db), True
        self.table = _ident(table)
        self.clock = clock

    async def setup(self) -> None:
        t = self.table

        def ddl(conn):
            conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (name TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                         f"doc TEXT NOT NULL, updated_at REAL NOT NULL, updated_by TEXT)")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {t}_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                         f"version INTEGER NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, before TEXT, after TEXT, "
                         f"t REAL NOT NULL)")

        await self.db.write(ddl)

    async def get(self, name: str) -> tuple[int, dict]:
        """(版本号, 文档)。不存在时返回 (0, {})：任何缺失的配置都等于"没有限制 / 默认值"。"""
        row = await self.db.run(lambda conn: conn.execute(
            f"SELECT version, doc FROM {self.table} WHERE name = ?", (name,)).fetchone())
        return (0, {}) if row is None else (row["version"], json.loads(row["doc"]))

    async def versions(self, names: Iterable[str]) -> dict[str, int]:
        names = list(names)
        marks = ",".join("?" * len(names))
        rows = await self.db.run(lambda conn: conn.execute(
            f"SELECT name, version FROM {self.table} WHERE name IN ({marks})", names).fetchall())
        found = {r["name"]: r["version"] for r in rows}
        return {n: found.get(n, 0) for n in names}

    async def update(self, name: str, change: Callable[[dict], dict | None], *, actor: str, reason: str) -> int:
        """读-改-写一个配置文档，返回新版本号。整个过程在一个 BEGIN IMMEDIATE 事务里：
        两个人同时改（值班工程师停用工具、发布机器人扩量），不会有人的修改被悄悄覆盖。

        change(doc) 可以原地修改 doc 后返回 None，也可以返回一个新文档。reason 不能为空：事后复盘第一个问题就是"为什么改"。
        """
        if not actor or not reason.strip():
            raise ValueError("actor 和 reason 都不能为空：每一次配置变更都要能回答'谁、为什么'")
        t = self.table

        def op(conn):
            now = self.clock()
            row = conn.execute(f"SELECT version, doc FROM {t} WHERE name = ?", (name,)).fetchone()
            before = json.loads(row["doc"]) if row else {}
            draft = copy.deepcopy(before)
            after = change(draft)
            after = draft if after is None else after
            version = (row["version"] if row else 0) + 1
            conn.execute(f"INSERT OR REPLACE INTO {t} (name, version, doc, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
                         (name, version, json.dumps(after, ensure_ascii=False, sort_keys=True), now, actor))
            conn.execute(f"INSERT INTO {t}_audit (name, version, actor, reason, before, after, t) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (name, version, actor, reason, json.dumps(before, ensure_ascii=False, sort_keys=True),
                          json.dumps(after, ensure_ascii=False, sort_keys=True), now))
            return version

        return await self.db.write(op)

    async def set(self, name: str, doc: dict, *, actor: str, reason: str) -> int:
        return await self.update(name, lambda _: copy.deepcopy(doc), actor=actor, reason=reason)

    async def audit(self, name: str | None = None, limit: int = 100) -> list[dict]:
        def op(conn):
            q, params = f"SELECT * FROM {self.table}_audit", []
            if name is not None:
                q, params = q + " WHERE name = ?", [name]
            rows = conn.execute(q + " ORDER BY id LIMIT ?", (*params, int(limit))).fetchall()
            return [{**dict(r), "before": json.loads(r["before"]), "after": json.loads(r["after"])} for r in rows]

        return await self.db.run(op)

    async def close(self) -> None:
        if self._owns_db:
            await self.db.close()

    async def __aenter__(self) -> "ConfigCenter":
        await self.setup()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class ConfigWatcher:
    """一个进程里的配置读取端：后台轮询版本号，请求路径只读本地快照。

        watcher = ConfigWatcher(center, ["flags", "release:itbuddy.system"], poll_interval=0.2)
        await watcher.start()                       # 先同步读一次：没有配置就不接流量
        KillSwitch(lambda: watcher.snapshot("flags"), tools)   # 每次工具调用读快照，不查库
        ...
        await watcher.aclose()                      # 进程退出前停掉后台轮询

    on_change(name, old_version, new_version, doc)：看到新版本时调用（记日志 / 打事件，用来测"多久生效"）。
    snapshot() 返回的字典请当成只读的：它会被这个进程里所有并发的请求共用。
    """

    def __init__(self, center: ConfigCenter, names: Iterable[str], *, poll_interval: float = 0.5,
                 on_change: Callable[[str, int, int, dict], None] | None = None):
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        self.center = center
        self.names = list(names)
        self.poll_interval = poll_interval
        self.on_change = on_change
        self._docs: dict[str, dict] = {n: {} for n in self.names}
        self._versions: dict[str, int] = {n: -1 for n in self.names}
        self._task: asyncio.Task | None = None
        self.polls = 0
        self.errors = 0  # 轮询失败次数（失败时保留旧快照继续服务）

    def snapshot(self, name: str) -> dict:
        return self._docs[name]

    def version(self, name: str) -> int:
        return self._versions[name]

    async def refresh(self) -> list[str]:
        """查一次版本号，变了的重新读全文。返回这次更新了的配置名。"""
        self.polls += 1
        latest = await self.center.versions(self.names)
        changed = []
        for name, v in latest.items():
            if v == self._versions[name]:
                continue
            version, doc = await self.center.get(name)  # 读全文时可能又被改了一次：以读到的版本号为准
            old = self._versions[name]
            self._docs[name], self._versions[name] = doc, version  # 整体替换（不是原地修改）：正在读旧快照的请求不受影响
            changed.append(name)
            if self.on_change is not None and old >= 0:
                try:
                    self.on_change(name, old, version, doc)
                except Exception:  # noqa: BLE001 —— 回调出错不能拖垮轮询
                    logger.exception("on_change 回调出错")
        return changed

    async def start(self) -> "ConfigWatcher":
        await self.refresh()  # 启动时必须读到：这里的异常直接抛给调用方
        self._task = asyncio.create_task(self._poll_loop(), name="config-watcher")
        return self

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001 —— fail-static：读不到就继续用最后一次成功读到的配置
                self.errors += 1
                logger.warning("读取配置中心失败，继续使用版本 %s：%s", self._versions, e)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.wait({self._task})
            self._task = None
