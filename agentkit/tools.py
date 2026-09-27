"""工具系统：Agent 与真实世界交互的唯一通道。

企业级工具设计的 7 条原则（第 03 课详讲）：
1. Schema 即契约：用类型注解自动生成 JSON Schema，参数写清楚 description / 枚举 / 取值范围；
2. 永远校验参数：模型输出的 JSON 可能不合法、可能缺字段、可能多字段；
3. 错误即观察：工具出错不抛给框架，而是变成文字反馈给模型，让它自己纠正；
4. 身份不交给模型：user_id / tenant_id 由系统通过 ctx 注入，绝不作为模型可填的参数；
5. 风险分级：read / write / dangerous，供权限系统做审批（第 09 课）；
6. 超时 + 输出截断：一个慢工具或一个巨大的返回值都不能拖垮整个 Agent；
7. 写操作幂等：同一次调用重放时不能重复扣款 / 重复建工单（第 08 课）。
"""

from __future__ import annotations

import contextvars
import inspect
import json
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .types import ToolCall

Risk = Literal["read", "write", "dangerous"]


class ToolError(Exception):
    """业务错误（如"订单不存在"）。消息会原样反馈给模型，所以请写成模型能看懂、能据此行动的话。"""


@dataclass
class ToolContext:
    """系统注入给工具的**可信**上下文。工具函数只要声明一个名为 ctx 的参数就能拿到。

    为什么不让模型传 user_id？因为模型的输入可能被提示词注入操纵，
    让模型决定"我是谁"等于让攻击者决定"我是谁"。
    """

    run_id: str = "local"
    call_id: str = "call"
    tenant_id: str | None = None
    user_id: str | None = None
    roles: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        """同一个 run 里的同一次工具调用 → 同一个 key。重放时据此去重。"""
        return f"{self.run_id}:{self.call_id}"


@dataclass
class ToolResult:
    ok: bool
    content: str
    error_type: str | None = None  # not_found / invalid_args / timeout / tool_error / exception / denied
    detail: str | None = None  # 只给工程师看的内部细节（异常原文），写进追踪/日志，不给模型


