"""第 13 课 Demo（二）：从本课代码到框架 —— agentkit.distributed 跑 Agent 任务，3 个 worker 进程 + 真实故障注入。

    python lessons/13_distributed_concurrency/demo_agents.py             # 真实模型（每个 worker 进程各自调用 .env 里的模型）
    python lessons/13_distributed_concurrency/demo_agents.py --offline   # 离线：剧本模型（asyncio.sleep 模拟耗时）

demo.py 里你手写了 worker 循环；这里换成框架：
    WorkerPool          拉起 3 个 `python -m agentkit.distributed.worker` 进程（和生产里每个 Pod 跑的命令一样）
    run_worker          每个进程里的主循环：领取、心跳续租、带 fence 提交、SIGTERM 优雅停机
    AgentJobHandler     把 Agent 的 run / resume 包装成任务；每次领取用这次的 fence 创建检查点视图（接管）
    SQLiteJobQueue / SQLiteCheckpointer / SQLiteIdempotencyStore   所有进程共享的状态，都在同一个 SQLite 文件里
业务代码在 worker_app.py：一个 Agent + 一个有副作用的写工具 create_ticket。

三次真实的故障注入（操作系统信号，不是 sleep 演出来的）：
  ① kill -9：任务 #1 的持有者在"工具已执行、幂等记录已写、检查点还没落盘"的窗口里被杀
      → 租约过期后别的进程接手，重放同一个工具调用 → SQLiteIdempotencyStore 命中，工具不再执行
  ② SIGSTOP：任务 #2 的持有者在第二次调用模型时被冻结 → 别人接手并完成 → SIGCONT 解冻后，
      它的检查点写入 / 心跳 / 提交全部被 fence 拒绝（"僵尸"写不进去）
  ③ SIGTERM：对一个正在处理任务的 worker 发 SIGTERM（K8s 删除 Pod）→ 停止领取、做完手上的任务、退出码 0
最后打印时间线（pool.events()）和检查结果；任何一项检查不通过，退出码为 1。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

from agentkit import default_llm
from agentkit.distributed import SQLiteCheckpointer, SQLiteIdempotencyStore, SQLiteJobQueue, WorkerPool

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
APP = f"{HERE / 'worker_app.py'}:make_handler"

MESSAGES = [
    "3 楼东区的打印机一直卡纸",
    "VPN 连不上，提示证书过期",
    "新同事还没有 Git 仓库权限",
    "会议室 B 的投影仪没有信号",
    "邮箱提示空间已满",
    "财务系统登录后一直白屏",
    "工位网口插上网线没反应",
    "帮我重置一下 Jira 的密码",
]


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def rows(db: Path, sql: str, params=()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise
    finally:
        conn.close()


async def eventually(cond, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError(f"{timeout:.0f}s 内没有等到{what}")


def describe(e: dict) -> str | None:
    """把一条事件翻译成时间线上的一行；不关心的事件返回 None。"""
    name, job = e["event"], e.get("job")
    if name == "claimed":
        return f"领取任务 #{job}（第 {e['attempts']} 次，fence={e['fence']}）"
    if name == "tool_executed":
        return f"🧾 create_ticket 真正执行 → {e['ticket']}（任务 #{job}，幂等键 {e['key']}）"
    if name == "idempotency_hit":
        return f"♻️  幂等存储命中 {e['key']} → 直接用上次的结果，工具没有再执行"
    if name == "chaos_window":
        return f"（任务 #{job}：工具已执行、幂等记录已写，检查点还没落盘 —— 故障窗口 {e['seconds']:.0f} 秒）"
    if name == "llm_call" and e.get("phase") == "answer":
        return f"任务 #{job}：拿到工具结果，第二次调用模型……"
    if name == "completed":
        return f"✅ 完成任务 #{job}（fence={e['fence']}）"
    if name == "heartbeat_rejected":
        return f"💔 任务 #{job} 续租被拒绝（fence={e['fence']} 已过期）"
    if name == "ownership_lost":
        err = e.get("error", "")
        why = "检查点写入被拒绝（CheckpointConflict）" if "检查点冲突" in err else "租约已丢失"
        return f"❌ 任务 #{job}：{why} → 停手，什么都不提交"
    if name == "fence_rejected":
        return f"❌ 任务 #{job} 的提交被 fence 拒绝"
    if name == "draining":
        return "进入停机收尾（draining）：不再领取新任务"
    if name == "stopped":
        return "下线"
    if name == "started":
        return "上线"
    return None


async def run(args) -> int:
    workdir = RUNS / "agents"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    db = workdir / "jobs.db"
    # 调度器先把数据库文件和表建好（切换 WAL 模式也在这时完成），再拉起 worker 进程
    queue = SQLiteJobQueue(db)
    await queue.setup()
    for store in (SQLiteCheckpointer(db), SQLiteIdempotencyStore(db)):
        await store.setup()
        await store.close()
    ids = []
    for i, text in enumerate(MESSAGES):
        payload = {"op": "run", "input": text, "metadata": {"user_id": f"u{100 + i}"}}
        if i == 0:
            payload["chaos"] = "kill_in_window"
        ids.append(await queue.enqueue("run", payload, tenant_id="acme", idempotency_key=f"msg-{i + 1}"))
    j1, j2 = ids[0], ids[1]

    options = {"offline": "1" if args.offline else "0", "first_latency": 0.2, "second_latency": 1.0, "chaos_window": 5}
    pool = WorkerPool(f"sqlite:///{db}", APP, n=3, concurrency=1, lease=args.lease, poll=0.05, grace=20,
                      options=options, log_dir=workdir / "logs")
    print(f"{len(ids)} 个 Agent 任务（IT 报修 → create_ticket → 回复），3 个 worker 进程，每个进程同时处理 1 个任务。")
    print(f"租约 {args.lease} 秒，心跳每 {args.lease / 3:.2f} 秒一次。所有共享状态都在 {db.relative_to(HERE.parents[1])}。\n")
    t0 = time.time()
    faults: list[tuple[float, str, str]] = []  # (t, 谁, 说明)
    names: dict[int, str] = {}  # pid → 显示名
    wait = 60 if args.offline else 240

    def note(who: str, text: str) -> None:
        faults.append((time.time(), who, text))

    def label(wid: str, pid: int) -> str:
        if pid not in names:
            n = sum(1 for p, v in names.items() if v.startswith(wid))
            names[pid] = wid if n == 0 else f"{wid}′"  # 同一个 worker_id 重启后的新进程加一撇
        return names[pid]

    pool.start()
    exit_codes: dict[str, int | None] = {}
    zombie_wid = term_wid = killed_wid = None
    try:
        for w in pool.workers:
            label(w.worker_id, w.pid)

        # ① kill -9：在故障窗口里杀掉任务 #1 的持有者
        ev = await eventually(lambda: pool.events("chaos_window"), wait, "任务 #1 进入故障窗口")
        ev = ev[0]
        victim = next(i for i, w in enumerate(pool.workers) if w.worker_id == ev["worker_id"])
        killed_wid, killed_pid = pool.workers[victim].worker_id, pool.workers[victim].pid
        pool.kill(victim)
        exit_codes["killed"] = pool.workers[victim].returncode
        note("调度器", f"💥 kill -9 {killed_wid}（pid {killed_pid}，退出码 {exit_codes['killed']}）：租约没还、检查点没写、不留遗言")
        pool.restart(victim)  # 像 K8s 一样用同一个名字重新拉起一个进程
        note("调度器", f"重新拉起 {killed_wid}′（新 pid {pool.workers[victim].pid}），相当于 K8s 重建 Pod")
        label(killed_wid, pool.workers[victim].pid)

        # ② SIGSTOP：任务 #2 在第二次调用模型时，冻结它的持有者
        ev = await eventually(lambda: [e for e in pool.events("llm_call") if e["job"] == j2 and e["phase"] == "answer"],
                              wait, "任务 #2 进入第二次模型调用")
        ev = ev[0]
        zombie = next(i for i, w in enumerate(pool.workers) if w.worker_id == ev["worker_id"])
        zombie_wid, zombie_pid = pool.workers[zombie].worker_id, pool.workers[zombie].pid
        pool.pause(zombie)
        note("调度器", f"🧊 SIGSTOP {zombie_wid}（pid {zombie_pid}）：进程被冻结，心跳停了，它自己毫不知情")

        async def succeeded(job_id):
            job = await queue.get(job_id)
            return job if job and job.status == "succeeded" else None

        await eventually(lambda: succeeded(j2), wait, "任务 #2 被别人接手并完成")
        pool.resume(zombie)
        note("调度器", f"▶️  SIGCONT {zombie_wid}：解冻，它接着执行被冻结前的那一行")
        await eventually(lambda: [e for e in pool.events() if e.get("pid") == zombie_pid and e["event"] in
                                  ("ownership_lost", "heartbeat_rejected", "fence_rejected")], 30, "僵尸的写入被拒绝")

        # ③ SIGTERM：挑一个正在处理任务的 worker（不是僵尸），请它优雅停机
        async def busy_worker():
            for job in await queue.list_jobs("leased"):
                w = next((i for i, x in enumerate(pool.workers) if x.worker_id == job.worker_id and x.alive), None)
                if w is not None and w != zombie and w != victim:
                    return (w, job.id)
            return None

        target, in_flight = await eventually(busy_worker, wait, "一个正在处理任务的 worker")
        term_wid, term_pid = pool.workers[target].worker_id, pool.workers[target].pid
        term_at = time.time()
        pool.terminate(target)
        note("调度器", f"🛑 SIGTERM {term_wid}（pid {term_pid}，手上正在处理任务 #{in_flight}）：K8s 删除 Pod 时发的就是它")
        exit_codes["terminated"] = await asyncio.to_thread(pool.workers[target].wait, 60)
        note("调度器", f"{term_wid} 退出，退出码 {exit_codes['terminated']}")

        async def all_done():
            return (await queue.stats())["counts"]["succeeded"] == len(ids)

        await eventually(all_done, wait * 2, "全部任务完成")
    finally:
        pool.stop()
    elapsed = time.time() - t0

    # ------------------------------------------------------------ 时间线
    print("时间线（worker 进程打到标准输出的事件 + 调度器的操作，按时间排序）：")
    lines = []
    for e in pool.events():
        text = describe(e)
        if text:
            lines.append((e["t"], label(e["worker_id"], e["pid"]), text))
    lines += faults
    for t, who, text in sorted(lines, key=lambda x: x[0]):
        print(f"  [+{t - t0:5.2f}s] {pad(who, 7)}│ {text}")

    # ------------------------------------------------------------ 检查
    jobs = {j.id: j for j in await queue.list_jobs()}
    await queue.close()
    events = pool.events()
    completed = Counter(e["job"] for e in events if e["event"] == "completed")
    tickets = rows(db, "SELECT run_id, count(*) AS n FROM tickets GROUP BY run_id")
    tool_calls = rows(db, "SELECT run_id, count(*) AS n FROM tool_calls GROUP BY run_id")
    idem_hits = [e for e in events if e["event"] == "idempotency_hit"]
    claims = {j: [e for e in events if e["event"] == "claimed" and e["job"] == j] for j in (j1, j2)}
    zombie_events = [e for e in events if e.get("pid") == zombie_pid]
    ckpt2 = rows(db, "SELECT writer, fence FROM agent_runs WHERE run_id = ?", (f"job-{j2}",))
    term_events = [e for e in events if e.get("pid") == term_pid]
    checks = [
        (f"{len(ids)} 个任务全部成功", all(j.status == "succeeded" for j in jobs.values())),
        ("每个任务恰好提交了一次（completed 事件按任务计数全是 1）", sorted(completed.values()) == [1] * len(ids)),
        ("每个任务只建了一张工单（下游 tickets 表）", len(tickets) == len(ids) and all(r["n"] == 1 for r in tickets)),
        ("工具函数一共只真正执行了 8 次：kill -9 之后的重放由 SQLiteIdempotencyStore 挡住",
         sum(r["n"] for r in tool_calls) == len(ids) and any(e["job"] == j1 for e in idem_hits)),
        (f"任务 #{j1} 被领取两次，接手者的 fence 更大",
         len(claims[j1]) == 2 and claims[j1][1]["fence"] > claims[j1][0]["fence"]),
        (f"僵尸 {zombie_wid} 醒来后的写入被拒绝，没有提交任务 #{j2}",
         any(e["event"] in ("ownership_lost", "fence_rejected") and e.get("job") == j2 for e in zombie_events)
         and not any(e["event"] == "completed" and e["job"] == j2 for e in zombie_events)),
        (f"任务 #{j2} 的检查点最后由接手者写入（writer={ckpt2[0]['writer'] if ckpt2 else '?'}）",
         bool(ckpt2) and ckpt2[0]["writer"] == jobs[j2].worker_id != zombie_wid),
        (f"kill -9 的进程退出码 -9，SIGTERM 的进程退出码 0",
         exit_codes.get("killed") == -9 and exit_codes.get("terminated") == 0),
        (f"{term_wid} 收到 SIGTERM 后没有再领取新任务，自己做完了在途的任务 #{in_flight} 才下线",
         not any(e["event"] == "claimed" and e["t"] > term_at for e in term_events)
         and any(e["event"] == "completed" and e["job"] == in_flight for e in term_events)
         and term_events[-1]["event"] == "stopped" and jobs[in_flight].worker_id == term_wid),
    ]
    print(f"\n检查（{elapsed:.1f} 秒完成）：")
    for text, ok in checks:
        print(f"  {'✅' if ok else '❌'} {text}")
    stats = [e["stats"] for e in events if e["event"] == "stopped"]
    print(f"\n各进程的 run_worker 统计合计：领取 {sum(s['claimed'] for s in stats)} 次，成功 {sum(s['succeeded'] for s in stats)}，"
          f"ownership_lost {sum(s['ownership_lost'] for s in stats)}，fence_rejected {sum(s['fence_rejected'] for s in stats)}"
          "（被 kill -9 的进程没有机会打出统计）")
    print(f"日志：{(workdir / 'logs').relative_to(HERE.parents[1])}/（每个 worker 一个文件，一行一个 JSON 事件）")
    return 0 if all(ok for _, ok in checks) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用剧本模型代替真实模型")
    parser.add_argument("--lease", type=float, default=None, help="租约秒数（默认：离线 1.0，真实模型 3.0）")
    args = parser.parse_args()
    args.lease = args.lease or (1.0 if args.offline else 3.0)
    if not args.offline:
        try:
            print(f"🌐 真实模型：{default_llm().model}（每个 worker 进程各自调用）")
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
    else:
        print("🔌 离线模式：剧本模型（第一轮 0.2 秒、第二轮 1.0 秒，asyncio.sleep）")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
