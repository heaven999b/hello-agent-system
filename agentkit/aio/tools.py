"""异步工具执行：三种执行方式，对应三种真实的超时语义。

| 工具类型 | 怎么执行 | 超时时发生什么 |
|---|---|---|
| `async def` 工具（调用 HTTP API、数据库等） | 直接在事件循环里 await | **真正取消**：CancelledError 传进工具，连接被释放 |
| 普通同步函数 | 放进有上限的线程池，不阻塞事件循环 | 调用方按时拿到超时结果，**但线程无法被强杀**，会在后台跑完（Python 的硬限制） |
| 标记为进程隔离的工具（`isolated(tool)`） | 在子进程里执行 | **硬超时**：直接 kill 子进程，适合 CPU 密集或不可信代码 |

参数校验、幂等、错误格式化与同步版 ToolRegistry 完全一致（共用同一套函数）。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import multiprocessing
from concurrent.futures import ThreadPoolExecutor

from ..tools import Tool, ToolContext, ToolError, ToolRegistry, ToolResult, exception_result, format_output, timeout_result
from ..types import ToolCall


def isolated(t: Tool) -> Tool:
    """把一个同步工具标记为"在子进程里执行"。被包装的函数必须是模块级函数（子进程要能 import 到它）。"""
    if inspect.iscoroutinefunction(t.fn):
        raise TypeError("async 工具不需要进程隔离：它本身就可以被取消")
    t.isolation = "process"
    return t


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


class AsyncToolExecutor:
    def __init__(self, registry: ToolRegistry, *, max_threads: int = 32):
        self.registry = registry
        # 有上限的线程池：同步工具再多，也不会无限开线程把进程拖垮（这也是一种背压）
        self._threads = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="agentkit-tool")

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        t = self.registry.get(call.name)
        if t is None:
            return self.registry.not_found(call.name)
        kwargs, error = t.parse_arguments(call.arguments)
        if error is not None:
            return ToolResult(False, error, "invalid_args")
        if t.wants_ctx:
            kwargs["ctx"] = ctx

        store = self.registry.idempotency_store
        use_idem = store is not None and t.risk in ("write", "dangerous")
        if use_idem:
            cached = await maybe_await(store.get(ctx.idempotency_key))  # 同步或异步的幂等存储都支持
            if cached is not None:
                return cached

        try:
            if getattr(t, "isolation", None) == "process":
                output = await run_in_subprocess(t.fn, kwargs, t.timeout_s)
            elif inspect.iscoroutinefunction(t.fn):
                output = await asyncio.wait_for(t.fn(**kwargs), t.timeout_s)
            else:
                loop = asyncio.get_running_loop()
                runner = functools.partial(contextvars.copy_context().run, t.fn, **kwargs)
                output = await asyncio.wait_for(loop.run_in_executor(self._threads, runner), t.timeout_s)
        except asyncio.TimeoutError:
            return timeout_result(t)
        except Exception as e:  # noqa: BLE001 —— 工具异常变成观察；CancelledError 是 BaseException，会正常穿透
            return exception_result(t, e)

        result = ToolResult(True, format_output(t, output))
        if use_idem:
            await maybe_await(store.put(ctx.idempotency_key, result))
        return result

    def close(self) -> None:
        self._threads.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------------------------------ 进程隔离


def _subprocess_entry(conn, fn, kwargs) -> None:
    try:
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
    ctx = multiprocessing.get_context("spawn")  # spawn 比 fork 安全：fork 一个带着事件循环和线程的进程容易死锁
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_subprocess_entry, args=(child, fn, kwargs), daemon=True)
    proc.start()
    child.close()
    loop = asyncio.get_running_loop()
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
        proc.join(timeout=2)
        parent.close()
    if kind == "ok":
        return payload
    if kind == "tool_error":
        raise ToolError(payload)
    raise RuntimeError(payload)