class Tool:
    """把一个普通 Python 函数包装成 Agent 可调用的工具。一般用 @tool 装饰器创建。"""

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        risk: Risk = "read",
        timeout_s: float = 30.0,
        max_output_chars: int = 4000,
    ):
        self.fn = fn
        self.name = name or fn.__name__
        self.description = (description or inspect.getdoc(fn) or "").strip()
        if not self.description:
            raise ValueError(f"工具 {self.name} 没有描述。工具描述是写给模型看的'说明书'，必须写。")
        self.risk = risk
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self.wants_ctx = "ctx" in inspect.signature(fn).parameters
        self.args_model = _build_args_model(fn, self.name)

    def schema(self) -> dict:
        """生成 OpenAI function-calling 格式的工具定义。"""
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }

    def parse_arguments(self, arguments: str) -> tuple[dict | None, str | None]:
        """解析并校验模型给的参数 JSON。返回 (参数 dict, None) 或 (None, 给模型看的错误说明)。"""
        try:
            raw = json.loads(arguments or "{}")
            if not isinstance(raw, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as e:
            return None, f"错误：参数不是合法的 JSON 对象（{e}）。请重新生成参数。"
        try:
            args = self.args_model.model_validate(raw)
        except ValidationError as e:
            problems = "\n".join(f"- {'.'.join(map(str, err['loc'])) or '参数'}: {err['msg']}" for err in e.errors())
            return None, f"错误：参数校验失败：\n{problems}\n请修正后重试。"
        return dict(args), None

    def __call__(self, *args, **kwargs):  # 允许像普通函数一样直接调用，方便单元测试
        return self.fn(*args, **kwargs)

    def __repr__(self) -> str:
        return f"Tool({self.name!r}, risk={self.risk!r})"


def _build_args_model(fn: Callable, name: str) -> type[BaseModel]:
    """从函数签名自动生成 Pydantic 参数模型（进而生成 JSON Schema）。

    支持 Annotated[str, Field(description=...)] 给参数写描述，ctx 参数会被跳过。
    extra="forbid"：模型编造了不存在的参数时直接报错，而不是悄悄忽略。
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    for pname, p in inspect.signature(fn).parameters.items():
        if pname == "ctx":
            continue
        annotation = hints.get(pname, str)
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[pname] = (annotation, default)
    return create_model(f"{name}_args", __config__=ConfigDict(extra="forbid"), **fields)


def tool(fn: Callable | None = None, **options) -> Any:
    """装饰器：@tool 或 @tool(risk="write", timeout_s=5)。"""
    if fn is None:
        return lambda f: Tool(f, **options)
    return Tool(fn, **options)


class IdempotencyStore:
    """幂等存储：记住"某个 idempotency_key 已经成功执行过，结果是什么"。

    生产环境用 Redis / 数据库唯一索引实现，并设置过期时间；这里用内存 dict 演示原理。
    """

    def __init__(self):
        self._results: dict[str, ToolResult] = {}

    def get(self, key: str) -> ToolResult | None:
        return self._results.get(key)

    def put(self, key: str, result: ToolResult) -> None:
        self._results[key] = result


class ToolRegistry:
    """工具注册表 + 统一执行入口。所有工具调用都经过 execute()，这是做校验/超时/幂等的唯一关口。"""

    def __init__(self, tools: Iterable[Tool] = (), idempotency_store: IdempotencyStore | None = None):
        self._tools: dict[str, Tool] = {}
        self.idempotency_store = idempotency_store
        for t in tools:
            self.register(t)

    def register(self, t: Tool) -> None:
        if not isinstance(t, Tool):
            raise TypeError(f"{t!r} 不是 Tool，是不是忘了加 @tool 装饰器？")
        if t.name in self._tools:
            raise ValueError(f"工具名重复：{t.name}")
        self._tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, only: Iterable[str] | None = None) -> list[dict]:
        allowed = set(only) if only is not None else None
        return [t.schema() for t in self._tools.values() if allowed is None or t.name in allowed]

    def execute(self, call: ToolCall, ctx: ToolContext | None = None) -> ToolResult:
        ctx = ctx or ToolContext(call_id=call.id)
        t = self._tools.get(call.name)
        if t is None:
            return ToolResult(False, f"错误：不存在名为 {call.name!r} 的工具。可用工具：{', '.join(self._tools)}", "not_found")

        # 1) 解析 JSON（模型可能输出不合法的 JSON）+ 2) 按 Schema 校验（缺字段、类型错、越界、多余字段）
        kwargs, error = t.parse_arguments(call.arguments)
        if error is not None:
            return ToolResult(False, error, "invalid_args")
        if t.wants_ctx:
            kwargs["ctx"] = ctx

        # 3) 幂等：写操作如果这个 key 已成功执行过，直接返回上次结果，不再产生副作用
        use_idem = self.idempotency_store is not None and t.risk in ("write", "dangerous")
        if use_idem:
            cached = self.idempotency_store.get(ctx.idempotency_key)
            if cached is not None:
                return cached

        # 4) 带超时执行。注意：Python 线程无法被强杀，超时后线程可能仍在后台跑，
        #    生产中高风险/不可信的工具应放到独立进程或沙箱（容器、gVisor、Firecracker）里执行。
        #    copy_context：把当前的 contextvars（如追踪的 Span 栈）带进工具线程，
        #    否则工具内部再调用子 Agent 时，子 Agent 的 trace 会"断链"，无法嵌套在父 Span 下。
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(contextvars.copy_context().run, t.fn, **kwargs)
        try:
            output = future.result(timeout=t.timeout_s)
        except FutureTimeout:
            return ToolResult(False, f"错误：工具 {t.name} 执行超时（>{t.timeout_s}s）。可以稍后重试或换一种方式。", "timeout")
        except ToolError as e:
            return ToolResult(False, f"错误：{e}", "tool_error")
        except Exception as e:  # noqa: BLE001 —— 任何异常都要变成观察，不能让 Agent 崩溃
            # 意外异常的原文（SQL、内网地址、堆栈……）不能给模型：模型可能把它转述给用户。
            # 给模型一句可行动的提示 + 错误编号；原文放进 detail，由追踪/日志记录，工程师用编号关联排查。
            error_id = uuid.uuid4().hex[:8]
            return ToolResult(
                False,
                f"错误：工具 {t.name} 内部出错（错误编号 {error_id}）。这不是参数问题，可以稍后重试一次；"
                f"如果仍然失败，请告诉用户该功能暂时不可用，并提供错误编号。不要猜测或透露内部技术细节。",
                "exception",
                detail=f"[{error_id}] {type(e).__name__}: {e}",
            )
        finally:
            pool.shutdown(wait=False)

        # 5) 序列化 + 截断：防止一个巨大的返回值撑爆上下文窗口
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        if len(text) > t.max_output_chars:
            text = text[: t.max_output_chars] + f"\n...[输出已截断，原始长度 {len(text)} 字符]"
        result = ToolResult(True, text)
        if use_idem:
            self.idempotency_store.put(ctx.idempotency_key, result)
        return result
