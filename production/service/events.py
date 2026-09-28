"""运行进度事件：worker / API 写入 Redis Streams，API 的 SSE 端点读出来推给浏览器。

为什么是 Redis Streams（三个候选的比较见讲义问题卡片 4 前的 2.4 节）：

| 候选 | 客户端晚到 / 断线重连 | 代价 |
|---|---|---|
| Redis Pub/Sub | 发布时没人订阅，事件就丢了（at-most-once） | 最简单 |
| Postgres LISTEN/NOTIFY | 同样只投递给"正在 LISTEN 的会话"；每个监听者占一个专用连接，PgBouncer 事务池模式下不能用；payload 默认上限 8000 字节 | 不用多一个组件 |
| **Redis Streams** | 事件按 ID 持久保存（MAXLEN 截断 + 过期），SSE 的 Last-Event-ID 直接当作 XREAD 的起点 → **断线重连不丢事件** | 要设上限和 TTL，否则内存一直涨 |

SSE 的 id 字段就是 Stream 的条目 ID（形如 1790535593173-0），浏览器 EventSource 重连时自动带上 Last-Event-ID。
fakeredis 的 TCP 服务支持 XADD / XREAD BLOCK（本课实测），所以本机也能端到端验证。

事件是**尽力而为**的：Redis 暂时不可用时只记日志，不能让一次运行因为"进度推送失败"而失败。
运行的真实状态永远以 Postgres 检查点和任务表为准（GET /v1/runs/{id}）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

# 取消安全的 wait_for：Python 3.12 之前 asyncio.wait_for 在"结果和外部取消同时到达"时会吞掉取消（CPython gh-86296）
from agentkit import wait_for

log = logging.getLogger("itdesk.events")

# 客户端据此结束 SSE：运行已经到达终态，或者需要人来做下一步（审批）
FINAL_EVENTS = frozenset({"completed", "failed"})
PAUSE_EVENTS = frozenset({"awaiting_approval"})


class EventBus:
    def __init__(self, redis, blocking_redis=None, *, maxlen: int = 500, ttl_s: int = 86400, prefix: str = "itdesk:events"):
        self.r = redis
        self.rb = blocking_redis or redis  # XREAD BLOCK 会占住一个连接：用单独的客户端，别把限流、幂等的连接饿死
        self.maxlen, self.ttl_s, self.prefix = maxlen, ttl_s, prefix

    def key(self, run_id: str) -> str:
        return f"{self.prefix}:{{{run_id}}}"  # hash tag：Redis Cluster 下同一个 run 的事件落在同一个 slot

    async def publish(self, run_id: str, event: str, timeout: float = 1.0, **data: Any) -> str | None:
        fields = {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str), "ts": f"{time.time():.3f}"}
        try:
            async with self.r.pipeline(transaction=False) as pipe:
                pipe.xadd(self.key(run_id), fields, maxlen=self.maxlen, approximate=True)
                pipe.expire(self.key(run_id), self.ttl_s)
                entry_id, _ = await wait_for(pipe.execute(), timeout)
            return entry_id.decode() if isinstance(entry_id, bytes) else entry_id
        except Exception as e:  # noqa: BLE001 —— 进度推送失败不能拖垮运行本身
            log.warning("publish %s for %s failed: %s", event, run_id, e)
            return None

    async def read(self, run_id: str, after: str = "0-0", block_ms: int = 5000, count: int = 100) -> list[tuple[str, str, dict]]:
        """读 after 之后的事件（不含 after）。没有新事件时最多阻塞 block_ms 毫秒，返回空列表。"""
        res = await self.rb.xread({self.key(run_id): after}, block=block_ms, count=count)
        return [_decode(entry_id, fields) for _stream, entries in (res or []) for entry_id, fields in entries]

    async def history(self, run_id: str) -> list[tuple[str, str, dict]]:
        return [_decode(i, f) for i, f in await self.r.xrange(self.key(run_id), "-", "+")]


def _text(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _decode(entry_id, fields: dict) -> tuple[str, str, dict]:
    f = {_text(k): _text(v) for k, v in fields.items()}
    return _text(entry_id), f.get("event", "message"), json.loads(f.get("data") or "{}")


class TaskSet:
    """同步回调里要发异步事件（run_worker 的 on_event 是同步函数）：创建任务并持有引用，停机时统一等完。"""

    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float = 5.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)
