"""第 05 课 Demo：同一个多步任务，用三种架构各做一遍，比较输出、模型调用次数、token 和耗时。

    .venv/bin/python lessons/05_agent_architectures/demo.py            # 真实模型（读取 .env）
    .venv/bin/python lessons/05_agent_architectures/demo.py --offline  # 离线剧本，无需 API key

任务：查三个城市在出差当天的天气，给出出差建议。工具是假的（数据写死在本文件里），
而且"广州"的主接口被故意设成维护中 —— 看三种架构各自怎么应对这个意外。

  1. ReAct            agentkit.Agent：模型边想边做，每拿到一个观察结果，就重新决定下一步
  2. Plan-and-Execute 规划器一次写出完整计划 → 代码逐步执行（不经过模型）→ 某一步失败才重规划 → 汇总
  3. Reflection       ReAct 先写初稿 → 批评者对照工具数据挑错（先代码检查、再模型检查）→ 按意见修改

最后打印对比表：模型调用、工具调用、token、耗时，以及一个用代码做的"质量检查"。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from contextlib import contextmanager
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agentkit import Agent, ScriptedLLM, ToolError, ToolRegistry, Usage, call_tool, call_tools, default_llm, reply, tool
from agentkit.context import estimate_tokens
from agentkit.types import ToolCall, calls_in
from agentkit.workflows import Review, complete, complete_json, evaluator_optimizer

# ====================================================================== 任务与假工具

TRIP = [("北京", "10-15"), ("上海", "10-16"), ("广州", "10-17")]
TASK = (
    "我下周连续出差：10-15 在北京，10-16 在上海，10-17 在广州。请查询每个城市当天的天气，然后给我出差建议："
    "每个城市一行（天气、穿衣和要带的东西），最后一行指出需要提前调整行程的风险。总共不超过 5 行，不要用表格。"
)
MAX_LINES = 5

WEATHER = {
    ("北京", "10-15"): {"天气": "晴", "气温": "8~19°C", "降水概率": "5%", "提示": "早晚温差 11°C，北风 4 级"},
    ("上海", "10-16"): {"天气": "小雨转中雨", "气温": "17~22°C", "降水概率": "80%"},
    ("广州", "10-17"): {
        "天气": "暴雨",
        "气温": "24~28°C",
        "降水概率": "95%",
        "预警": "台风蓝色预警：10-17 全天阵风 8~9 级，白云机场航班可能大面积延误或取消",
    },
}
AIRPORTS = {"PEK": "北京", "PKX": "北京", "SHA": "上海", "PVG": "上海", "CAN": "广州"}
UNDER_MAINTENANCE = {"广州"}  # 主接口"维护中"的城市：制造一个计划之外的失败
TOOL_LOG: list[str] = []  # 记录每次真正执行的工具调用，用来统计"工具调用次数"


def _forecast(city: str, date: str) -> dict:
    data = WEATHER.get((city, date))
    if data is None:
        known = "、".join(f"{c} {d}" for c, d in WEATHER)
        raise ToolError(f"没有 {city} {date} 的预报。目前只能查询：{known}")
    return {"城市": city, "日期": date, **data}


@tool
def get_weather(
    city: Annotated[str, Field(description="城市中文名，如 北京")],
    date: Annotated[str, Field(description="日期，格式 MM-DD，如 10-15")],
) -> dict:
    """按城市查询某一天的天气预报：天气、气温、降水概率，以及气象预警（如果有）。"""
    TOOL_LOG.append(f"get_weather({city},{date})")
    if city in UNDER_MAINTENANCE:
        raise ToolError(f"{city}气象站接口维护中（503）。可以改用 get_weather_by_airport 按机场三字码查询，例如广州白云机场是 CAN。")
    return _forecast(city, date)


@tool
def get_weather_by_airport(
    airport_code: Annotated[str, Field(description="机场三字码，如 PEK、SHA、CAN")],
    date: Annotated[str, Field(description="日期，格式 MM-DD，如 10-15")],
) -> dict:
    """备用接口：按机场三字码查询机场所在城市某一天的天气预报（字段同 get_weather）。"""
    TOOL_LOG.append(f"get_weather_by_airport({airport_code},{date})")
    city = AIRPORTS.get(airport_code.strip().upper())
    if city is None:
        raise ToolError(f"不认识机场三字码 {airport_code}。支持：{', '.join(AIRPORTS)}")
    return {"机场": airport_code.upper(), **_forecast(city, date)}


TOOLS = [get_weather, get_weather_by_airport]


# ====================================================================== 计量与打印


class Meter:
    def __init__(self):
        self.calls = 0
        self.usage = Usage()


class Metered:
    """装饰器模式：对外还是一个 LLM，顺手把每次调用记到 Meter 上（和第 06 课 Demo 一样）。"""

    def __init__(self, llm, meter: Meter):
        self.llm, self.meter, self.model = llm, meter, llm.model

    def chat(self, messages, tools=None, **kwargs):
        response = self.llm.chat(messages, tools, **kwargs)
        self.meter.calls += 1
        self.meter.usage = self.meter.usage + response.usage
        return response


class Estimated:
    """离线剧本里 usage 是写死的小数字。为了让离线对比表也有参考意义，按消息长度估算 token。"""

    def __init__(self, llm):
        self.llm, self.model = llm, llm.model

    def chat(self, messages, tools=None, **kwargs):
        response = self.llm.chat(messages, tools, **kwargs)
        tool_tokens = estimate_tokens([{"content": json.dumps(tools, ensure_ascii=False)}]) if tools else 0
        response.usage = Usage(estimate_tokens(messages) + tool_tokens, estimate_tokens([response.to_message()]))
        return response


METER = Meter()
SUMMARY: list[dict] = []


@contextmanager
def measure(name: str, who_thinks: str, outputs: dict):
    calls0, tokens0, tools0, t0 = METER.calls, METER.usage.total, len(TOOL_LOG), time.time()
    yield
    row = {
        "name": name,
        "who": who_thinks,
        "seconds": time.time() - t0,
        "llm_calls": METER.calls - calls0,
        "tool_calls": len(TOOL_LOG) - tools0,
        "tokens": METER.usage.total - tokens0,
        "output": outputs.get("text", ""),
    }
    SUMMARY.append(row)
    print(f"\n  ⏱ 耗时 {row['seconds']:.1f}s ｜ 模型调用 {row['llm_calls']} 次 ｜ 工具调用 {row['tool_calls']} 次 ｜ tokens {row['tokens']}")


def section(title: str) -> None:
    print("\n" + "=" * 76 + f"\n{title}\n" + "=" * 76)


def note(text: str) -> None:
    for line in text.strip().splitlines():
        print(f"  💡 {line.strip()}")


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def show_block(text: str, max_lines: int = 12, indent: str = "    │ ") -> None:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    for ln in lines[:max_lines]:
        print(f"{indent}{ln}")
    if len(lines) > max_lines:
        print(f"{indent}……（省略 {len(lines) - max_lines} 行）")


def short_args(arguments: str) -> str:
    try:
        return ",".join(str(v) for v in json.loads(arguments or "{}").values())
    except json.JSONDecodeError:
        return arguments


def show_trajectory(messages: list[dict], between_turns: list[str] = ()) -> None:
    """把 Agent 的消息历史打印成"第几步、模型决定做什么、哪个工具失败了"。

    between_turns：多轮对话时，在第 2、3……条用户消息的位置依次打印的说明（比如"收到审稿意见"）。
    """
    results = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    step, turn = 0, 0
    for m in messages:
        if m["role"] == "user":
            if 0 < turn <= len(between_turns):
                print(between_turns[turn - 1])
            turn += 1
            continue
        if m["role"] != "assistant":
            continue
        step += 1
        calls = calls_in(m)
        if not calls:
            print(f"    第 {step} 步（模型）→ 给出回答")
            continue
        print(f"    第 {step} 步（模型）→ " + "  ".join(f"{c.name}({short_args(c.arguments)})" for c in calls))
        for c in calls:
            content = results.get(c.id, "")
            if content.startswith("错误"):
                print(f"        ← ❌ {content[:70]}")


def tool_evidence(messages: list[dict]) -> list[str]:
    """从消息历史里取出成功的工具结果（给批评者当"事实依据"）。"""
    return [m["content"] for m in messages if m["role"] == "tool" and not m["content"].startswith("错误")]


def quality(text: str) -> tuple[str, bool]:
    """用代码做一个最小的质量检查：三个城市都覆盖了吗？提醒台风了吗？行数超了吗？"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    cities = sum(c in (text or "") for c, _ in TRIP)
    typhoon = "台风" in (text or "")
    ok = cities == len(TRIP) and typhoon and len(lines) <= MAX_LINES
    return f"{cities}/{len(TRIP)} 城 · 台风{'✅' if typhoon else '❌'} · {len(lines)} 行{'✅' if len(lines) <= MAX_LINES else '❌'}", ok


