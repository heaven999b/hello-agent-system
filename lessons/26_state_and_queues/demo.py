"""第 26 课 Demo：同一个 Agent，跑在真实的 Postgres + Redis 上 —— 多进程 worker、故障注入、审批、同步 vs 异步。

    python lessons/26_state_and_queues/demo.py --offline        # 离线：ScriptedLLM，约 50 秒
    python lessons/26_state_and_queues/demo.py                  # 真实模型：7 个任务（模型并发 ≤ 2），第 3 部分仍用模拟模型
    python lessons/26_state_and_queues/demo.py --offline --only 3

基础设施全部在本机拉起，不需要 Docker：
    - 嵌入式 Postgres（pip 包 pgserver，真正的 Postgres 16 进程，unix socket，多进程共享）
    - fakeredis TCP 服务（Redis 协议 + Lua；不模拟持久化、主从切换、集群分片）
缺少可选依赖时打印安装命令并以退出码 0 结束。

四个部分：
  1. 3 个 worker 进程 × 3 个租户 × 30 个任务：按租户限流（Redis Lua 令牌桶）、建工单（下游唯一约束做幂等），
     中途 kill -9 一个 worker、冻结（SIGSTOP）两个 worker 制造"僵尸"，最后统计：没有重复工单、fence 拒绝了
     僵尸的提交、检查点冲突被检测到；然后用 SIGTERM 优雅停机。
  2. 审批：收件箱里查出等待审批的 run → 批准 → 入队 resume（重复点击只入队一次）→ 一个全新的 worker 进程恢复执行。
  3. 同一批任务：同步 worker（1 个 / 3 个进程）vs 单进程异步 worker（并发 16），以及连接池大小的影响。
  4. 异步 worker 优雅停机：宽限期后被取消的写操作保持"未回答"，别的 worker resume 时用同一个 call_id 重放，
     幂等键不变，下游唯一约束去重 —— 工单不会建两张。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import importlib.util
import json
import multiprocessing as mp
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from agentkit import ToolContext, tool  # 工具的类型注解要能在模块全局里解析到（from __future__ import annotations）

HERE = Path(__file__).resolve().parent
INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'
REQUIRED = ("psycopg", "psycopg_pool", "redis", "pgserver", "fakeredis", "lupa")

TENANTS = {  # 租户 → (套餐, 每秒模型调用数, 桶容量)
    "acme": ("标准", 4.0, 4),
    "globex": ("标准", 4.0, 4),
    "initech": ("免费", 1.0, 2),
}

MESSAGES = {
    "ticket": [
        "3 楼东区的打印机一直卡纸",
        "会议室 B 的投影仪没有信号",
        "工位网口插上网线没反应",
        "显示器上有一条竖线越来越明显",
        "笔记本电池鼓包了",
    ],
    "question": [
        "VPN 证书过期了怎么更新？",
        "怎么申请 Git 仓库权限？",
        "邮箱空间满了如何清理？",
        "如何连接 5 楼的访客 Wi-Fi？",
    ],
    "password": ["帮我重置一下 Jira 的密码，账号 zhang.san"],
}

SYSTEM_PROMPT = (
    "你是公司的 IT 服务台助手。每条用户消息只调用一次工具：\n"
    "1) 设备或系统故障 → 调用 create_ticket 建工单，然后用一句话告诉用户工单号；\n"
    "2) '怎么/如何'之类的问题 → 调用 search_kb，然后根据结果用一两句话回答；\n"
    "3) 要求重置密码 → 调用 reset_password。\n不要追问，回答用简短的中文。"
)

KB = {
    "VPN": "VPN 证书在自助门户 → 证书管理里一键续期，续期后重启客户端。",
    "Git": "在权限平台提交 Git 仓库权限申请，直属主管审批后自动开通。",
    "邮箱": "邮箱设置 → 存储管理里清理 30 天前的大附件，或申请扩容到 50GB。",
    "Wi-Fi": "访客 Wi-Fi 名为 Guest-5F，密码每天在前台屏幕上更新。",
}


# =====================================================================================
# 打印小工具
# =====================================================================================


def banner(title: str) -> None:
    print("\n" + "═" * 78 + f"\n  {title}\n" + "═" * 78, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, w: int) -> str:
    return text + " " * max(0, w - width(text))


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    info("".join(pad(h, w) for h, w in zip(headers, widths)))
    info("─" * sum(widths))
    for row in rows:
        info("".join(pad(str(c), w) for c, w in zip(row, widths)))


def short(text, n: int = 36) -> str:
    text = " ".join(str(text or "").split())
    return text if width(text) <= n else text[: n - 1] + "…"


# =====================================================================================
# 共享：跨进程的日志通道（Redis 列表）
# =====================================================================================
# 为什么不用 multiprocessing.Queue？它的写锁是跨进程共享的：一个 worker 恰好在写队列时被 SIGSTOP，
# 其他所有 worker 的 put() 都会卡住。Redis 列表没有这种共享锁，被冻结 / 被杀的进程不会连累别人。

EVENTS, CHAOS = "demo:events", "demo:chaos"


def post(r, key: str, **fields) -> None:
    fields.setdefault("ts", time.time())
    r.rpush(key, json.dumps(fields, ensure_ascii=False, default=str))


def drain(r, key: str) -> list[dict]:
    out = []
    while (raw := r.lpop(key)) is not None:
        out.append(json.loads(raw))
    return out


# =====================================================================================
# 下游系统：工单库（另一个团队的服务；唯一约束 = 幂等）
# =====================================================================================

TICKETS_DDL = """CREATE TABLE IF NOT EXISTS tickets (
    id              serial PRIMARY KEY,
    idempotency_key text UNIQUE,            -- 同一个 key 只能建一张工单：由数据库保证，不靠调用方自觉
    tenant_id       text,
    run_id          text,
    title           text,
    created_by      text,
    created_at      timestamptz DEFAULT now())"""


def create_ticket_row(dsn: str, key: str, tenant: str | None, run_id: str, title: str, worker: str) -> tuple[str, bool]:
    """INSERT ... ON CONFLICT DO NOTHING：执行副作用和记下幂等键是同一条语句，中间没有缝。"""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        row = c.execute(
            "INSERT INTO tickets (idempotency_key, tenant_id, run_id, title, created_by) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (key, tenant, run_id, title, worker),
        ).fetchone()
        if row:
            return f"T-{1000 + row[0]}", True
        existing = c.execute("SELECT id FROM tickets WHERE idempotency_key = %s", (key,)).fetchone()
        return f"T-{1000 + existing[0]}", False


# =====================================================================================
# 模型
# =====================================================================================


class LimitedLLM:
    """真实模式：所有 worker 进程共享一个信号量，同一时刻最多 2 个模型请求在途（多个 agent 共用一个网关）。"""

    def __init__(self, inner, sem):
        self.inner, self.sem, self.model = inner, sem, inner.model

    def chat(self, messages, tools=None, **kwargs):
        with self.sem:
            return self.inner.chat(messages, tools, **kwargs)


def scripted_reply(messages):
    """离线剧本：按对话内容决定"调用哪个工具"或"怎么回答"。"""
    from agentkit import call_tool, reply

    last = messages[-1]
    if last["role"] == "tool":
        content = last.get("content") or ""
        if m := re.search(r"T-\d+", content):
            return reply(f"已为你创建工单 {m.group(0)}，IT 同事会尽快联系你。")
        if "重置" in content:
            return reply("密码已重置，新密码已发到你的企业邮箱。")
        if content.startswith(("拒绝", "未执行", "错误")):
            return reply("这个操作没有完成，请稍后再试或联系 IT 同事。")
        return reply(f"根据知识库：{content}")
    text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if "密码" in text:
        return call_tool("reset_password", user="zhang.san")
    if "怎么" in text or "如何" in text:
        return call_tool("search_kb", query=text[:12])
    return call_tool("create_ticket", title=text[:20], priority="medium")


def offline_llm(latency: float):
    from agentkit import ScriptedLLM

    def policy(messages):
        time.sleep(latency)  # 模拟模型推理耗时
        return scripted_reply(messages)

    return ScriptedLLM([policy] * 12)


# =====================================================================================
# 第 1、2 部分：同步 worker 进程
# =====================================================================================


def build_tools(cfg: dict, job, r):
    wid = cfg["worker_id"]

    @tool
    def search_kb(query: Annotated[str, Field(description="要查的问题关键词")]) -> str:
        """在 IT 知识库里查找操作指引。"""
        for k, v in KB.items():
            if k.lower() in query.lower():
                return v
        return "知识库里没有找到相关条目，建议提交工单。"

    @tool(risk="write", timeout_s=120)
    def create_ticket(
        title: Annotated[str, Field(description="一句话概括问题，不超过 30 字")],
        priority: Annotated[Literal["low", "medium", "high"], Field(description="影响个人 medium，影响多人 high")],
        ctx: ToolContext,
    ) -> str:
        """为设备或系统故障创建 IT 工单，返回工单号。每条报修只调用一次。"""
        # 幂等键 = run_id + tool_call_id：run_id 由任务决定（job-7），tool_call_id 在检查点里，
        # 所以无论哪个 worker、第几次尝试，重放这次调用时 key 都一样
        no, created = create_ticket_row(cfg["dsn"], ctx.idempotency_key, ctx.tenant_id, ctx.run_id, title, wid)
        if created:
            post(r, EVENTS, who=wid, kind="ticket", job=job.id, msg=f"🧾 建工单 {no}（幂等键 {ctx.idempotency_key}）")
        else:
            post(r, EVENTS, who=wid, kind="dedup", job=job.id, msg=f"♻️  下游唯一约束命中：返回已有工单 {no}，没有重复创建")
        chaos = job.payload.get("chaos")
        if chaos in ("kill_after_ticket", "freeze_mid_run") and job.attempts == 1:
            what = "kill" if chaos == "kill_after_ticket" else "freeze"
            post(r, EVENTS, who=wid, kind="info", job=job.id, msg="工单建好了，但结果还没写进检查点……")
            post(r, CHAOS, action=what, pid=os.getpid(), job=job.id, fence=job.fence, who=wid)
            time.sleep(30 if what == "kill" else 1.0)  # 等调度器下手（被冻结时这段 sleep 也一起冻结）
            if what == "freeze":
                post(r, EVENTS, who=wid, kind="info", job=job.id, msg="醒了！工具返回，Agent 继续往检查点里写……")
        return f"已创建工单 {no}"

    @tool(risk="dangerous")
    def reset_password(user: Annotated[str, Field(description="要重置密码的账号")]) -> str:
        """重置员工账号密码（高风险：需要人工审批）。"""
        return f"{user} 的密码已重置，新密码已发送到企业邮箱。"

    return [search_kb, create_ticket, reset_password]


def sync_worker_main(cfg: dict, sem=None) -> None:
    """一个 worker 进程：PostgresJobQueue + PostgresCheckpointer + Redis（幂等缓存、限流）+ 同步 Agent。"""
    import redis

    from agentkit import Agent, PermissionPolicy, default_llm
    from agentkit.contrib.postgres import AgentJobHandler, PostgresCheckpointer, PostgresJobQueue, run_worker, stop_on_signals
    from agentkit.contrib.redis_store import RateLimitHook, RedisIdempotencyStore, RedisTokenBucket
    from agentkit.hooks import StopRun

    wid = cfg["worker_id"]
    stop = threading.Event()
    stop_on_signals(stop)  # SIGTERM → 不再领取新任务，手头的做完再退出（K8s 滚动发布时就是这样）
    r = redis.Redis.from_url(cfg["redis"])
    queue = PostgresJobQueue(cfg["dsn"], base_backoff=0.2, max_backoff=2)
    ckpt = PostgresCheckpointer(cfg["dsn"])
    idem = RedisIdempotencyStore(r, ttl_seconds=3600)
    bucket = RedisTokenBucket(r, 4.0, 4, overrides={t: (rate, cap) for t, (_, rate, cap) in TENANTS.items()})

    class MeteredRateLimit(RateLimitHook):
        """把每个租户的限流数据记进 Redis：进程被 kill 了数据也还在，调度器最后统一汇总。"""

        def before_llm(self, state, messages):
            key = state.metadata.get("tenant_id") or "anonymous"
            t0 = time.monotonic()
            try:
                super().before_llm(state, messages)
                r.hincrby(f"demo:rl:{key}", "calls", 1)
            except StopRun:
                r.hincrby(f"demo:rl:{key}", "rejected", 1)
                raise
            finally:
                r.hincrbyfloat(f"demo:rl:{key}", "waited_s", time.monotonic() - t0)

    limiter = MeteredRateLimit(bucket, wait_timeout=cfg["rl_wait"])
    real_llm = None if cfg["offline"] else LimitedLLM(default_llm(), sem)

    def make_agent(checkpointer, job):
        llm = offline_llm(cfg["latency"]) if cfg["offline"] else real_llm
        return Agent(llm, build_tools(cfg, job, r), system_prompt=SYSTEM_PROMPT, name="helpdesk", max_steps=8,
                     checkpointer=checkpointer, hooks=[PermissionPolicy(), limiter], idempotency_store=idem)

    handler = AgentJobHandler(make_agent, ckpt, defer_seconds=1.0)

    def handle(job):
        if job.attempts > 1 and job.payload.get("op") == "run":
            prev = ckpt.get_run(f"job-{job.id}")
            if prev is not None:
                post(r, EVENTS, who=wid, kind="takeover", job=job.id,
                     msg=f"接手任务 #{job.id}（第 {job.attempts} 次领取，fence={job.fence}）：发现前任 {prev['writer']} "
                         f"的检查点（status={prev['status']}，第 {prev['state']['step']} 步）→ 从断点继续")
        result = handler(job)
        if job.payload.get("chaos") == "freeze_before_complete" and job.attempts == 1:
            post(r, EVENTS, who=wid, kind="info", job=job.id, msg="😵 Agent 跑完了，提交结果之前进程被冻结（模拟 GC 停顿）")
            post(r, CHAOS, action="freeze", pid=os.getpid(), job=job.id, fence=job.fence, who=wid)
            time.sleep(1.0)
            post(r, EVENTS, who=wid, kind="info", job=job.id, msg="醒了！以为自己还持有租约，继续提交结果……")
        return result

    def on_event(name: str, data: dict) -> None:
        job = data.get("job")
        base = {"who": wid, "kind": name, "job": getattr(job, "id", None), "tenant": getattr(job, "tenant_id", None)}
        if name == "claimed":
            post(r, EVENTS, **base, attempts=job.attempts, fence=job.fence, op=job.payload.get("op"))
        elif name == "completed":
            res = data.get("result") or {}
            post(r, EVENTS, **base, status=res.get("status"), output=res.get("output"), fence=job.fence)
        elif name in ("fence_rejected", "ownership_lost", "heartbeat_rejected", "deferred", "failed"):
            post(r, EVENTS, **base, error=data.get("error") or data.get("reason"))
        elif name == "stopped":
            post(r, EVENTS, **base, stats=data.get("stats"))

    post(r, EVENTS, who=wid, kind="started", pid=os.getpid())
    run_worker(queue, handle, worker_id=wid, stop_event=stop, lease_seconds=cfg["lease"],
               heartbeat_interval=cfg["lease"] / 4, poll_interval=0.1, on_event=on_event)
    queue.close()
    ckpt.close()


class Printer:
    """调度器里的日志线程：把 worker 发来的事件按时间打印出来。"""

    def __init__(self, r, t0: float, verbose: bool):
        self.r, self.t0, self.verbose = r, t0, verbose
        self.seen: list[dict] = []
        self._halt = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def log(self, who: str, msg: str, ts: float | None = None) -> None:
        print(f"   [+{(ts or time.time()) - self.t0:5.1f}s] {pad(who, 9)}│ {msg}", flush=True)

    def _run(self) -> None:
        while not self._halt.is_set():
            for e in drain(self.r, EVENTS):
                self.seen.append(e)
                self._show(e)
            self._halt.wait(0.05)

    def _show(self, e: dict) -> None:
        kind, who, ts = e["kind"], e["who"], e["ts"]
        if "msg" in e:
            self.log(who, e["msg"], ts)
        elif kind == "fence_rejected":
            self.log(who, f"❌ 提交被拒绝（LeaseLost）：{short(e['error'], 60)}", ts)
        elif kind == "heartbeat_rejected":
            self.log(who, f"💔 心跳被拒绝：任务 #{e['job']} 的 fence 已经过期，租约早就不是我的了", ts)
        elif kind == "ownership_lost":
            self.log(who, f"❌ 检查点冲突（CheckpointConflict）：{short(e['error'], 58)}", ts)
        elif kind == "completed" and (self.verbose or e.get("status") == "paused"):
            tag = "⏸  等待审批" if e.get("status") == "paused" else "✅"
            self.log(who, f"{tag} 完成 #{e['job']}（{e['tenant']}）：{short(e.get('output'), 40)}", ts)
        elif self.verbose and kind == "claimed":
            self.log(who, f"领取任务 #{e['job']}（op={e.get('op')}，第 {e.get('attempts')} 次尝试，fence={e.get('fence')}）", ts)
        elif self.verbose and kind in ("deferred", "failed"):
            self.log(who, f"{kind} #{e['job']} {short(e.get('error') or '', 50)}", ts)

    def start(self):
        self.thread.start()
        return self

    def stop(self) -> None:
        time.sleep(0.2)
        self._halt.set()
        self.thread.join(2)
        for e in drain(self.r, EVENTS):
            self.seen.append(e)
            self._show(e)


def spawn(ctx, target, *args) -> mp.Process:
    p = ctx.Process(target=target, args=args, daemon=True)
    p.start()
    return p


def worker_cfg(args, dsn: str, redis_url: str, wid: str) -> dict:
    return {"worker_id": wid, "dsn": dsn, "redis": redis_url, "offline": args.offline, "latency": 0.15,
            "lease": 2.0 if args.offline else 3.0, "rl_wait": 1.0}


def enqueue_batch(queue, offline: bool) -> dict[int, dict]:
    """入队：3 个租户，每个租户 10 个（真实模式 2 个）任务；再加一个需要审批的重置密码。返回 {job_id: 描述}。"""
    per_tenant = 10 if offline else 2
    chaos_plan = {("acme", 1): "kill_after_ticket", ("globex", 1): "freeze_mid_run", ("initech", 1): "freeze_before_complete"}
    jobs: dict[int, dict] = {}
    order = []
    for i in range(per_tenant):
        for tenant in TENANTS:
            order.append((tenant, i))
    for tenant, i in order:
        kind = "ticket" if i % 2 == 0 else "question"
        pool = MESSAGES[kind]
        text = pool[(i // 2) % len(pool)]
        payload = {"op": "run", "input": text, "metadata": {"user_id": f"{tenant}-u{i}", "roles": ["employee"]}}
        if (tenant, i) in chaos_plan:
            payload["chaos"] = chaos_plan[(tenant, i)]
            payload["input"] = MESSAGES["ticket"][i % len(MESSAGES["ticket"])]
        jid = queue.enqueue("agent", payload, tenant_id=tenant, idempotency_key=f"msg-{tenant}-{i}")
        jobs[jid] = {"tenant": tenant, "text": payload["input"], "chaos": payload.get("chaos")}
        if (tenant, i) == ("acme", 0):  # 需要审批的那个
            payload = {"op": "run", "input": MESSAGES["password"][0], "metadata": {"user_id": "acme-zhang", "roles": ["employee"]}}
            jid = queue.enqueue("agent", payload, tenant_id="acme", idempotency_key="msg-acme-pw")
            jobs[jid] = {"tenant": "acme", "text": payload["input"], "chaos": None}
    return jobs


class Chaos:
    """调度器里的"混沌工程"线程：收到 worker 的信号后 kill -9 或者 SIGSTOP 它。"""

    def __init__(self, r, queue, printer: Printer, respawn, lease_s: float):
        self.r, self.queue, self.printer, self.respawn, self.lease_s = r, queue, printer, respawn, lease_s
        self.pending = 0
        self.log: list[str] = []
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._halt.is_set():
            for c in drain(self.r, CHAOS):
                time.sleep(0.1)  # 让 worker 自己的日志先打印出来（worker 此刻正在 sleep，等我们下手）
                if c["action"] == "kill":
                    os.kill(c["pid"], signal.SIGKILL)
                    self.printer.log("调度器", f"💥 kill -9 {c['who']}（pid {c['pid']}）：不释放租约、不写检查点、不留遗言")
                    self.log.append(f"kill -9 {c['who']}（任务 #{c['job']}）")
                    self.respawn()
                else:
                    os.kill(c["pid"], signal.SIGSTOP)
                    self.printer.log("调度器", f"🧊 SIGSTOP {c['who']}：整个进程被冻结（心跳线程也停了），租约 {self.lease_s:g} 秒后过期")
                    self.log.append(f"SIGSTOP {c['who']}（任务 #{c['job']}）")
                    with self._lock:
                        self.pending += 1
                    threading.Thread(target=self._thaw_later, args=(c,), daemon=True).start()
            self._halt.wait(0.02)

    def _thaw_later(self, c: dict) -> None:
        deadline = time.time() + 60
        while time.time() < deadline:  # 等别的 worker 接手并做完，再让僵尸醒来
            job = self.queue.get(c["job"])
            if job.fence > c["fence"] and job.status in ("succeeded", "failed", "dead"):
                break
            time.sleep(0.1)
        os.kill(c["pid"], signal.SIGCONT)
        self.printer.log("调度器", f"▶️  SIGCONT {c['who']}：任务 #{c['job']} 早已被别人完成，僵尸醒来")
        with self._lock:
            self.pending -= 1

    def start(self):
        self.thread.start()
        return self

    def stop(self) -> None:
        self._halt.set()
        self.thread.join(2)


def part1(args, dsn: str, redis_url: str, r, ctx) -> dict:
    import psycopg

    from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue

    banner("第 1 部分：3 个 worker 进程 × 3 个租户 —— 限流、幂等、kill -9、僵尸、优雅停机")
    queue = PostgresJobQueue(dsn)
    ckpt = PostgresCheckpointer(dsn)
    queue.setup()
    ckpt.setup()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(TICKETS_DDL)
    jobs = enqueue_batch(queue, args.offline)
    n = len(jobs)
    step(f"入队 {n} 个任务（{len(TENANTS)} 个租户；幂等键 msg-<租户>-<序号>，重复提交只入队一次）")
    dup = queue.enqueue("agent", {"op": "run", "input": "重复提交"}, tenant_id="acme", idempotency_key="msg-acme-0")
    info(f"再用同一个幂等键 msg-acme-0 提交一次 → 返回已有的 job #{dup}，队列里仍然是 {queue.stats()['counts']['queued']} 个")
    for t, (plan, rate, cap) in TENANTS.items():
        info(f"租户 {pad(t, 8)} {plan}套餐：模型调用 {rate:g} 次/秒，突发 {cap} 次（Redis 令牌桶，所有 worker 共享）")
    chaos_jobs = {jid: d["chaos"] for jid, d in jobs.items() if d["chaos"]}
    labels = {"kill_after_ticket": "建完工单后 kill -9", "freeze_mid_run": "建完工单后冻结（跑到一半的僵尸）",
              "freeze_before_complete": "跑完后、提交前冻结（提交阶段的僵尸）"}
    for jid, c in chaos_jobs.items():
        info(f"故障注入：任务 #{jid}（{jobs[jid]['tenant']}）—— {labels[c]}")

    step("启动 3 个 worker 进程（spawn 方式：彼此不共享内存，只通过 Postgres 和 Redis 协作）")
    t0 = time.time()
    printer = Printer(r, t0, args.verbose).start()
    sem = ctx.Semaphore(2)
    procs: dict[str, mp.Process] = {}
    counter = {"n": 3}

    def respawn():
        counter["n"] += 1
        wid = f"worker-{counter['n']}"
        procs[wid] = spawn(ctx, sync_worker_main, worker_cfg(args, dsn, redis_url, wid), sem)
        printer.log("调度器", f"🔁 启动替补 {wid}（相当于 K8s 发现 Pod 挂了，拉起一个新的）")

    chaos = Chaos(r, queue, printer, respawn, worker_cfg(args, dsn, redis_url, "x")["lease"]).start()
    for i in (1, 2, 3):
        wid = f"worker-{i}"
        procs[wid] = spawn(ctx, sync_worker_main, worker_cfg(args, dsn, redis_url, wid), sem)

    last_report = time.time()
    deadline = time.time() + (240 if args.offline else 420)
    while time.time() < deadline:
        s = queue.stats()["counts"]
        done = s["succeeded"] + s["failed"] + s["dead"]
        if done >= n and chaos.pending == 0:
            break
        if time.time() - last_report > 3:
            printer.log("调度器", f"进度：完成 {done}/{n}，排队 {s['queued']}，执行中 {s['leased']}")
            last_report = time.time()
        time.sleep(0.1)
    elapsed = time.time() - t0
    time.sleep(1.5)  # 让刚醒来的僵尸把它的遭遇（被拒绝的提交 / 检查点冲突）打印完

    step("所有任务结束 → 给每个 worker 发 SIGTERM（优雅停机：不再领取，手头的做完再退出）")
    for wid, p in procs.items():
        if p.is_alive():
            os.kill(p.pid, signal.SIGTERM)
    for p in procs.values():
        p.join(10)
    chaos.stop()
    printer.stop()
    info("退出码：" + "，".join(f"{wid}={p.exitcode}" for wid, p in procs.items())
         + "（-9 = 被 kill -9；0 = 收到 SIGTERM 后正常退出）")

    report_part1(queue, ckpt, dsn, r, printer.seen, jobs, chaos.log, elapsed)
    queue.close()
    ckpt.close()
    return jobs


def report_part1(queue, ckpt, dsn, r, events, jobs, chaos_log, elapsed) -> None:
    import psycopg

    step(f"📊 结果（{elapsed:.1f} 秒）")
    s = queue.stats()["counts"]
    with psycopg.connect(dsn) as c:
        tickets, keys = c.execute("SELECT count(*), count(DISTINCT idempotency_key) FROM tickets").fetchone()
        multi = c.execute("SELECT id, attempts, fence FROM agent_jobs WHERE attempts > 1 ORDER BY id").fetchall()
        per_tenant_done = dict(c.execute(
            "SELECT tenant_id, EXTRACT(EPOCH FROM max(finished_at) - min(created_at))::float FROM agent_jobs GROUP BY tenant_id"
        ).fetchall())
    kinds = [e["kind"] for e in events]
    dedups = kinds.count("dedup")
    info(f"任务：{s['succeeded']}/{len(jobs)} 成功，failed {s['failed']}，dead {s['dead']}；"
         f"因崩溃 / 卡死被重新领取的：{', '.join(f'#{i}（第 {a} 次尝试完成，fence={f}）' for i, a, f in multi) or '无'}；"
         f"因限流被推迟 {kinds.count('deferred')} 次（不计入尝试次数）")
    ok = "✅" if tickets == keys else "❌"
    info(f"工单：{tickets} 张，幂等键 {keys} 个 → 重复 {tickets - keys} 张 {ok}；下游唯一约束挡下了 {dedups} 次重放")
    info(f"fence：拒绝了僵尸 worker 的 {kinds.count('fence_rejected')} 次提交、{kinds.count('heartbeat_rejected')} 次心跳 ✅")
    info(f"检查点：检测到 {kinds.count('ownership_lost')} 次冲突（僵尸在被接管之后还想写检查点）✅")
    info(f"故障注入：{'；'.join(chaos_log) or '无'}")
    step("按租户看限流效果（数据来自 Redis：限流 Hook 在每个进程里记账，进程被 kill 了数据也还在）")
    rows = []
    for t, (plan, rate, cap) in TENANTS.items():
        h = {k.decode(): float(v) for k, v in r.hgetall(f"demo:rl:{t}").items()}
        rows.append([t, f"{plan}（{rate:g}/s）", f"{int(h.get('calls', 0))}", f"{int(h.get('rejected', 0))}",
                     f"{h.get('waited_s', 0):.1f}s", f"{per_tenant_done.get(t, 0):.1f}s"])
    table(["租户", "套餐", "模型调用", "等不到→推迟", "等令牌总时长", "全部完成用时"], rows, [10, 16, 10, 14, 14, 14])
    info("推迟 = RateLimitHook 等了 1 秒还拿不到令牌 → StopRun(rate_limited) → AgentJobHandler 抛 RetryLater →")
    info("任务回到队列（不消耗重试次数），worker 去做别的租户的任务，而不是在这里干等。")


def part2(args, dsn: str, redis_url: str, r, ctx) -> None:
    from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue

    banner("第 2 部分：审批 → 入队 resume → 一个全新的 worker 进程恢复执行")
    queue, ckpt = PostgresJobQueue(dsn), PostgresCheckpointer(dsn)
    inbox = ckpt.list_runs(status="paused")
    step("审批收件箱：ckpt.list_runs(status='paused')（按 (status, updated_at) 索引查询）")
    if not inbox:
        info("收件箱是空的：这次运行里模型没有发起需要审批的操作（真实模型不一定每次都调用 reset_password），跳过。")
        return
    for row in inbox:
        p = row["pending"]
        info(f"run {row['run_id']}（租户 {row['tenant_id']}，用户 {row['user_id']}）等待审批：{p['name']}({p['arguments']})，"
             f"最后写入者 {row['writer']}")
    row = inbox[0]
    run_id, call_id = row["run_id"], row["pending"]["id"]
    step("审批人 alice 点了“批准”——手抖点了两次；API 用 approve:<run_id>:<call_id> 作为幂等键入队 resume 任务")
    payload = {"op": "resume", "run_id": run_id, "approvals": {call_id: True}, "by": "alice", "comment": "已电话核实本人"}
    a = queue.enqueue("agent", payload, tenant_id=row["tenant_id"], idempotency_key=f"approve:{run_id}:{call_id}")
    b = queue.enqueue("agent", payload, tenant_id=row["tenant_id"], idempotency_key=f"approve:{run_id}:{call_id}")
    info(f"两次入队返回的 job_id：#{a}、#{b} → {'同一个任务 ✅' if a == b else '❌ 重复了'}")
    step("启动一个之前从没出现过的 worker-9 进程来处理它（状态全在 Postgres 里，任何进程都能接着跑）")
    t0 = time.time()
    printer = Printer(r, t0, verbose=True).start()
    sem = ctx.Semaphore(2)  # 要在父进程里保留引用：子进程启动时才去"打开"它，父进程先回收了子进程就找不到了
    p = spawn(ctx, sync_worker_main, worker_cfg(args, dsn, redis_url, "worker-9"), sem)
    deadline = time.time() + 120
    while time.time() < deadline and p.is_alive() and queue.get(a).status not in ("succeeded", "failed", "dead"):
        time.sleep(0.1)
    if p.is_alive():
        os.kill(p.pid, signal.SIGTERM)
    p.join(10)
    printer.stop()
    job = queue.get(a)
    final = ckpt.get_run(run_id)
    info(f"resume 任务 #{a}：{job.status}；run {run_id} 现在是 {final['status']}，最后写入者 {final['writer']}")
    info(f"回复：{short(final['output'], 60)}")
    for entry in final["state"]["approval_log"]:
        at = time.strftime("%H:%M:%S", time.localtime(entry["at"]))
        info(f"审批记录：{entry['by']} 于 {at} {'批准' if entry['approved'] else '拒绝'} {entry['tool']}（{entry['comment']}）")
    queue.close()
    ckpt.close()


# =====================================================================================
# 第 3 部分：同步 worker vs 异步 worker
# =====================================================================================

BENCH_LATENCY = 0.15  # 每次模型调用的模拟耗时（秒）


def bench_responder(messages):
    from agentkit import call_tool, reply

    return reply("查到了：请按指引操作。") if messages[-1]["role"] == "tool" else call_tool("lookup", q="VPN")


def bench_tools():
    @tool
    def lookup(q: str) -> str:
        """查知识库"""
        return KB["VPN"]

    return [lookup]


def bench_sync_main(cfg: dict) -> None:
    from agentkit import Agent, ScriptedLLM
    from agentkit.contrib.postgres import AgentJobHandler, PostgresCheckpointer, PostgresJobQueue, run_worker

    def slow(messages):
        time.sleep(BENCH_LATENCY)  # 同步调用模型：这 0.15 秒里整个进程什么也干不了
        return bench_responder(messages)

    queue, ckpt = PostgresJobQueue(cfg["dsn"]), PostgresCheckpointer(cfg["dsn"])
    handler = AgentJobHandler(lambda c: Agent(ScriptedLLM([slow] * 4), bench_tools(), checkpointer=c), ckpt)
    stop = threading.Event()
    stop_on = threading.Thread(target=lambda: (_wait_all_done(queue, cfg["n"]), stop.set()), daemon=True)
    stop_on.start()
    run_worker(queue, handler, worker_id=cfg["worker_id"], stop_event=stop, poll_interval=0.02, lease_seconds=30)


def _wait_all_done(queue, n: int) -> None:
    while queue.stats()["counts"]["succeeded"] < n:
        time.sleep(0.02)


def bench_async_main(cfg: dict, out) -> None:
    out.put(asyncio.run(_bench_async(cfg)))


async def _bench_async(cfg: dict) -> dict:
    from psycopg_pool import AsyncConnectionPool

    from agentkit.aio import AsyncAgent, AsyncScriptedLLM
    from agentkit.contrib.postgres import AgentJobHandler, AsyncPostgresCheckpointer, AsyncPostgresJobQueue, run_async_worker

    async with contextlib.AsyncExitStack() as stack:
        pool = await stack.enter_async_context(
            AsyncConnectionPool(cfg["dsn"], min_size=2, max_size=cfg["pool"], kwargs={"autocommit": True}))
        business_db = None
        if cfg["hold"]:  # 另一个库（比如业务库）的连接池，4 个连接
            business_db = await stack.enter_async_context(
                AsyncConnectionPool(cfg["dsn"], min_size=4, max_size=4, kwargs={"autocommit": True}))
        queue, ckpt = AsyncPostgresJobQueue(pool), AsyncPostgresCheckpointer(pool)  # 队列和检查点共用一个池
        llm = AsyncScriptedLLM(responder=bench_responder, latency=BENCH_LATENCY)
        tools = bench_tools()
        # 整个进程共用一个 AsyncAgent（一个工具线程池）；每个任务的 fenced 检查点视图由 handler 通过 checkpointer= 传入
        handler = AgentJobHandler(AsyncAgent(llm, tools, checkpointer=ckpt), ckpt)

        async def hold_connection_while_waiting_for_the_model(job):
            async with business_db.connection() as conn:  # ❌ 反模式：开着一个业务库事务 / 连接去等模型
                await conn.execute("SELECT 1")
                return await handler(job)

        h = hold_connection_while_waiting_for_the_model if cfg["hold"] else handler
        t0 = time.perf_counter()
        stats = await run_async_worker(queue, h, worker_id="async-1", stop_event=asyncio.Event(),
                                       concurrency=cfg["concurrency"], max_jobs=cfg["n"], poll_interval=0.02)
        return {"elapsed": time.perf_counter() - t0, "llm_peak": llm.max_in_flight, "succeeded": stats["succeeded"]}


class ConnSampler:
    """每 20 毫秒数一次这个库上的连接数（pg_stat_activity），记下峰值。"""

    def __init__(self, dsn: str):
        import psycopg

        self.conn = psycopg.connect(dsn, autocommit=True)
        self.peak = 0
        self._halt = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        q = "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
        while not self._halt.wait(0.02):
            self.peak = max(self.peak, self.conn.execute(q).fetchone()[0])

    def stop(self) -> int:
        self._halt.set()
        self.thread.join(2)
        self.conn.close()
        return self.peak


def part3(args, server_uri: str, ctx) -> None:
    import psycopg

    from agentkit.contrib.postgres import PostgresCheckpointer, PostgresJobQueue
    from agentkit.testing import create_database

    banner("第 3 部分：同一批任务 —— 同步 worker（多进程）vs 单进程异步 worker")
    n = args.bench_jobs
    info(f"{n} 个 Agent 任务，每个调 2 次模型（每次模拟 {BENCH_LATENCY} 秒）+ 1 次工具 + 约 5 次检查点写入。"
         f"{'（真实模式下这一部分也用模拟模型：它测的是 worker 架构，不是模型速度。）' if not args.offline else ''}")
    dsn = create_database(server_uri, "bench")
    queue, ckpt = PostgresJobQueue(dsn), PostgresCheckpointer(dsn)
    queue.setup()
    ckpt.setup()

    def reset() -> None:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("TRUNCATE agent_jobs, agent_runs RESTART IDENTITY")
        for i in range(n):
            queue.enqueue("agent", {"op": "run", "input": f"VPN 连不上 #{i}"}, tenant_id="acme")

    variants = [
        ("同步 · 1 进程", "sync", 1, "-", {}),
        ("同步 · 3 进程", "sync", 3, "-", {}),
        ("异步 · 1 进程 · 并发 16", "async", 1, "16", {"concurrency": 16, "pool": 16, "hold": False}),
        ("异步 · 并发 16 · 连接池 4", "async", 1, "4", {"concurrency": 16, "pool": 4, "hold": False}),
        ("异步 · 并发 16 · 全程占着连接", "async", 1, "4（业务库）", {"concurrency": 16, "pool": 16, "hold": True}),
    ]
    rows = []
    for label, kind, procs, pool, extra in variants:
        reset()
        sampler = ConnSampler(dsn)
        t0 = time.perf_counter()
        if kind == "sync":
            ps = [spawn(ctx, bench_sync_main, {"dsn": dsn, "n": n, "worker_id": f"sync-{i}"}) for i in range(procs)]
            for p in ps:
                p.join(300)
            elapsed, llm_peak = time.perf_counter() - t0, procs
        else:
            out = ctx.Queue()
            p = spawn(ctx, bench_async_main, {"dsn": dsn, "n": n, **extra}, out)
            res = out.get(timeout=300)
            p.join(30)
            elapsed, llm_peak = res["elapsed"], res["llm_peak"]
        peak = sampler.stop()
        done = queue.stats()["counts"]["succeeded"]
        per_proc = "1" if kind == "sync" else str(extra["concurrency"])
        rows.append([label, f"{procs} × {per_proc}", pool, str(peak), f"{elapsed:.2f}s", f"{done / elapsed:.1f}", str(llm_peak)])
        info(f"{pad(label, 32)} 完成 {done}/{n}，{elapsed:.2f}s")
    step("对比")
    table(["方案", "进程×并发", "连接池", "连接峰值", "耗时", "任务/秒", "在途模型调用峰值"], rows, [32, 11, 12, 10, 9, 9, 16])
    info("连接峰值 = 这个库上同时打开的连接数（含父进程入队 / 统计用的连接，不含采样用的那一个）。")
    info("观察：① 同步 worker 等模型时整个进程闲着，只能靠加进程提速，每个进程还要自己的连接；")
    info("② 一个异步进程同时推进 16 个任务，吞吐是 3 个同步进程的好几倍；")
    info("③ Agent 的时间几乎都花在等模型上，检查点写入只借用连接几毫秒 → 池只有 4 个连接也够 16 路并发；")
    info("④ 但如果每个任务在等模型时一直占着连接（比如开着事务调模型），并发就被卡成连接数（这里是 4）。")
    queue.close()
    ckpt.close()


# =====================================================================================
# 第 4 部分：异步 worker 优雅停机 —— 被取消的写操作，resume 后不重复
# =====================================================================================

SHUTDOWN_TITLES = ["3 楼打印机卡纸", "投影仪没信号", "网口没反应", "显示器有竖线", "笔记本电池鼓包", "耳机麦克风没声音"]


def part4(args, server_uri: str) -> None:
    from agentkit.testing import create_database

    banner("第 4 部分：异步 worker 优雅停机 —— 被取消的写操作，resume 后不重复")
    info("场景：一个 Pod 里的异步 worker 同时在跑 6 个建工单任务。其中一个任务的下游已经把工单建好了，")
    info("响应还在路上，这时 Pod 收到 SIGTERM。宽限期到了它还没回来，只能取消。")
    if not args.offline:
        info("（真实模式下这一部分也用模拟模型：要确定地卡在“下游已执行、响应未返回”这一刻。）")
    asyncio.run(_part4(create_database(server_uri, "shutdown")))


async def _part4(dsn: str) -> None:
    import psycopg

    from agentkit import call_tool, reply
    from agentkit.aio import AsyncAgent, AsyncScriptedLLM
    from agentkit.contrib.postgres import AgentJobHandler, AsyncPostgresCheckpointer, AsyncPostgresJobQueue, run_async_worker

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(TICKETS_DDL)
    calls: list[tuple[str, str, bool]] = []  # (worker, 幂等键, 是否新建)
    in_flight = asyncio.Event()

    @tool(risk="write")
    async def create_ticket(title: Annotated[str, Field(description="一句话概括问题")], ctx: ToolContext) -> str:
        """为设备故障创建 IT 工单，返回工单号。"""
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "INSERT INTO tickets (idempotency_key, tenant_id, run_id, title, created_by) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                (ctx.idempotency_key, ctx.tenant_id, ctx.run_id, title, WORKER.get()))
            row = await cur.fetchone()
            created = row is not None
            if not created:
                row = await (await conn.execute("SELECT id FROM tickets WHERE idempotency_key = %s",
                                                (ctx.idempotency_key,))).fetchone()
        calls.append((WORKER.get(), ctx.idempotency_key, created))
        if title.startswith("笔记本") and len([c for c in calls if c[1] == ctx.idempotency_key]) == 1:
            in_flight.set()
            await asyncio.sleep(60)  # 下游已经建好了，响应"还在路上"
        return f"T-{1000 + row[0]}"

    def responder(messages):
        last = messages[-1]
        if last["role"] == "tool" and last["content"].startswith("T-"):
            return reply(f"已为你创建工单 {last['content']}")
        text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        return call_tool("create_ticket", title=text)

    queue, ckpt = AsyncPostgresJobQueue(dsn, base_backoff=0), AsyncPostgresCheckpointer(dsn)
    await queue.setup()
    await ckpt.setup()
    for i, title in enumerate(SHUTDOWN_TITLES):
        await queue.enqueue("agent", {"op": "run", "input": title}, tenant_id="acme", idempotency_key=f"shutdown-{i}")

    def pod(name: str) -> AgentJobHandler:  # 一个 Pod = 一个共享的 AsyncAgent
        agent = AsyncAgent(AsyncScriptedLLM(responder=responder, latency=0.1), [create_ticket], checkpointer=ckpt)
        return AgentJobHandler(agent, ckpt)

    step("pod-1 启动：并发 8，租约 2 秒，宽限期 0.5 秒")
    stop = asyncio.Event()

    async def sigterm_when_downstream_is_done():
        await in_flight.wait()
        info("K8s 发来 SIGTERM：pod-1 不再领取新任务，等在途任务最多 0.5 秒")
        stop.set()

    WORKER.set("pod-1")
    stats, _ = await asyncio.gather(
        run_async_worker(queue, pod("pod-1"), worker_id="pod-1", stop_event=stop, concurrency=8,
                         lease_seconds=2, grace_period=0.5, poll_interval=0.02),
        sigterm_when_downstream_is_done(),
    )
    info(f"pod-1 退出：完成 {stats['succeeded']} 个，宽限期后取消 {stats['cancelled']} 个（不提交、不归还）")
    stuck = [r for r in await ckpt.list_runs(limit=20) if r["status"] == "cancelled"]
    for row in stuck:
        state = (await ckpt.get_run(row["run_id"]))["state"]
        last = state["messages"][-1]
        pending = [tc["id"] for tc in last.get("tool_calls") or []]
        info(f"run {row['run_id']} 的检查点：status=cancelled，最后一条是 assistant 的写工具调用 {pending}，"
             f"没有补“未执行”（保持未回答）")

    step("等租约自然过期（2 秒），pod-2 接手")
    await asyncio.sleep(2.2)
    WORKER.set("pod-2")
    stats2 = await run_async_worker(queue, pod("pod-2"), worker_id="pod-2", stop_event=asyncio.Event(),
                                    concurrency=8, max_jobs=len(stuck), poll_interval=0.02)
    info(f"pod-2 完成 {stats2['succeeded']} 个")

    step("📊 结果")
    for row in stuck:
        keys = [(w, k, new) for w, k, new in calls if k.startswith(row["run_id"] + ":")]
        for w, k, new in keys:
            info(f"{w} 执行 create_ticket，幂等键 {k} → {'新建' if new else '唯一约束命中，返回已有工单'}")
        same = len({k for _, k, _ in keys}) == 1
        info(f"两次执行用的是{'同一个' if same else '不同的'}幂等键 {'✅' if same else '❌'}")
    with psycopg.connect(dsn) as c:
        n, distinct = c.execute("SELECT count(*), count(DISTINCT run_id) FROM tickets").fetchone()
    info(f"工单 {n} 张，对应 {distinct} 个 run → {'没有重复 ✅' if n == distinct else '有重复 ❌'}")
    info("原理：写工具在取消时保持“未回答”，resume 重放的是检查点里的同一个 tool_call（同一个 call_id），")
    info("幂等键 = run_id:call_id 不变，下游唯一约束把第二次执行变成“返回已有结果”。")
    info("如果取消时给它补上“未执行”，模型在 resume 后会重新发起一个 call_id 不同的调用，幂等键变了，工单就会建两张。")
    await queue.close()
    await ckpt.close()


WORKER: contextvars.ContextVar[str] = contextvars.ContextVar("demo_worker", default="?")  # 当前是哪个 Pod 在执行工具


# =====================================================================================
# 入口
# =====================================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用 ScriptedLLM，不调用真实模型")
    parser.add_argument("--only", default="1,2,3,4", help="只跑某几个部分，例如 --only 1,2")
    parser.add_argument("--bench-jobs", type=int, default=24, help="第 3 部分的任务数")
    parser.add_argument("--verbose", action="store_true", help="打印每一个 worker 事件")
    args = parser.parse_args()
    parts = {int(x) for x in args.only.split(",") if x.strip()}

    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        print(f"本 Demo 需要可选依赖：{', '.join(missing)} 未安装。\n请运行：{INSTALL_HINT}")
        return 0
    if not args.offline:
        from agentkit.config import env

        if not env("LLM_API_KEY"):
            print("没有配置 LLM_API_KEY（.env），改用 --offline 模式运行。")
            args.offline = True

    import redis

    from agentkit.testing import create_database, embedded_postgres, fake_redis_server

    ctx = mp.get_context("spawn")
    print(f"模式：{'离线（ScriptedLLM）' if args.offline else '真实模型（' + (os.environ.get('LLM_MODEL') or '默认模型') + '）'}")
    with contextlib.ExitStack() as stack:
        try:  # 只有"拉起基础设施"失败才降级退出；后面的逻辑出错就让它报错
            server_uri = stack.enter_context(embedded_postgres())
            redis_url = stack.enter_context(fake_redis_server())
        except (ImportError, OSError, RuntimeError) as e:
            print(f"本机基础设施启动失败：{type(e).__name__}: {e}\n请确认已安装：{INSTALL_HINT}")
            return 0
        print(f"嵌入式 Postgres：{server_uri.split('?')[0]}（unix socket）；fakeredis：{redis_url}")
        dsn = create_database(server_uri, "helpdesk")
        r = redis.Redis.from_url(redis_url)
        if 1 in parts or 2 in parts:
            part1(args, dsn, redis_url, r, ctx)
        if 2 in parts:
            part2(args, dsn, redis_url, r, ctx)
        if 3 in parts:
            part3(args, server_uri, ctx)
        if 4 in parts:
            part4(args, server_uri)
    print("\n完成。讲义：lessons/26_state_and_queues/README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
