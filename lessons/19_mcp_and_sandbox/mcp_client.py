"""第 19 课：一个零依赖、手写协议的最小 MCP stdio 客户端（asyncio）。

    async with StdioMCPClient([sys.executable, "lessons/19_mcp_and_sandbox/mcp_server.py"]) as client:
        tools = await mcp_tools(client)      # MCP 服务器上的工具，包装成 agentkit 的 async 工具
        agent = Agent(llm, tools)            # 和本地 @tool 一样用
        await agent.run("……")
    # 离开 async with：关 stdin → 等退出 → SIGTERM → SIGKILL，子进程一定被收掉

它做四件事：
  1. 用 asyncio 子进程启动服务器，按行收发 JSON-RPC。一个后台任务读 stdout，按 id 把响应交给等待它的请求：
     多个请求可以同时在路上（Agent 同一轮的只读工具并发执行），服务器也可以乱序回复
  2. connect()：按规范的 stdio 向后兼容流程协商"时代"——先发 server/discover 探测，
     是现代服务器（2026-07-28）就直接用；否则回退到旧版 initialize 握手
  3. list_tools() / call_tool()：超时或调用方取消时，发 notifications/cancelled 告诉服务器别再干了
  4. tool_from_mcp_schema()：把远程工具定义包装成 agentkit Tool（async），风险等级按"是否信任这台服务器"决定

客户端和服务器是两个独立程序，所以这里故意不 import mcp_server.py，协议常量各写一份。
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import re
from collections import deque
from typing import Any, Awaitable, Callable, Iterable, Sequence

from agentkit import Tool, ToolError, wait_for

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
# 一条消息（一行）最多多大。asyncio 的 StreamReader 默认只有 64 KiB，一个大的工具结果就会超
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


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
    """通过 stdio 连接一个 MCP 服务器。推荐用 `async with StdioMCPClient(...) as client:`，离开时一定会关掉子进程。

    mode: "auto"（先探测现代版，失败回退旧版，推荐）/ "legacy"（直接 initialize 握手）/ "modern"
    env: 额外传给服务器的环境变量，叠加在 default_environment() 之上（不会继承你的全部环境变量）
    on_message: 回调 on_message(direction, message)，direction 为 "→" 或 "←"，demo 用它打印报文。
    close_timeout_s: 关闭时每一步（等它自己退出、SIGTERM 之后）最多等多久，然后升级到下一步。
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
        close_timeout_s: float = 2.0,
        on_message: Callable[[str, dict], None] | None = None,
    ):
        self.command = list(command)
        self.mode = mode
        self.env, self.cwd = env, cwd
        self.timeout_s, self.probe_timeout_s, self.close_timeout_s = timeout_s, probe_timeout_s, close_timeout_s
        self.on_message = on_message
        self.era: str | None = None  # 连接后为 "modern" 或 "legacy"
        self.protocol_version: str | None = None
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.instructions: str | None = None
        self.stderr_lines: deque[str] = deque(maxlen=200)  # 服务器日志：规范说客户端可以捕获、转发或忽略
        self.returncode: int | None = None  # 关闭后记下服务器进程的退出码（被信号杀死时是负数）
        self.max_in_flight = 0  # 同一时刻最多有几个请求在等响应：并发真实发生的证据
        self._proc: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task] = []
        self._ids = itertools.count(1)
        self._pending: dict[Any, asyncio.Future] = {}  # 请求 id → 等待响应的 Future
        self._alive = False  # stdout 读到 EOF（服务器退出）后为 False：新请求直接失败，不必等到超时

    # ───────────── 进程与收发 ─────────────

    async def start(self) -> "StdioMCPClient":
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=default_environment(self.env),
            cwd=self.cwd,
            limit=MAX_MESSAGE_BYTES,
        )
        self._alive = True
        self._readers = [
            asyncio.create_task(self._read_stdout(self._proc.stdout)),
            asyncio.create_task(self._read_stderr(self._proc.stderr)),
        ]
        return self

    def _write(self, msg: dict) -> None:
        """把一条消息写进服务器的 stdin。write 本身不等待：整行一次性进入发送缓冲区，并发的请求不会交错。"""
        if self.on_message:
            self.on_message("→", msg)
        assert self._proc and self._proc.stdin, "先调用 start()"
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")

    async def _send(self, msg: dict) -> None:
        self._write(msg)
        assert self._proc and self._proc.stdin
        await self._proc.stdin.drain()  # 背压：服务器读得慢、管道满了，就在这里等

    async def _read_stdout(self, stdout: asyncio.StreamReader) -> None:
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except ValueError:  # 一行超过了 MAX_MESSAGE_BYTES：丢掉这条，等它的请求会超时
                    self.stderr_lines.append(f"[client] 服务器发来一条超过 {MAX_MESSAGE_BYTES} 字节的消息，已丢弃")
                    continue
                if not raw:
                    break  # EOF：服务器退出了
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.stderr_lines.append(f"[client] 服务器往 stdout 写了非 JSON 内容：{raw[:200]!r}")
                    continue
                if not isinstance(msg, dict):
                    continue
                if self.on_message:
                    self.on_message("←", msg)
                if "method" in msg and "id" in msg:  # 服务器发来的请求（旧版可能发 ping）
                    reply = {"jsonrpc": "2.0", "id": msg["id"]}
                    if msg["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": f"Method not found: {msg['method']}"}
                    if self._proc is not None:  # 正在关闭时就不回了
                        self._write(reply)
                elif "id" in msg:  # 响应：按 id 交给等待它的那个请求（它可能已经超时走了，那就丢掉）
                    waiter = self._pending.pop(msg["id"], None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(msg)
                # 其余是服务器通知（progress / log），这里简单忽略
        finally:  # 服务器退出（或客户端在关闭）：唤醒所有还在等的请求，不让它们干等到超时
            self._alive = False
            for waiter in self._pending.values():
                if not waiter.done():
                    waiter.set_result(None)
            self._pending.clear()

    async def _read_stderr(self, stderr: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await stderr.readline()
            except ValueError:
                continue
            if not raw:
                return
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    def _meta(self) -> dict:
        """现代版每个请求都要带的"自我介绍"：协议版本 + 客户端能力（必填）+ 客户端身份（建议）。"""
        return {META_VERSION: self.protocol_version or MODERN_VERSION, META_CLIENT_INFO: CLIENT_INFO, META_CLIENT_CAPS: {}}

    def _gone(self) -> MCPError:
        return MCPError(-32000, f"MCP 服务器进程已退出。最近的服务器日志：{list(self.stderr_lines)[-3:]}")

    async def request(self, method: str, params: dict | None = None, *, timeout_s: float | None = None, modern: bool | None = None) -> dict:
        """发一个请求并等待对应 id 的响应。协议错误抛 MCPError，超时抛 TimeoutError。

        超时、或者调用方不再需要结果（比如 Agent 的运行被取消）时，按规范发 notifications/cancelled，
        让服务器停下手里的活，而不是白白做完。
        """
        if self._proc is None or not self._alive:
            raise self._gone() if self._proc is not None else MCPError(-32000, "还没有连接：先 await connect()")
        params = dict(params or {})
        use_modern = self.era == "modern" if modern is None else modern
        if use_modern:
            params["_meta"] = {**params.get("_meta", {}), **self._meta()}
        rid = next(self._ids)
        waiter = asyncio.get_running_loop().create_future()
        self._pending[rid] = waiter
        self.max_in_flight = max(self.max_in_flight, len(self._pending))
        limit = timeout_s or self.timeout_s
        try:
            try:
                await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            except (ConnectionError, OSError) as e:  # 服务器已经退出，管道断了
                raise MCPError(-32000, f"无法写入 MCP 服务器（{e}）。最近的服务器日志：{list(self.stderr_lines)[-3:]}") from e
            msg = await wait_for(waiter, limit)  # 取消安全的 wait_for（agentkit.timeouts）
        except asyncio.TimeoutError:
            self._cancel_remote(rid, method, "timeout")
            raise TimeoutError(f"MCP 请求 {method} 超时（>{limit}s）") from None
        except asyncio.CancelledError:
            self._cancel_remote(rid, method, "cancelled by caller")
            raise  # 取消必须继续往外传
        finally:
            self._pending.pop(rid, None)
        if msg is None:
            raise self._gone()
        if "error" in msg:
            err = msg["error"]
            raise MCPError(err.get("code", 0), err.get("message", ""), err.get("data"))
        return msg.get("result", {})

    def _cancel_remote(self, rid: int, method: str, reason: str) -> None:
        """规范：不再等某个请求的结果时，应发取消通知（initialize 除外）。只写不等：取消路径上不能再卡住。"""
        if method == "initialize" or self._proc is None or not self._alive:
            return
        try:
            self._write({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rid, "reason": reason}})
        except (ConnectionError, OSError, RuntimeError):
            pass  # 服务器已经没了：没人需要这条通知

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._send(msg)

    # ───────────── 连接：协商用哪一代协议 ─────────────

    async def connect(self) -> "StdioMCPClient":
        if self._proc is None:
            await self.start()
        if self.mode in ("auto", "modern"):
            try:
                result = await self.request("server/discover", timeout_s=self.probe_timeout_s, modern=True)
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
        result = await self.request(
            "initialize",
            {"protocolVersion": LEGACY_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            modern=False,
        )
        if result.get("protocolVersion") not in LEGACY_VERSIONS:
            await self.aclose()
            raise MCPError(-32602, f"服务器选择了我们不支持的协议版本 {result.get('protocolVersion')!r}")
        self.era, self.protocol_version = "legacy", result["protocolVersion"]
        self.server_capabilities = result.get("capabilities", {})
        self.server_info = result.get("serverInfo", {})
        self.instructions = result.get("instructions")
        await self.notify("notifications/initialized")
        return self

    # ───────────── 工具 ─────────────

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:  # tools/list 支持分页：有 nextCursor 就继续取
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})

    # ───────────── 关闭 ─────────────

    async def aclose(self) -> None:
        """规范推荐的 stdio 关机顺序：关 stdin → 等它退出 → 还不退就 SIGTERM → 再不退 SIGKILL。

        每一步最多等 close_timeout_s 秒。进程一定会被收掉（不留僵尸），退出码记在 self.returncode。
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()  # EOF：请服务器自己退出
            # (升级动作, 这一步最多等多久)：先干等，再 SIGTERM，最后 SIGKILL（SIGKILL 之后一定会退出，不设上限）
            for escalate, limit in ((None, self.close_timeout_s), (proc.terminate, self.close_timeout_s), (proc.kill, None)):
                if escalate is not None:
                    try:
                        escalate()
                    except ProcessLookupError:  # 已经退出了
                        pass
                try:
                    await wait_for(proc.wait(), limit)
                    break
                except asyncio.TimeoutError:
                    continue
        except BaseException:  # 关闭过程中又被取消：别把子进程留下
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise
        finally:
            self.returncode = proc.returncode
            # 读 stdout/stderr 的任务在进程退出后会读到 EOF 自己结束；有孙进程攥着管道时别无限等
            readers, self._readers = self._readers, []
            if readers:
                _, pending = await asyncio.wait(readers, timeout=1.0)
                for t in pending:
                    t.cancel()
                await asyncio.wait(readers)
                for t in readers:  # 取走异常：没人取的任务异常会在垃圾回收时报"never retrieved"
                    if not t.cancelled() and t.exception() is not None:
                        self.stderr_lines.append(f"[client] 读管道的任务出错：{t.exception()!r}")

    async def __aenter__(self) -> "StdioMCPClient":
        try:
            return await self.connect()
        except BaseException:
            await self.aclose()  # 连接失败也要把已经启动的子进程收掉
            raise

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


# ───────────────────────────── 远程工具 → agentkit Tool ─────────────────────────────


def openai_safe_name(name: str) -> str:
    """MCP 工具名允许点号、最长 128 字符；OpenAI 兼容接口的函数名只允许 [a-zA-Z0-9_-]、最长 64。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return safe[:64] or "tool"