# ====================================================================== 架构 1：ReAct

REACT_PROMPT = (
    "你是差旅助手。需要数据时调用工具，可以一次并行调用多个工具；工具报错时，按错误提示换一种方式获取数据。"
    "所有结论都必须来自工具数据，不要编造。最终回答用中文，严格遵守用户的格式要求。"
)


def run_react(llm) -> None:
    section("架构 1：ReAct —— 边想边做（agentkit.Agent 本身就是 ReAct）")
    note("每一步都回到模型：模型看到上一步的观察结果，再决定下一步。遇到意外（广州接口维护）时，下一步自然会绕过去。")
    outputs: dict = {}
    with measure("ReAct", "每一步都由模型决定", outputs):
        agent = Agent(llm, TOOLS, system_prompt=REACT_PROMPT, name="react", max_steps=8)
        result = agent.run(TASK)
        print("\n  执行轨迹：")
        show_trajectory(result.messages)
        print(f"\n  🤖 输出（status={result.status}）：")
        show_block(result.output or "")
        outputs["text"] = result.output or ""


# ====================================================================== 架构 2：Plan-and-Execute


class PlanStep(BaseModel):
    id: str = Field(description="步骤编号：s1、s2……，不能重复")
    tool: Literal["get_weather", "get_weather_by_airport"] = Field(description="这一步调用的工具")
    args: dict[str, str] = Field(description="工具参数，如 {\"city\": \"北京\", \"date\": \"10-15\"}")


