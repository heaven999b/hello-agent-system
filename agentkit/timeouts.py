"""取消安全的 wait_for：超时和外部取消都要以"停下来"为准。

Python 3.12 之前的 `asyncio.wait_for` 有一个已知竞态（CPython gh-86296）：
被等待的操作刚好完成、外部取消又在同一轮事件循环里到达时，wait_for 会返回结果、把取消吞掉。
对 Agent 服务来说，这意味着"用户在工具刚执行完的那一刻断开，运行却没停，接着调模型、接着花钱"
（第 30 课 R3，实测在 3.11.7 上可以稳定复现，3.12+ 已修复）。

本模块的 `wait_for` 在各版本上语义一致：
- 3.11+：用 `asyncio.timeout()`。它在当前任务里内联等待，靠 cancel/uncancel 计数区分
  "自己的超时"和"外部取消"，外部取消一定会传出去（3.12 的 wait_for 本身就是这样实现的）。
- 3.10：没有 `asyncio.timeout()`，用 `asyncio.wait` 自己实现：被等待的操作放进独立任务，
  只要外部被取消，就取消内部任务、等它收尾，再把取消原样抛出。

取消优先的代价：如果内部操作其实已经完成，它的结果会被丢弃。对"拿到就要归还"的资源
（例如信号量），用 `on_discard` 归还，否则会泄漏。
"""

from __future__ import annotations

import asyncio
import functools
import sys
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

_HAS_TIMEOUT_CM = sys.version_info >= (3, 11)


async def wait_for(aw: Awaitable[T], timeout: float | None, *, on_discard: Callable[[Any], None] | None = None) -> T:
    """等待 aw，最多 timeout 秒（None 表示不限时）。超时抛 asyncio.TimeoutError；外部取消一定抛 CancelledError。"""
    if timeout is None:
        return await aw
    if _HAS_TIMEOUT_CM:
        return await _wait_for_timeout_cm(aw, timeout)
    return await _wait_for_via_wait(aw, timeout, on_discard)


async def _wait_for_timeout_cm(aw: Awaitable[T], timeout: float) -> T:
    async with asyncio.timeout(timeout):  # type: ignore[attr-defined]  # 3.11+
        return await aw


async def _wait_for_via_wait(aw: Awaitable[T], timeout: float, on_discard: Callable[[Any], None] | None) -> T:
    inner = asyncio.ensure_future(aw)
    try:
        done, _ = await asyncio.wait({inner}, timeout=timeout)
    except asyncio.CancelledError:
        # 外部取消优先：哪怕 inner 在同一轮里已经完成，也不返回它的结果
        await _cancel_and_settle(inner, on_discard)
        raise
    if done:
        return inner.result()
    await _cancel_and_settle(inner, on_discard)
    raise asyncio.TimeoutError


async def _cancel_and_settle(inner: asyncio.Future, on_discard: Callable[[Any], None] | None) -> None:
    inner.cancel()
    try:
        await asyncio.wait({inner})  # 等内部任务真正结束（它的 finally 跑完），不留悬空任务
    finally:
        if inner.done():
            _settle(inner, on_discard)
        else:  # 等待期间又被取消：交给回调收尾
            inner.add_done_callback(functools.partial(_settle, on_discard=on_discard))


def _settle(inner: asyncio.Future, on_discard: Callable[[Any], None] | None) -> None:
    if inner.cancelled():
        return
    exc = inner.exception()  # 取走异常，避免 "Task exception was never retrieved"
    if exc is None and on_discard is not None:
        on_discard(inner.result())
