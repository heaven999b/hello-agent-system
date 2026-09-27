"""同一任务 · agentkit 版（基准线：前面课程里你亲手写的那套）。

    python lessons/20_frameworks_bridge/impl_agentkit.py            # 真实模型
    python lessons/20_frameworks_bridge/impl_agentkit.py --offline  # ScriptedLLM 剧本

对照要点（其余三个实现文件用同样的五个标题，方便并排阅读）：
  状态      RunState（消息历史、步数、用量、待审批调用），每一步存进 Checkpointer
  工具      @tool / Tool(fn, risk=...)：从类型注解自动生成 JSON Schema，风险等级是工具的属性
  循环      Agent._loop：while step < max_steps → 调模型 → 有工具调用就执行 → 没有就结束
  审批      PermissionPolicy 钩子抛 PauseRun → 状态落盘 → agent.approve(run_id, 决定) 从断点继续
  追踪      Tracer：agent.run / llm.chat / tool.* 嵌套 Span，render_tree 打印成树
"""

from __future__ import annotations

# --- bootstrap（四个 impl 文件相同：按路径加载同目录模块，不计入行数对比）---
import importlib.util
import sys
from pathlib import Path


def _load_sibling(name: str):
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


shared = _load_sibling("shared_tools")
# --- end bootstrap ---

import time

import agentkit
from agentkit import Agent, PermissionPolicy, ResilientLLM, ScriptedLLM, Tracer, call_tool, call_tools, default_llm, reply, tool


def build_agent(llm, desk) -> Agent:
    fns = desk.functions()
    tools = [
        tool(fns["search_kb"]),
        tool(fns["get_account_status"]),
        tool(fns["reset_password"], risk="dangerous"),  # 风险等级写在工具上，由权限钩子决定要不要审批
    ]
    return Agent(
        llm,
        tools,
        system_prompt=shared.SYSTEM_PROMPT,
        name="it-helpdesk",
        max_steps=8,
        hooks=[PermissionPolicy(ask_risks={"dangerous"})],  # approver=None → 抛 PauseRun 走异步审批
        tracer=Tracer(),
    )


def run(question: str = shared.QUESTION, approver=shared.auto_approver, llm=None) -> "shared.FrameworkResult":
    desk = shared.ITDesk(user_id="alice")
    agent = build_agent(llm or ResilientLLM(default_llm()), desk)
    approvals = []
    t0 = time.perf_counter()
    result = agent.run(question, metadata={"user_id": desk.user_id})
    while result.status == "paused":  # 暂停 = 状态已落盘，进程此时可以退出，审批人一小时后再来也行
        call = result.pending_approval
        ok = approver(call.name, call.parsed_args())
        approvals.append((call.name, call.parsed_args(), ok))
        result = agent.approve(result.run_id, ok, by="demo-approver")
    seconds = time.perf_counter() - t0
    spans = sum(1 for root in agent.tracer.traces for _ in root.walk())  # 想看整棵树：agentkit.tracing.render_tree(root)
    return shared.FrameworkResult(
        framework="agentkit",
        version=agentkit.__version__,
        answer=result.output or "",
        llm_calls=result.steps,
        tool_calls=desk.tool_names(),
        approvals=approvals,
        seconds=seconds,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        notes=[
            f"最终状态 {result.status}；审批日志 {len(agent.checkpointer.load(result.run_id).approval_log)} 条（谁、何时、批没批）",
            f"追踪：{len(agent.tracer.traces)} 棵 Span 树（run + resume），共 {spans} 个 Span",
        ],
        extra={"spans": spans, "traces": len(agent.tracer.traces)},
    )


def offline_llm() -> ScriptedLLM:
    """离线剧本：和真实模型最常见的轨迹一致——先并行查知识库和账号，再申请重置，最后作答。"""
    return ScriptedLLM(
        [
            call_tools(("search_kb", {"query": "VPN 证书过期"}), ("get_account_status", {})),
            call_tool("reset_password", reason="连续输错密码导致账号锁定，本人请求重置"),
            reply(
                "alice 你好：\n1. VPN 证书过期：打开 VPN 客户端 → 设置 → 证书 → 点击“更新证书”，完成后重新连接（KB-101）。\n"
                "2. 账号锁定：审批已通过，已解除锁定并向 a***@corp.example 发送一次性重置链接，15 分钟内有效（KB-201）。"
            ),
        ]
    )


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    res = run(llm=offline_llm() if offline else None)
    shared.print_result(res)
