"""agentkit.contrib.otel —— 追踪接 OpenTelemetry，指标接 Prometheus（第 28 课）。

第 10 课的 `agentkit.Tracer` 把一次运行记成 Span 树、导出 JSONL、用 viewer 看。它讲清了原理，
但只活在一个进程里：没有采样、没有跨进程传播、没有标准协议，指标也要事后从 JSONL 里算。
这个模块把同一套埋点接到行业标准上，**agentkit 的接口一行不改**：

    OTelTracer          继承 agentkit.Tracer 做"双写"：agentkit 自己的 Span 树照常生成（render_tree、
                        viewer、RunResult.trace 都不受影响），同时实时创建对应的 OTel span，
                        属性映射到 OpenTelemetry GenAI 语义约定（invoke_agent / chat / execute_tool）。
                        它同时是一个可选 Hook：放进 hooks 可以补上核心 span 里没有的
                        gen_ai.tool.call.id / gen_ai.conversation.id / 停止原因，以及（显式开启时的）消息内容。
                        并发的运行、同一轮里并行执行的工具（async 工具各在自己的 task 里，同步工具在线程池里）
                        之下，父子关系均经测试验证。
    setup_tracing       一行配好生产用的 TracerProvider：资源、父级优先的比例采样、OTLP/HTTP 批量导出。
    inject_context / extract_context / continue_trace
                        W3C traceparent 跨队列、跨进程传播：生产者把它放进任务 payload，
                        worker 取出后接着同一条 trace 继续（第 30 课的 worker 直接用）。
    PrometheusHook      一个 agentkit Hook：运行数、耗时直方图、token、成本、工具调用、待审批数、在途运行数。
                        回调都是普通方法、只做内存计数（纯计算的 Hook 不必写成 async；但普通方法在事件循环线程里
                        执行，里面绝不能做阻塞 IO）。
    start_metrics_server  暴露 /metrics；多进程（gunicorn / 多 worker）时自动切换到 prometheus_client 的多进程模式。

语义约定版本：GenAI 约定仍是 **Development** 状态，已迁到独立仓库
https://github.com/open-telemetry/semantic-conventions-genai 。本模块按该仓库 main 分支
（2026-09-24，commit e57c543）核实属性名；约定还会变，升级 SDK 时请对照 GENAI_SEMCONV_REF 复查。

依赖：pip install -e ".[otel]"（opentelemetry-sdk、opentelemetry-exporter-otlp-proto-http、prometheus-client）。
映射函数 to_genai_attributes 是纯 Python，不需要这些包；其余功能在调用时才导入。
"""

from __future__ import annotations

import json
import os
import threading
import time
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping

from ..guardrails import redact_pii
from ..hooks import Hook
from ..pricing import estimate_cost
from ..tracing import Span, Tracer
from . import require

if TYPE_CHECKING:  # 只用于类型标注，运行时不导入可选依赖
    from ..state import RunState
    from ..tools import Tool, ToolResult
    from ..types import LLMResponse, Message, ToolCall

GENAI_SEMCONV_REF = "open-telemetry/semantic-conventions-genai@main e57c543 (2026-09-24)"
CAPTURE_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

__all__ = [
    "GENAI_SEMCONV_REF",
    "CAPTURE_CONTENT_ENV",
    "to_genai_attributes",
    "genai_mapping",
    "normalize_stop_reason",
    "OTelTracer",
    "setup_tracing",
    "inject_context",
    "extract_context",
    "continue_trace",
    "current_trace_id",
    "LabelGuard",
    "PrometheusHook",
    "start_metrics_server",
    "mark_process_dead",
]

# =============================================================================================
# 1. agentkit 属性 → OTel GenAI 语义约定（纯函数，不依赖 opentelemetry）
# =============================================================================================

_ROOT_SPANS = ("agent.run", "agent.resume")
# 内容类属性：GenAI 约定里是 Opt-In（默认不采集）。agentkit 核心已经做过脱敏和截断，但仍然是用户数据。
_CONTENT_RENAMES = {"tool.arguments": "gen_ai.tool.call.arguments", "tool.result_preview": "gen_ai.tool.call.result"}
_GENAI_CONTENT = frozenset(
    {"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"}
)
_RENAMES = {
    "agent.name": "gen_ai.agent.name",
    "tool.name": "gen_ai.tool.name",
    # agentkit 早期用的名字；当前约定是 gen_ai.usage.reasoning.output_tokens
    "gen_ai.usage.reasoning_tokens": "gen_ai.usage.reasoning.output_tokens",
}
# agentkit 核心产生的非标准属性：加 agentkit. 命名空间，避免和别人的属性撞名
_AGENTKIT_KEYS = frozenset({"run_id", "step", "messages", "result", "interrupted", "tenant.id"})
_NOT_A_FAILURE = frozenset({"denied"})  # 权限策略拒绝是"系统按设计工作"，不是故障
# 运行级失败：模型不可用（failed）、步数耗尽（max_steps）、超时（stop_reason=timeout）。
# 不算失败：paused（等审批）、cancelled（客户端主动断开）、stopped 里的预算中止 / 输入拦截 / 限流。
# 限流（rate_limited）在 SLO 里算坏事件，但不标 ERROR：过载时它成批出现，标成 ERROR 会让尾部采样把它们全留下，
# 在系统最忙的时候反而放大追踪流量。
_RUN_FAILURES = frozenset({"failed", "max_steps"})
_REASON_FAILURES = frozenset({"timeout"})


