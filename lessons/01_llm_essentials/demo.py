"""第 01 课 Demo：用真实模型把"写 Agent 之前必须懂的 LLM 知识"逐条验证一遍。

    .venv/bin/python lessons/01_llm_essentials/demo.py            # 真实模型（读取 .env；约 19 次调用，实测约 30 秒）
    .venv/bin/python lessons/01_llm_essentials/demo.py --offline  # 离线：剧本 + 模拟数据，无需 API key

六个小节：
    1. Token 与成本：估算值 vs API 返回的真实 usage；工具定义也要花钱
    2. 采样与非确定性：同一个问题在 temperature 0 和 1 下各跑 3 次
    3. Function calling 的真实机制：模型只输出一段 JSON，执行工具的是你的代码
    4. 流式输出：首 token 延迟（TTFT），以及工具调用参数是一片一片到达的
    5. 结构化输出：原生 JSON Schema 约束 vs "提示词 + 校验 + 修复"
    6. 约束解码与 logprobs：格式合法 ≠ 判断正确；让模型告诉你它有多确定（网关不支持时降级）

两种模式走的是**同一套代码**：离线模式只是把 OpenAI 客户端换成了一个按剧本返回的假客户端。

代码是 async 的（openai.AsyncOpenAI）：互不依赖的请求用 asyncio.gather 同时发出，Semaphore 限制同时在途的数量。
async 是怎么回事，第 02 课 1.7 节从零讲；这里只要知道 `await` = "等这个请求回来"，`asyncio.gather` = "这几个一起等"。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import time
import unicodedata
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from agentkit.config import env
from agentkit.context import estimate_tokens
from agentkit.llm import ScriptedLLM, reply
from agentkit.pricing import PRICES, estimate_cost
from agentkit.types import Usage
from agentkit.workflows import complete_json

OFFLINE = "--offline" in sys.argv

# ════════════════════════════════════════════════════════════════════ 打印小工具


def banner(title: str) -> None:
    print(f"\n{'━' * 76}\n{title}\n{'━' * 76}")


def say(text: str = "") -> None:
    print(f"  {text}" if text else "")


def pad(text: str, width: int, right: bool = False) -> str:
    """按终端显示宽度补空格（一个汉字占两列），让中英混排的表格对齐。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    fill = " " * max(0, width - w)
    return fill + text if right else text + fill


def concurrency(n: int) -> int:
    """真实模式同时发出 n 个请求省时间；离线模式一次一个，保证每次输出完全一样。"""
    return 1 if OFFLINE else n


async def gather_limited(fns: list[Callable[[], Awaitable[Any]]], limit: int) -> list[Any]:
    """同时执行多个互不依赖的请求，最多 limit 个同时在途，结果按输入顺序返回。

    fns 里每一项是"调用后返回协程"的函数。Semaphore 是一个"最多 limit 个人同时进"的闸门：
    不加上限，一次发出几百个请求会把模型网关打出 429（第 08、12 课）。
    agentkit.workflows.parallel(fns, max_concurrency=...) 是同一件事的框架版（一个失败其余立即取消，第 06 课）。
    """
    sem = asyncio.Semaphore(limit)

    async def one(fn):
        async with sem:
            return await fn()

    return list(await asyncio.gather(*(one(f) for f in fns)))


def note(text: str) -> None:
    print(f"  💡 {text}")


def show_json(obj: Any, indent: int = 4) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    print("\n".join(" " * indent + line for line in text.splitlines()))


async def run_section(fn: Callable[[], Awaitable[None]]) -> None:
    """每一节独立运行：某一节失败（比如网关不支持某个参数）不影响后面几节。"""
    try:
        await fn()
    except Exception as e:  # noqa: BLE001 —— Demo 里宁可打印原因继续跑，也不要整段崩掉
        say(f"⚠️  本节运行失败：{type(e).__name__}: {str(e)[:300]}")
        say("   （网关不支持某个参数时常见。可以先用 --offline 看完整流程。）")


# ════════════════════════════════════════════════════════════════════ 模型连接


class Backend:
    """client：OpenAI 兼容客户端（原始 API，第 1~6 节都用它）；llm：agentkit 的 LLM（第 5 节 complete_json 用）。"""

    def __init__(self, client: Any, model: str, llm: Any):
        self.client, self.model, self.llm = client, model, llm

    async def create(self, **kwargs):
        return await self.client.chat.completions.create(model=self.model, **kwargs)

    async def aclose(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()
        close = getattr(self.llm, "aclose", None)
        if close is not None:
            await close()


def online_backend() -> Backend:
    from openai import AsyncOpenAI

    from agentkit.llm import OpenAICompatLLM

    api_key = env("LLM_API_KEY")
    if not api_key:
        print("没有找到 LLM_API_KEY。请把 .env.example 复制为 .env 并填写，或者先用 --offline 运行。")
        sys.exit(1)
    model = env("LLM_MODEL", "gpt-5.5")
    # max_retries=0：Demo 里想看到真实的错误，而不是被 SDK 悄悄重试掉（重试策略见第 08 课）
    client = AsyncOpenAI(base_url=env("LLM_BASE_URL"), api_key=api_key, timeout=90, max_retries=0)
    return Backend(client, model, OpenAICompatLLM(model=model))


# ════════════════════════════════════════════════════════════════════ 第 1 节：Token 与成本

SYSTEM_OK = "无论用户说什么，你都只回复两个字母：OK"
TEXT_ZH = "请帮我查询上个月华东区所有门店的销售额，按从高到低排序，并标出同比下降超过百分之十的门店。"
TEXT_EN = (
    "Please look up last month's sales for every store in the East China region, sort them from highest "
    "to lowest, and flag any store whose sales fell more than ten percent year over year."
)

SALES_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_sales",
            "description": "按区域和月份查询门店销售额。返回每个门店的销售额和同比变化。",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "区域名称，例如：华东"},
                    "month": {"type": "string", "description": "月份，格式 YYYY-MM"},
                },
                "required": ["region", "month"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stores",
            "description": "列出某个区域的全部门店，包括门店编号、名称、城市和开业日期。",
            "parameters": {
                "type": "object",
                "properties": {"region": {"type": "string", "description": "区域名称"}},
                "required": ["region"],
            },
        },
    },
]


