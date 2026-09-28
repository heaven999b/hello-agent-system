"""迷你部署：在一台机器上用真实的进程拼出参考架构的骨架。

    客户端 ──HTTP──► API 进程 × N（uvicorn，python -m uvicorn mini_api:app）
                       │ 鉴权 · 按租户限流 · 路由 · 入队（202）
                       ▼
                 SQLite 文件（任务队列 · 检查点 · 跨进程令牌桶 / 槽位）
                       ▲
                       │ 领取 · 心跳 · 检查点 · 提交
               worker 进程 × M（WorkerPool → python -m agentkit.distributed.worker）

每个 API 进程、每个 worker 进程都是独立的操作系统进程，它们之间只通过这个 SQLite 文件协作。
和真实部署的差别：所有进程在一台机器上、SQLite 只允许一个写者（第 13 课 3.12 节实测了上限）、
没有负载均衡器（客户端自己轮流发给各个 API 进程）。多机版见第 26、31 课。

需要可选依赖：pip install -e ".[server]"（FastAPI、uvicorn、httpx）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentkit.distributed import SQLiteCheckpointer, SQLiteJobQueue, WorkerPool

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
INSTALL_HINT = 'pip install -e ".[server]"'


def missing_server_deps() -> list[str]:
    return [m for m in ("fastapi", "uvicorn", "httpx") if importlib.util.find_spec(m) is None]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ApiProcess:
    name: str
    backend: str
    port: int
    popen: subprocess.Popen
    log_path: Path

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self.popen.poll() is None


class MiniDeployment:
    """apis：[(名字, 限流后端)]，后端是 memory（每个进程自己的桶）或 sqlite（所有进程共用一个桶）。"""

    def __init__(self, workdir: str | Path, *, apis: list[tuple[str, str]], workers: int = 2, lease: float = 2.0,
                 offline: bool = True, router: str | Path | None = None, worker_concurrency: int = 8,
                 worker_options: dict | None = None):
        self.workdir = Path(workdir)
        self.db = self.workdir / "jobs.db"
        self.api_specs, self.n_workers, self.lease = apis, workers, lease
        self.offline, self.router = offline, str(router or HERE / "solution.py")
        self.worker_concurrency = worker_concurrency
        self.worker_options = {"offline": "1" if offline else "0", **(worker_options or {})}
        self.apis: dict[str, ApiProcess] = {}
        self.pool: WorkerPool | None = None

    async def start(self, timeout: float = 90.0) -> "MiniDeployment":
        """拉起所有进程并等它们就绪；中途失败时先把已经拉起的进程停掉再抛出。"""
        try:
            return await self._start(timeout)
        except BaseException:
            await self.stop()
            raise

    async def _start(self, timeout: float) -> "MiniDeployment":
        import httpx

        self.workdir.mkdir(parents=True, exist_ok=True)
        # 先由这一个进程建好数据库和表（切换 WAL 模式也在这时完成），再拉起其他进程：
        # 几个进程同时新建同一个 SQLite 文件时，有进程会直接收到 "database is locked"
        queue, ckpt = SQLiteJobQueue(self.db), SQLiteCheckpointer(self.db)
        await queue.setup()
        await ckpt.setup()
        await queue.close()
        await ckpt.close()

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
        env.update(PYTHONUNBUFFERED="1", MINI_DB=str(self.db), MINI_ROUTER=self.router,
                   MINI_OFFLINE="1" if self.offline else "0")
        for name, backend in self.api_specs:
            port = free_port()
            log_path = self.workdir / f"{name}.log"
            with open(log_path, "ab") as log:
                popen = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "mini_api:app", "--app-dir", str(HERE), "--host", "127.0.0.1",
                     "--port", str(port), "--log-level", "info", "--no-access-log"],
                    stdout=log, stderr=subprocess.STDOUT, env={**env, "MINI_NAME": name, "MINI_RATE_BACKEND": backend},
                )
            self.apis[name] = ApiProcess(name, backend, port, popen, log_path)

        self.pool = WorkerPool(f"sqlite:///{self.db}", f"{HERE / 'worker_app.py'}:make_handler", n=self.n_workers,
                               concurrency=self.worker_concurrency, lease=self.lease, poll=0.05, grace=10,
                               options=self.worker_options, log_dir=self.workdir / "logs", name="worker-")
        self.pool.start()

        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            for api in self.apis.values():
                while True:
                    if not api.alive:
                        raise RuntimeError(f"API 进程 {api.name} 启动失败：\n{api.log_path.read_text()[-3000:]}")
                    try:
                        if (await client.get(f"{api.url}/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{timeout}s 内 API 进程 {api.name} 没有就绪")
                    await asyncio.sleep(0.1)
        while len(self.pool.events("started")) < self.n_workers:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout}s 内 worker 没有全部启动：\n{self.pool.logs()[-3000:]}")
            await asyncio.sleep(0.05)
        return self

    async def stop(self, timeout: float = 10.0) -> dict[str, int | None]:
        """先停 API（SIGTERM：不再接新请求，处理完手上的请求再退出），再停 worker（SIGTERM：排空在途任务）。
        超时仍未退出的进程 SIGKILL。返回各进程的退出码。"""
        codes: dict[str, int | None] = {}
        for api in self.apis.values():
            if api.alive:
                api.popen.send_signal(signal.SIGTERM)
        for api in self.apis.values():
            try:
                codes[api.name] = await asyncio.to_thread(api.popen.wait, timeout)
            except subprocess.TimeoutExpired:
                api.popen.kill()
                codes[api.name] = await asyncio.to_thread(api.popen.wait)
        if self.pool is not None:
            for w, code in zip(self.pool.workers, await asyncio.to_thread(self.pool.stop, timeout)):
                codes[w.worker_id] = code
        return codes

    async def __aenter__(self) -> "MiniDeployment":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def logs(self) -> str:
        parts = [f"===== {a.name} =====\n{a.log_path.read_text(errors='replace')}" for a in self.apis.values()
                 if a.log_path.exists()]
        if self.pool is not None:
            parts.append(self.pool.logs())
        return "\n".join(parts)