class RemoteTool(Tool):
    """远程 MCP 工具在 agentkit 里的"替身"。

    - schema()：把服务器给的 inputSchema 原样交给模型（不用本地函数签名生成）；
    - parse_arguments()：只检查"是不是 JSON 对象"，真正的参数校验交给服务器（规范要求服务器必须校验）；
    - fn：一个 async 函数，把调用转发给 invoke(arguments)（也是 async：要等服务器回复）。
      所以 Agent 在事件循环里 await 它：超时或运行被取消时，等待中的请求被取消，客户端给服务器发 notifications/cancelled。
    """

    def __init__(
        self,
        definition: dict,
        invoke: Callable[[dict], Awaitable[str]],
        *,
        name: str,
        risk: str,
        timeout_s: float = 30.0,
        max_output_chars: int = 4000,
    ):
        description = (definition.get("description") or definition.get("title") or f"MCP 工具 {definition['name']}").strip()

        async def _placeholder() -> None:  # 只为复用 Tool.__init__ 里的通用初始化（async：让 Tool 按 async 工具执行）
            ...

        super().__init__(
            _placeholder, name=name, description=description, risk=risk, timeout_s=timeout_s, max_output_chars=max_output_chars
        )

        async def forward(**arguments) -> str:
            return await invoke(arguments)

        self.fn = forward
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
    call_fn: Callable[[str, dict], Awaitable[dict]],
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
    call_fn(远程工具名, 参数) 是 async 函数（例如 client.call_tool），返回 CallToolResult；isError=true 时抛 ToolError，
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

    async def invoke(arguments: dict) -> str:
        result = await call_fn(remote, arguments)
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