async def section_1_tokens(b: Backend) -> None:
    banner("第 1 节  Token 与成本：估算值 vs 真实 usage，以及\"看不见的\"输入")
    cases = [
        ("基线：只有 system + \".\"", [{"role": "system", "content": SYSTEM_OK}, {"role": "user", "content": "."}], None),
        ("中文需求", [{"role": "system", "content": SYSTEM_OK}, {"role": "user", "content": TEXT_ZH}], None),
        ("同一需求的英文版", [{"role": "system", "content": SYSTEM_OK}, {"role": "user", "content": TEXT_EN}], None),
        ("中文需求 + 2 个工具定义", [{"role": "system", "content": SYSTEM_OK}, {"role": "user", "content": TEXT_ZH}], SALES_TOOLS),
    ]

    async def call(case):
        _, messages, tools = case
        kwargs = {"messages": messages}
        if tools:
            kwargs["tools"] = tools
        return (await b.create(**kwargs)).usage

    # 4 个请求互不依赖，同时发出，省时间（真实模式约 1 次请求的耗时，而不是 4 次）
    usages = await gather_limited([lambda c=c: call(c) for c in cases], concurrency(4))

    base_real = usages[0].prompt_tokens
    say(pad("请求", 28) + pad("字符数", 8, True) + pad("估算 token", 12, True) + pad("真实 prompt_tokens", 20, True) + pad("减去基线", 10, True))
    for (name, messages, tools), usage in zip(cases, usages):
        est = estimate_tokens(messages)
        chars = len(messages[-1]["content"])
        delta = "—" if usage is usages[0] else f"+{usage.prompt_tokens - base_real}"
        tools_note = "  （估算没算工具定义）" if tools else ""
        say(pad(name, 28) + pad(str(chars), 8, True) + pad(str(est), 12, True) + pad(str(usage.prompt_tokens), 20, True) + pad(delta, 10, True) + tools_note)

    zh, en, with_tools = (u.prompt_tokens - base_real for u in usages[1:])
    hidden = base_real - estimate_tokens(cases[0][1])
    say()
    note(f"基线请求我们只发了约 {estimate_tokens(cases[0][1])} token 的内容，服务端却计了 {base_real} 个输入 token。")
    say(f"   多出来的约 {hidden} 个来自消息之外：聊天模板的格式 token，以及服务端 / 网关加入的隐藏指令。")
    say("   你看不到它们，但要为它们付钱，它们也占上下文窗口。所以算钱一律以 API 返回的 usage 为准。")
    note(f"同一个意思：中文 {len(TEXT_ZH)} 个字 ≈ {zh} token，英文 {len(TEXT_EN)} 个字符 ≈ {en} token。")
    say("   中英文谁更省 token 取决于具体模型的 tokenizer，别凭印象，拿你自己的模型测一次。")
    note(f"只是多带了 2 个工具定义，输入就多了 {with_tools - zh} token —— 工具定义每次请求都要发、都要付钱。")

    usage_zh = usages[1]
    cached = (getattr(usage_zh.prompt_tokens_details, "cached_tokens", 0) or 0) if usage_zh.prompt_tokens_details else 0
    price = PRICES["default"]
    cost = estimate_cost(Usage(usage_zh.prompt_tokens, 200, cached), "default")
    say()
    say(f"按 agentkit/pricing.py 里的示例单价 {price}（美元 / 百万 token，占位值，不是任何厂商的真实价格）：")
    say(f"   一次\"中文需求\"请求 = {usage_zh.prompt_tokens} 输入 + 假设 200 输出 ≈ ${cost:.6f}；每天 10 万次 ≈ ${cost * 100_000:,.2f}")
    say(f"   输出单价是输入的 {price[1] / price[0]:.0f} 倍（示例值）；本次 cached_tokens = {cached}（缓存通常要求前缀足够长，短请求不会命中）。")


# ════════════════════════════════════════════════════════════════════ 第 2 节：采样与非确定性

NAME_PROMPT = "给一家开在写字楼一楼的咖啡馆起一个中文名字。只输出名字本身，不要标点和解释。"


