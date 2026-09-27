"""Agent 主循环 —— 整个框架的心脏。

Agent 的本质只有一句话：**让模型在循环里调用工具，直到它给出最终答案**。

    messages = [system, user]
    loop:
        response = LLM(messages, tools)
        if 没有工具调用: return response.content          # 模型认为任务完成
        for call in response.tool_calls:
            result = 执行工具(call)
            messages.append(工具结果)                        # 结果喂回给模型，进入下一轮

第 02 课你会亲手写出这 20 行。这个文件是它的"企业版"，多了：
- 步数上限（防止死循环烧钱）
- 钩子（安全 / 权限 / 预算 / 审计 / 脱敏 可插拔）
- 上下文策略（历史太长时截断或摘要）
- 检查点（每一步都存盘，崩溃可恢复、可暂停等人工审批）
- 链路追踪（每次模型调用、工具调用都有 Span）
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .hooks import Hook, PauseRun, StopRun
from .llm import LLM, LLMError
from .pricing import estimate_cost
from .state import Checkpointer, InMemoryCheckpointer, RunState
from .guardrails import redact_pii
from .tools import IdempotencyStore, Tool, ToolContext, ToolRegistry, ToolResult
from .tracing import Span, Tracer
from .types import LLMResponse, Message, ToolCall, Usage, calls_in, system, tool_message, user

DEFAULT_SYSTEM_PROMPT = "你是一个严谨、有帮助的助手。需要外部信息或执行操作时使用工具；信息不足时直接说明，不要编造。"


class ContextStrategy(Protocol):
    """上下文策略：每次调用模型前，决定保留哪些历史消息（第 04 课）。"""

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
    tool_log: list[dict] | None = None  # Agent 产生的结果总会填充；手工构造 RunResult（如测试）时可省略

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @property
    def history(self) -> list[Message]:
        """传给下一轮 run(history=...) 的对话历史（去掉 system 消息）。

        多轮对话请用它，而不是自己过滤 messages：输入被拦截时它保留之前的历史，
        运行中止时未执行的工具调用也已补上"未执行"结果，保证下一轮消息协议合法。
        """
        msgs = [m for m in self.messages if m.get("role") != "system"]
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "assistant"), None)
        if idx is not None:
            answered = {m.get("tool_call_id") for m in msgs[idx + 1 :] if m.get("role") == "tool"}
            missing = [c for c in calls_in(msgs[idx]) if c.id not in answered]
            if missing:  # 例如被取消的运行里、保持未回答的写操作：要继续它请用 resume，而不是开新对话
                msgs = msgs + [tool_message(c.id, "未执行：该调用属于一次未完成的运行，如需继续请恢复那次运行") for c in missing]
        return msgs

    def tools_called(self) -> list[str]:
        """按顺序列出**本次运行**中模型请求过的工具名（含被拒绝的；不含传入的历史消息里的调用）。评估时常用。"""
        if self.tool_log is None:  # 手工构造的结果：退回从消息里推断
            return [c.name for m in self.messages if m.get("role") == "assistant" for c in calls_in(m)]
        return [t["name"] for t in self.tool_log]


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
        self._run_locks: dict[str, threading.Lock] = {}
        self._run_locks_guard = threading.Lock()

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._run_locks_guard:
            return self._run_locks.setdefault(run_id, threading.Lock())

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
        state.messages = self._initial_messages(history)  # 先放历史：即使输入被拦截，历史也不会丢

        def prepare() -> None:
            text = user_input
            for h in self.hooks:
                new = h.on_run_start(state, text)  # 可能抛 StopRun（如注入检测）
                if new is not None:
                    text = new
            state.messages.append(user(text))
            self._save(state)

        with self.tracer.span("agent.run", **self._root_attrs(state)) as span:
            pending = self._drive(state, prepare)
            self._annotate(span, state, cost_before=0.0)
        return self._result(state, pending, span)

    def resume(self, run_id: str, approvals: dict[str, bool] | None = None) -> RunResult:
        """从检查点恢复运行：崩溃恢复，或人工审批之后继续。

        approvals: {tool_call_id: True/False}，True 批准执行，False 拒绝。
        """
        with self._lock_for(run_id):  # 同一个 run 的恢复/审批串行：两个线程同时审批，高危工具也只执行一次
            return self._resume_locked(run_id, approvals)

    def _resume_locked(self, run_id: str, approvals: dict[str, bool] | None) -> RunResult:
        state = self.checkpointer.load(run_id)
        if state is None:
            raise KeyError(f"找不到 run_id={run_id} 的检查点")
        if state.status == "completed":
            return self._result(state, None, None)
        state.approvals.update(approvals or {})
        state.status, state.stop_reason, state.pending = "running", None, None
        cost_before = state.cost_usd
        with self.tracer.span("agent.resume", **self._root_attrs(state)) as span:
            pending = self._drive(state, None)
            self._annotate(span, state, cost_before=cost_before)
        return self._result(state, pending, span)

    def approve(self, run_id: str, approved: bool = True, *, by: str | None = None, comment: str = "") -> RunResult:
        """对当前等待审批的那次工具调用给出决定并继续运行。

        by / comment 会写入 state.approval_log（审计要求记录"谁、何时、为什么"批准）。
        """
        with self._lock_for(run_id):
            state = self.checkpointer.load(run_id)  # 锁内重新读取：已被别人审批过的，这里会明确报错
            if state is None or not state.pending:
                raise ValueError(f"run {run_id} 没有等待审批的操作")
            call_id = state.pending["id"]
            state.approval_log.append(
                {"call_id": call_id, "tool": state.pending["name"], "approved": approved, "by": by, "comment": comment, "at": time.time()}
            )
            self._save(state)
            return self._resume_locked(run_id, {call_id: approved})

    # ------------------------------------------------------------------ 主循环

    def _drive(self, state: RunState, prepare) -> ToolCall | None:
        """执行主循环并把各种"非正常结束"统一收敛为状态。返回等待审批的调用（如果有）。"""
        pending: ToolCall | None = None
        state.segment_started_at = time.time()
        try:
            if prepare:
                prepare()
            self._loop(state)
        except StopRun as e:
            state.status, state.stop_reason, state.output = "stopped", e.reason, e.message
            self._close_dangling_calls(state, f"未执行：运行已中止（{e.reason}）")
        except PauseRun as e:
            pending = e.call
            state.status, state.stop_reason, state.output = "paused", "needs_approval", e.message
            state.pending = {"id": e.call.id, "name": e.call.name, "arguments": e.call.arguments}
        except LLMError as e:  # 模型彻底不可用（重试、降级都失败了）
            state.status, state.stop_reason = "failed", f"llm_error: {e}"
            state.output = "抱歉，服务暂时不可用，请稍后再试。"
        finally:
            state.active_seconds += time.time() - state.segment_started_at
            for h in self.hooks:
                h.on_run_end(state)
            self._save(state)
        return pending

    def _loop(self, state: RunState) -> None:
        # 断点续跑：如果上次停在"模型已发起工具调用、但工具还没执行完"，先把它们补完
        self._run_pending_tools(state)
        while state.step < self.max_steps:
            response = self._call_llm(state)  # 步数在里面、真正调用模型之前才加一
            state.messages.append(response.to_message())
            self._save(state)

            if not response.tool_calls:  # 没有工具调用 = 模型给出了最终答案
                output = response.content or ""
                for h in self.hooks:
                    new = h.on_final(state, output)
                    if new is not None:
                        output = new
                state.messages[-1]["content"] = output  # 历史里也存处理后的版本（如已脱敏）
                # finish_reason == "length"：输出被 max_tokens 截断了。仍然返回，但打上标记，
                # 让调用方/评估/告警能区分"完整答案"和"说到一半的答案"。
                reason = "output_truncated" if response.finish_reason == "length" else "final_answer"
                state.status, state.stop_reason, state.output = "completed", reason, output
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

        # 步数 = 真正发出的模型调用次数：before_llm 叫停（限流推迟、预算用完）的那一步没有发生，不计数
        state.step += 1
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
                    "gen_ai.usage.cache_read.input_tokens": response.usage.cached_input_tokens,
                    "gen_ai.usage.reasoning_tokens": response.usage.reasoning_tokens,
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

    @staticmethod
    def _unanswered_calls(state: RunState) -> list[ToolCall]:
        """最后一条 assistant 消息里、还没有 tool 结果的工具调用。"""
        idx = next((i for i in range(len(state.messages) - 1, -1, -1) if state.messages[i]["role"] == "assistant"), None)
        if idx is None:
            return []
        done = {m.get("tool_call_id") for m in state.messages[idx + 1 :] if m["role"] == "tool"}
        return [c for c in calls_in(state.messages[idx]) if c.id not in done]

    def _run_pending_tools(self, state: RunState) -> None:
        """执行最后一条 assistant 消息里、还没有结果的工具调用。"""
        for call in self._unanswered_calls(state):
            result = self._execute_tool(state, call)
            state.messages.append(tool_message(call.id, result.content))
            self._save(state)  # 每个工具执行完都存盘，缩小"执行了但没记录"的窗口

    def _close_dangling_calls(self, state: RunState, note: str) -> None:
        """运行中止时，给没执行的工具调用补一条结果。

        OpenAI 协议要求每个 tool_call 都有对应的 tool 消息，否则下一轮带着这段历史调用模型会直接 400。
        （暂停等审批时不补：那些调用在 resume 时还要真正执行。）
        """
        for call in self._unanswered_calls(state):
            state.messages.append(tool_message(call.id, note))

    def _execute_tool(self, state: RunState, call: ToolCall) -> ToolResult:
        t = self.registry.get(call.name)
        if all(entry["id"] != call.id for entry in state.tool_log):  # resume 时同一调用不重复记录
            state.tool_log.append({"id": call.id, "name": call.name})
        # 追踪系统通常比业务库有更多人能访问，所以参数和结果预览都先脱敏再记录。
        # 注意顺序：必须先脱敏再截断 —— 先截断可能把手机号切成半截，正则匹配不到，漏出部分数字。
        with self.tracer.span(
            f"tool.{call.name}",
            **{"tool.name": call.name, "gen_ai.tool.call.id": call.id, "tool.arguments": redact_pii(call.arguments)[:500], "tool.risk": t.risk if t else None},
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
                    # 其余可信属性（部门、用户组、数据区域……）原样透传给工具
                    extra={k: v for k, v in state.metadata.items() if k not in ("tenant_id", "user_id", "roles")},
                )
                state.tool_calls_count += 1  # 与异步版一致：执行前计数
                result = self.registry.execute(call, ctx)
            for h in self.hooks:
                new = h.after_tool(state, call, result)
                if new is not None:
                    result = new
            # 结果预览只截取前 200 字符：足够排查问题，又避免把大段数据/敏感信息灌进追踪系统
            span.set(**{"tool.ok": result.ok, "tool.error_type": result.error_type, "tool.result_preview": redact_pii(result.content)[:200]})
            if result.detail:
                span.set(**{"tool.error_detail": redact_pii(result.detail)[:500]})
        return result

    # ------------------------------------------------------------------ 辅助

    def _initial_messages(self, history: list[Message] | None) -> list[Message]:
        msgs: list[Message] = [system(self.system_prompt)] if self.system_prompt else []
        msgs += [dict(m) for m in (history or []) if m.get("role") != "system"]
        return msgs

    def _save(self, state: RunState) -> None:
        self.checkpointer.save(state)

    def _root_attrs(self, state: RunState) -> dict:
        # 租户 / 用户写在根 Span 上：按租户做成本归因、按用户排查问题都靠它
        return {
            "agent.name": self.name,
            "run_id": state.run_id,
            "tenant.id": state.metadata.get("tenant_id"),
            "user.id": state.metadata.get("user_id"),
        }

    @staticmethod
    def _annotate(span: Span, state: RunState, cost_before: float) -> None:
        span.set(
            **{
                "agent.status": state.status,
                "agent.stop_reason": state.stop_reason,
                "agent.steps": state.step,
                "agent.cost_usd": round(state.cost_usd, 6),  # 整个 run 的累计值
                "agent.segment_cost_usd": round(state.cost_usd - cost_before, 6),  # 本段（run 或某次 resume）新增的成本
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
            tool_log=list(state.tool_log),
        )
