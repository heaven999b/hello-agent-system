"""模型网关适配器：把 ResilientLLM 的"进程内直连 + 自己写重试/降级"换成 LiteLLM Router（第 29 课）。

教学版的局限：ResilientLLM 在每个进程里各自直连厂商，重试、熔断、降级的状态都只在本进程内存里；
密钥散落在每个服务的环境变量里；预算和限流也各算各的。LiteLLM 提供两种形态：

- **Router（SDK）**：进程内的路由器。多个上游部署（deployment）组成一个"模型组"，
  负载均衡、重试、冷却（cooldown，相当于熔断）、按模型组降级（fallbacks）。本模块适配的就是它。
- **Proxy（网关服务）**：同一个 Router 跑成独立的 OpenAI 兼容服务，再加上虚拟 key、按团队/按 key 的预算、
  Redis 共享限流计数、缓存、审计。业务服务只需要把 base_url 指向网关（agentkit 的 OpenAICompatLLM 即可），
  部署配置见 lessons/29_gateway_and_guardrails/configs/litellm-config.yaml。

LiteLLMRouterLLM 实现 agentkit 的 LLM 协议，底层是 Router.acompletion：

    llm = LiteLLMRouterLLM.from_env()    # await llm.chat(...)；async for ev in llm.stream(...)

重试只放一层：Router 已经在做重试和降级，外面再套 ResilientLLM(max_attempts=3)
会把一次失败放大成 (num_retries+1) × 3 次请求（重试放大，第 08 课）。要叠加时把其中一层的重试次数设为 0 / 1。
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from agentkit.config import env
from agentkit.llm import LLMError, StreamDone, StreamEvent, TextDelta, ToolCallAccumulator
from agentkit.types import LLMResponse, Message, ToolCall, Usage, new_call_id

from . import require

__all__ = [
    "LiteLLMRouterLLM",
    "to_llm_error",
    "to_llm_response",
]


def _litellm():
    return require("litellm", "gateway")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """LiteLLM 的响应对象既支持属性也支持下标访问；usage 细节在不同上游可能是对象、dict 或 None。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage(u: Any) -> Usage:
    cached = _get(_get(u, "prompt_tokens_details"), "cached_tokens", 0) or 0
    reasoning = _get(_get(u, "completion_tokens_details"), "reasoning_tokens", 0) or 0
    return Usage(_get(u, "prompt_tokens", 0) or 0, _get(u, "completion_tokens", 0) or 0, cached, reasoning)


def to_llm_response(resp: Any, default_model: str = "") -> LLMResponse:
    """把 litellm.ModelResponse（OpenAI 格式）映射成 agentkit 的 LLMResponse。"""
    choice = resp.choices[0]
    msg = choice.message
    calls = []
    for tc in _get(msg, "tool_calls") or []:
        fn = _get(tc, "function")
        calls.append(
            ToolCall(id=_get(tc, "id") or new_call_id(), name=_get(fn, "name") or "", arguments=_get(fn, "arguments") or "{}")
        )
    u = _get(resp, "usage")
    return LLMResponse(
        content=_get(msg, "content"),
        tool_calls=calls,
        usage=_usage(u) if u is not None else Usage(),
        model=_get(resp, "model") or default_model,  # 真正回答的上游模型：降级后这里是备用模型的名字
        finish_reason=_get(choice, "finish_reason") or "stop",
    )