def softmax(logits: dict[str, float], temperature: float) -> dict[str, float]:
    exps = {k: math.exp(v / temperature) for k, v in logits.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


async def section_2_sampling(b: Backend) -> None:
    banner("第 2 节  采样与非确定性：temperature 到底改变了什么")
    say("原理（纯本地计算）：模型每一步先给所有候选 token 打分（logits），再按概率抽一个。")
    say("假设\"起名字\"的第一步只有 5 个候选，看 temperature 如何改变抽中的概率：")
    logits = {"拾光": 2.0, "云朵": 1.6, "转角": 1.2, "楼下": 0.8, "慢慢": 0.3}
    say(f"   {'候选':<6}" + "".join(f"{'T=' + str(t):>9}" for t in (0.2, 0.7, 1.0, 1.5)))
    tables = {t: softmax(logits, t) for t in (0.2, 0.7, 1.0, 1.5)}
    for k in logits:
        say(f"   {k:<6}" + "".join(f"{tables[t][k]:>9.1%}" for t in tables))
    probs = tables[1.0]
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    nucleus, acc = [], 0.0
    for k, p in ranked:  # top_p：从高到低累加，凑够 p 就停，只在这个"核"里抽
        nucleus.append(k)
        acc += p
        if acc >= 0.7:
            break
    say(f"   T=1.0 时再加 top_p=0.7：只在累计概率凑够 70% 的候选 {nucleus} 里抽，长尾候选直接出局。")
    rng = random.Random(7)
    samples = [rng.choices(list(probs), weights=list(probs.values()))[0] for _ in range(6)]
    say(f"   T→0 相当于每次都选最高分的\"{ranked[0][0]}\"；T=1.0 抽 6 次（随机种子 7）：{samples}")

    say()
    say(f"实测：同一个问题 × temperature 0 / 1 各 3 次。问题：{NAME_PROMPT}")
    messages = [{"role": "user", "content": NAME_PROMPT}]

    async def ask(temperature: float | None) -> str:
        kwargs = {"messages": messages} if temperature is None else {"messages": messages, "temperature": temperature}
        r = await b.create(**kwargs)
        return (r.choices[0].message.content or "").strip()

    try:
        results = await gather_limited([lambda t=t: ask(t) for t in (0, 0, 0, 1, 1, 1)], concurrency(6))
    except Exception as e:  # noqa: BLE001
        say(f"⚠️  这个模型 / 网关不接受 temperature 参数：{str(e)[:160]}")
        say("   推理模型常见这种情况（有的直接报 400，有的静默忽略）。改用默认参数跑 3 次：")
        results = await gather_limited([lambda: ask(None) for _ in range(3)], concurrency(3))
        say(f"   默认参数 × 3：{results}（{len(set(results))} 种不同结果）")
        return
    t0, t1 = results[:3], results[3:]
    say(f"   temperature=0 × 3：{t0}  → {len(set(t0))} 种不同结果")
    say(f"   temperature=1 × 3：{t1}  → {len(set(t1))} 种不同结果")
    if len(set(t0)) > 1:
        note("temperature=0 也给出了不同结果 —— 这正是本节的重点：0 不等于确定。")
    elif len(set(t1)) == 1:
        note("这次 6 个结果都一样。别急着下结论：有的网关 / 推理模型会静默忽略 temperature，而且 3 次样本太少。")
    else:
        note("temperature=0 这次一致，但这不是保证：服务端批处理、硬件差异、模型悄悄升级都会让它变。")
    say("   对 Agent 的意义：一次跑通不代表每次都能跑通。测试用剧本（ScriptedLLM），评估要多次采样看通过率（第 11 课）。")


# ════════════════════════════════════════════════════════════════════ 第 3 节：Function calling

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询一个城市当前的气温（摄氏度）和天气状况。",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市中文名，例如：北京"}},
            "required": ["city"],
        },
    },
}
EXECUTED: list[str] = []  # 记录 get_weather 真正被执行的次数 —— 用来证明"模型不执行工具"


def get_weather(city: str) -> str:
    EXECUTED.append(city)
    data = {"北京": (22, "晴"), "上海": (25, "多云"), "杭州": (24, "小雨")}
    temp, cond = data.get(city, (20, "未知"))
    return json.dumps({"city": city, "temp_c": temp, "condition": cond}, ensure_ascii=False)


