"""12-factor 配置：**所有**可变项都来自环境变量，代码里不写任何地址、密钥、配额。

同一个镜像在 dev / staging / prod 之间只换环境变量（K8s 里来自 ConfigMap 和 Secret，见 deploy/k8s/）。
每个变量的含义和默认值见 production/.env.example；这里只做解析和校验，出错就在启动时失败（fail fast），
而不是跑到第一个请求才发现连不上数据库。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
LESSON29_CONFIGS = REPO_ROOT / "lessons" / "29_gateway_and_guardrails" / "configs"


def _get(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    return default if value is None or value == "" else value


def _float(env, name, default: float) -> float:
    raw = _get(env, name)
    try:
        return default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，收到 {raw!r}") from None


def _int(env, name, default: int) -> int:
    return int(_float(env, name, default))


def _bool(env, name, default: bool) -> bool:
    raw = _get(env, name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _json(env, name, default):
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"环境变量 {name} 不是合法的 JSON：{e}") from None


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    role: str = "api"  # api / worker / migrate
    service_name: str = "itdesk-api"
    instance_id: str = "local"  # 写进检查点 writer 与 worker_id：K8s 里取 Pod 名（Downward API）
    environment: str = "dev"

    # 模型：litellm（AsyncLiteLLMRouterLLM，推荐）/ openai（AsyncOpenAICompatLLM 直连网关）/ scripted（离线剧本，压测和测试用）
    llm_backend: str = "litellm"
    llm_max_concurrency: int = 32  # 本进程对模型的在途上限（舱壁）；全局配额在网关（第 29 课）
    scripted_latency_s: float = 0.3
    scripted_jitter: float = 0.3
    tool_latency_s: float = 0.05  # 下游系统（工单、账号）的响应时间：副作用已落库、响应还没回来的窗口
    diagnostics_latency_s: float = 1.5

    run_timeout_s: float = 120.0
    max_steps: int = 8

    # worker
    worker_concurrency: int = 16
    lease_seconds: float = 30.0
    heartbeat_seconds: float = 0.0  # 0 = 租约的 1/3
    worker_grace_seconds: float = 20.0
    poll_seconds: float = 0.5
    release_on_cancel: bool = True  # 停机时被取消的任务立刻归还（fence 保证安全），而不是等租约过期
    health_port: int = 8081
    health_addr: str = "0.0.0.0"  # K8s 探针从 Pod IP 访问；本机 run_local 用 127.0.0.1
    metrics_port: int = 9100
    metrics_addr: str = "127.0.0.1"

    # 限流与舱壁
    api_rate_per_sec: float = 10.0  # 每个租户的请求速率（Redis 令牌桶，所有 API 副本共享）
    api_burst: float = 20.0
    api_rate_overrides: dict = field(default_factory=dict)  # {"tenant": [rate, burst]}
    llm_rate_per_sec: float = 20.0  # 每个租户的模型调用速率（worker 与 API 共享同一个桶）
    llm_burst: float = 40.0
    llm_rate_wait_s: float = 2.0
    streams_per_tenant: int = 20  # 每个 API 进程里，每个租户同时打开的交互式流（KeyedLimiter）
    streams_global: int = 200
    stream_admission_timeout_s: float = 0.05

    pg_pool_min: int = 2
    pg_pool_max: int = 10

    events_maxlen: int = 500
    events_ttl_s: int = 86400
    sse_max_seconds: float = 600.0  # 单条 SSE 连接的最长时长：到点主动断开，客户端带 Last-Event-ID 重连（对代理友好）

    api_keys: dict = field(default_factory=dict)  # {sha256(key): {"tenant","user","roles","plan","department"}}

    otel_sample_ratio: float = 1.0
    otlp_endpoint: str | None = None
    spans_jsonl_dir: str | None = None  # 测试 / 本机：每个进程把 span 写成 JSONL，端到端测试据此核对跨队列 trace

    cedar_policies: str = str(LESSON29_CONFIGS / "policies.cedar")
    cedar_schema: str = str(LESSON29_CONFIGS / "schema.cedarschema")
    seed_demo_data: bool = True

    @classmethod
    def from_env(cls, role: str = "api", env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        database_url = _get(env, "DATABASE_URL")
        redis_url = _get(env, "REDIS_URL")
        missing = [n for n, v in (("DATABASE_URL", database_url), ("REDIS_URL", redis_url)) if not v]
        if missing:
            raise RuntimeError(f"缺少必需的环境变量：{', '.join(missing)}（见 production/.env.example）")
        instance = _get(env, "POD_NAME") or f"{socket.gethostname()}-{os.getpid()}"
        s = cls(
            database_url=database_url,
            redis_url=redis_url,
            role=role,
            service_name=_get(env, "SERVICE_NAME", f"itdesk-{role}"),
            instance_id=instance,
            environment=_get(env, "DEPLOYMENT_ENV", "dev"),
            llm_backend=_get(env, "LLM_BACKEND", "litellm"),
            llm_max_concurrency=_int(env, "LLM_MAX_CONCURRENCY", 32),
            scripted_latency_s=_float(env, "SCRIPTED_LLM_LATENCY_MS", 300) / 1000,
            scripted_jitter=_float(env, "SCRIPTED_LLM_JITTER", 0.3),
            tool_latency_s=_float(env, "TOOL_LATENCY_MS", 50) / 1000,
            diagnostics_latency_s=_float(env, "DIAGNOSTICS_LATENCY_MS", 1500) / 1000,
            run_timeout_s=_float(env, "RUN_TIMEOUT_SECONDS", 120),
            max_steps=_int(env, "MAX_STEPS", 8),
            worker_concurrency=_int(env, "WORKER_CONCURRENCY", 16),
            lease_seconds=_float(env, "WORKER_LEASE_SECONDS", 30),
            heartbeat_seconds=_float(env, "WORKER_HEARTBEAT_SECONDS", 0),
            worker_grace_seconds=_float(env, "WORKER_GRACE_SECONDS", 20),
            poll_seconds=_float(env, "WORKER_POLL_SECONDS", 0.5),
            release_on_cancel=_bool(env, "WORKER_RELEASE_ON_CANCEL", True),
            health_port=_int(env, "WORKER_HEALTH_PORT", 8081),
            health_addr=_get(env, "WORKER_HEALTH_ADDR", "0.0.0.0"),
            metrics_port=_int(env, "METRICS_PORT", 9100),
            metrics_addr=_get(env, "METRICS_ADDR", "127.0.0.1"),
            api_rate_per_sec=_float(env, "API_RATE_PER_SEC", 10),
            api_burst=_float(env, "API_BURST", 20),
            api_rate_overrides=_json(env, "API_RATE_OVERRIDES_JSON", {}),
            llm_rate_per_sec=_float(env, "LLM_RATE_PER_SEC", 20),
            llm_burst=_float(env, "LLM_BURST", 40),
            llm_rate_wait_s=_float(env, "LLM_RATE_WAIT_SECONDS", 2),
            streams_per_tenant=_int(env, "STREAMS_PER_TENANT", 20),
            streams_global=_int(env, "STREAMS_GLOBAL", 200),
            stream_admission_timeout_s=_float(env, "STREAM_ADMISSION_TIMEOUT_MS", 50) / 1000,
            pg_pool_min=_int(env, "PG_POOL_MIN", 2),
            pg_pool_max=_int(env, "PG_POOL_MAX", 10),
            events_maxlen=_int(env, "EVENTS_MAXLEN", 500),
            events_ttl_s=_int(env, "EVENTS_TTL_SECONDS", 86400),
            sse_max_seconds=_float(env, "SSE_MAX_SECONDS", 600),
            api_keys=_json(env, "API_KEYS_JSON", {}),
            otel_sample_ratio=_float(env, "OTEL_SAMPLE_RATIO", 1.0),
            otlp_endpoint=_get(env, "OTEL_EXPORTER_OTLP_ENDPOINT"),
            spans_jsonl_dir=_get(env, "SPANS_JSONL_DIR"),
            cedar_policies=_get(env, "CEDAR_POLICIES", str(LESSON29_CONFIGS / "policies.cedar")),
            cedar_schema=_get(env, "CEDAR_SCHEMA", str(LESSON29_CONFIGS / "schema.cedarschema")),
            seed_demo_data=_bool(env, "SEED_DEMO_DATA", True),
        )
        s.validate()
        return s

    @property
    def heartbeat_interval(self) -> float:
        return self.heartbeat_seconds or self.lease_seconds / 3

    def validate(self) -> None:
        if self.llm_backend not in ("litellm", "openai", "scripted"):
            raise ValueError(f"LLM_BACKEND 只能是 litellm / openai / scripted，收到 {self.llm_backend!r}")
        if self.heartbeat_interval * 2 > self.lease_seconds:
            # 连续丢一次心跳就失去租约：GC 停顿、数据库抖一下都会让任务被别人接手（重复执行）
            raise ValueError("WORKER_HEARTBEAT_SECONDS 必须 ≤ 租约的一半（建议 1/3）")
        if self.role == "worker" and self.worker_concurrency < 1:
            raise ValueError("WORKER_CONCURRENCY 至少为 1")
