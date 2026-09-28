"""工具系统：Agent 与真实世界交互的唯一通道。

企业级工具设计的 7 条原则（第 03 课详讲）：
1. Schema 即契约：用类型注解自动生成 JSON Schema，参数写清楚 description / 枚举 / 取值范围；
2. 永远校验参数：模型输出的 JSON 可能不合法、可能缺字段、可能多字段；
3. 错误即观察：工具出错不抛给框架，而是变成文字反馈给模型，让它自己纠正；
4. 身份不交给模型：user_id / tenant_id 由系统通过 ctx 注入，绝不作为模型可填的参数；
5. 风险分级：read / write / dangerous，供权限系统做审批（第 09 课）；
6. 超时 + 输出截断：一个慢工具或一个巨大的返回值都不能拖垮整个 Agent；
7. 写操作幂等：同一次调用重放时不能重复扣款 / 重复建工单（第 08 课）。

工具函数可以是 `async def`，也可以是普通函数。三种执行方式，对应三种真实的超时语义：

| 工具类型 | 怎么执行 | 超时时发生什么 |
|---|---|---|
| `async def` 工具（调用 HTTP API、数据库等） | 直接在事件循环里 await | **真正取消**：CancelledError 传进工具，连接被释放 |
| 普通同步函数 | 放进线程池，不阻塞事件循环 | 调用方按时拿到超时结果，**但线程无法被强杀**，会在后台跑完（Python 的硬限制） |
| `@tool(isolation="process")` | 在子进程里执行 | **硬超时**：直接 kill 子进程，适合 CPU 密集或不可信代码 |
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import multiprocessing
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .timeouts import wait_for
from .types import ToolCall

Risk = Literal["read", "write", "dangerous"]
Isolation = Literal["thread", "process"]


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
        isolation: Isolation | None = None,
    ):
        self.fn = fn
        self.name = name or fn.__name__
        self.description = (description or inspect.getdoc(fn) or "").strip()
        if not self.description:
            raise ValueError(f"工具 {self.name} 没有描述。工具描述是写给模型看的'说明书'，必须写。")
        self.risk = risk
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        if isolation == "process" and inspect.iscoroutinefunction(fn):
            raise TypeError("async 工具不需要进程隔离：它本身就可以被取消")
        # None / "thread"：async 工具在事件循环里 await，同步工具进线程池；"process"：子进程执行，超时即 kill
        self.isolation = isolation
        self.wants_ctx = "ctx" in inspect.signature(fn).parameters
        self.args_model = _build_args_model(fn, self.name)

    @property
    def is_async(self) -> bool:
        """每次执行时判断（而不是构造时记下）：子类或包装器替换了 self.fn 也能正确识别。"""
        return inspect.iscoroutinefunction(self.fn)

    def schema(self) -> dict:
        """生成 OpenAI function-calling 格式的工具定义。

        结果会被缓存：工具定义创建后不再变化，而 Agent 每次调用模型都要带上全部工具定义，
        每次重新生成 JSON Schema 在高并发下是纯粹的 CPU 浪费（实测约占每个会话 CPU 的三分之一）。
        返回的是缓存对象本身，请不要修改它。
        """
        cached = getattr(self, "_schema_cache", None)
        if cached is not None:
            return cached
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        self._schema_cache = {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }
        return self._schema_cache

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

    def __call__(self, *args, **kwargs):  # 允许像普通函数一样直接调用，方便单元测试（async 工具返回协程，要 await）
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
    """装饰器：@tool 或 @tool(risk="write", timeout_s=5)，也可以装饰 async def 函数。"""
    if fn is None:
        return lambda f: Tool(f, **options)
    return Tool(fn, **options)


def isolated(t: Tool) -> Tool:
    """把一个同步工具标记为"在子进程里执行"（等价于 @tool(isolation="process")）。
    被包装的函数必须是模块级函数：子进程要能按模块名 import 到它。"""
    if t.is_async:
        raise TypeError("async 工具不需要进程隔离：它本身就可以被取消")
    t.isolation = "process"
    return t


async def maybe_await(value):
    """同步实现直接返回值，async 实现返回协程：两种都支持（钩子、检查点、幂等存储都可以是任意一种）。"""
    return await value if inspect.isawaitable(value) else value


class IdempotencyStore:
    """幂等存储：记住"某个 idempotency_key 已经成功执行过，结果是什么"。

    这里用内存 dict 演示原理，只在一个进程里有效；多个 worker 进程之间要用 agentkit.distributed.SQLiteIdempotencyStore
    （单机多进程），多机用 Redis / 数据库唯一索引（第 26 课）。get / put 也可以是 async 方法。
    """

    def __init__(self):
        self._results: dict[str, ToolResult] = {}

    def get(self, key: str) -> ToolResult | None:
        return self._results.get(key)

    def put(self, key: str, result: ToolResult) -> None:
        self._results[key] = result


class ToolRegistry:
    """工具注册表。所有工具调用都经过 ToolExecutor.execute()，这是做校验/超时/幂等的唯一关口。"""

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

    async def execute(self, call: ToolCall, ctx: ToolContext | None = None) -> ToolResult:
        """执行一次工具调用（校验 → 幂等 → 带超时执行 → 格式化）。Agent 内部用的是可共享的 ToolExecutor。"""
        return await ToolExecutor(self).execute(call, ctx or ToolContext(call_id=call.id))

    def not_found(self, name: str) -> ToolResult:
        return ToolResult(False, f"错误：不存在名为 {name!r} 的工具。可用工具：{', '.join(self._tools)}", "not_found")


class ToolExecutor:
    """统一的工具执行入口。

    max_threads：同步工具用的线程池大小。None 表示用事件循环的默认线程池（全进程共享、有上限，
    asyncio.run 结束时自动关闭）；服务里通常给一个独立的、有上限的池——同步工具再多，也不会无限开线程
    把进程拖垮（这也是一种背压），而且不和 asyncio.to_thread 等其他用途抢线程。
    """

    def __init__(self, registry: ToolRegistry, *, max_threads: int | None = None):
        self.registry = registry
        self._threads = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="agentkit-tool") if max_threads else None

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        t = self.registry.get(call.name)
        if t is None:
            return self.registry.not_found(call.name)

        # 1) 解析 JSON（模型可能输出不合法的 JSON）+ 2) 按 Schema 校验（缺字段、类型错、越界、多余字段）
        kwargs, error = t.parse_arguments(call.arguments)
        if error is not None:
            return ToolResult(False, error, "invalid_args")
        if t.wants_ctx:
            kwargs["ctx"] = ctx

        # 3) 幂等：写操作如果这个 key 已成功执行过，直接返回上次结果，不再产生副作用
        store = self.registry.idempotency_store
        use_idem = store is not None and t.risk in ("write", "dangerous")
        if use_idem:
            cached = await maybe_await(store.get(ctx.idempotency_key))
            if cached is not None:
                return cached

        # 4) 带超时执行（三种方式见模块说明）
        raised: Exception | None = None
        try:
            if t.isolation == "process":
                output = await run_in_subprocess(t.fn, kwargs, t.timeout_s)
            else:
                if t.is_async:
                    work = t.fn(**kwargs)
                else:
                    loop = asyncio.get_running_loop()
                    # copy_context：把当前的 contextvars（如追踪的 Span 栈）带进工具线程，
                    # 否则工具内部的 Span 会"断链"，无法嵌套在父 Span 下
                    runner = functools.partial(contextvars.copy_context().run, t.fn, **kwargs)
                    work = loop.run_in_executor(self._threads, runner)
                # 工具自己抛的异常先"装箱"再带出来：否则工具内部的 TimeoutError（比如下游超时）会被下面的
                # except 当成"我们的期限到了"，报成"执行超时（>30s）"，原始信息也丢了（第 16 课发现）。
                # 不用 asyncio.wait_for：3.12 之前它会在"工具刚完成 + 外部取消"同时发生时吞掉取消（timeouts.py）
                output, raised = await wait_for(_capture(work), t.timeout_s)
        except asyncio.TimeoutError:  # 只可能是我们自己的期限（进程隔离时由 run_in_subprocess 抛出）
            return timeout_result(t)
        except Exception as e:  # noqa: BLE001 —— 工具异常变成观察；CancelledError 是 BaseException，会正常穿透
            return exception_result(t, e)
        if raised is not None:
            return exception_result(t, raised)

        result = ToolResult(True, format_output(t, output))
        if use_idem:
            await maybe_await(store.put(ctx.idempotency_key, result))
        return result

    def close(self) -> None:
        if self._threads is not None:
            self._threads.shutdown(wait=False, cancel_futures=True)


async def _capture(work) -> tuple[Any, Exception | None]:
    """等待工具执行，返回 (结果, None) 或 (None, 工具抛出的异常)。取消（BaseException）照常向外传播。"""
    try:
        return await work, None
    except Exception as e:  # noqa: BLE001
        return None, e


# ------------------------------------------------------------------------------------ 进程隔离


class _FunctionRef:
    """按"模块名 + 限定名"引用一个函数，让子进程自己 import 回来。

    直接 pickle 函数也是按名字引用，但它要求 getattr(模块, 名字) 就是这个函数本身；
    而 @tool(isolation="process") 装饰模块级函数后，模块里这个名字已经变成了 Tool 对象，pickle 会报
    PicklingError（模型只看到"工具内部出错"）。这里解析时遇到 Tool 就取它的 .fn。
    """

    def __init__(self, fn):
        self.module, self.qualname = fn.__module__, fn.__qualname__

    def resolve(self):
        import importlib

        obj = importlib.import_module(self.module)
        for part in self.qualname.split("."):
            obj = getattr(obj, part)
        return obj.fn if isinstance(obj, Tool) else obj


def _function_ref(fn):
    """能按名字找回来的（模块级函数 / 类里的函数）就传引用；否则原样传，交给 pickle（嵌套函数会在这里报错）。"""
    if "<locals>" not in getattr(fn, "__qualname__", "<locals>") and getattr(fn, "__module__", None) not in (None, "__main__"):
        return _FunctionRef(fn)
    return fn


def _subprocess_entry(conn, fn, kwargs) -> None:
    try:
        if isinstance(fn, _FunctionRef):
            fn = fn.resolve()
        conn.send(("ok", fn(**kwargs)))
    except ToolError as e:
        conn.send(("tool_error", str(e)))
    except BaseException as e:  # noqa: BLE001 —— 任何异常都要带回父进程，不能让子进程静默消失
        conn.send(("exception", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


async def run_in_subprocess(fn, kwargs: dict, timeout: float):
    """在独立子进程里执行 fn(**kwargs)，超时（或调用方被取消）就 kill 子进程。

    这是 Python 里唯一可靠的"硬超时"：线程杀不掉，协程只能在 await 点被取消，
    而一个死循环的纯计算函数没有 await 点。代价是进程启动开销（spawn 通常几百毫秒）和参数必须可 pickle。
    生产中更强的隔离是容器 / gVisor / microVM（第 19 课）。
    """
    mp = multiprocessing.get_context("spawn")  # spawn 比 fork 安全：fork 一个带着事件循环和线程的进程容易死锁
    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_subprocess_entry, args=(child, _function_ref(fn), kwargs), daemon=True)
    loop = asyncio.get_running_loop()
    # spawn 一个进程要 fork/exec + 传参，同步调用会卡住事件循环（实测每次 ~10ms，所有会话一起等）
    await loop.run_in_executor(None, proc.start)
    child.close()
    try:
        ready = await loop.run_in_executor(None, parent.poll, timeout)
        if not ready:
            raise asyncio.TimeoutError
        kind, payload = parent.recv()
    except EOFError:
        raise RuntimeError(f"工具子进程异常退出（exitcode={proc.exitcode}）") from None
    finally:
        if proc.is_alive():
            proc.kill()  # 超时或被取消：直接杀掉，不给它继续消耗 CPU 的机会
        try:
            await asyncio.shield(loop.run_in_executor(None, proc.join, 2))  # join 也可能阻塞：放进线程；被取消也要把僵尸进程收掉
        finally:
            parent.close()  # 即使在等待 join 时被取消，管道也要立即关闭，不能等垃圾回收
    if kind == "ok":
        return payload
    if kind == "tool_error":
        raise ToolError(payload)
    raise RuntimeError(payload)


def timeout_result(t: Tool) -> ToolResult:
    return ToolResult(False, f"错误：工具 {t.name} 执行超时（>{t.timeout_s}s）。可以稍后重试或换一种方式。", "timeout")


def exception_result(t: Tool, e: Exception) -> ToolResult:
    """把工具抛出的异常变成给模型看的观察。"""
    if isinstance(e, ToolError):
        return ToolResult(False, f"错误：{e}", "tool_error")
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


def format_output(t: Tool, output) -> str:
    """序列化 + 截断：防止一个巨大的返回值撑爆上下文窗口。"""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    if len(text) > t.max_output_chars:
        text = text[: t.max_output_chars] + f"\n...[输出已截断，原始长度 {len(text)} 字符]"
    return text
