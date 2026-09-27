"""agentkit.aio —— 生产级的异步运行时（第 30 课）。

同步的 agentkit.Agent 适合学习原理；真正的服务要用这里的 AsyncAgent：
一个进程并发推进大量会话、只读工具并行、真正的取消与超时、按租户的舱壁、流式输出。

    from agentkit.aio import AsyncAgent, AsyncOpenAICompatLLM, AsyncResilientLLM, KeyedLimiter

    agent = AsyncAgent(
        AsyncResilientLLM(AsyncOpenAICompatLLM(max_connections=50), max_concurrency=20),
        tools=[...],
        limiter=KeyedLimiter(per_key=5, global_limit=200),   # 每个租户最多 5 个同时在跑的运行
        run_timeout=120,
    )
    result = await agent.run("...", metadata={"tenant_id": "acme", "user_id": "alice"})

    async with contextlib.aclosing(agent.stream("...", metadata=...)) as events:
        async for event in events: ...
"""

from .agent import (
    AgentEvent,
    ApprovalRequired,
    AsyncAgent,
    RunFinished,
    RunStarted,
    ToolFinished,
    ToolStarted,
)
from .limits import AsyncTokenBucket, KeyedLimiter, LimitExceeded
from .llm import (
    AsyncLLM,
    AsyncOpenAICompatLLM,
    AsyncScriptedLLM,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallAccumulator,
    default_async_llm,
)
from .reliability import AsyncCircuitBreaker, AsyncResilientLLM, aretry_call
from .timeouts import wait_for
from .tools import AsyncToolExecutor, isolated, maybe_await, run_in_subprocess

__all__ = [name for name in dir() if not name.startswith("_")]
