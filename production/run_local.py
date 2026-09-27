"""本机无 Docker 一键启动参考服务：嵌入式 Postgres + fakeredis TCP 服务 + 1 个 API 进程 + N 个 worker 进程。

    python production/run_local.py                      # 离线（剧本模型，带延迟），3 个 worker
    python production/run_local.py --workers 2 --llm litellm --concurrency 1   # 真实模型（走 .env 里的网关）

启动后打印访问地址、每个演示身份的 API key、日志目录；Ctrl-C 依次优雅停止：API（停止接新请求）→ worker（SIGTERM 排空）
→ Redis → Postgres。

和生产的差别（讲义 3.1 节如实列出）：
- 所有进程在一台机器上，Postgres 是真的（pgserver），但单机、没有复制；
- Redis 是 fakeredis（Python 实现的协议兼容服务），不模拟持久化、主从切换、集群分片，吞吐也远低于真 Redis；
- 指标用 prometheus_client 的多进程模式汇总（共享目录），K8s 里是每个 Pod 各自被抓取。

LocalStack 也被压测（loadtest.py）、端到端测试（tests/test_e2e.py）和第 31 课的 demo 直接调用。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIRED = ["psycopg", "psycopg_pool", "pgserver", "fakeredis", "redis", "fastapi", "uvicorn", "httpx",
            "prometheus_client", "opentelemetry.sdk", "cedarpy"]
INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'


def missing_dependencies() -> list[str]:
    import importlib.util

    out = []
    for m in REQUIRED:
        try:
            if importlib.util.find_spec(m) is None:
                out.append(m)
        except ModuleNotFoundError:
            out.append(m)
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url: str, timeout: float = 1.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen
    log_path: Path
    health_port: int | None = None
    metrics_port: int | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def alive(self) -> bool:
        return self.popen.poll() is None


class LocalStack:
    def __init__(self, *, workers: int = 3, concurrency: int = 8, llm: str = "scripted", latency_ms: float = 300,
                 env: dict | None = None, api_port: int | None = None, quiet: bool = True):
        self.n_workers = workers
        self.concurrency = concurrency
        self.llm = llm
        self.latency_ms = latency_ms
        self.extra_env = dict(env or {})
        self.api_port = api_port or free_port()
        self.quiet = quiet
        self.workers: list[Proc] = []
        self.retired: list[Proc] = []
        self.api: Proc | None = None
        self._stack = contextlib.ExitStack()
        self._worker_seq = 0
        self.keys: dict[str, str] = {}

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "LocalStack":
        from agentkit.testing import create_database, embedded_postgres, fake_redis_server
        from production.service.identity import demo_keys
        from production.service.migrate import migrate_all

        t0 = time.time()
        self.run_dir = Path(tempfile.mkdtemp(prefix="itdesk-local-"))
        for sub in ("logs", "prom", "spans"):
            (self.run_dir / sub).mkdir()
        try:
            self.pg_server = self._stack.enter_context(embedded_postgres())
            self.database_url = create_database(self.pg_server, "itdesk")
            self.redis_url = self._stack.enter_context(fake_redis_server())
            asyncio.run(migrate_all(self.database_url))
            self.keys, hashed = demo_keys()
            self.base_env = self._base_env(hashed)
            self.api = self._spawn_api()
            for _ in range(self.n_workers):
                self.start_worker()
            self.wait_ready()
        except BaseException:
            self.stop()
            raise
        self.startup_seconds = time.time() - t0
        return self

    def __enter__(self) -> "LocalStack":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def _base_env(self, hashed_keys: dict) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROMETHEUS_MULTIPROC")}
        env.update({
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            "PYTHONUNBUFFERED": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "DATABASE_URL": self.database_url,
            "REDIS_URL": self.redis_url,
            "API_KEYS_JSON": json.dumps(hashed_keys),
            "LLM_BACKEND": self.llm,
            "SCRIPTED_LLM_LATENCY_MS": str(self.latency_ms),
            "PROMETHEUS_MULTIPROC_DIR": str(self.run_dir / "prom"),
            "SPANS_JSONL_DIR": str(self.run_dir / "spans"),
            "DEPLOYMENT_ENV": "local",
            "WORKER_CONCURRENCY": str(self.concurrency),
            "WORKER_HEALTH_ADDR": "127.0.0.1",
            "METRICS_ADDR": "127.0.0.1",
            # 本机默认值比生产短：故障注入后几秒内就能看到接手（生产：租约 30s、宽限 20s，见 deploy/k8s）
            "WORKER_LEASE_SECONDS": "6",
            "WORKER_GRACE_SECONDS": "1",  # 比长任务（诊断 + 建单）短：滚动重启时能看到"取消 → 归还 → 接手"
            "WORKER_POLL_SECONDS": "0.1",
            "PG_POOL_MAX": "8",
            "API_RATE_PER_SEC": "50",
            "API_BURST": "100",
            "API_RATE_OVERRIDES_JSON": json.dumps({"noisy": [1, 2]}),  # "吵闹租户"配额很小：演示 429
            "TOOL_LATENCY_MS": "400",  # 下游（工单系统）响应 400ms：副作用已提交、响应未返回的窗口，故障注入容易落在这里
        })
        env.update({k: str(v) for k, v in self.extra_env.items()})
        return env

    def _popen(self, name: str, args: list[str], env: dict) -> tuple[subprocess.Popen, Path]:
        log_path = self.run_dir / "logs" / f"{name}.log"
        f = open(log_path, "ab")
        # start_new_session：终端里的 Ctrl-C 只发给本进程，由本进程按顺序停子进程（而不是所有人同时收到 SIGINT）
        p = subprocess.Popen(args, cwd=REPO_ROOT, env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        f.close()
        return p, log_path

    def _spawn_api(self) -> Proc:
        args = [sys.executable, "-m", "uvicorn", "--factory", "production.service.api:create_app",
                "--host", "127.0.0.1", "--port", str(self.api_port), "--timeout-graceful-shutdown", "5",
                "--log-level", "warning", "--no-access-log"]
        env = {**self.base_env, "POD_NAME": "api-0", "SERVICE_NAME": "itdesk-api"}
        p, log = self._popen("api-0", args, env)
        return Proc("api-0", p, log)

    def start_worker(self) -> Proc:
        name = f"worker-{self._worker_seq}"
        self._worker_seq += 1
        health, metrics = free_port(), free_port()
        env = {**self.base_env, "POD_NAME": name, "SERVICE_NAME": "itdesk-worker",
               "WORKER_HEALTH_PORT": str(health), "METRICS_PORT": str(metrics)}
        p, log = self._popen(name, [sys.executable, "-m", "production.service.worker"], env)
        proc = Proc(name, p, log, health_port=health, metrics_port=metrics)
        self.workers.append(proc)
        return proc

    def wait_ready(self, timeout: float = 60.0, procs: list[Proc] | None = None) -> None:
        deadline = time.time() + timeout
        targets = procs if procs is not None else [self.api, *self.workers]
        for proc in targets:
            url = f"{self.api_url}/readyz" if proc is self.api else f"http://127.0.0.1:{proc.health_port}/readyz"
            while True:
                if not proc.alive():
                    raise RuntimeError(f"{proc.name} 启动失败，日志：{proc.log_path}\n{proc.log_path.read_text()[-3000:]}")
                code, _ = http_get(url)
                if code == 200:
                    break
                if time.time() > deadline:
                    raise TimeoutError(f"{proc.name} 在 {timeout}s 内没有就绪，日志：{proc.log_path}")
                time.sleep(0.1)

    # ------------------------------------------------------------------ 故障注入

    def kill_worker(self, proc: Proc) -> None:
        """kill -9：进程什么都来不及做（不归还任务、不写 cancelled），只能靠租约过期 + 检查点。"""
        proc.popen.send_signal(signal.SIGKILL)
        proc.popen.wait()
        self._retire(proc)

    def terminate_worker(self, proc: Proc, wait: bool = True, timeout: float = 30.0) -> int | None:
        """SIGTERM：优雅停机（排空 → 取消 → 归还 → 退出）。"""
        proc.popen.send_signal(signal.SIGTERM)
        if not wait:
            return None
        code = proc.popen.wait(timeout=timeout)
        self._retire(proc)
        return code

    def retire_if_exited(self) -> None:
        for proc in list(self.workers):
            if not proc.alive():
                self._retire(proc)

    def _retire(self, proc: Proc) -> None:
        if proc in self.workers:
            self.workers.remove(proc)
            self.retired.append(proc)
        # 等价于 gunicorn 的 child_exit 钩子：清掉它的 live* gauge 文件（计数器文件保留，总数不丢）
        with contextlib.suppress(Exception):
            from prometheus_client import multiprocess

            multiprocess.mark_process_dead(proc.pid, str(self.run_dir / "prom"))

    def worker_by_name(self, name: str) -> Proc | None:
        return next((w for w in self.workers if w.name == name), None)

    # ------------------------------------------------------------------ 停止

    def stop(self) -> None:
        procs = [p for p in [self.api, *self.workers] if p is not None and p.alive()]
        if self.api is not None and self.api.alive():
            self.api.popen.send_signal(signal.SIGTERM)  # 先停入口：不再接新请求
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.api.popen.wait(timeout=15)
        for w in self.workers:
            if w.alive():
                w.popen.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.popen.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.popen.kill()
                p.popen.wait()
        for w in list(self.workers):
            self._retire(w)
        self._stack.close()

    def cleanup(self) -> None:
        """删除日志、指标、span 目录（测试结束时调用；手动运行时保留，方便排查）。"""
        shutil.rmtree(getattr(self, "run_dir", ""), ignore_errors=True)

    # ------------------------------------------------------------------ 展示

    def describe(self) -> str:
        lines = [
            f"API            {self.api_url}   （/healthz  /readyz  /metrics  /docs）",
            f"Postgres       {self.database_url}",
            f"Redis          {self.redis_url}",
            f"日志           {self.run_dir / 'logs'}",
            "worker         " + ", ".join(f"{w.name}(pid {w.pid}, health :{w.health_port}, metrics :{w.metrics_port})" for w in self.workers),
            "API key（本次启动随机生成，只在本机有效）：",
        ]
        lines += [f"  {name:<14} {key}" for name, key in self.keys.items()]
        k = self.keys.get("acme-alice", "<key>")
        lines += [
            "试一试：",
            f"  curl -N -X POST {self.api_url}/v1/chat/stream -H 'Authorization: Bearer {k}' "
            "-H 'Content-Type: application/json' -d '{\"message\": \"VPN 连不上怎么办\"}'",
            f"  curl -X POST {self.api_url}/v1/runs -H 'Authorization: Bearer {k}' -H 'Idempotency-Key: demo-1' "
            "-H 'Content-Type: application/json' -d '{\"message\": \"屏幕闪烁，帮我提个工单\"}'",
        ]
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8, help="每个 worker 同时处理的任务数")
    parser.add_argument("--llm", choices=["scripted", "litellm", "openai"], default="scripted")
    parser.add_argument("--latency-ms", type=float, default=300, help="离线模型每次调用的延迟")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    missing = missing_dependencies()
    if missing:
        print(f"缺少依赖：{', '.join(missing)}。请先安装：{INSTALL_HINT}")
        return 0
    stack = LocalStack(workers=args.workers, concurrency=args.concurrency, llm=args.llm, latency_ms=args.latency_ms,
                       api_port=args.port, env={"LLM_MAX_CONCURRENCY": args.concurrency} if args.llm != "scripted" else None)
    print("正在启动：嵌入式 Postgres → fakeredis → 迁移 → API → worker ……", flush=True)
    stack.start()
    print(f"\n✅ 已就绪（{stack.startup_seconds:.1f}s）\n" + stack.describe() + "\n\nCtrl-C 停止。", flush=True)
    stopping = {"flag": False}

    def request_stop(*_):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stopping["flag"]:
            time.sleep(0.5)
            for w in list(stack.workers):
                if not w.alive():  # 像 Deployment 一样：worker 意外退出就补一个
                    print(f"⚠️  {w.name} 已退出（code {w.popen.returncode}），补一个新的 worker", flush=True)
                    stack._retire(w)
                    stack.start_worker()
    finally:
        print("\n正在停止：API → worker（SIGTERM，排空在途任务）→ Redis → Postgres ……", flush=True)
        stack.stop()
        print(f"已停止。日志保留在 {stack.run_dir / 'logs'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
