"""第 10 课 Demo：高并发与分布式执行 —— 多个 worker 进程 + 一个共享的 SQLite。

    python lessons/10_distributed_concurrency/demo.py             # 真实模型（任务很少，模型并发 ≤ 3）
    python lessons/10_distributed_concurrency/demo.py --offline   # 离线：ScriptedLLM + sleep 模拟模型耗时

每个 worker 都是一个独立的操作系统进程（multiprocessing，spawn 方式启动），彼此不共享内存，
只能通过同一个 SQLite 文件协作 —— 和"多台机器 + 一个共享数据库"的并发语义一样。

四个场景：
  1. 横向扩展：同一批 Agent 任务，1/2/4/8 个 worker 的吞吐；以及模型并发上限如何给吞吐"封顶"
  2. worker 崩溃：kill -9 一个刚建完工单的 worker → 租约过期 → 别的 worker 接手 → 幂等键防止重复建单
  3. 僵尸 worker：worker 卡住超过租约 → 任务被接手 → 它醒来后提交的旧结果被 fencing token 拒绝
  4. 同一会话并发写：不做控制（丢失更新）vs 版本号 CAS vs 按会话串行（不调用模型，两种模式输出一致）

运行产物（队列库、工单库、检查点、会话库）写在 lessons/10_distributed_concurrency/runs/ 下。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing as mp
import queue
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal

from pydantic import Field

from agentkit import Agent, FileCheckpointer, ScriptedLLM, ToolContext, call_tool, default_llm, reply, tool

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（与 exercise.py 里的同名函数一致，保证全进程只有一份）。"""
    key = f"{HERE.name}__{name}"  # 例如 "10_distributed_concurrency__jobqueue"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


jobqueue = _load_sibling("jobqueue")
session_store = _load_sibling("session_store")

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
        # 每次调用新开一个连接（相当于一次 HTTP 请求）：工具在 agentkit 的工具线程里执行，
        # 而 sqlite3 连接不能跨线程使用
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


class LimitedLLM:
    """给任意 LLM 套一个"全局并发上限"：所有 worker 进程共享一个信号量，同一时刻最多 N 个模型请求在途。

    这是第 5 张卡片里的"并发信号量"方案的单机版。跨机器时要换成分布式信号量（如 Redis + 租约），
    否则持有许可的进程一旦被 kill，许可就永远还不回来了。
    """

    def __init__(self, inner, sem):
        self.inner, self.sem, self.model = inner, sem, inner.model

    def chat(self, messages, tools=None, **kwargs):
        with self.sem:
            return self.inner.chat(messages, tools, **kwargs)


def offline_llm(latency: float) -> ScriptedLLM:
    """离线剧本：第一轮调用 create_ticket，拿到工具结果后回复工单号。sleep 模拟模型推理耗时。"""

    def policy(messages):
        time.sleep(latency)
        last = messages[-1]
        if last["role"] == "tool":
            m = re.search(r"T-\d+", last["content"] or "")
            return reply(f"已为你创建工单 {m.group(0) if m else '（未知）'}，IT 同事会尽快联系你。")
        text = next(msg["content"] for msg in reversed(messages) if msg["role"] == "user")
        response = call_tool("create_ticket", title=text[:20], priority="medium")
        # 真实模型返回的 tool_call_id 是全局唯一的随机串；这里也用随机串，
        # 因为它会成为幂等键的一部分（agentkit 默认的 call_1、call_2 在不同进程里会重复）
        response.tool_calls[0].id = f"call_{uuid.uuid4().hex[:12]}"
        return response

    return ScriptedLLM([policy] * 6)


# ---------------------------------------------------------------- worker：心跳线程


class Heartbeat(threading.Thread):
    """后台续约：每 interval_s 秒把租约延长到 now + lease_s。worker 一死，心跳就停，租约自然过期。"""

    def __init__(self, db: str, job_id: int, fence: int, lease_s: float, interval_s: float):
        super().__init__(daemon=True)
        self.db, self.job_id, self.fence = db, job_id, fence
        self.lease_s, self.interval_s = lease_s, interval_s
        self._halt, self._paused, self.lost = threading.Event(), threading.Event(), threading.Event()

    def run(self) -> None:
        q = jobqueue.JobQueue(self.db)  # 线程要用自己的连接
        try:
            while not self._halt.wait(self.interval_s):
                if self._paused.is_set():
                    continue
                try:
                    q.heartbeat(self.job_id, self.fence, self.lease_s)
                except jobqueue.LeaseLostError:
                    self.lost.set()
                    return
        finally:
            q.close()

    def pause(self) -> None:  # 模拟"整个进程被冻结"：心跳线程也停了
        self._paused.set()

    def stop(self) -> None:
        self._halt.set()


