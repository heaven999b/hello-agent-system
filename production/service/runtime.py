"""API 和 worker 共用的装配代码：连接池、Redis、模型、工具、Hook、检查点、队列、事件。

每个进程在启动时创建**一个** Runtime，所有请求 / 任务共享它（连接池、线程池、Hook 实例、指标对象都是进程级的）。
每次运行真正独立的东西（对话、步数、审批、fence）全在 RunState 和"本次运行的检查点视图"里（第 30 课 2.1）。

Hook 顺序（Agent 按列表顺序调用）：
    OTelTracer          放第一个：给根 span 补 conversation id、工具 call id（第 28 课）
    AsyncClassifierGuard  输入护栏：命中直接 StopRun，一次模型调用都不花（第 29 课）
    PrometheusHook      运行数、耗时、token、工具调用（第 28 课）
    AsyncRateLimitHook  调模型前按租户从 Redis 令牌桶拿令牌；等不到 → rate_limited → 后台任务 RetryLater（第 26 课）
    CedarPolicy         工具级 + 参数级授权；dangerous 工具 → PauseRun 等人工审批（第 29 课）
    RunEventsHook       放最后：只有通过了授权的工具调用才推送 tool_started
"""

from __future__ import annotations

import logging
import os
from collections import deque

from agentkit.aio import AsyncAgent, AsyncResilientLLM, AsyncToolExecutor, wait_for  # wait_for：取消安全版（gh-86296）
from agentkit.contrib.guards import AsyncClassifierGuard, RegexClassifier
from agentkit.contrib.otel import OTelTracer, PrometheusHook, normalize_stop_reason
from agentkit.contrib.policy import CedarPolicy, entity_args_context
from agentkit.contrib.postgres import AsyncPostgresCheckpointer, AsyncPostgresJobQueue
from agentkit.contrib.redis_store import AsyncRateLimitHook, AsyncRedisIdempotencyStore, AsyncRedisTokenBucket
from agentkit.hooks import Hook
from agentkit.tools import ToolRegistry

from . import telemetry
from .backend import Backend
from .config import Settings
from .events import EventBus, TaskSet
from .scripted import scripted_llm
from .tools import SYSTEM_PROMPT, make_tools

QUEUE_TABLE = "agent_jobs"
RUNS_TABLE = "agent_runs"


SWALLOWED_CANCEL_EVENT = "swallowed_cancellation"  # agentkit.aio 补抛被吞掉的取消时，warning 日志 extra 里的 agentkit_event


class SwallowedCancelCounter(logging.Handler):
    """把框架"补抛被吞掉的取消"这件事变成指标 itdesk_swallowed_cancellations_total。

    分工：检测和补抛由 agentkit.aio 负责（Task.cancelling() 比进入运行时的基线大 → 调模型、执行工具之前补抛，
    Python 3.11+；3.10 上没有 cancelling()，检查关闭）。服务这边只负责让它**可见**：不为 0 就说明有依赖在吞取消
    （Python < 3.12 的 asyncio.wait_for 竞态，本课实测 redis-py、psycopg_pool 都会），该升级 Python 或换库了。
    按日志记录上的结构化字段 agentkit_event 匹配，而不是按措辞：框架改了文案，计数也不会悄悄停在 0。
    """

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "agentkit_event", None) == SWALLOWED_CANCEL_EVENT:
            telemetry.SWALLOWED_CANCELS.inc()


def install_swallowed_cancel_counter() -> None:
    logger = logging.getLogger("agentkit.aio")
    if not any(isinstance(h, SwallowedCancelCounter) for h in logger.handlers):
        logger.addHandler(SwallowedCancelCounter(level=logging.WARNING))


class RunEventsHook(Hook):
    """把工具开始 / 结束、运行段结束推送到 Redis Streams（尽力而为，失败不影响运行）。"""

    def __init__(self, bus: EventBus):
        self.bus = bus

    async def before_tool(self, state, call, tool):
        await self.bus.publish(state.run_id, "tool_started", tool=call.name, call_id=call.id)
        return None

    async def after_tool(self, state, call, result):
        await self.bus.publish(state.run_id, "tool_finished", tool=call.name, call_id=call.id, ok=result.ok,
                               error_type=result.error_type)
        return None

    async def on_run_end(self, state):
        # 注意：on_run_end 在最后一次保存检查点**之前**执行，所以这只是"进度"，终态以任务结果 / 检查点为准
        await self.bus.publish(state.run_id, "segment_ended", status=state.status,
                               stop_reason=normalize_stop_reason(state.stop_reason), step=state.step)
        return None


