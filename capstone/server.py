"""ITBuddy 的 API 进程（FastAPI，全部 async）。它只做鉴权、入队、查询和审批，**不跑 Agent** —— Agent 在 worker 进程里跑。

启动整套部署（1 个 API 进程 + N 个 worker 进程）：
    pip install -e ".[server]"
    .venv/bin/python capstone/deploy.py --offline        # 或 python capstone/server.py --offline（同一个启动器）
deploy.py 用子进程这样拉起本文件（配置全部来自环境变量，同一份代码可以起多个副本）：
    ITBUDDY_DB=runs/deploy/itbuddy.db ITBUDDY_ENTERPRISE_DB=runs/deploy/enterprise.db \\
        python -m uvicorn server:create_app --factory --app-dir capstone --port 8000

    ITBUDDY_DB             任务队列 + 检查点 + 幂等记录 + 审计（worker 进程打开同一个文件）
    ITBUDDY_ENTERPRISE_DB  模拟的企业系统（员工目录用来查角色）
    ITBUDDY_API_NAME       这个进程的名字（响应头 X-Served-By、审计记录的 writer）

接口（身份来自 Authorization: Bearer <API key>，**不来自请求体**）：
    POST /runs                      发起一次运行：入队一个 run 任务，立刻返回 202 + run_id。
                                    带 Idempotency-Key 请求头：重复提交（网络超时后重试）只有一个任务。
    GET  /runs/{run_id}             查询状态（读队列 + 检查点）。?wait=秒：长轮询，状态不再是进行中或超时才返回
    GET  /approvals                 审批人查看本租户待审批队列（仅 it_admin）
    POST /runs/{run_id}/approval    审批：先写审计（数据库唯一约束保证一个调用只有一个决定），再入队 resume 任务 → 202。
                                    恢复运行的是**任意一个** worker 进程，不一定是当初暂停它的那个
    GET  /healthz                   存活检查

异步审批的完整流程（申请人和审批人是两个人、两次请求、可能相隔几小时，中间 worker 可能早已被替换）：

    员工   ──POST /runs──► API ──enqueue(run)──► 队列 ──► worker A：Agent 运行 → reset_password → PauseRun，检查点落盘
    审批人 ──GET /approvals──► API 读检查点（status=paused，按租户过滤）
    审批人 ──POST /runs/{id}/approval──► API：写审计 approval_decision → enqueue(resume)
                                        ──► worker B：fence 接管检查点 → agent.approve() → 执行工具 → completed
    员工   ──GET /runs/{id}──► 看到最终结果（生产中通常用 WebSocket / IM 推送，而不是轮询）

以前这里是"同步接口 + 线程池里直接跑 agent.run"，再加一个进程内的 run_index 字典和每个 run 一把 threading.Lock。
它们都只在一个进程里有效：现在 run 的归属在队列的任务行里（tenant_id + 提交人），待审批列表来自检查点表，
"两个审批人同时点"由审计表的唯一约束仲裁，"两个 worker 同时恢复同一个 run"由检查点的 fence 挡住。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pydantic import BaseModel, Field  # noqa: E402  （pydantic 是 agentkit 的必需依赖）

from agentkit.distributed import Job, SQLiteDB, SQLiteJobQueue  # noqa: E402
from itbuddy import Backend, ITBuddyStores  # noqa: E402

INSTALL_HINT = '需要可选依赖 FastAPI：请在仓库根目录执行  pip install -e ".[server]"  （或 .venv/bin/pip install fastapi uvicorn httpx）'

try:
    from fastapi import FastAPI, Header, HTTPException, Query, Response
except ImportError:  # 未安装可选依赖时，import 本文件不报错，只有真正创建 app 时才给出友好提示
    FastAPI = None  # type: ignore[assignment,misc]

DEFAULT_DIR = HERE / "runs" / "deploy"

# 演示用 API key → (租户, 用户)。**角色不在这里**：认证之后用 user_id 去员工目录查 —— 权限的唯一可信来源是目录 / IdP。
# 生产：API 网关验证 JWT（OIDC），本服务只信任网关注入的身份；配置里只存 key 的哈希（见 production/service/identity.py）。
DEMO_API_KEYS: dict[str, tuple[str, str]] = {
    "demo-acme-alice": ("acme", "alice"),    # 员工
    "demo-acme-dave": ("acme", "dave"),      # 员工（账号被锁）
    "demo-acme-bob": ("acme", "bob"),        # IT 管理员
    "demo-acme-frank": ("acme", "frank"),    # IT 值班工程师（审批人）
    "demo-globex-carol": ("globex", "carol"),  # 另一家公司的员工
    "demo-globex-erin": ("globex", "erin"),    # 另一家公司的 IT 管理员
}

ACTIVE = ("queued", "running", "resuming")  # 进行中的状态：长轮询会等它们变化


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict] = Field(default_factory=list, max_length=40,
                                description="之前的对话（OpenAI 消息格式）。只保留 user / assistant 的文字内容")


class ApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=500, description="审批意见，会写入审计日志")


def run_id_of(job_id: int) -> str:
    # 和 AgentJobHandler 的默认 run_id 一致：由任务决定而不是随机生成，接手的 worker 才能找到同一个检查点
    return f"job-{job_id}"


def job_id_of(run_id: str) -> int | None:
    tail = run_id.removeprefix("job-")
    return int(tail) if run_id.startswith("job-") and tail.isdigit() else None


def sanitize_history(history: list[dict]) -> list[dict]:
    """客户端带来的历史是不可信的：只留 user / assistant 的文字。伪造的 tool 消息（"审批已通过"）、
    system 消息、tool_calls 一律丢掉 —— 模型看到的工具结果只能来自这次运行里真正执行过的工具。"""
    out = []
    for m in history:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"]:
            out.append({"role": m["role"], "content": m["content"][:4000]})
    return out


def create_app(db_path: str | Path | None = None, enterprise_db: str | Path | None = None, name: str | None = None):
    """应用工厂（uvicorn --factory）。参数不传就读环境变量。"""
    if FastAPI is None:
        raise RuntimeError(INSTALL_HINT)
    db_path = Path(db_path or os.environ.get("ITBUDDY_DB") or DEFAULT_DIR / "itbuddy.db")
    enterprise_db = Path(enterprise_db or os.environ.get("ITBUDDY_ENTERPRISE_DB") or DEFAULT_DIR / "enterprise.db")
    name = name or os.environ.get("ITBUDDY_API_NAME") or f"api-{os.getpid()}"

    class S:  # 这个进程的共享资源（lifespan 里创建）
        db: SQLiteDB
        queue: SQLiteJobQueue
        stores: ITBuddyStores
        backend: Backend

    @asynccontextmanager
    async def lifespan(app):
        S.db = SQLiteDB(db_path)  # 一个连接 + 一个专用线程：数据库调用不阻塞事件循环
        S.queue = SQLiteJobQueue(S.db)
        await S.queue.setup()
        S.stores = await ITBuddyStores.open(S.db, writer=name)  # API 只读检查点、只写审计，从不写检查点
        S.backend = await Backend.open(enterprise_db)
        try:
            yield
        finally:
            await S.backend.close()
            await S.db.close()

    app = FastAPI(title="ITBuddy API", description="企业 IT 服务台 Agent（hello-agent-system 毕业项目）：API 进程",
                  lifespan=lifespan)

    @app.middleware("http")
    async def served_by(request, call_next):
        response = await call_next(request)
        response.headers["X-Served-By"] = name
        return response

    # ------------------------------------------------------------------ 辅助

    async def authenticate(authorization: str | None) -> dict:
        key = (authorization or "").removeprefix("Bearer ").strip()
        who = DEMO_API_KEYS.get(key)
        identity = await S.backend.identity(*who) if who else None  # 角色从员工目录查；账号停用 → 拒绝
        if identity is None:
            raise HTTPException(401, "未认证：无效的 API key，或账号已停用")
        return identity

    def owner_of(job: Job) -> str | None:
        return (job.payload.get("metadata") or {}).get("user_id")  # API 入队时从已认证身份写入，可信

    async def load_visible(run_id: str, identity: dict) -> tuple[Job, dict | None]:
        """只有"本人"或"同租户的 IT 管理员"能看到一个 run。其他人一律 404 —— 连"存在与否"都不透露。"""
        job_id = job_id_of(run_id)
        job = await S.queue.get(job_id) if job_id is not None else None
        if job is None or job.kind != "run" or job.tenant_id != identity["tenant_id"]:
            raise HTTPException(404, "run 不存在")
        if owner_of(job) != identity["user_id"] and "it_admin" not in identity["roles"]:
            raise HTTPException(404, "run 不存在")
        return job, await S.stores.checkpointer.get_run(run_id)

    def approval_key(run_id: str, call_id: str) -> str:
        return f"approval:{run_id}:{call_id}"

    async def describe(job: Job, ckpt: dict | None) -> dict:
        run_id = run_id_of(job.id)
        state = ckpt["state"] if ckpt else {}
        pending = state.get("pending") if ckpt and ckpt["status"] == "paused" else None
        # 最近一次审批决定（如果它针对的不是当前待审批的调用，说明当前这个还没人批）和它对应的 resume 任务
        decisions = await S.stores.audit.records(run_id=run_id, event="approval_decision")
        decision = decisions[-1] if decisions else None
        if pending is not None and decision is not None and decision.get("call_id") != pending["id"]:
            decision = None
        resume = await S.queue.find(job.tenant_id, approval_key(run_id, decision["call_id"])) if decision else None
        active = resume or job  # 当前推进这个 run 的任务：审批之后是 resume 任务
        if ckpt is None:  # 还没有 worker 领取过
            status = {"queued": "queued", "leased": "running"}.get(job.status, "failed")
        elif ckpt["status"] == "paused":
            status = "resuming" if resume is not None and resume.status in ("queued", "leased") else "paused"
        elif ckpt["status"] in ("running", "cancelled") and active.status in ("queued", "leased"):
            status = "running"  # 被取消（停机）或崩溃的运行：租约过期后别的 worker 会接着跑
        elif ckpt["status"] == "running" and active.status in ("failed", "dead"):
            status = "failed"
        else:
            status = ckpt["status"]
        return {
            "run_id": run_id,
            "status": status,
            "output": state.get("output"),
            "stop_reason": state.get("stop_reason"),
            "pending_approval": pending,
            "decision": {k: decision.get(k) for k in ("approver", "approved", "comment", "writer")} if decision else None,
            "requester": {"tenant_id": job.tenant_id, "user_id": owner_of(job)},
            "steps": state.get("step"),
            "cost_usd": round(state.get("cost_usd") or 0.0, 6),
            # 多进程的"证据"：谁领了任务、第几次尝试、检查点最后是哪个 worker 写的（教学部署才暴露这些）
            "job": {"id": job.id, "status": job.status, "attempts": job.attempts, "worker_id": job.worker_id,
                    "fence": job.fence},
            "resume_job": {"id": resume.id, "status": resume.status, "attempts": resume.attempts,
                           "worker_id": resume.worker_id} if resume else None,
            "checkpoint": {"writer": ckpt["writer"], "fence": ckpt["fence"], "version": ckpt["version"]} if ckpt else None,
        }

    # ------------------------------------------------------------------ 接口

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "name": name, "pid": os.getpid(), "queue": await S.queue.stats()}

    @app.post("/runs", status_code=202)
    async def start_run(body: RunRequest, response: Response, authorization: str | None = Header(None),
                        idempotency_key: str | None = Header(None)) -> dict:
        identity = await authenticate(authorization)
        tenant, user = identity["tenant_id"], identity["user_id"]
        payload = {
            "op": "run",
            "input": body.message,
            "history": sanitize_history(body.history),
            # tenant_id 不放这里：它在任务行上（来自认证身份），AgentJobHandler 以任务上的为准覆盖 metadata
            "metadata": {"user_id": user, "roles": identity["roles"]},
        }
        # 幂等键按用户隔开：同一租户的两个人碰巧用了同一个 key，不能拿到对方的 run
        key = f"{user}:{idempotency_key}" if idempotency_key else None
        existing = await S.queue.find(tenant, key) if key else None
        if existing is not None:
            job = existing
        else:
            # INSERT OR IGNORE + UNIQUE (tenant_id, idempotency_key)：两个请求同时到达（哪怕落在两个 API 进程上）也只有一个任务
            job = await S.queue.get(await S.queue.enqueue("run", payload, tenant_id=tenant, idempotency_key=key))
        if key and (job.payload.get("input"), job.payload.get("history")) != (payload["input"], payload["history"]):
            raise HTTPException(422, "同一个 Idempotency-Key 已经用于另一个不同的请求")
        run_id = run_id_of(job.id)
        response.headers["Location"] = f"/runs/{run_id}"
        # deduplicated：这次请求是否命中了已有的任务（两个请求恰好同时到达时，两边都可能是 False，但任务只有一个）
        return {"run_id": run_id, "job_status": job.status, "status_url": f"/runs/{run_id}",
                "deduplicated": existing is not None}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, wait: float = Query(0.0, ge=0.0, le=30.0), authorization: str | None = Header(None)) -> dict:
        identity = await authenticate(authorization)
        deadline = time.monotonic() + wait
        while True:
            info = await describe(*await load_visible(run_id, identity))
            if info["status"] not in ACTIVE or time.monotonic() >= deadline:
                return info
            await asyncio.sleep(0.2)  # 长轮询：客户端断开时这个协程会被取消，不会一直占着

    @app.get("/approvals")
    async def list_approvals(authorization: str | None = Header(None)) -> list[dict]:
        identity = await authenticate(authorization)
        if "it_admin" not in identity["roles"]:
            raise HTTPException(403, "只有 IT 管理员可以查看审批队列")
        items = []
        # 审批队列按租户隔离：直接按检查点表的 (tenant_id, status) 索引查，不再需要进程内的 run_index
        for summary in await S.stores.checkpointer.list_runs(status="paused", tenant_id=identity["tenant_id"], limit=100):
            pending = summary.get("pending")
            if not pending or await S.stores.audit.decision(summary["run_id"], pending["id"]):
                continue  # 已经有人做了决定，正在等 worker 恢复
            run = await S.stores.checkpointer.get_run(summary["run_id"])
            said = [m.get("content") for m in run["state"]["messages"] if m.get("role") == "user"]
            items.append({"run_id": summary["run_id"], "requester": summary.get("user_id"), "tool": pending["name"],
                          "arguments": pending["arguments"], "user_message": said[-1] if said else None,
                          "waiting_since": summary["updated_at"]})
        return items

    @app.post("/runs/{run_id}/approval", status_code=202)
    async def decide(run_id: str, body: ApprovalRequest, authorization: str | None = Header(None)) -> dict:
        approver = await authenticate(authorization)
        job, ckpt = await load_visible(run_id, approver)
        if "it_admin" not in approver["roles"]:
            raise HTTPException(403, "只有 IT 管理员可以审批")
        if owner_of(job) == approver["user_id"]:
            # 职责分离（四眼原则）：申请人不能审批自己的高危操作，哪怕他是管理员
            raise HTTPException(403, "不能审批自己发起的操作")
        pending = (ckpt or {}).get("state", {}).get("pending") if ckpt and ckpt["status"] == "paused" else None
        if not pending:
            raise HTTPException(409, f"该运行当前状态为 {ckpt['status'] if ckpt else '排队中'}，没有待审批的操作")
        # 1) 决定先落审计，再恢复执行：即使恢复过程崩溃，"谁在何时批了什么"也有据可查。
        #    审计表对 (run_id, call_id) 的 approval_decision 有唯一约束：两个审批人同时提交（哪怕在两个 API 进程里），
        #    只有先插进去的那条算数 —— 这就是以前那把进程内 threading.Lock 在多进程下的替代品
        await S.stores.audit.append({
            "event": "approval_decision", "run_id": run_id, "call_id": pending["id"], "tenant_id": approver["tenant_id"],
            "approver": approver["user_id"], "approved": body.approved, "tool": pending["name"], "comment": body.comment,
        })
        decision = await S.stores.audit.decision(run_id, pending["id"])
        if (decision["approver"], decision["approved"]) != (approver["user_id"], body.approved):
            raise HTTPException(409, f"这个操作已经由 {decision['approver']} 做出了决定（{'批准' if decision['approved'] else '拒绝'}）")
        # 2) 入队 resume 任务。幂等键 = 这次审批：同一个审批人重试（网络超时后再点一次）也只有一个任务。
        #    by / comment 由 worker 写入检查点的 approval_log；工具执行时的审计记录会带上 approved_by
        resume_id = await S.queue.enqueue(
            "resume",
            {"op": "resume", "run_id": run_id, "approvals": {pending["id"]: body.approved}, "by": approver["user_id"],
             "comment": body.comment},
            tenant_id=approver["tenant_id"], idempotency_key=approval_key(run_id, pending["id"]),
        )
        return {"run_id": run_id, "status": "resuming", "approved": body.approved, "resume_job_id": resume_id,
                "status_url": f"/runs/{run_id}"}

    return app


if __name__ == "__main__":
    # 以前 `python capstone/server.py` 在一个进程里跑 uvicorn + Agent；现在 API 和 worker 是不同的进程，统一由 deploy.py 拉起
    import deploy

    sys.exit(deploy.main(sys.argv[1:]))