# ---------------------------------------------------------------- worker：Agent 任务


def build_agent(llm, tickets: TicketSystem, job, worker_id: str, cfg: dict, log, crash_q) -> Agent:
    @tool(risk="write", timeout_s=120)
    def create_ticket(
        title: Annotated[str, Field(description="一句话概括问题，不超过 30 字")],
        priority: Annotated[Literal["low", "medium", "high"], Field(description="影响个人 medium，影响多人 high，咨询 low")],
        ctx: ToolContext,
    ) -> str:
        """为用户的 IT 报修创建工单，返回工单号。每条报修只调用一次。"""
        # 幂等键 = run_id + tool_call_id。run_id 由任务 id 决定（job-7），tool_call_id 存在检查点里，
        # 所以无论哪个 worker、第几次尝试，重放这次调用时 key 都一样。
        key = ctx.idempotency_key if cfg["idempotent"] else None
        no, created = tickets.create(title, priority, job_id=job.id, created_by=worker_id, idempotency_key=key)
        if created:
            log(f"🧾 建工单 {no}" + (f"（幂等键 {key}）" if key else "（没带幂等键）"))
        else:
            log(f"♻️  幂等键命中 → 返回已有工单 {no}，没有重复创建")
        if job.payload.get("chaos") == "kill_after_side_effect" and job.attempts == 1:
            log("工单建好了，但结果还没写进检查点、任务也还没提交……")
            crash_q.put(worker_id)  # 通知调度器：可以在这个最要命的时刻 kill 我了
            time.sleep(60)
        return f"已创建工单 {no}"

    return Agent(llm, [create_ticket], system_prompt=SYSTEM_PROMPT, name="helpdesk", max_steps=4,
                 checkpointer=FileCheckpointer(cfg["ckpt_dir"]))


def handle_agent_job(job, q, tickets: TicketSystem, base_llm, cfg: dict, log, crash_q) -> None:
    wid = cfg["worker_id"]
    log(f"领取任务 #{job.id}（第 {job.attempts} 次尝试，fence={job.fence}）：{short(job.payload['text'], 22)}")
    hb = Heartbeat(cfg["db"], job.id, job.fence, cfg["lease_s"], cfg["heartbeat_s"])
    hb.start()
    try:
        llm = offline_llm(cfg["latency"]) if cfg["offline"] else base_llm
        if cfg.get("sem") is not None:
            llm = LimitedLLM(llm, cfg["sem"])
        agent = build_agent(llm, tickets, job, wid, cfg, log, crash_q)
        run_id = f"job-{job.id}"  # 由任务决定，而不是随机生成：换了 worker 也能找到同一个检查点
        if agent.checkpointer.load(run_id) is not None:
            log("发现前任留下的检查点 → 从断点继续，而不是从头再来")
            result = agent.resume(run_id)
        else:
            result = agent.run(job.payload["text"], run_id=run_id,
                               metadata={"tenant_id": job.tenant_id, "user_id": job.payload["user"]})

        if job.payload.get("chaos") == "freeze_before_complete" and job.attempts == 1:
            hb.pause()
            log(f"😵 Agent 跑完了，正要提交时卡住 {cfg['freeze_s']:.0f} 秒（模拟 GC 停顿 / 虚拟机被暂停：心跳也停了）")
            time.sleep(cfg["freeze_s"])
            log("醒了！以为自己还持有租约，继续提交结果……")

        if hb.lost.is_set():
            log("心跳发现租约已经被别人接手 → 放弃提交")
        elif result.ok:
            q.complete(job.id, job.fence, result.output or "")
            log(f"✅ 完成 #{job.id}：{short(result.output, 34)}")
        else:
            status = q.fail(job.id, job.fence, f"{result.status}: {result.stop_reason}")
            log(f"⚠️  任务 #{job.id} 失败（{result.stop_reason}）→ 状态变为 {status}")
    except jobqueue.LeaseLostError as e:
        log(f"❌ 提交被拒绝（LeaseLostError）：{e}")
    finally:
        hb.stop()