async def section_3_function_calling(b: Backend) -> None:
    banner("第 3 节  Function calling 的真实机制：模型只会\"提议\"，执行的是你的代码")
    messages: list[dict] = [
        {"role": "system", "content": "你是出行助手。需要天气数据时调用工具，不要编造。回答不超过两句话。"},
        {"role": "user", "content": "北京现在天气怎么样？出门要带伞吗？"},
    ]
    say("① 把消息 + 工具定义（JSON Schema）发给模型，tool_choice=\"auto\"（让模型自己决定调不调）")
    resp = await b.create(messages=messages, tools=[WEATHER_TOOL], tool_choice="auto")
    msg = resp.choices[0].message
    say(f"② 模型返回（finish_reason={resp.choices[0].finish_reason!r}），原始 JSON：")
    raw = msg.model_dump(exclude_none=True)
    show_json(raw)
    if not msg.tool_calls:
        say("   模型这次没有调用工具，直接回答了。这也是 tool_choice=\"auto\" 的合法结果。")
        return
    tc = msg.tool_calls[0]
    say(f"③ 此刻 get_weather 被执行了 {len(EXECUTED)} 次。模型只是写了一段\"我想调用 {tc.function.name}\"的 JSON。")
    say(f"   注意 arguments 的类型是 {type(tc.function.arguments).__name__}：一段 JSON 文本，要你自己解析和校验。")

    # assistant 消息（带 tool_calls）放回历史。手工转成纯 dict，而不是把 SDK 对象整个塞回去：
    # 历史要能打印、存盘、跨进程恢复，也避免把 SDK 多出来的字段发给不认识它们的网关
    messages.append({
        "role": "assistant",
        "content": msg.content,
        "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                       for c in msg.tool_calls],
    })
    for call in msg.tool_calls:
        args = json.loads(call.function.arguments or "{}")
        result = get_weather(**args)
        say(f"④ 🔧 我们的代码执行 get_weather({args}) → {result}")
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    final = await b.create(messages=messages, tools=[WEATHER_TOOL])
    say(f"⑤ 把结果发回去（第二次请求，{len(messages)} 条消息，完整历史重发），模型据此回答：")
    say(f"   {final.choices[0].message.content}")
    note("整个过程模型调用了 2 次、工具执行了 1 次。每一次都是你的代码在做决定：执不执行、怎么执行、结果要不要给它看。")


# ════════════════════════════════════════════════════════════════════ 第 4 节：流式输出

TICKET_TOOL = {
    "type": "function",
    "function": {
        "name": "create_ticket",
        "description": "创建一张 IT 工单。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "工单标题，20 字以内"},
                "description": {"type": "string", "description": "详细描述，80~150 字，写清现象、影响和紧急程度"},
                "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["title", "description", "priority"],
        },
    },
}


def merge_tool_call_deltas(chunks: list[dict]) -> list[dict]:
    """最小版拼接：按 index 分组，id/name 取第一次出现的值，arguments 按顺序拼接。
    练习 (a) 要你写一个更健壮的版本（交错到达、None 值、缺 id 报错……）；
    agentkit 里的生产版是 agentkit.llm.ToolCallAccumulator（OpenAICompatLLM.stream() 在用）。"""
    calls: dict[int, dict] = {}
    for chunk in chunks:
        for tc in ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls") or []:
            slot = calls.setdefault(tc["index"], {"id": None, "name": None, "arguments": ""})
            fn = tc.get("function") or {}
            slot["id"] = slot["id"] or tc.get("id")
            slot["name"] = slot["name"] or fn.get("name")
            slot["arguments"] += fn.get("arguments") or ""
    return [calls[i] for i in sorted(calls)]


async def open_stream(b: Backend, **kwargs):
    """优先带 stream_options.include_usage（让最后一个 chunk 带上 usage）；网关不认这个参数就去掉重试。
    返回一个 async 迭代器：用 `async for chunk in stream` 逐片读取，每读一片都可能要等网络。"""
    try:
        return await b.create(stream=True, stream_options={"include_usage": True}, **kwargs)
    except Exception as e:  # noqa: BLE001
        say(f"   （网关不支持 stream_options：{str(e)[:80]}，改为不带它重试）")
        return await b.create(stream=True, **kwargs)


async def section_4_streaming(b: Backend) -> None:
    banner("第 4 节  流式输出：TTFT，以及\"工具调用参数是一片一片到达的\"")
    say("4a. 文本流：边生成边显示")
    start = time.perf_counter()
    ttft, n_chunks, usage = None, 0, None
    print("    ", end="")
    async for chunk in await open_stream(b, messages=[{"role": "user", "content": "用三句话介绍杭州，每句不超过 25 个字。"}]):
        n_chunks += 1
        if chunk.usage:
            usage = chunk.usage
        if chunk.choices and chunk.choices[0].delta.content:
            if ttft is None:
                ttft = time.perf_counter() - start
            print(chunk.choices[0].delta.content.replace("\n", "\n    "), end="", flush=True)
    total = time.perf_counter() - start
    print()
    say(f"   TTFT（首 token 时间）= {ttft or 0:.2f}s，全部完成 = {total:.2f}s，共 {n_chunks} 个 chunk")
    note(f"不用流式，用户要盯着空白屏幕等 {total:.1f}s；用流式，{ttft or 0:.1f}s 就能看到第一个字。总耗时并没有变短。")
    if usage:
        say(f"   流的最后一个 chunk 带了 usage：输入 {usage.prompt_tokens}，输出 {usage.completion_tokens}")
    else:
        say("   这个网关没有在流里返回 usage（部分兼容网关不支持 include_usage）。流式时成本统计要另想办法。")

    say()
    say("4b. 单个工具调用的流：模型一边\"写\"参数，参数一边一片一片地到达")
    chunks = await stream_tool_calls(b, VPN_REQUEST, [TICKET_TOOL])
    if not chunks:
        return
    first_fragment = next(
        (tc["function"]["arguments"] for c in chunks if c.get("choices")
         for tc in (c["choices"][0]["delta"].get("tool_calls") or []) if (tc.get("function") or {}).get("arguments")),
        "",
    )
    try:
        json.loads(first_fragment)
        say("   （这次第一片就已经是完整 JSON：服务端把参数攒齐了一次发出。拼接逻辑照样适用。）")
    except json.JSONDecodeError as e:
        say(f"   ❌ 经典错误：拿到第一片就 json.loads({first_fragment[:12]!r}) → JSONDecodeError: {e.msg}")
    show_merged(chunks)

    say()
    say("4c. 并行工具调用的流：两个调用靠 index 区分")
    chunks = await stream_tool_calls(b, VPN_REQUEST + "顺便查一下杭州天气。", [TICKET_TOOL, WEATHER_TOOL])
    if chunks:
        show_merged(chunks)
    note("id 和 name 只在每个调用的第一片出现；多个调用靠 index 区分。分几片、怎么切，由服务端决定，")
    say("   同一个网关，单个调用和并行调用的切法都可能不同。你的代码不能对分片方式做任何假设。")


