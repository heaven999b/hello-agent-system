"""第 01 课 Demo（框架版）：同一个任务，用 agentkit.Agent 实现，并和 demo_raw.py 对比。

    .venv/bin/python lessons/01_agent_loop/demo.py            # 真实模型
    .venv/bin/python lessons/01_agent_loop/demo.py --offline  # 离线剧本，无需 API key

看点：
  第 1 部分：一个"打印事件"的 Hook 让你看到主循环的每个节拍；追踪树让你看到每一步的耗时和 token。
  第 2 部分：Agent 的 5 种结束方式（最终答案 / 步数上限 / 预算 / 模型故障 / 等待审批）。
"""

from __future__ import annotations

import sys
from typing import Annotated

from pydantic import Field

from agentkit import (
    Agent,
    BudgetHook,
    Hook,
    LLMError,
    PermissionPolicy,
    ResilientLLM,
    ScriptedLLM,
    call_tool,
    call_tools,
    default_llm,
    render_tree,
    reply,
    tool,
)

FAKE_WEATHER = {"北京": 31, "上海": 27, "广州": 33}


@tool
def get_weather(city: Annotated[str, Field(description="城市中文名，例如：北京")]) -> dict:
    """查询一个城市当前的气温（摄氏度）和天气状况。"""
    if city not in FAKE_WEATHER:
        return {"error": f"没有 {city} 的数据，目前只支持 {list(FAKE_WEATHER)}"}
    return {"city": city, "temp_c": FAKE_WEATHER[city], "condition": "晴"}


class EventPrinter(Hook):
    """一个什么都不改、只负责打印的钩子 —— 让你看见主循环在什么时刻调用了哪些钩子。"""

    def on_run_start(self, state, user_input):
        print(f"  [on_run_start] run_id={state.run_id}  用户输入：{user_input}")

    def before_llm(self, state, messages):
        print(f"  [before_llm]   第 {state.step} 步：准备把 {len(messages)} 条消息发给模型")

    def after_llm(self, state, response):
        what = "请求调用 " + ", ".join(f"{c.name}{c.arguments}" for c in response.tool_calls) if response.tool_calls else "给出最终答案"
        print(f"  [after_llm]    模型{what}  (finish_reason={response.finish_reason}, tokens={response.usage.total})")

    def before_tool(self, state, call, tool):
        print(f"  [before_tool]  即将执行 {call.name}，风险等级={tool.risk if tool else '未知工具'}")

    def after_tool(self, state, call, result):
        print(f"  [after_tool]   {call.name} → {'成功' if result.ok else '失败'}：{result.content[:60]}")

    def on_final(self, state, output):
        print("  [on_final]     拿到最终答案（这里是做脱敏/审查的地方，第 06 课）")

    def on_run_end(self, state):
        print(f"  [on_run_end]   运行结束 status={state.status}（这里是写审计日志的地方，第 06 课）")


def banner(title: str) -> None:
    print(f"\n{'═' * 70}\n{title}\n{'═' * 70}")


def part1(offline: bool) -> None:
    banner("第 1 部分：同一个任务，用 agentkit.Agent 实现")
    if offline:
        llm = ScriptedLLM([
            call_tools(("get_weather", {"city": "北京"}), ("get_weather", {"city": "上海"})),
            reply("北京更热：北京 31°C，上海 27°C，北京比上海高 4°C。"),
        ])
    else:
        llm = ResilientLLM(default_llm())  # 套一层重试/降级（第 05 课），对 Agent 完全透明
    agent = Agent(
        llm,
        [get_weather],
        system_prompt="你是天气助手。需要天气数据时调用工具，不要编造。回答简洁。",
        max_steps=5,
        hooks=[EventPrinter()],
    )

    question = "北京和上海现在哪个更热？高几度？"
    print(f"用户问题：{question}\n\n▶ 钩子事件流（主循环的每个节拍）：")
    result = agent.run(question)

    print(f"\n▶ 运行结果（RunResult）：")
    print(f"  output      = {result.output}")
    print(f"  status      = {result.status}    stop_reason = {result.stop_reason}")
    print(f"  steps       = {result.steps}    tools_called = {result.tools_called()}")
    print(f"  tokens      = {result.usage.input_tokens} 输入 + {result.usage.output_tokens} 输出    "
          f"估算成本 = ${result.cost_usd:.5f}（示例价格，见 agentkit/pricing.py）")
    print(f"  消息角色序列 = {[m['role'] for m in result.messages]}")

    print("\n▶ 追踪树（第 07 课详讲）：")
    print(render_tree(result.trace))

    print("\n💡 对照 demo_raw.py：消息序列完全一样（system → user → assistant(tool_calls) → tool × N → assistant）。")
    print("   框架没有改变 Agent 的本质，只是把 步数上限 / 钩子 / 检查点 / 追踪 / 成本统计 做成了标准件。")


def part2() -> None:
    banner("第 2 部分：Agent 的 5 种结束方式（离线剧本，结果确定）")

    @tool(risk="dangerous")
    def reset_password(employee_id: str) -> str:
        """重置员工的登录密码（高风险操作）。"""
        return f"{employee_id} 的密码已重置"

    scenarios = [
        ("模型给出最终答案", Agent(ScriptedLLM([reply("你好！")]), [get_weather])),
        ("模型一直调工具停不下来",
         Agent(ScriptedLLM([call_tool("get_weather", city="北京") for _ in range(3)]), [get_weather], max_steps=3)),
        ("token 预算耗尽",
         Agent(ScriptedLLM([call_tool("get_weather", city="北京") for _ in range(5)]), [get_weather],
               hooks=[BudgetHook(max_tokens=50)])),
        ("模型服务彻底不可用",
         Agent(ScriptedLLM([LLMError("503 服务过载", status_code=503, retryable=True)]), [get_weather])),
        ("高风险操作等待人工审批",
         Agent(ScriptedLLM([call_tool("reset_password", employee_id="E1001")]), [reset_password],
               hooks=[PermissionPolicy()])),
    ]
    print(pad("场景", 26) + pad("status", 11) + pad("stop_reason", 26) + pad("steps", 7) + "output")
    print("─" * 110)
    for label, agent in scenarios:
        r = agent.run("演示")
        out = r.output or ""
        out = out if len(out) <= 30 else out[:30] + "…"
        print(pad(label, 26) + pad(r.status, 11) + pad(str(r.stop_reason), 26) + pad(str(r.steps), 7) + out)

    print("\n💡 关键设计：不论以哪种方式结束，agent.run() 都「返回」一个 RunResult，而不是把异常抛给调用方。")
    print("   调用方（Web 接口、工单系统）只需要看 status 决定下一步：展示答案 / 提示重试 / 通知审批人。")
    print("   其中 paused 的运行已存入检查点，审批后用 agent.approve(run_id) 从断点继续（第 05、06 课）。")


def pad(s: str, width: int) -> str:
    """按终端显示宽度补空格（中文字符占两格），用来对齐表格。"""
    shown = sum(2 if ord(ch) > 127 else 1 for ch in s)
    return s + " " * max(1, width - shown)


def main() -> None:
    offline = "--offline" in sys.argv
    print("🎬 离线模式：用剧本扮演模型" if offline else "🌐 真实模型模式（每次结果可能略有不同）")
    try:
        part1(offline)
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ 第 1 部分调用模型失败：{type(e).__name__}: {e}\n   可以先用 --offline 运行，或 make check-env 检查配置。")
    part2()


if __name__ == "__main__":
    main()
