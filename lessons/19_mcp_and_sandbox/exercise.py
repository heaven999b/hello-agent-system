"""第 19 课练习：MCP 服务端分发、进程级沙箱、远程工具适配。

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=19
    # 或者：.venv/bin/python -m pytest lessons/19_mcp_and_sandbox -v

三道题：
  (a) handle_request        MCP 服务端的 JSON-RPC 分发：协议错误 vs 工具执行错误、通知不回复、两代协议
  (b) run_with_limits       进程级沙箱：临时目录、最小环境变量、超时杀整个进程组、输出截断
  (c) tool_from_mcp_schema  把远程 MCP 工具定义变成 agentkit Tool，风险等级由"你"决定而不是由服务器决定

同目录的 mcp_server.py / mcp_client.py / sandbox.py 是完整的参考实现（demo 用的就是它们）。
建议先自己写，卡住了再去对照。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

from agentkit import Tool, ToolContext, ToolError, ToolRegistry  # noqa: F401  完成 TODO 时会用到
from agentkit.types import ToolCall  # noqa: F401


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "19_mcp_and_sandbox__mcp_server"，避免和其他课的同名模块冲突
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


mcp_server = _load_sibling("mcp_server")
mcp_client = _load_sibling("mcp_client")
sandbox = _load_sibling("sandbox")

# ── 可以直接使用的常量和小工具（不需要修改）─────────────────────────────
MODERN_VERSIONS = mcp_server.MODERN_VERSIONS  # ("2026-07-28",)
LEGACY_VERSIONS = mcp_server.LEGACY_VERSIONS  # ("2025-11-25", "2025-06-18", ...)，新的在前
SUPPORTED_VERSIONS = mcp_server.SUPPORTED_VERSIONS
META_VERSION = mcp_server.META_VERSION  # "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = mcp_server.META_CLIENT_CAPS  # "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = mcp_server.SERVER_INFO
INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32600, -32601, -32602
UNSUPPORTED_PROTOCOL_VERSION = -32022

mcp_tool_def = mcp_server.mcp_tool_def  # agentkit Tool → tools/list 里的一项
jsonrpc_result = mcp_server.jsonrpc_result  # jsonrpc_result(id, result_dict)
jsonrpc_error = mcp_server.jsonrpc_error  # jsonrpc_error(id, code, message, data=None)

SandboxResult = sandbox.SandboxResult  # stdout / stderr / exit_code / timed_out / truncated ...

RemoteTool = mcp_client.RemoteTool  # RemoteTool(definition, invoke, *, name, risk, timeout_s=30)
openai_safe_name = mcp_client.openai_safe_name  # "admin.tools.list" → "admin_tools_list"
result_to_text = mcp_client.result_to_text  # CallToolResult → 给模型看的文字


# =====================================================================
# (a) handle_request：MCP 服务端的 JSON-RPC 分发
# =====================================================================


def handle_request(req: Any, tools: Iterable[Tool]) -> dict | None:
    """处理一条 JSON-RPC 消息，返回响应 dict；不该回复时返回 None。

    规则（按顺序检查）：
    1. req 不是 dict、或 req["jsonrpc"] != "2.0"、或 method 不是字符串 → 错误 -32600
       （响应的 id：能从 req 里读到合法 id 就用它，否则用 None）。
    2. 没有 "id" 字段的消息是**通知**：无论什么方法，一律返回 None（规范：接收方绝不能回复通知）。
    3. params 缺省视为 {}；params 不是对象 → -32602。
    4. 现代请求（2026-07-28）：params["_meta"] 里有 META_VERSION 字段。
       - 版本不在 MODERN_VERSIONS → -32022，
         data = {"supported": list(SUPPORTED_VERSIONS), "requested": 请求的版本}
       - _meta 缺少 META_CLIENT_CAPS（或它不是 dict）→ -32602
       - 现代请求的**成功**结果里要带 "resultType": "complete"
       没有 META_VERSION 的请求按旧版处理（结果里不加 resultType）。
    5. 方法：
       - "initialize"（旧版握手）→ {"protocolVersion": v, "capabilities": {"tools": {...}}, "serverInfo": SERVER_INFO}
         其中 v：请求的 params["protocolVersion"] 在 LEGACY_VERSIONS 里就原样返回，否则返回 LEGACY_VERSIONS[0]
       - "server/discover"（现代探测）→ {"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}}}
       - "tools/list" → {"tools": [mcp_tool_def(t) for t in tools]}（按注册顺序）
       - "tools/call" →
           · params["name"] 不是字符串、或 params["arguments"]（缺省 {}）不是对象 → -32602（协议错误）
           · 工具不存在 → -32602，message 里包含工具名（协议错误：模型没法靠改参数修好它）
           · 否则用 ToolRegistry.execute(ToolCall(...)) 执行，返回
             {"content": [{"type": "text", "text": 结果文字}], "isError": not result.ok}
             注意：参数校验失败、ToolError、工具内部异常都是**工具执行错误**（isError: true），不是协议错误。
       - 其他方法 → -32601

    提示：
    - ToolRegistry(tools) 可以直接用 list 构造；registry.get(name) 找不到返回 None。
    - ToolCall 的 arguments 是 JSON **字符串**：json.dumps(arguments)。
    - 用 jsonrpc_result / jsonrpc_error 构造响应。
    """
    raise NotImplementedError("TODO: 练习 (a) —— MCP 服务端 JSON-RPC 分发")


# =====================================================================
# (b) run_with_limits：进程级沙箱
# =====================================================================


def run_with_limits(code: str, timeout_s: float = 2.0, max_output: int = 2000) -> SandboxResult:
    """在一次性子进程里执行 Python 代码 code，返回 SandboxResult。

    要求：
    - 在一个新建的临时目录里运行（cwd = 临时目录），结束后删掉这个目录；
    - 环境变量只给最小集合：PATH，以及把 HOME、TMPDIR 指向临时目录 —— 不能继承父进程的环境变量
      （父进程里可能有 LLM_API_KEY）；
    - 用 [sys.executable, "-I", 脚本路径] 运行（-I：隔离模式，忽略 PYTHON* 环境变量）；
    - 超过 timeout_s 秒：杀掉**整个进程组**（代码自己启动的子进程也要一起死），
      返回 timed_out=True，exit_code 为非 0（被信号杀死时 Popen 给的是负数），
      并且保留超时前已经输出的内容；
    - 非零退出码如实返回，stderr 原样保留（模型要靠 Traceback 改代码）；
    - stdout / stderr 任意一个超过 max_output 个字符：截断到 max_output 个字符，后面追加一行说明，
      truncated=True。

    提示：
    - subprocess.Popen(..., start_new_session=True) 让子进程成为新进程组的组长，
      之后 os.killpg(proc.pid, signal.SIGKILL) 就能整组杀掉；
    - proc.communicate(timeout=...) 超时会抛 subprocess.TimeoutExpired，杀掉进程组后
      再调用一次 proc.communicate() 取回已经输出的内容（给它也加个超时，防止有进程逃出了进程组）；
    - 为什么不直接用 subprocess.run(timeout=...)？它超时只杀直接子进程，孙进程会变成孤儿继续跑。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 临时目录 + 最小环境 + 超时杀进程组 + 输出截断")


# =====================================================================
# (c) tool_from_mcp_schema：远程工具 → agentkit Tool
# =====================================================================


def tool_from_mcp_schema(
    schema: dict,
    call_fn: Callable[[str, dict], dict],
    *,
    trusted: bool = False,
    risk_overrides: dict[str, str] | None = None,
    name_prefix: str = "",
) -> Tool:
    """把 tools/list 返回的一项工具定义 schema 包装成 agentkit Tool。

    参数：
      schema:  {"name", "description", "inputSchema", "annotations"?}
      call_fn: call_fn(远程工具名, 参数 dict) → CallToolResult dict（{"content": [...], "isError": bool}）
      trusted: 是否信任这台服务器的注解
      risk_overrides: {远程工具名: "read" / "write" / "dangerous"}，你自己审查后定的等级，优先级最高
      name_prefix: 给模型看的名字前缀（多个服务器可能都有叫 search 的工具）

    风险等级 risk 的决定规则：
      1. 远程工具名在 risk_overrides 里 → 用它；
      2. 否则 trusted=True → 看注解：readOnlyHint 为 True → "read"；
         否则 destructiveHint 显式为 False → "write"；否则 → "dangerous"
         （注意 MCP 的默认值：readOnlyHint 默认 false，destructiveHint 默认 **true**）；
      3. 否则（不信任的服务器）→ "dangerous"，不管它的注解怎么说。

    其他要求：
      - 给模型看的名字 = openai_safe_name(name_prefix + 远程工具名)；调用服务器时用**原始**远程工具名；
      - 模型调用时：把参数 dict 原样交给 call_fn；返回结果用 result_to_text 变成文字；
        如果结果 isError 为 true，抛 ToolError(文字) —— ToolRegistry 会把它变成"错误：..."反馈给模型。

    提示：用现成的 RemoteTool(schema, invoke, name=..., risk=...)，你只需要写 invoke(arguments) -> str。
    """
    raise NotImplementedError("TODO: 练习 (c) —— 风险等级由客户端决定 + isError 变 ToolError")


if __name__ == "__main__":
    # 随手试试你的 handle_request（写完 (a) 之后）
    import json

    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    print(json.dumps(handle_request(req, mcp_server.DEMO_TOOLS), ensure_ascii=False, indent=2)[:800])
