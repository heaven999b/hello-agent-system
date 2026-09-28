"""可靠性：让 Agent 在一个"一切都会失败"的世界里稳定运行。

模型 API 会限流（429）、会超时、会 5xx、会整片宕机。企业级的四件套：
1. 重试 + 指数退避 + 抖动：只重试"可能成功"的错误（429/5xx/超时），不重试 400/401；
2. 熔断器：下游持续失败时快速失败，别让所有请求都排队等超时，也给下游喘息时间；
3. 降级：主模型不可用时切换备用模型（或更便宜/更小的模型、或缓存、或人工兜底）；
4. 预算：步数、token、金额、时长都要有上限（见 budget.py）。

ResilientLLM 用"装饰器模式"把这些能力套在任何 LLM 外面，对 Agent 完全透明。

异步带来的两条额外规则：
- 取消必须穿透：asyncio.CancelledError 不是"失败"，不能被重试、不能计入熔断，要原样往外抛
  （它是 BaseException，下面所有 `except Exception` 都不会捕获它）；
- 流式输出只能在"第一个 token 之前"重试或降级：已经推给用户的半句话收不回来，中途失败只能如实报错。

这里的熔断器状态在本进程内存里：多个 worker 进程各有一份。进程之间共享熔断状态
见 agentkit.distributed.SQLiteCircuitBreaker（单机多进程）与第 29 课的网关（多机）。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import AsyncIterator, Awaitable, Callable, Sequence, TypeVar

from .llm import LLM, LLMError, StreamDone, StreamEvent
from .types import LLMResponse, Message

T = TypeVar("T")


def is_retryable(e: Exception) -> bool:
    if isinstance(e, LLMError):
        return e.retryable
    return isinstance(e, (TimeoutError, asyncio.TimeoutError, ConnectionError))


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0, rng: random.Random | None = None) -> float:
    """第 attempt 次重试（从 1 开始）前的等待时间："全抖动"指数退避。

    指数：base * 2^(attempt-1)，封顶 cap；
    全抖动：在 [0, 上限] 内随机取值 —— 避免成千上万个客户端在同一时刻整齐地重试（惊群效应）。
    """
    upper = min(cap, base * (2 ** (attempt - 1)))
    return (rng or random).uniform(0, upper)


async def retry_call(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_if: Callable[[Exception], bool] = is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """fn 是一个"每次调用都返回新协程"的函数（例如 lambda: llm.chat(messages)）：协程只能 await 一次，重试要重新创建。"""
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as e:  # CancelledError 是 BaseException，不会被这里捕获 —— 取消直接穿透
            if attempt == max_attempts or not retry_if(e):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            # 服务端明确说了"请 N 秒后再试"（Retry-After）就听它的：比自己猜的退避更准
            server_hint = getattr(e, "retry_after", None)
            if server_hint is not None:
                delay = max(delay, min(server_hint, max_delay * 4))
            if on_retry:
                on_retry(attempt, e, delay)
            await sleep(delay)  # 等待期间让出事件循环：别的会话照常推进
    raise AssertionError("unreachable")


class CircuitOpenError(LLMError):
    def __init__(self, name: str):
        super().__init__(f"熔断器 [{name}] 处于打开状态，快速失败", retryable=False)


class CircuitBreaker:
    """三态熔断器。

        closed ──连续失败 N 次──► open ──等待 reset_timeout──► half_open
          ▲                                                    │
          └────────────── 试探请求成功 ◄──────────────────────┘（试探失败则回到 open）

    半开时只放行**一个**试探请求，其余并发请求继续快速失败：否则下游刚恢复一点，
    几百个并发请求一拥而上，又把它压垮。
    """

    def __init__(
        self,
        name: str = "llm",
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock=time.monotonic,
        record_if: Callable[[Exception], bool] | None = None,
    ):
        """record_if：哪些异常计入熔断。默认全部计入（简单）；生产中建议只统计反映"下游不健康"的错误
        （如 is_retryable：429/5xx/超时），否则一个请求自己的 400（比如上下文超长）也会把所有用户切到备用模型。"""
        self.record_if = record_if
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_timeout:
            return "half_open"
        return "open"

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.opened_at = self.clock()  # 打开（或重新打开）熔断器

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        state = self.state
        if state == "open":
            raise CircuitOpenError(self.name)
        probe = state == "half_open"
        if probe:
            if self._probing:  # 已经有一个试探请求在路上
                raise CircuitOpenError(self.name)
            self._probing = True
        try:
            result = await fn()
        except Exception as e:
            if self.record_if is not None and not self.record_if(e):
                raise  # 请求自身的问题，不代表下游不健康：不计入熔断
            self.record_failure()
            if probe:
                self.opened_at = self.clock()
            raise
        finally:
            if probe:
                self._probing = False
        self.record_success()
        return result


class ResilientLLM:
    """重试 → 熔断 → 降级，外加每个模型的并发上限（舱壁）。对外仍然是一个普通的 LLM。

    max_concurrency：同一时刻对单个模型的最大在途请求数。超出的请求在本进程内排队，
    而不是一起打到网关上触发 429 —— 这是"背压"最朴素的形式。
    """

    def __init__(
        self,
        primary: LLM,
        fallbacks: Sequence[LLM] = (),
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        record_if: Callable[[Exception], bool] | None = None,
        max_concurrency: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock=time.monotonic,
    ):
        self.chain = [
            (
                llm,
                CircuitBreaker(llm.model, failure_threshold, reset_timeout, clock, record_if=record_if),
                asyncio.Semaphore(max_concurrency) if max_concurrency else None,
            )
            for llm in [primary, *fallbacks]
        ]
        self.model = primary.model
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self.events: list[str] = []  # 记录重试/降级事件，方便观测

    def _on_retry(self, model: str):
        return lambda n, e, d: self.events.append(f"retry {model} #{n} after {d:.2f}s: {e}")

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        errors: list[str] = []
        transient: list[bool] = []
        for llm, breaker, sem in self.chain:
            async def attempt(llm=llm):
                return await retry_call(
                    lambda: llm.chat(messages, tools, **kwargs),
                    max_attempts=self.max_attempts,
                    base_delay=self.base_delay,
                    sleep=self.sleep,
                    on_retry=self._on_retry(llm.model),
                )

            try:
                if sem is None:
                    return await breaker.call(attempt)
                async with sem:
                    return await breaker.call(attempt)
            except LLMError as e:
                errors.append(f"{llm.model}: {e}")
                transient.append(e.retryable or isinstance(e, CircuitOpenError))
                self.events.append(f"fallback from {llm.model}: {e}")
        # 全部是暂时性故障（限流、5xx、熔断中）时保留 retryable=True：外层如果还有重试（例如 Temporal 的 RetryPolicy），
        # 它应该过一会儿再试；只要有一个是 400/401 这类永久错误，就明确告诉外层"别再试了"
        raise LLMError("所有模型都失败了 → " + " | ".join(errors), retryable=bool(transient) and all(transient))

    async def stream(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> AsyncIterator[StreamEvent]:
        errors = []
        for llm, breaker, sem in self.chain:
            if breaker.state == "open":
                errors.append(f"{llm.model}: 熔断中")
                continue
            for attempt in range(1, self.max_attempts + 1):
                started = False
                try:
                    if sem is not None:
                        await sem.acquire()
                    try:
                        source = llm.stream(messages, tools, **kwargs) if hasattr(llm, "stream") else None
                        if source is None:  # 模型不支持流式：退化为一次性返回
                            yield StreamDone(await llm.chat(messages, tools, **kwargs))
                            started = True
                        else:
                            async for event in source:
                                started = True
                                yield event
                    finally:
                        if sem is not None:
                            sem.release()
                    breaker.record_success()
                    return
                except LLMError as e:
                    if started:
                        raise  # 已经把部分内容推给了用户：不能静默重试，否则用户会看到重复的文字
                    breaker.record_failure()
                    if attempt < self.max_attempts and is_retryable(e) and breaker.state != "open":
                        delay = backoff_delay(attempt, self.base_delay)
                        self.events.append(f"retry(stream) {llm.model} #{attempt} after {delay:.2f}s: {e}")
                        await self.sleep(delay)
                        continue
                    errors.append(f"{llm.model}: {e}")
                    self.events.append(f"fallback(stream) from {llm.model}: {e}")
                    break
        raise LLMError("所有模型都失败了 → " + " | ".join(errors), retryable=False)