class Plan(BaseModel):
    steps: list[PlanStep] = Field(description="按执行顺序排列的步骤")


def tools_doc() -> str:
    lines = []
    for t in TOOLS:
        params = ", ".join(t.schema()["function"]["parameters"]["properties"])
        lines.append(f"- {t.name}({params})：{t.description}")
    return "\n".join(lines)


PLANNER_PROMPT = """你是规划器。请把任务拆成一组工具调用步骤，一次性给出完整计划。
可用工具：
{tools}
要求：每一步只调用一个工具；只规划"查数据"的步骤，最后的汇总由系统完成。

任务：{task}"""

REPLANNER_PROMPT = """你是重规划器。按计划执行时，有一步失败了。
任务：{task}
已成功的步骤及结果：
{done}
失败的步骤：{failed}
错误信息：{error}
原计划中还没执行的步骤：{remaining}
可用工具：
{tools}

请给出从现在起还需要执行的新步骤（替换失败的步骤和还没执行的步骤）。不要重复已成功的步骤，新步骤的 id 不能与已成功步骤的 id 相同。"""

SOLVER_PROMPT = """任务：{task}

以下是按计划执行得到的工具数据（只能依据这些数据作答，不要编造）：
{evidence}"""

MAX_REPLANS = 2


def fmt_step(s: PlanStep) -> str:
    return f"{s.id}: {s.tool}({','.join(s.args.values())})"


