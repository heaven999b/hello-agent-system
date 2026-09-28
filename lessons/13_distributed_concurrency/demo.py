"""第 13 课 Demo（一）：自己动手 —— 用本课的 jobqueue / session_store，在真实的多进程里复现并解决并发问题。

    python lessons/13_distributed_concurrency/demo.py                # 场景 2、3 调用真实模型
    python lessons/13_distributed_concurrency/demo.py --offline      # 离线：ScriptedLLM（模型耗时用 asyncio.sleep）
    python lessons/13_distributed_concurrency/demo.py --offline --only 3

每个 worker 都是一个独立的操作系统进程（multiprocessing，spawn 方式启动），彼此不共享内存，
只能通过 runs/ 下的 SQLite 文件协作。故障都是真实的操作系统信号：kill -9（SIGKILL）、SIGSTOP / SIGCONT。
（SIGSTOP / SIGCONT 只有 macOS / Linux 才有。）

五个场景：
  1. 原子领取：6 个进程同时抢 30 个任务 —— "先查再改"的错误写法 vs 你的 claim_job
  2. worker 崩溃：kill -9 一个刚建完工单的 worker → 租约过期 → 别人接手 → 幂等键防止重复建单
  3. 僵尸 worker：SIGSTOP 冻结一个跑到一半的 worker → 别人接手并完成 → SIGCONT 解冻后，它的写入被 fence 拒绝
     （对比两种检查点：不认 fence 的 FileCheckpointer 被它悄悄覆盖；带 fence 的 SQLiteCheckpointer 拒绝它）
  4. 同一会话并发写：不做控制（丢失更新）vs 版本号 CAS vs 按会话串行（不调用模型，两种模式输出一致）
  5. 共享配额：持有名额的进程被 kill -9 —— multiprocessing.Semaphore 永久少一个名额，SQLiteSemaphore 租约到期自动归还

跑 Agent 的 worker 进程里是一个事件循环：Agent 是 async 的，心跳是同一个循环里的另一个协程；
本课的 JobQueue 是同步的 sqlite3 代码，所以 worker 通过 jobqueue.AsyncJobQueue（专用线程）调用它（README 3.11 节）。

框架版（agentkit.distributed：WorkerPool + AgentJobHandler + 故障注入时间线）见 demo_agents.py；
吞吐实测（1/2/4 个进程、SQLite 单写者的上限）见 demo_scale.py。
运行产物写在 lessons/13_distributed_concurrency/runs/ 下。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import multiprocessing as mp
import os
import queue
import random
import re
import shutil
import signal
import sqlite3
import sys
import time
import unicodedata
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal

from pydantic import Field

from agentkit import Agent, FileCheckpointer, Hook, ScriptedLLM, ToolContext, call_tool, default_llm, reply, tool
from agentkit.distributed import CheckpointConflict, SQLiteCheckpointer, SQLiteSemaphore

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（与 exercise.py 里的同名函数一致，保证全进程只有一份）。"""
    key = f"{HERE.name}__{name}"  # 例如 "13_distributed_concurrency__jobqueue"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


jobqueue = _load_sibling("jobqueue")
session_store = _load_sibling("session_store")
race = _load_sibling("race")

# 16 条真实感的 IT 报修消息：每条都是一个 Agent 任务
MESSAGES = [
    "3 楼东区的打印机一直卡纸，红灯闪个不停",
    "VPN 连不上，提示证书过期",
    "新来的同事还没有 Git 仓库权限",
    "会议室 B 的投影仪没有信号",
    "我的电脑开机特别慢，要五分钟",
    "邮箱提示空间已满，发不出邮件",
    "财务系统登录后一直白屏",
    "工位网口好像坏了，插上网线没反应",
    "帮我重置一下 Jira 的密码",
    "显示器上有一条竖线，越来越明显",
    "申请一台测试用的 Linux 虚拟机",
    "Outlook 日历同步不到手机上",
    "耳机麦克风在视频会议里没声音",
    "共享盘打开文件特别慢",
    "笔记本电池鼓包了",
    "5 楼的 Wi-Fi 经常断线",
]