def agent_worker(cfg: dict, barrier=None, sem=None, crash_q=None) -> None:
    """一个 worker 进程的一生：循环"领取 → 处理 → 提交"，直到所有任务都结束。"""
    cfg = {**cfg, "sem": sem}
    log = make_logger(cfg["worker_id"], cfg["t0"], cfg["verbose"])
    q = jobqueue.JobQueue(cfg["db"])
    tickets = TicketSystem(Path(cfg["tickets_db"]))
    base_llm = None if cfg["offline"] else default_llm()
    if barrier is not None:
        barrier.wait()  # 所有 worker 都准备好了再一起开始，计时才公平
    log("上线")
    while True:
        job = q.claim(cfg["worker_id"], cfg["lease_s"])
        if job is None:
            if q.all_done():
                break
            time.sleep(cfg["poll_s"] * random.uniform(0.5, 1.5))  # 带抖动的轮询：别让所有 worker 同一时刻一起查
            continue
        handle_agent_job(job, q, tickets, base_llm, cfg, log, crash_q)
    log("队列空了，下线")


# ---------------------------------------------------------------- worker：会话写入（场景 4）


def session_worker(cfg: dict, barrier) -> None:
    """处理"用户在同一个会话里发来的消息"：读会话 → 调模型（sleep 模拟）→ 把这条消息写回会话。"""
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


# =====================================================================
# 调度器（父进程）
# =====================================================================


def spawn(ctx, target, *args) -> mp.Process:
    p = ctx.Process(target=target, args=args, daemon=True)
    p.start()
    return p


def worker_cfg(workdir: Path, worker_id: str, args, t0: float, *, verbose: bool, idempotent: bool = True) -> dict:
    return {
        "worker_id": worker_id, "db": str(workdir / "queue.db"), "tickets_db": str(workdir / "tickets.db"),
        "ckpt_dir": str(workdir / "checkpoints"), "offline": args.offline, "latency": args.latency,
        "lease_s": args.lease, "heartbeat_s": args.lease / 4, "freeze_s": args.lease * 2.5,
        "poll_s": 0.05 if args.offline else 0.2, "idempotent": idempotent, "verbose": verbose, "t0": t0,
    }


def enqueue_messages(workdir: Path, n: int, chaos: dict[int, str] | None = None):
    q = jobqueue.JobQueue(workdir / "queue.db")
    TicketSystem(workdir / "tickets.db")  # 先建好表，免得多个 worker 同时初始化
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


# ---------------------------------------------------------------- 场景 1：横向扩展


def run_batch(ctx, args, workers: int, limit: int | None, n_jobs: int) -> dict:
    workdir = fresh_dir(f"scaling_{workers}w_{limit or 'nolimit'}")
    q = enqueue_messages(workdir, n_jobs)
    sem = ctx.BoundedSemaphore(limit) if limit else None
    barrier = ctx.Barrier(workers + 1)
    procs = [
        spawn(ctx, agent_worker, worker_cfg(workdir, f"worker-{i + 1}", args, time.time(), verbose=False), barrier, sem)
        for i in range(workers)
    ]
    barrier.wait(timeout=120)  # 所有进程都启动好了（导入库、连上数据库、建好模型客户端）
    t0 = time.time()
    join_all(procs, timeout=600)
    jobs = q.list_jobs()
    done = [j for j in jobs if j.status == "succeeded"]
    elapsed = max((j.updated_at for j in done), default=t0) - t0
    per_worker: dict[str, int] = {}
    for j in done:
        per_worker[j.worker_id] = per_worker.get(j.worker_id, 0) + 1
    return {"elapsed": elapsed, "done": len(done), "total": len(jobs), "per_worker": per_worker}


def scenario_scaling(ctx, args) -> None:
    banner("场景 1：横向扩展 —— 加 worker 能快多少？瓶颈会跑到哪里去？")
    if args.offline:
        n_jobs, configs = 16, [(1, None), (2, None), (4, None), (8, None), (8, 3)]
        info(f"{n_jobs} 条报修 → {n_jobs} 个 Agent 任务。每个任务调用 2 次模型（建工单 + 回复），离线模式每次 sleep {args.latency}s。")
    else:
        n_jobs, configs = 3, [(1, 3), (3, 3)]
        info(f"真实模型：{n_jobs} 个 Agent 任务，每个调用 2 次模型。所有 worker 共享一个信号量，模型并发 ≤ 3。")
    info("Agent 的时间几乎全花在'等模型'上（I/O 密集），所以加 worker（加进程）能近似线性地提速 —— 直到撞上下一个瓶颈。")

    rows, base = [], None
    for workers, limit in configs:
        r = run_batch(ctx, args, workers, limit, n_jobs)
        throughput = r["done"] / r["elapsed"] if r["elapsed"] > 0 else 0.0
        base = base or r["elapsed"]
        spread = "/".join(str(v) for _, v in sorted(r["per_worker"].items()))
        rows.append([str(workers), f"≤{limit}" if limit else "不限", f"{r['done']}/{r['total']}",
                     f"{r['elapsed']:.2f}s", f"{throughput:.1f}", f"{base / r['elapsed']:.1f}x", spread])
    print()
    table(["worker 数", "模型并发", "完成", "耗时", "吞吐(个/秒)", "加速比", "各 worker 处理数"],
          rows, [11, 10, 8, 9, 13, 9, 18])
    if args.offline:
        takeaway("1→8 个 worker 吞吐接近线性增长；但给模型加上'最多 3 个并发'之后，8 个 worker 也只剩约 3 倍 ——\n"
                 "      瓶颈从'worker 不够'转移到了'模型配额不够'。这时再加 worker 只会让更多任务堆在信号量前排队。")
    else:
        takeaway("真实模型每次 3-5 秒、波动大，数字不必精确，看趋势：3 个 worker 明显快于 1 个。\n"
                 "      继续加 worker 的话，模型并发上限（这里是 3）就成了天花板 —— 离线模式的最后一行专门演示了这一点。")


