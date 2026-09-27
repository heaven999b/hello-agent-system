"""第 19 课 Demo：MCP 协议与代码执行沙箱。

    .venv/bin/python lessons/19_mcp_and_sandbox/demo.py            # 真实模型（约 6～8 次模型调用）
    .venv/bin/python lessons/19_mcp_and_sandbox/demo.py --offline  # 离线剧本，无需 API key

第 1 部分  MCP 报文实录（不调用模型，两种模式输出一致）
    1a 现代版 2026-07-28：server/discover → tools/list → tools/call（成功 / 业务错误 / 参数错误）→ 未知方法
    1b 旧版 2025-11-25：initialize → notifications/initialized → tools/list → tools/call
    1c 防 rug pull：锁定工具定义的指纹，服务器"更新"后定义变了 → 拒绝加载
    1d （可选）装了官方 mcp SDK 时，和官方实现互相连一遍
第 2 部分  Agent 通过 MCP 客户端调用远程工具完成任务（未审查过的写操作要人工审批）
第 3 部分  代码执行沙箱
    3a Agent 写代码、在沙箱里执行（CodeAct 风格，每次执行都要审批）
    3b 死循环 → 墙钟超时，整个进程组被杀
    3c 内存炸弹 → 被限制（Linux：RLIMIT_AS；macOS：轮询 phys_footprint 兜底）
    3d 偷读"私钥"、偷偷联网 → 进程级沙箱挡不住；macOS 上加一层 Seatbelt 才挡住
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pwd
import shutil
import socket
import sys
import threading
from pathlib import Path
from types import ModuleType

from agentkit import Agent, Hook, PermissionPolicy, ResilientLLM, ScriptedLLM, call_tool, default_llm, reply

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
SERVER_CMD = [sys.executable, str(HERE / "mcp_server.py")]


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（与 exercise.py 里的同名函数一致，保证全进程只有一份）。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


mcp_client = _load_sibling("mcp_client")
sandbox = _load_sibling("sandbox")
sdk_compare = _load_sibling("sdk_compare")


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 76 + f"\n  {title}\n" + "═" * 76, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def say(text: str | None) -> None:
    """打印模型的回答（可能有多行），每行都缩进对齐。"""
    lines = (text or "").strip().splitlines() or [""]
    info(f"🤖 {lines[0]}")
    for line in lines[1:]:
        info(f"   {line}")


def short(text: str | None, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class WirePrinter:
    """把客户端收发的每一条 JSON-RPC 报文打印出来。重复的 _meta 和过长的工具列表会被折叠，其余原样输出。"""

    def __init__(self) -> None:
        self.meta_shown = False

    def __call__(self, direction: str, msg: dict) -> None:
        msg = json.loads(json.dumps(msg))  # 深拷贝，折叠时不影响真实报文
        params = msg.get("params")
        if isinstance(params, dict) and "_meta" in params:
            if self.meta_shown:
                params["_meta"] = "…（同上：协议版本 + 客户端身份 + 客户端能力）"
            self.meta_shown = True
        result = msg.get("result")
        if isinstance(result, dict):
            if "_meta" in result:
                result["_meta"] = "…（serverInfo）"
            tools = result.get("tools")
            if isinstance(tools, list) and len(tools) > 1:
                result["tools"] = tools[:1] + [f"…（其余 {len(tools) - 1} 个工具略：{', '.join(t['name'] for t in tools[1:])}）"]
        arrow = "客户端 → 服务器" if direction == "→" else "服务器 → 客户端"
        print(f"   {direction} [{arrow}] {json.dumps(msg, ensure_ascii=False)}", flush=True)


# =====================================================================
# 第 1 部分：MCP 报文实录
# =====================================================================


def part1_wire() -> None:
    banner("第 1 部分：MCP 报文实录 —— 协议就是一行一行的 JSON-RPC")
    info(f"服务器：{Path(SERVER_CMD[1]).relative_to(HERE.parents[1])}（子进程，stdin/stdout 各一个管道，一行一条消息）")

    step("1a 现代版（2026-07-28）：没有握手，每个请求自己带协议版本和能力")
    with mcp_client.StdioMCPClient(SERVER_CMD, mode="auto", on_message=WirePrinter()) as c:
        info(f"→ 协商结果：era={c.era}，protocolVersion={c.protocol_version}")
        c.list_tools()
        c.call_tool("get_travel_policy", {"city": "东京"})
        c.call_tool("get_travel_policy", {"city": "火星"})  # 业务错误
        c.call_tool("convert_currency", {"amount": "两万", "from_currency": "JPY"})  # 参数错误
        try:
            c.request("resources/list")
        except mcp_client.MCPError as e:
            info(f"→ 客户端收到协议错误 MCPError{e}")
    takeaway("同样是'没做成'：未知方法走 JSON-RPC error（-32601）；业务错误、参数错误走正常 result + isError: true。"
             "后者的文字会交给模型，让它自己改参数重试。")

    step("1b 旧版（2025-11-25）：先 initialize 握手，再 notifications/initialized，然后才能正常调用")
    with mcp_client.StdioMCPClient(SERVER_CMD, mode="legacy", on_message=WirePrinter()) as c:
        c.list_tools()
        c.call_tool("get_travel_policy", {"city": "上海"})
    takeaway("注意 notifications/initialized 没有 id，服务器也没有回复它 —— 这就是'通知'。"
             "今天大多数已部署的服务器仍说旧版，所以本课的服务器两代都支持（dual-era）。")

    step("1c 防 rug pull：第一次审查时锁定工具指纹，之后每次连接都比对")
    with mcp_client.StdioMCPClient(SERVER_CMD) as c:
        pinned = mcp_client.tool_fingerprints(c.list_tools())
    info(f"审查通过时锁定的指纹：{pinned}")
    try:
        mcp_client.mcp_tools(SERVER_CMD, pinned=pinned, env={"MINI_MCP_VARIANT": "rugpull"})
        info("（没检测到变化？这不应该发生）")
    except PermissionError as e:
        info(f"服务器'更新'后再连接 → 拒绝加载：{e}")
    with mcp_client.StdioMCPClient(SERVER_CMD, env={"MINI_MCP_VARIANT": "rugpull"}) as c:
        poisoned = next(t for t in c.list_tools() if t["name"] == "get_travel_policy")
    info("被篡改的工具说明书（模型能看到，用户界面里通常看不到）：")
    for line in poisoned["description"].splitlines():
        info(f"   │ {line}")
    info(f"   │ 参数：{list(poisoned['inputSchema']['properties'])}  ← 多了一个用来夹带数据的 notes")
    takeaway("工具名没变、功能照常，只是说明书里多了给模型的'暗示'。指纹一比就露馅：这就是给工具定义'锁版本'。")

    step("1d （可选）和官方 MCP Python SDK 互相连一遍")
    sdk_compare.run_comparison(out=info)


# =====================================================================
# 第 2 部分：Agent 通过 MCP 调用远程工具
# =====================================================================

TRAVEL_SYSTEM = "你是公司的差旅报销助手。需要数据时调用工具，不要自己编造标准和汇率。外币金额先用工具换算成人民币。回答简洁。"
TRAVEL_TASK = "我下周去东京出差 3 晚，酒店每晚 21000 日元。帮我看看超没超公司标准；没超的话，把 3 晚住宿费按人民币提交报销，标题写“东京出差住宿”。"


def approver(call, state) -> bool:
    info(f"   🔐 [审批] {call.name} 风险等级 dangerous → 批准（演示中自动批准）")
    return True


class LiveTrace(Hook):
    """边运行边打印工具调用。放在 PermissionPolicy 前面，打印顺序就是：调用 → 审批 → 结果。"""

    def __init__(self, show_calls: bool = True):
        self.show_calls = show_calls

    def before_tool(self, state, call, tool):
        if self.show_calls:
            info(f"🛠  {call.name}({short(call.arguments, 110)})")
        return None

    def after_tool(self, state, call, result):
        info(f"   ↳ {short(result.content, 110)}")
        return None


def part2_agent(offline: bool) -> None:
    banner("第 2 部分：Agent 通过 MCP 客户端调用远程工具")
    tools = mcp_client.mcp_tools(
        SERVER_CMD,
        include=["get_travel_policy", "convert_currency", "submit_expense"],  # 最小权限：不导入 delete_expense
        risk_overrides={"get_travel_policy": "read", "convert_currency": "read"},  # 我们审查过的两个只读工具
    )
    client = tools[0].client
    info(f"已连接 {client.server_info.get('name')}（{client.era}，{client.protocol_version}），服务器共 4 个工具，只导入 3 个：")
    for t in tools:
        info(f"  - {t.name:<18} 服务器注解 {t.annotations} → 本地风险等级 {t.risk}")
    info("服务器声称 submit_expense 不具破坏性（destructiveHint=false），但我们没审查过它 → 按 dangerous 处理，要审批。")

    if offline:
        llm = ScriptedLLM(
            [
                call_tool("get_travel_policy", city="东京"),
                call_tool("convert_currency", amount=21000, from_currency="JPY", to_currency="CNY"),
                call_tool("submit_expense", title="东京出差住宿", amount_cny=3024),
                reply("没超标：东京每晚上限 1100 元，你每晚 21000 日元 ≈ 1008 元。已提交 3 晚住宿费 3024 元，单号 EXP-1001。"),
            ]
        )
    else:
        llm = ResilientLLM(default_llm())
    agent = Agent(llm, tools, system_prompt=TRAVEL_SYSTEM, hooks=[LiveTrace(), PermissionPolicy(approver=approver)], max_steps=8)
    step(f"用户：{TRAVEL_TASK}")
    result = agent.run(TRAVEL_TASK)
    say(result.output)
    info(f"状态 {result.status}，{result.steps} 步，工具调用顺序 {result.tools_called()}")
    client.close()
    takeaway("对 Agent 来说，远程 MCP 工具和本地 @tool 没有区别：同样的 schema、同样的'错误即观察'、同样的审批钩子。"
             "区别在信任：远程服务器的注解只是'自我介绍'，风险等级要由你来定。")


# =====================================================================
# 第 3 部分：代码执行沙箱
# =====================================================================

PRIME_TASK = "1 到 1000000 之间一共有多少个质数？请写 Python 代码算出来，不要凭记忆回答。"
PRIME_CODE = (
    "n = 1_000_000\n"
    "sieve = bytearray([1]) * (n + 1)\n"
    "sieve[0] = sieve[1] = 0\n"
    "for i in range(2, int(n ** 0.5) + 1):\n"
    "    if sieve[i]:\n"
    "        sieve[i * i :: i] = bytearray(len(range(i * i, n + 1, i)))\n"
    "print(sum(sieve))\n"
)


def code_approver(call, state) -> bool:
    try:
        code = json.loads(call.arguments).get("code", "")
    except (json.JSONDecodeError, AttributeError):
        code = call.arguments
    info("🔐 [审批] 模型想执行这段代码：")
    for line in code.splitlines()[:12]:
        info(f"   │ {line}")
    if len(code.splitlines()) > 12:
        info(f"   │ …（共 {len(code.splitlines())} 行）")
    info("   → 批准（演示中自动批准）")
    return True


def show_result(r, notes: bool = True) -> None:
    info(f"exit_code={r.exit_code}  timed_out={r.timed_out}  killed_reason={r.killed_reason}  用时 {r.duration_s}s")
    if r.stdout:
        info(f"stdout: {short(r.stdout, 150)}")
    if r.stderr:
        info(f"stderr: {short(r.stderr.strip().splitlines()[-1], 150)}")
    for n in r.notes if notes else []:
        info(f"· {n}")


def part3_sandbox(offline: bool) -> None:
    banner("第 3 部分：代码执行沙箱 —— 能挡住什么，挡不住什么")
    info(f"平台：{sys.platform}；RLIMIT_AS 可用：{sandbox.rlimit_as_works()}；RLIMIT_CPU 可靠：{sandbox.rlimit_cpu_reliable()}；"
         f"Seatbelt 可用：{sandbox.seatbelt_available()}")

    step("3a CodeAct 风格：模型写代码，沙箱执行，结果作为观察返回（run_python 是 dangerous，每次都要审批）")
    run_python_tool = sandbox.make_run_python_tool(sandbox.SandboxLimits(timeout_s=10))
    if offline:
        llm = ScriptedLLM([call_tool("run_python", code=PRIME_CODE), reply("1 到 1000000 之间一共有 78498 个质数。")])
    else:
        llm = ResilientLLM(default_llm())
    agent = Agent(
        llm,
        [run_python_tool],
        system_prompt="你是严谨的数据助手。需要计算时用 run_python 工具执行代码，不要心算或凭记忆。回答只给结论，不要复述代码。",
        hooks=[PermissionPolicy(approver=code_approver), LiveTrace(show_calls=False)],
        max_steps=6,
    )
    step(f"用户：{PRIME_TASK}")
    result = agent.run(PRIME_TASK)
    say(result.output)

    step("3b 死循环：墙钟超时 1 秒，整个进程组被 SIGKILL")
    show_result(sandbox.run_python("print('开始计算…', flush=True)\nwhile True:\n    pass", sandbox.SandboxLimits(timeout_s=1)), notes=False)

    step("3c 内存炸弹：连续分配 64 MB 的块，最多 1 GB；上限 256 MB")
    bomb = (
        "chunks = []\n"
        "try:\n"
        "    for i in range(16):\n"
        "        chunks.append(b'x' * (64 * 1024 * 1024))\n"
        "        print('已分配', (i + 1) * 64, 'MB', flush=True)\n"
        "    print('分配完 1 GB：限制没有生效！')\n"
        "except MemoryError:\n"
        "    print('MemoryError：分配到', len(chunks) * 64, 'MB 时被内核拒绝')\n"
    )
    r = sandbox.run_python(bomb, sandbox.SandboxLimits(timeout_s=10, memory_mb=256))
    show_result(r)
    info(f"最后一行输出：{r.stdout.strip().splitlines()[-1] if r.stdout.strip() else '（无）'}")

    step("3d 偷读'私钥'和偷偷联网：先用进程级沙箱，再加一层 OS 沙箱")
    fake_key = RUNS / "fake_home" / ".ssh" / "id_ed25519"
    fake_key.parent.mkdir(parents=True, exist_ok=True)
    fake_key.write_text("-----BEGIN FAKE KEY----- 这是演示用的假私钥 -----END FAKE KEY-----\n", encoding="utf-8")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    def accept_forever() -> None:
        try:
            while True:
                listener.accept()[0].close()
        except OSError:  # 演示结束、socket 被关闭
            pass

    threading.Thread(target=accept_forever, daemon=True).start()
    info(f"准备：在仓库里放一个假私钥 {fake_key.relative_to(HERE.parents[1])}；在 127.0.0.1:{port} 开一个'攻击者服务器'。")
    probe = f"""
