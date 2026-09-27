"""可靠性：让 Agent 在一个"一切都会失败"的世界里稳定运行。

模型 API 会限流（429）、会超时、会 5xx、会整片宕机。企业级的四件套：
1. 重试 + 指数退避 + 抖动：只重试"可能成功"的错误（429/5xx/超时），不重试 400/401；
2. 熔断器：下游持续失败时快速失败，别让所有请求都排队等超时，也给下游喘息时间；
3. 降级：主模型不可用时切换备用模型（或更便宜/更小的模型、或缓存、或人工兜底）；
4. 预算：步数、token、金额、时长都要有上限（见 budget.py）。

ResilientLLM 用"装饰器模式"把这些能力套在任何 LLM 外面，对 Agent 完全透明。
"""

from __future__ import annotations

import random
import time
from typing import Callable, Sequence, TypeVar

from .llm import LLM, LLMError
from .types import LLMResponse, Message

T = TypeVar("T")


def is_retryable(e: Exception) -> bool:
    if isinstance(e, LLMError):
        return e.retryable
    return isinstance(e, (TimeoutError, ConnectionError))


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0, rng: random.Random | None = None) -> float:
    """第 attempt 次重试（从 1 开始）前的等待时间："全抖动"指数退避。

    指数：base * 2^(attempt-1)，封顶 cap；
    全抖动：在 [0, 上限] 内随机取值 —— 避免成千上万个客户端在同一时刻整齐地重试（惊群效应）。
    """
    upper = min(cap, base * (2 ** (attempt - 1)))
    return (rng or random).uniform(0, upper)


def retry_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_if: Callable[[Exception], bool] = is_retryable,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts or not retry_if(e):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            # 服务端明确说了"请 N 秒后再试"（Retry-After）就听它的：比自己猜的退避更准
            server_hint = getattr(e, "retry_after", None)
            if server_hint is not None:
                delay = max(delay, min(server_hint, max_delay * 4))
            if on_retry:
                on_retry(attempt, e, delay)
            sleep(delay)
    raise AssertionError("unreachable")


class CircuitOpenError(LLMError):
    def __init__(self, name: str):
        super().__init__(f"熔断器 [{name}] 处于打开状态，快速失败", retryable=False)


class CircuitBreaker:
    """三态熔断器。

        closed ──连续失败 N 次──► open ──等待 reset_timeout──► half_open
          ▲                                                    │
          └────────────── 试探请求成功 ◄──────────────────────┘（试探失败则回到 open）
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

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_timeout:
            return "half_open"
        return "open"

    def call(self, fn: Callable[[], T]) -> T:
        if self.state == "open":
            raise CircuitOpenError(self.name)
        try:
            result = fn()
        except Exception as e:
            if self.record_if is not None and not self.record_if(e):
                raise  # 请求自身的问题，不代表下游不健康：不计入熔断
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                self.opened_at = self.clock()  # 打开（或重新打开）熔断器
            raise
        self.failures = 0
        self.opened_at = None
        return result


class ResilientLLM:
    """重试 → 熔断 → 降级，对外仍然是一个普通的 LLM。"""

    def __init__(
        self,
        primary: LLM,
        fallbacks: Sequence[LLM] = (),
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock=time.monotonic,
    ):
        self.chain = [(llm, CircuitBreaker(llm.model, failure_threshold, reset_timeout, clock)) for llm in [primary, *fallbacks]]
        self.model = primary.model
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self.events: list[str] = []  # 记录重试/降级事件，方便观测

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        errors: list[str] = []
        transient: list[bool] = []
        for llm, breaker in self.chain:
            def attempt(llm=llm):
                return retry_call(
                    lambda: llm.chat(messages, tools, **kwargs),
                    max_attempts=self.max_attempts,
                    base_delay=self.base_delay,
                    sleep=self.sleep,
                    on_retry=lambda n, e, d, m=llm.model: self.events.append(f"retry {m} #{n} after {d:.2f}s: {e}"),
                )

            try:
                return breaker.call(attempt)
            except LLMError as e:
                errors.append(f"{llm.model}: {e}")
                transient.append(e.retryable or isinstance(e, CircuitOpenError))
                self.events.append(f"fallback from {llm.model}: {e}")
        # 全部是暂时性故障（限流、5xx、熔断中）时保留 retryable=True：外层如果还有重试（例如 Temporal 的 RetryPolicy），
        # 它应该过一会儿再试；只要有一个是 400/401 这类永久错误，就明确告诉外层"别再试了"
        raise LLMError("所有模型都失败了 → " + " | ".join(errors), retryable=bool(transient) and all(transient))
