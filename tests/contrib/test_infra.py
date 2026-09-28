"""确认第四部分共用的本地基础设施可用，而且用的是适配器实际使用的 async 驱动（未安装可选依赖时自动跳过）。"""

import pytest


async def test_each_test_gets_a_fresh_postgres_database(pg_uri):
    psycopg = pytest.importorskip("psycopg")
    async with await psycopg.AsyncConnection.connect(pg_uri, autocommit=True) as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE TABLE only_here (id int)")
        cur = await conn.execute("SELECT count(*) FROM only_here")
        assert (await cur.fetchone())[0] == 0


async def test_fake_redis_supports_nx_and_lua(redis_client, redis_url):
    import redis.asyncio as aredis

    r = aredis.Redis.from_url(redis_url)
    try:
        assert await r.set("k", "v", nx=True) and await r.set("k", "v", nx=True) is None
        assert await r.eval("return 7", 0) == 7
        sha = await r.script_load("return 8")  # 适配器先 SCRIPT LOAD 预热、再 EVALSHA（原因见 redis_store._Scripts）
        assert await r.evalsha(sha, 0) == 8
    finally:
        await r.aclose()
