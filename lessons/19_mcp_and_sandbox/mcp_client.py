"""第 19 课：一个零依赖、手写协议的最小 MCP stdio 客户端。

    tools = mcp_tools([sys.executable, "lessons/19_mcp_and_sandbox/mcp_server.py"])
    agent = Agent(llm, tools)          # MCP 服务器上的工具，和本地 @tool 一样用

它做四件事：
  1. 用子进程启动服务器，按行收发 JSON-RPC，用 id 把响应和请求配对（后台线程读 stdout）
  2. connect()：按规范的 stdio 向后兼容流程协商"时代"——先发 server/discover 探测，
     是现代服务器（2026-07-28）就直接用；否则回退到旧版 initialize 握手
  3. list_tools() / call_tool()
  4. tool_from_mcp_schema()：把远程工具定义包装成 agentkit Tool，风险等级按"是否信任这台服务器"决定

客户端和服务器是两个独立程序，所以这里故意不 import mcp_server.py，协议常量各写一份。
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import os
import queue
import re
import subprocess
import threading
from collections import deque
from typing import Any, Callable, Iterable, Sequence

from agentkit import Tool, ToolError

MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-11-25"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
UNSUPPORTED_PROTOCOL_VERSION = -32022
CLIENT_INFO = {"name": "agentkit-mini-client", "version": "0.1.0"}
# 启动服务器子进程时默认只传这几个环境变量（和官方 Python SDK 在 POSIX 上的默认值一致）。
# 如果直接继承父进程的全部环境变量，你的 LLM_API_KEY、云厂商凭证会被交给每一个 MCP 服务器。
INHERITED_ENV_VARS = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")


def default_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in INHERITED_ENV_VARS if k in os.environ}
    env.update(extra or {})
    return env


class MCPError(Exception):
    """对方返回了 JSON-RPC 协议错误（注意：工具执行失败不走这里，而是 isError: true 的正常结果）。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.data = code, message, data


class StdioMCPClient:
    """通过 stdio 连接一个 MCP 服务器。

    mode: "auto"（先探测现代版，失败回退旧版，推荐）/ "legacy"（直接 initialize 握手）/ "modern"
    env: 额外传给服务器的环境变量，叠加在 default_environment() 之上（不会继承你的全部环境变量）
    on_message: 回调 on_message(direction, message)，direction 为 "→" 或 "←"，demo 用它打印报文。
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        mode: str = "auto",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 20.0,
        probe_timeout_s: float = 5.0,
        on_message: Callable[[str, dict], None] | None = None,
    ):
        self.command = list(command)
        self.mode = mode
        self.env, self.cwd = env, cwd
        self.timeout_s, self.probe_timeout_s = timeout_s, probe_timeout_s
        self.on_message = on_message
        self.era: str | None = None  # 连接后为 "modern" 或 "legacy"
        self.protocol_version: str | None = None
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.instructions: str | None = None
        self.stderr_lines: deque[str] = deque(maxlen=200)  # 服务器日志：规范说客户端可以捕获、转发或忽略
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[Any, queue.Queue] = {}
        self._lock = threading.Lock()  # 保护 stdin 写入和 _pending

    # ───────────── 进程与收发 ─────────────

    def start(self) -> "StdioMCPClient":
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=default_environment(self.env),
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return self

    def _send(self, msg: dict) -> None:
        if self.on_message:
            self.on_message("→", msg)
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._lock:
            assert self._proc and self._proc.stdin, "先调用 start()"
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.stderr_lines.append(f"[client] 服务器往 stdout 写了非 JSON 内容：{raw[:200]!r}")
                continue
            if self.on_message:
                self.on_message("←", msg)
            if "method" in msg and "id" in msg:  # 服务器发来的请求（旧版可能发 ping）
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": f"Method not found: {msg['method']}"}
                self._send(reply)
            elif "id" in msg:  # 响应：按 id 交给等待它的那个请求
                with self._lock:
                    waiter = self._pending.pop(msg["id"], None)
                if waiter is not None:
                    waiter.put(msg)
            # 其余是服务器通知（progress / log），这里简单忽略
        with self._lock:  # 服务器退出：唤醒所有还在等的请求
            for waiter in self._pending.values():
                waiter.put(None)
            self._pending.clear()

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in self._proc.stderr:
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    def _meta(self) -> dict:
        """现代版每个请求都要带的"自我介绍"：协议版本 + 客户端能力（必填）+ 客户端身份（建议）。"""
        return {META_VERSION: self.protocol_version or MODERN_VERSION, META_CLIENT_INFO: CLIENT_INFO, META_CLIENT_CAPS: {}}

    def request(self, method: str, params: dict | None = None, *, timeout_s: float | None = None, modern: bool | None = None) -> dict:
        """发一个请求并等待对应 id 的响应。协议错误抛 MCPError，超时抛 TimeoutError。"""
        params = dict(params or {})
        use_modern = self.era == "modern" if modern is None else modern
        if use_modern:
            params["_meta"] = {**params.get("_meta", {}), **self._meta()}
        rid = next(self._ids)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except OSError as e:  # 服务器已经退出，管道断了
            raise MCPError(-32000, f"无法写入 MCP 服务器（{e}）。最近的服务器日志：{list(self.stderr_lines)[-3:]}") from e
        try:
            msg = waiter.get(timeout=timeout_s or self.timeout_s)
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            # 规范：超时后应发取消通知，告诉服务器别再为这个请求干活
            self.notify("notifications/cancelled", {"requestId": rid, "reason": "timeout"})
            raise TimeoutError(f"MCP 请求 {method} 超时（>{timeout_s or self.timeout_s}s）") from None
        if msg is None:
            raise MCPError(-32000, f"MCP 服务器进程已退出。最近的服务器日志：{list(self.stderr_lines)[-3:]}")
        if "error" in msg:
            err = msg["error"]
            raise MCPError(err.get("code", 0), err.get("message", ""), err.get("data"))
        return msg.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._send(msg)

    # ───────────── 连接：协商用哪一代协议 ─────────────

    def connect(self) -> "StdioMCPClient":
        if self._proc is None:
            self.start()
        if self.mode in ("auto", "modern"):
            try:
                result = self.request("server/discover", timeout_s=self.probe_timeout_s, modern=True)
            except MCPError as e:
                supported = (e.data or {}).get("supported", []) if isinstance(e.data, dict) else []
                if e.code == UNSUPPORTED_PROTOCOL_VERSION and not any(v in LEGACY_VERSIONS for v in supported):
                    raise  # 现代服务器、但和我们没有共同版本：回退也没用
                if self.mode == "modern":
                    raise
            except TimeoutError:
                if self.mode == "modern":
                    raise
            else:
                if MODERN_VERSION in result.get("supportedVersions", []):
                    self.era, self.protocol_version = "modern", MODERN_VERSION
                    self.server_capabilities = result.get("capabilities", {})
                    self.server_info = result.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
                    self.instructions = result.get("instructions")
                    return self
        # 旧版：initialize → 检查对方选的版本 → notifications/initialized
        result = self.request(
            "initialize",
            {"protocolVersion": LEGACY_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            modern=False,
        )
        if result.get("protocolVersion") not in LEGACY_VERSIONS:
            self.close()
            raise MCPError(-32602, f"服务器选择了我们不支持的协议版本 {result.get('protocolVersion')!r}")
        self.era, self.protocol_version = "legacy", result["protocolVersion"]
        self.server_capabilities = result.get("capabilities", {})
        self.server_info = result.get("serverInfo", {})
        self.instructions = result.get("instructions")
        self.notify("notifications/initialized")
        return self

    # ───────────── 工具 ─────────────

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:  # tools/list 支持分页：有 nextCursor 就继续取
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    # ───────────── 关闭 ─────────────

    def close(self) -> None:
        """规范推荐的 stdio 关机顺序：关 stdin → 等它退出 → 还不退就 SIGTERM → 再不退 SIGKILL。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def __enter__(self) -> "StdioMCPClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()


