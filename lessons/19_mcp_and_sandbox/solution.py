"""第 19 课练习参考答案。先自己做，再来对照。"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

from agentkit import Tool, ToolContext, ToolError, ToolRegistry
from agentkit.types import ToolCall


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（与 exercise.py 里的同名函数一致，保证全进程只有一份）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


mcp_server = _load_sibling("mcp_server")
mcp_client = _load_sibling("mcp_client")
sandbox = _load_sibling("sandbox")

MODERN_VERSIONS = mcp_server.MODERN_VERSIONS
LEGACY_VERSIONS = mcp_server.LEGACY_VERSIONS
SUPPORTED_VERSIONS = mcp_server.SUPPORTED_VERSIONS
META_VERSION = mcp_server.META_VERSION
META_CLIENT_CAPS = mcp_server.META_CLIENT_CAPS
SERVER_INFO = mcp_server.SERVER_INFO
INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32600, -32601, -32602
UNSUPPORTED_PROTOCOL_VERSION = -32022

mcp_tool_def = mcp_server.mcp_tool_def
jsonrpc_result = mcp_server.jsonrpc_result
jsonrpc_error = mcp_server.jsonrpc_error

SandboxResult = sandbox.SandboxResult

RemoteTool = mcp_client.RemoteTool
openai_safe_name = mcp_client.openai_safe_name
result_to_text = mcp_client.result_to_text


# =====================================================================
# (a) handle_request
# =====================================================================


def _valid_id(rid: Any) -> bool:
    return isinstance(rid, (str, int)) and not isinstance(rid, bool)


def handle_request(req: Any, tools: Iterable[Tool]) -> dict | None:
    registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)

    # 1) 结构不合法 → -32600
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        rid = req.get("id") if isinstance(req, dict) else None
        return jsonrpc_error(rid if _valid_id(rid) else None, INVALID_REQUEST, "Invalid Request")

    # 2) 通知：不回复
    if "id" not in req:
        return None
    rid = req["id"]

    # 3) params 必须是对象
    params = req.get("params") or {}
    if not isinstance(params, dict):
        return jsonrpc_error(rid, INVALID_PARAMS, "Invalid params: params 必须是 JSON 对象")

    # 4) 现代请求：版本 + 必填的客户端能力
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(META_VERSION)
    modern = version is not None
    if modern:
        if version not in MODERN_VERSIONS:
            data = {"supported": list(SUPPORTED_VERSIONS), "requested": version}
            return jsonrpc_error(rid, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version", data)
        if not isinstance(meta.get(META_CLIENT_CAPS), dict):
            return jsonrpc_error(rid, INVALID_PARAMS, f"Invalid params: _meta 缺少 {META_CLIENT_CAPS}")

    def ok(payload: dict) -> dict:
        return jsonrpc_result(rid, {"resultType": "complete", **payload} if modern else payload)

    # 5) 分发
    method = req["method"]
    if method == "initialize":
        requested = params.get("protocolVersion")
        chosen = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return jsonrpc_result(
            rid, {"protocolVersion": chosen, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}
        )
    if method == "server/discover":
        return ok({"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}}})
    if method == "tools/list":
        return ok({"tools": [mcp_tool_def(registry.get(n)) for n in registry.names()]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        arguments = {} if arguments is None else arguments
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return jsonrpc_error(rid, INVALID_PARAMS, "Invalid params: 需要字符串 name 和对象 arguments")
        if registry.get(name) is None:
            return jsonrpc_error(rid, INVALID_PARAMS, f"Unknown tool: {name}")
        call = ToolCall(id=str(rid), name=name, arguments=json.dumps(arguments, ensure_ascii=False))
        result = registry.execute(call, ToolContext(run_id="mcp", call_id=str(rid)))
        return ok({"content": [{"type": "text", "text": result.content}], "isError": not result.ok})
    return jsonrpc_error(rid, METHOD_NOT_FOUND, f"Method not found: {method}")


# =====================================================================
# (b) run_with_limits
# =====================================================================


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[输出已截断：原始 {len(text)} 字符]", True


def run_with_limits(code: str, timeout_s: float = 2.0, max_output: int = 2000) -> SandboxResult:
    workdir = tempfile.mkdtemp(prefix="exercise-sandbox-")
    try:
        script = os.path.join(workdir, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        env = {"PATH": "/usr/bin:/bin", "HOME": workdir, "TMPDIR": workdir}
        proc = subprocess.Popen(
            [sys.executable, "-I", script],
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # 新进程组：超时时连孙进程一起杀
        )
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                out, err = proc.communicate(timeout=2)  # 取回超时前的输出
            except subprocess.TimeoutExpired:  # 有进程用 setsid 逃出了进程组、还占着管道
                proc.kill()
                out, err = b"", b""
        stdout, t1 = _cut(out.decode("utf-8", "replace"), max_output)
        stderr, t2 = _cut(err.decode("utf-8", "replace"), max_output)
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            timed_out=timed_out,
            truncated=t1 or t2,
            killed_reason="timeout" if timed_out else None,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# =====================================================================
# (c) tool_from_mcp_schema
# =====================================================================


def _risk_from_annotations(annotations: dict | None) -> str:
    annotations = annotations or {}
    if annotations.get("readOnlyHint", False) is True:
        return "read"
    if annotations.get("destructiveHint", True) is False:  # 默认 true：不写就当作可能有破坏性
        return "write"
    return "dangerous"


def tool_from_mcp_schema(
    schema: dict,
    call_fn: Callable[[str, dict], dict],
    *,
    trusted: bool = False,
    risk_overrides: dict[str, str] | None = None,
    name_prefix: str = "",
) -> Tool:
    remote = schema["name"]
    overrides = risk_overrides or {}
    if remote in overrides:
        risk = overrides[remote]
    elif trusted:
        risk = _risk_from_annotations(schema.get("annotations"))
    else:
        risk = "dangerous"

    def invoke(arguments: dict) -> str:
        result = call_fn(remote, arguments)
        text = result_to_text(result)
        if result.get("isError"):
            raise ToolError(text or f"工具 {remote} 执行失败")
        return text

    return RemoteTool(schema, invoke, name=openai_safe_name(name_prefix + remote), risk=risk)
