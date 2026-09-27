"""第 02 课 Demo（原始版）：不用任何 Agent 框架，只用 openai SDK 手写一个 Agent。

    .venv/bin/python lessons/02_agent_loop/demo_raw.py            # 真实模型（读取 .env）
    .venv/bin/python lessons/02_agent_loop/demo_raw.py --offline  # 离线剧本，无需 API key

看点：Agent 没有魔法。它就是一个 for 循环 + 一个不断变长的 messages 列表。
"""

from __future__ import annotations

import json
import sys

from openai import OpenAI

from agentkit.config import env

# ═══════════════════════════ 最小 Agent：核心只有下面这几十行 ═══════════════════════════

# 1) 工具 = 普通 Python 函数 + 一份写给模型看的"说明书"（JSON Schema）
FAKE_WEATHER = {"北京": 31, "上海": 27, "广州": 33}


def get_weather(city: str) -> str:
    if city not in FAKE_WEATHER:
        return f"错误：没有 {city} 的天气数据，目前只支持 {list(FAKE_WEATHER)}"
    return json.dumps({"city": city, "temp_c": FAKE_WEATHER[city], "condition": "晴"}, ensure_ascii=False)


FUNCTIONS = {"get_weather": get_weather}
TOOLS = [{
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
}]


def run_agent(client, model: str, question: str, max_steps: int = 5) -> str:
    # 2) messages 就是 Agent 的全部"记忆"
    messages = [
        {"role": "system", "content": "你是天气助手。需要天气数据时调用工具，不要编造。回答简洁。"},
        {"role": "user", "content": question},
    ]
    for step in range(1, max_steps + 1):  # 3) 循环 + 步数上限
        resp = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
        choice = resp.choices[0]
        msg = choice.message

        # 4) 模型的回复（不论是答案还是工具调用）原样追加进历史
        assistant = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        messages.append(assistant)
        show_round(step, len(messages) - 1, choice.finish_reason, assistant)

        if not msg.tool_calls:  # 5) 没有工具调用 = 最终答案，循环结束
            show_all(messages)
            return msg.content or ""

        for tc in msg.tool_calls:  # 6) 执行每一个工具调用（可能是并行的多个），结果用 tool_call_id 配对
            try:
                result = FUNCTIONS[tc.function.name](**json.loads(tc.function.arguments or "{}"))
            except Exception as e:  # 错误也是一种"观察"，交给模型处理，而不是让程序崩溃
                result = f"错误：{type(e).__name__}: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            show_tool(messages[-1])

    return f"（达到最大步数 {max_steps}，任务未完成）"


# ═══════════════════════════ 以下只是打印和离线模式，可以跳过 ═══════════════════════════


def show_round(step: int, n_sent: int, finish_reason: str, assistant: dict) -> None:
    print(f"\n━━━━━━━━ 第 {step} 轮：把 {n_sent} 条消息发给模型 ━━━━━━━━")
    print(f"finish_reason = {finish_reason!r}")
    print("模型返回的 assistant 消息（原样追加进 messages）：")
    print(json.dumps(assistant, ensure_ascii=False, indent=2))
    if assistant.get("tool_calls"):
        print(f"👉 模型没有直接回答，而是请求调用 {len(assistant['tool_calls'])} 个工具。由我们的代码去执行：")


def show_tool(tool_msg: dict) -> None:
    print(f"   🔧 执行完毕，追加 tool 消息：{json.dumps(tool_msg, ensure_ascii=False)}")


def show_all(messages: list[dict]) -> None:
    print("\n━━━━━━━━ 最终的完整 messages（这就是 Agent 的全部状态）━━━━━━━━")
    for i, m in enumerate(messages):
        extra = ""
        if m.get("tool_calls"):
            extra = "  tool_calls=" + ", ".join(f"{c['function']['name']}{c['function']['arguments']}#{c['id'][-6:]}" for c in m["tool_calls"])
        if m["role"] == "tool":
            extra = f"  (回应 #{m['tool_call_id'][-6:]})"
        content = (m.get("content") or "").replace("\n", " ")
        print(f"[{i}] {m['role']:<9} {content[:60]}{extra}")


class OfflineClient:
    """离线模式：假装自己是 openai.OpenAI。

    内部用 agentkit 的 ScriptedLLM 按剧本出牌，再包装成和 openai SDK 一模一样的 ChatCompletion 对象，
    所以上面的 run_agent() 一个字都不用改。这也说明了：Agent 循环只依赖"消息进、消息出"这个接口。
    """

    def __init__(self):
        from types import SimpleNamespace

        from agentkit import ScriptedLLM, call_tools, reply

        self._llm = ScriptedLLM([
            call_tools(("get_weather", {"city": "北京"}), ("get_weather", {"city": "上海"})),  # 第 1 轮：并行调用两次
            reply("北京更热：北京 31°C，上海 27°C，北京比上海高 4°C。"),  # 第 2 轮：看到结果后给出答案
        ])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model: str, messages: list, tools=None, **_):
        from openai.types.chat import ChatCompletion

        r = self._llm.chat(messages, tools)
        message = {"role": "assistant", "content": r.content}
        if r.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in r.tool_calls
            ]
        return ChatCompletion.model_validate({
            "id": "offline", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls" if r.tool_calls else "stop", "message": message}],
            "usage": {"prompt_tokens": r.usage.input_tokens, "completion_tokens": r.usage.output_tokens,
                      "total_tokens": r.usage.total},
        })


def main() -> None:
    offline = "--offline" in sys.argv
    if offline:
        client, model = OfflineClient(), "scripted"
        print("🎬 离线模式：用剧本扮演模型（输出固定，便于对照讲义）")
    else:
        # max_retries=2：借用 SDK 自带的重试扛住偶发的 429/5xx。
        # （agentkit 故意关掉它，改用自己可观测、可控的 ResilientLLM —— 第 08 课讲为什么）
        client = OpenAI(base_url=env("LLM_BASE_URL"), api_key=env("LLM_API_KEY"), max_retries=2)
        model = env("LLM_MODEL", "gpt-5.5")
        print(f"🌐 真实模型：{model}（每次运行结果可能略有不同）")

    question = "北京和上海现在哪个更热？高几度？"
    print(f"\n用户问题：{question}")
    try:
        answer = run_agent(client, model, question)
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ 调用模型失败：{type(e).__name__}: {e}")
        print("   可以先用 --offline 离线运行；或执行 make check-env 检查 .env 配置。")
        sys.exit(1)
    print(f"\n✅ 最终答案：{answer}")
    print("\n回顾：模型自己从不执行任何东西。它只会输出文字或\"请帮我调用某某工具\"，")
    print("真正执行工具、把结果塞回 messages、决定何时停止的，都是我们写的这个 for 循环。")


if __name__ == "__main__":
    main()