def normalize_stop_reason(reason: Any) -> str:
    """stop_reason 可能带原始错误信息（如 "llm_error: 503 Service Unavailable ..."），当标签或属性前只保留类别。"""
    return str(reason or "none").split(":", 1)[0].strip() or "none"


def genai_mapping(
    name: str,
    attrs: Mapping[str, Any],
    *,
    provider_name: str | None = "openai",
    capture_content: bool = False,
    pseudonymize: Callable[[str], str] | None = None,
) -> tuple[str, dict, str]:
    """把一个 agentkit span 翻译成 (OTel span 名, GenAI 属性, span kind)。kind 取 internal / client。

    规则（顺序即优先级）：
      1. 值为 None 的属性丢掉；`error`（异常原文）不当属性，交给 span status。
      2. 内容类属性（tool.arguments、tool.result_preview、gen_ai.input/output.messages 等）
         只有 capture_content=True 才保留 —— GenAI 约定规定它们是 Opt-In。
      3. user.id 永远不原样导出：给了 pseudonymize（如 HMAC）就写成 user.hash，否则丢掉。
      4. finish_reason → gen_ai.response.finish_reasons（字符串数组）；agent.name / tool.name 改名为 gen_ai.*。
      5. 已经是 gen_ai.* / error.* 的原样保留；agentkit 核心的其余属性加 `agentkit.` 前缀；
         用户自己加的属性（如 app.feature）原样保留。
      6. 按 span 名补上 gen_ai.operation.name、span 名和错误类型：
            agent.run / agent.resume → invoke_agent {agent}   （failed / max_steps / 超时 → error.type；
                                       paused、cancelled、限流不算失败；
                                       resume 上的累计 token 改名为 agentkit.run.cumulative_*_tokens）
            llm.chat                 → chat {model}           （kind=client，补 gen_ai.provider.name）
            tool.<name>              → execute_tool {name}     （工具失败且不是 denied → error.type）
         其余 span 名保持原样，只做属性翻译。
    """
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None or key == "error":
            continue
        if key in _CONTENT_RENAMES or key in _GENAI_CONTENT:
            if capture_content:
                out[_CONTENT_RENAMES.get(key, key)] = value
            continue
        if key == "user.id":
            if pseudonymize is not None:
                out["user.hash"] = pseudonymize(str(value))
            continue
        if key == "finish_reason":
            out["gen_ai.response.finish_reasons"] = [str(value)]
        elif key in _RENAMES:
            out[_RENAMES[key]] = value
        elif key.startswith(("gen_ai.", "error.", "agentkit.")):
            out[key] = value
        elif key in _AGENTKIT_KEYS or key.startswith(("agent.", "tool.")):
            out[f"agentkit.{key}"] = value
        else:
            out[key] = value

    if name in _ROOT_SPANS:
        op, kind = "invoke_agent", "internal"
        agent = out.get("gen_ai.agent.name")
        otel_name = f"invoke_agent {agent}" if agent else "invoke_agent"
        if name == "agent.resume":
            out["agentkit.resumed"] = True
            # agentkit 在 resume 的根 span 上写的是整个 run 的**累计** token（含暂停前那一段），
            # 不是这次调用的用量。照搬成 gen_ai.usage.* 会被按 span 求和的后端重复计数，所以改名。
            for direction in ("input", "output"):
                if f"gen_ai.usage.{direction}_tokens" in out:
                    out[f"agentkit.run.cumulative_{direction}_tokens"] = out.pop(f"gen_ai.usage.{direction}_tokens")
        if attrs.get("agent.status") in _RUN_FAILURES:
            out.setdefault("error.type", str(attrs["agent.status"]))
        elif attrs.get("agent.stop_reason") in _REASON_FAILURES:
            out.setdefault("error.type", str(attrs["agent.stop_reason"]))
    elif name == "llm.chat":
        op, kind = "chat", "client"
        model = out.get("gen_ai.request.model")
        if model in (None, "", "?"):
            out.pop("gen_ai.request.model", None)
            otel_name = "chat"
        else:
            otel_name = f"chat {model}"
        if provider_name:
            out["gen_ai.provider.name"] = provider_name
    elif name.startswith("tool."):
        op, kind = "execute_tool", "internal"
        tool_name = out.setdefault("gen_ai.tool.name", name[len("tool.") :])
        otel_name = f"execute_tool {tool_name}"
        out.setdefault("gen_ai.tool.type", "function")
        if attrs.get("tool.ok") is False and attrs.get("tool.error_type") not in _NOT_A_FAILURE:
            out.setdefault("error.type", str(attrs.get("tool.error_type") or "_OTHER"))
    else:
        return name, out, "internal"
    out["gen_ai.operation.name"] = op
    return otel_name, out, kind