# ---------------------------------------------------------------- 场景 2：worker 崩溃 + 幂等


def run_crash_case(ctx, args, idempotent: bool) -> dict:
    workdir = fresh_dir(f"crash_{'idem' if idempotent else 'noidem'}")
    n_jobs = 3
    q = enqueue_messages(workdir, n_jobs, chaos={1: "kill_after_side_effect"})
    t0 = time.time()
    log = make_logger("调度器", t0)
    sem = ctx.BoundedSemaphore(3)
    crash_q = ctx.Queue()
    procs = {
        wid: spawn(ctx, agent_worker, worker_cfg(workdir, wid, args, t0, verbose=True, idempotent=idempotent), None, sem, crash_q)
        for wid in ("worker-1", "worker-2")
    }
    try:
        victim = crash_q.get(timeout=30 if args.offline else 150)
        procs[victim].kill()
        procs[victim].join()
        log(f"💥 kill -9 {victim}：进程瞬间消失 —— 不释放租约、不写检查点、不留遗言")
        log(f"   任务 #1 的租约还剩最多 {args.lease:.0f} 秒，到期前没人能接手；拉起替补 worker-3（相当于 K8s 重建 Pod）")
        procs["worker-3"] = spawn(ctx, agent_worker,
                                  worker_cfg(workdir, "worker-3", args, t0, verbose=True, idempotent=idempotent), None, sem, crash_q)
    except queue.Empty:
        log("⚠️ 没等到崩溃信号（模型没有调用 create_ticket？），跳过 kill")
    join_all([p for p in procs.values() if p.is_alive()], timeout=600)
    job1 = q.get(1)
    return {
        "stats": q.stats(),
        "tickets": TicketSystem(workdir / "tickets.db").per_job(),
        "job1": job1,
        "n_jobs": n_jobs,
    }


def scenario_crash(ctx, args) -> None:
    banner("场景 2：worker 崩溃 —— 租约过期、别人接手，会不会重复建工单？")
    info("3 条报修，2 个 worker。处理任务 #1 的 worker 刚在工单系统里建完工单，就被 kill -9 了。")
    info(f"租约 {args.lease:.0f} 秒、每 {args.lease / 4:.1f} 秒心跳一次（生产中一般是 30 秒到几分钟，这里调短是为了演示快）。")
    info("接手的 worker 会从检查点恢复：检查点里有'模型决定调用 create_ticket'，但没有结果 → 这次调用会被重放。")

    results = {}
    for idempotent in (False, True):
        step(("第一轮：工具调用下游时【不带】幂等键" if not idempotent else "第二轮：工具调用下游时【带上】幂等键（run_id + tool_call_id）"))
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
             "      注意幂等键必须传给【下游】，由下游在同一个事务里'执行 + 记录 key'：\n"
             "      进程内存里的 IdempotencyStore 会随进程一起死掉，在分布式场景下毫无作用。")


# ---------------------------------------------------------------- 场景 3：僵尸 worker + fencing token


