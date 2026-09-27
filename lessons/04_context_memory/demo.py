"""第 04 课 Demo：上下文工程与长期记忆。

    python lessons/04_context_memory/demo.py            # 真实模型（读取 .env）
    python lessons/04_context_memory/demo.py --offline  # 离线剧本，无需 API key

你会看到 5 个实验：
  1. 一个采购 Agent 第 7 轮时，上下文里都装了什么、各占多少
  2. 同一段长对话：SlidingWindow（滑动窗口）vs SummarizingCompactor（摘要压缩）
  3. 两种"天真截断"如何制造孤立 tool 消息，以及 API 会怎么回应
  4. 长期记忆：会话 1 记住偏好，会话 2（全新历史）通过 recall 想起来
  5. 记忆隔离与"被遗忘权"：换一个用户 / 换一个租户检索不到；删除后彻底消失
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from typing import Annotated

from pydantic import Field

from agentkit import (
    Agent,
    LLMError,
    MemoryStore,
    ScriptedLLM,
    SlidingWindow,
    SummarizingCompactor,
    call_tool,
    default_llm,
    estimate_tokens,
    memory_tools,
    reply,
    tool,
)
from agentkit.context import find_summary

# ====================================================================== 打印小工具


def section(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def note(text: str) -> None:
    for line in text.strip().splitlines():
        print(f"  💡 {line.strip()}")


def pad(text: str, width: int) -> str:
    """按终端显示宽度补空格（一个汉字占 2 列），让中英混排的表格对齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def preview(m: dict, width: int = 46) -> str:
    if m.get("tool_calls"):
        text = "调用工具 " + ", ".join(f"{c['function']['name']}({c['function']['arguments']})" for c in m["tool_calls"])
    else:
        text = (m.get("content") or "").replace("\n", " ")
    return text if len(text) <= width else text[:width] + "…"


def show_messages(msgs: list[dict], indent: str = "    ") -> None:
    for m in msgs:
        role = m["role"] + (f"[{m['tool_call_id']}]" if m["role"] == "tool" else "")
        print(f"{indent}{pad(role, 16)} {estimate_tokens([m]):>5} tok  {preview(m)}")


# ====================================================================== 场景：企业采购助手

SYSTEM_PROMPT = (
    "你是星辰科技的企业采购助手，可以查询商品目录、供应商资质和库存，并协助员工下单。"
    "规则：1) 严格遵守员工提出的预算和发票要求；2) 下单前复述关键条件请员工确认；"
    "3) 信息不足时直接说明，不要编造。"
)


@tool
def search_products(
    keyword: Annotated[str, Field(description="商品关键词，如'27寸 4K 显示器'")],
    max_price: Annotated[float | None, Field(description="单价上限（元），不限则不填")] = None,
) -> str:
    """在公司采购目录中搜索商品，返回商品列表（SKU、名称、单价、供应商、规格）。"""
    return "（本 Demo 只用它生成工具定义，不会真的执行）"


@tool
def get_supplier(supplier_id: Annotated[str, Field(description="供应商 ID，如 S-002")]) -> str:
    """查询供应商资质：能否开增值税专用发票、历史评分、合作年限。"""
    return ""


@tool
def check_stock(sku: Annotated[str, Field(description="商品 SKU")]) -> str:
    """查询某个 SKU 的实时库存和预计发货时间。"""
    return ""


@tool(risk="write")
def create_order(
    sku: Annotated[str, Field(description="商品 SKU")],
    quantity: Annotated[int, Field(ge=1, le=100, description="数量")],
    invoice_title: Annotated[str, Field(description="发票抬头")],
) -> str:
    """创建采购订单（写操作，需员工确认后才能调用）。"""
    return ""


TOOLS = [search_products, get_supplier, check_stock, create_order]
KEY_CONSTRAINT = "星辰科技有限公司"  # 用户第 1 句话里提出的发票抬头，只在那一句里出现过


def _products(keyword: str, skus: list[int], only_4k: bool = False) -> str:
    items = [
        {
            "sku": f"MON-{i:03d}",
            "name": f"{keyword} 型号{chr(65 + i)}",
            "price": 1599 + i * 180,
            "supplier_id": f"S-00{i % 4 + 1}",
            "specs": {"尺寸": "27英寸", "分辨率": "3840x2160" if (only_4k or i % 2) else "2560x1440", "面板": "IPS", "刷新率": "60Hz"},
            "description": "广色域专业显示器，支持 Type-C 一线连接和升降旋转支架，适合设计与办公场景。",
        }
        for i in skus
    ]
    return json.dumps({"total": len(items), "items": items}, ensure_ascii=False)


