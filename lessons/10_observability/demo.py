"""第 10 课 Demo：给 Agent 装上"黑匣子"。

一个电商客服 Agent 处理 5 个任务（一个会遇到物流接口故障、一个带手机号、一个在同一轮里并行调用 3 个只读工具），我们：
  1. 每次运行打印 Span 树 —— 看见 Agent 每一步做了什么、花了多久、用了多少 token；
     并行的只读工具调用在 trace 里是时间上重叠的 span（Agent 用 asyncio 并发执行它们）
  2. 把所有 Span 导出为 JSONL —— 这就是发给可观测性后端的原始数据
  3. 从 JSONL 算出监控看板上的指标 —— 成功率、p50/p95、token、工具错误率……
  4. 复盘坏案例 —— 从"工具错误率高"一路定位到具体的 trace 和最慢路径
  5. 隐私 —— 埋点处的自动脱敏，以及正则脱敏抓不住的盲区

运行：
    python lessons/10_observability/demo.py            # 真实模型（读取 .env）
    python lessons/10_observability/demo.py --offline  # 离线剧本，无需 API key
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import unicodedata
from pathlib import Path
from typing import Annotated

from pydantic import Field

from agentkit import (
    Agent,
    ScriptedLLM,
    ToolError,
    Tracer,
    Usage,
    call_tool,
    call_tools,
    default_llm,
    jsonl_exporter,
    redact_pii,
    render_tree,
    reply,
    tool,
)

HERE = Path(__file__).resolve().parent
TRACE_DIR = HERE / "traces"
RAW_PATH = TRACE_DIR / "demo.jsonl"


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def pad(text: str, width: int) -> str:
    """按终端显示宽度补空格（中文字符占 2 列），让中英文混排的表格对齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


# ---------------------------------------------------------------- 模拟的业务系统
# 工具都是 async 函数：查数据库、调物流接口的等待用 await asyncio.sleep 表示，
# 等待期间事件循环去推进别的事（同一轮里的其他只读工具、同一进程里的其他会话）。

ORDERS = {
    "A1001": {"status": "已发货", "tracking_no": "SF1001", "item": "降噪耳机", "phone": "13812345678"},
    "A1002": {"status": "已发货", "tracking_no": "YT2002", "item": "机械键盘", "phone": "13900001111"},
    "A1003": {"status": "待发货", "tracking_no": None, "item": "显示器支架", "phone": "13812345678"},
}
POLICIES = {
    "退货": "自签收之日起 7 天内可无理由退货（商品完好、配件齐全）；7-15 天内仅支持质量问题退换。",
    "运费": "质量问题退货运费由商家承担；无理由退货运费由买家承担。",
}


@tool
async def lookup_order(order_id: Annotated[str, Field(description="订单号，例如 A1001")]) -> dict:
    """查询订单状态、商品和物流单号。"""
    await asyncio.sleep(0.05)  # 数据库查询耗时
    order = ORDERS.get(order_id.strip().upper())
    if order is None:
        raise ToolError(f"订单 {order_id} 不存在，请让用户核对订单号。")
    return {k: v for k, v in order.items() if k != "phone"}


@tool
async def track_shipment(tracking_no: Annotated[str, Field(description="物流单号，例如 SF1001")]) -> str:
    """查询物流轨迹。"""
    await asyncio.sleep(0.15)  # 外部物流接口通常比内部数据库慢
    if tracking_no.upper().startswith("YT"):
        # 模拟真实故障：某家物流商的接口持续超时
        raise ToolError("物流商接口超时（YT 网关返回 504），暂时无法查询该物流单。")
    return f"{tracking_no}：今天 09:12 已到达【上海浦东转运中心】，预计明天送达。"


@tool
async def search_policy(query: Annotated[str, Field(description="要检索的售后问题关键词")]) -> str:
    """检索售后政策知识库（退货、运费等）。"""
    await asyncio.sleep(0.03)
    hits = [f"【{k}】{v}" for k, v in POLICIES.items() if k in query] or list(POLICIES.values())
    return "\n".join(hits)


