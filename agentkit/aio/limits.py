"""异步并发控制：舱壁（bulkhead）与令牌桶。

一个进程里同时跑几百个会话时，最常见的事故是"一个租户把所有资源吃光"：
他一口气发了 500 个请求，其他租户的请求全在后面排队。两种控制手段：

- KeyedLimiter：按 key（通常是租户）限制**同时在跑**的运行数，再加一个全局上限保护下游（网关、连接池）。
  舱壁的意思是：船舱之间有隔板，一个舱进水，整艘船不会沉。
- AsyncTokenBucket：按 key 限制**速率**（每秒多少次），等待令牌时让出事件循环，不阻塞其他会话。

这些都是单进程内的控制；多实例部署时，全局配额要放到 Redis 或网关层（第 26、29 课）。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable


class LimitExceeded(Exception):
    """在等待时间内没拿到并发槽位或令牌。"""


class KeyedLimiter:
    def __init__(self, per_key: int, *, global_limit: int | None = None, overrides: dict[str, int] | None = None):
        if per_key < 1:
            raise ValueError("per_key 至少为 1")
        self.per_key = per_key
        self.overrides = dict(overrides or {})
        self._global = asyncio.Semaphore(global_limit) if global_limit else None
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._in_use: dict[str, int] = {}

    def limit_for(self, key: str) -> int:
        return self.overrides.get(key, self.per_key)

    def in_use(self, key: str) -> int:
        return self._in_use.get(key, 0)

    @asynccontextmanager
    async def slot(self, key: str | None, timeout: float | None = None) -> AsyncIterator[None]:
        """占用一个并发槽位；timeout 秒内拿不到就抛 LimitExceeded（服务端据此返回 429，而不是无限排队）。"""
        key = key or "_default"
        sem = self._sems.get(key)
        if sem is None:
            sem = self._sems[key] = asyncio.Semaphore(self.limit_for(key))
        deadline = None if timeout is None else time.monotonic() + timeout
        await _acquire(sem, timeout, key)
        try:
            if self._global is not None:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                await _acquire(self._global, remaining, "global")
        except BaseException:
            sem.release()
            raise
        self._in_use[key] = self._in_use.get(key, 0) + 1
        try:
            yield
        finally:
            self._in_use[key] -= 1
            if self._global is not None:
                self._global.release()
            sem.release()
            if self._in_use[key] == 0:
                # 空闲的 key 回收掉，避免租户很多时字典无限增长
                del self._in_use[key]
                if not sem.locked() and self._sems.get(key) is sem:
                    del self._sems[key]


async def _acquire(sem: asyncio.Semaphore, timeout: float | None, what: str) -> None:
    if timeout is None:
        await sem.acquire()
        return
    try:
        await asyncio.wait_for(sem.acquire(), timeout)
    except asyncio.TimeoutError:
        raise LimitExceeded(f"{what} 的并发槽位已满，等待 {timeout:.2f}s 后仍未获得") from None


class AsyncTokenBucket:
    """按 key 的令牌桶：rate 为每秒补充的令牌数，capacity 为桶容量（允许的突发量）。"""

    def __init__(self, rate: float, capacity: float, clock: Callable[[], float] = time.monotonic):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate 和 capacity 必须大于 0")
        self.rate, self.capacity, self.clock = rate, capacity, clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def _take(self, key: str, tokens: float) -> float:
        """尝试取令牌：成功返回 0，否则返回还需等待的秒数。
        事件循环是单线程的，这里中间没有 await，所以"补充 + 扣减"天然是原子的。"""
        now = self.clock()
        level, last = self._buckets.get(key, (self.capacity, now))
        level = min(self.capacity, level + (now - last) * self.rate)
        if level >= tokens:
            self._buckets[key] = (level - tokens, now)
            return 0.0
        self._buckets[key] = (level, now)
        return (tokens - level) / self.rate

    def try_acquire(self, key: str = "default", tokens: float = 1) -> bool:
        if tokens > self.capacity:
            raise ValueError("一次请求的令牌数不能超过桶容量")
        return self._take(key, tokens) == 0.0

    async def acquire(self, key: str = "default", tokens: float = 1, timeout: float | None = None) -> bool:
        """等到拿到令牌为止（等待期间让出事件循环）；超过 timeout 仍拿不到则返回 False。"""
        if tokens > self.capacity:
            raise ValueError("一次请求的令牌数不能超过桶容量")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = self._take(key, tokens)
            if wait == 0.0:
                return True
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(wait)