VPN_REQUEST = "我的笔记本连不上公司 VPN，报错 809，下午要给客户演示，很急。帮我建个工单。"


async def stream_tool_calls(b: Backend, user_text: str, tools: list[dict]) -> list[dict]:
    """发起一次流式请求，边收边打印工具调用分片，返回全部 chunk（转成 dict，和练习 (a) 的输入格式一致）。"""
    start = time.perf_counter()
    chunks: list[dict] = []
    shown: dict[int, int] = {}
    async for chunk in await open_stream(b, messages=[{"role": "user", "content": user_text}], tools=tools):
        chunks.append(chunk.model_dump())
        for tc in (chunk.choices[0].delta.tool_calls or []) if chunk.choices else []:
            shown[tc.index] = shown.get(tc.index, 0) + 1
            if shown[tc.index] <= 5:  # 每个调用只打印前 5 片，否则会刷屏
                head = f"id={tc.id[:14]}… name={tc.function.name}" if tc.id else "（无 id、无 name）"
                frag = tc.function.arguments or ""
                frag = repr(frag[:40] + "…") if len(frag) > 40 else repr(frag)
                say(f"   +{time.perf_counter() - start:5.2f}s index={tc.index} {pad(head, 38)} arguments 片段={frag}")
            elif shown[tc.index] == 6:
                say(f"   ……index={tc.index} 后面还有更多分片")
    if not shown:
        say("   模型这次没有调用工具（或者网关不支持流式工具调用），跳过拼接演示。")
        return []
    say("   " + "，".join(f"index={i} 的参数分了 {n} 片到达" for i, n in sorted(shown.items())))
    return chunks


def show_merged(chunks: list[dict]) -> None:
    say("   ✅ 正确做法：按 index 攒齐所有分片，流结束后再解析 JSON：")
    for call in merge_tool_call_deltas(chunks):
        args = json.loads(call["arguments"] or "{}")
        brief = {k: (v[:24] + "…" if isinstance(v, str) and len(v) > 24 else v) for k, v in args.items()}
        say(f"      {call['name']}({json.dumps(brief, ensure_ascii=False)})  ← id={call['id']}")


# ════════════════════════════════════════════════════════════════════ 第 5 节：结构化输出


class TicketTriage(BaseModel):
    category: Literal["network", "account", "hardware", "software", "other"] = Field(description="问题类别")
    priority: Literal["P1", "P2", "P3", "P4"] = Field(description="P1 最紧急")
    summary: str = Field(max_length=30, description="一句话摘要，不超过 30 个字")
    needs_human: bool = Field(description="是否需要人工介入")


EMAIL = "王经理：我从今早 9 点起就登不上 OA 了，一直提示密码错误，可我昨天刚改过密码。今天下午 3 点前必须在 OA 上提交季度报销，麻烦尽快处理！—— 财务部 李娜"

TRIAGE_SCHEMA = {
    "name": "ticket_triage",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["network", "account", "hardware", "software", "other"]},
            "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
            "summary": {"type": "string", "description": "一句话摘要，不超过 30 个字"},
            "needs_human": {"type": "boolean"},
        },
        "required": ["category", "priority", "summary", "needs_human"],
        "additionalProperties": False,
    },
}


class RecordingLLM:
    """包一层，记录 complete_json 每一次拿到的原始输出，方便看清"校验 → 修复"的过程。"""

    def __init__(self, inner):
        self.inner, self.model, self.outputs = inner, getattr(inner, "model", "?"), []

    async def chat(self, messages, tools=None, **kwargs):
        r = await self.inner.chat(messages, tools, **kwargs)
        self.outputs.append(r.content or "")
        return r


