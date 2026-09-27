"""Redis 适配器：幂等存储、Lua 令牌桶限流、带 fencing token 的锁（第 26 课）。

    同步版                    异步版（redis.asyncio，给 agentkit.aio.AsyncAgent）
    RedisIdempotencyStore     AsyncRedisIdempotencyStore   IdempotencyStore 的 get / put，外加 claim / release
    RedisTokenBucket          AsyncRedisTokenBucket        Lua 脚本原子实现的令牌桶，时钟取 Redis 服务器的 TIME
    RateLimitHook             AsyncRateLimitHook           Hook：调用模型前按租户拿令牌，等不到就 StopRun("rate_limited")
    RedisLock                                              SET NX PX + INCR 发放 fencing token；Lua "比较后删除"

定位：Redis 适合放"丢了能重建、或者短命"的协调数据（限流计数、幂等缓存、短锁）。
**不要**把它当成正确性的唯一保证：复制是异步的，主从切换可能丢掉已经确认的写入。
真正不能错的东西（工单不能重复、检查点不能被旧 worker 覆盖）要落到有事务的地方（下游唯一约束、Postgres CAS）。

依赖：redis-py（pip install -e ".[redis]"）。client_or_url 可以是客户端实例或 redis:// URL
（同步版用 redis.Redis，异步版用 redis.asyncio.Redis）。
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
import uuid
from dataclasses import asdict
from typing import Any, Callable

from ..hooks import Hook, StopRun
from ..tools import IdempotencyStore, ToolResult
from . import require

redis = require("redis", "redis")

__all__ = [
    "AsyncRateLimitHook",
    "AsyncRedisIdempotencyStore",
    "AsyncRedisTokenBucket",
    "LockNotAcquired",
    "RateLimitHook",
    "RedisIdempotencyStore",
    "RedisLock",
    "RedisTokenBucket",
    "TOKEN_BUCKET_LUA",
]


def _client(client_or_url: Any):
    return redis.Redis.from_url(client_or_url) if isinstance(client_or_url, str) else client_or_url


def _aclient(client_or_url: Any):
    import redis.asyncio as aredis

    return aredis.Redis.from_url(client_or_url) if isinstance(client_or_url, str) else client_or_url


def _register(client, lua: str):
    """注册脚本并立即 SCRIPT LOAD 预热脚本缓存：第一次调用就是 EVALSHA 命中，少一次往返。
    （实测 fakeredis 在很多线程同时走"EVALSHA 未命中 → SCRIPT LOAD"路径时会断开连接，预热也顺便绕开了它。
    Redis 重启 / 主从切换后缓存会清空，那时 redis-py 会自动回退到 SCRIPT LOAD。）"""
    script = client.register_script(lua)
    client.script_load(lua)
    return script


class _AsyncWarmup:
    """异步客户端不能在构造函数里 await：第一次使用时再预热脚本缓存（加锁，只做一次）。"""

    def __init__(self, client, *luas: str):
        self.r, self.luas, self.done = client, luas, False
        self._lock = asyncio.Lock()

    async def __call__(self) -> None:
        if self.done:
            return
        async with self._lock:
            if not self.done:
                for lua in self.luas:
                    await self.r.script_load(lua)
                self.done = True


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


# =====================================================================================
# 幂等存储
# =====================================================================================

_CLAIM_LUA = """
-- KEYS[1] = 结果 key，KEYS[2] = "执行中"标记；ARGV[1] = 持有者 token，ARGV[2] = 标记的存活毫秒数
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0                                   -- 已经有结果了：不需要再执行
end
if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return 1                                   -- 抢到了"执行权"
end
return 0                                     -- 别人正在执行
"""

_COMPARE_AND_DELETE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _IdemKeys:
    namespace: str

    def _k(self, key: str) -> str:
        return f"{self.namespace}:{{{key}}}"  # hash tag：结果和"执行中"标记在 Redis Cluster 里落在同一个 slot

    def _inflight(self, key: str) -> str:
        return self._k(key) + ":inflight"

    @staticmethod
    def _dump(result: ToolResult) -> str:
        return json.dumps(asdict(result), ensure_ascii=False)

    @staticmethod
    def _load(raw) -> ToolResult | None:
        return None if raw is None else ToolResult(**json.loads(raw))


class RedisIdempotencyStore(_IdemKeys, IdempotencyStore):
    """幂等存储的 Redis 版：所有 worker 共享、带过期时间。直接传给 Agent(idempotency_store=...)。

    get / put（agentkit 的 ToolRegistry 自动调用）：写工具**成功之后**记下结果，同一个幂等键再来时直接返回。
    它挡住的是"**先后**重放"：崩溃恢复、重试时，已经成功过的调用不再执行。

    claim / release（需要你在工具里显式调用）：执行**之前**用 SET NX 占一个"执行中"标记。
    它挡住的是"**同时**执行"：僵尸 worker 和接手的 worker 同时跑到同一个工具调用时，get 都返回 None，
    两个都会执行 —— claim 保证只有一个能进去。

    两者都挡不住的缝：副作用做完了、put 之前进程死了 → 结果没记下，标记过期后重放还会再执行一次。
    所以幂等**最终必须下沉到下游系统**：把幂等键传给下游，由它在"执行副作用"的同一个事务里用唯一约束去重
    （第 08 课的幂等键、第 13 课的 TicketSystem）。Redis 这一层是省钱、省时间的缓存，不是正确性的保证。
    """

    def __init__(self, client_or_url: Any, namespace: str = "idem", ttl_seconds: int = 86400):
        super().__init__()
        self.r = _client(client_or_url)
        self.namespace = namespace
        self.ttl_seconds = int(ttl_seconds)
        self._claim = _register(self.r, _CLAIM_LUA)
        self._cad = _register(self.r, _COMPARE_AND_DELETE_LUA)
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> ToolResult | None:
        return self._load(self.r.get(self._k(key)))

    def put(self, key: str, result: ToolResult) -> None:
        pipe = self.r.pipeline(transaction=True)  # MULTI/EXEC：写结果和清标记要么都做、要么都不做
        pipe.set(self._k(key), self._dump(result), ex=self.ttl_seconds)
        pipe.delete(self._inflight(key))
        pipe.execute()
        with self._lock:
            self._tokens.pop(key, None)

    def claim(self, key: str, ttl_seconds: float = 60) -> bool:
        """占用"执行权"：True = 你来执行；False = 已有结果（请 get）或别人正在执行。

        ttl_seconds 要大于工具的最长执行时间，否则标记过期后别人会以为没人在做。
        """
        token = uuid.uuid4().hex
        ok = self._claim(keys=[self._k(key), self._inflight(key)], args=[token, int(ttl_seconds * 1000)])
        if ok:
            with self._lock:
                self._tokens[key] = token
        return bool(ok)

    def release(self, key: str) -> bool:
        """执行失败时释放执行权（只删除自己的标记），让重试不必等标记过期。"""
        with self._lock:
            token = self._tokens.pop(key, None)
        return token is not None and bool(self._cad(keys=[self._inflight(key)], args=[token]))

    def in_flight(self, key: str) -> bool:
        return bool(self.r.exists(self._inflight(key)))


class AsyncRedisIdempotencyStore(_IdemKeys):
    """RedisIdempotencyStore 的异步版（async get / put / claim / release），给 AsyncAgent 用。语义完全一致。"""

    def __init__(self, client_or_url: Any, namespace: str = "idem", ttl_seconds: int = 86400):
        self.r = _aclient(client_or_url)
        self.namespace = namespace
        self.ttl_seconds = int(ttl_seconds)
        self._claim = self.r.register_script(_CLAIM_LUA)
        self._cad = self.r.register_script(_COMPARE_AND_DELETE_LUA)
        self._warmup = _AsyncWarmup(self.r, _CLAIM_LUA, _COMPARE_AND_DELETE_LUA)
        self._tokens: dict[str, str] = {}

    async def get(self, key: str) -> ToolResult | None:
        return self._load(await self.r.get(self._k(key)))

    async def put(self, key: str, result: ToolResult) -> None:
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(self._k(key), self._dump(result), ex=self.ttl_seconds)
            pipe.delete(self._inflight(key))
            await pipe.execute()
        self._tokens.pop(key, None)

    async def claim(self, key: str, ttl_seconds: float = 60) -> bool:
        await self._warmup()
        token = uuid.uuid4().hex
        ok = await self._claim(keys=[self._k(key), self._inflight(key)], args=[token, int(ttl_seconds * 1000)])
        if ok:
            self._tokens[key] = token
        return bool(ok)

    async def release(self, key: str) -> bool:
        token = self._tokens.pop(key, None)
        if token is None:
            return False
        await self._warmup()
        return bool(await self._cad(keys=[self._inflight(key)], args=[token]))

    async def in_flight(self, key: str) -> bool:
        return bool(await self.r.exists(self._inflight(key)))


# =====================================================================================
# 令牌桶
# =====================================================================================

TOKEN_BUCKET_LUA = """
-- KEYS[1] = 桶（hash：tokens, ts）；ARGV = 速率(个/秒), 容量, 本次要拿的令牌数
-- 返回 {是否放行 0/1, 还要等几秒(字符串), 剩余令牌(字符串)}
-- 注意：Lua 的小数返回给 Redis 时会被截断成整数，所以小数一律用 tostring 转成字符串返回
local rate      = tonumber(ARGV[1])
local capacity  = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