@tool
async def find_orders_by_phone(phone: Annotated[str, Field(description="用户手机号")]) -> list:
    """按手机号查询用户名下的订单号列表。"""
    await asyncio.sleep(0.05)
    return [oid for oid, o in ORDERS.items() if o["phone"] == phone]


TOOLS = [lookup_order, track_shipment, search_policy, find_orders_by_phone]
SYSTEM_PROMPT = (
    "你是电商平台的售后客服助手。查询订单、物流、政策时必须使用工具，不要编造。"
    "工具报错时，如实告诉用户原因和下一步建议，不要反复重试同一个失败的工具超过一次。回答简洁。"
)

TASKS = [
    ("t1-查物流", "我的订单 A1001 到哪了？"),
    ("t2-问政策", "耳机买了 10 天了，还能无理由退货吗？"),
    ("t3-工具故障", "帮我看看订单 A1002 的物流进度"),
    ("t4-含手机号", "我的手机号是 13812345678，帮我查一下我名下有哪些订单"),
    ("t5-并行查询", "订单 A1001 和 A1003 现在分别是什么状态？另外退货的运费谁出？"),
]
PARALLEL_TASK = "t5-并行查询"


# ---------------------------------------------------------------- 离线剧本
# 每一步是 (模型耗时秒数, 模型的响应)。耗时交给 ScriptedLLM(latency=...)：它用 asyncio.sleep 等待，
# 和真实的网络等待一样不占 CPU、不阻塞事件循环。token 数取真实量级。

def with_usage(response, input_tokens: int, output_tokens: int):
    response.usage = Usage(input_tokens, output_tokens)
    return response


def offline_scripts() -> dict[str, list[tuple[float, object]]]:
    return {
        "t1-查物流": [
            (0.35, call_tool("lookup_order", order_id="A1001", input_tokens=410, output_tokens=22)),
            (0.30, call_tool("track_shipment", tracking_no="SF1001", input_tokens=480, output_tokens=24)),
            (0.55, reply("您的订单 A1001（降噪耳机）已到达上海浦东转运中心，预计明天送达。", 560, 38)),
        ],
        "t2-问政策": [
            (0.30, call_tool("search_policy", query="退货", input_tokens=405, output_tokens=18)),
            (0.60, reply("已超过 7 天无理由退货期限；如果是质量问题，15 天内仍可申请退换。", 520, 45)),
        ],
        "t3-工具故障": [
            (0.30, call_tool("lookup_order", order_id="A1002", input_tokens=408, output_tokens=22)),
            (0.35, call_tool("track_shipment", tracking_no="YT2002", input_tokens=470, output_tokens=24)),
            (0.40, call_tool("track_shipment", tracking_no="YT2002", input_tokens=530, output_tokens=24)),
            (0.70, reply("抱歉，物流商接口暂时超时，无法查询 YT2002 的进度。订单已发货，建议稍后再查。", 600, 52)),
        ],
        "t4-含手机号": [
            (0.40, call_tool("find_orders_by_phone", phone="13812345678", input_tokens=420, output_tokens=26)),
            (0.50, reply("您名下有 2 个订单：A1001（已发货）和 A1003（待发货）。", 470, 30)),
        ],
        # 同一轮里 3 个只读工具：Agent 用 asyncio.gather 并发执行它们（有写操作时才会逐个执行）
        PARALLEL_TASK: [
            (0.45, with_usage(call_tools(("lookup_order", {"order_id": "A1001"}), ("lookup_order", {"order_id": "A1003"}),
                                         ("search_policy", {"query": "运费"})), 430, 64)),
            (0.60, reply("A1001（降噪耳机）已发货，A1003（显示器支架）待发货。质量问题退货运费由商家承担，"
                         "无理由退货由您承担。", 590, 48)),
        ],
    }


