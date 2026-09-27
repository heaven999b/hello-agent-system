"""ITBuddy HTTP API（可选组件），演示"异步审批"模式。

安装与启动：
    pip install -e ".[server]"
    .venv/bin/python capstone/server.py          # http://127.0.0.1:8000/docs 查看接口文档

接口：
    POST /runs                      发起一次运行（直到完成，或遇到高危操作而暂停）
    GET  /runs/{run_id}             查询运行状态（轮询）
    GET  /approvals                 审批人查看本租户待审批队列（仅 it_admin）
    POST /runs/{run_id}/approval    审批人批准 / 拒绝，并从检查点恢复运行

异步审批的完整流程（和 CLI 的区别在于：申请人和审批人是两个人、两个请求、可能相隔几小时）：

    员工  ──POST /runs──►  Agent 运行 ──遇到 reset_password──► PauseRun，状态落盘，返回 status=paused
    审批人 ──GET /approvals──► 看到待审批项（申请人、工具、参数）
    审批人 ──POST /runs/{id}/approval──► agent.approve() 从检查点恢复 ──► 执行 / 拒绝 ──► completed
    员工  ──GET /runs/{id}──► 看到最终结果（生产中通常用 WebSocket / IM 推送，而不是轮询）

身份：示例从请求头 X-Tenant-Id / X-User-Id 读取 —— **这只是演示！**
生产中身份必须来自 API 网关验证过签名的 JWT / mTLS 证书（网关把 claims 注入可信请求头，并剥离客户端自带的同名头）。
客户端自报身份 = 任何人都可以说"我是 IT 管理员"。另外注意，就算在演示里，
角色也不从请求头读，而是用 user_id 去员工目录查 —— 权限的唯一可信来源是目录 / IdP。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentkit import Agent, RunResult  # noqa: E402
from agentkit.state import RunState  # noqa: E402
from itbuddy import Backend, ITBuddyAuditLog, build_agent, find_hook  # noqa: E402

INSTALL_HINT = '需要可选依赖 FastAPI：请在仓库根目录执行  pip install -e ".[server]"  （或 .venv/bin/pip install fastapi uvicorn httpx）'

from pydantic import BaseModel, Field  # noqa: E402  （pydantic 是 agentkit 的必需依赖）

try:
    from fastapi import FastAPI, Header, HTTPException
except ImportError:  # 未安装可选依赖时，import 本文件不报错，只有真正创建 app 时才给出友好提示
    FastAPI = None  # type: ignore[assignment,misc]


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict] = Field(default_factory=list, description="之前的对话消息（可选，OpenAI 消息格式）")


class ApprovalRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=500, description="审批意见，会写入审计日志")


def create_app(agent: Agent | None = None, backend: Backend | None = None, runs_dir: str | Path | None = None):
    """应用工厂：测试时注入 ScriptedLLM 组装的 agent 和独立的 backend。"""
    if FastAPI is None:
        raise RuntimeError(INSTALL_HINT)

    backend = backend or Backend()
    agent = agent or build_agent(backend=backend, runs_dir=runs_dir)
    audit = find_hook(agent, ITBuddyAuditLog)
    # 运行索引：run_id -> 所属租户。生产中是一张 runs 表，按 (tenant_id, status) 建索引供审批队列查询。
    run_index: dict[str, str] = {}
    # 每个 run 一把锁：两个审批人同时点"批准"时，只能有一个真正恢复运行，否则高危操作可能被执行两次。
    # 生产中用数据库的行锁 / 乐观锁（版本号）实现。
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    app = FastAPI(title="ITBuddy API", description="企业 IT 服务台 Agent（hello-agent-system 毕业项目）")

    # ------------------------------------------------------------------ 辅助

    def authenticate(tenant_id: str, user_id: str) -> dict:
        identity = backend.identity(tenant_id, user_id)
        if identity is None:
            raise HTTPException(401, "未认证：找不到该用户或账号已停用")
        return identity

    def load_visible_run(run_id: str, identity: dict) -> RunState:
        """只有"本人"或"同租户的 IT 管理员"能看到一个 run。其他人一律 404 —— 连"存在与否"都不透露。"""
        state = agent.checkpointer.load(run_id)
        if state is None:
            raise HTTPException(404, "run 不存在")
        m = state.metadata
        same_tenant = m.get("tenant_id") == identity["tenant_id"]
        is_owner = same_tenant and m.get("user_id") == identity["user_id"]
        if not (is_owner or (same_tenant and "it_admin" in identity["roles"])):
            raise HTTPException(404, "run 不存在")
        return state

    def view(state_or_result: RunState | RunResult) -> dict:
        s = state_or_result
        pending = getattr(s, "pending", None)
        if pending is None and getattr(s, "pending_approval", None) is not None:
            c = s.pending_approval
            pending = {"id": c.id, "name": c.name, "arguments": c.arguments}
        return {
            "run_id": s.run_id,
            "status": s.status,
            "output": s.output,
            "stop_reason": s.stop_reason,
            "pending_approval": pending if s.status == "paused" else None,
            "requester": {"tenant_id": s.metadata.get("tenant_id"), "user_id": s.metadata.get("user_id")},
            "cost_usd": round(s.cost_usd, 6),
        }

    def lock_for(run_id: str) -> threading.Lock:
        with locks_guard:
            return locks.setdefault(run_id, threading.Lock())

    # ------------------------------------------------------------------ 接口
    # 用同步函数（def 而不是 async def）：Agent.run 是阻塞调用，FastAPI 会把同步接口放进线程池执行，
    # 不会卡住事件循环。运行更久的 Agent（分钟级）应放到任务队列（Celery / Temporal），这里直接返回 202。

    @app.post("/runs")
    def start_run(body: RunRequest, x_tenant_id: str = Header(...), x_user_id: str = Header(...)) -> dict:
        identity = authenticate(x_tenant_id, x_user_id)
        # history 里的 system 消息会被 Agent 丢弃（_initial_messages），客户端无法借 history 注入系统提示词
        result = agent.run(body.message, history=body.history, metadata=identity)
        run_index[result.run_id] = identity["tenant_id"]
        return view(result)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, x_tenant_id: str = Header(...), x_user_id: str = Header(...)) -> dict:
        return view(load_visible_run(run_id, authenticate(x_tenant_id, x_user_id)))

    @app.get("/approvals")
    def list_approvals(x_tenant_id: str = Header(...), x_user_id: str = Header(...)) -> list[dict]:
        identity = authenticate(x_tenant_id, x_user_id)
        if "it_admin" not in identity["roles"]:
            raise HTTPException(403, "只有 IT 管理员可以查看审批队列")
        items = []
        for run_id, tenant in list(run_index.items()):
            if tenant != identity["tenant_id"]:
                continue  # 审批队列同样按租户隔离
            state = agent.checkpointer.load(run_id)
            if state and state.status == "paused" and state.pending:
                items.append(view(state))
        return items

    @app.post("/runs/{run_id}/approval")
    def decide(run_id: str, body: ApprovalRequest, x_tenant_id: str = Header(...), x_user_id: str = Header(...)) -> dict:
        approver = authenticate(x_tenant_id, x_user_id)
        with lock_for(run_id):
            state = load_visible_run(run_id, approver)
            if "it_admin" not in approver["roles"]:
                raise HTTPException(403, "只有 IT 管理员可以审批")
            if state.metadata.get("user_id") == approver["user_id"]:
                # 职责分离（四眼原则）：申请人不能审批自己的高危操作，哪怕他是管理员
                raise HTTPException(403, "不能审批自己发起的操作")
            if state.status != "paused" or not state.pending:
                raise HTTPException(409, f"该运行当前状态为 {state.status}，没有待审批的操作")
            if audit:  # 审批决定在恢复执行之前先落审计：即使恢复过程崩溃，"谁在何时批了什么"也有据可查
                audit.record("approval_decision", run_id=run_id, tenant_id=approver["tenant_id"],
                             approver=approver["user_id"], approved=body.approved,
                             tool=state.pending["name"], comment=body.comment)
            # by / comment 写入检查点的 state.approval_log；工具执行时的 tool_call 审计记录会带上 approved_by
            result = agent.approve(run_id, body.approved, by=approver["user_id"], comment=body.comment)
        return view(result)

    return app


if __name__ == "__main__":
    if FastAPI is None:
        print(INSTALL_HINT)
        sys.exit(1)
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
