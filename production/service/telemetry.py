"""可观测性接线：追踪（第 28 课的 OTelTracer / setup_tracing）、服务自己的指标、结构化日志。

- 追踪：OTLP 地址来自标准环境变量 OTEL_EXPORTER_OTLP_ENDPOINT（setup_tracing 自己读）。
  本机和端到端测试另外把 span 写成 JSONL（SPANS_JSONL_DIR），测试据此核对"API 的 send span 和 worker 的
  process span 在同一条 trace 里"。每个进程写自己的文件，避免多进程同时追加同一个文件。
- 指标：Agent 级指标来自 PrometheusHook；这里补上服务级指标（HTTP、429、SSE 连接数、worker 任务事件）。
  设置了 PROMETHEUS_MULTIPROC_DIR 时（本机多进程）走 prometheus_client 的多进程模式：计数器写在共享目录的
  mmap 文件里，**进程被 kill -9 之后计数也不会丢**（本课实测），任何一个进程的 /metrics 都能汇总全部进程。
  K8s 里每个 Pod 一个进程，不设这个变量，由 Prometheus 分别抓取、用 rate() 处理重启归零。
- 日志：一行一个 JSON，带 trace_id，方便和 trace 串起来（第 10、28 课的"三支柱"）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# ------------------------------------------------------------------ 服务级指标（进程级单例）

HTTP_REQUESTS = Counter("itdesk_http_requests", "HTTP 请求数", ["route", "code"])
HTTP_LATENCY = Histogram(
    "itdesk_http_request_duration_seconds", "非流式请求耗时", ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
RATE_LIMITED = Counter("itdesk_rate_limited", "被拒绝的请求（429）", ["layer"])  # layer: api_bucket / stream_bulkhead
RATE_LIMIT_ERRORS = Counter("itdesk_rate_limiter_errors", "限流器本身出错（Redis 不可用）时按 fail-open 放行的次数")
SSE_STREAMS = Gauge("itdesk_sse_streams", "当前打开的 SSE 连接", ["kind"], multiprocess_mode="livesum")
JOB_EVENTS = Counter("itdesk_worker_job_events", "worker 的任务事件", ["event"])
JOB_RELEASED = Counter("itdesk_worker_jobs_released", "停机时被取消并立即归还的任务")
SWALLOWED_CANCELS = Counter("itdesk_swallowed_cancellations", "被依赖库吞掉、由 agentkit.aio 在步骤边界补抛的取消次数")
TTFT = Histogram("itdesk_stream_ttft_seconds", "交互式流的首个文本片段延迟", buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16))


def metrics_registry():
    """/metrics 用的 registry：多进程模式下按官方做法新建一个并挂上 MultiProcessCollector。"""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry(support_collectors_without_names=True)
        multiprocess.MultiProcessCollector(registry)
        return registry
    return REGISTRY


def render_metrics() -> bytes:
    return generate_latest(metrics_registry())


# ------------------------------------------------------------------ 追踪

class JsonlSpanExporter:
    """把结束的 span 追加写进 JSONL（测试 / 本机排查用）。生产走 OTLP → Collector（第 28 课）。"""

    def __init__(self, directory: str, service: str):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = Path(directory) / f"spans-{service}-{os.getpid()}.jsonl"
        self._lock = threading.Lock()

    def export(self, spans) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        lines = []
        for s in spans:
            ctx = s.get_span_context()
            attrs = dict(s.attributes or {})
            lines.append(json.dumps({
                "name": s.name,
                "trace_id": format(ctx.trace_id, "032x"),
                "span_id": format(ctx.span_id, "016x"),
                "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                "kind": s.kind.name,
                "service": s.resource.attributes.get("service.name"),
                "pid": os.getpid(),
                "start": s.start_time, "end": s.end_time,
                "attrs": {k: v for k, v in attrs.items() if isinstance(v, (str, int, float, bool))},
            }, ensure_ascii=False))
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def setup(settings) -> Any:
    """返回 TracerProvider。OTLP 导出（若配置了 OTEL_EXPORTER_OTLP_ENDPOINT）+ 可选的 JSONL。"""
    from agentkit.contrib.otel import setup_tracing

    exporter = JsonlSpanExporter(settings.spans_jsonl_dir, settings.service_name) if settings.spans_jsonl_dir else None
    return setup_tracing(
        settings.service_name,
        sample_ratio=settings.otel_sample_ratio,
        exporter=exporter,
        resource_attributes={
            "service.version": os.environ.get("SERVICE_VERSION", "dev"),
            "deployment.environment.name": settings.environment,
            "service.instance.id": settings.instance_id,
        },
    )


# ------------------------------------------------------------------ 日志

def log(event: str, **fields: Any) -> None:
    """一行一个 JSON 写到 stdout（容器里由日志采集器收走）。带上当前 trace_id，日志和 trace 就能互相跳转。"""
    try:
        from agentkit.contrib.otel import current_trace_id

        trace_id = current_trace_id()
    except Exception:  # noqa: BLE001
        trace_id = None
    record = {"ts": round(time.time(), 3), "event": event, "pid": os.getpid(), **fields}
    if trace_id:
        record["trace_id"] = trace_id
    sys.stdout.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