def scenario_zombie(ctx, args) -> None:
    banner("场景 3：僵尸 worker —— 卡住的 worker 醒来后，还能提交结果吗？")
    info(f"worker-1 跑完 Agent、正要提交时'卡住'了 {args.lease * 2.5:.0f} 秒（租约只有 {args.lease:.0f} 秒）。")
    info("真实世界里这叫 stop-the-world：GC 停顿、虚拟机被迁移、容器被 CPU 限流、网络分区……进程没死，但时间停了。")
    workdir = fresh_dir("zombie")
    q = enqueue_messages(workdir, 1, chaos={1: "freeze_before_complete"})
    t0 = time.time()
    first = spawn(ctx, agent_worker, worker_cfg(workdir, "worker-1", args, t0, verbose=True))
    while (job := q.get(1)) is not None and job.status == "queued" and first.is_alive():
        time.sleep(0.05)  # 等 worker-1 先领走任务，再启动 worker-2
    second = spawn(ctx, agent_worker, worker_cfg(workdir, "worker-2", args, t0, verbose=True))
    join_all([first, second], timeout=600)

    job = q.get(1)
    tickets = TicketSystem(workdir / "tickets.db").per_job()
    print()
    info(f"任务 #1 最终状态：{job.status}，由 {job.worker_id} 提交，fence={job.fence}，共被领取 {job.attempts} 次")
    info(f"保存的结果：{short(job.result, 40)}")
    info(f"工单数：{sum(len(v) for v in tickets.values())}（接手的 worker 从检查点发现 Agent 已经跑完，直接提交，没有重跑）")
    takeaway("worker-1 醒来时并不知道自己的租约早就过期了 —— 它'以为'自己还是主人。\n"
             "      挡住它的不是它自己的检查，而是存储端的 fencing token：fence=1 < 当前的 2，一律拒绝。\n"
             "      这就是为什么'分布式锁/租约必须配 fencing token'：持有者无法可靠地知道自己已经失去了锁。")


# ---------------------------------------------------------------- 场景 4：同一会话并发写


def pick_session_impl() -> str:
    """exercise.py 写完了就用你的实现；还没写完就用参考答案。"""
    probe = fresh_dir("probe") / "probe.db"
    try:
        ex = _load_sibling("exercise")
        store = session_store.SessionStore(probe)
        out = ex.update_session_with_retry(store, "probe", lambda d: {**d, "ok": True}, backoff_s=0)
        return "exercise" if out.data.get("ok") and store.get("probe").data.get("ok") else "solution"
    except Exception:  # noqa: BLE001 —— NotImplementedError 或者实现有 bug，都退回参考答案
        return "solution"


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
    impl = pick_session_impl()
    info(f"用户在同一个会话里连发 {n_msgs} 条消息，{n_workers} 个 worker 同时处理。每条消息：读会话 → 调模型（sleep {args.think}s）→ 写回。")
    info("本节不调用真实模型（用 sleep 模拟），离线和在线模式的行为一致。")
    info(f"B、C 使用的 update_session_with_retry 来自：{impl}.py"
         + ("（你的实现 👍）" if impl == "exercise" else "（exercise.py 还没写完，先用参考答案）"))

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


# =====================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="第 10 课 Demo：高并发与分布式执行")
    parser.add_argument("--offline", action="store_true", help="使用离线剧本（ScriptedLLM），不调用真实模型")
    parser.add_argument("--lease", type=float, default=None, help="租约秒数（默认：离线 2 秒，真实模型 3 秒）")
    parser.add_argument("--only", default="1,2,3,4", help="只运行指定场景，如 --only 2,3")
    args = parser.parse_args()
    args.lease = args.lease or (2.0 if args.offline else 3.0)
    args.latency = 0.15  # 离线模式下每次"模型调用"的耗时
    args.think = 0.05  # 场景 4 里每次"模型调用"的耗时

    if args.offline:
        print("🔌 离线模式：ScriptedLLM + sleep 模拟模型延迟")
    else:
        try:
            model = default_llm().model
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"🌐 真实模型：{model}（所有 worker 共享一个信号量，模型并发 ≤ 3；场景 4 不调用模型）")
    print(f"   worker 是真正的独立进程（spawn），共享的只有 {RUNS.relative_to(HERE.parents[1])}/ 下的 SQLite 文件。")

    ctx = mp.get_context("spawn")  # macOS / Windows 默认就是 spawn；Linux 上显式指定，行为一致
    started = time.time()
    scenarios = {"1": scenario_scaling, "2": scenario_crash, "3": scenario_zombie, "4": scenario_session}
    for key in args.only.replace(" ", "").split(","):
        scenarios[key](ctx, args)

    banner("小结")
    info("1. 横向扩展：worker 无状态 + 状态放共享存储，加进程就能提速，直到撞上模型配额 → 要限流、要排队。")
    info("2. 投递语义：租约让任务'不丢'（at-least-once），幂等键让'重复执行'无害 → 效果上恰好一次。")
    info("3. fencing token：租约过期后的旧持有者（僵尸）必须被存储端拒绝，只靠它自觉是不可靠的。")
    info("4. 会话并发写：不控制就会静悄悄丢数据；CAS 能兜底，按会话串行是对话类 Agent 的首选。")
    info(f"总耗时 {time.time() - started:.0f} 秒。运行产物在 {RUNS.relative_to(HERE.parents[1])}/")


if __name__ == "__main__":
    main()
