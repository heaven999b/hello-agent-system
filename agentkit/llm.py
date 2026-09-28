"""LLM 抽象层（异步）。

企业级设计要点：业务代码只依赖一个很小的接口 `await llm.chat(messages, tools) -> LLMResponse`，
而不是直接依赖某家厂商的 SDK。好处：
- 换模型 / 换厂商 / 接模型网关，业务代码不用改；
- 可以用"装饰器"叠加能力：重试、熔断、降级、缓存、限流（见第 08 课 ResilientLLM）；
- 测试时换成 ScriptedLLM，零成本、100% 可复现。

为什么是 async？调一次模型要等 1–10 秒，这段时间 CPU 什么都不做。同步写法下，一个线程只能干等；
一个服务要同时接待几百个会话，就得开几百个线程（内存和上下文切换都扛不住）。
async 写法下，`await llm.chat(...)` 把控制权交还给事件循环，它去推进别的会话，模型回复到了再回来（第 02 课）。

本模块提供：
- LLM 协议：`async def chat(...)`，以及可选的流式接口 `stream(...)`；
- OpenAICompatLLM：基于 openai.AsyncOpenAI，带连接池上限；流式输出逐段产出 TextDelta，最后产出 StreamDone；
- ScriptedLLM：剧本模型，可以设置延迟，用来测试、压测和证明"并发真的发生了"。
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Protocol, Union

from .config import env
from .types import LLMResponse, Message, ToolCall, Usage, new_call_id


class LLM(Protocol):
    model: str

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse: ...


class LLMError(Exception):
    """统一的模型调用异常。retryable 标记这个错误"重试是否可能成功"（第 08 课）。"""

    def __init__(
        self, message: str, *, status_code: int | None = None, retryable: bool = False, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after  # 服务端通过 Retry-After 头建议的等待秒数（如果有）


# ------------------------------------------------------------------------------------ 流式事件


@dataclass
class TextDelta:
    """流式输出的一小段文本。"""

    text: str


@dataclass
class StreamDone:
    """流式输出结束：携带完整的 LLMResponse（文本、拼好的工具调用、用量）。"""

    response: LLMResponse


StreamEvent = Union[TextDelta, StreamDone]


class ToolCallAccumulator:
    """流式工具调用的参数是分片到达的：id 和 name 只在第一片出现，arguments 被切成很多片。
    按 index 拼接（第 01 课练习 accumulate_tool_call_deltas 的生产版），支持一轮里并行的多个调用。"""

    def __init__(self):
        self._slots: dict[int, dict] = {}

    def add(self, deltas) -> None:
        for d in deltas or []:
            index = getattr(d, "index", None) or 0
            slot = self._slots.setdefault(index, {"id": None, "name": None, "arguments": []})
            if getattr(d, "id", None):
                slot["id"] = d.id
            fn = getattr(d, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"].append(fn.arguments)

    def result(self) -> list[ToolCall]:
        return [
            ToolCall(id=s["id"] or new_call_id(), name=s["name"] or "", arguments="".join(s["arguments"]) or "{}")
            for _, s in sorted(self._slots.items())
        ]


# ------------------------------------------------------------------------------------ 真实模型


class OpenAICompatLLM:
    """任何 OpenAI 兼容接口（cliproxyapi / OpenAI / DeepSeek / Qwen / vLLM ...）的异步客户端。

    max_connections：到模型网关的 HTTP 连接池上限。它就是这个进程对网关的最大并发，
    应该和网关/厂商给你的并发配额匹配；超过上限的请求会在客户端排队，而不是把网关打出 429。
    max_retries=0：我们故意关掉 SDK 自带的重试，把重试放到自己可见、可控、可观测的 ResilientLLM 里（第 08 课）。
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        temperature: float | None = None,
        max_connections: int = 100,
    ):
        import httpx
        import openai

        self._openai = openai
        self.model = model or env("LLM_MODEL", "gpt-5.5")
        api_key = api_key or env("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("没有找到 LLM_API_KEY。请把 .env.example 复制为 .env 并填写，或设置环境变量。")
        self.temperature = temperature
        http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max(1, max_connections // 5)),
            timeout=timeout,
        )
        self._client = openai.AsyncOpenAI(
            base_url=base_url or env("LLM_BASE_URL"), api_key=api_key, timeout=timeout, max_retries=0, http_client=http_client
        )

    def _params(self, messages, tools, kwargs) -> dict:
        params: dict = {"model": self.model, "messages": messages}
        if tools:
            params["tools"] = tools
        if self.temperature is not None:
            params["temperature"] = self.temperature
        params.update(kwargs)
        return params

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        try:
            resp = await self._client.chat.completions.create(**self._params(messages, tools, kwargs))
        except (self._openai.APIStatusError, self._openai.APIConnectionError) as e:
            raise map_openai_error(e, self._openai) from e
        return response_from_openai(resp, self.model)

    async def stream(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> AsyncIterator[StreamEvent]:
        params = self._params(messages, tools, kwargs)
        params["stream"] = True
        params.setdefault("stream_options", {"include_usage": True})  # 流式时要显式要求返回用量，否则算不了成本
        errors = (self._openai.APIStatusError, self._openai.APIConnectionError)
        try:
            stream = await self._client.chat.completions.create(**params)
        except errors as e:
            raise map_openai_error(e, self._openai) from e
        acc, parts = ToolCallAccumulator(), []
        usage, finish, model = Usage(), "stop", self.model
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = usage_from_openai(chunk.usage)
                if getattr(chunk, "model", None):
                    model = chunk.model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None:
                    if delta.content:
                        parts.append(delta.content)
                        yield TextDelta(delta.content)
                    if delta.tool_calls:
                        acc.add(delta.tool_calls)
                if choice.finish_reason:
                    finish = choice.finish_reason
        except errors as e:
            raise map_openai_error(e, self._openai) from e
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                await close()  # 提前退出（如客户端断开）时立刻释放连接，不让连接池被占满
        yield StreamDone(LLMResponse(content="".join(parts) or None, tool_calls=acc.result(), usage=usage, model=model, finish_reason=finish))

    async def aclose(self) -> None:
        await self._client.close()


def map_openai_error(e: Exception, openai_module) -> LLMError:
    """把 OpenAI SDK 的异常统一成 LLMError。"""
    if isinstance(e, openai_module.APIStatusError):
        code = e.status_code
        # 429 限流、408 超时、5xx 服务端错误：重试可能成功；400/401/403/404：重试没用。
        # 但 429 有两种：限流（等一等就好）和额度用完（insufficient_quota，重试一万次也没用）。
        retryable = code in (408, 409, 429) or code >= 500
        if code == 429 and "insufficient_quota" in str(e):
            retryable = False
        retry_after = None
        try:
            retry_after = float(e.response.headers.get("retry-after"))
        except (TypeError, ValueError, AttributeError):
            pass
        return LLMError(str(e), status_code=code, retryable=retryable, retry_after=retry_after)
    return LLMError(f"连接模型失败：{e}", retryable=True)  # APIConnectionError，包含超时


def usage_from_openai(raw_usage) -> Usage:
    if not raw_usage:
        return Usage()
    details = getattr(raw_usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    out_details = getattr(raw_usage, "completion_tokens_details", None)
    reasoning = (getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0
    return Usage(raw_usage.prompt_tokens or 0, raw_usage.completion_tokens or 0, cached, reasoning)


def response_from_openai(resp, default_model: str) -> LLMResponse:
    """把 Chat Completions 响应转换成 LLMResponse。"""
    choice = resp.choices[0]
    msg = choice.message
    calls = [
        ToolCall(id=tc.id or new_call_id(), name=tc.function.name, arguments=tc.function.arguments or "{}")
        for tc in (msg.tool_calls or [])
    ]
    return LLMResponse(
        content=msg.content,
        tool_calls=calls,
        usage=usage_from_openai(resp.usage),
        model=resp.model or default_model,
        finish_reason=choice.finish_reason or "stop",
    )


# ------------------------------------------------------------------------------------ 剧本模型

ScriptItem = Union[LLMResponse, BaseException, Callable[[list[Message]], LLMResponse]]


class ScriptedLLM:
    """按剧本返回的假模型 —— 测试 Agent 的标准做法。

    二选一：
    - script：按顺序消费的列表，每一项可以是
        LLMResponse：直接返回；
        Exception：抛出（用来模拟限流、超时）；
        函数 f(messages) -> LLMResponse：根据当前对话动态决定；
    - responder：函数 f(messages) -> LLMResponse，每次调用都用它（适合几百个并发会话，互不串台）。

    latency：每次调用的模拟耗时（秒），或函数 f(调用序号) -> 秒。等待用的是 asyncio.sleep，
    所以并发的会话会真正重叠；被取消时 CancelledError 从这里抛出 —— 正是取消传播要验证的。
    记录：calls（每次调用的消息深拷贝，方便断言"Agent 到底给模型发了什么"；keep_calls 限制条数，
    长时间压测时不会无限增长）、in_flight、max_in_flight（"同一时刻真实并发了多少个模型调用"的证据）。
    """

    def __init__(
        self,
        script: list[ScriptItem] | None = None,
        *,
        responder: Callable[[list[Message]], LLMResponse] | None = None,
        latency: float | Callable[[int], float] = 0.0,
        model: str = "scripted",
        stream_chunk: int = 4,
        keep_calls: int | None = None,
    ):
        if (script is None) == (responder is None):
            raise ValueError("script 和 responder 必须且只能提供一个")
        self.script = list(script or [])
        self.responder = responder
        self.latency = latency
        self.model = model
        self.stream_chunk = stream_chunk
        self.keep_calls = keep_calls
        self.calls: list[dict] = []
        self.call_count = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def _next(self, messages, tools, kwargs) -> LLMResponse:
        self.call_count += 1
        n = self.call_count
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "kwargs": kwargs})
        if self.keep_calls is not None and len(self.calls) > self.keep_calls:
            del self.calls[: len(self.calls) - self.keep_calls]
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            delay = self.latency(n) if callable(self.latency) else self.latency
            if delay:
                await asyncio.sleep(delay)
            if self.responder is not None:
                item = self.responder(messages)
            else:
                if not self.script:
                    raise RuntimeError("ScriptedLLM 剧本用完了：Agent 调用模型的次数比预期多")
                item = self.script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                if callable(item):
                    item = item(messages)
            item = copy.deepcopy(item)  # 同一个剧本对象被多次返回时，调用方修改它不会影响下一次
            item.model = item.model or self.model
            return item
        finally:
            self.in_flight -= 1

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        return await self._next(messages, tools, kwargs)

    async def stream(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> AsyncIterator[StreamEvent]:
        response = await self._next(messages, tools, kwargs)
        text = response.content or ""
        for i in range(0, len(text), self.stream_chunk):
            await asyncio.sleep(0)  # 让出事件循环，模拟分片到达
            yield TextDelta(text[i : i + self.stream_chunk])
        yield StreamDone(response)


# ---------- 构造剧本的便捷函数 ----------

def reply(text: str, input_tokens: int = 20, output_tokens: int = 10) -> LLMResponse:
    """剧本：模型直接给出最终回答。"""
    return LLMResponse(content=text, usage=Usage(input_tokens, output_tokens))


def call_tool(tool_name: str, /, input_tokens: int = 20, output_tokens: int = 10, **arguments) -> LLMResponse:
    """剧本：模型调用一个工具。例：call_tool("get_weather", city="北京")"""
    call = ToolCall(id=new_call_id(), name=tool_name, arguments=json.dumps(arguments, ensure_ascii=False))
    return LLMResponse(tool_calls=[call], usage=Usage(input_tokens, output_tokens), finish_reason="tool_calls")


def call_tools(*calls: tuple[str, dict]) -> LLMResponse:
    """剧本：模型在一轮里并行调用多个工具。"""
    tcs = [ToolCall(id=new_call_id(), name=n, arguments=json.dumps(a, ensure_ascii=False)) for n, a in calls]
    return LLMResponse(tool_calls=tcs, usage=Usage(20, 10), finish_reason="tool_calls")


def default_llm(model: str | None = None, **kwargs) -> OpenAICompatLLM:
    """按 .env 配置创建真实模型客户端。"""
    return OpenAICompatLLM(model=model, **kwargs)
