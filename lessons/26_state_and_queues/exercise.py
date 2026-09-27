"""第 26 课练习：把第 13 课的三个核心动作，用 Postgres 和 Redis 的"生产写法"再写一遍。

  (a) cas_save        检查点的乐观并发：一条带 version 条件的 SQL（CAS）
  (b) claim_one       任务领取：回收过期租约 + FOR UPDATE SKIP LOCKED 原子领取（8 个线程同时抢，不能领重）
  (c) refill + TOKEN_BUCKET_LUA   令牌桶：先写纯函数想清楚补充逻辑，再把它搬进 Redis 的 Lua 脚本

测试用的是真实的 Postgres（嵌入式，pgserver）和 fakeredis（支持 Lua），由仓库根目录 conftest.py 提供；
没装可选依赖时测试会自动跳过：pip install -e ".[prod,prod-local]"

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=26
    # 或者：.venv/bin/python -m pytest lessons/26_state_and_queues -v

卡住了？先看 README 第 3 节（适配器怎么接），再看 agentkit/contrib/postgres.py 和 redis_store.py 里的完整版。
"""

from __future__ import annotations

# =====================================================================
# 已经写好的部分：表结构和几个小工具（不用改）
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     text PRIMARY KEY,
    version    bigint NOT NULL,
    state      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jobs (
    id           bigserial PRIMARY KEY,
    payload      jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'queued',      -- queued / leased / done / dead
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    fence        bigint NOT NULL DEFAULT 0,
    worker_id    text,
    lease_until  timestamptz,
    run_at       timestamptz NOT NULL DEFAULT now()
);
"""


def setup(conn) -> None:
    """建表。conn 是 psycopg.connect(..., autocommit=True, row_factory=dict_row) 打开的连接。"""
    conn.execute(SCHEMA)


def load_run(conn, run_id: str) -> dict | None:
    """读一条检查点：{"run_id", "version", "state"}，不存在返回 None。"""
    return conn.execute("SELECT run_id, version, state FROM runs WHERE run_id = %s", (run_id,)).fetchone()


def enqueue(conn, payload: dict | None = None, *, delay_seconds: float = 0, max_attempts: int = 3) -> int:
    """入队一个任务，返回 id。delay_seconds > 0 表示"这么多秒之后才能被领取"（延迟任务 / 退避中）。"""
    import json

    row = conn.execute(
        "INSERT INTO jobs (payload, max_attempts, run_at) VALUES (%s::jsonb, %s, now() + make_interval(secs => %s)) "
        "RETURNING id",
        (json.dumps(payload or {}), max_attempts, float(delay_seconds)),
    ).fetchone()
    return row["id"]


# =====================================================================
# 练习 (a)：cas_save —— 检查点的 CAS
# =====================================================================


def cas_save(conn, run_id: str, expected_version: int, state_json: str) -> bool:
    """把 state_json（JSON 文本）写进 runs 表，**只有**数据库里的版本号还是 expected_version 时才写。

    返回 True 表示写进去了，False 表示冲突（在你读完之后有人写过 / 别人抢先创建了它）。
      - expected_version == 0：表示"我认为它还不存在" → INSERT，version = 1
            用 INSERT ... ON CONFLICT (run_id) DO NOTHING；插入了 0 行 → 别人抢先创建了 → 返回 False
      - expected_version >= 1：UPDATE runs SET state = ..., version = version + 1, updated_at = now()
                                WHERE run_id = ... AND version = expected_version
            更新了 0 行 → 版本号对不上 → 返回 False（什么都不改）

    提示：
      - state_json 是字符串，SQL 里写 %s::jsonb 转成 jsonb；
      - 判断写没写进去：cursor.rowcount（conn.execute 返回的就是 cursor），或者加 RETURNING 看有没有返回行；
      - 为什么不能"先 SELECT 看版本号对不对，再 UPDATE"？两步之间别人可能已经写了 —— 检查必须放进
        UPDATE 的 WHERE 里，由数据库在写入的那一刻完成（第 13 课 3.4 节）。
    """
    raise NotImplementedError("TODO: 练习 (a) —— INSERT ... ON CONFLICT DO NOTHING / UPDATE ... WHERE version = ?")


# =====================================================================
# 练习 (b)：claim_one —— 回收过期租约 + SKIP LOCKED 领取
# =====================================================================


def claim_one(conn, worker_id: str, lease_seconds: float) -> dict | None:
    """领取一个任务，返回领取后的整行（dict），没有可领取的任务返回 None。分两步（可以放进同一个事务）：

    第 1 步：回收过期租约。status='leased' 且 lease_until < now() 的任务，持有者大概率已经崩溃：
        - attempts >= max_attempts → status='dead'（毒消息：每次都把 worker 弄崩，别再发给下一个了）
        - 否则                    → status='queued'（重新排队）
      两种情况都把 lease_until 清成 NULL。

    第 2 步：原子领取"最老的"可领取任务（status='queued' 且 run_at <= now()，按 run_at, id 排序（适配器改成了按 id 排序，原因见 README 3.3；本练习只测"先领到最老的可执行任务"，两种写法都对））：
        UPDATE jobs SET status='leased', worker_id=..., lease_until = now() + make_interval(secs => ...),
                        attempts = attempts + 1, fence = fence + 1
        WHERE id = (SELECT id FROM jobs WHERE ... ORDER BY run_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING *

    ⚠️ 为什么一定要 FOR UPDATE SKIP LOCKED？
      - 只写 SELECT ... LIMIT 1 再 UPDATE：两个 worker 可能选中同一行，都更新成功 → 一个任务被执行两次；
      - 只写 FOR UPDATE（不跳过）：第二个 worker 会**排队等**第一个 worker 的行锁，所有 worker 被串行化；
      - FOR UPDATE SKIP LOCKED：被别人锁住的行直接跳过，去拿下一个。测试会拿着一行的锁不放，
        检查你的实现是跳过它（正确），还是卡住等锁（测试给连接设了 lock_timeout，卡住会报错）。
    时间一律用数据库的 now()，不要用 Python 的 time.time()：所有 worker 以同一个时钟判断租约。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 先回收过期租约，再用 FOR UPDATE SKIP LOCKED 领取")


# =====================================================================
# 练习 (c)：令牌桶 —— 纯函数 + Lua 脚本
# =====================================================================


def refill(tokens: float, last_ts: float, now: float, rate: float, capacity: float) -> float:
    """惰性补充：距上次更新过了 (now - last_ts) 秒，按 rate（个/秒）补令牌，最多补到 capacity。

    边界：now <= last_ts（时钟没走，或者倒退了）→ 不补，原样返回 tokens（第 12 课讲过为什么不能往回拨）。
    例：refill(0, 100.0, 102.5, rate=1, capacity=10) == 2.5；refill(9, 100, 200, 1, 10) == 10
    """
    raise NotImplementedError("TODO: 练习 (c) —— 令牌桶的补充公式")


# 把上面的逻辑搬进 Redis 的 Lua 脚本（整个"读 → 补充 → 判断 → 写回"必须原子，所以必须在 Redis 里做）。
# 调用约定（测试和 take() 都按这个来调用）：
#   KEYS[1] = 桶的 key，是一个 hash，字段固定叫 tokens 和 ts（上次更新的时间，秒，可以带小数）
#   ARGV[1] = rate（个/秒），ARGV[2] = capacity，ARGV[3] = 这次要拿的令牌数
#   返回 {allowed, tostring(剩余令牌数)}：allowed 是 1 或 0
# 要点：
#   - 当前时间用 redis.call('TIME')（返回 {秒, 微秒} 两个字符串），不要从客户端传进来：所有 worker 共用 Redis 的时钟
#   - 桶不存在（HMGET 拿到 nil）→ 当成满桶：tokens = capacity，ts = now
#   - 不管放不放行，都把新的 tokens 和 ts 用 HSET 写回去
#   - 剩余令牌数要 tostring() 再返回：Lua 的小数直接返回给 Redis 会被截断成整数（1.5 会变成 1）
TOKEN_BUCKET_LUA: str | None = None  # TODO: 练习 (c) —— 换成你的 Lua 脚本字符串


def take(client, key: str, rate: float, capacity: float, requested: float = 1) -> tuple[bool, float]:
    """执行 TOKEN_BUCKET_LUA，返回 (是否放行, 剩余令牌数)。已经写好，不用改。"""
    if TOKEN_BUCKET_LUA is None:
        raise NotImplementedError("TODO: 练习 (c) —— 写出 TOKEN_BUCKET_LUA")
    allowed, left = client.eval(TOKEN_BUCKET_LUA, 1, key, rate, capacity, requested)
    return bool(allowed), float(left.decode() if isinstance(left, bytes) else left)
