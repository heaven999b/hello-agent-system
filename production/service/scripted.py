"""离线模式的"模型"：带延迟的 AsyncScriptedLLM + 一个按对话内容决定下一步的 responder。

压测和端到端测试不能调真实模型（贵、慢、不确定），但又要让 Agent 走完真实的路径：
多轮工具调用、并行只读工具、写工具、需要审批的高危工具、流式文本。responder 只看"最后一条用户消息"
和"之后已经走了几轮工具"，所以同一个 run 在另一个 worker 上 resume 时，会从断点接着给出下一步 ——
这正是检查点恢复要验证的东西。

用户消息里的关键词决定剧本（压测脚本按这些关键词构造请求）：
    "密码"  → reset_password（需要审批）→ 回答
    "诊断"  → search_kb → check_system_status → run_diagnostics（慢）→ create_ticket → 回答     （长任务）
    "工单"  → create_ticket → 回答
    "VPN"  → 同一轮并行 search_kb + check_system_status → 回答
    其余    → 直接回答
"""

from __future__ import annotations

import json
import random

from agentkit import call_tool, call_tools, reply
from agentkit.aio import AsyncScriptedLLM
from agentkit.types import LLMResponse, Message, Usage


def _last_user(messages: list[Message]) -> tuple[int, str]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i, messages[i].get("content") or ""
    return -1, ""


def _tool_results(messages: list[Message], start: int) -> list[str]:
    return [m.get("content") or "" for m in messages[start:] if m.get("role") == "tool"]


def _ticket_no(results: list[str]) -> str | None:
    for r in reversed(results):
        try:
            data = json.loads(r)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("ticket_no"):
            return data["ticket_no"]
    return None


def _answer(text: str, tokens_in: int = 180, tokens_out: int = 60) -> LLMResponse:
    r = reply(text)
    r.usage = Usage(tokens_in, tokens_out)
    return r


def respond(messages: list[Message]) -> LLMResponse:
    idx, text = _last_user(messages)
    rounds = sum(1 for m in messages[idx + 1:] if m.get("role") == "assistant" and m.get("tool_calls"))
    results = _tool_results(messages, idx + 1)
    last = results[-1] if results else ""

    if "密码" in text:
        if rounds == 0:
            return call_tool("reset_password", reason="忘记密码，账号被锁定")
        if last.startswith("拒绝") or "未获批准" in last or "没有批准" in last:
            return _answer("审批人没有批准这次密码重置。如需帮助请联系 IT 值班工程师。")
        return _answer("已为您发起密码重置，一次性链接已发送到企业邮箱，30 分钟内有效。IT 永远不会向您索要密码。")

    if "诊断" in text:
        plan = [
            lambda: call_tool("search_kb", query="VPN 断线"),
            lambda: call_tool("check_system_status", system="vpn"),
            lambda: call_tool("run_diagnostics", system="vpn"),
            lambda: call_tool("create_ticket", title="VPN 频繁断线", description=text[:200], category="network", priority="high"),
        ]
        if rounds < len(plan):
            return plan[rounds]()
        return _answer(f"诊断完成：VPN 存在丢包（已知故障 INC-2041），已为您创建工单 {_ticket_no(results) or '（未知）'}，"
                       "网络组会在 4 个工作小时内联系您。临时方案：在客户端切换为 TCP 443 模式。")

    if "工单" in text or "报修" in text:
        if rounds == 0:
            return call_tool("create_ticket", title="设备故障报修", description=text[:200], category="hardware")
        return _answer(f"已为您创建工单 {_ticket_no(results) or '（未知）'}，IT 工程师会在 4 个工作小时内响应。")

    if "VPN" in text.upper():
        if rounds == 0:  # 同一轮两个只读工具：AsyncAgent 会并行执行
            return call_tools(("search_kb", {"query": "VPN"}), ("check_system_status", {"system": "vpn"}))
        return _answer("根据 [KB-001]：先确认 AcmeConnect 已安装并完成多因素认证；遇到错误 809 请切换为 TCP 443 模式。"
                       "另外当前有已知故障 INC-2041（上海办公室 VPN 间歇性断连），预计 18:00 前恢复。")

    return _answer("您好，我是 IT 服务台助手，可以帮您查知识库、查系统状态、提工单和发起密码重置。")


def scripted_llm(latency_s: float, jitter: float = 0.3, seed: int | None = None) -> AsyncScriptedLLM:
    """每次调用耗时 latency_s × (1 ± jitter)。真实模型的延迟不是常数（第 30 课 3.6：两次运行差一倍），压测要带上抖动。"""
    rng = random.Random(seed)

    def latency(_n: int) -> float:
        return max(0.0, latency_s * (1 + jitter * (2 * rng.random() - 1)))

    return AsyncScriptedLLM(responder=respond, latency=latency, model="scripted", stream_chunk=8)
