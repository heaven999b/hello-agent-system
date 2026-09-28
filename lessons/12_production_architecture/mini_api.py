"""迷你部署的 API 进程（FastAPI）。由 deployment.py 用真正的子进程启动：

    python -m uvicorn mini_api:app --app-dir lessons/12_production_architecture --port 8101

同一份代码可以起好几个进程（就像负载均衡后面的几个 API 副本），配置全部来自环境变量：
    MINI_DB            SQLite 文件：任务队列、检查点、跨进程令牌桶都在里面（worker 进程用同一个文件）
    MINI_RATE_BACKEND  memory = 每个进程自己的令牌桶（第 12 课练习的 TenantRateLimiter，状态在进程内存里）
                       sqlite = 所有进程共用的令牌桶（agentkit.distributed.SQLiteTokenBucket）
    MINI_ROUTER        exercise.py 或 solution.py 的路径：用里面的 choose_model 做模型路由、Plan 定义套餐
    MINI_NAME          这个进程的名字（写进响应头 X-Served-By，看得出请求落到了哪个进程）
    MINI_OFFLINE       1 = 同步接口用剧本模型；0 = 真实模型

接口：
    POST /runs          鉴权 → 按租户限流（429 + Retry-After）→ 路由选模型 → 入队 → 立刻返回 202 + run_id
    GET  /runs/{id}     读任务和检查点：状态、第几步、最后由哪个 worker 写入、fence、用量（sync- 开头的读同步接口留下的检查点）
    POST /runs/sync     ❌ 反面教材：在这个 HTTP 请求里把 Agent 跑完再返回（长任务会被客户端 / 网关超时掐断）
    GET  /healthz       存活检查
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

import hr_agent  # 同目录模块（uvicorn --app-dir 把本目录放进了 sys.path）
from agentkit.distributed import SQLiteCheckpointer, SQLiteDB, SQLiteJobQueue, SQLiteTokenBucket

# 身份：生产中来自网关验证过签名的 JWT；这里用一张 API key 表代替。身份绝不从请求体或模型输出里拿。
API_KEYS = {
    "key-acme-alice": {"tenant": "acme", "user": "u-alice"},
    "key-globex-bob": {"tenant": "globex", "user": "u-bob"},
    "key-hooli-carl": {"tenant": "hooli", "user": "u-carl"},
}
TENANT_PLANS = {"acme": "enterprise", "globex": "pro", "hooli": "free"}


def _load(path: str):
    name = f"mini_router_{Path(path).stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass 需要在 sys.modules 里找到自己的模块
    spec.loader.exec_module(module)
    return module


ROUTER = _load(os.environ.get("MINI_ROUTER", str(Path(__file__).with_name("solution.py"))))
PLANS = {
    "free": ROUTER.Plan("free", capacity=5, refill_rate=2),
    "pro": ROUTER.Plan("pro", capacity=20, refill_rate=5),
    "enterprise": ROUTER.Plan("enterprise", capacity=50, refill_rate=20),
}
# 虚构的模型目录：名字和价格都是示例（美元 / 百万 token），不对应任何真实产品
CATALOG = [
    ROUTER.ModelSpec("lite", context_window=32_000, supports_tools=False, strong=False, input_price=0.05, output_price=0.2),
    ROUTER.ModelSpec("mini", context_window=128_000, supports_tools=True, strong=False, input_price=0.15, output_price=0.6),
    ROUTER.ModelSpec("pro", context_window=200_000, supports_tools=True, strong=True, input_price=3.0, output_price=15.0),
]

NAME = os.environ.get("MINI_NAME", f"api-{os.getpid()}")
BACKEND = os.environ.get("MINI_RATE_BACKEND", "sqlite")
OFFLINE = os.environ.get("MINI_OFFLINE", "1") == "1"
DB_PATH = os.environ.get("MINI_DB", "runs/mini/jobs.db")


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    complexity: str = Field(default="medium", description="low / medium / high：产品里由功能入口决定（例如'生成报告'是 high）")


class State:
    db: SQLiteDB
    queue: SQLiteJobQueue
    ckpt: SQLiteCheckpointer
    shared_buckets: dict[str, SQLiteTokenBucket]
    local_limiter = None
    sync_llm = None


S = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.db = SQLiteDB(DB_PATH)  # 一个连接 + 一个专用线程：数据库调用不阻塞事件循环
    S.queue, S.ckpt = SQLiteJobQueue(S.db), SQLiteCheckpointer(S.db)
    await S.queue.setup()
    await S.ckpt.setup()
    # sqlite：每个套餐一个共享的桶（按租户分 key），所有 API 进程读写的是同一行记录
    S.shared_buckets = {name: SQLiteTokenBucket(S.db, rate=p.refill_rate, capacity=p.capacity) for name, p in PLANS.items()}
    for b in S.shared_buckets.values():
        await b.setup()
    # memory：你在练习里写的 TenantRateLimiter（没写完时是参考答案），桶在这个进程的内存里
    S.local_limiter = ROUTER.TenantRateLimiter(PLANS, TENANT_PLANS, default_plan="free")
    # 同步接口用的模型客户端：整个进程共用一个（真实模型时它带着连接池）
    S.sync_llm = hr_agent.make_llm("pro", OFFLINE)
    yield
    close = getattr(S.sync_llm, "aclose", None)
    if close is not None:
        await close()
    await S.db.close()


app = FastAPI(title="mini deployment API", lifespan=lifespan)


def authenticate(authorization: str | None) -> dict:
    key = (authorization or "").removeprefix("Bearer ").strip()
    identity = API_KEYS.get(key)
    if identity is None:
        raise HTTPException(401, "无效的 API key")
    return identity


async def rate_limit(tenant: str) -> float | None:
    """放行返回 None；被限流返回建议的等待秒数。"""
    if BACKEND == "memory":
        if S.local_limiter.try_acquire(tenant):  # 纯内存、没有 await：在事件循环里天然是原子的
            return None
        return S.local_limiter.retry_after(tenant)
    plan = PLANS[TENANT_PLANS.get(tenant, "free")]
    if await S.shared_buckets[plan.name].try_acquire(tenant):  # 一个 SQLite 写事务里"补充 + 扣减"
        return None
    return 1 / plan.refill_rate  # 桶里不足 1 个令牌时，最多再等 1/rate 秒就有了（保守的上界）


@app.get("/healthz")
async def healthz(response: Response) -> dict:
    response.headers["X-Served-By"] = NAME
    return {"ok": True, "name": NAME, "pid": os.getpid(), "rate_backend": BACKEND}


@app.post("/runs", status_code=202)
async def start_run(body: RunRequest, response: Response, authorization: str | None = Header(None),
                    idempotency_key: str | None = Header(None)) -> dict:
    response.headers["X-Served-By"] = NAME
    ident = authenticate(authorization)
    wait = await rate_limit(ident["tenant"])
    if wait is not None:
        raise HTTPException(429, "Too Many Requests", headers={"Retry-After": str(max(1, math.ceil(wait))),
                                                                 "X-Served-By": NAME})
    task = {"input_tokens": len(body.message) * 2 + 500, "output_tokens": 300, "needs_tools": True,
            "complexity": body.complexity}
    model = ROUTER.choose_model(task, CATALOG)
    payload = {"op": "run", "input": body.message,
               "metadata": {"user_id": ident["user"], "model": model, "roles": ["employee"]}}
    job_id = await S.queue.enqueue("run", payload, tenant_id=ident["tenant"], idempotency_key=idempotency_key)
    run_id = f"job-{job_id}"  # AgentJobHandler 的默认 run_id：由任务决定，接手的 worker 找得到同一个检查点
    return {"run_id": run_id, "status_url": f"/runs/{run_id}", "model": model, "served_by": NAME}


@app.get("/runs/{run_id}")
async def get_run(run_id: str, response: Response, authorization: str | None = Header(None)) -> dict:
    response.headers["X-Served-By"] = NAME
    ident = authenticate(authorization)
    run = await S.ckpt.get_run(run_id)
    if run_id.startswith("sync-"):  # 同步接口跑的 run：没有任务，只有检查点
        if run is None or run["tenant_id"] != ident["tenant"]:
            raise HTTPException(404, "没有这个 run")
        return {"run_id": run_id, "run_status": run["status"], "step": run["state"].get("step"),
                "output": run["state"].get("output"), "updated_at": run["updated_at"]}
    job = await S.queue.get(int(run_id.removeprefix("job-"))) if run_id.startswith("job-") else None
    if job is None or job.tenant_id != ident["tenant"]:
        raise HTTPException(404, "没有这个 run")  # 别的租户的 run 也回 404：不暴露"它存在"这件事
    state = run["state"] if run else {}
    return {
        "run_id": run_id, "job_status": job.status, "attempts": job.attempts, "fence": job.fence,
        "worker_id": job.worker_id, "model": job.payload.get("metadata", {}).get("model"),
        "run_status": run["status"] if run else None, "writer": run["writer"] if run else None,
        "step": state.get("step"), "usage": state.get("usage"), "output": state.get("output"),
    }


@app.post("/runs/sync")
async def run_sync(body: RunRequest, response: Response, authorization: str | None = Header(None),
                   x_request_id: str | None = Header(None)) -> dict:
    """❌ 反面教材：在请求里把 Agent 跑完。客户端超时断开后，这里照样跑到底（白花钱），客户端却以为失败了。"""
    response.headers["X-Served-By"] = NAME
    ident = authenticate(authorization)
    run_id = f"sync-{x_request_id or uuid.uuid4().hex[:8]}"  # 客户端带了请求 ID 就用它：事后还能查到这次运行
    agent = hr_agent.make_agent(S.sync_llm, checkpointer=S.ckpt)
    started = time.time()
    result = await agent.run(body.message, run_id=run_id,
                             metadata={"tenant_id": ident["tenant"], "user_id": ident["user"]})
    return {"run_id": run_id, "status": result.status, "output": result.output, "seconds": round(time.time() - started, 2)}