def _call(call_id: str, name: str, **args) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def build_long_conversation() -> list[dict]:
    """一段真实感的 7 轮采购对话：关键约束只在第 1 句出现，后面夹着大量工具结果。"""
    supplier = {
        "supplier_id": "S-002", "name": "华东视讯科技", "vat_special_invoice": True, "rating": 4.7,
        "years": 6, "orders_last_year": 214, "return_rate": "0.8%",
        "certificates": ["ISO9001", "3C 认证", "一般纳税人资格"], "address": "上海市闵行区某科技园 3 号楼",
        "notes": "支持月结，账期 30 天；大客户可提供上门安装与 3 年质保。",
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"我要给设计部采购 3 台显示器。硬性要求：单价不超过 2500 元，必须能开增值税专用发票，发票抬头「{KEY_CONSTRAINT}」。"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c1", "search_products", keyword="显示器", max_price=2500)]},
        {"role": "tool", "tool_call_id": "c1", "content": _products("显示器", [0, 1, 2, 3, 4])},
        {"role": "assistant", "content": "找到 5 款符合预算的显示器，推荐前两款：MON-000（1599 元）和 MON-001（1779 元，4K）。"},
        {"role": "user", "content": "第二款的供应商靠谱吗？"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c2", "get_supplier", supplier_id="S-002")]},
        {"role": "tool", "tool_call_id": "c2", "content": json.dumps(supplier, ensure_ascii=False)},
        {"role": "assistant", "content": "S-002 华东视讯评分 4.7、合作 6 年，是一般纳税人，可以开专票。"},
        {"role": "user", "content": "第一款和第二款库存够吗？"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c3", "check_stock", sku="MON-000"), _call("c4", "check_stock", sku="MON-001")]},
        {"role": "tool", "tool_call_id": "c3", "content": '{"sku": "MON-000", "stock": 12, "ship_in_days": 1}'},
        {"role": "tool", "tool_call_id": "c4", "content": '{"sku": "MON-001", "stock": 5, "ship_in_days": 2}'},
        {"role": "assistant", "content": "MON-000 库存 12 台（次日发货），MON-001 库存 5 台（2 天内发货），都够 3 台。"},
        {"role": "user", "content": "设计部说最好是 27 寸 4K 的，还有别的推荐吗？"},
        {"role": "assistant", "content": None, "tool_calls": [_call("c5", "search_products", keyword="27寸 4K 显示器", max_price=2500)]},
        {"role": "tool", "tool_call_id": "c5", "content": _products("27寸4K显示器", [1, 3, 5], only_4k=True)},
        {"role": "assistant", "content": "27 寸 4K 的有 MON-001、MON-003（2139 元）、MON-005（2499 元），MON-001 性价比最高。"},
        {"role": "user", "content": "好，就按我最开始说的要求，帮我下单 MON-001。"},
    ]


def text_of(msgs: list[dict]) -> str:
    return "\n".join((m.get("content") or "") for m in msgs)


# ====================================================================== 1. 上下文构成


def demo_anatomy(conv: list[dict]) -> None:
    section("实验 1：上下文里到底装了什么？（采购 Agent 第 7 轮，发给模型前的一刻）")
    schemas = [t.schema() for t in TOOLS]
    parts = {
        "system 提示词": estimate_tokens([conv[0]]),
        "工具定义（schema）": estimate_tokens([{"role": "system", "content": json.dumps(schemas, ensure_ascii=False)}]),
        "用户消息": estimate_tokens([m for m in conv if m["role"] == "user"]),
        "助手回复/调用": estimate_tokens([m for m in conv if m["role"] == "assistant"]),
        "工具结果": estimate_tokens([m for m in conv if m["role"] == "tool"]),
    }
    total = sum(parts.values())
    for name, tok in parts.items():
        bar = "█" * round(tok / total * 40)
        print(f"  {pad(name, 20)}{tok:>6} tok  {tok / total:>5.1%}  {bar}")
    print(f"  {pad('合计', 20)}{total:>6} tok")
    note(
        """工具结果通常是最大的一块，而且大多是"用过就没用了"的原始数据 —— 这正是清理/压缩的重点对象。
        工具定义每次调用都要发送，工具越多越贵；estimate_tokens(messages) 默认不包含这一块，做预算时别忘了它。
        模型是无状态的：每一步都要把上面这一整坨重新发一遍。20 步的 Agent = 这些内容被重复计费 20 次。"""
    )


# ====================================================================== 2. 滑动窗口 vs 摘要压缩


class RecordingLLM:
    """包装任意 LLM，记下它最后一次的原始输出 —— 用来看"模型本来写了多长的摘要"。"""

    def __init__(self, llm):
        self.llm, self.model, self.last_output = llm, llm.model, ""

    def chat(self, messages, tools=None, **kwargs):
        response = self.llm.chat(messages, tools, **kwargs)
        self.last_output = response.content or ""
        return response


def run_compactor(label: str, llm, conv: list[dict], budget: int, max_summary_chars: int) -> None:
    recorder = RecordingLLM(llm)
    compactor = SummarizingCompactor(recorder, max_tokens=budget, keep_recent_tokens=budget // 3,
                                     max_summary_chars=max_summary_chars)
    t0 = time.time()
    compacted = compactor.apply(conv)
    after = estimate_tokens(compacted)
    print(f"\n  {label}\n    → {len(compacted)} 条，约 {after} tokens（摘要调用耗时 {time.time() - t0:.1f}s），保留的消息：")
    show_messages(compacted)

    raw = recorder.last_output.strip()
    print(f"\n    模型写的摘要原文：{len(raw)} 字（上限 max_summary_chars={max_summary_chars}"
          f"{'，超出部分会被代码硬截断' if len(raw) > max_summary_chars else ''}）")
    print(f"    system 消息原封不动？{'✅ 是（摘要是 system 之后的一条独立消息，提示词缓存不受影响）' if compacted[0] == conv[0] else '❌ 被改写了'}")
    summary = find_summary(compacted)
    if summary is None:
        print("    ⚠️ 结果里找不到摘要消息：摘要太长，拼上后仍超预算 → 触发滑动窗口兜底 → 摘要自己被挤掉了！"
              "\n       而原文只留了 keep_recent_tokens 以内的最近几条，结果比 ① 纯滑动窗口保留得还少。")
    else:
        lines = summary.splitlines()
        print(f"    find_summary() 取出的摘要（共 {len(lines)} 行）：")
        for line in lines[:8]:
            print(f"    │ {line[:90]}")
        if len(lines) > 8:
            print(f"    │ ……（省略 {len(lines) - 8} 行）")
        if summary.endswith("（摘要已截断）"):
            print("    ✂️ 模型没有遵守长度要求，末尾被代码硬截断 —— 截断保留开头，所以提示词要让最重要的约束写在最前面。")
    kept = KEY_CONSTRAINT in text_of(compacted)
    print(f"    发票抬头「{KEY_CONSTRAINT}」还在吗？{'✅ 在' if kept else '❌ 丢了'}"
          f"｜在预算 {budget} 以内吗？{'✅' if after <= budget else '❌'}")


def demo_compaction(conv: list[dict], verbose_llm, brief_llm) -> None:
    section("实验 2：同一段长对话，SlidingWindow vs SummarizingCompactor")
    before = estimate_tokens(conv)
    budget = before * 45 // 100
    print(f"  原始对话：{len(conv)} 条消息，约 {before} tokens；把预算设为 {budget} tokens\n")

    window = SlidingWindow(max_tokens=budget).apply(conv)
    print(f"  ① SlidingWindow(max_tokens={budget}) → {len(window)} 条，约 {estimate_tokens(window)} tokens，保留的消息：")
    show_messages(window)
    kept = KEY_CONSTRAINT in text_of(window)
    print(f"\n    用户第 1 句提出的发票抬头「{KEY_CONSTRAINT}」还在吗？{'✅ 在' if kept else '❌ 丢了'}")

    run_compactor("② SummarizingCompactor，但几乎不限摘要长度（max_summary_chars=100000，重现本课第一版踩过的坑）",
                  verbose_llm, conv, budget, max_summary_chars=100_000)
    run_compactor("③ SummarizingCompactor，摘要限长 200 字（提示词要求 + 代码硬截断）",
                  brief_llm, conv, budget, max_summary_chars=200)
    note(
        """滑动窗口：零成本、零延迟，但"最早说的硬性要求"最先被丢 —— 而它往往最重要。
        摘要压缩：多花一次模型调用（还会增加这一轮的延迟），换来要点保留；但摘要可能漏掉、写错，或者写得太长。
        不限长时，模型常把工具原始数据抄进摘要。兜底的滑动窗口能保证"不超预算"，却保证不了"信息还在"——
        所以长度预算才是根本：提示词里要求限长，代码里再硬截断（agentkit 的 max_summary_chars）。
        摘要放在 system 之后、用标签包起来并注明"仅供参考"：它的原料含有不可信的工具输出，不能升级成系统指令。"""
    )


# ====================================================================== 3. 孤立 tool 消息


def protocol_problems(msgs: list[dict]) -> list[str]:
    """检查 tool 消息配对协议，返回发现的问题（空列表 = 没问题）。"""
    problems, pending = [], set()
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            if m["tool_call_id"] not in pending:
                problems.append(f"第 {i} 条 tool 消息（{m['tool_call_id']}）前面没有对应的 tool_calls → 孤立")
            pending.discard(m["tool_call_id"])
        else:
            if pending:
                problems.append(f"第 {i} 条之前，工具调用 {sorted(pending)} 没有结果 → 有调用没结果")
            pending = {c["id"] for c in m.get("tool_calls") or []} if m["role"] == "assistant" else set()
    if pending:
        problems.append(f"结尾处工具调用 {sorted(pending)} 没有结果")
    return problems


def demo_orphans(llm, offline: bool) -> None:
    section("实验 3：孤立 tool 消息 —— 为什么天真的截断会让 API 返回 400")
    full = [
        {"role": "system", "content": "你是客服助手，回答不超过 30 字。"},
        {"role": "user", "content": "查一下订单 A1001"},
        {"role": "assistant", "content": None, "tool_calls": [_call("call_9", "get_order", order_id="A1001")]},
        {"role": "tool", "tool_call_id": "call_9", "content": '{"order_id": "A1001", "status": "已发货", "eta": "9月29日"}'},
        {"role": "assistant", "content": "订单 A1001 已发货。"},
        {"role": "user", "content": "那预计哪天到？"},
    ]
    tools = [{"type": "function", "function": {"name": "get_order", "description": "查询订单",
              "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]
    cases = {
        "错误 A：只保留 system + 最后 3 条（切在了块中间）": [full[0]] + full[3:],
        "错误 B：为了省 token，把工具结果消息直接删掉": [m for m in full if m["role"] != "tool"],
    }
    for title, msgs in cases.items():
        print(f"\n  {title}")
        show_messages(msgs, indent="      ")
        for p in protocol_problems(msgs):
            print(f"      ⚠️ {p}")
        if offline:
            print("      （离线模式不调用 API。OpenAI 官方接口对这两种情况都会返回 400 invalid_request_error）")
            continue
        t0 = time.time()
        try:
            r = llm.chat(msgs, tools=tools)
            answer = (r.content or "").strip()[:60] or "（空回答）"
            print(f"      API 没有报错（{time.time() - t0:.1f}s），模型回答：{answer}")
            print("      ⚠️ 没报错不代表没问题：有些网关会静默丢弃/忽略这条消息，信息悄悄丢了，比 400 更难排查。")
        except LLMError as e:
            print(f"      API 返回错误 status={e.status_code}（{time.time() - t0:.1f}s）：{str(e)[:150]}")
    note(
        """正确做法：按"块"截断（agentkit/context.py 的 split_blocks），或者只替换工具结果的 content、不删消息（本课练习 2）。
        不同网关的报错文字不同、宽严不同，所以别依赖 API 帮你兜底 —— 自己保证协议正确。"""
    )


# ====================================================================== 4 & 5. 长期记忆


MEMORY_PROMPT = """你是公司内部的员工助手，回答简洁，不超过 5 行。
你有长期记忆工具：
- 用户明确表达了以后仍然有用的个人偏好或事实（饮食禁忌、称呼、常用语言等）时，调用 remember 记下来；
  不要记录密码、证件号、银行卡等敏感信息。
- 当回答可能依赖用户的个人偏好时，先调用 recall 检索。recall 是关键词匹配，query 里请多写几个相关词
  （包括同义词），例如"饮食 忌口 过敏 素食 口味"。"""


def remember_script() -> list:
    # 离线剧本复现了真实模型的一次典型行为：把一句话拆成两条记忆分别保存
    return [call_tool("remember", fact="用户吃素。"), call_tool("remember", fact="用户对花生过敏。"),
            reply("好的，已经记住：你吃素、对花生过敏。以后推荐餐饮会注意。")]


def recall_script(query: str) -> list:
    def answer(messages):  # 根据 recall 的真实返回结果作答（离线也体现真实的数据流）
        memory = messages[-1]["content"]
        if "素" in memory:
            return reply("考虑到你吃素、对花生过敏，推荐：香菇青菜、番茄豆腐煲、菌菇炒时蔬。")
        if "花生" in memory:
            return reply("考虑到你对花生过敏，推荐：清蒸鲈鱼、黑椒牛柳、蒜蓉西兰花（都不含花生）。")
        return reply("推荐：宫保鸡丁、清蒸鲈鱼、干锅花菜。有忌口可以告诉我。")

    return [call_tool("recall", query=query), answer]


def scope_items(store: MemoryStore, tenant: str, uid: str) -> list:
    return [i for i in store.items if i.tenant_id == tenant and i.user_id == uid]


def check_recall(result, store: MemoryStore, tenant: str, uid: str) -> None:
    """诊断：这个用户存了哪些记忆，这次 recall 实际召回了哪些。"""
    recalled = "\n".join(m["content"] for m in result.messages if m["role"] == "tool")
    if "recall" not in result.tools_called():
        print("    ⚠️ 这次模型没有调用 recall —— 按需检索依赖模型\"想起来去查\"，这本身就是一个失败点。")
        return
    missed = [i.text for i in scope_items(store, tenant, uid) if i.text not in recalled]
    if missed:
        print(f"    ⚠️ 漏召回：{missed} 存在记忆库里，但这次没被检索到！")
        print("       原因：MemoryStore 按字面关键词（中文二元组）匹配，查询和记忆措辞不同就搜不到"
              "（例如 query 里的\"素食\"匹配不上记忆里的\"吃素\"）。")
        print("       后果：模型拿着不完整的记忆，自信地给出了可能错误的建议 —— 检索质量直接决定回答质量。")
    else:
        print("    ✅ 该用户的全部记忆都被召回了。")


def show_tool_flow(result) -> None:
    for m in result.messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                print(f"    → 模型调用 {c['function']['name']}({c['function']['arguments']})")
        elif m["role"] == "tool":
            print(f"    ← 工具返回：{m['content'][:80]}")
    print(f"    🤖 回答：{(result.output or '').strip()}")


def demo_memory(make_llm, offline: bool) -> MemoryStore:
    section("实验 4：长期记忆 —— 会话 1 记住偏好，会话 2 用全新的历史想起来")
    store = MemoryStore()  # 生产中是数据库/向量库；传 path= 可以落盘到 JSON 文件
    alice = {"tenant_id": "acme", "user_id": "alice"}

    def agent(script):
        return Agent(make_llm(script), memory_tools(store), system_prompt=MEMORY_PROMPT, name="assistant")

    q1 = "请记住：我吃素，而且对花生过敏。"
    print(f"\n  【会话 1】tenant=acme user=alice：{q1}")
    r1 = agent(remember_script()).run(q1, metadata=alice)
    show_tool_flow(r1)
    print("\n  记忆库现在的内容（注意每条都带着 tenant_id / user_id）：")
    for item in store.items:
        print(f"    id={item.id} tenant={item.tenant_id} user={item.user_id} text={item.text!r}")

    q2 = "下周五部门团建午餐，帮我推荐 3 道菜。"
    print(f"\n  【会话 2】全新的对话（history 为空），tenant=acme user=alice：{q2}")
    r2 = agent(recall_script("饮食 忌口 过敏 素食 口味")).run(q2, metadata=alice)
    show_tool_flow(r2)
    check_recall(r2, store, "acme", "alice")
    note(
        """会话 2 的 messages 里根本没有会话 1 的内容 —— 是 recall 工具从外部存储里"取回"了记忆。
        tenant_id / user_id 来自 metadata → ToolContext，模型无法通过参数伪造身份去读别人的记忆。
        生产中用向量检索 + 关键词的混合检索提高召回；用户记忆很少时，也可以在会话开始时全部注入，干脆不检索。"""
    )

    section("实验 5：记忆隔离与被遗忘权")
    bob = {"tenant_id": "acme", "user_id": "bob"}
    print(f"\n  【同租户的另一个用户】tenant=acme user=bob：{q2}")
    r3 = agent(recall_script("饮食 忌口 过敏 素食 口味")).run(q2, metadata=bob)
    show_tool_flow(r3)

    alice_items = scope_items(store, "acme", "alice")
    query = " ".join(i.text for i in alice_items)
    print(f"\n  直接查询存储层（绕过模型），用 alice 记忆的原文当关键词（保证能命中）：{query!r}")
    for tenant, uid in [("acme", "alice"), ("acme", "bob"), ("globex", "alice")]:
        hits = store.search(tenant, uid, query)
        label = "、".join(h.text for h in hits) or "（空）"
        print(f"    search(tenant={tenant!r:<9} user={uid!r:<8}) → {label}")
    print("    ↑ globex 租户里也有一个叫 alice 的用户，但她和 acme 的 alice 是两个人，记忆必须互相不可见。")

    print(f"\n  alice 行使被遗忘权：删除她的全部 {len(alice_items)} 条记忆")
    for item in alice_items:
        print(f"    forget('acme', 'alice', {item.id!r}) → {store.forget('acme', 'alice', item.id)}")
    print(f"  再次检索 → {[h.text for h in store.search('acme', 'alice', query)] or '（空）'}")
    note(
        """隔离必须在存储层用过滤条件强制执行（where tenant_id=? and user_id=?），不能靠提示词让模型"别看别人的"。
        真实系统里"删除"还要覆盖：向量索引、摘要里的副本、缓存、日志、备份 —— 这才是被遗忘权难的地方。"""
    )
    return store


# ====================================================================== main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="使用 ScriptedLLM 剧本，不调用真实模型")
    args = parser.parse_args()

    conv = build_long_conversation()
    if args.offline:
        print("🧪 离线模式：使用 ScriptedLLM 剧本（输出是预先写好的，但数据流和真实运行完全一致）")
        # 离线剧本复现了真实模型的典型行为：不给长度限制时，摘要会把工具原始数据大段抄进去
        verbose = (
            "- 最终目标：为设计部采购 3 台显示器。\n"
            f"- 硬性约束：单价不超过 2500 元；必须能开增值税专用发票；发票抬头「{KEY_CONSTRAINT}」。\n"
            "- 已确认的工具结果：\n"
            + "\n".join(f"  - {m['content']}" for m in conv if m["role"] == "tool")
            + "\n- 尚未完成：尚未最终确认型号；尚未创建订单。"
        )
        brief = (
            "- 目标：为设计部采购 3 台 27 寸 4K 显示器，用户已选定 MON-001（1779 元）。\n"
            f"- 硬性约束：单价 ≤ 2500 元；必须能开增值税专用发票；发票抬头「{KEY_CONSTRAINT}」。\n"
            "- 已确认：MON-001 供应商 S-002 可开专票、评分 4.7；库存 5 台，2 天内发货。\n"
            "- 待办：复述条件请用户确认后，再调用 create_order。"
        )
        verbose_llm = ScriptedLLM([reply(verbose)])
        brief_llm = ScriptedLLM([reply(brief)])
        make_llm = ScriptedLLM
        raw_llm = None
    else:
        real = default_llm()
        print(f"🌐 真实模型模式：{real.model}（如需离线运行，加 --offline）")
        verbose_llm = brief_llm = real
        make_llm = lambda script: real  # noqa: E731  真实模式下忽略剧本
        raw_llm = real

    demo_anatomy(conv)
    demo_compaction(conv, verbose_llm, brief_llm)
    demo_orphans(raw_llm, args.offline)
    demo_memory(make_llm, args.offline)

    section("小结")
    print(
        "  1. 上下文 = system + 工具定义 + 历史 + 工具结果 + 检索内容；工具结果往往最大、最该清理。\n"
        "  2. 截断/清理/压缩都必须保持 tool 消息配对协议，否则 400（或更糟：被静默丢弃）。\n"
        "  3. 长期记忆 = 外部存储 + 检索 + 注入；隔离、删除、防投毒是企业级的硬要求。\n"
        "  下一步：完成 exercise.py，然后运行 make lesson N=04"
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        if "LLM_API_KEY" not in str(e):
            raise
        print(f"\n❌ {e}\n提示：没有 API key 时可以加 --offline 运行。")
        sys.exit(1)
