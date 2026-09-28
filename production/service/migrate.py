"""数据库迁移：建表（业务表、检查点表、队列表）+ 种子数据。幂等，可重复执行。

发布流水线里执行**一次**（K8s Job：deploy/k8s/migrate-job.yaml），而不是每个 Pod 启动时各跑一遍：
几十个 Pod 同时跑 DDL，最好的情况是互相排队（advisory lock），最坏的情况是旧版本的 Pod 读到了新表结构。
生产中建议换成 Alembic / Flyway / Atlas 这类有版本记录、能回滚的迁移工具。

    python -m production.service.migrate
"""

from __future__ import annotations

import asyncio
import os

from .backend import migrate


async def migrate_all(database_url: str, *, seed: bool = True) -> None:
    from psycopg_pool import AsyncConnectionPool

    from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue

    from .runtime import QUEUE_TABLE, RUNS_TABLE

    async with AsyncConnectionPool(database_url, min_size=1, max_size=2, kwargs={"autocommit": True}, open=False) as pool:
        await pool.open(wait=True, timeout=30)
        await PostgresCheckpointer(pool, RUNS_TABLE).setup()
        await PostgresJobQueue(pool, QUEUE_TABLE).setup()
        await migrate(pool, seed=seed)


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("缺少 DATABASE_URL")
    asyncio.run(migrate_all(url, seed=os.environ.get("SEED_DEMO_DATA", "true").lower() in ("1", "true", "yes")))
    print('{"event": "migrated"}', flush=True)


if __name__ == "__main__":
    main()
