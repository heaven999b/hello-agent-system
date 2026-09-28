"""第 13 课 Demo（三）：实测吞吐 —— 加进程能快多少？瓶颈会跑到哪里去？

    python lessons/13_distributed_concurrency/demo_scale.py --offline    # 只有离线模式：真实模型的耗时波动太大，测不出稳定的数字

两组测量，全部是真实的 worker 进程（WorkerPool → python -m agentkit.distributed.worker），共享一个 SQLite 文件：
  A. 空任务（handler 什么都不做）：每个任务只剩"领取 + 提交"两次写事务 —— 测的是队列本身，也就是 SQLite 单写者的上限；
  B. Agent 任务（剧本模型，每个任务 2 次模型调用、每次 asyncio.sleep 0.1 秒，外加 1 次写工具和几次检查点写入）：
     - 1 个进程 × 并发 1 → 1 个进程 × 并发 8：同一个进程里用 asyncio 同时推进多个任务（第 02 课）；
     - 4 个进程 × 并发 8：再加进程；
     - 4 个进程 × 并发 8，但所有进程共用 3 个模型并发名额（SQLiteSemaphore）：配额给吞吐封顶；
     - 1 / 4 / 8 个进程 × 并发 64：并发拉满，瓶颈换成了共享数据库的写入。
计时从"开闸"算起：任务先带着很长的延迟入队，等所有 worker 进程都启动完毕，再用一条 UPDATE 让它们同时变成可领取，
所以进程启动时间不算在内。数字取决于机器和当时的负载，讲义里写明了测量条件。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

from agentkit.distributed import SQLiteCheckpointer, SQLiteIdempotencyStore, SQLiteJobQueue, WorkerPool

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
APP = str(HERE / "worker_app.py")


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    print("  " + "".join(pad(h, w) for h, w in zip(headers, widths)))
    print("  " + "─" * sum(widths))
    for r in rows:
        print("  " + "".join(pad(c, w) for c, w in zip(r, widths)))


async def measure(name: str, *, app: str, n_jobs: int, procs: int, concurrency: int, options: dict,
                  agent_jobs: bool) -> dict:
    workdir = RUNS / "scale" / name
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    db = workdir / "jobs.db"
    queue = SQLiteJobQueue(db)
    await queue.setup()
    if agent_jobs:  # 调度器先建好所有表（几个进程同时新建 SQLite 文件、切换 WAL 模式时会撞锁）
        for store in (SQLiteCheckpointer(db), SQLiteIdempotencyStore(db)):
            await store.setup()
            await store.close()
    for i in range(n_jobs):
        payload = {"op": "run", "input": f"第 {i + 1} 条报修"} if agent_jobs else {"i": i}
        await queue.enqueue("run", payload, tenant_id="acme", delay_seconds=3600)  # 先关着闸
    pool = WorkerPool(f"sqlite:///{db}", app, n=procs, concurrency=concurrency, lease=30, poll=0.02, grace=10,
                      options=options, log_dir=workdir / "logs")
    with pool:
        deadline = time.monotonic() + 60
        while len(pool.events("started")) < procs:
            if time.monotonic() > deadline:
                raise TimeoutError(pool.logs())
            await asyncio.sleep(0.05)
        gate = time.time()
        conn = sqlite3.connect(db, timeout=30)
        conn.execute("UPDATE agent_jobs SET run_at = ?", (gate,))  # 开闸：所有任务同时变成可领取
        conn.commit()
        conn.close()
        deadline = time.monotonic() + 120
        while (await queue.stats())["counts"]["succeeded"] < n_jobs:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{name}: 120 秒内没有全部完成\n{pool.logs()[-3000:]}")
            await asyncio.sleep(0.02)
    finished = max(j.finished_at for j in await queue.list_jobs(limit=n_jobs))
    await queue.close()
    elapsed = finished - gate
    cpu = sum(e["seconds"] for e in pool.events("cpu"))  # 各进程从第一个任务到退出用掉的 CPU 秒
    return {"elapsed": elapsed, "rate": n_jobs / elapsed, "cpu_util": cpu / (elapsed * procs)}


async def run(args) -> None:
    print(f"A. 空任务 × {args.noop_jobs}：只测队列（每个任务 = 领取 + 提交，两次写事务），每个进程 run_worker 并发 8")
    rows, base = [], None
    for procs in (1, 2, 4):
        r = await measure(f"noop_{procs}p", app=f"{APP}:make_noop_handler", n_jobs=args.noop_jobs, procs=procs,
                          concurrency=8, options={}, agent_jobs=False)
        base = base or r["rate"]
        rows.append([f"{procs}", f"{r['elapsed']:.2f}s", f"{r['rate']:.0f}", f"{r['rate'] * 2:.0f}", f"{r['rate'] / base:.2f}x",
                     f"{r['cpu_util']:.0%}"])
    table(["进程数", "耗时", "任务/秒", "写事务/秒（×2）", "相对 1 个进程", "worker CPU 利用率"], rows, [10, 10, 10, 18, 16, 18])
    print("  → 加进程几乎不加速：所有进程排队等同一把写锁。SQLite 同一时刻只允许一个写者，这就是它作为队列的天花板。")
    print("    （CPU 利用率 = 各 worker 进程用掉的 CPU 秒 ÷ (耗时 × 进程数)：进程越多、每个进程越闲 —— 大家都在排队。）\n")

    print("B. Agent 任务：每个任务 2 次模型调用（各 asyncio.sleep 0.1 秒）+ 1 次写工具。")
    print("   领取 1 + 接管检查点 1 + 检查点保存 5 + 工具写入 1 + 幂等记录 1 + 提交 1 = 每个任务 10 次写事务（实测计数）")
    configs = [(1, 1, 0, 16), (1, 8, 0, 96), (4, 8, 0, 96), (4, 8, 3, 96), (1, 64, 0, 512), (4, 64, 0, 512), (8, 64, 0, 512)]
    rows = []
    for procs, conc, slots, n in configs:
        options = {"offline": "1", "first_latency": 0.1, "second_latency": 0.1, "events": "0", "cpu_report": "1"}
        if slots:
            options["model_slots"] = slots
        r = await measure(f"agent_{procs}p_{conc}c_{slots}s", app=f"{APP}:make_handler", n_jobs=n,
                          procs=procs, concurrency=conc, options=options, agent_jobs=True)
        ideal = min(procs * conc, slots or procs * conc) / 0.2  # 只受"等模型"限制时的理论上限
        rows.append([f"{procs} × {conc}", f"≤{slots}" if slots else "不限", str(n), f"{r['elapsed']:.2f}s",
                     f"{r['rate']:.1f}", f"{ideal:.0f}", f"{r['rate'] / ideal:.0%}", f"{r['rate'] * 10:.0f}",
                     f"{r['cpu_util']:.0%}"])
    table(["进程 × 并发", "模型名额", "任务数", "耗时", "任务/秒", "理论上限", "达到", "写事务/秒", "CPU 利用率"],
          rows, [14, 10, 8, 10, 10, 10, 8, 11, 10])
    print("  → 理论上限 = 同时在跑的任务数 ÷ 每个任务等模型的 0.2 秒。")
    print("    并发不高时，时间几乎全花在等模型上（I/O 密集）：第一把杠杆是同一个进程里的 async 并发（1×1 → 1×8），")
    print("    加进程再乘上去（1×8 → 4×8）；所有进程共用 3 个模型名额时，4 个进程也只有 3 个并发的吞吐。")
    print("    并发拉到 ×64 之后，加进程的收益越来越小（4×64 → 8×64），而 worker 的 CPU 大部分时间闲着：")
    print("    每秒几千次写事务全部排队经过同一把写锁，瓶颈从'等模型'变成了'共享数据库只有一个写者'。")
    print("    再往上走，就该换 Postgres（行级锁，多个写者并行，第 26 课）。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="（只支持离线模式，参数保留是为了和其他 demo 一致）")
    parser.add_argument("--noop-jobs", type=int, default=2000)
    args = parser.parse_args()
    if not args.offline:
        print("demo_scale.py 只测离线剧本模型：真实模型每次几秒、波动很大，测不出稳定的吞吐数字。按离线模式运行。\n")
    t0 = time.time()
    asyncio.run(run(args))
    print(f"\n总耗时 {time.time() - t0:.0f} 秒（含每组测量拉起进程的时间）。运行产物在 {(RUNS / 'scale').relative_to(HERE.parents[1])}/")


if __name__ == "__main__":
    sys.exit(main())