-- 时钟取 Redis 服务器的 TIME：所有 worker 共用同一个时钟，不受各机器的时钟漂移影响
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state  = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])
if tokens == nil or ts == nil then          -- 新桶（或已过期被删除）：满的
  tokens = capacity
  ts = now
end
if now > ts then                            -- 惰性补充；时钟倒退时不补、也不把 ts 往回拨（第 12 课）
  tokens = math.min(capacity, tokens + (now - ts) * rate)
  ts = now
end

local allowed = 0
local wait = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
elseif rate > 0 then
  wait = (requested - tokens) / rate
else
  wait = -1                                 -- 速率为 0：永远等不到
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts)
-- 空闲到桶被补满之后，这个 key 就没有保存的必要了（删掉 = 满桶，语义不变）
if rate > 0 then
  redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
end
return {allowed, tostring(wait), tostring(tokens)}
"""


class _BucketBase:
    def __init__(self, rate_per_sec: float, capacity: float, prefix: str, overrides):
        if capacity <= 0 or rate_per_sec < 0:
            raise ValueError("capacity 必须 > 0，rate_per_sec 不能为负")
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self.prefix = prefix
        self.overrides = dict(overrides or {})

    def limits(self, key: str) -> tuple[float, float]:
        """某个 key 的 (速率, 容量)：按套餐覆盖，没有覆盖就用默认值。"""
        return self.overrides.get(key, (self.rate, self.capacity))

    def _args(self, key: str, tokens: float) -> tuple[list, list]:
        rate, capacity = self.limits(key)
        if tokens > capacity:
            raise ValueError(f"一次要 {tokens} 个令牌，超过了桶容量 {capacity}：永远不可能放行")
        return [f"{self.prefix}:{key}"], [rate, capacity, tokens]

    @staticmethod
    def _parse(reply) -> tuple[bool, float, float]:
        allowed, wait, left = reply
        return bool(allowed), float(_text(wait)), float(_text(left))

    @staticmethod
    def _sleep_for(ok: bool, wait: float, deadline: float | None) -> float | None:
        """None = 不用再等了（成功，或者截止前肯定等不到）；否则返回这次要睡多久。"""
        if ok or wait < 0:
            return None
        if deadline is not None and wait > deadline - time.monotonic():
            return None  # 按当前速率，截止前肯定等不到：立刻失败，别白等
        # 稍微多睡一点并加抖动：同时在等的 worker 不会在同一瞬间一起醒来抢同一个令牌
        return wait * random.uniform(1.0, 1.2) + 0.001


class RedisTokenBucket(_BucketBase):
    """分布式令牌桶：所有 worker 共享同一个桶，按 key（通常是租户）隔离。

    为什么必须用 Lua？"读令牌数 → 算补充 → 判断 → 写回"是一个读-改-写序列。
    放在客户端做，两个 worker 可能同时读到"还剩 1 个"，都放行 —— 限流被击穿（和第 13 课丢失更新是同一个问题）。
    Lua 脚本在 Redis 里原子执行，执行期间不会插入别的命令。

    为什么时钟取 Redis 的 TIME？补充量 = 流逝时间 × 速率。如果用各 worker 的本机时间，
    时钟快的机器会"凭空"补出令牌，时钟慢的会把 ts 拨回去。以 Redis 服务器为唯一时钟，问题消失。

    overrides：按 key 覆盖 (速率, 容量)，例如按套餐给不同租户不同的配额。
    """

    def __init__(self, client: Any, rate_per_sec: float, capacity: float, prefix: str = "tb", *,
                 overrides: dict[str, tuple[float, float]] | None = None):
        super().__init__(rate_per_sec, capacity, prefix, overrides)
        self.r = _client(client)
        self._script = _register(self.r, TOKEN_BUCKET_LUA)

    def take(self, key: str, tokens: float = 1) -> tuple[bool, float, float]:
        """原子地尝试拿 tokens 个令牌，返回 (是否放行, 还需等待的秒数, 剩余令牌数)。"""
        keys, args = self._args(key, tokens)
        return self._parse(self._script(keys=keys, args=args))

    def try_acquire(self, key: str, tokens: float = 1) -> bool:
        return self.take(key, tokens)[0]

    def acquire(self, key: str, tokens: float = 1, timeout: float | None = None) -> bool:
        """拿不到就等（按脚本算出的等待时间睡眠，而不是忙轮询）。timeout=None 表示一直等；等不到返回 False。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            ok, wait, _ = self.take(key, tokens)
            pause = self._sleep_for(ok, wait, deadline)
            if pause is None:
                return ok
            time.sleep(pause)