def to_genai_attributes(agentkit_span_name: str, attrs: Mapping[str, Any], **options) -> tuple[str, dict]:
    """genai_mapping 的简化版：只返回 (OTel span 名, 属性)。options 同 genai_mapping。"""
    otel_name, out, _ = genai_mapping(agentkit_span_name, attrs, **options)
    return otel_name, out


def _otel_value(value: Any) -> Any:
    """OTel 属性值只能是 str/bool/int/float 或它们的同类数组；其余一律转成 JSON 字符串（后端兼容性最好）。"""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and value:
        for kind in (str, bool, int, float):
            if all(isinstance(v, kind) and (kind is bool or not isinstance(v, bool)) for v in value):
                return list(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _capture_from_env() -> bool:
    """与 OTel Python GenAI 工具包一致：NO_CONTENT（默认）/ SPAN_ONLY / EVENT_ONLY / SPAN_AND_EVENT。
    本模块只写 span，所以 SPAN_ONLY、SPAN_AND_EVENT（以及旧式的 true）视为开启。"""
    return os.environ.get(CAPTURE_CONTENT_ENV, "").strip().upper() in {"SPAN_ONLY", "SPAN_AND_EVENT", "TRUE"}


# =============================================================================================
# 2. OTelTracer：agentkit Span 树 + OTel span 双写
# =============================================================================================


class OTelTracer(Tracer, Hook):
    """agentkit Tracer 的 OpenTelemetry 版。用法与 Tracer 完全相同：Agent(..., tracer=OTelTracer(provider))。

    - 双写：agentkit Span 树照常生成（并照常调用 exporter，如 jsonl_exporter）；同时实时创建 OTel span
      并设为"当前 span"。所以工具里发出的 HTTP 请求、数据库调用，只要有 OTel 自动埋点，就会挂在
      execute_tool span 下面；在工具里调用 inject_context() 也能拿到正确的父 span。
    - ID 对齐：agentkit Span 的 trace_id / span_id 改用 OTel 的（32 / 16 位十六进制），
      RunResult.trace.trace_id 就是能在 Jaeger / Tempo / Langfuse 里直接搜索的那个 ID。
    - 状态：agentkit 的 error → StatusCode.ERROR（异常信息先脱敏）；工具失败（denied 除外）、
      运行 failed / max_steps 也标 ERROR 并写 error.type，尾部采样的"错误全留"策略靠它；
      PauseRun / StopRun 不是故障，只记 agentkit.interrupted，状态保持 UNSET。
    - 内容：默认不记录 prompt、回复、工具参数和结果（GenAI 约定的 Opt-In）。capture_content=True
      或环境变量 OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY 时才记录，而且仍然先经过
      redact_pii 脱敏、截断到 max_content_chars。打开之前请读第 09、10、28 课关于隐私的部分：
      追踪后端的访问面通常比业务库大得多，正则脱敏也抓不住姓名、地址这类自由文本。
    - 作为 Hook（可选，建议放在 hooks 列表第一个）：补上 gen_ai.tool.call.id、gen_ai.conversation.id
      （取自 metadata["conversation_id"]），开启内容采集时记录用户输入和最终回答。
    - 线程与 asyncio：agentkit 的 span 栈和 OTel 的当前 span 都存在 contextvars 里，并且在同一个 with 块里
      一起 set、一起 reset。每个线程、每个 asyncio task 都有自己的 context 副本（task 在创建时复制父 context），
      所以多个并发运行交错执行、同一轮里并行执行的工具 task（以及复制了 context 的同步工具线程），父子关系都各归各的；
      tests/contrib/test_otel.py 用 50 个并发运行（每个带 3 个并行工具）验证了这一点。唯一的要求是：span 在哪个 task
      里进入，就在哪个 task 里退出（普通的 with / async 函数写法天然满足）。
    """

    def __init__(
        self,
        tracer_provider: Any = None,
        name: str = "agentkit",
        *,
        provider_name: str | None = "openai",
        capture_content: bool | None = None,
        pseudonymize: Callable[[str], str] | None = None,
        max_content_chars: int = 2000,
        exporter: Callable[[Span], None] | None = None,
        keep_last: int = 1000,
    ):
        Tracer.__init__(self, exporter=exporter, keep_last=keep_last)
        self._trace = require("opentelemetry.trace", "otel")
        self._context = require("opentelemetry.context", "otel")
        from agentkit import __version__

        self.tracer_provider = tracer_provider or self._trace.get_tracer_provider()
        self._otel = self.tracer_provider.get_tracer(name, __version__)
        self.provider_name = provider_name  # OpenAI 兼容接口（含 cliproxyapi 这类网关）按约定写 "openai"
        self.capture_content = _capture_from_env() if capture_content is None else capture_content
        self.pseudonymize = pseudonymize
        self.max_content_chars = max_content_chars
        kinds = self._trace.SpanKind
        self._kinds = {
            "internal": kinds.INTERNAL,
            "client": kinds.CLIENT,
            "server": kinds.SERVER,
            "producer": kinds.PRODUCER,
            "consumer": kinds.CONSUMER,
        }

    def _map(self, name: str, attrs: Mapping[str, Any]) -> tuple[str, dict, str]:
        return genai_mapping(
            name,
            attrs,
            provider_name=self.provider_name,
            capture_content=self.capture_content,
            pseudonymize=self.pseudonymize,
        )

    @contextmanager
    def span(self, name: str, **attrs) -> Iterator[Span]:
        # 允许调用方用 otel.kind 指定 span kind（如队列生产者 producer / 消费者 consumer），它不进 agentkit 属性
        kind_hint = attrs.pop("otel.kind", None)
        otel_name, start_attrs, kind = self._map(name, attrs)
        # 采样相关的属性（operation.name、agent.name、tool.name）在创建时就给出：约定要求如此，采样器才看得到
        otel_span = self._otel.start_span(
            otel_name,
            kind=self._kinds.get(kind_hint or kind, self._kinds["internal"]),
            attributes={k: _otel_value(v) for k, v in start_attrs.items()},
            record_exception=False,
            set_status_on_exception=False,
        )
        token = self._context.attach(self._trace.set_span_in_context(otel_span))
        agent_span: Span | None = None
        error: BaseException | None = None
        try:
            with Tracer.span(self, name, **attrs) as agent_span:
                ctx = otel_span.get_span_context()
                if ctx.is_valid:  # 没装 SDK（NoOp）时 ID 无效，保留 agentkit 自己的 ID
                    agent_span.trace_id = format(ctx.trace_id, "032x")
                    agent_span.span_id = format(ctx.span_id, "016x")
                yield agent_span
        except BaseException as e:
            error = e
            raise
        finally:
            try:
                if agent_span is not None:
                    self._finish(otel_span, agent_span, error)
            finally:
                self._context.detach(token)
                otel_span.end()

    def _finish(self, otel_span: Any, s: Span, error: BaseException | None) -> None:
        from opentelemetry.trace import Status, StatusCode

        _, attrs, _ = self._map(s.name, s.attrs)
        for key, value in attrs.items():
            otel_span.set_attribute(key, _otel_value(value))
        if error is not None and not isinstance(error, Exception):
            # asyncio.CancelledError / KeyboardInterrupt 是 BaseException：agentkit 的 Tracer 不捕获它们，
            # 这里补一个标记，方便区分"被取消 / 超时"和"正常结束"（状态保持 UNSET，由调用方决定算不算失败）
            otel_span.set_attribute("agentkit.interrupted", type(error).__name__)
        if s.status == "error":
            desc = redact_pii(str(s.attrs.get("error", "")))[:300]
            otel_span.set_attribute("error.type", type(error).__name__ if error is not None else "_OTHER")
            otel_span.set_status(Status(StatusCode.ERROR, desc))
            if error is not None:  # 异常事件的 message 也用脱敏后的版本（异常原文里常有用户输入、SQL、内网地址）
                otel_span.record_exception(error, attributes={"exception.message": desc})
        elif "error.type" in attrs:
            # 约定：不要把 error.type 再抄一遍到 status description 里
            otel_span.set_status(Status(StatusCode.ERROR))

    def force_flush(self, timeout_millis: int = 5000) -> bool:
        """把 BatchSpanProcessor 里排队的 span 立即导出。短命进程（脚本、cron、Lambda）退出前一定要调用。"""
        flush = getattr(self.tracer_provider, "force_flush", None)
        return bool(flush(timeout_millis)) if flush else True

    # ------------------------------------------------------------------ 作为 Hook 的增强（可选）

    def current_span(self) -> Span | None:
        """当前 agentkit span（本线程 / 本 asyncio task 的 span 栈顶）。它与 OTel 的当前 span 始终是同一个：
        两者都存在 contextvars 里，并且在同一个 with 块里一起设置、一起还原。"""
        stack = self._stack.get()
        return stack[-1] if stack else None

    _current = current_span

    def _clip(self, text: str | None) -> str:
        text = redact_pii(text or "")  # 先脱敏再截断（第 10 课：先截断会把号码切成半截，正则就匹配不到了）
        return text if len(text) <= self.max_content_chars else text[: self.max_content_chars] + "…[truncated]"

    def _annotate_root(self, state: "RunState") -> None:
        s = self._current()
        conversation = state.metadata.get("conversation_id")
        if s is not None and s.name in _ROOT_SPANS and conversation:
            # 约定：只在确实有会话 ID 时填写，不能拿 run_id / trace_id 凑数
            s.attrs.setdefault("gen_ai.conversation.id", str(conversation))

    def on_run_start(self, state: "RunState", user_input: str) -> str | None:
        self._annotate_root(state)
        s = self._current()
        if self.capture_content and s is not None and s.name in _ROOT_SPANS:
            s.attrs["gen_ai.input.messages"] = json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": self._clip(user_input)}]}], ensure_ascii=False
            )
        return None

    def before_llm(self, state: "RunState", messages: list["Message"]) -> None:
        self._annotate_root(state)  # resume 不经过 on_run_start，在这里补
        return None

    def _tag_call(self, call: "ToolCall") -> None:
        s = self._current()
        if s is not None and s.name.startswith("tool."):
            s.attrs["gen_ai.tool.call.id"] = call.id

    def before_tool(self, state: "RunState", call: "ToolCall", tool: "Tool | None") -> str | None:
        self._tag_call(call)
        return None

    def after_tool(self, state: "RunState", call: "ToolCall", result: "ToolResult") -> "ToolResult | None":
        self._tag_call(call)  # 前面的 hook 拒绝了调用时 before_tool 不会轮到我们，这里兜底
        return None

    def on_run_end(self, state: "RunState") -> None:
        # on_run_end 在根 span 里执行：把停止原因（归一化后）记到根 span 上，映射时据此判断"超时算失败"
        s = self._current()
        if s is not None and s.name in _ROOT_SPANS:
            s.attrs["agent.stop_reason"] = normalize_stop_reason(state.stop_reason)
        return None

    def on_final(self, state: "RunState", output: str) -> str | None:
        s = self._current()
        if self.capture_content and s is not None and s.name in _ROOT_SPANS:
            s.attrs["gen_ai.output.messages"] = json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": self._clip(output)}], "finish_reason": "stop"}],
                ensure_ascii=False,
            )
        return None


