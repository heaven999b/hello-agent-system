"""server.py 的离线测试：异步审批的完整流程 + 访问控制。未安装 FastAPI 时自动跳过。

安装：pip install -e ".[server]"
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # TestClient 依赖 httpx

from fastapi.testclient import TestClient  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agentkit import ScriptedLLM, call_tool, reply  # noqa: E402
from itbuddy import Backend, build_agent  # noqa: E402


def _load_server():
    spec = importlib.util.spec_from_file_location("itbuddy_server", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["itbuddy_server"] = module
    spec.loader.exec_module(module)
    return module


def headers(tenant: str, user: str) -> dict:
    return {"X-Tenant-Id": tenant, "X-User-Id": user}


@pytest.fixture
def setup(tmp_path):
    backend = Backend()
    llm = ScriptedLLM([call_tool("reset_password", reason="忘记密码"), reply("重置链接已发送到您的企业邮箱")])
    agent = build_agent(llm, backend=backend, runs_dir=tmp_path)
    client = TestClient(_load_server().create_app(agent=agent, backend=backend))
    return client, backend


def test_async_approval_flow(setup, tmp_path):
    client, backend = setup
    # 1) 员工发起：遇到高危操作 → 暂停
    r = client.post("/runs", json={"message": "我忘记密码了，帮我重置"}, headers=headers("acme", "alice"))
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "paused" and run["pending_approval"]["name"] == "reset_password"
    run_id = run["run_id"]

    # 2) 审批人查看本租户的审批队列
    queue = client.get("/approvals", headers=headers("acme", "frank")).json()
    assert [item["run_id"] for item in queue] == [run_id]
    assert client.get("/approvals", headers=headers("globex", "erin")).json() == []  # 别家公司的管理员看不到

    # 3) 审批人批准 → 从检查点恢复 → 完成
    r = client.post(f"/runs/{run_id}/approval", json={"approved": True, "comment": "已电话核实本人"},
                    headers=headers("acme", "frank"))
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert [x.target_user_id for x in backend.password_resets] == ["alice"]
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(x["event"] == "tool_call" and x["approved_by"] == "frank" for x in audit)  # 谁批的，进入审计

    # 4) 员工轮询结果
    r = client.get(f"/runs/{run_id}", headers=headers("acme", "alice"))
    assert r.json()["status"] == "completed" and "重置链接" in r.json()["output"]

    # 5) 重复审批 → 409（已经不是 paused 状态）
    r = client.post(f"/runs/{run_id}/approval", json={"approved": True}, headers=headers("acme", "frank"))
    assert r.status_code == 409


def test_access_control(setup):
    client, backend = setup
    run_id = client.post("/runs", json={"message": "重置我的密码"}, headers=headers("acme", "alice")).json()["run_id"]

    assert client.post("/runs", json={"message": "hi"}, headers=headers("acme", "mallory")).status_code == 401
    # 跨租户：连"存在与否"都不透露，一律 404
    assert client.get(f"/runs/{run_id}", headers=headers("globex", "carol")).status_code == 404
    # 同租户的其他普通员工：404
    assert client.get(f"/runs/{run_id}", headers=headers("acme", "dave")).status_code == 404
    # 普通员工不能审批（dave 看不到这个 run，所以也是 404，不透露存在性）
    assert client.post(f"/runs/{run_id}/approval", json={"approved": True},
                       headers=headers("acme", "dave")).status_code == 404
    # 申请人不能审批自己（职责分离）—— alice 不是管理员，先被 403 拦下
    assert client.post(f"/runs/{run_id}/approval", json={"approved": True},
                       headers=headers("acme", "alice")).status_code == 403
    # 员工不能看审批队列
    assert client.get("/approvals", headers=headers("acme", "alice")).status_code == 403
    assert backend.password_resets == []


def test_admin_cannot_approve_own_request(tmp_path):
    backend = Backend()
    llm = ScriptedLLM([call_tool("reset_password", reason="账号被锁", target_user_id="dave")])
    agent = build_agent(llm, backend=backend, runs_dir=tmp_path)
    client = TestClient(_load_server().create_app(agent=agent, backend=backend))
    run_id = client.post("/runs", json={"message": "重置 dave 的密码"}, headers=headers("acme", "bob")).json()["run_id"]
    r = client.post(f"/runs/{run_id}/approval", json={"approved": True}, headers=headers("acme", "bob"))
    assert r.status_code == 403 and "自己" in r.json()["detail"]
    assert backend.password_resets == []
