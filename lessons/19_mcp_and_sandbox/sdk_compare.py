"""第 19 课（可选）：用官方 MCP Python SDK 做同样的事，验证手写实现能和官方实现互通。

    pip install "mcp>=2.2"                                    # 可选依赖，课程离线测试不需要它
    .venv/bin/python lessons/19_mcp_and_sandbox/sdk_compare.py

两个方向各验证一次：
  A. 官方 SDK 客户端  →  我们手写的 mcp_server.py（auto 模式走现代版，legacy 模式走 initialize 握手）
  B. 我们手写的客户端 →  官方 SDK 写的服务器（本文件加 --serve 参数运行）
官方 SDK 用的是 2.x 版本的 API（1.x 里的 FastMCP 在 2.x 改名为 MCPServer）。
两边都是 async：官方客户端基于 anyio（在 asyncio 上运行），我们的客户端直接用 asyncio，所以 A、B 在同一个事件循环里跑。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def sdk_available() -> bool:
    return importlib.util.find_spec("mcp") is not None


def serve_with_sdk() -> None:
    """同样的两个只读工具，用官方 SDK 写只要十几行：装饰器 + 类型注解，协议细节全被藏起来了。"""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("sdk-travel", instructions="官方 SDK 版的差旅工具")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def get_travel_policy(city: str) -> dict:
        """查询公司差旅标准：某城市每晚酒店报销上限和每日餐补（单位：人民币元）。"""
        caps = {"东京": (1100, 300), "上海": (650, 100)}
        if city not in caps:
            raise ValueError(f"没有城市 {city!r} 的差旅标准")
        return {"city": city, "hotel_cap_per_night_cny": caps[city][0], "meal_allowance_per_day_cny": caps[city][1]}

    server.run("stdio")


async def run_comparison(out=print) -> bool:
    """跑 A、B 两个方向的互通验证。没装官方 SDK 时返回 False。"""
    if not sdk_available():
        out("  （没有安装官方 mcp SDK，跳过。想看对照：pip install \"mcp>=2.2\"）")
        return False
    try:
        from mcp import Client, StdioServerParameters
    except ImportError as e:  # 比如装的是 1.x：没有 mcp.Client
        out(f"  （已安装的 mcp SDK 版本不兼容本对照（需要 2.x）：{e}，跳过）")
        return False

    # 官方 SDK 默认只给服务器子进程传 HOME/PATH 等少数环境变量；这里显式加上 PYTHONPATH，
    # 保证即使当前解释器没有 pip install -e 本仓库，服务器也能 import agentkit。
    ours = StdioServerParameters(command=sys.executable, args=[str(HERE / "mcp_server.py")], env={"PYTHONPATH": str(REPO), "MINI_MCP_QUIET": "1"})

    async def sdk_client_to_our_server(mode: str) -> None:
        async with Client(ours, mode=mode) as client:
            tools = await client.list_tools()
            ok = await client.call_tool("get_travel_policy", {"city": "东京"})
            bad = await client.call_tool("get_travel_policy", {"city": "火星"})
            out(f"  [A] 官方客户端 mode={mode!r:9} → 协商版本 {client.protocol_version}，"
                f"工具 {[t.name for t in tools.tools]}")
            out(f"      正常调用 isError={ok.is_error}：{ok.content[0].text}")
            out(f"      业务错误 isError={bad.is_error}：{bad.content[0].text[:40]}…")

    for mode in ("auto", "legacy"):
        await sdk_client_to_our_server(mode)

    # B：我们的客户端连官方 SDK 服务器
    spec = importlib.util.spec_from_file_location(f"{HERE.name}__mcp_client", HERE / "mcp_client.py")
    mcp_client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp_client)
    async with mcp_client.StdioMCPClient([sys.executable, str(__file__), "--serve"]) as c:
        tools = await c.list_tools()
        result = await c.call_tool("get_travel_policy", {"city": "上海"})
        out(f"  [B] 手写客户端 → 官方服务器 {c.server_info.get('name')}：era={c.era}，版本 {c.protocol_version}")
        out(f"      工具 {[t['name'] for t in tools]}，注解 {tools[0].get('annotations')}")
        out(f"      调用结果：{' '.join(mcp_client.result_to_text(result).split())}")
    return True


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve_with_sdk()
    else:
        asyncio.run(run_comparison())
