"""AsyncAgent：同一个进程里并发运行成百上千个会话的 Agent 运行时。

与同步 Agent 的语义逐项一致（钩子、上下文策略、检查点、审批暂停/恢复、预算、追踪、脱敏），
在此之上做了同步版做不到的事：

1. 并发：等待模型/工具时让出事件循环，一个进程同时推进大量会话（同一个 AsyncAgent 实例可被并发复用，
   每次运行的全部数据都在 RunState 里，实例本身不保存任何"本次运行"的东西）；
2. 并行工具：同一轮里的多个**只读**工具并发执行；只要有一个写/高危工具，就按顺序执行，保证副作用有序；
3. 真正的取消：调用方取消（例如 HTTP 客户端断开）→ 正在进行的模型调用和 async 工具立刻停止，
   检查点落盘为 status="cancelled"，未执行的工具调用补上"未执行"结果，CancelledError 继续向外传播；
4. 截止时间：run_timeout 到期即停止（stop_reason="timeout"）；
5. 舱壁：按租户限制同时在跑的运行数（KeyedLimiter），拿不到槽位时停止（stop_reason="rate_limited"）；
6. 流式：stream() 逐步产出事件（文本片段、工具开始/结束、需要审批、完成），消费方停止读取即取消运行。

钩子、检查点、幂等存储既可以是同步实现，也可以是 async 实现（async def 的方法会被 await）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterable, Union

from ..agent import DEFAULT_SYSTEM_PROMPT, Agent, RunResult
from ..guardrails import redact_pii
from ..hooks import Hook, PauseRun, StopRun
from ..llm import LLMError
from ..pricing import estimate_cost
from ..state import InMemoryCheckpointer, RunState
from ..tools import IdempotencyStore, Tool, ToolContext, ToolRegistry, ToolResult
from ..tracing import Span, Tracer
from ..types import LLMResponse, Message, ToolCall, calls_in, system, tool_message, user
from .limits import KeyedLimiter, LimitExceeded
from .llm import StreamDone, TextDelta
from .tools import AsyncToolExecutor, maybe_await


# ------------------------------------------------------------------------------------ 流式事件


@dataclass
class RunStarted:
    run_id: str


@dataclass
class ToolStarted:
    call: ToolCall


@dataclass
class ToolFinished:
    call: ToolCall
    result: ToolResult


@dataclass
class ApprovalRequired:
    call: ToolCall
    message: str


@dataclass
class RunFinished:
    result: RunResult


AgentEvent = Union[RunStarted, TextDelta, ToolStarted, ToolFinished, ApprovalRequired, RunFinished]

# 当前运行的事件出口。用 ContextVar 而不是实例属性：同一个 AsyncAgent 被很多会话并发使用，
# 每个 asyncio Task 有自己的上下文副本，事件不会串到别人的流里。
_emitter: ContextVar[Callable[[AgentEvent], None] | None] = ContextVar("agentkit_aio_emitter", default=None)


def _emit(event: AgentEvent) -> None:
    fn = _emitter.get()
    if fn is not None:
        fn(event)


async def _call_hook(hook: Hook, method: str, *args):
    return await maybe_await(getattr(hook, method)(*args))


# ------------------------------------------------------------------------------------ AsyncAgent


class AsyncAgent:
    def __init__(
        self,
        llm,
        tools: Iterable[Tool] | ToolRegistry = (),
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        name: str = "agent",
        max_steps: int = 10,
        hooks: Iterable[Hook] = (),
        context_strategy=None,
        checkpointer=None,
        tracer: Tracer | None = None,
        idempotency_store: IdempotencyStore | None = None,
        parallel_tools: bool = True,
        max_parallel_tools: int = 8,
        run_timeout: float | None = None,
        limiter: KeyedLimiter | None = None,
        limiter_key: Callable[[RunState], str | None] = lambda state: state.metadata.get("tenant_id"),
        limiter_timeout: float | None = None,
        max_threads: int = 32,
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
        self.parallel_tools = parallel_tools
        self.max_parallel_tools = max_parallel_tools
        self.run_timeout = run_timeout
        self.limiter = limiter
        self.limiter_key = limiter_key
        self.limiter_timeout = limiter_timeout
        self.executor = AsyncToolExecutor(self.registry, max_threads=max_threads)

    # ------------------------------------------------------------------ 公共 API

    async def run(
        self,
        user_input: str,
        *,
        history: list[Message] | None = None,
        metadata: dict | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        state = RunState(metadata=dict(metadata or {}))
        if run_id:
            state.run_id = run_id
        state.messages = self._initial_messages(history)

        async def prepare() -> None:
            text = user_input
            for h in self.hooks:
                new = await _call_hook(h, "on_run_start", state, text)
                if new is not None:
                    text = new
            state.messages.append(user(text))
            await self._save(state)

        _emit(RunStarted(state.run_id))
        with self.tracer.span("agent.run", **self._root_attrs(state)) as span:
            pending = await self._drive(state, prepare, span, cost_before=0.0)
        return Agent._result(state, pending, span)

    async def resume(self, run_id: str, approvals: dict[str, bool] | None = None) -> RunResult:
        state = await maybe_await(self.checkpointer.load(run_id))
        if state is None:
            raise KeyError(f"找不到 run_id={run_id} 的检查点")
        if state.status == "completed":
            return Agent._result(state, None, None)
        state.approvals.update(approvals or {})
        state.status, state.stop_reason, state.pending = "running", None, None
        cost_before = state.cost_usd
        _emit(RunStarted(state.run_id))
        with self.tracer.span("agent.resume", **self._root_attrs(state)) as span:
            pending = await self._drive(state, None, span, cost_before=cost_before)
        return Agent._result(state, pending, span)

    async def approve(self, run_id: str, approved: bool = True, *, by: str | None = None, comment: str = "") -> RunResult:
        state = await maybe_await(self.checkpointer.load(run_id))
        if state is None or not state.pending:
            raise ValueError(f"run {run_id} 没有等待审批的操作")
        call_id = state.pending["id"]
        state.approval_log.append(
            {"call_id": call_id, "tool": state.pending["name"], "approved": approved, "by": by, "comment": comment, "at": time.time()}
        )
        await self._save(state)
        return await self.resume(run_id, {call_id: approved})

    def stream(self, user_input: str, **kwargs) -> AsyncIterator[AgentEvent]:
        """流式运行：逐步产出事件，最后一个事件是 RunFinished。

        推荐用 contextlib.aclosing 包起来（或交给 Web 框架的流式响应）：
        消费方一旦停止读取（例如 HTTP 客户端断开），运行会被取消，检查点记为 cancelled。
        """
        return self._stream(lambda: self.run(user_input, **kwargs))

    def stream_resume(self, run_id: str, approvals: dict[str, bool] | None = None) -> AsyncIterator[AgentEvent]:
        return self._stream(lambda: self.resume(run_id, approvals))

    async def aclose(self) -> None:
        self.executor.close()
        close = getattr(self.llm, "aclose", None)
        if close is not None:
            await close()

    # ------------------------------------------------------------------ 主循环

    async def _drive(self, state: RunState, prepare, span: Span, cost_before: float) -> ToolCall | None:
        pending: ToolCall | None = None
        state.segment_started_at = time.time()
        slot = (
            self.limiter.slot(self.limiter_key(state), timeout=self.limiter_timeout)
            if self.limiter is not None
            else contextlib.nullcontext()
        )
        try:
            async with slot:
                body = self._prepare_and_loop(state, prepare)
                if self.run_timeout is not None:
                    await asyncio.wait_for(body, self.run_timeout)
                else:
                    await body
        except StopRun as e:
            state.status, state.stop_reason, state.output = "stopped", e.reason, e.message
            self._close_dangling_calls(state, f"未执行：运行已中止（{e.reason}）")
        except PauseRun as e:
            pending = e.call
            state.status, state.stop_reason, state.output = "paused", "needs_approval", e.message
            state.pending = {"id": e.call.id, "name": e.call.name, "arguments": e.call.arguments}
            _emit(ApprovalRequired(e.call, e.message))
        except LLMError as e:
            state.status, state.stop_reason = "failed", f"llm_error: {e}"
            state.output = "抱歉，服务暂时不可用，请稍后再试。"
        except asyncio.TimeoutError:
            state.status, state.stop_reason = "stopped", "timeout"
            state.output = f"（运行超过时限 {self.run_timeout}s，已停止。）"
            self._close_dangling_calls(state, "未执行：运行超时")
        except LimitExceeded as e:
            state.status, state.stop_reason, state.output = "stopped", "rate_limited", f"当前并发已满，请稍后再试（{e}）"
        except asyncio.CancelledError:
            # 取消不是失败：记录下来、收好尾，然后**必须**继续向外抛，否则调用方的取消就失效了
            state.status, state.stop_reason, state.output = "cancelled", "cancelled", "（运行已取消）"
            self._close_dangling_calls(state, "未执行：运行已取消")
            raise
        finally:
            state.active_seconds += time.time() - state.segment_started_at
            for h in self.hooks:
                await _call_hook(h, "on_run_end", state)
            await self._save(state)
            Agent._annotate(span, state, cost_before)
        return pending

    async def _prepare_and_loop(self, state: RunState, prepare) -> None:
        if prepare is not None:
            await prepare()
        await self._run_pending_tools(state)
        while state.step < self.max_steps:
            state.step += 1
            response = await self._call_llm(state)
            state.messages.append(response.to_message())
            await self._save(state)
            if not response.tool_calls:
                output = response.content or ""
                for h in self.hooks:
                    new = await _call_hook(h, "on_final", state, output)
                    if new is not None:
                        output = new
                state.messages[-1]["content"] = output
                reason = "output_truncated" if response.finish_reason == "length" else "final_answer"
                state.status, state.stop_reason, state.output = "completed", reason, output
                return
            await self._run_pending_tools(state)
        state.status, state.stop_reason = "max_steps", "max_steps"
        state.output = f"（已达到最大步数 {self.max_steps}，任务未完成。）"

    async def _call_llm(self, state: RunState) -> LLMResponse:
        if self.context_strategy is not None:
            apply = getattr(self.context_strategy, "aapply", None)
            if apply is not None:
                state.messages = await apply(state.messages)
            else:
                # 同步策略（比如调用同步模型做摘要的 SummarizingCompactor）放到线程里，避免卡住事件循环
                state.messages = await asyncio.to_thread(self.context_strategy.apply, state.messages)
        for h in self.hooks:
            await _call_hook(h, "before_llm", state, state.messages)

        visible = self.registry.names()
        for h in self.hooks:
            visible = await _call_hook(h, "visible_tools", state, visible)
        tools = self.registry.schemas(visible) or None

        streaming = _emitter.get() is not None and hasattr(self.llm, "stream")
        with self.tracer.span(
            "llm.chat",
            **{"gen_ai.request.model": getattr(self.llm, "model", "?"), "step": state.step, "messages": len(state.messages), "stream": streaming},
        ) as span:
            if streaming:
                response = None
                async for event in self.llm.stream(state.messages, tools=tools):
                    if isinstance(event, TextDelta):
                        _emit(event)
                    elif isinstance(event, StreamDone):
                        response = event.response
                if response is None:
                    raise LLMError("流式响应没有收到结束事件", retryable=True)
            else:
                response = await self.llm.chat(state.messages, tools=tools)
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
            await _call_hook(h, "after_llm", state, response)
        return response

    async def _run_pending_tools(self, state: RunState) -> None:
        calls = Agent._unanswered_calls(state)
        if not calls:
            return
        tools = [self.registry.get(c.name) for c in calls]
        parallel = self.parallel_tools and len(calls) > 1 and all(t is not None and t.risk == "read" for t in tools)
        if not parallel:
            for call in calls:  # 有写操作：按模型给出的顺序逐个执行，每个执行完都落盘
                result = await self._execute_tool(state, call)
                state.messages.append(tool_message(call.id, result.content))
                await self._save(state)
            return

        sem = asyncio.Semaphore(self.max_parallel_tools)

        async def one(call: ToolCall) -> ToolResult:
            async with sem:
                return await self._execute_tool(state, call)

        results = await asyncio.gather(*(one(c) for c in calls), return_exceptions=True)
        # 按原始顺序写回结果；遇到控制流异常（暂停、中止）时，先把已经完成的结果落盘再抛出
        first_exc: BaseException | None = None
        for call, r in zip(calls, results):
            if isinstance(r, BaseException):
                first_exc = first_exc or r
                continue
            state.messages.append(tool_message(call.id, r.content))
        await self._save(state)
        if first_exc is not None:
            raise first_exc

    async def _execute_tool(self, state: RunState, call: ToolCall) -> ToolResult:
        t = self.registry.get(call.name)
        if all(entry["id"] != call.id for entry in state.tool_log):
            state.tool_log.append({"id": call.id, "name": call.name})
        with self.tracer.span(
            f"tool.{call.name}",
            **{"tool.name": call.name, "tool.arguments": redact_pii(call.arguments)[:500], "tool.risk": t.risk if t else None},
        ) as span:
            denial = None
            for h in self.hooks:
                denial = await _call_hook(h, "before_tool", state, call, t)
                if denial:
                    break
            _emit(ToolStarted(call))
            if denial:
                result = ToolResult(False, denial, "denied")
            else:
                ctx = ToolContext(
                    run_id=state.run_id,
                    call_id=call.id,
                    tenant_id=state.metadata.get("tenant_id"),
                    user_id=state.metadata.get("user_id"),
                    roles=tuple(state.metadata.get("roles", ())),
                    extra={k: v for k, v in state.metadata.items() if k not in ("tenant_id", "user_id", "roles")},
                )
                result = await self.executor.execute(call, ctx)
                state.tool_calls_count += 1
            for h in self.hooks:
                new = await _call_hook(h, "after_tool", state, call, result)
                if new is not None:
                    result = new
            span.set(**{"tool.ok": result.ok, "tool.error_type": result.error_type, "tool.result_preview": redact_pii(result.content)[:200]})
            if result.detail:
                span.set(**{"tool.error_detail": redact_pii(result.detail)[:500]})
        _emit(ToolFinished(call, result))
        return result

    # ------------------------------------------------------------------ 流式

    async def _stream(self, start: Callable[[], Awaitable[RunResult]]) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue = asyncio.Queue()
        token = _emitter.set(queue.put_nowait)
        try:
            task = asyncio.ensure_future(start())  # 新 Task 复制当前上下文：它看到的是这个流自己的事件出口
        finally:
            _emitter.reset(token)
        getter: asyncio.Future | None = None
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                while not queue.empty():
                    yield queue.get_nowait()
                yield RunFinished(task.result())  # 运行本身失败时，这里会把异常抛给消费方
                return
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
            if not task.done():
                task.cancel()  # 消费方不再读取（断开连接、提前退出）→ 取消运行，别再花钱
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # ------------------------------------------------------------------ 辅助

    def _initial_messages(self, history: list[Message] | None) -> list[Message]:
        msgs: list[Message] = [system(self.system_prompt)] if self.system_prompt else []
        msgs += [dict(m) for m in (history or []) if m.get("role") != "system"]
        return msgs

    def _root_attrs(self, state: RunState) -> dict:
        return {
            "agent.name": self.name,
            "run_id": state.run_id,
            "tenant.id": state.metadata.get("tenant_id"),
            "user.id": state.metadata.get("user_id"),
            "agent.runtime": "asyncio",
        }

    async def _save(self, state: RunState) -> None:
        await maybe_await(self.checkpointer.save(state))  # 同步或异步的检查点都支持

    @staticmethod
    def _close_dangling_calls(state: RunState, note: str) -> None:
        for call in Agent._unanswered_calls(state):
            state.messages.append(tool_message(call.id, note))
