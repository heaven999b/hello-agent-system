"""ITBuddy 的本机多进程部署：API 进程（uvicorn，默认 1 个）+ N 个 worker 进程，它们之间只通过 SQLite 文件协作。

    .venv/bin/python capstone/deploy.py --offline            # 离线剧本模型，不需要 API key；Ctrl-C（或 SIGTERM）停止
    .venv/bin/python capstone/deploy.py --offline --demo     # 起部署 → 跑一遍演示（跨进程审批、kill -9 接手）→ 停止
    .venv/bin/python capstone/deploy.py --offline --bench 200 --workers 2   # 小压测：200 个运行，看吞吐和分工
    .venv/bin/python capstone/deploy.py --workers 3          # 真实模型（读 .env）
    .venv/bin/python capstone/deploy.py --offline --apis 2   # 2 个 API 副本（负载均衡器后面的样子；客户端自己选端口）

    客户端 ──HTTP──► API 进程（python -m uvicorn server:create_app --factory）  鉴权 · 入队 → 202 · 查询 · 审批
                        │ enqueue(run / resume)
                        ▼
      <dir>/itbuddy.db     任务队列（租约 + fence）· 检查点（fence 接管）· 幂等记录 · 审计（只追加）· 共享熔断器
                        ▲ claim / heartbeat / checkpoint / complete
      worker 进程 × N（python -m agentkit.distributed.worker --app capstone/worker_app.py:make_handler）
                        │ 工具调用（带 Idempotency-Key）
                        ▼
      <dir>/enterprise.db  模拟的 5 个企业系统：员工目录 / 工单 / 知识库 / 账号 / 系统状态

每个方框都是真实的操作系统进程，kill -9、SIGTERM 都是真实的信号。和多机部署的差别：所有进程在一台机器上、
SQLite 同一时刻只有一个写者、没有负载均衡器和网关。多机版本（Postgres + Redis + K8s）见 production/ 与第 26、31 课。

需要可选依赖：pip install -e ".[server]"（FastAPI、uvicorn、httpx）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from agentkit.distributed import SQLiteJobQueue, WorkerPool  # noqa: E402
from itbuddy import Backend, ITBuddyStores  # noqa: E402

DEFAULT_DIR = HERE / "runs" / "deploy"
INSTALL_HINT = '需要可选依赖：pip install -e ".[server]"（FastAPI、uvicorn、httpx）'
ROLES = {"alice": "员工", "dave": "员工（账号被锁）", "bob": "IT 管理员", "frank": "IT 值班工程师（审批人）",
         "carol": "员工", "erin": "IT 管理员"}


def missing_server_deps() -> list[str]:
    return [m for m in ("fastapi", "uvicorn", "httpx") if importlib.util.find_spec(m) is None]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ApiProcess:
    name: str
    port: int
    popen: subprocess.Popen
    log_path: Path

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def alive(self) -> bool:
        return self.popen.poll() is None


class ITBuddyDeployment:
    """拉起 API 进程 + WorkerPool，并提供故障注入的入口（dep.pool.kill(i) / terminate(i) / pause(i)）。

    async with ITBuddyDeployment(tmp_path, workers=2, lease=1.0) as dep:
        async with dep.client("demo-acme-alice") as alice: ...        # 发给第 1 个 API 进程
        async with dep.client("demo-acme-bob", api=1) as bob: ...     # apis=2 时发给第 2 个

    port：第 1 个 API 进程的端口（None = 随机空闲端口）；其余 API 进程用随机空闲端口。
    """

    def __init__(self, workdir: str | Path = DEFAULT_DIR, *, workers: int = 2, apis: int = 1, offline: bool = True,
                 latency: float = 0.2, lease: float = 30.0, concurrency: int = 8, port: int | None = None,
                 worker_options: dict | None = None, grace: float = 10.0):
        self.workdir = Path(workdir)
        self.db = self.workdir / "itbuddy.db"
        self.enterprise_db = self.workdir / "enterprise.db"
        self.n_workers, self.offline, self.lease, self.concurrency, self.grace = workers, offline, lease, concurrency, grace
        self.n_apis, self.port = apis, port
        self.worker_options = {"enterprise_db": str(self.enterprise_db), "runs_dir": str(self.workdir),
                               "offline": "1" if offline else "0", "latency": str(latency), **(worker_options or {})}
        self.apis: list[ApiProcess] = []
        self.pool: WorkerPool | None = None

    # ------------------------------------------------------------------ 生命周期

    async def start(self, timeout: float = 90.0) -> "ITBuddyDeployment":
        """拉起所有进程并等它们就绪；中途失败时先把已经拉起的进程停掉再抛出。"""
        try:
            return await self._start(timeout)
        except BaseException:
            await self.stop()
            raise

    async def _start(self, timeout: float) -> "ITBuddyDeployment":
        import httpx

        self.workdir.mkdir(parents=True, exist_ok=True)
        # 先由这一个进程建表、播种，再拉起其他进程：之后每个进程的 setup 都只是"已存在，跳过"
        queue = SQLiteJobQueue(self.db)
        await queue.setup()
        await queue.close()
        await (await ITBuddyStores.open(self.db)).close()
        await (await Backend.open(self.enterprise_db)).close()

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
        env.update(PYTHONUNBUFFERED="1", ITBUDDY_DB=str(self.db), ITBUDDY_ENTERPRISE_DB=str(self.enterprise_db))
        (self.workdir / "logs").mkdir(parents=True, exist_ok=True)
        for i in range(self.n_apis):
            name = "api" if self.n_apis == 1 else f"api-{i}"
            port = (self.port if i == 0 else None) or free_port()
            log_path = self.workdir / "logs" / f"{name}.log"
            with open(log_path, "ab") as log:
                popen = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "server:create_app", "--factory", "--app-dir", str(HERE),
                     "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning", "--no-access-log"],
                    stdout=log, stderr=subprocess.STDOUT, env={**env, "ITBUDDY_API_NAME": name},
                )
            self.apis.append(ApiProcess(name, port, popen, log_path))
        self.pool = WorkerPool(f"sqlite:///{self.db}", f"{HERE / 'worker_app.py'}:make_handler", n=self.n_workers,
                               concurrency=self.concurrency, lease=self.lease, poll=0.05, grace=self.grace,
                               options=self.worker_options, log_dir=self.workdir / "logs", name="worker-")
        self.pool.start()

        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            for api in self.apis:
                while True:
                    if not api.alive:
                        raise RuntimeError(f"API 进程 {api.name} 启动失败：\n{api.log_path.read_text(errors='replace')[-3000:]}")
                    try:
                        if (await client.get(f"{api.url}/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{timeout}s 内 API 进程 {api.name} 没有就绪")
                    await asyncio.sleep(0.1)
        while len(self.pool.events("started")) < self.n_workers:
            dead = [w.worker_id for w in self.pool.workers if not w.alive]
            if dead:
                raise RuntimeError(f"worker {dead} 启动失败：\n{self.pool.logs()[-3000:]}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout}s 内 worker 没有全部启动：\n{self.pool.logs()[-3000:]}")
            await asyncio.sleep(0.05)
        return self

    async def stop(self, timeout: float = 15.0) -> dict[str, int | None]:
        """先停 API（SIGTERM：不再接新请求，处理完手上的请求再退出），再停 worker（SIGTERM：排空在途任务）。
        超时仍未退出的进程 SIGKILL。返回各进程的退出码。"""
        codes: dict[str, int | None] = {}
        for api in self.apis:
            if api.alive:
                api.popen.send_signal(signal.SIGTERM)
        for api in self.apis:
            try:
                # uvicorn 优雅关闭后按惯例"以收到的信号退出"，所以正常的退出码是 -15（SIGTERM）
                codes[api.name] = await asyncio.to_thread(api.popen.wait, timeout)
            except subprocess.TimeoutExpired:
                api.popen.kill()
                codes[api.name] = await asyncio.to_thread(api.popen.wait)
        if self.pool is not None:
            for w, code in zip(self.pool.workers, await asyncio.to_thread(self.pool.stop, timeout)):
                codes[w.worker_id] = code
        return codes

    async def __aenter__(self) -> "ITBuddyDeployment":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ------------------------------------------------------------------ 观测与故障注入

    @property
    def api(self) -> ApiProcess:
        return self.apis[0]

    @property
    def url(self) -> str:
        return self.api.url

    def client(self, api_key: str, *, api: int = 0, timeout: float = 30.0):
        import httpx

        return httpx.AsyncClient(base_url=self.apis[api].url, timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"})

    def worker_index(self, worker_id: str) -> int:
        return next(i for i, w in enumerate(self.pool.workers) if w.worker_id == worker_id)

    def pid_of(self, worker_id: str) -> int | None:
        return self.pool.workers[self.worker_index(worker_id)].pid

    def query(self, db: Path, sql: str, params=()) -> list[dict]:
        """直接读 SQLite 文件（只读，给测试和演示看"真实发生了什么"）。"""
        conn = sqlite3.connect(db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def logs(self) -> str:
        parts = [f"===== {a.name} (pid {a.pid}) =====\n{a.log_path.read_text(errors='replace')}" for a in self.apis
                 if a.log_path.exists()]
        if self.pool is not None:
            parts.append(self.pool.logs())
        return "\n".join(parts)


async def wait_status(http, run_id: str, done=lambda info: info["status"] not in ("queued", "running", "resuming"),
                      timeout: float = 60.0) -> dict:
    """轮询（每次长轮询最多 2 秒）直到 done(info)。给演示和测试用。"""
    deadline = time.monotonic() + timeout
    while True:
        r = await http.get(f"/runs/{run_id}", params={"wait": 2})
        r.raise_for_status()
        info = r.json()
        if done(info):
            return info
        if time.monotonic() > deadline:
            raise TimeoutError(f"{timeout}s 内没有等到 {run_id} 的目标状态，最后一次：{info}")
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------ 演示


async def demo(dep: ITBuddyDeployment) -> None:
    """跑一遍：幂等提交、跨进程审批（暂停它的 worker 被"滚动发布"替换掉）、跨租户隔离、kill -9 接手只建一张单、审计。"""
    say = print
    async with dep.client("demo-acme-alice") as alice, dep.client("demo-acme-frank") as frank, \
            dep.client("demo-globex-carol") as carol:
        say("\n① 员工 alice 提交\"帮我重置密码\"（带 Idempotency-Key，模拟网络超时后重试一次）")
        body = {"message": "我忘记密码了，帮我重置"}
        first = (await alice.post("/runs", json=body, headers={"Idempotency-Key": "demo-reset-1"})).json()
        again = (await alice.post("/runs", json=body, headers={"Idempotency-Key": "demo-reset-1"})).json()
        run_id = first["run_id"]
        say(f"   202 → {run_id}；重试 → {again['run_id']}（deduplicated={again['deduplicated']}，队列里只有一个任务）")
        info = await wait_status(alice, run_id, lambda i: i["status"] == "paused")
        paused_by = info["checkpoint"]["writer"]
        say(f"   status=paused，等待审批：{info['pending_approval']['name']}；暂停它的是 {paused_by}（pid {dep.pid_of(paused_by)}）")

        say(f"\n② 审批要几个小时后才到；这期间 {paused_by} 被滚动发布替换掉了（SIGTERM → 排空 → 退出）")
        dep.pool.terminate(dep.worker_index(paused_by))
        code = await asyncio.to_thread(dep.pool.workers[dep.worker_index(paused_by)].wait, 30)
        say(f"   {paused_by} 退出码 {code}")
        say(f"   另一家公司的 carol 查看这个 run → {(await carol.get(f'/runs/{run_id}')).status_code}；"
            f"尝试审批 → {(await carol.post(f'/runs/{run_id}/approval', json={'approved': True})).status_code}")
        queue = (await frank.get("/approvals")).json()
        say(f"   值班工程师 frank 的审批队列：{[(q['run_id'], q['requester'], q['tool']) for q in queue]}")
        r = await frank.post(f"/runs/{run_id}/approval", json={"approved": True, "comment": "已电话核实本人"})
        say(f"   frank 批准 → {r.status_code} {r.json()['status']}")
        info = await wait_status(alice, run_id)
        resumed_by = info["checkpoint"]["writer"]
        say(f"   status={info['status']}；恢复它的是 {resumed_by}（pid {dep.pid_of(resumed_by)}）")
        say(f"   ITBuddy> {info['output']}")
        say(f"   再批一次 → {(await frank.post(f'/runs/{run_id}/approval', json={'approved': True})).status_code}（已经不在等待审批）")
        dep.pool.restart(dep.worker_index(paused_by))  # 新版本的 Pod 起来了（同一个 worker id）

        say("\n③ alice 报修；持有任务的 worker 在\"工单系统已经建好单、结果还没记下\"时被 kill -9")
        r = await alice.post("/runs", json={"message": "我的笔记本屏幕一直闪，帮我提个工单"})
        ticket_run = r.json()["run_id"]
        deadline = time.monotonic() + 30
        while not dep.query(dep.enterprise_db, "SELECT 1 FROM tickets WHERE idempotency_key LIKE ?", (f"{ticket_run}:%",)):
            if time.monotonic() > deadline:
                raise TimeoutError("没有等到建单")
            await asyncio.sleep(0.02)
        victim = (await alice.get(f"/runs/{ticket_run}")).json()["job"]["worker_id"]
        dep.pool.kill(dep.worker_index(victim))
        say(f"   kill -9 {victim}（pid {dep.pid_of(victim)}）")
        t0 = time.monotonic()
        info = await wait_status(alice, ticket_run)
        say(f"   {time.monotonic() - t0:.1f}s 后完成：接手者 {info['job']['worker_id']}，第 {info['job']['attempts']} 次尝试，"
            f"fence {info['job']['fence']}")
        say(f"   ITBuddy> {info['output']}")
        rows = dep.query(dep.enterprise_db, "SELECT outcome, pid FROM side_effect_attempts WHERE idempotency_key LIKE ? "
                                            "ORDER BY id", (f"{ticket_run}:%",))
        tickets = dep.query(dep.enterprise_db, "SELECT id FROM tickets WHERE idempotency_key LIKE ?", (f"{ticket_run}:%",))
        say(f"   工单系统收到 {len(rows)} 次建单请求：{[(r['outcome'], r['pid']) for r in rows]} → 只有 {len(tickets)} 张工单 "
            f"{[t['id'] for t in tickets]}")
        dep.pool.restart(dep.worker_index(victim))

        say("\n④ 审计日志（所有进程写进同一张只追加的表）：密码重置那次运行")
        for rec in dep.query(dep.db, "SELECT event, writer, pid, record FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)):
            detail = json.loads(rec["record"])
            extra = {k: detail[k] for k in ("status", "tool", "approver", "approved", "approved_by", "comment") if detail.get(k) is not None}
            say(f"   {rec['event']:<18} writer={rec['writer']:<9} pid={rec['pid']:<6} {extra}")


async def bench(dep: ITBuddyDeployment, n: int) -> dict:
    """小压测：两个租户交替提交 n 个"VPN 怎么连"（每个运行 2 次模型调用），等全部完成。
    返回墙钟时间、吞吐、每个 worker 处理了多少个（进程之间怎么分工）。"""
    async with dep.client("demo-acme-alice") as alice, dep.client("demo-globex-carol") as carol:
        clients = [alice, carol]
        t0 = time.monotonic()
        responses = await asyncio.gather(*(clients[i % 2].post("/runs", json={"message": f"公司 VPN 怎么连？#{i}"})
                                           for i in range(n)))
        submitted = time.monotonic() - t0
        run_ids = [(clients[i % 2], r.json()["run_id"]) for i, r in enumerate(responses)]
        infos = await asyncio.gather(*(wait_status(c, rid, timeout=600) for c, rid in run_ids))
        elapsed = time.monotonic() - t0
    per_worker: dict[str, int] = {}
    for info in infos:
        per_worker[info["job"]["worker_id"]] = per_worker.get(info["job"]["worker_id"], 0) + 1
    return {"runs": n, "completed": sum(i["status"] == "completed" for i in infos), "submit_s": round(submitted, 2),
            "elapsed_s": round(elapsed, 2), "runs_per_s": round(n / elapsed, 1), "per_worker": dict(sorted(per_worker.items()))}


def _show(path: Path) -> str:
    """在当前目录下的路径显示成相对路径（输出里不带个人目录）。"""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def print_banner(dep: ITBuddyDeployment) -> None:
    print(f"ITBuddy 已启动（{'离线剧本模型' if dep.offline else '真实模型'}，{len(dep.apis)} 个 API 进程，{dep.n_workers} 个 worker 进程）")
    for a in dep.apis:
        print(f"  {a.name:<9} {a.url}   接口文档 {a.url}/docs   pid {a.pid}")
    for w in dep.pool.workers:
        print(f"  {w.worker_id:<9} pid {w.pid}")
    print(f"  数据      {_show(dep.db)}（队列 / 检查点 / 幂等 / 审计）")
    print(f"            {_show(dep.enterprise_db)}（模拟的企业系统）")
    print(f"  日志      {_show(dep.workdir / 'logs')}    追踪 {_show(dep.workdir / 'traces')}")
    print("演示 API key（请求头 Authorization: Bearer <key>；角色来自员工目录）：")
    from server import DEMO_API_KEYS

    for key, (tenant, user) in DEMO_API_KEYS.items():
        print(f"  {key:<20} {tenant:<7} {user:<6} {ROLES.get(user, '')}")
    print("试试：")
    print(f"  curl -s -X POST {dep.url}/runs -H 'Authorization: Bearer demo-acme-alice' "
          "-H 'Content-Type: application/json' -d '{\"message\":\"我忘记密码了，帮我重置\"}'")
    print(f"  curl -s '{dep.url}/runs/<run_id>?wait=5' -H 'Authorization: Bearer demo-acme-alice'")
    print(f"  curl -s {dep.url}/approvals -H 'Authorization: Bearer demo-acme-frank'")
    print(f"  curl -s -X POST {dep.url}/runs/<run_id>/approval -H 'Authorization: Bearer demo-acme-frank' "
          "-H 'Content-Type: application/json' -d '{\"approved\": true, \"comment\": \"已电话核实本人\"}'")


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ITBuddy 本机多进程部署：API 进程 + worker 进程")
    ap.add_argument("--workers", type=int, default=2, help="worker 进程数")
    ap.add_argument("--apis", type=int, default=1, help="API 进程数（副本）")
    ap.add_argument("--offline", action="store_true", help="离线剧本模型（不需要 API key，零成本）")
    ap.add_argument("--latency", type=float, default=0.3, help="离线模型每次调用的耗时（秒）")
    ap.add_argument("--port", type=int, default=8000, help="API 端口，0 = 随机空闲端口")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="数据库、日志、追踪所在的目录")
    ap.add_argument("--lease", type=float, default=None, help="任务租约秒数（worker 崩溃后多久被接手）；默认 30，--demo 时 1.5")
    ap.add_argument("--fresh", action="store_true", help="启动前清空 --dir 里的数据库（重新播种）")
    ap.add_argument("--demo", action="store_true", help="启动后跑一遍演示，然后停止")
    ap.add_argument("--bench", type=int, default=0, metavar="N", help="启动后提交 N 个运行做小压测，然后停止")
    ap.add_argument("--concurrency", type=int, default=8, help="每个 worker 进程同时处理的任务数上限")
    args = ap.parse_args(argv)

    if missing_server_deps():
        print(INSTALL_HINT)
        return 1
    if not args.offline:
        from agentkit import default_llm

        try:
            default_llm()  # 在拉起进程之前就检查模型配置，而不是让每个 worker 各报一遍错
        except RuntimeError as e:
            print(f"无法创建模型客户端：{e}\n可以先用 --offline 运行。")
            return 2
    workdir = Path(args.dir)
    if args.fresh or args.demo or args.bench:
        for name in ("itbuddy.db", "enterprise.db"):
            for suffix in ("", "-wal", "-shm"):
                (workdir / f"{name}{suffix}").unlink(missing_ok=True)
    lease = args.lease if args.lease is not None else (1.5 if args.demo else 30.0)
    options = {"post_commit_delay": "1.5"} if args.demo else {}  # 演示 kill -9 用的故障注入（见 worker_app.py）
    dep = ITBuddyDeployment(workdir, workers=args.workers, apis=args.apis, offline=args.offline, latency=args.latency,
                            lease=lease, port=args.port or None, worker_options=options, concurrency=args.concurrency)
    await dep.start()
    result = None
    try:
        print_banner(dep)
        if args.demo:
            await demo(dep)
        elif args.bench:
            result = await bench(dep, args.bench)
        else:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            print("按 Ctrl-C 停止（SIGTERM 同样有效）")
            await stop.wait()
    finally:
        codes = await dep.stop()
        print(f"\n已停止，各进程退出码：{codes}")
    if result is not None:
        peaks = {e["worker_id"]: e["stats"]["max_in_flight"] for e in dep.pool.events("stopped")}
        print(f"\n小压测（{args.workers} 个 worker × 每个最多 {args.concurrency} 个并发任务，"
              f"{'离线剧本模型每次调用 ' + str(args.latency) + 's' if args.offline else '真实模型'}）：")
        print(f"  {result['completed']}/{result['runs']} 完成，全部提交用时 {result['submit_s']}s，"
              f"全部完成用时 {result['elapsed_s']}s，{result['runs_per_s']} 个运行/秒")
        print(f"  每个 worker 完成的运行数：{result['per_worker']}；每个 worker 同时在跑的任务数峰值：{peaks}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