# =============================================================================================
# 3. setup_tracing：生产用 TracerProvider
# =============================================================================================


def _traces_url(endpoint: str) -> str:
    """OTLPSpanExporter(endpoint=...) 要的是完整 URL（含 /v1/traces）；而环境变量 OTEL_EXPORTER_OTLP_ENDPOINT
    是基础地址，SDK 会自己补路径。两种写法都接受，统一补齐。"""
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/v1/traces") else endpoint + "/v1/traces"


def setup_tracing(
    service_name: str,
    otlp_endpoint: str | None = None,
    sample_ratio: float = 1.0,
    console: bool = False,
    *,
    exporter: Any = None,
    resource_attributes: Mapping[str, Any] | None = None,
    set_global: bool = True,
) -> Any:
    """一行配置好生产用的 TracerProvider 并返回它。

    - 资源：service.name（代码参数优先于 OTEL_SERVICE_NAME），另合并 OTEL_RESOURCE_ATTRIBUTES 与 resource_attributes
      （建议放 service.version、deployment.environment.name）。
    - 采样：ParentBased(TraceIdRatioBased(sample_ratio))。有上游父 span 时跟随上游的决定 —— 这样队列 worker
      接着生产者的 trace 继续时，不会出现"生产者记了、worker 没记"的半截 trace。这是头部采样；
      "错误和慢请求全留"要在 Collector 做尾部采样（见 configs/otel-collector.yaml）。
    - 导出：给了 otlp_endpoint（如 http://localhost:4318，会自动补 /v1/traces）或设置了环境变量
      OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT 时，用 OTLP/HTTP + BatchSpanProcessor 异步批量导出，
      认证头走 OTEL_EXPORTER_OTLP_HEADERS。导出失败只会丢 span，不会影响 Agent 运行。
    - console=True 额外打印到终端；exporter 用于测试（如 InMemorySpanExporter，同步导出）。
    - set_global：设为全局 provider（一个进程只能设一次；测试里传 False）。
    """
    if not 0.0 <= sample_ratio <= 1.0:
        raise ValueError(f"sample_ratio 必须在 [0, 1] 之间，收到 {sample_ratio}")
    trace = require("opentelemetry.trace", "otel")
    sdk = require("opentelemetry.sdk.trace", "otel")
    export = require("opentelemetry.sdk.trace.export", "otel")
    sampling = require("opentelemetry.sdk.trace.sampling", "otel")
    resources = require("opentelemetry.sdk.resources", "otel")

    resource = resources.Resource.create({**dict(resource_attributes or {}), "service.name": service_name})
    provider = sdk.TracerProvider(
        resource=resource, sampler=sampling.ParentBased(sampling.TraceIdRatioBased(sample_ratio))
    )
    env_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint or env_endpoint:
        otlp = require("opentelemetry.exporter.otlp.proto.http.trace_exporter", "otel")
        span_exporter = otlp.OTLPSpanExporter(endpoint=_traces_url(otlp_endpoint)) if otlp_endpoint else otlp.OTLPSpanExporter()
        provider.add_span_processor(export.BatchSpanProcessor(span_exporter))
    if console:
        provider.add_span_processor(export.SimpleSpanProcessor(export.ConsoleSpanExporter()))
    if exporter is not None:
        provider.add_span_processor(export.SimpleSpanProcessor(exporter))
    if set_global:
        trace.set_tracer_provider(provider)
    return provider


