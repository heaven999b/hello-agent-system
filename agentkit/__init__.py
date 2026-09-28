"""agentkit —— 一个为教学而写、按生产标准设计的最小 Agent 框架（async）。

每个模块对应一节课：
    types / llm        LLM 抽象与消息格式            （第 01、02 课）
    agent              Agent 主循环（async）         （第 02 课）
    tools              工具系统                      （第 03 课）
    context / memory   上下文工程与长期记忆          （第 04 课）
    workflows          Agent 架构、编排模式与多 Agent（第 05、06 课）
    reliability/budget/state  可靠性、预算、检查点   （第 08 课）
    guardrails / permissions / audit  安全与治理     （第 09 课）
    tracing / viewer   可观测性                      （第 10 课）
    evals              评估                          （第 11 课）
    limits / timeouts  并发控制：舱壁、令牌桶、取消安全的超时（第 12、30 课）
    distributed        多进程：任务队列、租约、fencing、worker 进程池（第 13 课）

整个框架只有一套实现，而且是 async 的：一个进程里同时推进成百上千个会话（第 02 课讲为什么）。
多个进程之间怎么分工、怎么接手崩溃的运行，由 agentkit.distributed 负责（SQLite，单机多进程，零依赖）；
多机部署把同一套接口换成成熟组件：
    agentkit.contrib   Postgres、Redis、Temporal、OpenTelemetry、LiteLLM、Cedar 适配器（第 26–29 课）
    production/        把它们组装起来的参考服务：API + 多 worker + 压测 + 故障注入（第 31 课）
逐模块"这里做到了什么、上线还差什么"见 docs/production-readiness.md。

第三部分（17–25 课）在 agentkit 之上构建进阶能力，代码在各课目录中：
    检索质量（17）、记忆系统（18）、MCP 与沙箱（19）、框架对照（20）、
    Agent 的数据（21）、评估方法论（22）、优化（23）、编码 Agent（24）、主动式 Agent（25）
"""

from .agent import (
    Agent,
    AgentEvent,
    ApprovalRequired,
    RunFinished,
    RunResult,
    RunStarted,
    ToolFinished,
    ToolStarted,
)
from .audit import AuditLog
from .budget import BudgetHook
from .context import SlidingWindow, SummarizingCompactor, estimate_tokens
from .guardrails import InputGuard, OutputGuard, ToolOutputGuard, UNTRUSTED_DATA_RULE, detect_injection, redact_pii
from .hooks import Hook, PauseRun, StopRun
from .limits import KeyedLimiter, LimitExceeded, TokenBucket
from .llm import (
    LLM,
    LLMError,
    OpenAICompatLLM,
    ScriptedLLM,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallAccumulator,
    call_tool,
    call_tools,
    default_llm,
    reply,
)
from .memory import MemoryStore, memory_tools
from .permissions import PermissionPolicy
from .reliability import CircuitBreaker, CircuitOpenError, ResilientLLM, retry_call
from .state import FileCheckpointer, InMemoryCheckpointer, RunState
from .timeouts import wait_for
from .tools import (
    IdempotencyStore,
    Tool,
    ToolContext,
    ToolError,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    isolated,
    maybe_await,
    run_in_subprocess,
    tool,
)
from .tracing import Span, Tracer, jsonl_exporter, render_tree
from .types import LLMResponse, ToolCall, Usage

__all__ = [name for name in dir() if not name.startswith("_")]
__version__ = "0.2.0"
