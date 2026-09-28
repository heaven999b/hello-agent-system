"""在本机拉起一组真正的 worker 进程，并对它们做故障注入（第 13 课）。

    pool = WorkerPool("sqlite:///runs/jobs.db", app="app.py:make_handler", n=3, concurrency=8, lease=2)
    with pool:                        # 启动 3 个独立的操作系统进程（python -m agentkit.distributed.worker ...）
        pool.kill(0)                  # SIGKILL：进程当场消失，来不及做任何收尾 —— 它手上的任务靠租约过期被别人接手
        pool.pause(1)                 # SIGSTOP：进程被冻结（模拟长时间 GC 停顿、虚拟机被挂起、网络分区）——
        ...                           #          它还"以为"自己持有租约，醒来后的写入要被 fence 拒绝
        pool.resume(1)                # SIGCONT：解冻
        pool.terminate(2)             # SIGTERM：优雅停机（K8s 删除 Pod），停止领取、做完在途任务再退出
        events = pool.events()        # 每个进程打到标准输出的 JSON 事件，用来断言"谁领了什么、谁被拒绝了"
    # 退出 with：对还活着的进程发 SIGTERM，超时仍不退出的发 SIGKILL

这些都是真实的操作系统信号，作用在真实的进程上：没有 sleep 模拟，也没有"用线程假装进程"。
局限：所有进程在同一台机器上（多机见第 26 课的 Postgres 版本与网络分区注入）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[2]


class WorkerProcess:
    """一个 worker 进程的句柄。"""

    def __init__(self, index: int, worker_id: str, cmd: list[str], log_path: Path, env: dict):
        self.index, self.worker_id, self.cmd, self.log_path, self.env = index, worker_id, cmd, log_path, env
        self.proc: subprocess.Popen | None = None
        self.restarts = 0

    def start(self) -> "WorkerProcess":
        log = open(self.log_path, "ab")  # noqa: SIM115 —— 由子进程持有，退出时关闭
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=log, stderr=subprocess.STDOUT, env=self.env, cwd=os.getcwd())
        finally:
            log.close()  # 子进程已经继承了文件描述符，父进程这边可以关掉
        return self

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def signal(self, sig: int) -> None:
        if self.alive:
            os.kill(self.proc.pid, sig)

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.proc.wait(timeout) if self.proc else None
        except subprocess.TimeoutExpired:
            return None

    def events(self) -> list[dict]:
        """这个进程打到标准输出的 JSON 事件（非 JSON 的行，比如 traceback，放在 {"event": "_log", "line": ...} 里）。"""
        out = []
        if not self.log_path.exists():
            return out
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
                out.append(ev if isinstance(ev, dict) and "event" in ev else {"event": "_log", "line": line})
            except json.JSONDecodeError:
                out.append({"event": "_log", "line": line})
        return out

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""


class WorkerPool:
    """拉起 n 个 `python -m agentkit.distributed.worker` 进程。参数与 worker 的命令行一一对应。"""

    def __init__(
        self,
        queue_url: str,
        app: str,
        n: int = 2,
        *,
        concurrency: int = 8,
        lease: float = 30.0,
        poll: float = 0.2,
        grace: float = 10.0,
        kinds: Iterable[str] | None = None,
        options: dict | None = None,
        env: dict | None = None,
        log_dir: str | Path | None = None,
        name: str = "w",
        python: str = sys.executable,
    ):
        self.queue_url, self.app, self.n = queue_url, app, n
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.mkdtemp(prefix="agentkit_workers_"))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        base_env = dict(os.environ)
        # 子进程要能 import agentkit：把仓库根目录放到 PYTHONPATH 最前面（已 pip install -e 时也无害）
        base_env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), base_env.get("PYTHONPATH")]))
        base_env["PYTHONUNBUFFERED"] = "1"
        base_env.update(env or {})
        self._env = base_env
        self._common = [
            "--queue", queue_url, "--app", app, "--concurrency", str(concurrency), "--lease", str(lease),
            "--poll", str(poll), "--grace", str(grace),
        ]
        if kinds:
            self._common += ["--kinds", ",".join(kinds)]
        for k, v in (options or {}).items():
            self._common += ["--opt", f"{k}={v}"]
        self.python = python
        self.workers = [self._make(i, f"{name}{i}") for i in range(n)]

    def _make(self, index: int, worker_id: str) -> WorkerProcess:
        cmd = [self.python, "-m", "agentkit.distributed.worker", *self._common, "--worker-id", worker_id]
        return WorkerProcess(index, worker_id, cmd, self.log_dir / f"{worker_id}.log", self._env)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "WorkerPool":
        for w in self.workers:
            w.start()
        return self

    def add(self) -> WorkerProcess:
        """扩容：再拉起一个 worker（第 31 课的水平扩展就是在 K8s 里做这件事）。"""
        w = self._make(len(self.workers), f"{self.workers[0].worker_id.rstrip('0123456789')}{len(self.workers)}")
        self.workers.append(w)
        return w.start()

    def restart(self, i: int) -> WorkerProcess:
        """用同一个 worker_id 重新拉起第 i 个进程（模拟 K8s 重启崩溃的 Pod）。"""
        w = self.workers[i]
        if w.alive:
            raise RuntimeError(f"worker {w.worker_id} 还活着，先 kill / terminate")
        w.restarts += 1
        return w.start()

    def stop(self, timeout: float = 15.0) -> list[int | None]:
        """优雅停机：先对所有活着的进程发 SIGTERM（先解冻被 SIGSTOP 的），等 timeout 秒，仍未退出的 SIGKILL。"""
        for w in self.workers:
            if w.alive:
                w.signal(signal.SIGCONT)
                w.signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout
        for w in self.workers:
            if w.proc is not None and w.wait(max(0.0, deadline - time.monotonic())) is None:
                w.signal(signal.SIGKILL)
                w.wait(5)
        return [w.returncode for w in self.workers]

    def __enter__(self) -> "WorkerPool":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------ 故障注入

    def kill(self, i: int) -> None:
        """SIGKILL（kill -9）：进程当场死亡，不执行任何 finally / 信号处理 / 收尾。"""
        self.workers[i].signal(signal.SIGKILL)
        self.workers[i].wait(5)

    def pause(self, i: int) -> None:
        """SIGSTOP：冻结进程（不能被捕获或忽略）。它的心跳停了，但它自己不知道。"""
        self.workers[i].signal(signal.SIGSTOP)

    def resume(self, i: int) -> None:
        """SIGCONT：解冻。进程从被冻结的那一行继续执行，手里拿着可能早已过期的租约。"""
        self.workers[i].signal(signal.SIGCONT)

    def terminate(self, i: int) -> None:
        """SIGTERM：请求优雅停机（停止领取、做完在途任务、退出）。"""
        self.workers[i].signal(signal.SIGTERM)

    # ------------------------------------------------------------------ 观测

    @property
    def pids(self) -> list[int | None]:
        return [w.pid for w in self.workers]

    def events(self, name: str | None = None) -> list[dict]:
        """所有进程的事件，按时间排序；name 只保留某一种事件（例如 "fence_rejected"）。"""
        evs = [e for w in self.workers for e in w.events() if e.get("event") != "_log"]
        evs.sort(key=lambda e: e.get("t", 0))
        return [e for e in evs if name is None or e.get("event") == name]

    def logs(self) -> str:
        """所有进程的原始输出（排查问题用：异常的 traceback 在这里）。"""
        return "\n".join(f"===== {w.worker_id} (pid {w.pid}) =====\n{w.log_text()}" for w in self.workers)