# =============================================================================================
# 4. W3C Trace Context 传播：跨队列、跨进程
# =============================================================================================


def inject_context(carrier: dict | None = None, context: Any = None) -> dict:
    """把当前 trace 上下文写进 carrier（通常是任务 payload 里的一个 dict），返回 carrier。

    写入的是 W3C `traceparent`（以及 `tracestate`、`baggage`，如果有）。例：
        {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    注意 baggage 会原样传给所有下游（包括第三方服务），不要往里放用户 ID 之类的敏感信息。
    """
    propagate = require("opentelemetry.propagate", "otel")
    carrier = {} if carrier is None else carrier
    propagate.inject(carrier, context=context)
    return carrier


def extract_context(carrier: Mapping[str, str] | None) -> Any:
    """从 carrier 里恢复 trace 上下文（opentelemetry.context.Context）。格式不对时返回空上下文（开启新 trace），不抛异常。"""
    propagate = require("opentelemetry.propagate", "otel")
    return propagate.extract(dict(carrier or {}))


class continue_trace:  # noqa: N801 —— 用法上是一个上下文管理器函数，保持小写
    """在 with / async with 块里"接着"carrier 所代表的 trace 继续：块内新建的 span 以生产者的 span 为父。

    asyncio worker（每个任务一个 task，并发处理互不串线）：
        async def handle(job):
            async with continue_trace(job["trace"]):   # 写成普通的 with 也可以：进入 / 退出都只是内存操作
                await agent.run(job["input"])       # invoke_agent span 与生产者在同一条 trace 里

        async def worker(q: asyncio.Queue):
            while True:
                job = await q.get()
                asyncio.create_task(handle(job))   # task 创建时复制 context，退出块时只还原本 task 的 context

    进入时把提取出的上下文 attach 到**当前** context（当前线程 / 当前 task），退出时 detach。所以进入和退出
    必须发生在同一个 task 里 —— 不要在一个 task 里进入、把对象交给另一个 task 去退出。

    这是消息约定（messaging semconv）允许的"单条消息把创建上下文当父级"的做法；批量消费、或者任务在队列里
    等了很久（几小时）时，更推荐新开 trace 并用 span link 关联，见第 28 课问题 6。
    """

    def __init__(self, carrier: Mapping[str, str] | None):
        self._carrier = carrier
        self._token: Any = None
        self.context: Any = None

    def __enter__(self) -> Any:
        otel_context = require("opentelemetry.context", "otel")
        if self._token is not None:
            raise RuntimeError("continue_trace 对象不能重入；每次使用新建一个")
        self.context = extract_context(self._carrier)
        self._token = otel_context.attach(self.context)
        return self.context

    def __exit__(self, *exc: Any) -> None:
        from opentelemetry import context as otel_context

        token, self._token = self._token, None
        otel_context.detach(token)

    async def __aenter__(self) -> Any:  # attach / detach 都是内存操作，不阻塞事件循环
        return self.__enter__()

    async def __aexit__(self, *exc: Any) -> None:
        self.__exit__(*exc)


