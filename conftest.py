"""仓库级 pytest fixture：第四部分的测试共用的真实基础设施（按需启动，未安装依赖时自动跳过）。"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def pg_server_uri():
    """整个测试会话共用一个嵌入式 Postgres 进程。"""
    pytest.importorskip("pgserver")
    pytest.importorskip("psycopg")
    from agentkit.testing import embedded_postgres

    with embedded_postgres() as uri:
        yield uri


@pytest.fixture
def pg_uri(pg_server_uri):
    """每个测试一个全新的数据库：测试之间互不干扰，也不用手动清表。"""
    from agentkit.testing import create_database

    return create_database(pg_server_uri)


@pytest.fixture(scope="session")
def redis_url():
    pytest.importorskip("fakeredis")
    pytest.importorskip("redis")
    from agentkit.testing import fake_redis_server

    with fake_redis_server() as url:
        yield url


@pytest.fixture
def redis_client(redis_url):
    """每个测试前清空 Redis。"""
    import redis

    client = redis.Redis.from_url(redis_url)
    client.flushall()
    yield client
    client.close()
