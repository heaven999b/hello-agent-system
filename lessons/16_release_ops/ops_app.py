"""第 16 课场景 3~5 的 worker 应用：每个 worker 进程这样加载它（WorkerPool 替你拼好这条命令）：

    python -m agentkit.distributed.worker --queue sqlite:///.../ops.db \\
        --app lessons/16_release_ops/ops_app.py:make_handler --opt poll=0.2 --opt latency=0.02

它运行在**独立的子进程**里，和控制面（demo.py）之间只通过同一个 SQLite 文件交流：
  - 任务队列：控制面把"线上流量"一条条入队，worker 领取、执行、提交；
  - 配置中心（configcenter.ConfigCenter）：
      "release:itbuddy.system"  全部 prompt 版本 + 当前流量分配（灰度控制器写入）
      "flags"                   紧急开关（值班工程师写入）
    每个 worker 进程一个 ConfigWatcher，后台每 poll 秒查一次版本号；看到新版本时打一行 config_changed 事件；
  - 记录表：requests（每个请求一行：用了哪个版本、按哪个配置版本分流、结果、耗时、成本）、
            tickets（写工具 create_ticket 每真正执行一次记一行，带执行时看到的开关版本）。

每个请求：读本地快照 → pick_version(user_id, rollout) 选 prompt 版本 → 用那个版本的 system prompt 跑 Agent
（KillSwitch 在每次工具调用前读开关快照）→ 把真实结果写进 requests。控制面的灰度指标就从这张表算出来。
"""

from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar

from agentkit import Agent, Hook, ScriptedLLM
from agentkit.distributed import WorkerContext, current_job
from agentkit.testing import load_sibling

cc = load_sibling(__file__, "configcenter")
ks = load_sibling(__file__, "killswitch")
reg = load_sibling(__file__, "registry")
itb = load_sibling(__file__, "itbuddy")

RELEASE = "release:itbuddy.system"

# 这个请求里每次模型调用的模拟耗时（延迟模型）。默认取 --opt latency；请求的 payload 里可以带 model_latency 覆盖
# （场景 5 的报修流量用 0.3 秒：比配置轮询间隔长，和真实情况一样 —— 真实的模型调用通常要 1~5 秒）。
# 用 ContextVar：每个任务是一个独立的 asyncio Task，互不影响。
MODEL_LATENCY: ContextVar[float] = ContextVar("model_latency", default=0.02)

DDL = (
    "CREATE TABLE IF NOT EXISTS requests (job_id INTEGER PRIMARY KEY, phase TEXT, user_id TEXT, tenant TEXT, "
    "version INTEGER, release_v INTEGER, flags_v INTEGER, status TEXT, stop_reason TEXT, errors INTEGER, blocked INTEGER, "
    "error_tools TEXT, latency_ms REAL, cost_usd REAL, worker TEXT, pid INTEGER, started REAL, finished REAL, output TEXT)",
    "CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, title TEXT, priority TEXT, "
    "worker TEXT, pid INTEGER, flags_v INTEGER, t REAL)",
)


def create_tables(conn) -> None:
    for sql in DDL:
        conn.execute(sql)


class Outcome(Hook):
    """数出这次运行里出错的工具调用（异常 / 超时）和被紧急开关拦下的调用，记进 state.metadata。"""

    def after_tool(self, state, call, result):
        if result.error_type in ("exception", "timeout"):
            state.metadata["tool_errors"] = state.metadata.get("tool_errors", 0) + 1
            state.metadata.setdefault("error_tools", []).append(call.name)
        elif result.error_type == "denied":
            state.metadata["blocked"] = state.metadata.get("blocked", 0) + 1
        return None


async def make_handler(wctx: WorkerContext):
    opts = wctx.options
    wid = wctx.worker_id
    db = wctx.db  # 队列用的 SQLiteDB：配置中心、记录表共用这一个连接和它的专用线程

    def emit(event: str, **info) -> None:  # 和 run_worker 的事件同样的格式：一行 JSON
        line = {"event": event, "worker_id": wid, "pid": os.getpid(), "t": round(time.time(), 4), **info}
        print(json.dumps(line, ensure_ascii=False, default=str), flush=True)

    center = cc.ConfigCenter(db)
    await center.setup()
    await db.write(create_tables)
    watcher = cc.ConfigWatcher(
        center, [RELEASE, "flags"], poll_interval=float(opts.get("poll", 0.5)),
        on_change=lambda name, old, new, doc: emit("config_changed", name=name, version=new, previous=old),
    )
    await watcher.start()  # 启动时读不到配置 → 直接失败，不接流量

    async def on_ticket(title: str, priority: str, ctx) -> str:
        job = current_job()
        flags_v = watcher.version("flags")

        def op(c):
            cur = c.execute("INSERT INTO tickets (job_id, title, priority, worker, pid, flags_v, t) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (job.id if job else None, title, priority, wid, os.getpid(), flags_v, time.time()))
            return cur.lastrowid

        return f"T-{3000 + await db.write(op)}"

    tools = itb.make_tools(on_ticket)
    switch = ks.KillSwitch(lambda: watcher.snapshot("flags"), tools)  # 每次检查读本地快照，不查库
    outcome = Outcome()
    default_latency = float(opts.get("latency", 0.02))
    llm = ScriptedLLM(responder=itb.scripted_model, latency=lambda n: MODEL_LATENCY.get(), model="scripted", keep_calls=0)

    async def handler(job) -> dict:
        p = job.payload
        MODEL_LATENCY.set(float(p.get("model_latency", default_latency)))
        started = time.time()
        release, release_v = watcher.snapshot(RELEASE), watcher.version(RELEASE)
        version = reg.pick_version(p["user_id"], reg.rollout_from_dict(release["rollout"]))
        pv = release["versions"][str(version)]
        agent = Agent(llm, tools, system_prompt=pv["template"], name="itbuddy", max_steps=pv["params"].get("max_steps", 4),
                      hooks=[switch, outcome])
        t0 = time.perf_counter()
        res = await agent.run(p["input"], metadata={"tenant_id": p["tenant"], "user_id": p["user_id"], "roles": ["employee"]})
        latency_ms = (time.perf_counter() - t0) * 1000
        errors, blocked = res.metadata.get("tool_errors", 0), res.metadata.get("blocked", 0)
        row = (job.id, p.get("phase"), p["user_id"], p["tenant"], version, release_v, watcher.version("flags"), res.status,
               res.stop_reason, errors, blocked, ",".join(res.metadata.get("error_tools", [])), round(latency_ms, 3),
               res.cost_usd, wid, os.getpid(), started, time.time(), (res.output or "")[:200])
        await db.write(lambda c: c.execute(f"INSERT OR REPLACE INTO requests VALUES ({','.join('?' * len(row))})", row))
        return {"version": version, "status": res.status, "stop_reason": res.stop_reason, "errors": errors,
                "blocked": blocked, "output": res.output, "worker": wid}

    async def aclose() -> None:  # worker 进程退出前调用：停掉后台轮询
        await watcher.aclose()

    handler.aclose = aclose
    return handler
