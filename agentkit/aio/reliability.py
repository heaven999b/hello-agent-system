"""异步版的重试、熔断、降级与舱壁。语义与 agentkit.reliability 一致，额外处理两件异步特有的事：

1. 取消必须穿透：asyncio.CancelledError 不是"失败"，不能被重试、不能计入熔断，要原样往外抛；
2. 流式输出只能在"第一个 token 之前"重试或降级：已经推给用户的半句话收不回来，
   中途失败只能如实报错，由上层决定怎么处理。
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Awaitable, Callable, Sequence, TypeVar

from ..llm import LLMError
from ..reliability import CircuitBreaker, CircuitOpenError, backoff_delay, is_retryable
from ..types import LLMResponse, Message
from .llm import StreamDone, StreamEvent

T = TypeVar("T")


async def aretry_call(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_if: Callable[[Exception], bool] = is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as e:  # CancelledError 是 BaseException，不会被这里捕获 —— 取消直接穿透
            if attempt == max_attempts or not retry_if(e):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            hint = getattr(e, "retry_after", None)
            if hint is not None:
                delay = max(delay, min(hint, max_delay * 4))
            if on_retry:
                on_retry(attempt, e, delay)
            await sleep(delay)
    raise AssertionError("unreachable")


class AsyncCircuitBreaker(CircuitBreaker):
    """状态机与同步版完全相同，只是被保护的调用是协程。"""

    _probing = False

    async def acall(self, fn: Callable[[], Awaitable[T]]) -> T:
        state = self.state
        if state == "open":
            raise CircuitOpenError(self.name)
        probe = state == "half_open"
        if probe:
            # 半开：只放行**一个**试探请求，其余并发请求继续快速失败。
            # 否则下游刚恢复一点，几百个并发请求一拥而上，又把它压垮（第 08 课练习 c 的异步版）
            if self._probing:
                raise CircuitOpenError(self.name)
            self._probing = True
        try:
            result = await fn()
        except Exception as e:
            if self.record_if is not None and not self.record_if(e):
                raise
            self.failures += 1
            if probe or self.state == "half_open" or self.failures >= self.failure_threshold:
                self.opened_at = self.clock()
            raise
        finally:
            if probe:
                self._probing = False
        self.failures = 0
        self.opened_at = None
        return result


class AsyncResilientLLM:
    """重试 → 熔断 → 降级，外加每个模型的并发上限（舱壁）。对外仍是一个 AsyncLLM。

    max_concurrency：同一时刻对单个模型的最大在途请求数。超出的请求在本进程内排队，
    而不是一起打到网关上触发 429 —— 这是"背压"最朴素的形式。
    """

    def __init__(
        self,
        primary,
        fallbacks: Sequence = (),
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
                AsyncCircuitBreaker(llm.model, failure_threshold, reset_timeout, clock, record_if=record_if),
                asyncio.Semaphore(max_concurrency) if max_concurrency else None,
            )
            for llm in [primary, *fallbacks]
        ]
        self.model = primary.model
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self.events: list[str] = []

    def _on_retry(self, model: str):
        return lambda n, e, d: self.events.append(f"retry {model} #{n} after {d:.2f}s: {e}")

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        errors: list[str] = []
        transient: list[bool] = []
        for llm, breaker, sem in self.chain:
            async def attempt(llm=llm):
                return await aretry_call(
                    lambda: llm.chat(messages, tools, **kwargs),
                    max_attempts=self.max_attempts,
                    base_delay=self.base_delay,
                    sleep=self.sleep,
                    on_retry=self._on_retry(llm.model),
                )

            try:
                if sem is None:
                    return await breaker.acall(attempt)
                async with sem:
                    return await breaker.acall(attempt)
            except LLMError as e:
                errors.append(f"{llm.model}: {e}")
                transient.append(e.retryable or isinstance(e, CircuitOpenError))
                self.events.append(f"fallback from {llm.model}: {e}")
        # 与同步版一致：全部是暂时性故障时保留 retryable=True，交给外层重试决定
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
                    breaker.failures, breaker.opened_at = 0, None
                    return
                except LLMError as e:
                    if started:
                        raise  # 已经把部分内容推给了用户：不能静默重试，否则用户会看到重复的文字
                    breaker.failures += 1
                    if breaker.state == "half_open" or breaker.failures >= breaker.failure_threshold:
                        breaker.opened_at = breaker.clock()
                    if attempt < self.max_attempts and is_retryable(e) and breaker.state != "open":
                        delay = backoff_delay(attempt, self.base_delay)
                        self.events.append(f"retry(stream) {llm.model} #{attempt} after {delay:.2f}s: {e}")
                        await self.sleep(delay)
                        continue
                    errors.append(f"{llm.model}: {e}")
                    self.events.append(f"fallback(stream) from {llm.model}: {e}")
                    break
        raise LLMError("所有模型都失败了 → " + " | ".join(errors), retryable=False)
