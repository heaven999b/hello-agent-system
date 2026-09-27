"""agentkit.contrib.temporal —— 用 Temporal 持久化工作流运行 agentkit Agent（第 27 课）。

agentkit 核心用"检查点 + PauseRun + resume"实现崩溃恢复和人工审批（第 02、08 课），
第 26 课把检查点换成了 Postgres。但"谁来发现进程死了、谁来触发恢复、谁来给审批计时、
两个进程同时 resume 怎么办"仍然要你自己写。本模块换一条路：把 Agent 主循环写成 Temporal
Workflow，让持久化执行引擎替你记住"执行到哪一步"。

    Agent._loop            →  AgentWorkflow.run（确定性代码，只负责编排）
    llm.chat               →  llm_step Activity（有 IO，可重试）
    registry.execute       →  execute_tool Activity（有副作用，按风险设置重试）
    checkpointer.save      →  事件历史（Temporal 自动记录每个 Activity 的输入和结果）
    PauseRun + resume      →  workflow.wait_condition + signal / update（带审批超时）
    ResilientLLM 的重试    →  RetryPolicy（重试只放在这一层，见下文）
    run_id:call_id 幂等键  →  workflow_id:call_id（跨 continue-as-new 保持不变）

公开 API（第 30 课和参考服务直接调用）：

    AgentInput / AgentResult / AgentStatus / ApprovalDecision / ToolSpec   数据类型（dataclass，可被默认数据转换器序列化）
    AgentWorkflow                         workflow：run / signal approve / update decide / query status
    AgentActivities                       activity（async）：describe_tools / llm_step / execute_tool
    make_worker(client, task_queue, llm_factory, tools, ...) -> Worker
    start_agent(client, input, metadata, workflow_id, task_queue, ...) -> WorkflowHandle
    approve(client, workflow_id, call_id, approved, by, comment="", wait=False) -> str | None
    agent_status(client, workflow_id) -> AgentStatus
    retry_policy_for(risk, idempotent) -> dict       工具 Activity 的重试参数（纯函数）
    sandbox_runner() -> SandboxedWorkflowRunner      让 agentkit 能在 workflow 沙箱里导入
    summarize_history(history) / activity_attempts(history) / format_history(lines)   事件历史摘要

哪些 agentkit Hook 可以直接放进 workflow（纯逻辑），哪些必须移到 activity（有 IO / 非确定性）：

    可以（纯逻辑，给定相同输入永远得出相同结果）：
        PermissionPolicy（approver=None 时）  RBAC、deny_tools、按风险审批；它抛的 PauseRun 在这里变成 wait_condition
        InputGuard / OutputGuard              正则匹配与脱敏
        LoopGuard（第 08 课）                 计数存在 state.metadata，会随 continue-as-new 一起携带
        BudgetHook(max_tokens / max_cost_usd / max_tool_calls)
    不可以（放到 activity，或改写成用 workflow.now() / workflow.uuid4()）：
        BudgetHook(max_seconds)   用了 time.time()
        ToolOutputGuard           用了 uuid.uuid4() 生成边界 → 用 make_worker(tool_hooks=[...]) 在 activity 里执行
        AuditLog                  写文件、time.time() → 同上，放进 tool_hooks
        Tracer / SummarizingCompactor / 记忆检索   有 IO（前者请改用 Temporal 的 OpenTelemetry 拦截器，第 28 课）

⚠️ 一个反直觉的事实：agentkit 以 passthrough 方式进入沙箱（见 sandbox_runner），而**沙箱只拦截它
重新导入的模块**。passthrough 模块里的 time.time() / uuid4() 不会被拦截，只会悄悄地让重放结果不一致。
所以"哪些 Hook 能放进 workflow"只能靠代码审查和重放测试（Replayer）保证，沙箱帮不上忙。

投递语义：Activity 是"至少执行一次"的。worker 在"工具已经执行完"和"结果报告给服务端"之间崩溃，
服务端只能等 heartbeat / start_to_close 超时后重试 —— 和第 13 课"租约过期后任务被别人重新领取"是同一个问题，
解法也相同：写操作要么不自动重试（maximum_attempts=1），要么带上稳定的幂等键（workflow_id:call_id）。

异步运行时（activity 全部是 async def）：
- 并发旋钮：worker 的 max_concurrent_activities（同时执行多少个 activity，通常要对齐模型网关的并发配额）
  与 max_concurrent_workflow_tasks（同时推进多少个 workflow 任务，大量 workflow 需要重放时会成为瓶颈），SDK 默认各 100。
  async activity 在等模型时不占线程，一个 worker 就能同时推进很多 workflow（第 27 课 Demo 场景 6：20 个 workflow
  串行 7.9 秒 → 并发 1.15 秒）。
- 阻塞 IO 的后果：async activity 跑在 worker 的事件循环上，一个同步的 requests / time.sleep / 同步数据库驱动
  会卡住这个 worker 上所有 activity、心跳和 workflow 任务的收发，并发退化成 1；心跳发不出去还会被判超时重试。
  同步工具交给 agentkit.aio.AsyncToolExecutor（有上限的线程池）；模型客户端在 worker 启动时创建，不在 activity 里懒加载。
  （Temporal 也支持同步 activity，但要求 Worker 配置 activity_executor，官方推荐 ThreadPoolExecutor。）
- 取消的传递：workflow 被取消（handle.cancel()）或 activity 心跳超时 → 服务端在心跳响应里通知 worker →
  SDK 取消这个 activity 的 asyncio 任务 → `await llm.chat(...)` 抛出 CancelledError，异步 HTTP 客户端中断请求。
  前提是 activity 在发心跳（_heartbeating，每 heartbeat_timeout / 2 一次）、模型客户端是异步的；
  同步客户端放在线程里时，取消只能"不再等它"，请求会在后台跑完并照样计费。

依赖：pip install -e ".[temporal]"
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable, Sequence

from agentkit.contrib import require

require("temporalio", "temporal")

from temporalio import activity, workflow  # noqa: E402
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy  # noqa: E402
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError  # noqa: E402

with workflow.unsafe.imports_passed_through():
    # agentkit 在沙箱里只能以 passthrough 方式导入：agentkit/config.py 在导入时调用了 Path.resolve()，
    # 沙箱重新导入它会直接报 "__call__ on pathlib.Path.resolve restricted"（本课实测）。
    from agentkit.agent import DEFAULT_SYSTEM_PROMPT
    from agentkit.aio import AsyncLLM, AsyncToolExecutor
    from agentkit.hooks import Hook, PauseRun, StopRun
    from agentkit.llm import LLM, LLMError
    from agentkit.permissions import PermissionPolicy
    from agentkit.pricing import estimate_cost
    from agentkit.state import RunState
    from agentkit.tools import IdempotencyStore, Tool, ToolContext, ToolRegistry, ToolResult
    from agentkit.types import LLMResponse, ToolCall, Usage, calls_in, system, tool_message, user

DEFAULT_TASK_QUEUE = "agentkit-agents"

# 工具执行结果里这两类错误"重试可能成功"（内部异常、超时），其余（参数错、不存在、业务错误、被拒绝）重试没用
TRANSIENT_TOOL_ERRORS = ("exception", "timeout")
# 由 execute_tool 主动抛出、并且**永远不该重试**的错误类型（写进 RetryPolicy.non_retryable_error_types）
NON_RETRYABLE_TOOL_ERRORS = ["InvalidArguments", "PermissionDenied"]


# ============================================================================================
# 数据类型：workflow / activity 的输入输出都是 dataclass，默认数据转换器（JSON）即可序列化。
# 官方建议 workflow / signal / activity 都只收一个 dataclass 参数：以后加字段不会破坏旧的执行。
# ============================================================================================


@dataclass
class ToolSpec:
    """workflow 需要知道的工具元数据。由 describe_tools activity 从 worker 的 ToolRegistry 读出，结果进入事件历史。"""

    name: str
    risk: str
    schema: dict
    timeout_s: float = 30.0
    idempotent: bool = False  # read 天然幂等；write/dangerous 只有在配置了幂等存储时才算


@dataclass
class ApprovalDecision:
    """审批决定：signal approve / update decide 的参数。"""

    call_id: str
    approved: bool
    by: str = ""
    comment: str = ""


@dataclass
class AgentInput:
    """AgentWorkflow 的输入。metadata 放**可信**身份（tenant_id / user_id / roles），来自启动 workflow 的服务端，不来自模型。"""

    user_input: str
    metadata: dict = field(default_factory=dict)
    max_steps: int = 10
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history: list = field(default_factory=list)  # 之前的对话（不含 system）
    # 权限（纯逻辑，在 workflow 里判断）
    ask_risks: list = field(default_factory=lambda: ["dangerous"])
    role_tools: dict | None = None  # 角色 → 工具名列表；None 表示不启用 RBAC
    deny_tools: list = field(default_factory=list)
    approval_timeout_s: float = 24 * 3600.0  # 审批超时按拒绝处理（fail closed）
    # 超时与重试
    llm_timeout_s: float = 120.0  # 单次模型调用（start_to_close）
    llm_total_timeout_s: float = 900.0  # 含所有重试（schedule_to_close）
    llm_max_attempts: int = 5
    retry_initial_interval_s: float = 1.0
    heartbeat_timeout_s: float = 10.0  # activity 多久没心跳就判定 worker 已死（也是取消送达的最长延迟）
    # 事件历史过长时 continue-as-new：服务端建议时一定会做；这里可以再设一个更小的阈值（测试 / 演示用）
    continue_as_new_after_events: int | None = None
    # continue-as-new 时携带的状态（内部使用，调用方不用填）
    carry: dict | None = None


@dataclass
class AgentResult:
    output: str | None
    status: str  # completed / max_steps / stopped / failed（被取消时 workflow 以 Canceled 结束，拿不到结果）
    stop_reason: str | None
    steps: int
    usage: Usage
    cost_usd: float
    messages: list
    tools_called: list
    approval_log: list
    runs: int  # 这次运行跨了几个 workflow run（1 = 没有 continue-as-new）
    workflow_id: str


@dataclass
class AgentStatus:
    """status() 查询的返回值：给审批界面、运维看板用。"""

    workflow_id: str
    status: str  # starting / running / waiting_approval / completed / max_steps / stopped / failed / cancelled
    step: int
    max_steps: int
    pending_approvals: list  # [{"call_id","name","arguments","since"}]
    tools_called: list
    usage: Usage
    cost_usd: float
    approval_log: list
    runs: int
    history_length: int  # 当前 run 的事件数（服务端默认 4096 时建议 continue-as-new，51200 时强制失败）
    history_size_bytes: int  # 当前 run 的事件历史字节数（默认 4 MB 建议 continue-as-new，50 MB 强制失败）


@dataclass
class LLMStepInput:
    messages: list
    tools: list | None = None


@dataclass
class ToolStepInput:
    call: ToolCall
    run_id: str  # = workflow_id，与 call.id 组成幂等键
    metadata: dict = field(default_factory=dict)  # 可信身份上下文
    approval: dict | None = None  # 这次调用对应的审批记录（给审计类 tool_hooks 用）


# ============================================================================================
# 重试策略（纯函数，第 27 课练习 (a) 的生产版）
# ============================================================================================


def retry_policy_for(risk: str, idempotent: bool, *, initial_interval_s: float = 1.0) -> dict:
    """工具 Activity 的 RetryPolicy 参数。

        read                       5 次（天然幂等，放心重试）
        write / dangerous + 幂等    3 次（下游用 workflow_id:call_id 去重）
        write / dangerous 不幂等    1 次（不自动重试：宁可让模型/人知道"结果未知"，也不重复扣款）
    """
    if risk not in ("read", "write", "dangerous"):
        raise ValueError(f"未知的风险等级：{risk!r}")
    attempts = 5 if risk == "read" else (3 if idempotent else 1)
    return {
        "maximum_attempts": attempts,
        "initial_interval": initial_interval_s,
        "backoff_coefficient": 2.0,
        "non_retryable_error_types": list(NON_RETRYABLE_TOOL_ERRORS),
    }


def _retry_policy(params: dict) -> RetryPolicy:
    return RetryPolicy(
        maximum_attempts=params["maximum_attempts"],
        initial_interval=timedelta(seconds=params["initial_interval"]),
        backoff_coefficient=params["backoff_coefficient"],
        non_retryable_error_types=params["non_retryable_error_types"],
    )


# ============================================================================================
# Activities：所有 IO 都在这里。全部是 async def，运行在 worker 的事件循环上，不受确定性约束。
# ============================================================================================


@contextlib.asynccontextmanager
async def _heartbeating():
    """在 activity 运行期间按 heartbeat_timeout / 2 的间隔发心跳（与 temporalio.contrib.openai_agents 的做法相同）。

    心跳有两个作用：
    1. 让服务端更快发现 worker 死了：等 heartbeat_timeout（默认 10 秒），而不是等满 start_to_close（可能几分钟）；
    2. **取消只能通过心跳送达**：workflow 被取消、或心跳超时后，服务端在心跳响应里告诉 worker，
       SDK 随即取消这个 activity 的 asyncio 任务 —— 正在 await 的 LLM 调用会收到 CancelledError。
    """
    hb = activity.info().heartbeat_timeout
    task = None
    if hb:

        async def beat(every: float) -> None:
            while True:
                await asyncio.sleep(every)
                activity.heartbeat()

        task = asyncio.create_task(beat(hb.total_seconds() / 2))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class AgentActivities:
    """LLM 与工具 Activity，全部是 async def。

    - llm_factory 在 make_worker 启动 worker 时调用一次，应返回 agentkit.aio.AsyncLLM（如 AsyncOpenAICompatLLM）。
      也兼容同步 LLM（放进线程池执行），但那样取消只能"停止等待"，HTTP 请求会在后台跑完、照样计费。
    - 工具经 agentkit.aio.AsyncToolExecutor 执行 —— 和 AsyncAgent（第 30 课）是同一套执行语义：
      async 工具真正可取消；同步工具进有上限的线程池；isolated(tool) 标记的工具在子进程里执行、超时直接 kill。
    - 创建的 LLM 应该**关掉自己的重试**：重试交给 Temporal 的 RetryPolicy。两层都重试会让尝试次数相乘
      （ResilientLLM 3 次 × RetryPolicy 5 次 = 15 次），而且外层完全看不见。
    - ⚠️ async activity 里绝不能有阻塞调用（requests、time.sleep、同步数据库驱动……）：它们跑在 worker 的事件循环上，
      一个阻塞调用会卡住这个 worker 上所有 async activity、心跳和 workflow 任务的收发（Demo 场景 6 实测）。
    """

    def __init__(
        self,
        llm_factory: Callable[[], "AsyncLLM | LLM"],
        tools: Iterable[Tool] | ToolRegistry,
        *,
        tool_hooks: Sequence[Hook] = (),
        idempotency_store: IdempotencyStore | None = None,
        idempotent_tools: Iterable[str] = (),
        sync_tool_threads: int = 16,
    ):
        self._llm_factory = llm_factory
        self._llm = None
        self._lock = threading.Lock()
        self.registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        if idempotency_store is not None:
            self.registry.idempotency_store = idempotency_store
        self.tool_hooks = list(tool_hooks)
        self.idempotent_tools = set(idempotent_tools)
        self.executor = AsyncToolExecutor(self.registry, max_threads=sync_tool_threads)

    def llm(self):
        """创建（一次）并返回 LLM。make_worker 会在启动时就调用它，而不是等到第一个 llm_step。

        本课实测的坑：懒加载时，第一次 llm_step 在 activity 里创建客户端（导入 openai / httpx、建连接池，
        本机约 0.6 秒，机器忙时超过 2 秒），这段同步代码卡住了事件循环、心跳发不出去，
        heartbeat_timeout=2 秒的 activity 被判超时重试 —— 模型调用可能因此多付一次钱。
        """
        with self._lock:
            if self._llm is None:
                self._llm = self._llm_factory()
            return self._llm

    def activities(self) -> list:
        return [self.describe_tools, self.llm_step, self.execute_tool]

    @activity.defn(name="describe_tools")
    async def describe_tools(self) -> list[ToolSpec]:
        """把 worker 上注册的工具告诉 workflow。

        为什么不让 workflow 直接读 registry？registry 是 worker 进程的状态，不同 worker 可能部署了不同版本；
        通过 activity 读出来，结果就进了事件历史，重放时每个 worker 看到的都是同一份。
        """
        has_store = self.registry.idempotency_store is not None
        specs = []
        for name in self.registry.names():
            t = self.registry.get(name)
            idem = t.risk == "read" or name in self.idempotent_tools or has_store
            specs.append(ToolSpec(name=name, risk=t.risk, schema=t.schema(), timeout_s=t.timeout_s, idempotent=idem))
        return specs

    @activity.defn(name="llm_step")
    async def llm_step(self, inp: LLMStepInput) -> LLMResponse:
        """调用一次模型。可重试的错误（429 / 5xx / 超时）交给 RetryPolicy；不可重试的（400 / 401 / 额度用完）标 non_retryable。

        取消：workflow 被取消或心跳超时时，SDK 取消本 activity 的任务，CancelledError 在 `await llm.chat(...)` 处抛出，
        异步 HTTP 客户端（httpx / openai.AsyncOpenAI）随之中断请求。这里不吞掉它，原样抛出 activity 才算"已取消"。
        """
        llm = self._llm if self._llm is not None else await asyncio.to_thread(self.llm)  # 兜底：别在事件循环里建客户端
        async with _heartbeating():
            try:
                if inspect.iscoroutinefunction(llm.chat):
                    return await llm.chat(inp.messages, tools=inp.tools or None)
                return await asyncio.to_thread(llm.chat, inp.messages, inp.tools or None)
            except LLMError as e:
                delay = timedelta(seconds=e.retry_after) if e.retry_after else None  # 服务端的 Retry-After 优先
                raise ApplicationError(
                    str(e),
                    {"status_code": e.status_code},
                    type="LLMError",
                    non_retryable=not e.retryable,
                    next_retry_delay=delay,
                ) from e
            except asyncio.CancelledError:
                details = activity.cancellation_details()
                activity.logger.info("llm_step 被取消：%s", details)
                raise
            # 其他异常（连接被重置、代码 bug……）原样抛出：Temporal 默认按可重试处理，次数受 maximum_attempts 限制

    @activity.defn(name="execute_tool")
    async def execute_tool(self, inp: ToolStepInput) -> ToolResult:
        """经 AsyncToolExecutor 执行一次工具调用（参数校验、超时、幂等、输出截断与 ToolRegistry.execute 一致）。

        工具把所有异常都变成了 ToolResult（"错误即观察"）。这里再做一次分流：
        内部异常 / 超时 → 抛 ApplicationError，让 RetryPolicy 决定是否重试；
        参数错、业务错误 → 原样返回给模型，重试没有意义。
        """
        meta = dict(inp.metadata or {})
        ctx = ToolContext(
            run_id=inp.run_id,
            call_id=inp.call.id,
            tenant_id=meta.get("tenant_id"),
            user_id=meta.get("user_id"),
            roles=tuple(meta.get("roles", ())),
            extra={k: v for k, v in meta.items() if k not in ("tenant_id", "user_id", "roles")},
        )
        async with _heartbeating():
            result = await self.executor.execute(inp.call, ctx)
        if not result.ok and result.error_type in TRANSIENT_TOOL_ERRORS:
            raise ApplicationError(result.content, result.detail, type="ToolTransientError")
        if self.tool_hooks:  # 有 IO 的 after_tool 钩子（审计、包裹不可信数据）在这里执行
            state = RunState(run_id=inp.run_id, metadata=meta)
            if inp.approval:
                state.approvals[inp.call.id] = bool(inp.approval.get("approved"))
                state.approval_log.append(inp.approval)
            for h in self.tool_hooks:
                # 这些钩子会写文件、写审计库（同步 IO）：放进线程执行，不能卡住事件循环
                new = await asyncio.to_thread(h.after_tool, state, inp.call, result)
                if new is not None:
                    result = new
        return result


# ============================================================================================
# Workflow：确定性代码，只负责编排。不能直接调模型、读时钟、生成随机数、做网络请求。
# ============================================================================================


class _ToolView:
    """给 PermissionPolicy 用的"工具替身"：workflow 里没有 Tool 对象（它属于 worker 进程），只有 ToolSpec。"""

    def __init__(self, spec: ToolSpec):
        self.name = spec.name
        self.risk = spec.risk

    def parse_arguments(self, arguments: str) -> tuple[dict | None, str | None]:
        # 只检查"是不是 JSON 对象"。完整的 Schema 校验在 registry 里做；参数明显不合法时不打扰审批人。
        try:
            value = json.loads(arguments or "{}")
        except json.JSONDecodeError as e:
            return None, f"参数不是合法 JSON：{e}"
        return (value, None) if isinstance(value, dict) else (None, "参数必须是 JSON 对象")


def _unanswered_calls(state: RunState) -> list[ToolCall]:
    """最后一条 assistant 消息里、还没有 tool 结果的工具调用（与 Agent._unanswered_calls 相同）。"""
    idx = next((i for i in range(len(state.messages) - 1, -1, -1) if state.messages[i]["role"] == "assistant"), None)
    if idx is None:
        return []
    done = {m.get("tool_call_id") for m in state.messages[idx + 1 :] if m["role"] == "tool"}
    return [c for c in calls_in(state.messages[idx]) if c.id not in done]


@workflow.defn
class AgentWorkflow:
    """在 Temporal 里运行的 Agent 主循环。

    - 每次模型调用是一个 llm_step activity，每次工具调用是一个 execute_tool activity；
    - 权限判断（RBAC、dangerous 需要审批）在 workflow 里做：它们是纯逻辑；
    - 需要审批时 wait_condition 等 signal approve（或 update decide），带超时，超时按拒绝处理；
    - 事件历史过长时 continue-as-new，把 RunState 带到新的 run 里；
    - query status() 随时可查：步数、等待中的审批、调用过的工具、用量。

    扩展：子类覆盖 extra_hooks() 可以加入更多**纯逻辑**的 agentkit Hook。Temporal 对子类有三个要求：
    用 @workflow.defn(name=...) 起新名字；重新声明 @workflow.run（调用 super().run(inp)）；run 的参数注解要和
    @workflow.init 的**字面上完全相同**（写 `inp: AgentInput`，写成 `kt.AgentInput` 会被判为"参数不匹配"）。
    """

    @workflow.init
    def __init__(self, inp: AgentInput) -> None:
        now = workflow.time()  # 不能用 time.time()：重放时必须拿到和第一次执行时相同的值
        carry = dict(inp.carry or {})
        if carry.get("state"):
            self._state = RunState.from_dict(carry["state"])
            self._state.segment_started_at = now
        else:
            # 显式给出 run_id / started_at：RunState 的默认工厂用了 uuid4() 和 time.time()，
            # 而 agentkit 是 passthrough 模块，沙箱拦不住它们 —— 只会悄悄地让每次重放得到不同的值。
            self._state = RunState(
                run_id=workflow.info().workflow_id,
                metadata=dict(inp.metadata or {}),
                started_at=now,
                segment_started_at=now,
            )
        self._inp = inp
        self._runs = int(carry.get("runs", 1))
        self._decisions: dict[str, dict] = dict(carry.get("decisions", {}))  # call_id → 决定（含提前到达的）
        self._ignored: list[dict] = list(carry.get("ignored", []))  # 被忽略的重复 / 迟到决定（审计用）
        self._pending: dict[str, dict] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._phase = "starting"
        self._steps_this_run = 0
        self._hooks: list[Hook] = [
            PermissionPolicy(
                role_tools={r: set(v) for r, v in inp.role_tools.items()} if inp.role_tools is not None else None,
                ask_risks=set(inp.ask_risks),
                deny_tools=set(inp.deny_tools),
            ),
            *self.extra_hooks(inp),
        ]

    def extra_hooks(self, inp: AgentInput) -> list[Hook]:
        """子类覆盖：追加纯逻辑的 Hook（InputGuard、OutputGuard、LoopGuard……）。默认没有。"""
        return []

    # ------------------------------------------------------------------ 消息处理：signal / update / query

    @workflow.signal
    def approve(self, decision: ApprovalDecision) -> None:
        """审批 signal（fire-and-forget）。允许"先到"：决定先存起来，workflow 走到等待点时直接取用。"""
        self._decide(decision)

    @workflow.update
    def decide(self, decision: ApprovalDecision) -> str:
        """审批 update：和 signal 效果相同，但调用方能同步拿到结果（accepted / ignored:duplicate / ignored:late）。"""
        return self._decide(decision)

    @decide.validator
    def _validate_decide(self, decision: ApprovalDecision) -> None:
        # 验证器被拒绝的 update 不会写进事件历史：审批界面点错了 call_id，直接得到错误，不留垃圾事件
        if decision.call_id not in self._pending and decision.call_id not in self._decisions:
            raise ValueError(f"没有等待审批的调用 {decision.call_id}")

    @workflow.query
    def status(self) -> AgentStatus:
        s = self._state
        return AgentStatus(
            workflow_id=workflow.info().workflow_id,
            status=self._phase,
            step=s.step,
            max_steps=self._inp.max_steps,
            pending_approvals=list(self._pending.values()),
            tools_called=[t["name"] for t in s.tool_log],
            usage=s.usage,
            cost_usd=round(s.cost_usd, 6),
            approval_log=list(s.approval_log),
            runs=self._runs,
            history_length=workflow.info().get_current_history_length(),
            history_size_bytes=workflow.info().get_current_history_size(),
        )

    def _decide(self, d: ApprovalDecision) -> str:
        """审批状态机：第一个决定生效；重复的、超时之后才到的决定只记录、不生效（练习 (b) 的生产版）。"""
        prev = self._decisions.get(d.call_id)
        if prev is not None:
            reason = "late" if prev.get("by") == "system:timeout" else "duplicate"
            self._ignored.append({"call_id": d.call_id, "by": d.by, "approved": d.approved, "reason": reason})
            return f"ignored:{reason}"
        self._decisions[d.call_id] = {
            "approved": bool(d.approved),
            "by": d.by,
            "comment": d.comment,
            "at": workflow.now().isoformat(),
            "early": d.call_id not in self._pending,
        }
        return "accepted"

    # ------------------------------------------------------------------ 主循环

    @workflow.run
    async def run(self, inp: AgentInput) -> AgentResult:
        state = self._state
        try:
            await self._load_tools()
            if not inp.carry:  # 新的运行（continue-as-new 后的 run 已经有完整消息）
                state.messages = ([system(inp.system_prompt)] if inp.system_prompt else []) + [
                    dict(m) for m in (inp.history or []) if m.get("role") != "system"
                ]
                text = inp.user_input
                for h in self._hooks:
                    new = h.on_run_start(state, text)  # 可能抛 StopRun（如注入检测）
                    if new is not None:
                        text = new
                state.messages.append(user(text))
            self._phase = "running"
            await self._loop()
        except StopRun as e:
            state.status, state.stop_reason, state.output = "stopped", e.reason, e.message
            for call in _unanswered_calls(state):
                state.messages.append(tool_message(call.id, f"未执行：运行已中止（{e.reason}）"))
        except _LLMFailed as e:
            state.status, state.stop_reason = "failed", f"llm_error: {e}"
            state.output = "抱歉，服务暂时不可用，请稍后再试。"
        except (asyncio.CancelledError, ActivityError) as e:
            # 调用方取消了这次运行（handle.cancel()）：进行中的 activity 会通过心跳收到取消。
            # 记下状态后重新抛出 CancelledError，Temporal 才会把 workflow 标记为 Canceled。
            # （worker 驱逐缓存中的 workflow 时 SDK 也会取消任务，这里只改内存状态、不发任何命令，所以无害。）
            if not _is_cancelled(e):
                raise
            state.status, state.stop_reason, state.output = "cancelled", "cancelled", "（运行已被取消。）"
            self._phase = "cancelled"
            for call in _unanswered_calls(state):
                state.messages.append(tool_message(call.id, "未执行：运行已被取消"))
            raise asyncio.CancelledError("agent run cancelled") from None
        # 不用 finally 做收尾：workflow 被驱逐（evict）时协程可能在别的线程上被垃圾回收，finally 里再调
        # workflow.time() 会报 "Not in workflow event loop"（本课实测）。continue-as-new 也不会走到这里。
        state.active_seconds += workflow.time() - state.segment_started_at
        self._phase = state.status
        for h in self._hooks:
            h.on_run_end(state)
        return AgentResult(
            output=state.output,
            status=state.status,
            stop_reason=state.stop_reason,
            steps=state.step,
            usage=state.usage,
            cost_usd=round(state.cost_usd, 6),
            messages=state.messages,
            tools_called=[t["name"] for t in state.tool_log],
            approval_log=state.approval_log,
            runs=self._runs,
            workflow_id=workflow.info().workflow_id,
        )

    async def _loop(self) -> None:
        state, inp = self._state, self._inp
        await self._run_pending_tools()  # continue-as-new 发生在工具结果齐全之后，这里通常为空
        while state.step < inp.max_steps:
            if self._should_continue_as_new():
                await self._continue_as_new()  # 不会返回
            state.step += 1
            self._steps_this_run += 1
            response = await self._call_llm()
            state.messages.append(response.to_message())
            if not response.tool_calls:
                output = response.content or ""
                for h in self._hooks:
                    new = h.on_final(state, output)
                    if new is not None:
                        output = new
                state.messages[-1]["content"] = output
                reason = "output_truncated" if response.finish_reason == "length" else "final_answer"
                state.status, state.stop_reason, state.output = "completed", reason, output
                return
            await self._run_pending_tools()
        state.status, state.stop_reason = "max_steps", "max_steps"
        state.output = f"（已达到最大步数 {inp.max_steps}，任务未完成。）"

    async def _load_tools(self) -> None:
        specs = await workflow.execute_activity_method(
            AgentActivities.describe_tools,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=self._inp.retry_initial_interval_s)),
        )
        self._specs = {s.name: s for s in specs}

    async def _call_llm(self) -> LLMResponse:
        state, inp = self._state, self._inp
        for h in self._hooks:
            h.before_llm(state, state.messages)
        visible = list(self._specs)
        for h in self._hooks:
            visible = h.visible_tools(state, visible)
        schemas = [self._specs[n].schema for n in visible if n in self._specs] or None
        try:
            response = await workflow.execute_activity_method(
                AgentActivities.llm_step,
                LLMStepInput(messages=state.messages, tools=schemas),
                start_to_close_timeout=timedelta(seconds=inp.llm_timeout_s),
                schedule_to_close_timeout=timedelta(seconds=inp.llm_total_timeout_s),
                heartbeat_timeout=timedelta(seconds=inp.heartbeat_timeout_s),
                retry_policy=RetryPolicy(
                    maximum_attempts=inp.llm_max_attempts,
                    initial_interval=timedelta(seconds=inp.retry_initial_interval_s),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=60),
                ),
                summary=f"step {state.step}",
            )
        except ActivityError as e:
            if _is_cancelled(e):
                raise
            raise _LLMFailed(_cause_message(e)) from e
        # 注意：这里只累计**成功**那次调用的用量。失败的尝试同样花了钱，但不会出现在这里 —— 以网关账单为准（第 29 课）。
        state.usage = state.usage + response.usage
        state.cost_usd += estimate_cost(response.usage, response.model or "default")
        for h in self._hooks:
            h.after_llm(state, response)
        return response

    async def _run_pending_tools(self) -> None:
        state = self._state
        for call in _unanswered_calls(state):
            result = await self._run_one_tool(call)
            state.messages.append(tool_message(call.id, result.content))

    async def _run_one_tool(self, call: ToolCall) -> ToolResult:
        state = self._state
        spec = self._specs.get(call.name)
        view = _ToolView(spec) if spec else None
        if all(entry["id"] != call.id for entry in state.tool_log):
            state.tool_log.append({"id": call.id, "name": call.name})
        denial = None
        i = 0
        while i < len(self._hooks):
            try:
                denial = self._hooks[i].before_tool(state, call, view)
            except PauseRun:
                await self._wait_for_approval(call)  # 等到决定后，从同一个钩子重新判断
                continue
            if denial:
                break
            i += 1
        if denial:
            result = ToolResult(False, denial, "denied")
        else:
            result = await self._execute_tool(call, spec)
            state.tool_calls_count += 1
        for h in self._hooks:
            new = h.after_tool(state, call, result)
            if new is not None:
                result = new
        return result

    async def _wait_for_approval(self, call: ToolCall) -> None:
        state, inp = self._state, self._inp
        self._pending[call.id] = {
            "call_id": call.id,
            "name": call.name,
            "arguments": call.arguments,
            "since": workflow.now().isoformat(),
        }
        state.pending = {"id": call.id, "name": call.name, "arguments": call.arguments}
        self._phase = "waiting_approval"
        workflow.logger.info("等待审批 %s(%s)", call.name, call.id)
        try:
            # 条件已经满足（审批 signal 先到了）时立即返回；否则挂起，不占任何 worker 资源，直到 signal 或定时器触发
            await workflow.wait_condition(
                lambda: call.id in self._decisions,
                timeout=timedelta(seconds=inp.approval_timeout_s),
                timeout_summary=f"approval-timeout:{call.name}",
            )
        except asyncio.TimeoutError:
            self._decisions[call.id] = {
                "approved": False,
                "by": "system:timeout",
                "comment": f"审批超时（{inp.approval_timeout_s:g}s），按拒绝处理",
                "at": workflow.now().isoformat(),
                "early": False,
            }
        d = self._decisions[call.id]
        state.approvals[call.id] = d["approved"]
        state.approval_log.append({"call_id": call.id, "tool": call.name, **{k: d[k] for k in ("approved", "by", "comment", "at")}})
        self._pending.pop(call.id, None)
        state.pending = None
        self._phase = "running"

    async def _execute_tool(self, call: ToolCall, spec: ToolSpec | None) -> ToolResult:
        state, inp = self._state, self._inp
        risk = spec.risk if spec else "read"
        params = retry_policy_for(risk, spec.idempotent if spec else True, initial_interval_s=inp.retry_initial_interval_s)
        timeout = (spec.timeout_s if spec else 30.0) + 10.0  # registry 自己有超时；这里兜底"worker 死了"的情况
        approval = next((a for a in reversed(state.approval_log) if a["call_id"] == call.id), None)
        try:
            return await workflow.execute_activity_method(
                AgentActivities.execute_tool,
                ToolStepInput(call=call, run_id=state.run_id, metadata=state.metadata, approval=approval),
                start_to_close_timeout=timedelta(seconds=timeout),
                heartbeat_timeout=timedelta(seconds=inp.heartbeat_timeout_s),
                retry_policy=_retry_policy(params),
                summary=call.name,
            )
        except ActivityError as e:
            if _is_cancelled(e):
                raise
            # 重试用尽（或不允许重试）。结果未知 —— 对写操作尤其要如实告诉模型，别让它"再试一次"
            return ToolResult(
                False,
                f"错误：工具 {call.name} 执行失败（已尝试 {params['maximum_attempts']} 次）：{_cause_message(e)}。"
                f"如果这是写操作，结果可能未知，请不要自行重试，告知用户需要人工核实。",
                "activity_failed",
                detail=str(e.cause or e),
            )

    # ------------------------------------------------------------------ continue-as-new

    def _should_continue_as_new(self) -> bool:
        if self._steps_this_run == 0:  # 新 run 至少走一步，避免阈值设得太小时无限 continue-as-new
            return False
        info = workflow.info()
        if info.is_continue_as_new_suggested():  # 服务端默认在 4096 个事件 / 4 MB 时建议
            return True
        limit = self._inp.continue_as_new_after_events
        return limit is not None and info.get_current_history_length() >= limit

    async def _continue_as_new(self) -> None:
        # 官方建议：等所有 signal / update 处理函数执行完再 continue-as-new
        await workflow.wait_condition(workflow.all_handlers_finished)
        state = self._state
        state.active_seconds += workflow.time() - state.segment_started_at
        carry = {"state": state.to_dict(), "runs": self._runs + 1, "decisions": self._decisions, "ignored": self._ignored}
        fields = {k: v for k, v in vars(self._inp).items() if k != "carry"}
        workflow.logger.info("事件历史 %s 条，continue-as-new", workflow.info().get_current_history_length())
        workflow.continue_as_new(AgentInput(**fields, carry=carry))


class _LLMFailed(Exception):
    """模型调用在所有重试之后仍然失败。"""


def _is_cancelled(e: BaseException) -> bool:
    if isinstance(e, asyncio.CancelledError):
        return True
    return isinstance(e, ActivityError) and isinstance(e.cause, CancelledError)


def _cause_message(e: ActivityError) -> str:
    cause = e.cause
    if isinstance(cause, ApplicationError):
        return cause.message
    return str(cause or e)


# ============================================================================================
# 便捷函数
# ============================================================================================


def sandbox_runner():
    """workflow 沙箱配置：agentkit 核心模块以 passthrough 方式进入沙箱，本模块（含 AgentWorkflow）照常被沙箱重新导入。

    为什么不直接 with_passthrough_modules("agentkit")？passthrough 按前缀匹配，那样 agentkit.contrib.temporal
    本身也会被放行，workflow 代码里的 time.time() / random 就再也拦不住了。
    """
    import pkgutil

    import agentkit
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    core = [f"agentkit.{m.name}" for m in pkgutil.iter_modules(agentkit.__path__) if m.name != "contrib"]
    return SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(*core))


def make_worker(
    client,
    task_queue: str,
    llm_factory: Callable[[], "AsyncLLM | LLM"],
    tools: Iterable[Tool] | ToolRegistry,
    *,
    tool_hooks: Sequence[Hook] = (),
    idempotency_store: IdempotencyStore | None = None,
    idempotent_tools: Iterable[str] = (),
    workflows: Sequence[type] = (),
    max_concurrent_activities: int = 100,
    max_concurrent_workflow_tasks: int = 100,
    sync_tool_threads: int = 16,
    identity: str | None = None,
    **worker_kwargs: Any,
):
    """创建（但不启动）一个同时承载 AgentWorkflow 和 Agent activities 的 worker。

    用法：`async with make_worker(...):` 或 `await make_worker(...).run()`。
    worker 是无状态的：可以随时加减、随时重启，同一 task_queue 上的任何 worker 都能接着跑任何 workflow。

    并发的两个旋钮（SDK 默认各 100 个槽位）：
    - max_concurrent_activities：这个 worker 同时执行多少个 activity。Agent 的时间几乎都花在等模型上，
      async activity 等待时不占线程，所以它通常受"模型网关给你的并发配额"约束，而不是 CPU；
    - max_concurrent_workflow_tasks：同时处理多少个 workflow 任务（推进编排、重放历史）。每个任务都很短，
      但 worker 刚启动、要为大量 workflow 重放历史时它会成为瓶颈。
    - sync_tool_threads：同步工具的线程池大小（async 工具不占线程）。

    idempotency_store：跨 worker 共享的幂等存储，用异步版（如第 26 课的 AsyncRedisIdempotencyStore；同步版的每次 get/put
    都会阻塞事件循环）。配置后 write/dangerous
    工具按"幂等"对待，允许重试 3 次；进程内的 IdempotencyStore 不能跨 worker 去重，不要在这里用它。

    activity 全部是 async def，所以不需要 activity_executor（SDK 只对同步 activity 要求它，官方推荐 ThreadPoolExecutor）。
    """
    from temporalio.worker import Worker

    acts = AgentActivities(
        llm_factory,
        tools,
        tool_hooks=tool_hooks,
        idempotency_store=idempotency_store,
        idempotent_tools=idempotent_tools,
        sync_tool_threads=sync_tool_threads,
    )
    acts.llm()  # 启动时就创建模型客户端：配置错了（如缺 API key）立刻失败，也避免在第一个 activity 里阻塞事件循环
    worker_kwargs.setdefault("workflow_runner", sandbox_runner())
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows) or [AgentWorkflow],
        activities=acts.activities(),
        max_concurrent_activities=max_concurrent_activities,
        max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
        identity=identity,
        **worker_kwargs,
    )
    worker.agent_activities = acts  # 方便测试 / 运维查看这个 worker 上的工具
    return worker


async def start_agent(
    client,
    input: str | AgentInput,
    metadata: dict | None = None,
    workflow_id: str | None = None,
    task_queue: str = DEFAULT_TASK_QUEUE,
    *,
    workflow_type: str | type = "AgentWorkflow",
    reuse_existing: bool = True,
    **input_fields: Any,
):
    """启动一次 Agent 运行，立即返回 WorkflowHandle（handle.result() 等结果，handle.query 查状态）。

    workflow_id 用业务键（如 f"ticket-{ticket_id}"）：reuse_existing=True 时同一个 ID 正在运行就直接返回已有的那个，
    相当于第 13 课"入队去重"——用户狂点提交也只会有一个 Agent 在跑。
    """
    inp = input if isinstance(input, AgentInput) else AgentInput(user_input=input, metadata=dict(metadata or {}), **input_fields)
    if metadata is not None and isinstance(input, AgentInput):
        inp.metadata = dict(metadata)
    target = workflow_type if isinstance(workflow_type, str) else workflow_type.run  # 子类（如带额外 Hook 的版本）传类本身
    return await client.start_workflow(
        target,
        inp,
        id=workflow_id or f"agent-{uuid.uuid4().hex[:12]}",
        task_queue=task_queue,
        result_type=AgentResult,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING if reuse_existing else WorkflowIDConflictPolicy.FAIL,
    )


async def approve(
    client,
    workflow_id: str,
    call_id: str,
    approved: bool = True,
    by: str = "",
    comment: str = "",
    *,
    wait: bool = False,
) -> str | None:
    """给等待中的工具调用一个审批决定。

    wait=False：发 signal，立即返回 None（审批"先到"也没关系，workflow 会先存起来）；
    wait=True：发 update，等 workflow 处理完并返回结果（"accepted" / "ignored:duplicate" / "ignored:late"），
               call_id 不存在时抛 WorkflowUpdateFailedError。
    """
    handle = client.get_workflow_handle(workflow_id)
    decision = ApprovalDecision(call_id=call_id, approved=approved, by=by, comment=comment)
    if wait:
        return await handle.execute_update("decide", decision, result_type=str)
    await handle.signal("approve", decision)
    return None


async def agent_status(client, workflow_id: str) -> AgentStatus:
    """查询运行状态（query 由 worker 基于内存状态或重放计算，不写事件历史）。"""
    return await client.get_workflow_handle(workflow_id).query("status", result_type=AgentStatus)


# ============================================================================================
# 事件历史摘要：演示 / 测试 / 排障时用来"看证据"
# ============================================================================================


def summarize_history(history) -> list[dict]:
    """把 WorkflowHistory 压成易读的行：{id, type, detail}。detail 里有 activity 名称、尝试次数、执行它的 worker。"""
    from temporalio.api.enums.v1 import EventType

    names: dict[int, str] = {}
    lines = []
    for ev in history.events:
        kind = EventType.Name(ev.event_type).removeprefix("EVENT_TYPE_").title().replace("_", "")
        detail = ""
        if ev.HasField("activity_task_scheduled_event_attributes"):
            names[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name
            detail = names[ev.event_id]
        elif ev.HasField("activity_task_started_event_attributes"):
            a = ev.activity_task_started_event_attributes
            detail = f"{names.get(a.scheduled_event_id, '?')} attempt={a.attempt} worker={a.identity}"
            if a.HasField("last_failure"):  # 重试时，上一次尝试为什么失败（异常、心跳超时……）
                detail += f" | 上次失败：{a.last_failure.message[:60]}"
        elif ev.HasField("activity_task_completed_event_attributes"):
            detail = names.get(ev.activity_task_completed_event_attributes.scheduled_event_id, "?")
        elif ev.HasField("activity_task_failed_event_attributes"):
            a = ev.activity_task_failed_event_attributes
            detail = f"{names.get(a.scheduled_event_id, '?')}: {a.failure.message[:80]}"
        elif ev.HasField("activity_task_timed_out_event_attributes"):
            a = ev.activity_task_timed_out_event_attributes
            detail = f"{names.get(a.scheduled_event_id, '?')}: {a.failure.message[:80]}"
        elif ev.HasField("workflow_execution_signaled_event_attributes"):
            detail = ev.workflow_execution_signaled_event_attributes.signal_name
        elif ev.HasField("workflow_execution_update_accepted_event_attributes"):
            detail = ev.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
        elif ev.HasField("timer_started_event_attributes"):
            detail = f"{ev.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta().total_seconds():g}s"
        elif ev.HasField("workflow_task_failed_event_attributes"):
            detail = ev.workflow_task_failed_event_attributes.failure.message[:120]
        lines.append({"id": ev.event_id, "type": kind, "detail": detail})
    return lines


def activity_attempts(history) -> list[tuple[str, int]]:
    """按完成顺序列出每个**已完成** activity 的 (名称, 最终尝试次数)。重放不会增加这里的条目 —— 这就是"不会重复执行"的证据。"""
    started: dict[int, int] = {}
    names: dict[int, str] = {}
    out = []
    for ev in history.events:
        if ev.HasField("activity_task_scheduled_event_attributes"):
            names[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name
        elif ev.HasField("activity_task_started_event_attributes"):
            a = ev.activity_task_started_event_attributes
            started[a.scheduled_event_id] = a.attempt
        elif ev.HasField("activity_task_completed_event_attributes"):
            sid = ev.activity_task_completed_event_attributes.scheduled_event_id
            out.append((names.get(sid, "?"), started.get(sid, 1)))
    return out


def format_history(lines: list[dict], only: Sequence[str] | None = None) -> str:
    """把 summarize_history 的结果排成文本。only：只保留这些事件类型（如 ["ActivityTaskStarted", ...]）。"""
    keep = [ln for ln in lines if only is None or ln["type"] in only]
    return "\n".join(f"  {ln['id']:>3}  {ln['type']:<34} {ln['detail']}" for ln in keep)


__all__ = [
    "DEFAULT_TASK_QUEUE",
    "AgentActivities",
    "AgentInput",
    "AgentResult",
    "AgentStatus",
    "AgentWorkflow",
    "ApprovalDecision",
    "LLMStepInput",
    "ToolSpec",
    "ToolStepInput",
    "activity_attempts",
    "agent_status",
    "approve",
    "format_history",
    "make_worker",
    "retry_policy_for",
    "sandbox_runner",
    "start_agent",
    "summarize_history",
]