async def mcp_tools(
    client: StdioMCPClient,
    *,
    include: Iterable[str] | None = None,
    trusted: bool = False,
    risk_overrides: dict[str, str] | None = None,
    pinned: dict[str, str] | None = None,
    name_prefix: str = "",
) -> list[Tool]:
    """把一个已连接的 MCP 服务器的工具包装成 agentkit Tool 列表，可以直接交给 Agent。

    include: 只导入这些工具（最小权限：接入一个服务器 ≠ 把它的全部工具都给模型）
    pinned:  锁定的工具指纹；服务器上的定义变了就拒绝加载（防 rug pull）
    客户端的生命周期由调用方管（async with StdioMCPClient(...)）：工具只在 async with 块里可用，
    离开时子进程一定被关掉 —— 不靠 atexit，也不会在事件循环关掉之后留下没人收的子进程。
    """
    definitions = await client.list_tools()
    if include is not None:
        wanted = set(include)
        definitions = [d for d in definitions if d["name"] in wanted]
    if pinned is not None:
        changed = changed_tools({n: pinned[n] for n in pinned if include is None or n in set(include)}, tool_fingerprints(definitions))
        if changed:
            raise PermissionError(f"MCP 工具定义与审查时不一致（可能是 rug pull）：{changed}。请重新审查后再更新指纹。")
    tools = []
    for d in definitions:
        t = tool_from_mcp_schema(d, client.call_tool, trusted=trusted, risk_overrides=risk_overrides, name_prefix=name_prefix)
        t.client = client  # type: ignore[attr-defined]
        tools.append(t)
    return tools
