"""Agent 主循环 —— 整个框架的心脏。

Agent 的本质只有一句话：**让模型在循环里调用工具，直到它给出最终答案**。

    messages = [system, user]
    loop:
        response = LLM(messages, tools)
        if 没有工具调用: return response.content          # 模型认为任务完成
        for call in response.tool_calls:
            result = 执行工具(call)
            messages.append(工具结果)                        # 结果喂回给模型，进入下一轮

第 01 课你会亲手写出这 20 行。这个文件是它的"企业版"，多了：
- 步数上限（防止死循环烧钱）
- 钩子（安全 / 权限 / 预算 / 审计 / 脱敏 可插拔）
- 上下文策略（历史太长时截断或摘要）
- 检查点（每一步都存盘，崩溃可恢复、可暂停等人工审批）
- 链路追踪（每次模型调用、工具调用都有 Span）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .hooks import Hook, PauseRun, StopRun
from .llm import LLM, LLMError
from .pricing import estimate_cost
from .state import Checkpointer, InMemoryCheckpointer, RunState
from .tools import IdempotencyStore, Tool, ToolContext, ToolRegistry, ToolResult
from .tracing import Span, Tracer
from .types import LLMResponse, Message, ToolCall, Usage, calls_in, system, tool_message, user

DEFAULT_SYSTEM_PROMPT = "你是一个严谨、有帮助的助手。需要外部信息或执行操作时使用工具；信息不足时直接说明，不要编造。"


class ContextStrategy(Protocol):
    """上下文策略：每次调用模型前，决定保留哪些历史消息（第 03 课）。"""

    def apply(self, messages: list[Message]) -> list[Message]: ...


@dataclass
class RunResult:
    output: str | None
    status: str  # completed / paused / max_steps / stopped / failed
    run_id: str
    steps: int
    usage: Usage
    cost_usd: float
    messages: list[Message]
    stop_reason: str | None = None
    pending_approval: ToolCall | None = None
    trace: Span | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def tools_called(self) -> list[str]:
        """按顺序列出本次运行中模型调用过的工具名（评估时常用）。"""
        return [c.name for m in self.messages if m.get("role") == "assistant" for c in calls_in(m)]


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: Iterable[Tool] | ToolRegistry = (),
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        name: str = "agent",
        max_steps: int = 10,
        hooks: Iterable[Hook] = (),
        context_strategy: ContextStrategy | None = None,
        checkpointer: Checkpointer | None = None,
        tracer: Tracer | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ):
        self.llm = llm
        self.registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        if idempotency_store is not None:
            self.registry.idempotency_store = idempotency_store
        self.system_prompt = system_prompt
        self.name = name
        self.max_steps = max_steps
        self.hooks = list(hooks)
        self.context_strategy = context_strategy
        self.checkpointer = checkpointer or InMemoryCheckpointer()
        self.tracer = tracer or Tracer()

    # ------------------------------------------------------------------ 公共 API

    def run(
        self,
        user_input: str,
        *,
        history: list[Message] | None = None,
        metadata: dict | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        """开始一次新的运行。metadata 放可信身份信息：tenant_id / user_id / roles。"""
        state = RunState(metadata=dict(metadata or {}))
        if run_id:
            state.run_id = run_id

        def prepare() -> None:
            text = user_input
            for h in self.hooks:
                new = h.on_run_start(state, text)
                if new is not None:
                    text = new
            state.messages = self._initial_messages(history, text)
            self._save(state)

        with self.tracer.span("agent.run", **{"agent.name": self.name, "run_id": state.run_id}) as span:
            pending = self._drive(state, prepare)
            self._annotate(span, state)
        return self._result(state, pending, span)

    def resume(self, run_id: str, approvals: dict[str, bool] | None = None) -> RunResult:
        """从检查点恢复运行：崩溃恢复，或人工审批之后继续。

        approvals: {tool_call_id: True/False}，True 批准执行，False 拒绝。
        """
        state = self.checkpointer.load(run_id)
        if state is None:
            raise KeyError(f"找不到 run_id={run_id} 的检查点")
        if state.status == "completed":
            return self._result(state, None, None)
        state.approvals.update(approvals or {})
        state.status, state.stop_reason, state.pending = "running", None, None
        with self.tracer.span("agent.resume", **{"agent.name": self.name, "run_id": run_id}) as span:
            pending = self._drive(state, None)
            self._annotate(span, state)
        return self._result(state, pending, span)

    def approve(self, run_id: str, approved: bool = True) -> RunResult:
        """便捷方法：对当前等待审批的那次工具调用给出决定并继续运行。"""
        state = self.checkpointer.load(run_id)
        if state is None or not state.pending:
            raise ValueError(f"run {run_id} 没有等待审批的操作")
        return self.resume(run_id, {state.pending["id"]: approved})

    # ------------------------------------------------------------------ 主循环

    def _drive(self, state: RunState, prepare) -> ToolCall | None:
        """执行主循环并把各种"非正常结束"统一收敛为状态。返回等待审批的调用（如果有）。"""
        pending: ToolCall | None = None
        try:
            if prepare:
                prepare()
            self._loop(state)
        except StopRun as e:
            state.status, state.stop_reason, state.output = "stopped", e.reason, e.message
        except PauseRun as e:
            pending = e.call
            state.status, state.stop_reason, state.output = "paused", "needs_approval", e.message
            state.pending = {"id": e.call.id, "name": e.call.name, "arguments": e.call.arguments}
        except LLMError as e:  # 模型彻底不可用（重试、降级都失败了）
            state.status, state.stop_reason = "failed", f"llm_error: {e}"
            state.output = "抱歉，服务暂时不可用，请稍后再试。"
        finally:
            for h in self.hooks:
                h.on_run_end(state)
            self._save(state)
        return pending

    def _loop(self, state: RunState) -> None:
        # 断点续跑：如果上次停在"模型已发起工具调用、但工具还没执行完"，先把它们补完
        self._run_pending_tools(state)
        while state.step < self.max_steps:
            state.step += 1
            response = self._call_llm(state)
            state.messages.append(response.to_message())
            self._save(state)

            if not response.tool_calls:  # 没有工具调用 = 模型给出了最终答案
                output = response.content or ""
                for h in self.hooks:
                    new = h.on_final(state, output)
                    if new is not None:
                        output = new
                state.messages[-1]["content"] = output  # 历史里也存处理后的版本（如已脱敏）
                state.status, state.stop_reason, state.output = "completed", "final_answer", output
                return

            self._run_pending_tools(state)

        state.status, state.stop_reason = "max_steps", "max_steps"
        state.output = f"（已达到最大步数 {self.max_steps}，任务未完成。）"

    def _call_llm(self, state: RunState) -> LLMResponse:
        if self.context_strategy is not None:
            state.messages = self.context_strategy.apply(state.messages)
        for h in self.hooks:
            h.before_llm(state, state.messages)

        visible = self.registry.names()
        for h in self.hooks:
            visible = h.visible_tools(state, visible)
        tools = self.registry.schemas(visible) or None

        with self.tracer.span(
            "llm.chat",
            **{"gen_ai.request.model": getattr(self.llm, "model", "?"), "step": state.step, "messages": len(state.messages)},
        ) as span:
            response = self.llm.chat(state.messages, tools=tools)
            span.set(
                **{
                    "gen_ai.response.model": response.model,
                    "gen_ai.usage.input_tokens": response.usage.input_tokens,
                    "gen_ai.usage.output_tokens": response.usage.output_tokens,
                    "finish_reason": response.finish_reason,
                    "result": ("tool_calls: " + ", ".join(c.name for c in response.tool_calls))
                    if response.tool_calls
                    else "final_answer",
                }
            )

        state.usage = state.usage + response.usage
        state.cost_usd += estimate_cost(response.usage, response.model or getattr(self.llm, "model", "default"))
        for h in self.hooks:
            h.after_llm(state, response)
        return response

    def _run_pending_tools(self, state: RunState) -> None:
        """执行最后一条 assistant 消息里、还没有结果的工具调用。"""
        idx = next((i for i in range(len(state.messages) - 1, -1, -1) if state.messages[i]["role"] == "assistant"), None)
        if idx is None:
            return
        done = {m.get("tool_call_id") for m in state.messages[idx + 1 :] if m["role"] == "tool"}
        for call in calls_in(state.messages[idx]):
            if call.id in done:
                continue
            result = self._execute_tool(state, call)
            state.messages.append(tool_message(call.id, result.content))
            self._save(state)  # 每个工具执行完都存盘，缩小"执行了但没记录"的窗口

    def _execute_tool(self, state: RunState, call: ToolCall) -> ToolResult:
        t = self.registry.get(call.name)
        with self.tracer.span(
            f"tool.{call.name}",
            **{"tool.name": call.name, "tool.arguments": call.arguments[:500], "tool.risk": t.risk if t else None},
        ) as span:
            denial = None
            for h in self.hooks:
                denial = h.before_tool(state, call, t)  # 可能抛 PauseRun 等待审批
                if denial:
                    break
            if denial:
                result = ToolResult(False, denial, "denied")
            else:
                ctx = ToolContext(
                    run_id=state.run_id,
                    call_id=call.id,
                    tenant_id=state.metadata.get("tenant_id"),
                    user_id=state.metadata.get("user_id"),
                    roles=tuple(state.metadata.get("roles", ())),
                )
                result = self.registry.execute(call, ctx)
                state.tool_calls_count += 1
            for h in self.hooks:
                new = h.after_tool(state, call, result)
                if new is not None:
                    result = new
            span.set(**{"tool.ok": result.ok, "tool.error_type": result.error_type})
        return result

    # ------------------------------------------------------------------ 辅助

    def _initial_messages(self, history: list[Message] | None, text: str) -> list[Message]:
        msgs: list[Message] = [system(self.system_prompt)] if self.system_prompt else []
        msgs += [m for m in (history or []) if m.get("role") != "system"]
        msgs.append(user(text))
        return msgs

    def _save(self, state: RunState) -> None:
        self.checkpointer.save(state)

    @staticmethod
    def _annotate(span: Span, state: RunState) -> None:
        span.set(
            **{
                "agent.status": state.status,
                "agent.steps": state.step,
                "agent.cost_usd": round(state.cost_usd, 6),
                "gen_ai.usage.input_tokens": state.usage.input_tokens,
                "gen_ai.usage.output_tokens": state.usage.output_tokens,
            }
        )

    @staticmethod
    def _result(state: RunState, pending: ToolCall | None, span: Span | None) -> RunResult:
        return RunResult(
            output=state.output,
            status=state.status,
            run_id=state.run_id,
            steps=state.step,
            usage=state.usage,
            cost_usd=state.cost_usd,
            messages=state.messages,
            stop_reason=state.stop_reason,
            pending_approval=pending,
            trace=span,
            metadata=state.metadata,
        )
