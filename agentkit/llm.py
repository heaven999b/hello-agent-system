"""LLM 抽象层。

企业级设计要点：业务代码只依赖一个很小的接口 `LLM.chat(messages, tools) -> LLMResponse`，
而不是直接依赖某家厂商的 SDK。好处：
- 换模型 / 换厂商 / 接模型网关，业务代码不用改；
- 可以用"装饰器"叠加能力：重试、熔断、降级、缓存、限流（见第 05 课 ResilientLLM）；
- 测试时换成 ScriptedLLM，零成本、100% 可复现。
"""

from __future__ import annotations

import copy
from typing import Callable, Protocol, Union

from .config import env
from .types import LLMResponse, Message, ToolCall, Usage, new_call_id


class LLM(Protocol):
    model: str

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse: ...


class LLMError(Exception):
    """统一的模型调用异常。retryable 标记这个错误"重试是否可能成功"（第 05 课）。"""

    def __init__(
        self, message: str, *, status_code: int | None = None, retryable: bool = False, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after  # 服务端通过 Retry-After 头建议的等待秒数（如果有）


class OpenAICompatLLM:
    """任何 OpenAI 兼容接口（cliproxyapi / OpenAI / DeepSeek / Qwen / vLLM ...）。

    注意 max_retries=0：我们故意关掉 SDK 自带的重试，
    把重试放到自己可见、可控、可观测的 ResilientLLM 里（第 05 课）。
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        temperature: float | None = None,
    ):
        import openai

        self._openai = openai
        self.model = model or env("LLM_MODEL", "gpt-5.5")
        base_url = base_url or env("LLM_BASE_URL")
        api_key = api_key or env("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("没有找到 LLM_API_KEY。请把 .env.example 复制为 .env 并填写，或设置环境变量。")
        self.temperature = temperature
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        params: dict = {"model": self.model, "messages": messages}
        if tools:
            params["tools"] = tools
        if self.temperature is not None:
            params["temperature"] = self.temperature
        params.update(kwargs)
        try:
            resp = self._client.chat.completions.create(**params)
        except self._openai.APIStatusError as e:
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
            raise LLMError(str(e), status_code=code, retryable=retryable, retry_after=retry_after) from e
        except self._openai.APIConnectionError as e:  # 包含超时
            raise LLMError(f"连接模型失败：{e}", retryable=True) from e

        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(id=tc.id or new_call_id(), name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
        ]
        usage = Usage()
        if resp.usage:
            details = getattr(resp.usage, "prompt_tokens_details", None)
            cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
            out_details = getattr(resp.usage, "completion_tokens_details", None)
            reasoning = (getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0
            usage = Usage(resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0, cached, reasoning)
        return LLMResponse(
            content=msg.content,
            tool_calls=calls,
            usage=usage,
            model=resp.model or self.model,
            finish_reason=choice.finish_reason or "stop",
        )


ScriptItem = Union[LLMResponse, Exception, Callable[[list[Message]], LLMResponse]]


class ScriptedLLM:
    """按剧本返回的假模型 —— 测试 Agent 的标准做法。

    script 里每一项可以是：
      - LLMResponse：直接返回
      - Exception：抛出（用来模拟限流、超时）
      - 函数 f(messages) -> LLMResponse：根据当前对话动态决定
    所有调用都记录在 self.calls，方便断言"Agent 到底给模型发了什么"。
    """

    def __init__(self, script: list[ScriptItem], model: str = "scripted"):
        self.script = list(script)
        self.model = model
        self.calls: list[dict] = []

    def chat(self, messages: list[Message], tools: list[dict] | None = None, **kwargs) -> LLMResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "kwargs": kwargs})
        if not self.script:
            raise RuntimeError("ScriptedLLM 剧本用完了：Agent 调用模型的次数比预期多")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        if not item.model:
            item.model = self.model
        return item


# ---------- 构造剧本的便捷函数 ----------

def reply(text: str, input_tokens: int = 20, output_tokens: int = 10) -> LLMResponse:
    """剧本：模型直接给出最终回答。"""
    return LLMResponse(content=text, usage=Usage(input_tokens, output_tokens))


def call_tool(tool_name: str, /, input_tokens: int = 20, output_tokens: int = 10, **arguments) -> LLMResponse:
    """剧本：模型调用一个工具。例：call_tool("get_weather", city="北京")"""
    import json

    call = ToolCall(id=new_call_id(), name=tool_name, arguments=json.dumps(arguments, ensure_ascii=False))
    return LLMResponse(tool_calls=[call], usage=Usage(input_tokens, output_tokens), finish_reason="tool_calls")


def call_tools(*calls: tuple[str, dict]) -> LLMResponse:
    """剧本：模型在一轮里并行调用多个工具。"""
    import json

    tcs = [ToolCall(id=new_call_id(), name=n, arguments=json.dumps(a, ensure_ascii=False)) for n, a in calls]
    return LLMResponse(tool_calls=tcs, usage=Usage(20, 10), finish_reason="tool_calls")


def default_llm(model: str | None = None) -> OpenAICompatLLM:
    """按 .env 配置创建真实模型客户端。"""
    return OpenAICompatLLM(model=model)
