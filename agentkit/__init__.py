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
