"""可观测性：链路追踪（Tracing）。

Agent 是非确定性的：同样的输入，今天走 3 步，明天可能走 7 步。出了问题如果只有一行
"回答错了"的日志，你根本无从排查。Tracing 把一次运行拆成嵌套的 Span 树：

    agent.run                       ← 一次完整运行（trace）
    ├─ llm.chat                     ← 每次模型调用：模型名、token、耗时、结束原因
    ├─ tool.search_kb               ← 每次工具调用：参数、成功与否、耗时
    └─ llm.chat

这里是一个零依赖的最小实现，字段命名参考 OpenTelemetry GenAI 语义约定
（gen_ai.request.model / gen_ai.usage.input_tokens ...），生产中直接换成
OpenTelemetry SDK，导出到 Jaeger / Langfuse / Phoenix / Datadog 等后端即可。
"""

from __future__ import annotations

import collections
import contextvars
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start: float
    end: float | None = None
    attrs: dict = field(default_factory=dict)
    status: str = "ok"  # ok / error
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return ((self.end or time.time()) - self.start) * 1000

    def set(self, **attrs) -> None:
        self.attrs.update(attrs)

    def walk(self) -> Iterator["Span"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "start": self.start,
            "end": self.end,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "attrs": self.attrs,
        }


class Tracer:
    """最小 Tracer。exporter 在每个根 Span 结束时被调用（写文件 / 发到后端）。"""

    def __init__(self, exporter: Callable[[Span], None] | None = None, keep_last: int = 1000):
        self._stack: contextvars.ContextVar[tuple[Span, ...]] = contextvars.ContextVar(
            f"span_stack_{id(self)}", default=()
        )
        self.exporter = exporter
        # 已完成的根 Span：内存里只保留最近 keep_last 条（方便测试和打印），长期运行的服务也不会内存泄漏
        self.traces: collections.deque[Span] = collections.deque(maxlen=keep_last)

    @contextmanager
    def span(self, name: str, **attrs) -> Iterator[Span]:
        stack = self._stack.get()
        parent = stack[-1] if stack else None
        s = Span(
            name=name,
            trace_id=parent.trace_id if parent else uuid.uuid4().hex[:16],
            span_id=uuid.uuid4().hex[:8],
            parent_id=parent.span_id if parent else None,
            start=time.time(),
            attrs=dict(attrs),
        )
        if parent:
            parent.children.append(s)
        token = self._stack.set(stack + (s,))
        try:
            yield s
        except Exception as e:
            if getattr(e, "trace_as_error", True):
                s.status = "error"
                s.attrs["error"] = f"{type(e).__name__}: {e}"
            else:  # 暂停 / 主动中止 不是故障，只做标记
                s.attrs["interrupted"] = type(e).__name__
            raise
        finally:
            s.end = time.time()
            self._stack.reset(token)
            if parent is None:
                self.traces.append(s)
                if self.exporter:
                    self.exporter(s)

    @property
    def last_trace(self) -> Span | None:
        return self.traces[-1] if self.traces else None


def jsonl_exporter(path: str | Path) -> Callable[[Span], None]:
    """把每个 Span 扁平化为一行 JSON，追加写入文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def export(root: Span) -> None:
        with path.open("a", encoding="utf-8") as f:
            for s in root.walk():
                f.write(json.dumps(s.to_dict(), ensure_ascii=False, default=str) + "\n")

    return export


def render_tree(span: Span, _prefix: str = "", _is_last: bool = True, _root: bool = True) -> str:
    """把 Span 树渲染成人类可读的文本，调试神器。"""
    a = span.attrs
    bits = [f"{span.name}", f"{span.duration_ms:.0f}ms"]
    if "gen_ai.usage.input_tokens" in a:
        bits.append(f"tokens={a['gen_ai.usage.input_tokens']}→{a.get('gen_ai.usage.output_tokens', 0)}")
    if "result" in a:
        bits.append(f"→ {a['result']}")
    if "tool.ok" in a:
        bits.append("ok" if a["tool.ok"] else f"FAIL({a.get('tool.error_type')})")
    if "agent.status" in a:
        bits.append(f"status={a['agent.status']} steps={a.get('agent.steps')} cost=${a.get('agent.cost_usd', 0):.5f}")
    if span.status == "error":
        bits.append(f"ERROR {a.get('error', '')}")
    line = "  ".join(bits)
    out = line if _root else f"{_prefix}{'└─ ' if _is_last else '├─ '}{line}"
    child_prefix = "" if _root else _prefix + ("   " if _is_last else "│  ")
    for i, c in enumerate(span.children):
        out += "\n" + render_tree(c, child_prefix, i == len(span.children) - 1, False)
    return out
