"""agentkit —— 一个为教学而写、按生产标准设计的最小 Agent 框架。

每个模块对应一节课：
    types / llm        LLM 抽象与消息格式            （第 01、02 课）
    agent              Agent 主循环                  （第 02 课）
    tools              工具系统                      （第 03 课）
    context / memory   上下文工程与长期记忆          （第 04 课）
    workflows          Agent 架构、编排模式与多 Agent（第 05、06 课）
    reliability/budget/state  可靠性、预算、检查点   （第 08 课）
    guardrails / permissions / audit  安全与治理     （第 09 课）
    tracing / viewer   可观测性                      （第 10 课）
    evals              评估                          （第 11 课）

第三部分（17–25 课）在 agentkit 之上构建进阶能力，代码在各课目录中：
    检索质量（17）、记忆系统（18）、MCP 与沙箱（19）、框架对照（20）、
    Agent 的数据（21）、评估方法论（22）、优化（23）、编码 Agent（24）、主动式 Agent（25）

注意：本包（agentkit 核心）是同步、单进程、状态在内存或本地文件里的教学实现，不要原样上线。
第四部分（26–31 课）给出生产路径，接口与核心一致：
    agentkit.aio       生产异步运行时：AsyncAgent（并发会话、真取消、run_timeout、舱壁、流式事件）、
                       AsyncOpenAICompatLLM、AsyncResilientLLM、AsyncToolExecutor、KeyedLimiter（第 30 课）
    agentkit.contrib   成熟组件适配器：Postgres、Redis、Temporal、OpenTelemetry、LiteLLM、Cedar（第 26–29 课）
    production/        把它们组装起来的参考服务：API + 多 worker + 压测 + 故障注入（第 31 课）
逐模块的差距与迁移方法见 docs/production-readiness.md。
"""

from .agent import Agent, RunResult
from .audit import AuditLog
from .budget import BudgetHook
from .context import SlidingWindow, SummarizingCompactor, estimate_tokens
from .guardrails import InputGuard, OutputGuard, ToolOutputGuard, UNTRUSTED_DATA_RULE, detect_injection, redact_pii
from .hooks import Hook, PauseRun, StopRun
from .llm import LLM, LLMError, OpenAICompatLLM, ScriptedLLM, call_tool, call_tools, default_llm, reply
from .memory import MemoryStore, memory_tools
from .permissions import PermissionPolicy
from .reliability import CircuitBreaker, ResilientLLM, retry_call
from .state import FileCheckpointer, InMemoryCheckpointer, RunState
from .tools import IdempotencyStore, Tool, ToolContext, ToolError, ToolRegistry, ToolResult, tool
from .tracing import Span, Tracer, jsonl_exporter, render_tree
from .types import LLMResponse, ToolCall, Usage

__all__ = [name for name in dir() if not name.startswith("_")]
__version__ = "0.1.0"
