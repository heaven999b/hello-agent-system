"""被发布的 Agent：IT 服务台 itbuddy —— demo 进程和 worker 进程（ops_app.py）共用的部分。

    PROMPT_V1 / V2 / V3   三个 prompt 版本（v2：紧急问题直接建 high 工单；v3：先用新工具 lookup_asset 查设备台账）
    make_tools(...)       四个工具：search_kb、get_ticket_status（读）、create_ticket（写）、lookup_asset（读，v3 新增）
    scripted_model        离线的"模型"：读 system prompt 里的规则决定怎么做 —— 换了 prompt 版本，行为真的会变
    make_traffic(...)     带固定随机种子的线上流量：(user_id, 租户, 用户输入)

失败都是**代码真实抛出的异常**，不是剧本里写好的数字：
    - get_ticket_status：工单系统偶发 504（工单号以 7 结尾的请求，约占全部流量的 1.5%）—— 基线本来就有的错误率；
    - lookup_asset：v3 上线的新工具有 bug，只认新 CMDB 里迁移过的资产编号，旧编号直接 KeyError
      （约占提到设备的请求的一半）—— 只有用 v3 的请求才会走到它。
"""

from __future__ import annotations

import random
import re
from typing import Awaitable, Callable, Literal

from agentkit import ToolContext, ToolError, call_tool, reply, tool
from agentkit.types import LLMResponse

KB = {
    "vpn": "VPN 连不上：确认没有连访客 Wi-Fi；用工号登录 GlobalConnect；报错 809 时重启客户端。",
    "显示器": "申请显示器：在 IT 门户提交'外设申请'，主管审批后 3 个工作日内发放。",
    "密码": "忘记密码：打开 id.example.com 自助重置，需要手机验证码。",
}

PROMPT_V1 = """你是 ACME 公司的 IT 服务台助手。
规则：
1. 先用 search_kb 查知识库，按文章内容给出步骤。
2. 知识库解决不了时，再用 create_ticket 建工单，priority 用 medium。
3. 回答简洁，不超过 80 字。"""

PROMPT_V2 = """你是 ACME 公司的 IT 服务台助手。
规则：
1. 先用 search_kb 查知识库，按文章内容给出步骤。
2. 知识库解决不了时，再用 create_ticket 建工单，priority 用 medium。
3. 用户说明情况紧急（如"很急""马上要用"），或问题影响多人时，不要查知识库，直接用 create_ticket 建 high 优先级工单，并告知工单号。
4. 回答简洁，不超过 80 字。"""

PROMPT_V3 = PROMPT_V2.replace("4. 回答简洁", "4. 用户提到设备编号（如 PC-1042）时，先用 lookup_asset 查设备台账，结合型号和保修状态回答。\n5. 回答简洁")

# 新 CMDB 里已经迁移过的资产（lookup_asset 只认这些）；旧编号还在老系统里 —— v3 的 bug 就藏在这里
MIGRATED_ASSETS = {f"PC-{n}": {"model": "ThinkPad X1", "warranty": "2027-06"} for n in range(1000, 1050)}


def make_tools(on_ticket: Callable[[str, str, ToolContext], Awaitable[str]] | None = None):
    """返回 [search_kb, get_ticket_status, create_ticket, lookup_asset]。

    on_ticket(title, priority, ctx) -> 工单号：真正建单的地方（worker 里写共享数据库）；不传则只返回一个假工单号。
    """

    @tool
    def search_kb(query: str) -> str:
        """搜索 IT 知识库，返回最相关的文章"""
        q = query.lower()
        hits = [text for key, text in KB.items() if key in q]
        return "\n".join(hits) or "没有找到相关文章"

    @tool
    async def get_ticket_status(ticket_id: str) -> str:
        """查询工单的处理状态"""
        if not re.fullmatch(r"T-\d{4}", ticket_id):
            raise ToolError(f"工单号格式应为 T-1234，收到 {ticket_id!r}")
        if ticket_id.endswith("7"):
            raise ConnectionError("上游 ticketing-api 返回 504")  # 基线就有的偶发故障
        return f"{ticket_id}：处理中，负责人 李工，预计今天 18:00 前完成"

    @tool(risk="write")
    async def create_ticket(title: str, priority: Literal["low", "medium", "high"], ctx: ToolContext) -> str:
        """创建一张 IT 工单，返回工单号"""
        ticket = await on_ticket(title, priority, ctx) if on_ticket is not None else "T-2001"
        return f"已创建工单 {ticket}（{priority}）：{title}"

    @tool
    async def lookup_asset(asset_id: str) -> str:
        """按设备编号（如 PC-1042）查询设备台账：型号、保修期"""
        info = MIGRATED_ASSETS[asset_id.upper()]  # BUG：没迁移的旧编号直接 KeyError（应该回退到老系统或返回"查不到"）
        return f"{asset_id}：{info['model']}，保修至 {info['warranty']}"

    return [search_kb, get_ticket_status, create_ticket, lookup_asset]


