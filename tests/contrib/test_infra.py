"""确认第四部分共用的本地基础设施可用（未安装可选依赖时自动跳过）。"""

import pytest

psycopg = pytest.importorskip("psycopg")


def test_each_test_gets_a_fresh_postgres_database(pg_uri):
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("CREATE TABLE only_here (id int)")
        assert conn.execute("SELECT count(*) FROM only_here").fetchone()[0] == 0


def test_fake_redis_supports_nx_and_lua(redis_client):
    assert redis_client.set("k", "v", nx=True) and redis_client.set("k", "v", nx=True) is None
    assert redis_client.register_script("return 7")() == 7