async def section_5_structured_output(b: Backend) -> None:
    banner("第 5 节  结构化输出：让下游代码拿到\"可靠的数据结构\"，而不是一段话")
    prompt = f"对下面这封 IT 求助邮件做分类，输出 JSON。\n\n邮件：{EMAIL}"
    say("5a. 原生结构化输出：response_format = json_schema（strict），由服务端约束解码")
    try:
        r = await b.create(messages=[{"role": "user", "content": prompt}], response_format={"type": "json_schema", "json_schema": TRIAGE_SCHEMA})
        content = r.choices[0].message.content or ""
        say(f"   原始输出：{content}")
        say(f"   Pydantic 校验：{TicketTriage.model_validate_json(content)!r}")
        note("即使服务端保证了 Schema，你的代码也要校验：Schema 表达不了的业务约束（例如摘要长度）、截断、拒答都要兜住。")
    except Exception as e:  # noqa: BLE001
        say(f"   ⚠️  这个模型 / 网关不支持 json_schema 结构化输出：{str(e)[:120]}")
        say("   → 降级到 5b 的做法：提示词里给 Schema + 代码校验 + 把错误发回去让模型修。")

    say()
    say("5b. agentkit.workflows.complete_json：提示词 + Pydantic 校验 + 失败时把错误发回去修复（最多 2 次）")
    rec = RecordingLLM(b.llm)
    result = await complete_json(rec, prompt, TicketTriage)
    for i, out in enumerate(rec.outputs, 1):
        flat = out.replace("\n", " ")
        verdict = "✅ 通过校验" if i == len(rec.outputs) else "❌ 校验失败，错误信息被发回给模型"
        say(f"   第 {i} 次输出：{flat[:110]}{'…' if len(flat) > 110 else ''}")
        say(f"              {verdict}")
    say(f"   最终得到的对象：{result!r}")
    if len(rec.outputs) == 1:
        note("真实模型大多数时候一次就能通过；修复循环是为少数失败准备的保险。离线模式（--offline）演示了修复的全过程。")
    else:
        note(f"一共调用了 {len(rec.outputs)} 次模型。修复有上限，成本可预期；修不好就抛异常，交给上层处理。")


# ════════════════════════════════════════════════════════════════════ 第 6 节：约束解码与 logprobs

LABELS = ("network", "account", "hardware", "software")
CLASSIFY_PROMPT = f"把下面这条 IT 求助分类，只输出一个英文单词（{' / '.join(LABELS)}），不要输出其他任何内容。\n\n{EMAIL}"
CONFIDENCE_THRESHOLD = 0.9  # 低于它就转人工复核。阈值要用标注数据校准，这里只是演示


async def section_6_decoding(b: Backend) -> None:
    banner("第 6 节  约束解码与 logprobs：\"只许说合法的话\"，以及\"它有多确定\"")
    say("6a. 约束解码的原理（纯本地计算，数字是为了演示编的；把每个候选值当成一个 token 是简化）")
    say("   模型要给工单填 priority，Schema 只允许 P1 / P2 / P3 / P4。假设第一步的候选打分如下：")
    logits = {"紧急": 2.6, "P2": 1.4, "P1": 1.1, "high": 0.9, "P3": 0.2}
    allowed = {"P1", "P2", "P3", "P4"}
    probs = softmax(logits, 1.0)
    kept = {k: v for k, v in probs.items() if k in allowed}
    total = sum(kept.values())
    say(f"   {pad('候选', 8)}{pad('原始概率', 12, True)}{pad('Schema 允许?', 16, True)}{pad('约束后概率', 14, True)}")
    for k, p in probs.items():
        after = f"{kept[k] / total:.1%}" if k in kept else "0（被屏蔽）"
        say(f"   {pad(k, 8)}{pad(f'{p:.1%}', 12, True)}{pad('是' if k in allowed else '否', 16, True)}{pad(after, 14, True)}")
    free = max(probs, key=probs.get)
    forced = max(kept, key=kept.get)
    say(f"   不加约束，贪心选最高分：{free!r} → 不在枚举里，校验失败")
    say(f"   约束解码：把不合法的候选概率置零、剩下的重新归一化 → 选出 {forced!r}，格式一定合法")
    note(f"模型\"想说\"的是紧急（也就是 P1），约束把它挤到了 {forced}：格式合法，判断却错了。约束管得了格式，管不了内容。")
    say("   complete_json 的修复循环走另一条路：先让它说出\"紧急\"，校验失败后把错误发回去，模型有机会重新想（第 5 节）。")
    say("   代价是多一次调用。真实模型在提示词里看到 Schema 后，概率通常已经集中在合法值上，这种\"挤压\"没这么夸张。")

    say()
    say("6b. 请求 logprobs：让模型给出每个输出 token 的对数概率，当作分类的置信度")
    messages = [{"role": "user", "content": CLASSIFY_PROMPT}]
    try:
        r = await b.create(messages=messages, logprobs=True, top_logprobs=5)
    except Exception as e:  # noqa: BLE001 —— 有的网关 / 推理模型直接拒绝这个参数
        say(f"   ⚠️  这个模型 / 网关拒绝了 logprobs 参数：{str(e)[:140]}")
        show_logprob_fallback()
        return
    content = (r.choices[0].message.content or "").strip()
    lp = r.choices[0].logprobs
    say(f"   模型输出：{content!r}")
    if lp is None or not lp.content:
        say("   ⚠️  请求成功（HTTP 200），但返回里没有 logprobs：这个网关 / 模型静默忽略了它，不报错，只是不生效。")
        show_logprob_fallback()
        return
    first = lp.content[0]
    say(f"   第一个 token {first.token!r} 的候选（top_logprobs）：")
    dist = {}
    for alt in first.top_logprobs:
        p = math.exp(alt.logprob)
        dist[alt.token.strip()] = dist.get(alt.token.strip(), 0.0) + p
        say(f"      {pad(repr(alt.token), 14)} logprob={alt.logprob:7.3f}   概率={p:6.1%}")
    label_probs = {k: v for k, v in dist.items() if k in LABELS}
    top = max(label_probs, key=label_probs.get) if label_probs else content
    conf = label_probs.get(top, 0.0)
    action = "自动路由" if conf >= CONFIDENCE_THRESHOLD else "转人工复核（或升级到更强的模型）"
    say(f"   置信度 = P({top!r}) = {conf:.1%}，阈值 {CONFIDENCE_THRESHOLD:.0%} → {action}")
    note("logprobs 是模型自己算出来的概率，不等于\"答对的概率\"：后训练可能破坏校准（GPT-4 技术报告的观察），阈值要用标注数据定。")


