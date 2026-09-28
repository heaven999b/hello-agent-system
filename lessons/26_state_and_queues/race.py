"""真实的多进程竞争：同时拉起 N 个 python 进程，各用各的连接去抢同一个 Postgres / Redis。

练习测试（test_exercise.py）用它检验你写的 cas_save / claim_one / TOKEN_BUCKET_LUA。第 13 课的 race.py 在 SQLite 上做同样的事。

每个子进程（python race.py MODE ...）：
  1. 按文件路径加载要检验的实现（exercise.py 或 solution.py）；
  2. 建好自己的连接（psycopg.AsyncConnection / redis.asyncio.Redis），写一个"我准备好了"的文件，
     然后等父进程创建"开跑"文件 —— 所有进程站在同一条起跑线上；
  3. 按模式循环调用练习函数，最后把结果以一行 JSON 打到标准输出（出错时打出异常类型和 traceback）。
父进程（run_race）收集结果；子进程里的 NotImplementedError 在父进程里原样抛出，其余异常变成 AssertionError。

为什么是进程而不是线程？进程之间不共享任何内存、没有 GIL、没有共用的 Python 对象，只能通过数据库 / Redis 协作 ——
和多台机器上的 worker 一样。谁先抢到、谁的写入被 CAS 挡回去，全是数据库和操作系统调度决定的真实结果。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from types import ModuleType


def _load(path: str) -> ModuleType:
    p = Path(path).resolve()
    key = f"_race_{p.parent.name}_{p.stem}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, p)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


# =====================================================================================
# 子进程
# =====================================================================================


async def _wait_for_start(ready: Path, go: Path, timeout: float = 60.0) -> None:
    ready.write_text(str(os.getpid()))
    deadline = time.monotonic() + timeout
    while not go.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("等了太久也没等到开跑信号")
        await asyncio.sleep(0.001)


async def _pg(uri: str):
    import psycopg
    from psycopg.rows import dict_row

    conn = await psycopg.AsyncConnection.connect(uri, autocommit=True, row_factory=dict_row)
    await conn.execute("SET lock_timeout = '5s'")  # 实现如果在排队等别人的行锁，5 秒后报错，而不是让测试卡死
    return conn


async def child_cas(args, ex) -> dict:
    """对同一个 run 做 times 次"读最新版本 → 想一会儿 → CAS 写回 +1"，冲突就重读重来。"""
    conn = await _pg(args.target)
    await _wait_for_start(Path(args.ready), Path(args.go))
    conflicts = 0
    for _ in range(args.times):
        while True:
            row = await ex.load_run(conn, "counter")
            await asyncio.sleep(args.think)  # "读完之后要处理一会儿"（对 Agent 来说是调一次模型）：窗口里别人会插进来
            if await ex.cas_save(conn, "counter", row["version"], json.dumps({"n": row["state"]["n"] + 1})):
                break
            conflicts += 1
    await conn.close()
    return {"conflicts": conflicts}


async def child_claim(args, ex) -> dict:
    """循环领取任务，直到领到 None（队列空了）。"""
    conn = await _pg(args.target)
    await _wait_for_start(Path(args.ready), Path(args.go))
    claims = []
    while (job := await ex.claim_one(conn, args.worker, 30)) is not None:
        claims.append({"id": job["id"], "fence": job["fence"], "attempts": job["attempts"], "worker_id": job["worker_id"]})
    await conn.close()
    return {"claims": claims}


async def child_take(args, ex) -> dict:
    """对同一个桶尝试拿 times 次令牌，记下放行了几次。"""
    import redis.asyncio as aredis

    client = aredis.Redis.from_url(args.target)
    await client.ping()  # 先把连接建好，开跑之后没有握手延迟
    await _wait_for_start(Path(args.ready), Path(args.go))
    granted = 0
    for _ in range(args.times):
        ok, _ = await ex.take(client, args.key, rate=args.rate, capacity=args.capacity)
        granted += ok
    await client.aclose()
    return {"granted": granted}


async def child_amain(args) -> dict:
    ex = _load(args.impl)
    runner = {"cas": child_cas, "claim": child_claim, "take": child_take}[args.mode]
    return await runner(args, ex)


def child_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["cas", "claim", "take"])
    ap.add_argument("--target", required=True, help="Postgres URI 或 Redis URL")
    ap.add_argument("--impl", required=True)
    ap.add_argument("--worker", required=True)
    ap.add_argument("--ready", required=True)
    ap.add_argument("--go", required=True)
    ap.add_argument("--times", type=int, default=10)
    ap.add_argument("--think", type=float, default=0.001)
    ap.add_argument("--key", default="tb:shared")
    ap.add_argument("--rate", type=float, default=0.001)
    ap.add_argument("--capacity", type=float, default=25)
    args = ap.parse_args(argv)
    out: dict = {"worker": args.worker, "pid": os.getpid()}
    try:
        out.update(asyncio.run(child_amain(args)))
        out["ok"] = True
    except BaseException as e:  # noqa: BLE001 —— 原样报告给父进程
        out.update(ok=False, type=type(e).__name__, message=str(e), traceback=traceback.format_exc())
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


# =====================================================================================
# 父进程
# =====================================================================================


async def run_race(n: int, mode: str, *, target: str, impl: str | Path, workdir: str | Path, timeout: float = 90.0,
                   **opts) -> list[dict]:
    """同时拉起 n 个子进程，等它们都准备好（连接都建好了）后一起开跑，返回每个进程的结果（按 worker 名排序）。

    opts：times / think（cas 模式），times / key / rate / capacity（take 模式）。
    """
    base = Path(workdir) / f".race-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    go = base / "go"
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(repo), os.environ.get("PYTHONPATH")]))}
    procs = []
    for i in range(n):
        cmd = [sys.executable, str(Path(__file__).resolve()), mode, "--target", target, "--impl", str(impl),
               "--worker", f"p{i + 1}", "--ready", str(base / f"ready-{i}"), "--go", str(go)]
        for k, v in opts.items():
            cmd += ["--" + k.replace("_", "-"), str(v)]
        procs.append(await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.PIPE, env=env))
    try:
        deadline = time.monotonic() + timeout
        while sum((base / f"ready-{i}").exists() for i in range(n)) < n:
            if any(p.returncode is not None for p in procs):  # 有进程还没准备好就退出了（比如加载实现时就出错）
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout}s 内 {n} 个进程没有全部就绪")
            await asyncio.sleep(0.005)
        go.touch()  # 起跑：所有子进程在 1 毫秒内看到它
        from agentkit import wait_for  # 取消安全版：3.12 之前的 asyncio.wait_for 可能吞掉取消

        outs = await wait_for(asyncio.gather(*(p.communicate() for p in procs)), max(1.0, deadline - time.monotonic()))
    finally:
        for p in procs:
            if p.returncode is None:
                p.kill()
                await p.wait()
        for f in base.iterdir():
            f.unlink()
        base.rmdir()
    results = []
    for p, (stdout, stderr) in zip(procs, outs):
        text = stdout.decode(errors="replace").strip()
        try:
            results.append(json.loads(text.splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            raise AssertionError(f"子进程 {p.pid} 没有正常输出结果（退出码 {p.returncode}）：\n{text}\n"
                                 f"{stderr.decode(errors='replace')}") from None
    for r in results:
        if not r["ok"]:
            if r["type"] == "NotImplementedError":
                raise NotImplementedError(f"[进程 {r['worker']}] {r['message']}")
            raise AssertionError(f"进程 {r['worker']}（pid {r['pid']}）出错：{r['type']}: {r['message']}\n{r['traceback']}")
    return sorted(results, key=lambda r: int(r["worker"][1:]))


if __name__ == "__main__":
    sys.exit(child_main())
