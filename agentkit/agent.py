"""Agent 主循环 —— 整个框架的心脏。

Agent 的本质只有一句话：**让模型在循环里调用工具，直到它给出最终答案**。

    messages = [system, user]
    loop:
        response = await LLM(messages, tools)
        if 没有工具调用: return response.content          # 模型认为任务完成
        for call in response.tool_calls:
            result = await 执行工具(call)
            messages.append(工具结果)                        # 结果喂回给模型，进入下一轮

第 02 课你会亲手写出这 20 行。这个文件是它的"企业版"，多了：
- 步数上限（防止死循环烧钱）
- 钩子（安全 / 权限 / 预算 / 审计 / 脱敏 可插拔）
- 上下文策略（历史太长时截断或摘要）
- 检查点（每一步都存盘，崩溃可恢复、可暂停等人工审批）
- 链路追踪（每次模型调用、工具调用都有 Span）

它是 async 的，所以同一个 Agent 实例可以在一个进程里同时推进成百上千个会话：
1. 并发：等待模型/工具时让出事件循环（实例本身不保存任何"本次运行"的东西，全部数据都在 RunState 里）；
2. 并行工具：同一轮里的多个**只读**工具并发执行；只要有一个写/高危工具，就按顺序执行，保证副作用有序；
3. 真正的取消：调用方取消（例如 HTTP 客户端断开）→ 正在进行的模型调用和 async 工具立刻停止，
   检查点落盘为 status="cancelled"，CancelledError 继续向外传播；
4. 截止时间：run_timeout 到期即停止（stop_reason="timeout"）；
5. 舱壁：按租户限制同时在跑的运行数（KeyedLimiter），拿不到槽位时停止（stop_reason="rate_limited"）；
6. 流式：stream() 逐步产出事件（文本片段、工具开始/结束、需要审批、完成），消费方停止读取即取消运行。

钩子、检查点、幂等存储既可以是同步实现，也可以是 async 实现（async def 的方法会被 await）。
一个进程能跑多少会话由它决定；多个 worker 进程怎么分工、怎么接手崩溃的运行，见 agentkit.distributed（第 13 课）。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Iterable, Protocol, Union

from .guardrails import redact_pii
from .hooks import Hook, PauseRun, StopRun
from .limits import KeyedLimiter, KeyedLocks, LimitExceeded
from .llm import LLM, LLMError, StreamDone, TextDelta
from .pricing import estimate_cost
from .state import Checkpointer, InMemoryCheckpointer, RunState
from .timeouts import wait_for
from .tools import IdempotencyStore, Tool, ToolContext, ToolExecutor, ToolRegistry, ToolResult, maybe_await
from .tracing import Span, Tracer
from .types import LLMResponse, Message, ToolCall, Usage, calls_in, system, tool_message, user

logger = logging.getLogger("agentkit")

DEFAULT_SYSTEM_PROMPT = "你是一个严谨、有帮助的助手。需要外部信息或执行操作时使用工具；信息不足时直接说明，不要编造。"


class ContextStrategy(Protocol):
    """上下文策略：每次调用模型前，决定保留哪些历史消息（第 04 课）。
    apply 可以是普通方法（纯计算，如滑动窗口），也可以是 async 方法（要调用模型做摘要）。"""

    def apply(self, messages: list[Message]) -> list[Message] | Awaitable[list[Message]]: ...


@dataclass
class RunResult:
    output: str | None
    status: str  # completed / paused / max_steps / stopped / failed / cancelled
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

# 当前运行的事件出口。用 ContextVar 而不是实例属性：同一个 Agent 被很多会话并发使用，
# 每个 asyncio Task 有自己的上下文副本，事件不会串到别人的流里。
_emitter: ContextVar[Callable[[AgentEvent], None] | None] = ContextVar("agentkit_emitter", default=None)

# 本次运行专用的检查点（例如 worker 按任务的 fence 创建的视图）。同样用 ContextVar：并发的运行互不影响。
_run_checkpointer: ContextVar[object | None] = ContextVar("agentkit_run_checkpointer", default=None)


# 进入本次运行时，当前任务身上已有的"未消化的取消请求"数（Task.cancelling()，3.11+）。
_cancel_baseline: ContextVar[int | None] = ContextVar("agentkit_cancel_baseline", default=None)


def _cancelling() -> int | None:
    task = asyncio.current_task()
    fn = getattr(task, "cancelling", None)  # 3.10 没有这个 API：检查自动关闭
    return fn() if fn is not None else None


def _raise_if_cancel_swallowed() -> None:
    """在步骤边界补抛被下游吞掉的取消。

    agentkit 自己用取消安全的 wait_for（timeouts.py），但管不到依赖库：3.12 之前，redis-py、
    psycopg_pool 等在内部用 asyncio.wait_for，同样会在"结果和取消同时到达"时吞掉取消（CPython gh-86296；
    第 31 课压测中实测：限流 Hook 调 Redis 时约 1/4 的这类取消被吞，断开的运行照样跑完、照样建单）。
    被吞掉的取消在 Task.cancelling() 里还留着计数：按 asyncio 的约定，正规地压制取消必须调用 uncancel()。
    所以只要计数比进入运行时大，就说明有人吞了取消，在调用模型、执行工具之前补抛。
    和 asyncio.timeout() 一样以进入时的计数为基线，不会误伤调用方自己的状态；
    run_timeout 到期时的取消被吞了也会在这里补抛，再由 timeout 转成 TimeoutError。"""
    baseline = _cancel_baseline.get()
    now = _cancelling()
    if baseline is not None and now is not None and now > baseline:
        # extra 里的 agentkit_event 是给运维用的稳定字段（例如计成指标），不要按日志措辞匹配
        logger.warning("取消请求被下游吞掉（Task.cancelling()=%d > 基线 %d），在步骤边界补抛", now, baseline,
                       extra={"agentkit_event": "swallowed_cancellation"})
        raise asyncio.CancelledError("取消请求被依赖库吞掉，在步骤边界补抛")


def _emit(event: AgentEvent) -> None:
    fn = _emitter.get()
    if fn is not None:
        fn(event)


async def _call_hook(hook: Hook, method: str, *args):
    return await maybe_await(getattr(hook, method)(*args))


def _snapshot(state: RunState) -> RunState:
    """浅快照：复制各个容器（消息列表、元数据……），不深拷贝每条消息，开销很小。"""
    return dataclasses.replace(
        state,
        messages=[dict(m) for m in state.messages],
        metadata=dict(state.metadata),
        approvals=dict(state.approvals),
        tool_log=list(state.tool_log),
        approval_log=list(state.approval_log),
        pending=dict(state.pending) if state.pending else None,
        usage=dataclasses.replace(state.usage),
    )


# ------------------------------------------------------------------------------------ Agent


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
        parallel_tools: bool = True,
        max_parallel_tools: int = 8,
        run_timeout: float | None = None,
        limiter: KeyedLimiter | None = None,
        limiter_key: Callable[[RunState], str | None] = lambda state: state.metadata.get("tenant_id"),
        limiter_timeout: float | None = None,
        max_threads: int | None = None,
        executor: ToolExecutor | None = None,
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
        # 可以注入共享的执行器：多个 Agent 共用一个有上限的线程池。
        # max_threads=None：同步工具用事件循环的默认线程池（全进程共享、有上限）
        self.executor = executor or ToolExecutor(self.registry, max_threads=max_threads)
        self._owns_executor = executor is None
        # 同一个 run 的检查点读写必须排队：被取消的运行会在后台（shield）把最后一次保存写完，
        # 紧接着的 resume 必须等它写完再读，否则会读到旧状态、或和它撞同一个版本号
        self._io_locks = KeyedLocks()
        # 同一个 run 的恢复/审批必须串行：并发两次 approve 不能让高危工具执行两次
        self._run_locks = KeyedLocks()

    # ------------------------------------------------------------------ 公共 API

    async def run(
        self,
        user_input: str,
        *,
        history: list[Message] | None = None,
        metadata: dict | None = None,
        run_id: str | None = None,
        checkpointer=None,
    ) -> RunResult:
        """开始一次新的运行。metadata 放可信身份信息：tenant_id / user_id / roles。

        checkpointer：只用于本次运行的检查点（例如 worker 按任务的 fence 创建的视图），不传则用构造时的。"""
        token = _run_checkpointer.set(checkpointer) if checkpointer is not None else None
        try:
            return await self._run(user_input, history=history, metadata=metadata, run_id=run_id)
        finally:
            if token is not None:
                _run_checkpointer.reset(token)

    async def _run(self, user_input: str, *, history, metadata, run_id) -> RunResult:
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
        return self._result(state, pending, span)

    async def resume(self, run_id: str, approvals: dict[str, bool] | None = None, *, checkpointer=None) -> RunResult:
        """从检查点恢复运行：崩溃恢复，或人工审批之后继续。

        approvals: {tool_call_id: True/False}，True 批准执行，False 拒绝。
        同一个 run 的恢复/审批在本进程内串行；跨进程的互斥靠检查点的 fence（agentkit.distributed、第 26 课）。
        """
        token = _run_checkpointer.set(checkpointer) if checkpointer is not None else None
        try:
            async with self._run_locks.hold(run_id):
                return await self._resume(run_id, approvals)
        finally:
            if token is not None:
                _run_checkpointer.reset(token)

    async def _resume(self, run_id: str, approvals: dict[str, bool] | None) -> RunResult:
        state = await self._load(run_id)
        if state is None:
            raise KeyError(f"找不到 run_id={run_id} 的检查点")
        if state.status == "completed":
            return self._result(state, None, None)
        state.approvals.update(approvals or {})
        state.status, state.stop_reason, state.pending = "running", None, None
        cost_before = state.cost_usd
        _emit(RunStarted(state.run_id))
        with self.tracer.span("agent.resume", **self._root_attrs(state)) as span:
            pending = await self._drive(state, None, span, cost_before=cost_before)
        return self._result(state, pending, span)

    async def approve(
        self, run_id: str, approved: bool = True, *, by: str | None = None, comment: str = "", checkpointer=None
    ) -> RunResult:
        """对当前等待审批的那次工具调用给出决定并继续运行。

        by / comment 会写入 state.approval_log（审计要求记录"谁、何时、为什么"批准）。
        """
        token = _run_checkpointer.set(checkpointer) if checkpointer is not None else None
        try:
            async with self._run_locks.hold(run_id):
                return await self._approve(run_id, approved, by=by, comment=comment)
        finally:
            if token is not None:
                _run_checkpointer.reset(token)

    async def _approve(self, run_id: str, approved: bool, *, by: str | None, comment: str) -> RunResult:
        # 在锁内重新读取：如果另一个审批已经处理过（pending 已清空），这里会明确报错，而不是再执行一次高危工具
        state = await self._load(run_id)
        if state is None or not state.pending:
            raise ValueError(f"run {run_id} 没有等待审批的操作")
        call_id = state.pending["id"]
        state.approval_log.append(
            {"call_id": call_id, "tool": state.pending["name"], "approved": approved, "by": by, "comment": comment, "at": time.time()}
        )
        await self._save(state)
        return await self._resume(run_id, {call_id: approved})

    def stream(self, user_input: str, **kwargs) -> AsyncIterator[AgentEvent]:
        """流式运行：逐步产出事件，最后一个事件是 RunFinished。

        推荐用 contextlib.aclosing 包起来（或交给 Web 框架的流式响应）：
        消费方一旦停止读取（例如 HTTP 客户端断开），运行会被取消，检查点记为 cancelled。
        """
        return self._stream(lambda: self.run(user_input, **kwargs))

    def stream_resume(self, run_id: str, approvals: dict[str, bool] | None = None) -> AsyncIterator[AgentEvent]:
        return self._stream(lambda: self.resume(run_id, approvals))

    async def aclose(self) -> None:
        """释放自己创建的线程池和模型客户端的连接（服务关闭时调用；脚本里可以不管）。"""
        if self._owns_executor:
            self.executor.close()
        close = getattr(self.llm, "aclose", None)
        if close is not None:
            await close()

    # ------------------------------------------------------------------ 主循环

    async def _drive(self, state: RunState, prepare, span: Span, cost_before: float) -> ToolCall | None:
        """执行主循环并把各种"非正常结束"统一收敛为状态。返回等待审批的调用（如果有）。"""
        pending: ToolCall | None = None
        state.segment_started_at = time.time()
        # 手动进入/退出舱壁槽位：要一直持有到**收尾完成**（on_run_end + 最后一次保存）才释放，
        # 否则下一个运行会在上一个运行真正结束前拿到槽位，同一租户的并发就会短暂超过上限
        slot = self.limiter.slot(self.limiter_key(state), timeout=self.limiter_timeout) if self.limiter is not None else None
        entered = False
        try:
            if slot is not None:
                await slot.__aenter__()
                entered = True
            body = self._prepare_and_loop(state, prepare)
            if self.run_timeout is not None:
                await wait_for(body, self.run_timeout)  # 取消安全版，见 timeouts.py
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
            self._close_dangling_calls(state, "未执行：运行超时", keep_side_effects=True)
        except LimitExceeded as e:
            state.status, state.stop_reason, state.output = "stopped", "rate_limited", f"当前并发已满，请稍后再试（{e}）"
        except asyncio.CancelledError:
            # 取消不是失败：记录下来、收好尾，然后**必须**继续向外抛，否则调用方的取消就失效了
            state.status, state.stop_reason, state.output = "cancelled", "cancelled", "（运行已取消）"
            self._close_dangling_calls(state, "未执行：运行已取消", keep_side_effects=True)
            raise
        finally:
            state.active_seconds += time.time() - state.segment_started_at
            # 收尾（on_run_end 钩子 + 最后一次保存）放进一个独立任务并用 shield 保护：
            # Web 框架（例如 Starlette/AnyIO）断开连接时可能**反复**取消，第二次取消如果打断了这次保存，
            # 检查点就会永远停在 running。shield 保证收尾任务跑完；外层照样收到取消并继续向外传播。
            finish = asyncio.ensure_future(self._finish(state, slot if entered else None))
            try:
                await asyncio.shield(finish)
            finally:
                self._annotate(span, state, cost_before)
        return pending

    async def _finish(self, state: RunState, slot=None) -> None:
        try:
            for h in self.hooks:
                await _call_hook(h, "on_run_end", state)
            await self._save(state)
        finally:
            # 舱壁名额在收尾真正完成后才释放：即使外层被再次取消、不再等待这个任务，也是如此
            if slot is not None:
                await slot.__aexit__(None, None, None)

    async def _prepare_and_loop(self, state: RunState, prepare) -> None:
        # 进入时记下基线。3.11+ 上本体和 _drive 在同一个任务里，进入 asyncio.timeout 不改变 cancelling() 计数；
        # 3.10 没有 cancelling()，检查自动关闭
        token = _cancel_baseline.set(_cancelling())
        try:
            await self._loop_body(state, prepare)
        finally:
            _cancel_baseline.reset(token)

    async def _loop_body(self, state: RunState, prepare) -> None:
        if prepare is not None:
            await prepare()
        # 断点续跑：如果上次停在"模型已发起工具调用、但工具还没执行完"，先把它们补完
        await self._run_pending_tools(state)
        while state.step < self.max_steps:
            _raise_if_cancel_swallowed()
            response = await self._call_llm(state)  # 步数在里面、真正调用模型之前才加一
            state.messages.append(response.to_message())
            await self._save(state)
            if not response.tool_calls:  # 没有工具调用 = 模型给出了最终答案
                output = response.content or ""
                for h in self.hooks:
                    new = await _call_hook(h, "on_final", state, output)
                    if new is not None:
                        output = new
                state.messages[-1]["content"] = output  # 历史里也存处理后的版本（如已脱敏）
                # finish_reason == "length"：输出被 max_tokens 截断了。仍然返回，但打上标记，
                # 让调用方/评估/告警能区分"完整答案"和"说到一半的答案"。
                reason = "output_truncated" if response.finish_reason == "length" else "final_answer"
                state.status, state.stop_reason, state.output = "completed", reason, output
                return
            await self._run_pending_tools(state)
        state.status, state.stop_reason = "max_steps", "max_steps"
        state.output = f"（已达到最大步数 {self.max_steps}，任务未完成。）"

    async def _call_llm(self, state: RunState) -> LLMResponse:
        if self.context_strategy is not None:
            state.messages = await maybe_await(self.context_strategy.apply(state.messages))
        for h in self.hooks:
            await _call_hook(h, "before_llm", state, state.messages)

        visible = self.registry.names()
        for h in self.hooks:
            visible = await _call_hook(h, "visible_tools", state, visible)
        tools = self.registry.schemas(visible) or None

        _raise_if_cancel_swallowed()  # Hook 里的 Redis / 数据库调用可能吞掉了取消：花钱之前再确认一次
        # 步数 = 真正发出的模型调用次数，所以在 before_llm 之后才计数：Hook 在调用前叫停（限流推迟、预算用完）
        # 的那一步没有发生。以前在循环开头就加一，被限流推迟 max_steps 次的运行一次模型都没调就以 max_steps 结束
        state.step += 1
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

    @staticmethod
    def _unanswered_calls(state: RunState) -> list[ToolCall]:
        """最后一条 assistant 消息里、还没有 tool 结果的工具调用。"""
        idx = next((i for i in range(len(state.messages) - 1, -1, -1) if state.messages[i]["role"] == "assistant"), None)
        if idx is None:
            return []
        done = {m.get("tool_call_id") for m in state.messages[idx + 1 :] if m["role"] == "tool"}
        return [c for c in calls_in(state.messages[idx]) if c.id not in done]

    async def _run_pending_tools(self, state: RunState) -> None:
        """执行最后一条 assistant 消息里、还没有结果的工具调用。"""
        calls = self._unanswered_calls(state)
        if not calls:
            return
        tools = [self.registry.get(c.name) for c in calls]
        parallel = self.parallel_tools and len(calls) > 1 and all(t is not None and t.risk == "read" for t in tools)
        if not parallel:
            for call in calls:  # 有写操作：按模型给出的顺序逐个执行，每个执行完都落盘（缩小"执行了但没记录"的窗口）
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
                denial = await _call_hook(h, "before_tool", state, call, t)
                if denial:
                    break
            _raise_if_cancel_swallowed()  # 产生副作用之前再确认一次：取消可能被 before_tool 里的调用吞掉
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
                    # 其余可信属性（部门、用户组、数据区域……）原样透传给工具
                    extra={k: v for k, v in state.metadata.items() if k not in ("tenant_id", "user_id", "roles")},
                )
                # 执行前就计数：并行的只读工具各自在自己的 task 里跑 before_tool，
                # 如果执行完才 +1，它们都会看到"还没超预算"，预算形同虚设
                state.tool_calls_count += 1
                result = await self.executor.execute(call, ctx)
            for h in self.hooks:
                new = await _call_hook(h, "after_tool", state, call, result)
                if new is not None:
                    result = new
            # 结果预览只截取前 200 字符：足够排查问题，又避免把大段数据/敏感信息灌进追踪系统
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
                # 用 wait 而不是 await task：如果这里再次被取消，await task 会把取消**转发**给运行任务，
                # 打断它的收尾；wait 只是等待，不转发取消
                await asyncio.wait({task})
                if not task.cancelled() and task.exception() is not None:
                    # 消费方已经走了，没人会接收这个异常：记录下来，而不是留一句 "exception was never retrieved"
                    logger.warning("流式运行在消费方断开后以异常结束：%r", task.exception())

    # ------------------------------------------------------------------ 辅助

    def _initial_messages(self, history: list[Message] | None) -> list[Message]:
        msgs: list[Message] = [system(self.system_prompt)] if self.system_prompt else []
        msgs += [dict(m) for m in (history or []) if m.get("role") != "system"]
        return msgs

    def _root_attrs(self, state: RunState) -> dict:
        # 租户 / 用户写在根 Span 上：按租户做成本归因、按用户排查问题都靠它
        return {
            "agent.name": self.name,
            "run_id": state.run_id,
            "tenant.id": state.metadata.get("tenant_id"),
            "user.id": state.metadata.get("user_id"),
        }

    def _checkpointer(self):
        return _run_checkpointer.get() or self.checkpointer

    async def _save(self, state: RunState) -> None:
        """保存检查点。同一个 run 的读写按顺序排队。

        异步检查点的**每一次**保存都放进独立任务并 shield：如果取消打断了一次"数据库已提交、
        但客户端还没收到回复"的 UPDATE，带版本号 CAS 的检查点在本地记住的版本号就过期了，
        之后的收尾保存会被当成冲突拒绝，状态永远停在 running（第 30 课 R1，实测约 4%）。
        shield 保证每次保存都完整走完；为了不和之后的修改互相影响，保存的是一份浅快照。
        同步检查点的 save 中间没有 await，不可能被取消打断，直接写，省掉创建任务的开销。
        """
        cp = self._checkpointer()
        if not inspect.iscoroutinefunction(getattr(cp, "save", None)):
            cp.save(state)
            return
        snapshot = _snapshot(state)
        await asyncio.shield(asyncio.ensure_future(self._save_now(cp, snapshot)))

    async def _save_now(self, cp, snapshot: RunState) -> None:
        async with self._io_locks.hold(snapshot.run_id):
            await cp.save(snapshot)

    async def _load(self, run_id: str) -> RunState | None:
        async with self._io_locks.hold(run_id):
            return await maybe_await(self._checkpointer().load(run_id))

    def _close_dangling_calls(self, state: RunState, note: str, *, keep_side_effects: bool = False) -> None:
        """运行中止时，给没执行的工具调用补一条"未执行"结果。

        OpenAI 协议要求每个 tool_call 都有对应的 tool 消息，否则下一轮带着这段历史调用模型会直接 400。
        （暂停等审批时不补：那些调用在 resume 时还要真正执行。）

        keep_side_effects=True（取消、超时）时，写/高危工具的调用**保持未回答**：
        它可能已经在下游执行了一半。如果补上"未执行"，resume 后模型会发起一个 call_id 不同的新调用，
        幂等键随之改变，副作用就会发生两次。保持未回答，resume 时会用**同一个 call_id** 重放，幂等键不变，
        由幂等存储 / 下游的 Idempotency-Key 去重（第 08、13、26 课）。
        """
        for call in self._unanswered_calls(state):
            t = self.registry.get(call.name)
            if keep_side_effects and t is not None and t.risk in ("write", "dangerous"):
                continue
            state.messages.append(tool_message(call.id, note))

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