def show_logprob_fallback() -> None:
    say("   降级方案（按可靠程度排序）：")
    say("     ① 换一个支持 logprobs 的接口或模型（很多推理模型不支持）；")
    say("     ② 同一问题采样多次，看答案是否一致（第 2 节的做法，要多花几次调用）；")
    say("     ③ 让模型在 JSON 里自报置信度 —— 最便宜也最不可靠，只能当参考。")
    say("   离线模式（--offline）演示了拿到 logprobs 之后怎么算置信度、怎么按阈值分流。")


# ════════════════════════════════════════════════════════════════════ 离线模式：假的 OpenAI 客户端
#
# 数字取自一次真实运行（本课程的 OpenAI 兼容网关，模型 gpt-5.5，2026-09），让离线输出和真实情况接近。
# 离线模式的所有"模型输出"都来自剧本，不代表模型的真实行为。


class FakeOpenAIClient:
    """假装自己是 openai.AsyncOpenAI：await chat.completions.create(**kwargs) 返回真正的 SDK 对象，所以上面的代码一个字都不用改。

    responder(kwargs) 返回：dict（非流式 ChatCompletion 的 message/usage 描述）或 list[dict]（流式 chunk 描述）。
    """

    def __init__(self, responder: Callable[[dict], Any]):
        from types import SimpleNamespace

        self._responder = responder
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        from openai.types.chat import ChatCompletion, ChatCompletionChunk

        spec = self._responder(kwargs)
        if kwargs.get("stream"):
            return self._stream(spec, ChatCompletionChunk)
        message = {"role": "assistant", "content": spec.get("content")}
        if spec.get("tool_calls"):
            message["tool_calls"] = spec["tool_calls"]
        prompt, completion = spec.get("usage", (20, 10))
        return ChatCompletion.model_validate({
            "id": "chatcmpl-offline", "object": "chat.completion", "created": 0, "model": kwargs["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": spec.get("finish_reason", "stop"),
                         "logprobs": spec.get("logprobs")}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
                      "prompt_tokens_details": {"cached_tokens": 0}},
        })

    @staticmethod
    async def _stream(chunks: list[dict], cls):
        for delay, delta, finish in chunks:
            await asyncio.sleep(delay)  # 模拟网络分片到达的间隔（等待时让出事件循环）
            chunk = {"id": "chatcmpl-offline", "object": "chat.completion.chunk", "created": 0, "model": "offline"}
            if delta is None:  # include_usage 的最后一个 chunk：choices 为空，只带 usage
                prompt, completion = finish
                chunk.update(choices=[], usage={"prompt_tokens": prompt, "completion_tokens": completion,
                                                "total_tokens": prompt + completion})
            else:
                chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
            yield cls.model_validate(chunk)