import os, pwd, socket
real_home = pwd.getpwuid(os.getuid()).pw_dir
print("HOME 环境变量指向临时目录，~/.ssh 看起来不存在：", not os.path.isdir(os.path.expanduser("~/.ssh")))
print("但真实家目录下的 .ssh 仍然可见（只检查是否存在，不读取）：", os.path.isdir(os.path.join(real_home, ".ssh")))
try:
    print("读取假私钥：", open({str(fake_key)!r}).read().strip()[:24], "…")
except OSError as e:
    print("读取假私钥：失败", type(e).__name__)
try:
    socket.create_connection(("127.0.0.1", {port}), timeout=2).close()
    print("连接攻击者服务器：成功（数据可以被发出去）")
except OSError as e:
    print("连接攻击者服务器：失败", type(e).__name__)
"""
    for os_sandbox in (False, True):
        label = "进程级沙箱（超时 + rlimit + 临时目录 + 最小环境变量）" if not os_sandbox else "再加一层 OS 沙箱（macOS Seatbelt）"
        info(f"\n   【{label}】")
        r = sandbox.run_python(probe, sandbox.SandboxLimits(os_sandbox=os_sandbox))
        for line in r.stdout.strip().splitlines():
            info(f"  {line}")
        if r.stderr.strip():
            info(f"  stderr: {short(r.stderr.strip().splitlines()[-1], 120)}")
        if os_sandbox:
            for n in r.notes:
                if n.startswith(("Seatbelt", "os_sandbox")):
                    info(f"  · {n}")
    listener.close()
    shutil.rmtree(RUNS / "fake_home", ignore_errors=True)
    try:
        RUNS.rmdir()  # 目录空了就顺手删掉
    except OSError:
        pass
    takeaway("进程级沙箱管得住'时间'和'资源'，管不住'身份'：代码以你的用户身份运行，能读你能读的所有文件、连你能连的所有地址。"
             "要挡住文件和网络，需要 OS 级隔离（Seatbelt / bubblewrap / 容器的 namespace），多租户再往上到 gVisor 或 microVM。")


def main() -> None:
    parser = argparse.ArgumentParser(description="第 19 课 Demo：MCP 协议与代码执行沙箱")
    parser.add_argument("--offline", action="store_true", help="使用离线剧本（ScriptedLLM），不调用真实模型")
    parser.add_argument("--only", default="1,2,3", help="只运行指定部分，如 --only 1,3")
    args = parser.parse_args()
    if args.offline:
        print("🔌 离线模式：模型的决策来自剧本（ScriptedLLM）；MCP 服务器和沙箱都是真实的子进程")
    else:
        try:
            model = default_llm().model
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"🌐 真实模型：{model}（第 1 部分和第 3b～3d 不调用模型）")
    parts = {"1": part1_wire, "2": lambda: part2_agent(args.offline), "3": lambda: part3_sandbox(args.offline)}
    for key in args.only.replace(" ", "").split(","):
        parts[key]()

    banner("小结")
    info("1. MCP = JSON-RPC 2.0 + 约定好的方法名；stdio 传输就是'子进程 + 一行一条消息'，手写两三百行就能和官方 SDK 互通。")
    info("2. 两类错误要分清：请求本身错了 → JSON-RPC error；工具没做成 → result.isError，让模型自己改。")
    info("3. 注解和描述都是服务器的'自我介绍'：风险等级由你定，工具定义要锁指纹，只导入需要的工具。")
    info("4. 进程级沙箱 = 超时 + 资源上限 + 临时目录 + 最小环境；它挡不住读文件和联网，那要靠 OS 级隔离或容器 / microVM。")


if __name__ == "__main__":
    main()