def current_trace_id() -> str | None:
    """当前 OTel trace_id（32 位十六进制）；没有活动 span 时返回 None。写日志时带上它，三支柱才串得起来。"""
    trace = require("opentelemetry.trace", "otel")
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


# =============================================================================================
# 5. Prometheus 指标
# =============================================================================================


class LabelGuard:
    """给一个标签设取值上限：前 max_values 个不同取值原样通过，之后的一律归到 "__other__"。

    每个不同的标签值都会生成一条新的时间序列；租户数、工具名看似有限，但一旦有人拿随机字符串调用
    （或模型编了一个不存在的工具名），序列数就会失控。allowed 给定时只放行名单内的取值
    （多进程部署时各进程的"先到先得"名单可能不同，用 allowed 才是确定的）。
    """

    OVERFLOW = "__other__"

    def __init__(self, max_values: int = 50, allowed: Iterable[str] | None = None):
        self.max_values = max_values
        self.allowed = frozenset(allowed) if allowed is not None else None
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, value: Any) -> str:
        v = "unknown" if value in (None, "") else str(value)
        if self.allowed is not None:
            return v if v in self.allowed else self.OVERFLOW
        with self._lock:
            if v in self._seen:
                return v
            if len(self._seen) < self.max_values:
                self._seen.add(v)
                return v
        return self.OVERFLOW


# 按 SLO 阈值挑的桶：Prometheus 只能精确回答"≤ 某个桶边界的比例"，所以 SLO 阈值（这里 30s）必须是桶边界。
# OTel 约定给 gen_ai.invoke_agent.duration 建议的桶是 0.1 × 2^n（0.1 … 409.6），不含 30，这里没有照搬。
DEFAULT_RUN_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0)

# 指标对象必须是进程级单例：同一个 registry 里重复注册同名指标会抛 "Duplicated timeseries"。
# 常见事故：每个请求 new 一个 Agent 和一个 Hook。这里按 (registry, namespace) 缓存，重复创建 Hook 也安全。
_METRICS: "weakref.WeakKeyDictionary[Any, dict[str, dict]]" = weakref.WeakKeyDictionary()
_METRICS_LOCK = threading.Lock()