def _offline_responder(kwargs: dict) -> Any:
    messages = kwargs.get("messages") or []
    last = messages[-1].get("content") or "" if messages else ""
    tools = kwargs.get("tools") or []

    # 第 1 节：usage 取自真实运行记录
    if messages and messages[0].get("content") == SYSTEM_OK:
        usage = {".": 321, TEXT_ZH: 355, TEXT_EN: 357}[last] + (118 if tools else 0)
        return {"content": "OK", "usage": (usage, 5)}

    # 第 2 节：temperature=0 时总是同一个名字；temperature=1 时轮换几个候选，模拟"抽样" 
    if last == NAME_PROMPT:
        names = ["拾光咖啡", "云朵咖啡", "转角咖啡", "楼下咖啡"]
        _offline_responder.calls = getattr(_offline_responder, "calls", 0) + 1
        if kwargs.get("temperature") == 0:
            return {"content": "拾光咖啡"}
        return {"content": names[_offline_responder.calls % len(names)]}

    # 第 3 节：第一次请求 → 工具调用；带着 tool 结果的第二次请求 → 最终回答
    if tools and tools[0]["function"]["name"] == "get_weather" and not kwargs.get("stream"):
        if messages[-1]["role"] == "tool":
            return {"content": "北京现在晴，22°C，不用带伞。", "usage": (180, 20)}
        return {
            "content": None,
            "finish_reason": "tool_calls",
            "tool_calls": [{"id": "call_offline_01", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}],
            "usage": (120, 18),
        }

    # 第 4 节：流式。分片方式模仿本课程网关的真实表现：
    #   单个工具调用 → 第一片带 id/name、arguments 为空，之后每片只有几个字符；
    #   并行工具调用 → 每个调用的参数一次性在一片里给出。
    if kwargs.get("stream"):
        if not tools:
            text = "杭州以西湖闻名天下。\n这里历史悠久，文化丰厚。\n山水秀美，生活宜人。"
            out = [(0.6, {"role": "assistant", "content": ""}, None)]
            out += [(0.02, {"content": text[i:i + 2]}, None) for i in range(0, len(text), 2)]
            return out + [(0.0, {}, "stop"), (0.0, None, (316, 55))]
        ticket_args = json.dumps({
            "title": "笔记本VPN报错809无法连接",
            "description": "用户笔记本电脑连接公司 VPN 时报错 809，无法访问内网资源。今天下午需要给客户做演示，影响较大，请尽快排查 VPN 配置与网络端口。",
            "priority": "high",
        }, ensure_ascii=False)
        out = [(0.9, {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_offline_t1", "type": "function",
                                                           "function": {"name": "create_ticket", "arguments": ""}}]}, None)]
        if len(tools) == 1:
            out += [(0.01, {"tool_calls": [{"index": 0, "function": {"arguments": ticket_args[i:i + 3]}}]}, None)
                    for i in range(0, len(ticket_args), 3)]
            return out + [(0.0, {}, "tool_calls")]
        out += [(0.01, {"tool_calls": [{"index": 0, "function": {"arguments": ticket_args}}]}, None)]
        out += [(0.01, {"role": "assistant", "tool_calls": [{"index": 1, "id": "call_offline_w1", "type": "function",
                                                            "function": {"name": "get_weather", "arguments": ""}}]}, None)]
        out += [(0.01, {"tool_calls": [{"index": 1, "function": {"arguments": '{"city":"杭州"}'}}]}, None)]
        return out + [(0.0, {}, "tool_calls")]

    # 第 6b 节：logprobs。本课程网关实测会静默忽略 logprobs（返回 None）；
    # 离线剧本模拟一个支持它的接口（如 OpenAI 官方接口），数值是示意用的，不是任何模型的真实输出
    if last == CLASSIFY_PROMPT:
        alts = [("account", -0.12), ("software", -2.30), ("network", -4.80), ("hardware", -7.20), ("acc", -9.10)]
        top = [{"token": t, "logprob": lp, "bytes": list(t.encode())} for t, lp in alts]
        spec = {"content": "account", "usage": (160, 1)}
        if kwargs.get("logprobs"):
            spec["logprobs"] = {"content": [{**top[0], "top_logprobs": top[:kwargs.get("top_logprobs", 0)]}], "refusal": None}
        return spec

    # 第 5a 节：原生结构化输出
    if kwargs.get("response_format"):
        return {"content": '{"category":"account","priority":"P1","summary":"OA 登录提示密码错误，急需报销","needs_human":true}'}

    return {"content": "（离线剧本没有覆盖这个请求）"}


def offline_backend() -> Backend:
    # 第 5b 节 complete_json 走 agentkit 的 LLM 接口：第一次故意给出不合规的输出，演示"校验 → 修复"
    llm = ScriptedLLM([
        reply('好的，分类结果如下：\n```json\n{"category": "账号", "priority": "紧急", "summary": "OA 登录不上", "needs_human": true}\n```'),
        reply('{"category": "account", "priority": "P1", "summary": "OA 登录提示密码错误，急需报销", "needs_human": true}'),
    ])
    return Backend(FakeOpenAIClient(_offline_responder), "offline-scripted", llm)


# ════════════════════════════════════════════════════════════════════ main


async def main() -> None:
    b = offline_backend() if OFFLINE else online_backend()
    mode = "离线模式：剧本 + 模拟数据（数字取自一次真实运行记录）" if OFFLINE else f"真实模型：{b.model}"
    print(f"第 01 课 Demo · LLM 与 Agent 开发必备知识 · {mode}")
    try:
        for section in (section_1_tokens, section_2_sampling, section_3_function_calling, section_4_streaming,
                        section_5_structured_output, section_6_decoding):
            await run_section(lambda: section(b))
    finally:
        await b.aclose()  # 关掉 HTTP 连接池（脚本退出前释放连接，避免 "Unclosed client" 警告）
    banner("小结")
    say("1. Token：按 token 计费和限流；估算只用于预算，算钱看 usage；工具定义和隐藏指令都算输入。")
    say("2. 采样：同一输入可能不同输出，temperature=0 也不例外 → 测试用剧本，评估看通过率。")
    say("3. Function calling：模型只输出\"想调用什么\"的 JSON，执行权永远在你的代码手里。")
    say("4. 流式：降低的是感知延迟（TTFT）；工具参数分片到达，必须按 index 攒齐再解析。")
    say("5. 结构化输出：优先用原生 Schema 约束；无论如何都要校验，失败就修复或大声失败。")
    say("6. 约束解码只保证格式；logprobs 能当置信度，但要校准，而且不是每个网关都支持。")


if __name__ == "__main__":
    asyncio.run(main())