# ───────────────────────────── 远程工具 → agentkit Tool ─────────────────────────────


def openai_safe_name(name: str) -> str:
    """MCP 工具名允许点号、最长 128 字符；OpenAI 兼容接口的函数名只允许 [a-zA-Z0-9_-]、最长 64。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return safe[:64] or "tool"


class RemoteTool(Tool):
    """远程 MCP 工具在 agentkit 里的"替身"。

    - schema()：把服务器给的 inputSchema 原样交给模型（不用本地函数签名生成）；
    - parse_arguments()：只检查"是不是 JSON 对象"，真正的参数校验交给服务器（规范要求服务器必须校验）；
    - fn：把调用转发给 invoke(arguments)。
    """

    def __init__(
        self,
        definition: dict,
        invoke: Callable[[dict], str],
        *,
        name: str,
        risk: str,
        timeout_s: float = 30.0,
        max_output_chars: int = 4000,
    ):
        description = (definition.get("description") or definition.get("title") or f"MCP 工具 {definition['name']}").strip()

        def _placeholder() -> None:  # 只为复用 Tool.__init__ 里的通用初始化
            ...

        super().__init__(
            _placeholder, name=name, description=description, risk=risk, timeout_s=timeout_s, max_output_chars=max_output_chars
        )
        self.fn = lambda **arguments: invoke(arguments)
        self.remote_name = definition["name"]
        self.definition = definition
        self.input_schema = definition.get("inputSchema") or {"type": "object"}
        self.annotations = definition.get("annotations") or {}

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.input_schema}}

    def parse_arguments(self, arguments: str) -> tuple[dict | None, str | None]:
        try:
            raw = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return None, f"错误：参数不是合法的 JSON 对象（{e}）。请重新生成参数。"
        if not isinstance(raw, dict):
            return None, "错误：参数必须是 JSON 对象。请重新生成参数。"
        return raw, None


def risk_from_annotations(annotations: dict | None) -> str:
    """按 MCP 注解推断风险等级。注意默认值：readOnlyHint 默认 false，destructiveHint 默认 true。"""
    annotations = annotations or {}
    if annotations.get("readOnlyHint", False) is True:
        return "read"
    if annotations.get("destructiveHint", True) is False:
        return "write"
    return "dangerous"


def result_to_text(result: dict) -> str:
    """把 CallToolResult 的 content 块拼成给模型看的文字。非文字内容只留占位说明。"""
    parts: list[str] = []
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "resource":
            res = block.get("resource", {})
            parts.append(res.get("text") or f"[嵌入资源 {res.get('uri', '?')}]")
        elif kind == "resource_link":
            parts.append(f"[资源链接 {block.get('uri', '?')}]")
        else:
            parts.append(f"[{kind} 内容（{block.get('mimeType', '未知类型')}），已省略]")
    if not parts and "structuredContent" in result:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)


def tool_from_mcp_schema(
    schema: dict,
    call_fn: Callable[[str, dict], dict],
    *,
    trusted: bool = False,
    risk_overrides: dict[str, str] | None = None,
    name_prefix: str = "",
    timeout_s: float = 30.0,
) -> Tool:
    """把 tools/list 里的一项包装成 agentkit Tool。

    风险等级的决定顺序（安全边界在你这边，不在服务器那边）：
      1. risk_overrides 里有 → 用它（运维审查过这个工具后自己定的等级）；
      2. trusted=True → 按注解推断（readOnlyHint / destructiveHint）；
      3. 否则 → "dangerous"：不信任的服务器，它说自己"只读"也不算数，每次调用都要审批。
    call_fn(远程工具名, 参数) 返回 CallToolResult；isError=true 时抛 ToolError，
    这样 ToolRegistry 会把它变成模型能看懂的"错误：..."观察（第 03 课的"错误即观察"）。
    """
    remote = schema["name"]
    overrides = risk_overrides or {}
    if remote in overrides:
        risk = overrides[remote]
    elif trusted:
        risk = risk_from_annotations(schema.get("annotations"))
    else:
        risk = "dangerous"

    def invoke(arguments: dict) -> str:
        result = call_fn(remote, arguments)
        text = result_to_text(result)
        if result.get("isError"):
            raise ToolError(text or f"工具 {remote} 执行失败")
        return text

    return RemoteTool(schema, invoke, name=openai_safe_name(name_prefix + remote), risk=risk, timeout_s=timeout_s)


def tool_fingerprints(definitions: Iterable[dict]) -> dict[str, str]:
    """给每个工具定义算一个指纹（名字 + 描述 + 参数 + 注解）。

    防 rug pull 的思路：第一次审查通过时记下指纹；以后每次连接都重新计算，
    有变化就拒绝加载、要求重新审查 —— 就像给依赖包锁版本、校验哈希。
    """
    fps = {}
    for d in definitions:
        canonical = json.dumps(
            {k: d.get(k) for k in ("name", "description", "inputSchema", "annotations")}, sort_keys=True, ensure_ascii=False
        )
        fps[d["name"]] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return fps


def changed_tools(pinned: dict[str, str], current: dict[str, str]) -> list[str]:
    """和锁定的指纹比，返回新增 / 删除 / 被修改的工具名。"""
    return sorted(n for n in set(pinned) | set(current) if pinned.get(n) != current.get(n))


_OPEN_CLIENTS: list[StdioMCPClient] = []


@atexit.register
def _close_all() -> None:
    for c in _OPEN_CLIENTS:
        c.close()


def mcp_tools(
    command: Sequence[str],
    *,
    include: Iterable[str] | None = None,
    trusted: bool = False,
    risk_overrides: dict[str, str] | None = None,
    pinned: dict[str, str] | None = None,
    name_prefix: str = "",
    **client_kwargs,
) -> list[Tool]:
    """启动一个 MCP 服务器，把它的工具包装成 agentkit Tool 列表，可以直接交给 Agent。

    include: 只导入这些工具（最小权限：接入一个服务器 ≠ 把它的全部工具都给模型）
    pinned:  锁定的工具指纹；服务器上的定义变了就拒绝加载（防 rug pull）
    客户端进程在解释器退出时自动关闭；也可以用 tools[0].client.close() 提前关。
    """
    client = StdioMCPClient(command, **client_kwargs).connect()
    _OPEN_CLIENTS.append(client)
    definitions = client.list_tools()
    if include is not None:
        wanted = set(include)
        definitions = [d for d in definitions if d["name"] in wanted]
    if pinned is not None:
        changed = changed_tools({n: pinned[n] for n in pinned if include is None or n in set(include)}, tool_fingerprints(definitions))
        if changed:
            client.close()
            raise PermissionError(f"MCP 工具定义与审查时不一致（可能是 rug pull）：{changed}。请重新审查后再更新指纹。")
    tools = []
    for d in definitions:
        t = tool_from_mcp_schema(d, client.call_tool, trusted=trusted, risk_overrides=risk_overrides, name_prefix=name_prefix)
        t.client = client  # type: ignore[attr-defined]
        tools.append(t)
    return tools