# ---------------------------------------------------------------- 离线"模型"


def scripted_model(messages: list[dict]) -> LLMResponse:
    """一个遵守 system prompt 规则的剧本模型（ScriptedLLM(responder=scripted_model)）。

    它读 system prompt 里有没有 v2 / v3 新增的规则来决定第一步做什么，所以**同一段代码、不同的 prompt 版本 = 不同的行为**，
    和真实模型一样。看到工具结果后给出最终回答；工具报错时如实道歉（这次运行就算"出错"）。
    """
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    last = messages[-1]
    if last["role"] == "tool":
        content = last["content"]
        if content.startswith("错误："):
            return reply("抱歉，查询时系统出错了，请稍后再试或转人工。", input_tokens=520, output_tokens=30)
        if "紧急停用" in content or "只读" in content:
            return reply("该功能暂时停用，请稍后再试或拨打 IT 热线。", input_tokens=520, output_tokens=25)
        return reply(f"根据查询结果：{content[:60]}", input_tokens=520, output_tokens=60)
    text = last["content"]
    asset = re.search(r"PC-\d{4}", text)
    ticket = re.search(r"T-\d{4}", text)
    if "直接用 create_ticket 建 high" in system and ("很急" in text or "马上要用" in text):
        return call_tool("create_ticket", title=text[:20], priority="high", input_tokens=480, output_tokens=40)
    if "lookup_asset" in system and asset:
        return call_tool("lookup_asset", asset_id=asset.group(0), input_tokens=500, output_tokens=30)
    if ticket:
        return call_tool("get_ticket_status", ticket_id=ticket.group(0), input_tokens=450, output_tokens=25)
    if "报修" in text or "坏了" in text:
        return call_tool("create_ticket", title=text[:20], priority="medium", input_tokens=460, output_tokens=40)
    key = next((k for k in KB if k in text.lower()), text[:8])
    return call_tool("search_kb", query=key, input_tokens=450, output_tokens=25)


# ---------------------------------------------------------------- 线上流量


TEMPLATES = [
    (30, "VPN 连不上，报错 809"),
    (10, "VPN 连不上，报错 809，很急，我明天一早要出差"),
    (15, "怎么申请一台新显示器？"),
    (10, "忘记密码了怎么办"),
    (15, "帮我查一下工单 T-{t} 的进度"),
    (20, "笔记本 PC-{pc} 开不了机，帮我看看还在保修期吗"),
]


def make_traffic(n: int, *, seed: int, users: int = 10_000) -> list[dict]:
    """n 个请求：用户从 users 个人里随机挑（同一个用户可能出现多次），内容按上面的比例生成。"""
    rng = random.Random(seed)
    weights = [w for w, _ in TEMPLATES]
    out = []
    for _ in range(n):
        tmpl = rng.choices([t for _, t in TEMPLATES], weights)[0]
        text = tmpl.format(t=rng.randint(1000, 9999), pc=rng.choice([rng.randint(1000, 1049), rng.randint(3000, 3999)]))
        out.append({"user_id": f"u{rng.randrange(users):05d}", "tenant": "acme", "input": text})
    return out


def repair_traffic(n: int, *, seed: int, tenant: str = "acme") -> list[dict]:
    """报修类请求：每个都会调用写工具 create_ticket（场景 5 用它测紧急开关）。"""
    rng = random.Random(seed)
    return [{"user_id": f"u{rng.randrange(10_000):05d}", "tenant": tenant, "input": f"{rng.randint(2, 9)} 楼打印机坏了，帮我报修"}
            for _ in range(n)]
