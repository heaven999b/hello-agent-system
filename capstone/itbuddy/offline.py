"""离线模式的"模型"：按关键词决定调用哪个工具的剧本（ScriptedLLM + responder），给部署演示和端到端测试用。

它不假装聪明，只给多进程部署提供一个**确定、零成本、带真实延迟**的负载：
- 同样的输入永远走同样的工具，断言可以写死；
- 只看消息、不看任何进程内状态：worker 被 kill -9 后，接手的进程从检查点恢复，会做出同样的决定；
- latency 用 asyncio.sleep 模拟模型耗时：一个 worker 进程里几十个会话的等待真正重叠。

真实模型的行为（会被骗、轨迹会变）用 run_evals.py 评估；攻击场景下的最坏情况用 ablation.py 的 CompromisedLLM。
"""

from __future__ import annotations

import json
import re

from agentkit import ScriptedLLM, call_tool, reply
from agentkit.types import LLMResponse, Message, calls_in

MODEL_NAME = "offline-scripted"


def offline_llm(latency: float = 0.2) -> ScriptedLLM:
    return ScriptedLLM(responder=respond, latency=latency, model=MODEL_NAME, keep_calls=0)


def respond(messages: list[Message]) -> LLMResponse:
    last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    if messages[-1].get("role") == "tool":
        return reply(_summarize(_tool_name(messages), messages[-1].get("content") or ""))
    if last_user != len(messages) - 1:  # 本轮已经回答过（不应该发生）：直接收尾
        return reply("好的。")
    return _decide(messages[last_user].get("content") or "")


def _decide(text: str) -> LLMResponse:
    if "密码" in text and ("重置" in text or "忘记" in text or "锁" in text):
        args: dict = {"reason": "忘记密码" if "忘记" in text else "账号被锁或需要重置"}
        m = re.search(r"([a-z][a-z0-9_]{1,30})\s*的(?:域账号)?密码", text)
        if m:
            args["target_user_id"] = m.group(1)
        return call_tool("reset_password", **args)
    if "报修" in text or ("工单" in text and any(w in text for w in ("提", "建", "开", "报"))):
        title = re.split(r"[，,。！!？?；;]", text.strip())[0][:40]
        return call_tool("create_ticket", title=title if len(title) >= 2 else "IT 问题", description=text[:2000],
                         category=_category(text), priority="medium")
    if "工单" in text:
        return call_tool("get_my_tickets", status="all")
    if any(w in text for w in ("故障", "断线", "坏了", "用不了", "很慢", "是不是出")):
        return call_tool("check_system_status", system=_system(text))
    m = re.search(r"查(?:一下|询)?\s*([a-z][a-z0-9_]{1,30})", text)
    if m:
        return call_tool("lookup_employee", query=m.group(1))
    return call_tool("search_kb", query=text[:100])


def _category(text: str) -> str:
    low = text.lower()
    if any(w in low for w in ("vpn", "wi-fi", "wifi", "网络")):
        return "network"
    if any(w in text for w in ("屏幕", "键盘", "鼠标", "打印机", "电脑", "显示器", "笔记本")):
        return "hardware"
    if any(w in text for w in ("账号", "密码", "权限")):
        return "account"
    return "software"


def _system(text: str) -> str:
    low = text.lower()
    for name in ("vpn", "email", "wifi", "printer", "erp"):
        if name in low:
            return name
    for word, name in (("邮箱", "email"), ("邮件", "email"), ("打印", "printer"), ("报销", "erp")):
        if word in text:
            return name
    return "all"


def _tool_name(messages: list[Message]) -> str:
    call_id = messages[-1].get("tool_call_id")
    for m in reversed(messages):
        for c in calls_in(m) if m.get("role") == "assistant" else ():
            if c.id == call_id:
                return c.name
    return "?"


def _inner(content: str) -> str:
    """去掉 ToolOutputGuard 加的 <untrusted_data> 外壳和安全提示，只看工具原始输出。"""
    m = re.search(r'<untrusted_data source="[^"]*" id="([^"]+)">\n(.*)\n</untrusted_data id="\1">', content, re.S)
    return m.group(2) if m else content


def _summarize(tool: str, content: str) -> str:
    raw = _inner(content)
    if raw.startswith(("拒绝", "错误")):
        return f"这个请求没有完成：{raw}"
    if tool == "create_ticket":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return f"工单系统返回：{raw[:200]}"
        dup = "（这是一次重放，工单系统按 Idempotency-Key 返回了已有的工单）" if data.get("duplicate_request") else ""
        return f"已为您创建工单 {data.get('ticket_id')}{dup}，IT 工程师会在 4 个工作小时内响应。"
    if tool == "reset_password":
        return raw
    if tool == "search_kb":
        hits = re.findall(r"\[(KB-\d+)\] ([^（\n]+)", raw)
        if not hits:
            return "知识库里没有找到相关文章，需要我帮您提交工单吗？"
        return "请参考知识库：" + "；".join(f"[{i}] {t}" for i, t in hits) + "。"
    if tool == "check_system_status":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300]
        bad = [f"{k}：{v.get('status')}（{v.get('incident')}，{v.get('summary')}）" for k, v in data.items()
               if v.get("status") != "operational"]
        return "当前已知故障：" + "；".join(bad) if bad else "目前没有已知故障，需要的话我可以帮您提交工单。"
    if tool == "get_my_tickets":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300]
        return f"您有 {len(data)} 张工单：" + "；".join(f"{t['ticket_id']}（{t['status']}）" for t in data)
    return f"查询结果：{raw[:300]}"
