"""第 19 课练习测试：离线、确定、快（整套 < 10 秒）。

运行：make lesson N=19    或    .venv/bin/python -m pytest lessons/19_mcp_and_sandbox -v

(a) 直接把 JSON-RPC dict 喂给 handle_request，不起子进程；
(b) 会真的起 Python 子进程，但每个用例都控制在 1 秒左右（超时用例用 0.5 秒）；
(c) 用假的 call_fn 代替 MCP 服务器，最后一个用例把远程工具接进真正的 Agent + PermissionPolicy。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Annotated

from pydantic import Field

from agentkit import Agent, PermissionPolicy, ScriptedLLM, ToolError, ToolRegistry, call_tool, reply, tool
from agentkit.testing import load_exercise
from agentkit.types import ToolCall

ex = load_exercise(__file__)

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


# ───────────────────────────── (a) handle_request ─────────────────────────────


@tool
def lookup(city: Annotated[str, Field(description="城市")]) -> dict:
    """查城市信息。"""
    if city == "火星":
        raise ToolError("没有这个城市")
    return {"city": city, "cap": 1100}


@tool
def explode() -> str:
    """总是内部出错的工具。"""
    raise RuntimeError("password=hunter2 at db-internal:5432")  # 内部细节，绝不能传给客户端


@tool(risk="dangerous")
def wipe(target: str) -> str:
    """危险工具。"""
    return f"wiped {target}"


TOOLS = [lookup, explode, wipe]


def rpc(method, params=None, id=1, **extra):
    msg = {"jsonrpc": "2.0", "id": id, "method": method, **extra}
    if params is not None:
        msg["params"] = params
    return msg


def test_legacy_initialize_negotiates_version():
    resp = ex.handle_request(rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}), TOOLS)
    assert resp["id"] == 1 and "error" not in resp
    assert resp["result"]["protocolVersion"] == "2025-06-18"  # 支持的旧版本：原样返回
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"]
    # 不认识的版本：返回自己支持的最新旧版本，由客户端决定要不要断开
    resp = ex.handle_request(rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}}, id="x"), TOOLS)
    assert resp["id"] == "x" and resp["result"]["protocolVersion"] == ex.LEGACY_VERSIONS[0]


def test_notifications_never_get_a_response():
    assert ex.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, TOOLS) is None
    assert ex.handle_request({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}, TOOLS) is None
    # 连不认识的通知也不能回复（哪怕是"方法不存在"的错误）
    assert ex.handle_request({"jsonrpc": "2.0", "method": "no/such/thing"}, TOOLS) is None


def test_tools_list_reuses_agentkit_schema_and_annotations():
    resp = ex.handle_request(rpc("tools/list"), TOOLS)
    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == ["lookup", "explode", "wipe"]
    assert tools[0]["inputSchema"] == lookup.schema()["function"]["parameters"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[2]["annotations"]["destructiveHint"] is True
    assert "resultType" not in resp["result"]  # 旧版请求：不加现代字段


def test_modern_requests_discover_and_result_type():
    resp = ex.handle_request(rpc("server/discover", {"_meta": MODERN_META}), TOOLS)
    assert "2026-07-28" in resp["result"]["supportedVersions"]
    assert resp["result"]["resultType"] == "complete"
    assert "tools" in resp["result"]["capabilities"]
    resp = ex.handle_request(rpc("tools/list", {"_meta": MODERN_META}, id=2), TOOLS)
    assert resp["result"]["resultType"] == "complete" and len(resp["result"]["tools"]) == 3


def test_modern_version_and_capabilities_are_validated():
    bad_version = {**MODERN_META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
    resp = ex.handle_request(rpc("tools/list", {"_meta": bad_version}), TOOLS)
    assert resp["error"]["code"] == -32022
    assert resp["error"]["data"]["requested"] == "2099-01-01"
    assert "2026-07-28" in resp["error"]["data"]["supported"]
    missing_caps = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    resp = ex.handle_request(rpc("tools/list", {"_meta": missing_caps}, id=9), TOOLS)
    assert resp["id"] == 9 and resp["error"]["code"] == -32602


def test_unknown_method_and_invalid_request():
    resp = ex.handle_request(rpc("resources/list", id=7), TOOLS)
    assert resp["id"] == 7 and resp["error"]["code"] == -32601
    resp = ex.handle_request({"id": 8, "method": "tools/list"}, TOOLS)  # 缺 jsonrpc 字段
    assert resp["error"]["code"] == -32600 and resp["id"] == 8
    resp = ex.handle_request(["not", "a", "dict"], TOOLS)
    assert resp["error"]["code"] == -32600 and resp["id"] is None


def test_tools_call_success():
    resp = ex.handle_request(rpc("tools/call", {"name": "lookup", "arguments": {"city": "东京"}}), TOOLS)
    result = resp["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"city": "东京", "cap": 1100}


def test_tool_failures_are_results_with_is_error_not_protocol_errors():
    # 业务错误（ToolError）
    resp = ex.handle_request(rpc("tools/call", {"name": "lookup", "arguments": {"city": "火星"}}), TOOLS)
    assert "error" not in resp and resp["result"]["isError"] is True
    assert "没有这个城市" in resp["result"]["content"][0]["text"]
    # 参数校验错误：2025-11-25 起规范要求作为工具执行错误返回，让模型能自己改
    resp = ex.handle_request(rpc("tools/call", {"name": "lookup", "arguments": {"town": "东京"}}, id=2), TOOLS)
    assert "error" not in resp and resp["result"]["isError"] is True
    # 工具内部异常：isError=true，而且不能把内部细节（密码、内网地址）传出去
    resp = ex.handle_request(rpc("tools/call", {"name": "explode", "arguments": {}}, id=3), TOOLS)
    text = resp["result"]["content"][0]["text"]
    assert resp["result"]["isError"] is True and "hunter2" not in text and "db-internal" not in text


def test_tools_call_protocol_errors():
    resp = ex.handle_request(rpc("tools/call", {"name": "no_such_tool", "arguments": {}}), TOOLS)
    assert resp["error"]["code"] == -32602 and "no_such_tool" in resp["error"]["message"]
    resp = ex.handle_request(rpc("tools/call", {"arguments": {}}, id=2), TOOLS)  # 缺 name
    assert resp["error"]["code"] == -32602
    resp = ex.handle_request(rpc("tools/call", {"name": "lookup", "arguments": "city=东京"}, id=3), TOOLS)
    assert resp["error"]["code"] == -32602


# ───────────────────────────── (b) run_with_limits ─────────────────────────────


def test_sandbox_normal_run():
    r = ex.run_with_limits("print(6 * 7)", timeout_s=5)
    assert r.stdout.strip() == "42"
    assert r.exit_code == 0 and r.timed_out is False and r.truncated is False


def test_sandbox_nonzero_exit_keeps_traceback():
    r = ex.run_with_limits("raise ValueError('boom')", timeout_s=5)
    assert r.exit_code == 1 and "ValueError: boom" in r.stderr and r.timed_out is False
    r = ex.run_with_limits("import sys; print('bye'); sys.exit(3)", timeout_s=5)
    assert r.exit_code == 3 and "bye" in r.stdout


def test_sandbox_timeout_kills_and_keeps_partial_output():
    start = time.monotonic()
    r = ex.run_with_limits("print('tick', flush=True)\nwhile True:\n    pass", timeout_s=0.5)
    assert time.monotonic() - start < 4
    assert r.timed_out is True and r.exit_code != 0
    assert "tick" in r.stdout


def test_sandbox_timeout_kills_the_whole_process_group():
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "print(p.pid, flush=True)\n"
        "p.wait()\n"
    )
    r = ex.run_with_limits(code, timeout_s=0.5)
    assert r.timed_out is True
    pid = int(r.stdout.split()[0])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:  # 孙进程要么已经不存在，要么是等待回收的僵尸（Z）
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)  # 清理现场，别留下孤儿进程
        raise AssertionError("超时后孙进程还活着：只杀了直接子进程，没有杀整个进程组")


def test_sandbox_truncates_output():
    r = ex.run_with_limits("print('x' * 50000)", timeout_s=5, max_output=1000)
    assert r.truncated is True
    assert r.stdout.startswith("x" * 1000) and len(r.stdout) < 1200


def test_sandbox_isolated_env_and_workdir(monkeypatch):
    monkeypatch.setenv("SANDBOX_TEST_SECRET", "s3cr3t-token")
    code = "import os\nprint(os.environ.get('SANDBOX_TEST_SECRET'))\nprint(os.getcwd())\nprint(os.environ.get('HOME'))"
    r = ex.run_with_limits(code, timeout_s=5)
    lines = r.stdout.split()
    assert "s3cr3t-token" not in r.stdout and lines[0] == "None"
    workdir = lines[1]
    assert os.path.realpath(workdir) != os.path.realpath(os.getcwd())
    assert os.path.realpath(lines[2]) == os.path.realpath(workdir)  # HOME 指向临时目录
    assert not os.path.exists(workdir)  # 用完即删


# ───────────────────────────── (c) tool_from_mcp_schema ─────────────────────────────

POLICY_DEF = {
    "name": "get_travel_policy",
    "description": "查询差旅标准。",
    "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    "annotations": {"readOnlyHint": True},
}


class FakeServer:
    def __init__(self, is_error: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.is_error = is_error

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        text = "没有这个城市" if self.is_error else f"{arguments.get('city')}：每晚 1100 元"
        return {"content": [{"type": "text", "text": text}], "isError": self.is_error}


def test_remote_tool_passes_schema_through():
    t = ex.tool_from_mcp_schema(POLICY_DEF, FakeServer(), trusted=True)
    fn = t.schema()["function"]
    assert fn["name"] == "get_travel_policy"
    assert fn["description"] == "查询差旅标准。"
    assert fn["parameters"] == POLICY_DEF["inputSchema"]


def test_risk_from_annotations_when_trusted():
    def risk(annotations):
        d = {**POLICY_DEF, "annotations": annotations}
        return ex.tool_from_mcp_schema(d, FakeServer(), trusted=True).risk

    assert risk({"readOnlyHint": True}) == "read"
    assert risk({"readOnlyHint": False, "destructiveHint": False}) == "write"
    assert risk({"readOnlyHint": False, "destructiveHint": True}) == "dangerous"
    assert risk({}) == "dangerous"  # destructiveHint 默认是 true
    assert risk(None) == "dangerous"


def test_untrusted_annotations_are_ignored_but_overrides_win():
    t = ex.tool_from_mcp_schema(POLICY_DEF, FakeServer())  # 默认不信任：自称只读也不算数
    assert t.risk == "dangerous"
    t = ex.tool_from_mcp_schema(POLICY_DEF, FakeServer(), risk_overrides={"get_travel_policy": "read"})
    assert t.risk == "read"
    liar = {**POLICY_DEF, "name": "delete_all", "annotations": {"readOnlyHint": True}}
    t = ex.tool_from_mcp_schema(liar, FakeServer(), trusted=True, risk_overrides={"delete_all": "dangerous"})
    assert t.risk == "dangerous"  # 你自己的审查结论优先于服务器的自我声明


def test_calls_are_forwarded_and_is_error_becomes_tool_error():
    server = FakeServer()
    t = ex.tool_from_mcp_schema(POLICY_DEF, server, trusted=True)
    registry = ToolRegistry([t])
    ok = registry.execute(ToolCall(id="c1", name="get_travel_policy", arguments='{"city": "东京"}'))
    assert ok.ok and "每晚 1100 元" in ok.content
    assert server.calls == [("get_travel_policy", {"city": "东京"})]

    failing = ex.tool_from_mcp_schema(POLICY_DEF, FakeServer(is_error=True), trusted=True)
    bad = ToolRegistry([failing]).execute(ToolCall(id="c2", name="get_travel_policy", arguments='{"city": "火星"}'))
    assert bad.ok is False and bad.error_type == "tool_error" and "没有这个城市" in bad.content


def test_names_are_made_safe_for_openai_but_calls_use_remote_name():
    server = FakeServer()
    d = {**POLICY_DEF, "name": "travel.policy.get"}
    t = ex.tool_from_mcp_schema(d, server, trusted=True, name_prefix="hr__")
    assert t.name == "hr__travel_policy_get"
    ToolRegistry([t]).execute(ToolCall(id="c1", name=t.name, arguments='{"city": "上海"}'))
    assert server.calls[0][0] == "travel.policy.get"


def test_agent_uses_remote_tool_and_untrusted_tool_needs_approval():
    server = FakeServer()
    remote = ex.tool_from_mcp_schema(POLICY_DEF, server)  # 不信任 → dangerous
    approvals: list[str] = []
    policy = PermissionPolicy(approver=lambda call, state: approvals.append(call.name) or True)
    llm = ScriptedLLM([call_tool("get_travel_policy", city="东京"), reply("东京每晚上限 1100 元。")])
    result = Agent(llm, [remote], hooks=[policy]).run("东京住宿标准是多少？")
    assert result.ok and result.output == "东京每晚上限 1100 元。"
    assert approvals == ["get_travel_policy"]  # 危险等级 → 走了审批
    assert server.calls == [("get_travel_policy", {"city": "东京"})]
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert "每晚 1100 元" in tool_msgs[0]["content"]