def run_plan_execute(llm) -> None:
    section("架构 2：Plan-and-Execute —— 先规划，再执行，失败才重规划")
    note("模型只在三个时刻思考：开头规划一次、某步失败时重规划、最后汇总一次。中间的执行全由代码完成，不经过模型。")
    outputs: dict = {}
    with measure("Plan-and-Execute", "开头规划 + 失败重规划 + 最后汇总", outputs):
        registry = ToolRegistry(TOOLS)
        plan = complete_json(llm, PLANNER_PROMPT.format(tools=tools_doc(), task=TASK), Plan).steps
        print("\n  📋 规划器给出的计划（1 次模型调用）：")
        for s in plan:
            print(f"    {fmt_step(s)}")

        results: dict[str, str] = {}
        remaining, replans = list(plan), 0
        print("\n  ⚙️  执行（由代码逐步调用工具，不经过模型）：")
        while remaining:
            step = remaining.pop(0)
            r = registry.execute(ToolCall(id=step.id, name=step.tool, arguments=json.dumps(step.args, ensure_ascii=False)))
            if r.ok:
                results[step.id] = r.content
                print(f"    ✅ {fmt_step(step)}")
                continue
            print(f"    ❌ {fmt_step(step)} → {r.content[:60]}")
            if replans >= MAX_REPLANS:
                print(f"    🛑 已重规划 {replans} 次，达到上限，带着已有数据去汇总")
                break
            replans += 1
            new_plan = complete_json(
                llm,
                REPLANNER_PROMPT.format(
                    task=TASK,
                    done="\n".join(f"- {k}: {v}" for k, v in results.items()) or "（无）",
                    failed=fmt_step(step),
                    error=r.content,
                    remaining="；".join(fmt_step(s) for s in remaining) or "（无）",
                    tools=tools_doc(),
                ),
                Plan,
            ).steps
            # 简化处理：跳过与已成功步骤重名的步骤（练习 1 里你会把它当作非法计划，更严格）
            remaining = [s for s in new_plan if s.id not in results]
            print(f"    🔁 重规划（第 {replans} 次，1 次模型调用）→ " + "；".join(fmt_step(s) for s in remaining))

        evidence = "\n".join(f"- {v}" for v in results.values())
        answer = complete(llm, SOLVER_PROMPT.format(task=TASK, evidence=evidence))
        print("\n  🤖 汇总输出（1 次模型调用）：")
        show_block(answer)
        outputs["text"] = answer


# ====================================================================== 架构 3：Reflection

CRITIC_PROMPT = """你是严格的差旅审稿人。请对照工具返回的数据，检查下面这份出差建议：
1. 有没有与数据矛盾、或数据里根本没有的内容（编造）；
2. 有没有遗漏数据里的风险（预警、强降雨）；
3. 建议是否具体、可执行，是否遵守了任务的格式要求。
全部满足就 passed=true、feedback 留空；否则 passed=false，feedback 用一两句话指出最重要的问题。

任务：{task}
工具数据：
{evidence}
出差建议：
{draft}"""

REVISE_PROMPT = "审稿意见：{feedback}\n请据此修改你上一版的出差建议。仍然遵守原来的格式要求，只输出修改后的完整建议。"