def build_llm(settings: Settings):
    """三种模型后端，外面统一套一层 AsyncResilientLLM 做**进程内舱壁**（max_concurrency）。

    重试只放一层（第 29 课）：LiteLLM Router 自己会重试和降级，所以 litellm 后端这层 max_attempts=1；
    直连网关的 openai 后端由这层重试 3 次。
    """
    if settings.llm_backend == "scripted":
        inner = scripted_llm(settings.scripted_latency_s, settings.scripted_jitter, seed=os.getpid())
        inner.calls = deque(maxlen=200)  # AsyncScriptedLLM 默认保存每次调用的深拷贝：长时间运行的服务里只留最近 200 次
        attempts = 1
    elif settings.llm_backend == "litellm":
        from agentkit.contrib.gateway import AsyncLiteLLMRouterLLM

        inner = AsyncLiteLLMRouterLLM.from_env(num_retries=2, timeout=60)
        attempts = 1
    else:
        from agentkit.aio import AsyncOpenAICompatLLM

        inner = AsyncOpenAICompatLLM(max_connections=settings.llm_max_concurrency)
        attempts = 3
    return AsyncResilientLLM(inner, max_attempts=attempts, max_concurrency=settings.llm_max_concurrency)


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = None
        self.redis = None
        self.redis_blocking = None
        self.tasks = TaskSet()

    @classmethod
    async def create(cls, settings: Settings) -> "Runtime":
        """表结构由迁移任务负责（migrate.py），这里只连接、不建表。"""
        self = cls(settings)
        await self._open()
        return self

    async def _open(self) -> None:
        from psycopg_pool import AsyncConnectionPool
        import redis.asyncio as aredis

        s = self.settings
        # 一个进程一个池：检查点、队列、业务表共用。池大小按"同时**持有**连接的协程数"估算（第 26 课问题 7）
        self.pool = AsyncConnectionPool(
            s.database_url, min_size=s.pg_pool_min, max_size=s.pg_pool_max, open=False,
            kwargs={"autocommit": True}, name=f"{s.service_name}-pool", timeout=10,
        )
        await self.pool.open(wait=True, timeout=30)
        self.redis = aredis.Redis.from_url(s.redis_url, socket_timeout=5, socket_connect_timeout=5, health_check_interval=30)
        # 阻塞读（XREAD BLOCK）单独一个客户端：socket 超时要比阻塞时长长，否则每次阻塞读都会被当成超时
        self.redis_blocking = aredis.Redis.from_url(s.redis_url, socket_timeout=30, socket_connect_timeout=5)
        self.bus = EventBus(self.redis, self.redis_blocking, maxlen=s.events_maxlen, ttl_s=s.events_ttl_s)

        self.ckpt = AsyncPostgresCheckpointer(self.pool, RUNS_TABLE)
        self.queue = AsyncPostgresJobQueue(self.pool, QUEUE_TABLE, max_attempts=5, base_backoff=0.5, max_backoff=30)

        self.backend = Backend(self.pool, tool_latency_s=s.tool_latency_s, diagnostics_latency_s=s.diagnostics_latency_s)
        self.registry = ToolRegistry(make_tools(self.backend))
        self.idempotency = AsyncRedisIdempotencyStore(self.redis, namespace="itdesk:idem", ttl_seconds=86400)
        self.registry.idempotency_store = self.idempotency
        # 进程级的工具执行器（有上限的线程池）：即使将来按任务定制 Agent，线程池也只有一个
        self.executor = AsyncToolExecutor(self.registry, max_threads=16)
        self.llm = build_llm(s)

        self.provider = telemetry.setup(s)
        self.tracer = OTelTracer(self.provider, provider_name="openai")
        # 待审批数由 API 的采样任务从数据库读出后 set（暂停和恢复常发生在不同进程，进程内增减会漂移）
        self.prom = PrometheusHook(track_approvals=False)
        self.guard = AsyncClassifierGuard(RegexClassifier(), on="input", threshold=0.5)
        llm_bucket = AsyncRedisTokenBucket(self.redis, s.llm_rate_per_sec, s.llm_burst, prefix="itdesk:rl:llm")
        self.ratelimit = AsyncRateLimitHook(llm_bucket, wait_timeout=s.llm_rate_wait_s)
        self.api_bucket = AsyncRedisTokenBucket(
            self.redis, s.api_rate_per_sec, s.api_burst, prefix="itdesk:rl:api",
            overrides={t: (float(v[0]), float(v[1])) for t, v in s.api_rate_overrides.items()},
        )
        self.policy = CedarPolicy(
            s.cedar_policies, s.cedar_schema, tools=self.registry,
            context_fn=entity_args_context({"reset_password": {"target_user_id": ("target_user", "User")}}),
        )
        self.events_hook = RunEventsHook(self.bus)
        install_swallowed_cancel_counter()

    # ------------------------------------------------------------------ Agent

    def hooks(self) -> list:
        return [self.tracer, self.guard, self.prom, self.ratelimit, self.policy, self.events_hook]

    def new_agent(self, checkpointer=None) -> AsyncAgent:
        """每个进程建一个，被所有会话 / 任务并发复用；每次运行通过 checkpointer= 传入自己的检查点视图。"""
        return AsyncAgent(
            self.llm,
            self.registry,
            system_prompt=SYSTEM_PROMPT,
            name="itdesk",
            max_steps=self.settings.max_steps,
            hooks=self.hooks(),
            checkpointer=checkpointer if checkpointer is not None else self.ckpt,
            tracer=self.tracer,
            run_timeout=self.settings.run_timeout_s,
            executor=self.executor,
        )

    # ------------------------------------------------------------------ 就绪检查

    async def check_dependencies(self, timeout: float = 1.0) -> dict:
        """readiness 用：Postgres 和 Redis 都能在 timeout 内应答才算就绪。liveness **不要**调它（见讲义问题 3）。"""
        async def pg():
            async with self.pool.connection(timeout=timeout) as c:
                await c.execute("SELECT 1")

        async def rds():
            await self.redis.ping()

        result = {}
        for name, fn in (("postgres", pg), ("redis", rds)):
            try:
                await wait_for(fn(), timeout)
                result[name] = "ok"
            except Exception as e:  # noqa: BLE001
                result[name] = f"{type(e).__name__}: {e}"[:200]
        return result

    async def aclose(self) -> None:
        await self.tasks.drain()
        try:
            self.tracer.force_flush(3000)
        except Exception:  # noqa: BLE001
            pass
        self.executor.close()
        close = getattr(self.llm.chain[0][0], "aclose", None) if hasattr(self.llm, "chain") else None
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
        for r in (self.redis, self.redis_blocking):
            if r is not None:
                await r.aclose()
        if self.pool is not None:
            await self.pool.close()