def scripted_llm(steps: list[tuple[float, object]]) -> ScriptedLLM:
    delays = [d for d, _ in steps]
    return ScriptedLLM([r for _, r in steps], latency=lambda n: delays[n - 1], model="scripted")


def waterfall(root, cell_ms: float) -> list[str]:
    """把一次运行的直接子 span 画成时间线（相对 agent.run 的开始时间）：看得出哪些 span 在时间上重叠。"""
    lines = []
    for s in root.children:
        a = (s.start - root.start) * 1000
        b = (s.end - root.start) * 1000
        bar = " " * int(a / cell_ms) + "█" * max(1, round((b - a) / cell_ms))
        lines.append(f"{pad(s.name, 22)}{pad(bar, 44)}{a:5.0f} → {b:5.0f}ms")
    return lines


def show_parallel_tools(root) -> None:
    """找到"同一轮里调用了多个工具"的那一组 span，证明它们在时间上重叠（真的并发了）。"""
    tools = [s for s in root.children if s.name.startswith("tool.")]
    groups: dict[int, list] = {}
    for s in tools:  # 按"紧跟在哪次模型调用之后"分组
        step_idx = sum(1 for c in root.children if c.name == "llm.chat" and c.start <= s.start)
        groups.setdefault(step_idx, []).append(s)
    batch = max(groups.values(), key=len) if groups else []
    cell = max(25.0, root.duration_ms / 40)  # 整条时间线最多 40 格左右
    print(f"  时间线（每格 {cell:.0f}ms，相对 agent.run 开始）：")
    for line in waterfall(root, cell):
        print("    " + line)
    if len(batch) < 2:
        print("  这次模型把工具分在几轮里依次调用，没有同一轮的多个工具可以并行（真实模型的行为每次可能不同）。")
        return
    overlap = max(s.start for s in batch) < min(s.end for s in batch)
    total = sum(s.duration_ms for s in batch)
    wall = (max(s.end for s in batch) - min(s.start for s in batch)) * 1000
    names = ", ".join(s.name.removeprefix("tool.") for s in batch)
    print(f"  同一轮的 {len(batch)} 个只读工具（{names}）："
          f"{'时间上互相重叠 → 并发执行' if overlap else '没有重叠'}；"
          f"各自耗时加起来 {total:.0f}ms，实际只占了 {wall:.0f}ms 墙钟。")


def load_metrics_impl():
    """优先用你在 exercise.py 里的实现；还没写完就用参考答案。"""
    sys.path.insert(0, str(HERE))
    import exercise  # noqa: E402

    try:
        exercise.compute_metrics([])
        exercise.slowest_path([], "x")
        return exercise, "exercise.py（你的实现）"
    except NotImplementedError:
        import solution  # noqa: E402

        return solution, "solution.py（参考答案 —— 完成练习后这里会自动换成你的实现）"


# ---------------------------------------------------------------- 主流程