class AsyncRedisTokenBucket(_BucketBase):
    """RedisTokenBucket 的异步版：等待令牌用 asyncio.sleep，**不阻塞事件循环**，同一进程里别的会话照常跑。"""

    def __init__(self, client: Any, rate_per_sec: float, capacity: float, prefix: str = "tb", *,
                 overrides: dict[str, tuple[float, float]] | None = None):
        super().__init__(rate_per_sec, capacity, prefix, overrides)
        self.r = _aclient(client)
        self._script = self.r.register_script(TOKEN_BUCKET_LUA)
        self._warmup = _AsyncWarmup(self.r, TOKEN_BUCKET_LUA)

    async def take(self, key: str, tokens: float = 1) -> tuple[bool, float, float]:
        keys, args = self._args(key, tokens)
        await self._warmup()
        return self._parse(await self._script(keys=keys, args=args))

    async def try_acquire(self, key: str, tokens: float = 1) -> bool:
        return (await self.take(key, tokens))[0]

    async def acquire(self, key: str, tokens: float = 1, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            ok, wait, _ = await self.take(key, tokens)
            pause = self._sleep_for(ok, wait, deadline)
            if pause is None:
                return ok
            await asyncio.sleep(pause)


class _RateLimitBase(Hook):
    def __init__(self, bucket, key_fn, tokens_fn, wait_timeout: float):
        self.bucket = bucket
        self.key_fn = key_fn
        self.tokens_fn = tokens_fn
        self.wait_timeout = wait_timeout
        self.stats: dict[str, dict[str, float]] = {}  # 本进程内的统计：key -> {calls, waited_s, rejected}
        self._lock = threading.Lock()

    def _record(self, key: str, ok: bool, waited: float) -> None:
        with self._lock:
            s = self.stats.setdefault(key, {"calls": 0, "waited_s": 0.0, "rejected": 0})
            s["waited_s"] += waited
            s["calls" if ok else "rejected"] += 1
        if not ok:
            raise StopRun("rate_limited", f"租户 {key} 的模型调用配额暂时用完（已等待 {waited:.1f} 秒），请稍后重试。")


class RateLimitHook(_RateLimitBase):
    """每次调用模型前，按 key（默认租户）从全局令牌桶拿令牌；等 wait_timeout 秒还拿不到就 StopRun("rate_limited")。

    tokens_fn(state, messages) 决定这次调用消耗多少令牌：默认 1（限 RPM）；
    按 token 限流（TPM）可以用 lambda s, m: agentkit.context.estimate_tokens(m)。

    为什么等不到要停下来而不是一直等？worker 在这里睡着，就占着一个 worker 槽位（队头阻塞）：
    别的租户的任务明明有配额，也得排在后面。配合 AgentJobHandler，rate_limited 会变成 RetryLater，
    任务回到队列、过一会儿再来，把 worker 让给别人。
    """

    def __init__(
        self,
        bucket: RedisTokenBucket,
        key_fn: Callable[[Any], str | None] = lambda state: state.metadata.get("tenant_id"),
        tokens_fn: Callable[[Any, list], float] = lambda state, messages: 1,
        wait_timeout: float = 5.0,
    ):
        if isinstance(bucket, AsyncRedisTokenBucket):
            raise TypeError("异步令牌桶请配合 AsyncRateLimitHook 使用")
        super().__init__(bucket, key_fn, tokens_fn, wait_timeout)

    def before_llm(self, state, messages) -> None:
        key = self.key_fn(state) or "anonymous"
        t0 = time.monotonic()
        ok = self.bucket.acquire(key, self.tokens_fn(state, messages), timeout=self.wait_timeout)
        self._record(key, ok, time.monotonic() - t0)


class AsyncRateLimitHook(_RateLimitBase):
    """RateLimitHook 的异步版：before_llm 是 async，等令牌时让出事件循环（给 AsyncAgent 用）。

    在异步 worker 里用同步的 RateLimitHook 会怎样？它的 time.sleep 会把整个事件循环卡住：
    同一进程里其他几十个会话、所有任务的续租协程全部跟着停 —— 一个租户被限流，整个进程一起"被限流"。
    """

    def __init__(
        self,
        bucket: AsyncRedisTokenBucket,
        key_fn: Callable[[Any], str | None] = lambda state: state.metadata.get("tenant_id"),
        tokens_fn: Callable[[Any, list], float] = lambda state, messages: 1,
        wait_timeout: float = 5.0,
    ):
        if not isinstance(bucket, AsyncRedisTokenBucket):
            raise TypeError("AsyncRateLimitHook 需要 AsyncRedisTokenBucket")
        super().__init__(bucket, key_fn, tokens_fn, wait_timeout)

    async def before_llm(self, state, messages) -> None:
        key = self.key_fn(state) or "anonymous"
        t0 = time.monotonic()
        ok = await self.bucket.acquire(key, self.tokens_fn(state, messages), timeout=self.wait_timeout)
        self._record(key, ok, time.monotonic() - t0)


# =====================================================================================
# 锁
# =====================================================================================

_ACQUIRE_LUA = """
-- KEYS[1] = 锁，KEYS[2] = fencing 计数器（永不过期）；ARGV[1] = 持有者 token，ARGV[2] = 租期毫秒
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return redis.call('INCR', KEYS[2])        -- 拿到锁的同时拿到一个更大的 fencing token（同一个原子操作里）
end
return false
"""

_EXTEND_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class LockNotAcquired(RuntimeError):
    pass


class RedisLock:
    """带 fencing token 的 Redis 锁（单实例）。

        acquire()  SET key token NX PX ttl 成功时，同一个 Lua 脚本里 INCR 计数器 → 返回 fencing token
        release()  Lua "比较后删除"：只有 value 还是自己的 token 才删（锁过期后别人拿到了，你不能把别人的锁删掉）
        extend()   同样是比较后续期

    **锁只能提高效率，正确性要靠 fencing token**：持有者可能在拿到锁之后停顿（GC、虚拟机暂停、网络延迟），
    醒来时锁早已过期、被别人拿走，它自己却不知道（第 13 课的时间线）。所以被保护的存储必须在写入时检查
    token，拒绝比见过的更小的 token（见 README 的 Postgres 示例）。

    Kleppmann 对 Redlock 的批评（第 13 课必读）：Redlock 依赖"时钟和停顿都有上界"的时序假设，
    而且不产生 fencing token。本类的 token 来自 Redis INCR —— 它和 Redis 一样"不够硬"：
    异步复制下主从切换可能丢掉最近的 INCR，token 可能被重复发放。需要严格正确性时，
    token 应该来自有共识 / 持久事务的系统（Postgres 序列、etcd revision、ZooKeeper zxid）。

    一个 RedisLock 实例代表一个持有者，不要在多个线程之间共享同一个实例。
    Redis Cluster：锁和计数器用 hash tag 放进同一个 slot（{name}）。
    """

    def __init__(self, client: Any, name: str, ttl_seconds: float, *, prefix: str = "lock"):
        self.r = _client(client)
        self.name = name
        self.ttl_ms = int(ttl_seconds * 1000)
        self.key = f"{prefix}:{{{name}}}"
        self.fence_key = f"{prefix}:{{{name}}}:fence"
        self._token: str | None = None
        self.fence: int | None = None
        self._acquire = _register(self.r, _ACQUIRE_LUA)
        self._release = _register(self.r, _COMPARE_AND_DELETE_LUA)
        self._extend = _register(self.r, _EXTEND_LUA)

    def acquire(self, blocking: bool = True, timeout: float | None = None, retry_interval: float = 0.05) -> int | None:
        """拿锁，成功返回 fencing token（单调递增的整数），失败返回 None。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        token = uuid.uuid4().hex
        while True:
            fence = self._acquire(keys=[self.key, self.fence_key], args=[token, self.ttl_ms])
            if fence is not None:
                self._token, self.fence = token, int(fence)
                return self.fence
            if not blocking or (deadline is not None and time.monotonic() >= deadline):
                return None
            time.sleep(retry_interval * random.uniform(0.5, 1.5))

    def release(self) -> bool:
        """释放锁。返回 False 说明锁已经不是你的了（过期后被别人拿走）—— 你在锁外面做了事，要当心。"""
        token, self._token = self._token, None
        if token is None:
            return False
        return bool(self._release(keys=[self.key], args=[token]))

    def extend(self, ttl_seconds: float | None = None) -> bool:
        """续期（只对自己持有的锁有效）。返回 False = 锁已经丢了。"""
        if self._token is None:
            return False
        ms = self.ttl_ms if ttl_seconds is None else int(ttl_seconds * 1000)
        return bool(self._extend(keys=[self.key], args=[self._token, ms]))

    def owned(self) -> bool:
        """锁现在是否还归你（只是一个快照：返回 True 的下一刻就可能过期，所以写入仍要带 fence）。"""
        return self._token is not None and _text(self.r.get(self.key)) == self._token

    def __enter__(self) -> int:
        fence = self.acquire()
        if fence is None:  # pragma: no cover - blocking=True 且没有超时时不会发生
            raise LockNotAcquired(self.name)
        return fence

    def __exit__(self, *exc) -> None:
        self.release()
