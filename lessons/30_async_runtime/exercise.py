"""第 30 课练习：写对 async 代码的三项基本功。

(a) bounded_gather   有上限的并发 + 结构化并发的出错 / 取消语义（AsyncAgent 并行工具、批量评估都要用）
(b) with_deadline    截止时间：超时就取消内部协程，但**绝不吞掉**外部的取消
(c) detect_blocking  用 ast 找出 async 函数里的阻塞调用（一个简化版的 lint，对应场景 3 的事故）

运行测试：make lesson N=30    或    .venv/bin/python -m pytest lessons/30_async_runtime -v

要求兼容 Python 3.10（CI 在 3.10 / 3.11 / 3.12 上跑）：不能用 asyncio.TaskGroup 和 asyncio.timeout（3.11 才有）。
"""

from __future__ import annotations

import ast
import asyncio
from typing import Any, Awaitable, Callable, Iterable, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------------------------
# (a) bounded_gather
# ---------------------------------------------------------------------------------------------


async def bounded_gather(coro_factories: Iterable[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    """并发执行每个工厂函数返回的协程，同一时刻最多 limit 个在跑。

    为什么传"工厂函数"而不是协程？协程对象一创建就占内存；一次传进来 10 万个协程，
    还没开始跑内存就爆了。工厂函数让你**拿到名额时才创建**协程 —— 这就是背压。

    要求：
    1. 返回值按**输入顺序**排列（不是完成顺序）；
    2. 同时在跑的数量 ≤ limit；工厂函数只在拿到名额时才调用；
    3. 任何一个抛出异常：取消其余还在跑的，**等它们收尾结束**，然后抛出第一个异常**本身**
       （不是 ExceptionGroup，也不要包装成新异常）；
    4. 调用方被取消：取消所有子任务、等它们收尾结束，然后 CancelledError 继续往外抛；
    5. limit < 1 抛 ValueError；输入为空返回 []。

    "等它们收尾结束"就是结构化并发的核心：bounded_gather 返回（或抛出）时，不能还有子任务在后台跑。

    提示：一种干净的写法是"min(limit, n) 个 worker 从同一个迭代器里领活"；
    asyncio.wait(..., return_when=asyncio.FIRST_EXCEPTION) 可以在第一个失败时醒来；
    asyncio.gather(*tasks, return_exceptions=True) 可以等一批被取消的任务全部结束。
    """
    raise NotImplementedError("TODO: 实现 bounded_gather")


# ---------------------------------------------------------------------------------------------
# (b) with_deadline
# ---------------------------------------------------------------------------------------------


async def with_deadline(coro: Awaitable[T], seconds: float, on_timeout: Callable[[], T] | Any) -> T:
    """最多等 seconds 秒。

    - coro 按时完成：返回它的结果；它抛异常：原样抛出（**包括它自己抛出的 TimeoutError** ——
      那是下游的超时错误，不是"我们的截止时间到了"，不能被当成超时吞掉）；
    - 截止时间到了：取消 coro、等它收尾结束，然后返回 on_timeout 的值
      （on_timeout 可调用就返回 on_timeout()，否则直接返回 on_timeout；没超时就不要调用它）；
    - 调用方被取消（外部取消）：内部协程也要被取消，并且 CancelledError **必须**继续往外抛 ——
      绝不能把外部取消当成超时，返回一个默认值；
    - 内部结果和外部取消在同一轮事件循环里同时到达时，也以取消为准。Python 3.11 及更早的 asyncio.wait_for
      在这里有竞态（CPython gh-86296）：它返回结果、吞掉取消。第 30 课在 AsyncAgent 里实测到了这个问题。

    提示：常见的错误写法是
        try: return await asyncio.wait_for(coro, seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError): return on_timeout
    它同时犯了两个错：吞掉外部取消；把下游自己的 TimeoutError 当成截止时间。
    用 asyncio.wait({task}, timeout=seconds) 可以区分"我的截止时间到了"和"任务自己抛了 TimeoutError"，
    而且它不会像 3.11 的 wait_for 那样吞掉取消。
    """
    raise NotImplementedError("TODO: 实现 with_deadline")


# ---------------------------------------------------------------------------------------------
# (c) detect_blocking
# ---------------------------------------------------------------------------------------------

# 在 async 函数里调用会卡住整个事件循环的函数（完整模块路径）
BLOCKING_CALLS = {
    "time.sleep",  # → await asyncio.sleep(...)
    "open",  # 同步文件 IO → await asyncio.to_thread(...) 或 aiofiles
    "urllib.request.urlopen",  # → httpx.AsyncClient
    "subprocess.run",  # → asyncio.create_subprocess_exec
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
}
BLOCKING_PREFIXES = ("requests.",)  # requests 库的任何调用（get / post / Session ……）→ httpx.AsyncClient / aiohttp


def detect_blocking(source: str) -> list[str]:
    """分析一段 Python 源码，找出 async 函数里的阻塞调用。

    返回形如 "函数名:行号:调用名" 的字符串列表，按行号（同一行按列）排序，例如 ["handler:5:time.sleep"]。

    规则：
    1. 只检查 async def 的函数体；普通 def 里的调用不算（它们可能本来就跑在线程里）；
    2. async def 里嵌套的普通 def 和 lambda 不算（常见用法是把它交给 asyncio.to_thread）；
       嵌套的 async def 要检查，报告的是内层函数的名字；
    3. 解析 import 别名，把调用名规范化成完整模块路径：
         import time as t            → t.sleep(1)      报告为 time.sleep
         from time import sleep      → sleep(1)        报告为 time.sleep
         from requests import get    → get(url)        报告为 requests.get
         import urllib.request       → urllib.request.urlopen(url) 报告为 urllib.request.urlopen
         from aiofiles import open   → open(p)         规范化为 aiofiles.open，不是内置 open，不报告
    4. 直接被 await 的调用不算（await 的对象本身就是可等待的，例如 await client.get(url)）；
       只作为参数传递的函数引用也不算，例如 asyncio.to_thread(time.sleep, 1) 里的 time.sleep 并没有被调用；
    5. 规范化后的名字在 BLOCKING_CALLS 里，或以 BLOCKING_PREFIXES 中的某个前缀开头，就算阻塞调用。

    简化：import 别名按整个文件收集，不区分作用域。

    提示：先 ast.walk 一遍收集 import 别名；再写一个递归函数，带着"当前所在的 async 函数名（或 None）"遍历子节点。
    """
    raise NotImplementedError("TODO: 实现 detect_blocking")