def to_llm_error(e: Exception) -> LLMError:
    """把 LiteLLM / Router 抛出的异常映射成 agentkit 的 LLMError，并标注"重试是否可能成功"。

    规则与 OpenAICompatLLM 一致：408/409/429/5xx、连接失败、超时 → 可重试；
    400/401/403/404、上下文超长、内容策略、预算耗尽 → 重试没用。
    额外两种 Router 特有的情况：
    - "no deployments available"（模型组里所有部署都在冷却期）→ 可重试，retry_after = 冷却时间；
    - 请求了一个根本不存在的模型组 → 配置错误，不可重试。
    """
    litellm = _litellm()
    msg = f"{type(e).__name__}: {e}"
    if isinstance(e, LLMError):
        return e
    # Router 冷却：所有部署都暂时不可用（RouterRateLimitError 是 ValueError 的子类，不带 status_code）
    cooldown = getattr(e, "cooldown_time", None)
    if cooldown is not None and "No deployments available" in str(e):
        return LLMError(msg, status_code=429, retryable=True, retry_after=float(cooldown))
    if isinstance(e, getattr(litellm, "BudgetExceededError", ())):
        return LLMError(msg, status_code=429, retryable=False)
    non_retryable = tuple(
        c
        for c in (
            getattr(litellm, "ContextWindowExceededError", None),
            getattr(litellm, "ContentPolicyViolationError", None),
        )
        if c is not None
    )
    code = getattr(e, "status_code", None)
    if isinstance(e, non_retryable):
        return LLMError(msg, status_code=code or 400, retryable=False)
    if isinstance(e, (litellm.Timeout, litellm.APIConnectionError, TimeoutError, ConnectionError)):
        return LLMError(msg, status_code=code, retryable=True)
    if isinstance(code, int):
        retryable = code in (408, 409, 429) or code >= 500
        if code == 429 and "insufficient_quota" in str(e):
            retryable = False  # 额度用完：重试一万次也没用
        retry_after = None
        headers = getattr(getattr(e, "response", None), "headers", None) or {}
        try:
            retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError, AttributeError):
            pass
        return LLMError(msg, status_code=code, retryable=retryable, retry_after=retry_after)
    return LLMError(msg, retryable=False)


