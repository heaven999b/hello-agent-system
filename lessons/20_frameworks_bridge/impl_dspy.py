"""同一任务 · DSPy 版（声明式：你写"输入什么、输出什么"，提示词由框架生成）。

    pip install dspy
    python lessons/20_frameworks_bridge/impl_dspy.py

对照要点：
  状态      没有"运行状态"对象：ReAct 把轨迹（thought / tool_name / tool_args / observation）拼成一个字符串字段
  工具      普通函数或 dspy.Tool；**不走原生 function calling**——工具说明被写进提示词，
            模型在 next_tool_name / next_tool_args 两个输出字段里"用文字"选工具
  循环      dspy.ReAct.forward：最多 max_iters 轮，模型选了内置的 finish 工具就停，
            之后再用一次 ChainOfThought 从轨迹里"抽取"最终答案（所以总会多一次模型调用）
  审批      没有暂停 / 恢复机制。只能在工具内部当场问审批人、等他答复（相当于 agentkit 的 PermissionPolicy(approver=...)，
            而不是 PauseRun 落盘暂停）；需要"等人几小时"就得把 DSPy 程序放进 LangGraph / Temporal 之类的运行时里
  追踪      lm.history（每次调用的消息、用量、成本）、dspy.inspect_history()、BaseCallback 回调、MLflow 集成
  async     模块同步调用 agent(question=...)；async 入口是 await agent.acall(question=...)（本文件用它）。
            async 路径上，同步的工具函数直接在事件循环里执行（不进线程池）：慢工具要写成 async def

连接本地网关：dspy.LM("openai/<模型名>", api_base=..., api_key=...)。
DSPy 3.4 起默认 engine="auto"：能用原生 lm15 引擎就用，否则回退到 LiteLLM；想强制走 LiteLLM
可以设环境变量 DSPY_ENGINE=litellm（两种我们都在本地网关上跑通过）。
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

import asyncio
import functools
import inspect
import os
import time

import dspy
from dspy.utils.callback import BaseCallback


class ITHelpdesk(dspy.Signature):
    __doc__ = shared.SYSTEM_PROMPT  # docstring 就是"指令"——也是优化器（第 23 课）会改写的那部分

    question: str = dspy.InputField(desc="员工的 IT 问题")
    answer: str = dspy.OutputField(desc="给员工的中文答复，注明参考的知识库文章编号")


class EventCounter(BaseCallback):
    """最小的 DSPy 回调：数一数模块、模型、工具各被调用了几次（真实项目里在这里转发到 OpenTelemetry）。"""

    def __init__(self):
        self.events: dict[str, int] = {}

    def _hit(self, kind: str) -> None:
        self.events[kind] = self.events.get(kind, 0) + 1

    def on_module_start(self, call_id, instance, inputs):
        self._hit(f"module:{type(instance).__name__}")

    def on_lm_start(self, call_id, instance, inputs):
        self._hit("lm")

    def on_tool_start(self, call_id, instance, inputs):
        self._hit(f"tool:{instance.name}")


def with_approval(fn, approver, approvals: list):
    """DSPy 没有 interrupt：审批只能在工具内部当场等审批人答复——审批人不回应，这次运行就停在这里，
    状态也不落盘（进程重启就丢）。写成 async：审批人可以是 async 函数（发 IM 卡片、等回调），等待时不卡事件循环。"""

    @functools.wraps(fn)  # 保留函数名、docstring、类型注解，dspy.Tool 靠它们生成工具说明
    async def guarded(**kwargs):
        ok = approver(fn.__name__, kwargs)
        if inspect.isawaitable(ok):
            ok = await ok
        approvals.append((fn.__name__, kwargs, ok))
        return fn(**kwargs) if ok else "审批人拒绝了该操作。请告诉用户该操作未获批准，不要重试。"

    return guarded


def make_lm(model: str | None = None) -> dspy.LM:
    cfg = shared.gateway_config()
    return dspy.LM(
        f"openai/{model or cfg['model']}",  # "openai/" 前缀 = 按 OpenAI 兼容协议调用
        api_base=cfg["base_url"],
        api_key=cfg["api_key"],
        cache=False,  # DSPy 默认缓存模型响应：不关的话，第二次运行会"0 秒完成、0 次调用"，对比就失真了
        num_retries=3,
        engine=os.environ.get("DSPY_ENGINE", "auto"),
    )


async def run(question: str = shared.QUESTION, approver=shared.auto_approver, lm=None) -> "shared.FrameworkResult":
    desk = shared.ITDesk(user_id="alice")
    fns = desk.functions()
    approvals: list = []
    tools = [
        dspy.Tool(fns["search_kb"]),
        dspy.Tool(fns["get_account_status"]),
        dspy.Tool(with_approval(fns["reset_password"], approver, approvals)),
    ]
    agent = dspy.ReAct(ITHelpdesk, tools=tools, max_iters=8)
    lm = lm or make_lm()
    counter = EventCounter()
    seen = len(getattr(lm, "history", []))  # 同一个 LM 对象可能被复用：只统计本次运行新增的记录
    t0 = time.perf_counter()
    with dspy.context(lm=lm, callbacks=[counter]):  # context 只影响这段代码（contextvars 实现，async 里也安全）
        pred = await agent.acall(question=question)  # async 入口；同步写法是 agent(question=question)
    seconds = time.perf_counter() - t0
    usage = [h.get("usage") or {} for h in getattr(lm, "history", [])[seen:]]
    steps = sum(1 for k in pred.trajectory if k.startswith("tool_name_"))
    return shared.FrameworkResult(
        framework="DSPy",
        version=dspy.__version__,
        answer=pred.answer,
        llm_calls=counter.events.get("lm", 0),
        tool_calls=desk.tool_names(),
        approvals=approvals,
        seconds=seconds,
        input_tokens=sum(u.get("prompt_tokens", 0) for u in usage) if usage else None,
        output_tokens=sum(u.get("completion_tokens", 0) for u in usage) if usage else None,
        notes=[
            f"ReAct 轨迹 {steps} 步（最后一步是内置的 finish 工具），另加 1 次 ChainOfThought 抽取答案",
            f"回调事件：{counter.events}",
            "审批：在工具里当场等审批人（没有检查点，进程重启就丢）",
        ],
        extra={"events": dict(counter.events), "react_steps": steps},
    )


if __name__ == "__main__":
    lm = make_lm()
    shared.print_result(asyncio.run(run(lm=lm)))
    print("\n—— DSPy 替你生成的第一条 system 提示词（前 1200 字）——")
    print(lm.history[0]["messages"][0]["content"][:1200])