def _metrics_for(pc: Any, registry: Any, namespace: str, tenant_label: bool, buckets: tuple, tenant_guard: "LabelGuard") -> dict:
    with _METRICS_LOCK:
        per_registry = _METRICS.setdefault(registry, {})
        existing = per_registry.get(namespace)
        if existing is not None:
            if existing["tenant_label"] != tenant_label:
                raise ValueError(f"namespace={namespace!r} 的指标已按 tenant_label={existing['tenant_label']} 注册，不能混用")
            return existing
        t = ["tenant"] if tenant_label else []
        kw = {"namespace": namespace, "registry": registry}
        m = {
            "tenant_label": tenant_label,
            # 租户上限也必须是进程级的：跟着指标一起缓存，否则"每个请求 new 一个 Hook"会让上限形同虚设
            "tenant_guard": tenant_guard,
            "reason_guard": LabelGuard(20),  # stop_reason 是有限枚举，但仍设上限兜底（自定义 StopRun 原因可能很多）
            "runs": pc.Counter(
                "runs", "Agent 运行段结束次数（run 或 resume 各算一段），按最终状态和停止原因", ["status", "reason", *t], **kw
            ),
            "duration": pc.Histogram(
                "run_duration_seconds", "Agent 运行段耗时（秒，不含等待审批的时间）", ["status"], buckets=buckets, **kw
            ),
            "tokens": pc.Counter("llm_tokens", "模型 token 用量", ["direction", *t], **kw),
            "cost": pc.Counter("llm_cost_usd", "模型成本估算（美元，单价见 agentkit/pricing.py）", t, **kw),
            # 不加 tenant：工具 × 错误类型 × 租户是乘法关系，序列数会爆
            "tools": pc.Counter("tool_calls", "工具调用次数（error_type=none 表示成功）", ["tool", "error_type"], **kw),
            # livesum：多进程时把各存活进程的值相加（每个进程只增减自己暂停的运行）
            "approvals": pc.Gauge("approvals_pending", "等待人工审批的运行数", multiprocess_mode="livesum", **kw),
            "in_flight": pc.Gauge("runs_in_flight", "正在执行的运行段数（不含等待审批的）", multiprocess_mode="livesum", **kw),
            # livemostrecent：多进程时取最近一次写入的值（队列深度应由一个采样者从数据库读出后 set）
            "queue_depth": pc.Gauge("queue_depth", "队列中待处理的任务数", ["queue"], multiprocess_mode="livemostrecent", **kw),
            "queue_age": pc.Gauge(
                "queue_oldest_job_age_seconds", "队列里最老任务已等待的秒数", ["queue"], multiprocess_mode="livemostrecent", **kw
            ),
        }
        per_registry[namespace] = m
        return m


