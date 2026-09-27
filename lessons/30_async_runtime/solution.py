"""第 30 课参考答案。接口与 exercise.py 完全一致。"""

from __future__ import annotations

import ast
import asyncio
from typing import Any, Awaitable, Callable, Iterable, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------------------------
# (a) bounded_gather
# ---------------------------------------------------------------------------------------------


async def bounded_gather(coro_factories: Iterable[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    if limit < 1:
        raise ValueError("limit 至少为 1")
    factories = list(coro_factories)
    results: list = [None] * len(factories)
    if not factories:
        return results

    jobs = iter(enumerate(factories))
    first_error: list[BaseException] = []

    async def worker() -> None:
        # 所有 worker 共享同一个迭代器：谁空闲谁领下一个。两次 next() 之间没有 await，所以不会领重。
        # 工厂函数在这里才被调用 —— 拿到名额才创建协程，这就是背压。
        for i, factory in jobs:
            try:
                results[i] = await factory()
            except Exception as e:  # CancelledError 是 BaseException，不会被记成"第一个失败"
                if not first_error:
                    first_error.append(e)
                raise

    workers = [asyncio.ensure_future(worker()) for _ in range(min(limit, len(factories)))]
    try:
        await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
    except asyncio.CancelledError:
        await _cancel_and_wait(workers)  # 调用方取消：子任务一个都不能留在后台
        raise
    if first_error:
        await _cancel_and_wait(workers)
        raise first_error[0]
    return results


async def _cancel_and_wait(tasks: list[asyncio.Future]) -> None:
    for t in tasks:
        t.cancel()
    # return_exceptions=True：子任务的 CancelledError / 异常都收进结果里，确保"全部结束"才返回
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------------------------
# (b) with_deadline
# ---------------------------------------------------------------------------------------------


async def with_deadline(coro: Awaitable[T], seconds: float, on_timeout: Callable[[], T] | Any) -> T:
    task = asyncio.ensure_future(coro)
    try:
        # asyncio.wait 不会取消 task，也不会抛出 task 的异常：超时只体现为"task 不在 done 里"
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except asyncio.CancelledError:
        task.cancel()  # 外部取消：内部协程也要停
        await asyncio.wait({task})  # 等它收尾；如果此时再次被取消，CancelledError 会直接抛出去
        raise  # 外部取消必须继续往外抛
    if task in done:
        return task.result()  # 正常结果，或原样抛出它自己的异常（包括它自己的 TimeoutError）
    task.cancel()
    await asyncio.wait({task})  # 等内部协程处理完 CancelledError（释放连接、回滚……）
    if not task.cancelled():
        return task.result()  # 它在截止时间的同一刻恰好完成了（或者吞掉了取消）：用它的真实结果
    return on_timeout() if callable(on_timeout) else on_timeout


# ---------------------------------------------------------------------------------------------
# (c) detect_blocking
# ---------------------------------------------------------------------------------------------

BLOCKING_CALLS = {
    "time.sleep",
    "open",
    "urllib.request.urlopen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
}
BLOCKING_PREFIXES = ("requests.",)


def detect_blocking(source: str) -> list[str]:
    tree = ast.parse(source)

    # 1) import 别名：局部名字 → 完整模块路径
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name  # import urllib.request as ur → ur = urllib.request
                # 没有 asname 时（import urllib.request），名字 urllib 就是模块本身，无需映射
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    # 2) 直接被 await 的调用
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)}

    def resolve(func: ast.expr) -> str | None:
        parts: list[str] = []
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None  # 例如 get_client().get(...)：静态分析无法知道它是什么
        return ".".join([aliases.get(func.id, func.id), *reversed(parts)])

    found: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                visit(child, None)  # 嵌套的同步函数通常会被交给线程执行
            else:
                if current is not None and isinstance(child, ast.Call) and id(child) not in awaited:
                    name = resolve(child.func)
                    if name is not None and (name in BLOCKING_CALLS or name.startswith(BLOCKING_PREFIXES)):
                        found.append((child.lineno, child.col_offset, f"{current}:{child.lineno}:{name}"))
                visit(child, current)

    visit(tree, None)
    return [text for _, _, text in sorted(found)]
