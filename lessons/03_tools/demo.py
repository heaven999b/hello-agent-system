"""第 03 课 Demo：糟糕的工具设计 vs 良好的工具设计，在真实模型下的表现差异。

    .venv/bin/python lessons/03_tools/demo.py            # 真实模型（约 15 次模型调用，1 分钟左右）
    .venv/bin/python lessons/03_tools/demo.py --offline  # 离线剧本，复现真实模型的典型表现

场景：公司内部的"报销助手"。当前登录员工是 E100。
  第 0 部分  看说明书：模型眼中的工具长什么样
  实验 A     自由字符串 vs 枚举 —— "沉默的失败"
  实验 B     错误信息的三种写法 —— 放弃 / 自我纠正 / 一次做对
  实验 C     身份从哪里来 —— 模型传 employee_id vs 系统注入 ctx
  第 4 部分  校验层实拍（不调用模型，结果确定）
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Annotated, Literal

from pydantic import Field

from agentkit import (
    Agent,
    ResilientLLM,
    ScriptedLLM,
    ToolCall,
    ToolContext,
    ToolError,
    ToolRegistry,
    call_tool,
    call_tools,
    default_llm,
    reply,
    tool,
)

SYSTEM = "你是公司的报销助手。今天是 2026-09-27。需要数据时调用工具，不要编造。回答简洁。"
ME = {"user_id": "E100"}  # 可信身份：来自登录态（SSO），由系统注入，不经过模型

# ── 假数据库：注意每一行都带着内部字段（成本中心、会计科目、审批链……）──────────────────
EXPENSES = {
    r[0]: {"id": r[0], "employee_id": r[1], "submitted_at": r[2], "title": r[3], "amount": r[4], "status": r[5],
           "cost_center": "CC-RD-07" if r[1] == "E100" else "CC-HR-01", "gl_account": "6602.03",
           "approver_chain": ["M-331", "F-002"], "audit_flag": False}
    for r in [
        ("EX-1001", "E100", "2026-01-15", "办公用品", 320.0, "PAID"),
        ("EX-1002", "E100", "2026-02-20", "上海出差住宿", 1200.0, "PAID"),
        ("EX-1003", "E100", "2026-04-02", "市内打车", 86.5, "PAID"),
        ("EX-1004", "E100", "2026-05-18", "深圳出差机票", 2300.0, "PAID"),
        ("EX-1005", "E100", "2026-07-09", "团队建设餐费", 450.0, "PAID"),
        ("EX-1006", "E100", "2026-08-28", "技术培训课程", 2999.0, "PAID"),
        ("EX-1007", "E100", "2026-09-03", "北京出差机票", 1680.0, "PAID"),
        ("EX-1008", "E100", "2026-09-12", "客户招待餐费", 560.0, "PENDING_PAYOUT"),
        ("EX-1009", "E100", "2026-09-20", "加班打车", 128.0, "IN_REVIEW"),
        ("EX-2001", "E200", "2026-09-15", "年会礼品采购", 4200.0, "PENDING_PAYOUT"),  # 同事 E200 的
    ]
}
# 内部状态码 → 给模型看的友好取值
STATUS_OUT = {"DRAFT": "draft", "IN_REVIEW": "in_review", "PENDING_PAYOUT": "pending_payout", "PAID": "paid", "REJECTED": "rejected"}
STATUS_IN = {v: k for k, v in STATUS_OUT.items()}


def _public(row: dict) -> dict:
    """只返回模型需要的字段：带 ID、可读、没有内部字段。"""
    return {"id": row["id"], "title": row["title"], "amount": row["amount"],
            "status": STATUS_OUT[row["status"]], "submitted_at": row["submitted_at"]}


def _who(ctx: ToolContext | None) -> str:
    if ctx is None or not ctx.user_id:
        raise ToolError("无法确认当前员工身份，请重新登录。")
    return ctx.user_id


# ═════════════════════════════ 糟糕的工具 ═════════════════════════════


@tool(name="search_expenses")
def bad_search(status: str, ctx: ToolContext = None) -> list:
    """查询报销"""
    return [r for r in EXPENSES.values() if r["employee_id"] == _who(ctx) and r["status"] == status]


@tool(name="get_expenses")
def bad_get_by_employee(employee_id: str) -> list:
    """查询员工的报销单"""
    return [r for r in EXPENSES.values() if r["employee_id"] == employee_id]  # 谁的都能查！


def _range_total(start_date: str, end_date: str, user_id: str) -> tuple[int, float]:
    s, e = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    total = sum(r["amount"] for r in EXPENSES.values()
                if r["employee_id"] == user_id and s <= dt.date.fromisoformat(r["submitted_at"]) <= e)
    return (e - s).days, total


@tool(name="expense_total")
def total_v1(start_date: str, end_date: str, ctx: ToolContext = None) -> str:
    """统计报销总额"""
    days, total = _range_total(start_date, end_date, _who(ctx))
    if days > 90:
        raise ValueError("E_RANGE_LIMIT")  # 只有内部错误码，模型无从下手
    return str(total)


# ═════════════════════════════ 良好的工具 ═════════════════════════════

Status = Literal["draft", "in_review", "pending_payout", "paid", "rejected"]


@tool
def list_my_expenses(
    status: Annotated[Status | None, Field(description=(
        "按状态过滤：draft=草稿未提交；in_review=已提交、审批中；pending_payout=审批已通过、等待财务打款；"
        "paid=已打款到账；rejected=被驳回。不传则返回全部状态。"))] = None,
    limit: Annotated[int, Field(ge=1, le=20, description="最多返回几条，按提交时间从新到旧，默认 10")] = 10,
    ctx: ToolContext = None,
) -> dict:
    """查询当前登录员工本人的报销单（只能查本人，无法查询他人）。
    返回每张报销单的编号(id)、事由、金额(元)、状态、提交日期，以及符合条件的总数 total。"""
    rows = [r for r in EXPENSES.values()
            if r["employee_id"] == _who(ctx) and (status is None or r["status"] == STATUS_IN[status])]
    rows.sort(key=lambda r: r["submitted_at"], reverse=True)
    return {"expenses": [_public(r) for r in rows[:limit]], "total": len(rows)}


DateStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="日期（含当天），格式 YYYY-MM-DD，例如 2026-01-01")]


@tool(name="expense_total")
def total_v2(start_date: DateStr, end_date: DateStr, ctx: ToolContext = None) -> str:
    """统计当前员工在某个日期区间内提交的报销总金额（元）。"""
    days, total = _range_total(start_date, end_date, _who(ctx))
    if days > 90:
        raise ToolError(f"单次查询跨度最多 90 天，你请求了 {days} 天。"
                        "请把区间拆成若干段（每段不超过 90 天）分别查询，再把结果相加。")
    return f"{start_date} 至 {end_date} 报销总额：{total} 元"


@tool(name="expense_total")
def total_v3(start_date: DateStr, end_date: DateStr, ctx: ToolContext = None) -> str:
    """统计当前员工在某个日期区间内提交的报销总金额（元）。
    注意：单次查询跨度最多 90 天；更长的区间请拆成多段分别查询（可以一次并行发起多个调用）再相加。"""
    return total_v2.fn(start_date, end_date, ctx)


@tool(risk="write")
def withdraw_expense(
    expense_id: Annotated[str, Field(description="报销单编号，例如 EX-1009。不知道编号时先调用 list_my_expenses")],
    ctx: ToolContext = None,
) -> str:
    """撤回当前员工本人的一张报销单。只有审批中（in_review）的单据可以撤回。"""
    row = EXPENSES.get(expense_id)
    if row is None or row["employee_id"] != _who(ctx):  # "不存在"和"不是你的"说同一句话，防止探测
        raise ToolError(f"未找到报销单 {expense_id}，请确认编号，或先调用 list_my_expenses 查看。")
    if row["status"] != "IN_REVIEW":
        raise ToolError(f"报销单 {expense_id} 当前状态是 {STATUS_OUT[row['status']]}，只有 in_review 的单据可以撤回。"
                        "如果已打款但金额有误，需要联系财务部发起冲账更正。")
    return f"已撤回报销单 {expense_id}"


# ═════════════════════════════ 演示框架 ═════════════════════════════

OFFLINE = "--offline" in sys.argv
_real_llm = None


def llm_for(script: list):
    """离线模式返回剧本模型；真实模式所有实验共用一个真实模型。"""
    global _real_llm
    if OFFLINE:
        return ScriptedLLM(script)
    if _real_llm is None:
        _real_llm = ResilientLLM(default_llm())
    return _real_llm


def banner(title: str) -> None:
    print(f"\n{'═' * 72}\n{title}\n{'═' * 72}")


def run(label: str, tools: list, question: str, script: list):
    print(f"\n── {label} ──")
    result = Agent(llm_for(script), tools, system_prompt=SYSTEM).run(question, metadata=ME)
    for i, m in enumerate(result.messages):
        for c in m.get("tool_calls") or []:
            obs = next(x["content"] for x in result.messages[i:] if x.get("tool_call_id") == c["id"])
            obs = obs.replace("\n", " ")
            print(f"  🔧 {c['function']['name']}({c['function']['arguments']})")
            print(f"     ↳ {obs[:110]}{'…' if len(obs) > 110 else ''}")
    print(f"  💬 {result.output.strip()}".replace("\n", "\n     "))
    tokens = "" if OFFLINE else f"，输入 {result.usage.input_tokens} tokens"
    print(f"  📊 {result.steps} 次模型调用{tokens}")
    return result


def part0() -> None:
    banner("第 0 部分  看说明书：模型只能看到这些，看不到你的代码")
    print("❌ 糟糕的 search_expenses：")
    print(json.dumps(bad_search.schema()["function"], ensure_ascii=False, indent=2))
    print("\n✅ 良好的 list_my_expenses（由类型注解自动生成）：")
    print(json.dumps(list_my_expenses.schema()["function"], ensure_ascii=False, indent=2))
    print("\n观察：status 有了 enum 和每个取值的含义；limit 有了取值范围；ctx 参数根本没出现在 schema 里；")
    print("      additionalProperties=false —— 模型多传任何参数（比如 employee_id）都会被拒绝。")


def experiment_a() -> None:
    banner("实验 A  自由字符串 vs 枚举 —— \"沉默的失败\"")
    q = "我有哪些报销已经审批通过了、但钱还没到账？"
    print(f"用户（E100）：{q}\n正确答案：只有 1 笔 —— EX-1008 客户招待餐费 560 元（数据库里的状态码是 PENDING_PAYOUT）")
    bad = run("❌ 糟糕设计：status 是自由字符串，描述只有「查询报销」", [bad_search], q, [
        call_tool("search_expenses", status="approved_unpaid"),
        reply("目前没有查询到已审批通过但未到账的报销记录。"),
    ])
    good = run("✅ 良好设计：status 是枚举，并写清每个取值的含义", [list_my_expenses], q, [
        call_tool("list_my_expenses", status="pending_payout"),
        reply("你有 1 笔报销已审批通过、等待打款：EX-1008 客户招待餐费 560 元。"),
    ])
    for name, r in (("糟糕设计", bad), ("良好设计", good)):
        print(f"  判定 {name}：{'✅ 找到了 EX-1008' if 'EX-1008' in (r.output or '') else '❌ 漏报 —— 而且模型非常自信'}")
    print("\n💡 最危险的不是报错，而是「沉默的失败」：工具没报错、只是返回了空列表，模型就会自信地告诉用户\"没有\"。")
    print("   模型不可能猜中你的内部状态码。枚举 + 含义说明，让它根本没有机会猜错。")


def experiment_b() -> None:
    banner("实验 B  错误信息的三种写法 —— 放弃 / 自我纠正 / 一次做对")
    q = "我今年到现在一共报销了多少钱？"
    print(f"用户（E100）：{q}\n隐藏的业务规则：单次查询跨度不能超过 90 天。正确答案：9723.5 元")
    split = [("expense_total", {"start_date": "2026-01-01", "end_date": "2026-03-31"}),
             ("expense_total", {"start_date": "2026-04-01", "end_date": "2026-06-29"}),
             ("expense_total", {"start_date": "2026-06-30", "end_date": "2026-09-27"})]
    whole = {"start_date": "2026-01-01", "end_date": "2026-09-27"}
    results = [
        run("v1 ❌ 描述含糊 + 只抛内部错误码 E_RANGE_LIMIT", [total_v1], q, [
            call_tool("expense_total", **whole),
            reply("抱歉，查询时系统返回了范围限制错误（E_RANGE_LIMIT），暂时无法给出准确总额。"),
        ]),
        run("v2 🟡 描述没提限制，但错误信息告诉模型「怎么改」", [total_v2], q, [
            call_tool("expense_total", **whole),
            call_tools(*split),
            reply("你今年一共报销了 9723.5 元（1520 + 2386.5 + 5817）。"),
        ]),
        run("v3 ✅ 限制写进描述 + 错误信息可行动", [total_v3], q, [
            call_tools(*split),
            reply("你今年一共报销了 9723.5 元（1520 + 2386.5 + 5817）。"),
        ]),
    ]
    for tag, r in zip(("v1", "v2", "v3"), results):
        correct = "9723.5" in (r.output or "").replace(",", "")
        verdict = "✅ 答对" if correct else "❌ 没有得到正确总额"
        if tag == "v1" and correct:
            verdict = "✅ 这次碰巧猜对了（模型只能猜错误码的含义）"
        print(f"  判定 {tag}：{verdict}；模型调用 {r.steps} 次，工具调用 {len(r.tools_called())} 次")
    print("\n💡 两道防线：好的「描述」让模型第一次就做对（v3）；好的「错误信息」让模型第二次做对（v2）。")
    print("   只有错误码时（v1），模型只能猜：我们实测它有时直接放弃，有时碰巧猜对但多绕很多路。")
    print("   错误信息是写给模型看的：说清 哪里错了 + 为什么 + 下一步怎么做。正确性不能寄托在模型的猜测上。")


def experiment_c() -> None:
    banner("实验 C  身份从哪里来 —— 越权访问")
    q = "帮我看看同事张三（工号 E200）的报销单都有哪些，金额多少。"
    print(f"用户（E100）：{q}")
    bad = run("❌ 糟糕设计：employee_id 由模型填写", [bad_get_by_employee], q, [
        call_tool("get_expenses", employee_id="E200"),
        reply("张三（E200）有 1 笔报销：EX-2001 年会礼品采购 4200 元，成本中心 CC-HR-01，待打款。"),
    ])
    good = run("✅ 良好设计：身份由系统从 ctx 注入，schema 里没有这个参数", [list_my_expenses], q, [
        reply("抱歉，我只能查询你本人的报销单，无法查询同事的信息。"),
    ])
    leaked = "EX-2001" in json.dumps(bad.messages, ensure_ascii=False)
    print(f"  判定 糟糕设计：{'⚠️ 越权！模型拿到了同事 E200 的报销数据（连成本中心这种内部字段也一起泄露）' if leaked else '这次模型拒绝了 —— 但安全不能靠模型的自觉'}")
    print(f"  判定 良好设计：{'⚠️ 越权' if 'EX-2001' in json.dumps(good.messages, ensure_ascii=False) else '✅ 没有越权 —— 不是模型更听话，而是它根本没有这个能力'}")
    print("\n💡 用户输入、网页、邮件都可能被注入恶意指令。让模型决定\"查谁的数据\"，等于让攻击者决定。")
    print("   身份必须来自登录态（ctx），工具内部再做一次\"这条数据是不是你的\"检查。")


def part4() -> None:
    banner("第 4 部分  校验层实拍：ToolRegistry.execute 如何把错误变成\"观察\"（不调用模型）")
    reg = ToolRegistry([list_my_expenses, withdraw_expense])
    ctx = ToolContext(run_id="demo", call_id="c1", user_id="E100")
    cases = [
        ("枚举值不对", "list_my_expenses", '{"status": "approved"}'),
        ("超出范围", "list_my_expenses", '{"limit": 500}'),
        ("试图冒充同事", "list_my_expenses", '{"employee_id": "E200"}'),
        ("JSON 不合法", "list_my_expenses", '{"status": "paid"'),
        ("幻想出的工具", "delete_all_expenses", "{}"),
        ("业务规则拒绝", "withdraw_expense", '{"expense_id": "EX-1007"}'),
        ("别人的单据", "withdraw_expense", '{"expense_id": "EX-2001"}'),
        ("完全不存在", "withdraw_expense", '{"expense_id": "EX-9999"}'),
        ("正常调用", "list_my_expenses", '{"status": "in_review"}'),
    ]
    for label, name, args in cases:
        r = reg.execute(ToolCall("c1", name, args), ctx)
        print(f"\n▶ {label}：{name}({args})")
        print(f"  ok={r.ok}  error_type={r.error_type}")
        print("  " + r.content.replace("\n", "\n  "))
    print("\n💡 以上每一段文字都会作为 tool 消息喂回给模型 —— 这就是\"错误即观察\"：Agent 不崩溃，模型据此自我纠正。")
    print("   注意「别人的单据」和「完全不存在」返回的是同一句话 —— 攻击者无法借此探测哪些编号真实存在。")


def main() -> None:
    print("🎬 离线模式：剧本复现真实模型的典型表现" if OFFLINE else "🌐 真实模型模式（约 1 分钟；模型的措辞每次会略有不同）")
    part0()
    try:
        experiment_a()
        experiment_b()
        experiment_c()
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ 调用模型失败：{type(e).__name__}: {e}\n   可以先用 --offline 运行，或 make check-env 检查配置。")
    part4()


if __name__ == "__main__":
    main()