class PrometheusHook(Hook):
    """把 Agent 运行指标暴露给 Prometheus 的 Hook。放进 Agent(hooks=[...]) 即可；多个 Agent 可以共用一个实例。

    暴露的指标（namespace 默认 agent）：
        agent_runs_total{status,reason[,tenant]}       运行段结束次数。status：completed / paused / stopped / failed /
                                                       max_steps / cancelled；reason：归一化的 stop_reason（final_answer、
                                                       timeout、budget_exceeded、rate_limited、llm_error、cancelled……）
        agent_run_duration_seconds{status}             运行段耗时直方图（桶见 DEFAULT_RUN_BUCKETS）
        agent_llm_tokens_total{direction[,tenant]}     input / output
        agent_llm_cost_usd_total{[tenant]}             成本估算
        agent_tool_calls_total{tool,error_type}        模型编造的工具名一律记为 tool="__unknown__"
        agent_approvals_pending                        等待人工审批的运行数
        agent_runs_in_flight                           正在执行的运行段数（并发度；配合 worker 的并发上限看饱和度）
        agent_queue_depth{queue} / agent_queue_oldest_job_age_seconds{queue}   由 set_queue_stats 设置

    标签基数：只有租户数量有上限时才开 tenant_label（超过 max_tenants 或不在 allowed_tenants 里的归入
    "__other__"）；user_id、run_id、trace_id 绝不能当标签 —— 它们属于 trace 和日志。

    线程与 asyncio：所有回调都是普通方法，只做内存里的计数（prometheus_client 自带锁），Agent 直接调用。
    **不要**在这个 Hook（或任何写成普通方法的 Hook）里做阻塞 IO —— 它在事件循环线程里执行，
    一次 50ms 的阻塞调用会让同一进程里所有并发运行一起停 50ms。需要查数据库的指标（如待审批数、
    队列深度）放到独立的定时任务里，用 set_pending_approvals / set_queue_stats 写入。
    """

    UNKNOWN_TOOL = "__unknown__"

    def __init__(
        self,
        registry: Any = None,
        namespace: str = "agent",
        tenant_label: bool = False,
        *,
        max_tenants: int = 50,
        allowed_tenants: Iterable[str] | None = None,
        track_approvals: bool = True,
        buckets: tuple = DEFAULT_RUN_BUCKETS,
    ):
        pc = require("prometheus_client", "otel")
        self._pc = pc
        self.registry = registry if registry is not None else pc.REGISTRY
        self.tenant_label = tenant_label
        self.track_approvals = track_approvals
        self._m = _metrics_for(pc, self.registry, namespace, tenant_label, tuple(buckets), LabelGuard(max_tenants, allowed_tenants))
        self._tenant: LabelGuard = self._m["tenant_guard"]  # 同一 (registry, namespace) 以第一次创建时的上限为准
        self._pending: set[str] = set()  # 本进程里暂停、等待审批的 run_id
        self._active: set[str] = set()  # 本进程里正在执行的 run_id
        self._lock = threading.Lock()  # 线程安全；asyncio 下也只是纳秒级的临界区，不会卡住事件循环

    # ------------------------------------------------------------------ 工具函数

    def _t(self, state: "RunState") -> tuple[str, ...]:
        return (self._tenant(state.metadata.get("tenant_id")),) if self.tenant_label else ()

    def _touch(self, state: "RunState") -> None:
        """本段运行里第一次看到这个 run：在途数 +1；如果它之前在等审批（同一进程里恢复），待审批数 -1。

        resume 不经过 on_run_start，所以每个回调都调用它，用集合去重。"""
        with self._lock:
            if state.run_id not in self._active:
                self._active.add(state.run_id)
                self._m["in_flight"].inc()
            if self.track_approvals and state.run_id in self._pending:
                self._pending.discard(state.run_id)
                self._m["approvals"].dec()

    # ------------------------------------------------------------------ Hook 回调（全部是普通方法，只做内存操作）

    def on_run_start(self, state: "RunState", user_input: str) -> str | None:
        self._touch(state)
        return None

    def before_llm(self, state: "RunState", messages: list["Message"]) -> None:
        self._touch(state)
        return None

    def after_llm(self, state: "RunState", response: "LLMResponse") -> None:
        self._touch(state)
        t = self._t(state)
        u = response.usage
        if u.input_tokens:
            self._m["tokens"].labels("input", *t).inc(u.input_tokens)
        if u.output_tokens:
            self._m["tokens"].labels("output", *t).inc(u.output_tokens)
        cost = estimate_cost(u, response.model or "default")  # 与 Agent 内部的成本计算一致
        if cost > 0:
            (self._m["cost"].labels(*t) if t else self._m["cost"]).inc(cost)
        return None

    def before_tool(self, state: "RunState", call: "ToolCall", tool: "Tool | None") -> str | None:
        self._touch(state)
        return None

    def after_tool(self, state: "RunState", call: "ToolCall", result: "ToolResult") -> "ToolResult | None":
        self._touch(state)
        name = self.UNKNOWN_TOOL if result.error_type == "not_found" else call.name
        self._m["tools"].labels(name, result.error_type or "none").inc()
        return None

    def on_run_end(self, state: "RunState") -> None:
        status = state.status
        reason = self._m["reason_guard"](normalize_stop_reason(state.stop_reason))
        self._m["runs"].labels(status, reason, *self._t(state)).inc()
        self._m["duration"].labels(status).observe(max(0.0, time.time() - state.segment_started_at))
        with self._lock:
            if state.run_id in self._active:
                self._active.discard(state.run_id)
                self._m["in_flight"].dec()
            if not self.track_approvals:
                return None
            if status == "paused":
                if state.run_id not in self._pending:
                    self._pending.add(state.run_id)
                    self._m["approvals"].inc()
            elif state.run_id in self._pending:
                self._pending.discard(state.run_id)
                self._m["approvals"].dec()
        return None

    # ------------------------------------------------------------------ 由外部采样者设置的指标

    def set_pending_approvals(self, n: int) -> None:
        """直接设置待审批数（例如定期 SELECT count(*) FROM runs WHERE status='paused'）。

        多 worker 部署时暂停和恢复常发生在不同进程，进程内的增减会漂移；这时构造 Hook 时传
        track_approvals=False，并只让**一个**进程定期调用本方法（多进程模式下该指标按 livesum 聚合，
        不调用的进程贡献 0）。
        """
        self._m["approvals"].set(n)

    def set_queue_stats(self, queue: str, depth: int, oldest_age_seconds: float = 0.0) -> None:
        """设置队列积压：深度与最老任务的等待时间。告警优先看等待时间 —— 它直接对应"用户等了多久"。"""
        self._m["queue_depth"].labels(queue).set(depth)
        self._m["queue_age"].labels(queue).set(oldest_age_seconds)

    def render(self) -> str:
        """当前 registry 的 Prometheus 文本格式（/metrics 的内容），调试和 demo 用。"""
        return self._pc.generate_latest(self.registry).decode("utf-8")


def start_metrics_server(port: int, addr: str = "127.0.0.1", registry: Any = None) -> tuple[Any, Any]:
    """在后台线程启动 /metrics HTTP 服务，返回 (server, thread)；port=0 时由系统分配端口（server.server_port）。

    - 默认只监听 127.0.0.1：/metrics 里有租户名、工具名等信息，不该对外暴露。容器里让 Prometheus 抓取时
      传 addr="0.0.0.0"，并用网络策略限制访问来源。
    - 多进程模式：设置了环境变量 PROMETHEUS_MULTIPROC_DIR（必须在导入 prometheus_client 之前设置，
      每次启动前清空该目录）时，按官方做法用一个新的 CollectorRegistry + MultiProcessCollector 汇总所有
      worker 进程写下的指标文件。gunicorn 的 child_exit 钩子里调用 mark_process_dead(worker.pid)。
    """
    pc = require("prometheus_client", "otel")
    if registry is None:
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            from prometheus_client import multiprocess

            registry = pc.CollectorRegistry(support_collectors_without_names=True)
            multiprocess.MultiProcessCollector(registry)
        else:
            registry = pc.REGISTRY
    return pc.start_http_server(port, addr=addr, registry=registry)


def mark_process_dead(pid: int) -> None:
    """多进程模式下，worker 退出时清理它的 live* 指标文件（gunicorn: def child_exit(server, worker): ...）。"""
    require("prometheus_client", "otel")
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(pid)