class LiteLLMRouterLLM:
    """实现 agentkit 的 LLM 协议，底层是 litellm.Router.acompletion。

    await chat(messages, tools) -> LLMResponse：一次完整调用。一个事件循环里可以同时挂几十个请求，不需要几十个线程。
    stream(messages, tools)：逐个 yield TextDelta（文本增量），最后 yield 一个 StreamDone（完整 LLMResponse，
    含拼好的工具调用和 usage）。降级只发生在拿到第一个分片之前（建立连接 / 上游直接报错）；
    流到一半断了，Router 没法把已经发给用户的半句话"收回来"。

    model_list: LiteLLM 格式的部署列表，每项 {"model_name": 模型组名, "litellm_params": {...}}；
                同名的多项组成一个模型组，Router 在组内负载均衡。
    fallbacks:  [{"主模型组": ["备用组1", "备用组2"]}]：主模型组重试耗尽后按顺序降级。
    num_retries: 每个模型组的重试次数（Router 负责退避，等待时让出事件循环）。
    timeout:    单次请求超时（秒）。
    model:      本 LLM 调用哪个模型组；默认 model_list 第一项的 model_name。
    router_kwargs: 原样传给 litellm.Router，如 routing_strategy、allowed_fails、cooldown_time、redis_host……
    """

    def __init__(
        self,
        model_list: list[dict],
        *,
        fallbacks: list[dict] | None = None,
        num_retries: int = 2,
        timeout: float = 60,
        model: str | None = None,
        **router_kwargs: Any,
    ):
        if not model_list:
            raise ValueError("model_list 不能为空")
        litellm = _litellm()
        self.model = model or model_list[0]["model_name"]
        self.fallbacks = list(fallbacks or [])
        self.router = litellm.Router(
            model_list=model_list,
            fallbacks=self.fallbacks,
            num_retries=num_retries,
            timeout=timeout,
            **router_kwargs,
        )
        self.last_route: dict = {}  # 最近一次调用的路由信息：实际模型组、重试次数、降级次数、耗时
        self.events: list[str] = []  # 与 ResilientLLM.events 对齐：记录降级，方便观测和 demo 展示

    def __repr__(self) -> str:  # 不打印 model_list：里面可能有 api_key
        return f"{type(self).__name__}(model={self.model!r}, fallbacks={self.fallbacks!r})"

    @classmethod
    def from_env(cls, *, primary: str | None = None, fallback: str | None = None, **kwargs: Any):
        """按环境变量（或 .env）构造：主模型 LLM_MODEL，备用 LLM_FALLBACK_MODEL，上游地址 LLM_BASE_URL，key LLM_API_KEY。

        key 只在内存里传给 Router，绝不写进任何文件。上游是任意 OpenAI 兼容接口（cliproxyapi / OpenAI / vLLM……），
        所以 litellm_params.model 用 "openai/<模型名>" 前缀，告诉 LiteLLM 按 OpenAI 协议调用 api_base。
        primary / fallback 可覆盖环境变量（例如故意配一个不存在的主模型，演示降级；fallback="" 表示不要备用）。
        """
        primary = primary or env("LLM_MODEL", "gpt-5.5")
        fallback = fallback if fallback is not None else env("LLM_FALLBACK_MODEL")
        base, key = env("LLM_BASE_URL"), env("LLM_API_KEY")
        if not key:
            raise RuntimeError("没有找到 LLM_API_KEY。请把 .env.example 复制为 .env 并填写，或设置环境变量。")

        def deployment(name: str) -> dict:
            params = {"model": f"openai/{name}", "api_key": key}
            if base:
                params["api_base"] = base
            return {"model_name": name, "litellm_params": params}

        model_list = [deployment(primary)]
        fallbacks = None
        if fallback and fallback != primary:  # 降级到自己没有意义
            model_list.append(deployment(fallback))
            fallbacks = [{primary: [fallback]}]
        kwargs.setdefault("fallbacks", fallbacks)
        return cls(model_list, model=primary, **kwargs)

    def _params(self, messages: list[Message], tools: list[dict] | None, kwargs: dict) -> dict:
        params: dict = {"model": self.model, "messages": messages}
        if tools:
            params["tools"] = tools
        params.update(kwargs)
        return params

    def _on_error(self, e: Exception, start: float) -> LLMError:
        self.last_route = {"model_group": self.model, "ok": False, "error": type(e).__name__,
                           "latency_s": round(time.perf_counter() - start, 3)}
        return to_llm_error(e)

    def _on_success(self, hidden: dict | None, start: float, **extra: Any) -> None:
        headers = (hidden or {}).get("additional_headers") or {}
        self.last_route = {
            "model_group": headers.get("x-litellm-model-group", self.model),
            # 注意：这个头只统计"最终成功的那个模型组"上的重试；主模型组重试失败后降级时，这里仍是 0
            "attempted_retries": headers.get("x-litellm-attempted-retries"),
            "attempted_fallbacks": headers.get("x-litellm-attempted-fallbacks"),
            "ok": True,
            "latency_s": round(time.perf_counter() - start, 3),
            **extra,
        }
        if self.last_route["model_group"] != self.model:
            self.events.append(f"fallback {self.model} -> {self.last_route['model_group']}")


    async def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs: Any) -> LLMResponse:
        start = time.perf_counter()
        try:
            resp = await self.router.acompletion(**self._params(messages, tools, kwargs))
        except Exception as e:  # noqa: BLE001
            raise self._on_error(e, start) from e
        self._on_success(getattr(resp, "_hidden_params", None), start)
        return to_llm_response(resp, default_model=self.model)

    async def stream(self, messages: list[Message], tools: list[dict] | None = None, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        start = time.perf_counter()
        params = self._params(messages, tools, kwargs)
        params["stream"] = True
        params.setdefault("stream_options", {"include_usage": True})  # 不加这个，流式响应里没有 token 用量
        acc = ToolCallAccumulator()  # 工具调用分片按 index 拼接：id/name 只在第一片，arguments 分很多片
        parts: list[str] = []
        usage, finish, model = Usage(), None, self.model
        ttft: float | None = None
        chunks = 0
        resp = None
        completed = False
        try:
            resp = await self.router.acompletion(**params)
            async for chunk in resp:
                chunks += 1
                model = _get(chunk, "model") or model
                u = _get(chunk, "usage")
                if u is not None and (_get(u, "prompt_tokens") or _get(u, "completion_tokens")):
                    usage = _usage(u)
                for choice in _get(chunk, "choices") or []:
                    finish = _get(choice, "finish_reason") or finish
                    delta = _get(choice, "delta")
                    if delta is None:
                        continue
                    text = _get(delta, "content")
                    if text:
                        if ttft is None:
                            ttft = time.perf_counter() - start
                        parts.append(text)
                        yield TextDelta(text)
                    if _get(delta, "tool_calls"):
                        acc.add(_get(delta, "tool_calls"))
            completed = True
        except Exception as e:  # noqa: BLE001 —— CancelledError 是 BaseException，不会被这里吞掉，取消照常穿透
            raise self._on_error(e, start) from e
        finally:
            close = getattr(resp, "aclose", None)
            if not completed and close is not None:
                try:
                    await close()  # 消费方提前退出（用户断开、运行被取消）：立刻释放上游连接，不再为没人看的 token 付费
                except Exception:  # noqa: BLE001
                    pass
        calls = acc.result()
        self._on_success(getattr(resp, "_hidden_params", None), start,
                         ttft_s=None if ttft is None else round(ttft, 3), chunks=chunks)
        yield StreamDone(LLMResponse(content="".join(parts) or None, tool_calls=calls, usage=usage, model=model,
                                     finish_reason=finish or ("tool_calls" if calls else "stop")))