def run_reflection(llm) -> None:
    section("架构 3：Reflection —— 先写初稿，再挑错，再修改")
    note("生成者是一个 ReAct Agent；批评者手里有工具数据（外部依据），先用代码检查，代码查不出的再交给模型。")
    outputs: dict = {}
    with measure("Reflection", "ReAct 写稿 + 模型/代码挑错 + 修改", outputs):
        writer = Agent(llm, TOOLS, system_prompt=REACT_PROMPT, name="writer", max_steps=8)
        last: dict = {}
        log: list[str] = []

        def generate(task: str, feedback: str | None) -> str:
            if feedback is None:
                last["result"] = writer.run(task)
            else:  # 带着完整历史继续对话：修改时仍然看得到之前的工具数据，也可以再调工具
                last["result"] = writer.run(REVISE_PROMPT.format(feedback=feedback), history=last["result"].history)
            return last["result"].output or ""

        def evaluate(draft: str) -> Review:
            evidence = tool_evidence(last["result"].messages)
            lines = [ln for ln in draft.splitlines() if ln.strip()]
            problems = []  # 1) 代码检查：确定、免费，不会被说服
            if len(lines) > MAX_LINES:
                problems.append(f"共 {len(lines)} 行，超过 {MAX_LINES} 行上限")
            missing = [c for c, _ in TRIP if c not in draft]
            if missing:
                problems.append(f"没有提到{'、'.join(missing)}")
            if any("预警" in e for e in evidence) and "台风" not in draft:
                problems.append("工具数据里有台风预警（航班可能延误或取消），但建议里没有提醒")
            if problems:
                log.append("代码检查")
                return Review(passed=False, feedback="；".join(problems) + "。")
            log.append("模型检查")  # 2) 模型检查：对照数据核对事实和可执行性
            prompt = CRITIC_PROMPT.format(task=TASK, evidence="\n".join(f"- {e}" for e in evidence), draft=draft)
            return complete_json(llm, prompt, Review)

        final, reviews = evaluator_optimizer(generate, evaluate, TASK, max_rounds=3)
        verdicts = [
            f"    🔍 第 {i} 轮评审 · {who} → " + ("✅ 通过" if r.passed else f"❌ 退回：{r.feedback}")
            for i, (r, who) in enumerate(zip(reviews, log), 1)
        ]
        print("\n  执行轨迹（初稿是一次完整的 ReAct 执行；之后每次修改都带着完整历史继续对话）：")
        show_trajectory(last["result"].messages, between_turns=verdicts)
        print(verdicts[-1])  # 最后一轮评审之后没有新的用户消息，单独打印
        print(f"\n  🤖 最终输出（{'评审通过' if reviews[-1].passed else '达到轮数上限仍未通过 → 应转人工'}）：")
        show_block(final)
        outputs["text"] = final
    note("agentkit 的 evaluator_optimizer 不会发现\"同一条意见反复出现\"—— 那种情况继续改只会烧钱。练习 2 的 reflect_loop 会补上这一点。")


# ====================================================================== 离线剧本

REACT_ANSWER = """北京 10-15：晴，8~19°C，早晚温差 11°C，带件风衣或薄外套。
上海 10-16：小雨转中雨，17~22°C，带长柄伞、穿防水鞋，出行多留时间。
广州 10-17：暴雨，24~28°C，穿速干衣物，带雨具。
风险：广州 10-17 有台风蓝色预警，白云机场航班可能大面积延误或取消，建议把广州行程提前或改线上，并订可退改的机票。"""

PLAN_ANSWER = """北京（10-15）：晴，8~19°C，温差大，带薄外套。
上海（10-16）：小雨转中雨，17~22°C，带伞、穿防水鞋。
广州（10-17）：暴雨，24~28°C，穿速干衣物、带雨具。
风险：广州当天台风蓝色预警，航班可能延误或取消，建议调整广州行程并预留改签余地。"""

DRAFT_WITHOUT_TYPHOON = """北京 10-15：晴，8~19°C，早晚凉，带件外套。
上海 10-16：有雨，17~22°C，记得带伞。
广州 10-17：暴雨，24~28°C，带伞、穿凉鞋。
风险：上海和广州都有雨，注意交通。"""