SYSTEM_PROMPT = (
    "你是公司的 IT 服务台助手。用户每发来一条报修：先调用一次 create_ticket 建工单"
    "（title 用一句话概括问题；priority：只影响个人用 medium，影响多人或业务中断用 high，咨询类用 low），"
    "然后用一句简短的中文告诉用户工单号。每条报修只建一张工单，不要追问。"
)


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 76 + f"\n  {title}\n" + "═" * 76, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def short(text: str | None, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def pad(text: str, width: int) -> str:
    """按终端显示宽度补空格（中文占两格），让表格对齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    info("".join(pad(h, w) for h, w in zip(headers, widths)))
    info("─" * sum(widths))
    for row in rows:
        info("".join(pad(c, w) for c, w in zip(row, widths)))


def make_logger(who: str, t0: float, enabled: bool = True):
    def log(msg: str) -> None:
        if enabled:
            print(f"   [+{time.time() - t0:5.1f}s] {pad(who, 9)}│ {msg}", flush=True)

    return log


def fresh_dir(name: str) -> Path:
    d = RUNS / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------- 下游系统：工单库


class TicketSystem:
    """模拟下游工单系统（另一个团队的服务，有自己的数据库）。

    create() 支持可选的幂等键：同一个 key 只会建一张工单 —— 由数据库唯一索引保证，
    "建工单"和"记下这个 key"在同一条 INSERT 里完成，不存在"建了但没记下来"的缝隙。
    重复请求返回第一次建的那张。不带 key 就每次都建新的 —— 这正是重复工单的来源。
    """

    SCHEMA = """CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE, job_id INTEGER,
        title TEXT, priority TEXT, created_by TEXT, created_at REAL)"""

    def __init__(self, path: Path):
        self.path = path
        with closing(jobqueue.connect(path)) as conn:
            conn.executescript(self.SCHEMA)

    def create(self, title: str, priority: str, *, job_id: int, created_by: str,
               idempotency_key: str | None = None) -> tuple[str, bool]:
        # 每次调用新开一个连接（相当于一次 HTTP 请求）：同步工具由 agentkit 放进线程池执行，
        # 每次可能落在不同的线程上，而 sqlite3 连接不能跨线程使用
        with closing(jobqueue.connect(self.path)) as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO tickets (idempotency_key, job_id, title, priority, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (idempotency_key, job_id, title, priority, created_by, time.time()),
                )
                return f"T-{1000 + cur.lastrowid}", True
            except sqlite3.IntegrityError:  # 唯一索引冲突：这个幂等键已经建过工单了
                row = conn.execute("SELECT id FROM tickets WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                return f"T-{1000 + row[0]}", False

    def per_job(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        with closing(jobqueue.connect(self.path)) as conn:
            for job_id, tid in conn.execute("SELECT job_id, id FROM tickets ORDER BY id"):
                out.setdefault(job_id, []).append(f"T-{1000 + tid}")
        return out


# ---------------------------------------------------------------- 模型


def offline_llm(worker_id: str, latency: float) -> ScriptedLLM:
    """离线剧本：第一轮调用 create_ticket，拿到工具结果后回复工单号。

    latency 用的是 asyncio.sleep（ScriptedLLM 内置）：等"模型"的时候事件循环照常运转，心跳协程照常续约。
    回复里带上 worker 名，场景 3 用它看出"检查点里最后的回答是谁写的"。
    """

    def policy(messages):
        last = messages[-1]
        if last["role"] == "tool":
            m = re.search(r"T-\d+", last["content"] or "")
            return reply(f"已为你创建工单 {m.group(0) if m else '（未知）'}，IT 同事会尽快联系你。（{worker_id} 回复）")
        text = next(msg["content"] for msg in reversed(messages) if msg["role"] == "user")
        # call_tool 生成的 tool_call_id 是随机串（和真实模型一样），它是幂等键的一部分：
        # 不同进程里各自从 call_1 数起的话，崩溃恢复时幂等键会撞车
        return call_tool("create_ticket", title=text[:20], priority="medium")

    return ScriptedLLM(responder=policy, latency=latency)


# ---------------------------------------------------------------- worker 进程（async）

CURRENT_JOB: ContextVar = ContextVar("demo13_current_job", default=None)


def build_agent(llm, tickets: TicketSystem, cfg: dict, log, notify_q) -> Agent:
    """一个进程一个 Agent，所有任务共用；每个任务的检查点视图在 run / resume 时传入。"""
    wid = cfg["worker_id"]

    @tool(risk="write", timeout_s=120)
    def create_ticket(
        title: Annotated[str, Field(description="一句话概括问题，不超过 30 字")],
        priority: Annotated[Literal["low", "medium", "high"], Field(description="影响个人 medium，影响多人 high，咨询 low")],
        ctx: ToolContext,
    ) -> str:
        """为用户的 IT 报修创建工单，返回工单号。每条报修只调用一次。"""
        job = CURRENT_JOB.get()  # agentkit 把当前的 contextvars 带进工具线程，所以这里拿得到
        # 幂等键 = run_id + tool_call_id。run_id 由任务决定（job-7），tool_call_id 存在检查点里，
        # 所以无论哪个 worker、第几次尝试，重放这次调用时 key 都一样。
        key = ctx.idempotency_key if cfg["idempotent"] else None
        no, created = tickets.create(title, priority, job_id=job.id, created_by=wid, idempotency_key=key)
        if created:
            log(f"🧾 建工单 {no}" + (f"（幂等键 {key}）" if key else "（没带幂等键）"))
        else:
            log(f"♻️  幂等键命中 → 返回已有工单 {no}，没有重复创建")
        if job.payload.get("chaos") == "kill_after_side_effect" and job.attempts == 1:
            log("工单建好了，但结果还没写进检查点、任务也还没提交……")
            notify_q.put(("kill", wid))  # 通知调度器：可以在这个最要命的时刻 kill 我了
            # 这是同步工具：agentkit 在线程池里执行它。这里的阻塞只占住一个工具线程，
            # 事件循环（包括心跳协程）照常运转 —— 直到调度器发来 SIGKILL
            time.sleep(60)
        return f"已创建工单 {no}"

    class FreezePoint(Hook):
        """场景 3：拿到工具结果、准备第二次调用模型时，通知调度器"可以冻结我了"。冻结本身是调度器发来的真实 SIGSTOP。"""

        def before_llm(self, state, messages) -> None:
            job = CURRENT_JOB.get()
            if job and job.payload.get("chaos") == "freeze_mid_run" and job.attempts == 1 and messages[-1]["role"] == "tool":
                log("工具已执行、检查点已写，正在第二次调用模型……")
                notify_q.put(("freeze", wid))

    return Agent(llm, [create_ticket], system_prompt=SYSTEM_PROMPT, name="helpdesk", max_steps=4, hooks=[FreezePoint()])


async def heartbeat(q, job, cfg: dict, log) -> None:
    """续约协程：和 Agent 跑在同一个事件循环里。进程被 SIGKILL / SIGSTOP 时它和整个进程一起停下，租约自然过期。"""
    while True:
        await asyncio.sleep(cfg["heartbeat_s"])
        try:
            await q.heartbeat(job.id, job.fence, cfg["lease_s"])
        except jobqueue.LeaseLostError as e:
            log(f"💔 心跳被拒绝：{short(str(e), 70)}")
            return


async def handle_agent_job(job, q, agent: Agent, ckpt_for, cfg: dict, log) -> None:
    log(f"领取任务 #{job.id}（第 {job.attempts} 次尝试，fence={job.fence}）：{short(job.payload['text'], 22)}")
    hb = asyncio.create_task(heartbeat(q, job, cfg, log))
    token = CURRENT_JOB.set(job)
    ckpt = ckpt_for(job)
    try:
        run_id = f"job-{job.id}"  # 由任务决定，而不是随机生成：换了 worker 也能找到同一个检查点
        existing = ckpt.load(run_id)
        existing = await existing if asyncio.iscoroutine(existing) else existing
        if existing is not None:
            log("发现前任留下的检查点 → 从断点继续，而不是从头再来")
            result = await agent.resume(run_id, checkpointer=ckpt)
        else:
            result = await agent.run(job.payload["text"], run_id=run_id, checkpointer=ckpt,
                                     metadata={"tenant_id": job.tenant_id, "user_id": job.payload["user"]})
        # 注意：这里没有先问"我的租约还在吗"再提交 —— 问了也没用（问完和提交之间还可能被冻结）。
        # 挡住过期持有者的，是存储在写入那一刻对 fence 的检查。
        if result.ok:
            await q.complete(job.id, job.fence, result.output or "")
            log(f"✅ 完成 #{job.id}：{short(result.output, 46)}")
        else:
            status = await q.fail(job.id, job.fence, f"{result.status}: {result.stop_reason}")
            log(f"⚠️  任务 #{job.id} 失败（{result.stop_reason}）→ 状态变为 {status}")
    except jobqueue.LeaseLostError as e:
        log(f"❌ 提交被拒绝（LeaseLostError）：{short(str(e), 60)}")
    except CheckpointConflict as e:
        log(f"❌ 检查点写入被拒绝（CheckpointConflict）：run {e.run_id} 已被 {e.writer} 接管 → 立刻停手")
    finally:
        hb.cancel()
        CURRENT_JOB.reset(token)


async def _agent_worker(cfg: dict, notify_q) -> None:
    log = make_logger(cfg["worker_id"], cfg["t0"], cfg["verbose"])
    q = jobqueue.AsyncJobQueue(cfg["db"])  # 同步的 JobQueue 放进专用线程：等写锁时不卡住事件循环
    tickets = TicketSystem(Path(cfg["tickets_db"]))
    llm = offline_llm(cfg["worker_id"], cfg["latency"]) if cfg["offline"] else default_llm()
    agent = build_agent(llm, tickets, cfg, log, notify_q)
    if cfg["checkpointer"] == "file":
        file_ckpt = FileCheckpointer(cfg["ckpt_dir"])

        def ckpt_for(job):
            return file_ckpt  # ❌ 不认 fence：谁来写都照单全收
    else:
        base = SQLiteCheckpointer(cfg["ckpt_db"])
        await base.setup()

        def ckpt_for(job):
            # ✅ 每次领取用这次的 fence 创建一个视图：load 时"接管"这个 run，fence 更旧的写入一律拒绝
            return base.fenced(job.fence, writer=cfg["worker_id"])
    log(f"上线（pid {os.getpid()}）")
    try:
        while True:
            job = await q.claim(cfg["worker_id"], cfg["lease_s"])
            if job is None:
                if await q.all_done():
                    break
                await asyncio.sleep(cfg["poll_s"] * random.uniform(0.5, 1.5))  # 带抖动的轮询
                continue
            await handle_agent_job(job, q, agent, ckpt_for, cfg, log)
    finally:
        await agent.aclose()
        await q.close()
        if cfg["checkpointer"] != "file":
            await base.close()
    log("队列空了，下线")


def agent_worker(cfg: dict, notify_q=None) -> None:
    """worker 进程入口（spawn 启动）：一个进程 = 一个事件循环，循环"领取 → 执行 → 提交"，直到所有任务都结束。"""
    asyncio.run(_agent_worker(cfg, notify_q))


# ---------------------------------------------------------------- worker：会话写入（场景 4）


def session_worker(cfg: dict, barrier) -> None:
    """处理"用户在同一个会话里发来的消息"：读会话 → 调模型（sleep 代表模型耗时）→ 把这条消息写回会话。

    这是一个**没有事件循环的同步进程**：time.sleep 和阻塞的 sqlite3 调用只挡住它自己，不影响任何别人。
    """
    wid, sid = cfg["worker_id"], cfg["session_id"]
    q = jobqueue.JobQueue(cfg["db"])
    store = session_store.SessionStore(cfg["sessions_db"])
    impl = _load_sibling(cfg["impl"])  # 优先用你在 exercise.py 里写的 update_session_with_retry
    barrier.wait()
    while True:
        job = q.claim(wid, 30)
        if job is None:
            if q.all_done():
                break
            time.sleep(random.uniform(0.005, 0.015))
            continue
        seq, stats = job.payload["seq"], {"model_calls": 0, "conflicts": 0}

        def add_reply(data: dict) -> dict:
            messages = data.setdefault("messages", [])
            if any(m["seq"] == seq for m in messages):  # 这条消息已经处理过了：更新函数本身也要幂等
                return data
            stats["model_calls"] += 1
            time.sleep(cfg["think_s"])  # "调用模型"：回复依赖当前的对话历史，所以历史变了就得重新调用
            messages.append({"seq": seq, "by": wid})
            return data

        if cfg["mode"] == "unsafe":
            current = store.get(sid)
            store.put_unsafe(sid, add_reply(current.data))  # ❌ 不看版本号，直接覆盖
        else:
            def count_conflict(_attempt, _err):
                stats["conflicts"] += 1

            try:
                impl.update_session_with_retry(store, sid, add_reply, max_attempts=100, backoff_s=0.01,
                                               on_conflict=count_conflict)
            except session_store.ConflictError as e:
                q.fail(job.id, job.fence, str(e))
                continue
        q.complete(job.id, job.fence, json.dumps(stats))


# ---------------------------------------------------------------- 配额持有者（场景 5）


def quota_holder_mp(sem, ready_q) -> None:
    sem.acquire()  # 拿走 multiprocessing 信号量的一个名额
    ready_q.put(os.getpid())
    time.sleep(60)


def quota_holder_sqlite(db: str, ready_q) -> None:
    async def main():
        sem = SQLiteSemaphore(db, "llm-quota", limit=2, lease_seconds=1.0)
        await sem.setup()
        async with sem.slot():  # 持有期间每 1/3 租约自动续约一次
            ready_q.put(os.getpid())
            await asyncio.sleep(60)

    asyncio.run(main())


# =====================================================================
# 调度器（父进程）
# =====================================================================


def spawn(ctx, target, *args) -> mp.Process:
    p = ctx.Process(target=target, args=args, daemon=True)
    p.start()
    return p


def worker_cfg(workdir: Path, worker_id: str, args, t0: float, *, verbose: bool = True, idempotent: bool = True,
               checkpointer: str = "sqlite") -> dict:
    return {
        "worker_id": worker_id, "db": str(workdir / "queue.db"), "tickets_db": str(workdir / "tickets.db"),
        "ckpt_dir": str(workdir / "checkpoints"), "ckpt_db": str(workdir / "checkpoints.db"), "checkpointer": checkpointer,
        "offline": args.offline, "latency": args.latency, "lease_s": args.lease, "heartbeat_s": args.lease / 4,
        "poll_s": 0.05 if args.offline else 0.2, "idempotent": idempotent, "verbose": verbose, "t0": t0,
    }


async def _create_checkpoint_db(path: Path) -> None:
    base = SQLiteCheckpointer(path)
    await base.setup()
    await base.close()


def enqueue_messages(workdir: Path, n: int, chaos: dict[int, str] | None = None):
    # 所有数据库文件都由调度器先建好（包括切换到 WAL 模式），再拉起 worker：
    # 几个进程同时新建同一个 SQLite 文件、同时切换 WAL 模式时，会有进程直接收到 "database is locked"
    q = jobqueue.JobQueue(workdir / "queue.db")
    TicketSystem(workdir / "tickets.db")
    asyncio.run(_create_checkpoint_db(workdir / "checkpoints.db"))
    for i in range(n):
        payload = {"text": MESSAGES[i % len(MESSAGES)], "user": f"u{100 + i}"}
        if chaos and i + 1 in chaos:
            payload["chaos"] = chaos[i + 1]
        # 幂等键来自上游（比如聊天前端给每条消息分配的 message_id）：用户连点两次也只入队一次
        q.enqueue("acme", payload, idempotency_key=f"msg-{i + 1}")
    return q


def join_all(procs, timeout: float) -> None:
    deadline = time.time() + timeout
    for p in procs:
        p.join(max(0.1, deadline - time.time()))
        if p.is_alive():
            print(f"   ⚠️ worker 进程 {p.pid} 超时未退出，强制结束", flush=True)
            p.kill()
            p.join()


def wait_until(cond, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def pick_impl(probe) -> tuple[str, str]:
    """exercise.py 写完了就用你的实现；还没写完（或探测失败）就用参考答案。返回 (文件路径, 说明)。"""
    try:
        if probe(_load_sibling("exercise")):
            return str(HERE / "exercise.py"), "exercise.py，你的实现 👍"
    except Exception:  # noqa: BLE001 —— NotImplementedError 或者实现有 bug，都退回参考答案
        pass
    return str(HERE / "solution.py"), "solution.py，exercise.py 还没写完，先用参考答案"


# ---------------------------------------------------------------- 场景 1：原子领取


def _probe_claim(ex) -> bool:
    probe = fresh_dir("probe_claim") / "q.db"
    jobqueue.JobQueue(probe).enqueue("t", {}, "k")
    with closing(jobqueue.connect(probe)) as conn:
        job = ex.claim_job(conn, "probe", 30)
        return job is not None and job.fence == 1 and ex.claim_job(conn, "probe", 30) is None


def scenario_claim_race(ctx, args) -> None:
    banner("场景 1：原子领取 —— 6 个进程同时抢 30 个任务，会不会有任务被领两次？")
    impl_path, impl_label = pick_impl(_probe_claim)
    info("6 个真实的 python 进程，各用各的 sqlite3 连接，站在同一条起跑线上一起开抢（race.py）。")
    info("每条 SQL 执行前停 1 毫秒，把本来就存在的竞态窗口放大，让错误写法稳定地暴露出来。")
    info(f"正确写法用的 claim_job 来自：{impl_label}")
    rows = []
    for label, impl in [("❌ 先 SELECT 再无条件 UPDATE", "naive"), (f"✅ claim_job（{Path(impl_path).name}）", impl_path)]:
        workdir = fresh_dir(f"claim_{'naive' if impl == 'naive' else 'correct'}")
        q = jobqueue.JobQueue(workdir / "queue.db")
        for i in range(30):
            q.enqueue("acme", {"n": i}, f"job-{i}")
        q.close()
        t = time.time()
        results = race.run_race(6, "claim", db=workdir / "queue.db", impl=impl, lease=30, widen=True)
        elapsed = time.time() - t
        claims = [c["id"] for r in results for c in r["claims"]]
        dup = len(claims) - len(set(claims))
        per = "/".join(str(len(r["claims"])) for r in results)
        rows.append([label, str(len(claims)), f"{dup}" + (" ❌" if dup else " ✅"), per, f"{elapsed:.2f}s"])
    print()
    table(["写法", "领取次数", "重复领取", "每个进程领到", "耗时（含起进程）"], rows, [32, 10, 10, 20, 16])
    takeaway("错误写法在单进程测试里完全正常，一到多进程就把同一个任务发给好几个 worker。\n"
             "      正确写法把\"检查\"放进写入本身：BEGIN IMMEDIATE，或者带条件的 UPDATE + 检查影响行数（练习 (a)）。")


# ---------------------------------------------------------------- 场景 2：worker 崩溃 + 幂等


def run_crash_case(ctx, args, idempotent: bool) -> dict:
    workdir = fresh_dir(f"crash_{'idem' if idempotent else 'noidem'}")
    n_jobs = 3
    q = enqueue_messages(workdir, n_jobs, chaos={1: "kill_after_side_effect"})
    t0 = time.time()
    log = make_logger("调度器", t0)
    notify_q = ctx.Queue()
    procs = {wid: spawn(ctx, agent_worker, worker_cfg(workdir, wid, args, t0, idempotent=idempotent), notify_q)
             for wid in ("worker-1", "worker-2")}
    try:
        _, victim = notify_q.get(timeout=60 if args.offline else 150)
        procs[victim].kill()  # SIGKILL
        procs[victim].join()
        log(f"💥 kill -9 {victim}（退出码 {procs[victim].exitcode}）：进程瞬间消失 —— 不释放租约、不写检查点、不留遗言")
        log(f"   任务 #1 的租约最多还剩 {args.lease:.1f} 秒，到期前没人能接手；拉起替补 worker-3（相当于 K8s 重建 Pod）")
        procs["worker-3"] = spawn(ctx, agent_worker, worker_cfg(workdir, "worker-3", args, t0, idempotent=idempotent), notify_q)
    except queue.Empty:
        log("⚠️ 没等到崩溃信号（模型没有调用 create_ticket？），跳过 kill")
    join_all([p for p in procs.values() if p.is_alive()], timeout=600)
    return {"stats": q.stats(), "tickets": TicketSystem(workdir / "tickets.db").per_job(), "job1": q.get(1), "n_jobs": n_jobs}


def scenario_crash(ctx, args) -> None:
    banner("场景 2：worker 崩溃 —— 租约过期、别人接手，会不会重复建工单？")
    info("3 条报修，2 个 worker 进程。处理任务 #1 的 worker 刚在工单系统里建完工单，就被 kill -9 了。")
    info(f"租约 {args.lease:.1f} 秒、每 {args.lease / 4:.2f} 秒心跳一次（生产中一般 30 秒到几分钟，这里调短是为了演示快）。")
    info("接手的 worker 从检查点恢复：检查点里有'模型决定调用 create_ticket'，但没有结果 → 这次调用会被重放。")

    results = {}
    for idempotent in (False, True):
        step("第一轮：工具调用下游时【不带】幂等键" if not idempotent else "第二轮：工具调用下游时【带上】幂等键（run_id + tool_call_id）")
        results[idempotent] = run_crash_case(ctx, args, idempotent)

    def describe(r: dict) -> list[str]:
        per_job, job1 = r["tickets"], r["job1"]
        total = sum(len(v) for v in per_job.values())
        dup = total - len(per_job)
        return [
            f"{r['stats'].get('succeeded', 0)}/{r['n_jobs']}",
            f"{total}" + (f"（重复 {dup} 张 ❌）" if dup else "（✅ 无重复）"),
            "、".join(per_job.get(1, [])) or "-",
            f"{job1.attempts} 次 / fence={job1.fence}",
        ]

    print()
    rows = [["不带幂等键", *describe(results[False])], ["带幂等键", *describe(results[True])]]
    table(["", "任务完成", "工单总数（应为 3）", "任务 #1 的工单", "任务 #1 领取次数"], rows, [12, 10, 22, 20, 20])
    takeaway("租约保证了'不丢'：worker 死了，任务过期后自动被别人接手（at-least-once）。\n"
             "      但'不丢'的代价是'可能做两次' —— 只有幂等键才能保证'做两次 = 做一次'（effectively-once）。\n"
             "      幂等键要传给【下游】，由下游在同一个事务里'执行 + 记录 key'。进程内存里的 IdempotencyStore\n"
             "      会随进程一起死掉；跨进程的 SQLiteIdempotencyStore（demo_agents.py）只记'成功之后'的结果，\n"
             "      崩溃恰好落在'下游已执行、还没记下'之间时同样拦不住 —— 最后一道防线永远是下游自己认 key。")


# ---------------------------------------------------------------- 场景 3：僵尸 worker（真实的 SIGSTOP）


def final_checkpoint(workdir: Path, kind: str) -> tuple[str | None, str | None]:
    """父进程直接读检查点：(最终回答, 最后写入者)。FileCheckpointer 不记写入者。"""
    if kind == "file":
        path = workdir / "checkpoints" / "job-1.json"
        return (json.loads(path.read_text(encoding="utf-8")).get("output"), None) if path.exists() else (None, None)
    with closing(sqlite3.connect(workdir / "checkpoints.db")) as conn:
        row = conn.execute("SELECT state, writer FROM agent_runs WHERE run_id = 'job-1'").fetchone()
    return (json.loads(row[0]).get("output"), row[1]) if row else (None, None)


def run_zombie_case(ctx, args, kind: str) -> dict:
    workdir = fresh_dir(f"zombie_{kind}")
    q = enqueue_messages(workdir, 1, chaos={1: "freeze_mid_run"})
    t0 = time.time()
    log = make_logger("调度器", t0)
    notify_q = ctx.Queue()
    first = spawn(ctx, agent_worker, worker_cfg(workdir, "worker-1", args, t0, checkpointer=kind), notify_q)
    wait_until(lambda: (q.get(1).status != "queued") or not first.is_alive(), 60)  # 等 worker-1 先领走任务
    second = spawn(ctx, agent_worker, worker_cfg(workdir, "worker-2", args, t0, checkpointer=kind), notify_q)
    froze = False
    try:
        notify_q.get(timeout=60 if args.offline else 150)
        os.kill(first.pid, signal.SIGSTOP)
        froze = True
        log(f"🧊 SIGSTOP worker-1（pid {first.pid}）：进程被操作系统冻结 —— 它的心跳协程也停了，但它自己毫不知情")
        taken = wait_until(lambda: q.get(1).status == "succeeded", 60 if args.offline else 180)
        log("接手者已经完成任务 #1" if taken else "⚠️ 等了很久也没人接手完成")
    except queue.Empty:
        log("⚠️ 没等到冻结信号（模型没有调用 create_ticket？），跳过冻结")
    finally:
        if froze:
            os.kill(first.pid, signal.SIGCONT)
            log("▶️  SIGCONT worker-1：解冻。它从被冻结的那一行继续执行，手里还攥着 fence=1 的旧租约")
    join_all([first, second], timeout=600)
    job = q.get(1)
    output, writer = final_checkpoint(workdir, kind)
    return {"job": job, "ckpt_output": output, "ckpt_writer": writer,
            "tickets": sum(len(v) for v in TicketSystem(workdir / "tickets.db").per_job().values())}


def scenario_zombie(ctx, args) -> None:
    banner("场景 3：僵尸 worker —— 被冻结的 worker 醒来后，还能写进去吗？")
    info("worker-1 跑到一半（工具已执行、正在第二次调用模型）时，调度器对它发 SIGSTOP：进程没死，但时间停了。")
    info("真实世界里这叫 stop-the-world：GC 停顿、虚拟机被迁移、容器被 CPU 限流……")
    info(f"租约 {args.lease:.1f} 秒后过期，worker-2 接手、从检查点继续并完成；然后调度器 SIGCONT 解冻 worker-1。")
    info("跑两遍，只换检查点：FileCheckpointer（不认 fence）vs SQLiteCheckpointer.fenced（带 fence，agentkit.distributed）。")
    results = {}
    for kind, label in (("file", "FileCheckpointer（不认 fence）"), ("sqlite", "SQLiteCheckpointer.fenced（带 fence）")):
        step(f"检查点：{label}")
        results[kind] = run_zombie_case(ctx, args, kind)

    def author(text: str | None) -> str:
        m = re.search(r"（(worker-\d+) 回复）", text or "")
        return m.group(1) if m else "（真实模型，见上方日志）"

    print()
    rows = []
    for kind, label in (("file", "FileCheckpointer"), ("sqlite", "SQLiteCheckpointer.fenced")):
        r = results[kind]
        job = r["job"]
        consistent = r["ckpt_output"] == job.result
        who = author(r["ckpt_output"]) + (f"（writer={r['ckpt_writer']}）" if r["ckpt_writer"] else "")
        rows.append([label, f"{job.status} / {job.worker_id} / fence={job.fence}", who,
                     "✅ 一致" if consistent else "❌ 不一致（被僵尸覆盖）"])
    table(["检查点", "队列：状态 / 提交者 / fence", "检查点里最终回答的作者", "检查点 = 提交的结果？"], rows, [28, 32, 30, 22])
    takeaway("worker-1 醒来时并不知道自己的租约早就过期了 —— 它'以为'自己还是主人。\n"
             "      队列的提交有 fence 挡着（练习 (b)）；但检查点也是'受租约保护的写入'：\n"
             "      FileCheckpointer 不认 fence，僵尸的最后一步悄悄覆盖了接手者的结果，而且没有任何报错；\n"
             "      SQLiteCheckpointer.fenced 在接手时把表里的 fence 改成新的，僵尸再写就是 CheckpointConflict。\n"
             "      凡是受租约保护的写入，都要由存储在写入那一刻检查 fence。")


# ---------------------------------------------------------------- 场景 4：同一会话并发写


def _probe_session(ex) -> bool:
    probe = fresh_dir("probe_session") / "probe.db"
    store = session_store.SessionStore(probe)
    out = ex.update_session_with_retry(store, "probe", lambda d: {**d, "ok": True}, backoff_s=0)
    return bool(out.data.get("ok") and store.get("probe").data.get("ok"))


def run_session_case(ctx, args, mode: str, grouped: bool, impl: str, n_msgs: int, n_workers: int) -> dict:
    workdir = fresh_dir(f"session_{mode}_{'grouped' if grouped else 'free'}")
    q = jobqueue.JobQueue(workdir / "queue.db")
    session_store.SessionStore(workdir / "sessions.db")
    for seq in range(1, n_msgs + 1):  # 用户在同一个会话里连发 n_msgs 条消息
        q.enqueue("acme", {"seq": seq}, idempotency_key=f"sess-42:msg-{seq}", group_key="sess-42" if grouped else None)
    barrier = ctx.Barrier(n_workers + 1)
    cfg = {"db": str(workdir / "queue.db"), "sessions_db": str(workdir / "sessions.db"), "session_id": "sess-42",
           "mode": mode, "impl": impl, "think_s": args.think}
    procs = [spawn(ctx, session_worker, {**cfg, "worker_id": f"worker-{i + 1}"}, barrier) for i in range(n_workers)]
    barrier.wait(timeout=120)
    t0 = time.time()
    join_all(procs, timeout=300)
    jobs = q.list_jobs()
    elapsed = max(j.updated_at for j in jobs) - t0
    stats = [json.loads(j.result) for j in jobs if j.status == "succeeded"]
    saved = [m["seq"] for m in session_store.SessionStore(workdir / "sessions.db").get("sess-42").data.get("messages", [])]
    return {
        "saved": len(saved), "in_order": saved == sorted(saved),
        "model_calls": sum(s["model_calls"] for s in stats), "conflicts": sum(s["conflicts"] for s in stats),
        "elapsed": elapsed, "failed": sum(1 for j in jobs if j.status != "succeeded"),
    }


def scenario_session(ctx, args) -> None:
    banner("场景 4：同一会话并发写 —— 丢失更新 vs 版本号 CAS vs 按会话串行")
    n_msgs, n_workers = 16, 4
    impl_path, impl_label = pick_impl(_probe_session)
    impl = Path(impl_path).stem
    info(f"用户在同一个会话里连发 {n_msgs} 条消息，{n_workers} 个 worker 进程同时处理。每条消息：读会话 → 调模型 → 写回。")
    info(f"本节不调用真实模型：会话 worker 是没有事件循环的同步进程，用 sleep {args.think}s 代表模型耗时；离线和在线模式行为一致。")
    info(f"B、C 使用的 update_session_with_retry 来自：{impl_label}")

    cases = [
        ("A. 不做并发控制（最后写入者胜）", "unsafe", False),
        ("B. 乐观并发：版本号 CAS + 重试", "cas", False),
        ("C. 按会话串行：队列 group_key", "cas", True),
    ]
    rows = []
    for label, mode, grouped in cases:
        r = run_session_case(ctx, args, mode, grouped, impl, n_msgs, n_workers)
        lost = n_msgs - r["saved"]
        wasted = r["model_calls"] - n_msgs + lost
        rows.append([
            label, f"{r['saved']}/{n_msgs}", f"{lost}" + (" ❌" if lost else " ✅"),
            f"{r['model_calls']}" + (f"（浪费 {wasted}）" if wasted > 0 else ""),
            str(r["conflicts"]) if mode == "cas" else "-",
            "-" if mode == "unsafe" else ("✅ 是" if r["in_order"] else "❌ 否"),
            f"{r['elapsed']:.2f}s",
        ])
    print()
    table(["方案", "保存的消息", "丢失", "模型调用次数", "CAS 冲突", "顺序正确", "耗时"], rows, [34, 12, 8, 18, 10, 10, 8])
    takeaway("A 没有任何报错，数据却悄悄丢了 —— 丢失更新是最危险的并发 bug，因为它不会告诉你。\n"
             "      B 一条不丢，但每次冲突都要'重新调用模型'（浪费钱和时间），而且消息处理顺序是乱的。\n"
             "      C 让同一会话的消息排队、一次只处理一条：零冲突、严格有序；不同会话之间仍然完全并行。\n"
             "      对话类 Agent 通常选 C 作为主方案，再保留 B（CAS）作为兜底的安全网。")


# ---------------------------------------------------------------- 场景 5：共享配额 + kill -9


async def _sqlite_quota_after_kill(db: str) -> tuple[int, float]:
    sem = SQLiteSemaphore(db, "llm-quota", limit=2, lease_seconds=1.0)
    await sem.setup()
    in_use = await sem.in_use()
    t0 = time.monotonic()
    async with sem.slot(timeout=10):  # 两个名额同时拿到，说明死者的名额已经回来了
        async with sem.slot(timeout=10):
            waited = time.monotonic() - t0
    await sem.close()
    return in_use, waited


def scenario_quota(ctx, args) -> None:
    banner("场景 5：共享配额 —— 持有名额的进程被 kill -9，名额还回得来吗？")
    info("模型网关只给了 2 个并发名额，所有 worker 进程共用。一个进程拿着名额时被 kill -9。")
    rows = []

    sem = ctx.BoundedSemaphore(2)
    ready = ctx.Queue()
    holder = spawn(ctx, quota_holder_mp, sem, ready)
    ready.get(timeout=60)
    holder.kill()
    holder.join()
    got = [sem.acquire(timeout=0.5), sem.acquire(timeout=2.0)]
    rows.append(["multiprocessing.BoundedSemaphore(2)", f"{sum(got)} 个",
                 "❌ 永久少了 1 个（没人会替死者 release）" if sum(got) < 2 else "✅"])

    db = str(fresh_dir("quota") / "quota.db")
    holder = spawn(ctx, quota_holder_sqlite, db, ready)
    ready.get(timeout=60)
    holder.kill()
    holder.join()
    in_use, waited = asyncio.run(_sqlite_quota_after_kill(db))
    rows.append(["SQLiteSemaphore(limit=2, 租约 1 秒)", "2 个",
                 f"✅ kill 后还占着 {in_use} 个，{waited:.2f} 秒后租约到期自动归还"])
    print()
    table(["实现", "之后能拿到", "结果"], rows, [38, 12, 50])
    takeaway("multiprocessing.Semaphore 只是操作系统里的一个计数器：谁 acquire 了、谁死了，它一概不知，\n"
             "      持有者被 kill -9 之后那个名额永远还不回来（跨机器时连这个计数器都没有）。\n"
             "      SQLiteSemaphore 的每个名额都是带租约的一行记录：持有期间自动续约，持有者一死，续约停止，\n"
             "      租约到期后名额自动释放 —— 这就是问题 5 卡片里'分布式信号量的许可必须带租约'。")


# =====================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="第 13 课 Demo（一）：自己动手的多进程并发")
    parser.add_argument("--offline", action="store_true", help="使用离线剧本（ScriptedLLM），不调用真实模型")
    parser.add_argument("--lease", type=float, default=None, help="租约秒数（默认：离线 1.5 秒，真实模型 3 秒）")
    parser.add_argument("--only", default="1,2,3,4,5", help="只运行指定场景，如 --only 2,3")
    args = parser.parse_args()
    args.lease = args.lease or (1.5 if args.offline else 3.0)
    args.latency = 0.3  # 离线模式下每次"模型调用"的耗时（asyncio.sleep）
    args.think = 0.05  # 场景 4 里每次"模型调用"的耗时

    if args.offline:
        print("🔌 离线模式：ScriptedLLM（每次调用 asyncio.sleep 0.3 秒）")
    else:
        try:
            model = default_llm().model
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"🌐 真实模型：{model}（场景 2、3 调用模型，其余场景不调用）")
    print(f"   worker 是真正的独立进程（spawn），共享的只有 {RUNS.relative_to(HERE.parents[1])}/ 下的 SQLite 文件。")

    ctx = mp.get_context("spawn")  # macOS / Windows 默认就是 spawn；Linux 上显式指定，行为一致
    started = time.time()
    scenarios = {"1": scenario_claim_race, "2": scenario_crash, "3": scenario_zombie, "4": scenario_session,
                 "5": scenario_quota}
    for key in args.only.replace(" ", "").split(","):
        scenarios[key](ctx, args)

    banner("小结")
    info("1. 原子领取：检查必须放进写入本身（写锁 / 条件 UPDATE），多进程下'先查再改'一定会重复。")
    info("2. 投递语义：租约让任务'不丢'（at-least-once），幂等键让'重复执行'无害 → 效果上恰好一次。")
    info("3. fencing token：过期的持有者（僵尸）必须被存储端拒绝 —— 队列提交如此，检查点写入也如此。")
    info("4. 会话并发写：不控制就会静悄悄丢数据；CAS 能兜底，按会话串行是对话类 Agent 的首选。")
    info("5. 跨进程的配额：名额必须带租约，否则一次 kill -9 就永久少一个。")
    info(f"总耗时 {time.time() - started:.0f} 秒。运行产物在 {RUNS.relative_to(HERE.parents[1])}/")


if __name__ == "__main__":
    main()
