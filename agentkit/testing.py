"""课程练习的测试辅助。

每节课目录里有 exercise.py（你来写）和 solution.py（参考答案）。
测试默认加载 exercise.py；设置环境变量 AGENTKIT_SOLUTION=1 则加载 solution.py（CI 用它保证测试本身正确）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from .tools import tool


def load_sibling(anchor_file: str, name: str) -> ModuleType:
    """加载与 anchor_file 同目录的模块 name.py，模块名带上课程目录前缀，避免不同课程的同名模块互相覆盖。"""
    here = Path(anchor_file).resolve().parent
    mod_name = f"_lesson_{here.name}_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, here / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def load_exercise(test_file: str, name: str = "exercise") -> ModuleType:
    here = Path(test_file).resolve().parent
    use_solution = os.environ.get("AGENTKIT_SOLUTION") == "1"
    filename = "solution.py" if use_solution and name == "exercise" else f"{name}.py"
    path = here / filename
    mod_name = f"_lesson_{here.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------------------------
# 第四部分：在本机不装 Docker 也能跑真实的基础设施（测试和 demo 共用）
# ---------------------------------------------------------------------------------------------


@contextmanager
def embedded_postgres():
    """启动一个嵌入式 Postgres（pip 包 pgserver，自带 pgvector），yield 连接 URI，退出时删除数据目录。

    这是真正的 Postgres 进程（不是模拟），通过 unix socket 连接，同一台机器上的多个进程都能共享。
    生产中换成托管 Postgres（RDS / Cloud SQL）或自建集群，代码只需要换连接串。
    """
    pgserver = importlib.import_module("pgserver")
    data_dir = tempfile.mkdtemp(prefix="agentkit_pg_")
    # pgserver 启动时会用 psutil 扫描进程命令行；macOS 上别的进程恰好在退出时会偶发
    # SystemError / PermissionError(KERN_PROCARGS2)（多个测试并行时实测约 1/15），和 Postgres 本身无关：重试几次
    flaky: tuple = (SystemError, PermissionError)
    try:
        flaky += (importlib.import_module("psutil").Error,)
    except ImportError:
        pass
    for attempt in range(4):
        try:
            server = pgserver.get_server(data_dir, cleanup_mode="delete")
            break
        except flaky:
            if attempt == 3:
                raise
            time.sleep(0.2 * (attempt + 1))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


def create_database(uri: str, name: str | None = None) -> str:
    """在已有 Postgres 上新建一个数据库，返回它的连接 URI（测试隔离用：每个测试一个干净的库）。"""
    psycopg = importlib.import_module("psycopg")
    name = name or f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    head, sep, tail = uri.partition("?")
    base = head.rsplit("/", 1)[0]
    return f"{base}/{name}{sep}{tail}"


@contextmanager
def fake_redis_server():
    """启动一个 fakeredis TCP 服务，yield redis:// URL。多个进程可以同时连接（支持 Lua 脚本）。

    fakeredis 是 Redis 协议的 Python 实现，适合测试和教学；生产中换成真正的 Redis / Valkey 集群，
    代码只需要换 URL。注意：它不模拟持久化、主从切换这些"真 Redis 才有"的故障，相关讨论见第 26 课。
    """
    fakeredis = importlib.import_module("fakeredis")

    class _Server(fakeredis.TcpFakeServer):
        request_queue_size = 128  # 默认监听 backlog 只有 5，多进程/多线程同时连接时会被拒

    server = _Server(("127.0.0.1", 0), server_type="redis")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"redis://{host}:{port}/0"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------------------------
# 进程隔离工具的示例函数：子进程要能按模块名 import 到函数，所以放在可导入的模块里
# ---------------------------------------------------------------------------------------------


def busy_loop(seconds: float) -> str:
    """纯 CPU 死循环（没有任何 await 点）：线程杀不掉、协程取消不了，只有杀进程才能停下。"""
    import time as _time

    end = _time.time() + seconds
    n = 0
    while _time.time() < end:
        n += 1
    return f"算了 {n} 次"


def whoami_pid() -> int:
    return os.getpid()


@tool(isolation="process", timeout_s=10)
def isolated_pid() -> int:
    """在子进程里返回自己的 pid。@tool(isolation="process") 直接装饰模块级函数的写法（回归测试用）。"""
    return os.getpid()