def offline_scripts() -> dict[str, list]:
    first_round = call_tools(
        ("get_weather", {"city": "北京", "date": "10-15"}),
        ("get_weather", {"city": "上海", "date": "10-16"}),
        ("get_weather", {"city": "广州", "date": "10-17"}),
    )
    plan = {"steps": [{"id": f"s{i}", "tool": "get_weather", "args": {"city": c, "date": d}} for i, (c, d) in enumerate(TRIP, 1)]}
    replan = {"steps": [{"id": "s3b", "tool": "get_weather_by_airport", "args": {"airport_code": "CAN", "date": "10-17"}}]}
    return {
        "react": [first_round, call_tool("get_weather_by_airport", airport_code="CAN", date="10-17"), reply(REACT_ANSWER)],
        "plan_execute": [
            reply(json.dumps(plan, ensure_ascii=False)),
            reply(json.dumps(replan, ensure_ascii=False)),
            reply(PLAN_ANSWER),
        ],
        # 剧本里初稿故意漏掉台风预警，用来演示批评环节能抓到什么
        "reflection": [
            call_tools(
                ("get_weather", {"city": "北京", "date": "10-15"}),
                ("get_weather", {"city": "上海", "date": "10-16"}),
                ("get_weather", {"city": "广州", "date": "10-17"}),
            ),
            call_tool("get_weather_by_airport", airport_code="CAN", date="10-17"),
            reply(DRAFT_WITHOUT_TYPHOON),
            reply(REACT_ANSWER),
            reply(json.dumps({"passed": True, "feedback": ""})),
        ],
    }


# ====================================================================== main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="使用 ScriptedLLM 剧本，不调用真实模型")
    args = parser.parse_args()

    if args.offline:
        print("🧪 离线模式：ScriptedLLM 剧本（输出是预先写好的；调用次数和数据流与真实运行一致；token 按文本长度估算；耗时无参考意义）")
        scripts = offline_scripts()
        llms = {name: Metered(Estimated(ScriptedLLM(items)), METER) for name, items in scripts.items()}
    else:
        real = Metered(default_llm(), METER)
        print(f"🌐 真实模型模式：{real.model}（如需离线运行，加 --offline）")
        llms = dict.fromkeys(["react", "plan_execute", "reflection"], real)

    print(f"\n📝 任务：{TASK}")
    run_react(llms["react"])
    run_plan_execute(llms["plan_execute"])
    run_reflection(llms["reflection"])

    section("对比：同一个任务，三种架构的价格和结果")
    header = f"  {pad('架构', 18)}{pad('模型调用', 10)}{pad('工具调用', 10)}{pad('tokens', 9)}{pad('耗时', 8)}{pad('质量检查', 28)}模型在哪里思考"
    print(header)
    for row in SUMMARY:
        check, _ = quality(row["output"])
        seconds = f"{row['seconds']:.1f}s"
        print(
            f"  {pad(row['name'], 18)}{pad(str(row['llm_calls']), 10)}{pad(str(row['tool_calls']), 10)}"
            f"{pad(str(row['tokens']), 9)}{pad(seconds, 8)}{pad(check, 28)}{row['who']}"
        )
    note(
        """ReAct 最灵活：意外发生时，下一步自然就绕过去了；代价是每一步都要把越来越长的历史重新发给模型。
        Plan-and-Execute 的执行阶段不经过模型：计划对了就又快又省，计划错了要靠重规划兜底（练习 1）。
        Reflection 在任何架构之上再加一层"挑错 + 修改"：批评者手里有外部依据（工具数据、代码检查）时才真正有用（练习 2）。
        质量检查一栏是用几行代码做的最小评估 —— 架构选型要靠这类数据说话，而不是靠感觉（第 11 课）。"""
    )
    print("\n  下一步：完成 exercise.py，然后运行 make lesson N=05")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        if "LLM_API_KEY" not in str(e):
            raise
        print(f"\n❌ {e}\n提示：没有 API key 时可以加 --offline 运行。")
        sys.exit(1)