async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用 ScriptedLLM 剧本代替真实模型")
    args = parser.parse_args()

    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_PATH.unlink(missing_ok=True)  # exporter 是追加写，每次演示前清空

    # 所有任务共用一个 Tracer：生产中一个进程通常只有一个全局 Tracer
    tracer = Tracer(exporter=jsonl_exporter(RAW_PATH))
    scripts = offline_scripts() if args.offline else {}
    mode = "离线剧本（ScriptedLLM，latency 模拟模型耗时）" if args.offline else "真实模型（.env 配置）"

    section(f"1. 运行 {len(TASKS)} 个任务，每次运行都会生成一棵 Span 树    模式：{mode}")
    print("读法：每行一个 Span（名称  耗时  关键属性），缩进表示父子关系。FAIL(...) 是工具失败。")
    for task_id, text in TASKS:
        llm = scripted_llm(scripts[task_id]) if args.offline else default_llm()
        agent = Agent(llm, TOOLS, system_prompt=SYSTEM_PROMPT, name="support", max_steps=6, tracer=tracer)
        result = await agent.run(text, metadata={"tenant_id": "shop-01", "user_id": "u-42"})
        print(f"\n▶ [{task_id}] 用户：{text}")
        print(f"  助手：{' '.join((result.output or '').split())[:160]}")
        print("  " + render_tree(result.trace).replace("\n", "\n  "))
        if task_id == PARALLEL_TASK:
            show_parallel_tools(result.trace)

    # ------------------------------------------------------------ 2. JSONL
    section("2. 导出：扁平的 JSONL —— 这就是发给可观测性后端的原始数据")
    spans = [json.loads(line) for line in RAW_PATH.read_text(encoding="utf-8").splitlines()]
    rel = RAW_PATH.relative_to(HERE.parent.parent)
    print(f"共 {len(spans)} 个 span → {rel}")
    print("每行是一个 span，用 parent_id 指向父节点（任何后端都能据此还原出树）。其中一行：")
    sample = next(s for s in spans if s["name"] == "llm.chat")
    print("  " + json.dumps(sample, ensure_ascii=False))
    print("（第 5 节之后会告诉你怎么在浏览器里看这些 trace）")

    # ------------------------------------------------------------ 3. 指标
    impl, impl_name = load_metrics_impl()
    section("3. 汇总指标 —— 监控看板上的数字，全部从 span 算出来")
    print(f"（计算函数来自 {impl_name}）\n")
    m = impl.compute_metrics(spans)
    roots = [s for s in spans if s["parent_id"] is None and s["name"] == "agent.run"]
    avg_cost = sum(s["attrs"].get("agent.cost_usd", 0) for s in roots) / max(len(roots), 1)
    rows = [
        ("运行次数", f"{m['runs']}"),
        ("成功率（status=completed）", f"{m['success_rate']:.0%}"),
        ("运行耗时 p50 / p95", f"{m['p50_ms'] / 1000:.2f}s / {m['p95_ms'] / 1000:.2f}s"),
        ("总 token", f"{m['total_tokens']:,}"),
        ("平均步数", f"{m['avg_steps']:.2f}"),
        ("平均每任务成本（示例价格）", f"${avg_cost:.5f}"),
    ]
    for k, v in rows:
        print(f"  {pad(k, 30)}{v}")
    print(f"\n  {pad('工具', 24)}{pad('调用', 6)}{pad('失败', 6)}错误率")
    for name, st in sorted(m["tools"].items(), key=lambda kv: -kv[1]["error_rate"]):
        flag = "   ← 需要关注" if st["error_rate"] >= 0.3 else ""
        print(f"  {pad(name, 24)}{st['calls']:<6}{st['errors']:<6}{st['error_rate']:.0%}{flag}")
    llm_failed = [s for s in roots if s["attrs"].get("agent.status") == "failed"]
    if llm_failed:
        print(f"\n⚠️  有 {len(llm_failed)} 次运行 status=failed：看第 1 节 trace 里 llm.chat 的 ERROR —— 模型服务本身不可用。"
              "\n   这正是 trace 的价值：一眼区分'模型挂了'和'Agent 答错了'。检查 .env / 网关，或先用 --offline 体验。")
    else:
        print(
            "\n观察：成功率可能是 100%（Agent 礼貌地告诉用户'查不到'也算 completed），"
            "但某个工具的错误率可能很高。\n只看成功率会错过问题 —— 这就是为什么要按工具拆分指标。"
        )

    # ------------------------------------------------------------ 4. 复盘
    section("4. 复盘坏案例：工具错误 → 定位 trace → 沿最慢路径看瓶颈")
    failed = [s for s in spans if s["name"].startswith("tool.") and s["attrs"].get("tool.ok") is False]
    if not failed:
        print("这次运行没有工具失败（真实模型可能换了做法）。挑耗时最长的一次运行来看：")
        worst_trace = max(roots, key=lambda s: s["duration_ms"])["trace_id"]
    else:
        f0 = failed[0]
        worst_trace = f0["trace_id"]
        print(f"找到 {len(failed)} 次失败的工具调用。第一条：")
        print(f"  trace_id = {f0['trace_id']}   span = {f0['name']}   error_type = {f0['attrs'].get('tool.error_type')}")
        print(f"  参数 = {f0['attrs'].get('tool.arguments')}")
    root = next(s for s in roots if s["trace_id"] == worst_trace)
    print(f"\n这个 trace 总耗时 {root['duration_ms']:.0f}ms，步数 {root['attrs'].get('agent.steps')}，"
          f"状态 {root['attrs'].get('agent.status')}")
    print("最慢路径：" + "  →  ".join(impl.slowest_path(spans, worst_trace)))
    children = sorted((s for s in spans if s["parent_id"] == root["span_id"]), key=lambda s: s["start"])
    total = root["duration_ms"] or 1
    print("时间都花在哪（按发生顺序）：")
    for s in children:
        bar = "█" * max(1, round(s["duration_ms"] / total * 40))
        print(f"  {s['name']:<26}{s['duration_ms']:>8.0f}ms  {bar}")
    llm_ms = sum(s["duration_ms"] for s in children if s["name"] == "llm.chat")
    tool_ms = sum(s["duration_ms"] for s in children if s["name"].startswith("tool."))
    print(f"模型调用合计 {llm_ms:.0f}ms（{llm_ms / total:.0%}），工具合计 {tool_ms:.0f}ms（{tool_ms / total:.0%}）。"
          "\n大多数 Agent 的延迟大头在模型调用：少走一步，往往比把工具优化快 10 倍更有用。")
    print("\n下一步（第 11 课）：把这个 case 加进评估集，修复后跑回归，保证它不再发生。")

    # ------------------------------------------------------------ 5. 隐私
    section("5. 隐私：trace 里的个人信息")
    phone = "13812345678"
    leaked = [s for s in spans if phone in json.dumps(s, ensure_ascii=False)]
    print("第一道防线（埋点处）：agentkit 记录 tool.arguments / tool.result_preview 之前，先调用 redact_pii：")
    for s in spans:
        if s["name"] == "tool.find_orders_by_phone":
            print(f"  {s['name']:<28} tool.arguments = {s['attrs'].get('tool.arguments')}")
            break
    print(f"  导出文件里包含原始手机号的 span：{len(leaked)} 个"
          "（用户消息和最终回答里也有手机号，但它们本来就不记进 span）")
    sample = "收货人张伟，地址上海市浦东新区张江路 88 号 1201 室，电话 13812345678，邮箱 zhangwei@example.com"
    print("\n但正则脱敏有盲区：格式固定的能抓住，自由文本抓不住。")
    print(f"  原文：{sample}")
    print(f"  脱敏：{redact_pii(sample)}")
    print(
        "\n要点：姓名和地址原样保留了。trace 往往会被发到第三方平台、被很多工程师查看、保留很久，"
        "\n所以默认策略应该是'不记录内容'；确实需要内容时，再叠加脱敏、外置存储和访问控制（README 问题 2）。"
    )

    # ------------------------------------------------------------ 下一步
    section("下一步：在浏览器里看 trace（瀑布图 + span 详情）")
    print("用 agentkit 自带的查看器把 JSONL 渲染成单文件 HTML（零依赖，断网也能打开）：\n")
    print(f"  python -m agentkit.viewer {rel} --open")
    print(f"  # 或：make viewer T={rel}\n")
    print("左侧是 trace 列表；右侧是瀑布图，条形长度 = 真实耗时；点击任意 span 可以看全部属性"
          "\n（模型、token、工具参数、tool.result_preview、错误信息）。"
          )


if __name__ == "__main__":
    asyncio.run(main())
