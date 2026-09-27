"""异步 LLM 接口：高并发服务的地基。

同步客户端调一次模型，整个线程就卡住 3–10 秒；一个进程要同时服务几百个会话，
要么开几百个线程（内存和上下文切换都扛不住），要么用 asyncio：等待模型的时候事件循环去推进别的会话。

本模块提供：
- AsyncLLM 协议：`async def chat(...)`，以及可选的流式接口 `stream(...)`；
- AsyncOpenAICompatLLM：基于 openai.AsyncOpenAI，带连接池上限；
- 流式输出：逐段产出 TextDelta，最后产出 StreamDone（其中的工具调用参数已按 index 拼好）；
- AsyncScriptedLLM：可以设置延迟的剧本模型，用来证明"并发真的发生了"。
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Protocol, Union

from ..config import env
from ..llm import map_openai_error, response_from_openai, usage_from_openai
from ..types import LLMResponse, Message, ToolCall, Usage, new_call_id


@dataclass
class TextDelta:
    """流式输出的一小段文本。"""

    text: str


@dataclass
class StreamDone:
    """流式输出结束：携带完整的 LLMResponse（文本、拼好的工具调用、用量）。"""

    response: LLMResponse


StreamEvent = Union[TextDelta, StreamDone]


class AsyncLLM(Protocol):
    model: str

    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse: ...


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


class AsyncOpenAICompatLLM:
    """任何 OpenAI 兼容接口的异步客户端。

    max_connections：到模型网关的 HTTP 连接池上限。它就是这个进程对网关的最大并发，
    应该和网关/厂商给你的并发配额匹配；超过上限的请求会在客户端排队，而不是把网关打出 429。
    max_retries=0：重试交给可见、可控的 AsyncResilientLLM。
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


class AsyncScriptedLLM:
    """异步剧本模型，测试和压测用。

    二选一：
    - script：按顺序消费的 LLMResponse / Exception 列表（适合单个会话的精确测试）；
    - responder：函数 f(messages) -> LLMResponse，根据当前对话决定回复（适合几百个并发会话，互不串台）。
    latency：每次调用的模拟耗时（秒），或函数 f(调用序号) -> 秒。
    记录 calls、in_flight、max_in_flight —— 最后一个值就是"同一时刻真实并发了多少个模型调用"的证据。
    """

    def __init__(
        self,
        script: list | None = None,
        *,
        responder: Callable[[list[Message]], LLMResponse] | None = None,
        latency: float | Callable[[int], float] = 0.0,
        model: str = "scripted",
        stream_chunk: int = 4,
    ):
        if (script is None) == (responder is None):
            raise ValueError("script 和 responder 必须且只能提供一个")
        self.script = list(script or [])
        self.responder = responder
        self.latency = latency
        self.model = model
        self.stream_chunk = stream_chunk
        self.calls: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def _next(self, messages, tools, kwargs) -> LLMResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "kwargs": kwargs})
        n = len(self.calls)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            delay = self.latency(n) if callable(self.latency) else self.latency
            if delay:
                await asyncio.sleep(delay)  # 被取消时，CancelledError 会从这里抛出 —— 正是取消传播要验证的
            if self.responder is not None:
                item = self.responder(messages)
            else:
                if not self.script:
                    raise RuntimeError("AsyncScriptedLLM 剧本用完了：调用模型的次数比预期多")
                item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            item = copy.deepcopy(item)
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


def default_async_llm(model: str | None = None, **kwargs) -> AsyncOpenAICompatLLM:
    """按 .env 配置创建异步模型客户端。"""
    return AsyncOpenAICompatLLM(model=model, **kwargs)
