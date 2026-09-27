"""Trace 查看器：把 jsonl_exporter 导出的 span 渲染成一个自包含的 HTML 页面。

    python -m agentkit.viewer traces.jsonl -o trace.html     # 单个文件
    python -m agentkit.viewer traces/ -o trace.html          # 目录：合并其中所有 *.jsonl

在 Python 里用：

    from agentkit.viewer import load_spans, render_html
    html = render_html(load_spans("traces.jsonl"), title="第 10 课")
    html = render_html(tracer.traces)          # 也可以直接传 Span 对象（会自动展开子 span）

页面长这样：左侧是 trace 列表（每个根 span 一条），右侧是瀑布图（waterfall）——
每一行是一个 span，按父子层级缩进，条形的位置和长度与真实起止时间成比例，
一眼就能看出"时间都花在哪了""哪一步失败了""在哪里停下来等审批"。点击任意 span 查看全部属性。

设计取舍：
- **单文件、零依赖**：不引用任何 CDN / 字体，断网、内网、邮件附件都能打开；
- **安全**：span 属性里常有用户输入（可能是恶意的 `</script><script>...`）。数据以 JSON 嵌入
  `<script type="application/json">` 并转义 `< > &` 与 U+2028/2029；渲染时只用 textContent，
  从不把不可信字符串拼进 innerHTML；再加一层 CSP（只允许本页内联脚本的哈希），纵深防御。
- 生产中你会用 Jaeger / Langfuse / Phoenix 等现成后端；这个查看器帮你先建立"看 trace"的直觉。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = ["DEFAULT_TITLE", "build_traces", "load_spans", "render_html", "main"]

DEFAULT_TITLE = "Agent Trace 查看器"


# ---------------------------------------------------------------------------- 读取


def load_spans(path: str | Path) -> list[dict]:
    """读取 jsonl_exporter 写出的文件：每行一个 span 的 JSON。

    空行跳过；写了一半的坏行（比如进程在写文件时崩溃）给出警告后跳过，不影响其余数据。
    """
    path = Path(path)
    spans: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                warnings.warn(f"{path}:{lineno} 不是合法的 JSON，已跳过（{e.msg}）", stacklevel=2)
                continue
            if isinstance(obj, dict):
                spans.append(obj)
            else:
                warnings.warn(f"{path}:{lineno} 不是 span 对象，已跳过", stacklevel=2)
    return spans


# ---------------------------------------------------------------------------- 整理成 trace 树

_AGENT_STATUS = {
    "completed": ("完成", "ok"),
    "paused": ("待审批", "warn"),
    "max_steps": ("步数上限", "warn"),
    "stopped": ("已中止", "warn"),
    "failed": ("失败", "error"),
    "running": ("运行中", "neutral"),
}


def _iter_span_dicts(spans: Iterable[Any]) -> Iterator[dict]:
    for item in spans:
        if isinstance(item, dict):
            yield item
        elif hasattr(item, "walk") and hasattr(item, "to_dict"):  # agentkit.tracing.Span
            for s in item.walk():
                yield s.to_dict()


def _num(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _int(value: Any) -> int:
    f = _num(value)
    return int(f) if f is not None else 0


def _clean(obj: Any, _depth: int = 0) -> Any:
    """把任意属性值转成"严格 JSON"：NaN/Infinity、bytes、自定义对象都变成字符串。"""
    if _depth > 50:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, dict):
        return {str(k): _clean(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_clean(v, _depth + 1) for v in obj]
    return str(obj)


def _normalize(raw: dict, index: int) -> dict:
    attrs = raw.get("attrs")
    if not isinstance(attrs, dict):
        attrs = {} if attrs is None else {"value": attrs}
    start = _num(raw.get("start"), 0.0)
    end = _num(raw.get("end"))
    duration = _num(raw.get("duration_ms"))
    if end is not None and end >= start:
        duration = (end - start) * 1000
    elif duration is not None and duration >= 0:
        end = start + duration / 1000
    else:
        end, duration = start, 0.0
    parent = raw.get("parent_id")
    return {
        "name": str(raw.get("name") or "(未命名)"),
        "trace_id": str(raw.get("trace_id") or "unknown"),
        "span_id": str(raw.get("span_id") or f"span-{index}"),
        "parent_id": None if parent in (None, "") else str(parent),
        "start": start,
        "end": end,
        "duration_ms": duration,
        "status": str(raw.get("status") or "ok"),
        "attrs": _clean(attrs),
    }


def _kind(span: dict) -> str:
    head = span["name"].split(".", 1)[0].lower()
    if head in ("agent", "llm", "tool"):
        return head
    attrs = span["attrs"]
    if "gen_ai.request.model" in attrs:
        return "llm"
    if "tool.name" in attrs:
        return "tool"
    if "agent.status" in attrs:
        return "agent"
    return "other"


def _state(span: dict) -> str:
    """ok / error（红）/ warn（黄：被中断、暂停、中止）。"""
    a = span["attrs"]
    if a.get("interrupted"):
        return "warn"
    if span["status"] == "error" or a.get("tool.ok") is False:
        return "error"
    tone = _AGENT_STATUS.get(str(a.get("agent.status")), ("", "ok"))[1]  # 根 span：暂停/中止标黄，失败标红
    return tone if tone in ("warn", "error") else "ok"


def _build_trace(trace_id: str, spans: list[dict]) -> dict:
    ids = {s["span_id"] for s in spans}
    children: dict[str | None, list[dict]] = {}
    for s in spans:
        parent = s["parent_id"] if s["parent_id"] in ids and s["parent_id"] != s["span_id"] else None
        children.setdefault(parent, []).append(s)
    for group in children.values():
        group.sort(key=lambda s: (s["start"], s["end"]))

    ordered: list[dict] = []
    seen: set[str] = set()

    def visit(roots: list[dict]) -> None:  # 迭代式深度优先，避免深层嵌套时递归溢出
        stack = [(s, 0) for s in reversed(roots)]
        while stack:
            s, depth = stack.pop()
            if s["span_id"] in seen:
                continue
            seen.add(s["span_id"])
            kids = children.get(s["span_id"], [])
            ordered.append({**s, "depth": depth, "child_count": len(kids)})
            stack.extend((c, depth + 1) for c in reversed(kids))

    visit(children.get(None, []))
    leftovers = sorted((s for s in spans if s["span_id"] not in seen), key=lambda s: s["start"])
    if leftovers:  # 父子关系成环等异常数据：当作根节点展示，而不是丢掉
        visit(leftovers)

    t0 = min(s["start"] for s in ordered)
    t1 = max(s["end"] for s in ordered)
    for s in ordered:
        s["offset_ms"] = (s["start"] - t0) * 1000
        s["kind"] = _kind(s)
        s["state"] = _state(s)

    root = ordered[0]
    ra = root["attrs"]
    llm = [s for s in ordered if s["kind"] == "llm"]
    if any("gen_ai.usage.input_tokens" in s["attrs"] for s in llm):
        input_tokens = sum(_int(s["attrs"].get("gen_ai.usage.input_tokens")) for s in llm)
        output_tokens = sum(_int(s["attrs"].get("gen_ai.usage.output_tokens")) for s in llm)
    else:
        input_tokens = _int(ra.get("gen_ai.usage.input_tokens"))
        output_tokens = _int(ra.get("gen_ai.usage.output_tokens"))
    errors = sum(s["state"] == "error" for s in ordered)
    warns = sum(s["state"] == "warn" for s in ordered)

    if root["status"] == "error":
        label, tone = "异常", "error"
    elif ra.get("agent.status") is not None:
        label, tone = _AGENT_STATUS.get(str(ra["agent.status"]), (str(ra["agent.status"]), "neutral"))
    elif errors:
        label, tone = "含错误", "error"
    elif warns:
        label, tone = "中断", "warn"
    else:
        label, tone = "正常", "ok"

    run_cost = _num(ra.get("agent.cost_usd"))
    return {
        "id": trace_id,
        "name": root["name"],
        "start": t0,
        "end": t1,
        "duration_ms": (t1 - t0) * 1000,
        "status_label": label,
        "tone": tone,
        "agent_name": ra.get("agent.name"),
        "run_id": None if ra.get("run_id") is None else str(ra.get("run_id")),
        "steps": ra.get("agent.steps"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "run_cost_usd": run_cost,  # agent.cost_usd 是整个 run 的累计值（resume 时包含之前的花费）
        "cost_usd": run_cost,  # 本条 trace 的增量，见 _link_runs
        "span_count": len(ordered),
        "llm_count": len(llm),
        "tool_count": sum(s["kind"] == "tool" for s in ordered),
        "error_count": errors,
        "warn_count": warns,
        "related": [],
        "spans": ordered,
    }


def _link_runs(traces: list[dict]) -> None:
    """同一个 run_id 的多条 trace（run → 暂停 → resume）互相关联，并把累计成本拆成每段的增量。"""
    runs: dict[str, list[dict]] = {}
    for t in traces:
        if t["run_id"]:
            runs.setdefault(t["run_id"], []).append(t)
    for group in runs.values():
        prev: float | None = None
        for t in group:  # traces 已按开始时间排序
            cost = t["run_cost_usd"]
            if cost is not None and prev is not None and cost >= prev:
                t["cost_usd"] = round(cost - prev, 9)
            if cost is not None:
                prev = cost
            t["related"] = [
                {"id": o["id"], "name": o["name"], "start": o["start"], "status_label": o["status_label"],
                 "tone": o["tone"]}
                for o in group
                if o is not t
            ]


def build_traces(spans: Iterable[Any]) -> list[dict]:
    """把扁平的 span 列表整理成按开始时间排序的 trace 列表（查看器页面用的数据模型）。

    每条 trace 带汇总信息（状态、耗时、token、成本……）和按深度优先排好序的 spans，
    每个 span 附加 depth / child_count / offset_ms / kind(agent|llm|tool|other) / state(ok|error|warn)。
    """
    unique: dict[tuple[str, str], dict] = {}
    for i, raw in enumerate(_iter_span_dicts(spans)):
        s = _normalize(raw, i)
        unique[(s["trace_id"], s["span_id"])] = s  # 同一个 span 出现多次时以最后一次为准
    grouped: dict[str, list[dict]] = {}
    for s in unique.values():
        grouped.setdefault(s["trace_id"], []).append(s)
    traces = [_build_trace(tid, group) for tid, group in grouped.items()]
    traces.sort(key=lambda t: (t["start"], t["id"]))
    _link_runs(traces)
    return traces


def _totals(traces: list[dict]) -> dict:
    costs = [t["cost_usd"] for t in traces if t["cost_usd"] is not None]
    return {
        "traces": len(traces),
        "spans": sum(t["span_count"] for t in traces),
        "errors": sum(t["error_count"] for t in traces),
        "input_tokens": sum(t["input_tokens"] for t in traces),
        "output_tokens": sum(t["output_tokens"] for t in traces),
        "cost_usd": round(sum(costs), 9) if costs else None,
    }


# ---------------------------------------------------------------------------- 渲染

_SCRIPT_JSON_ESCAPES = {
    "&": "\\u0026",
    "<": "\\u003c",
    ">": "\\u003e",
    " ": "\\u2028",
    " ": "\\u2029",
}


def _json_for_script(obj: Any) -> str:
    """序列化为可以安全放进 <script type="application/json"> 的 JSON。

    `<` `>` `&` 转成 \\u003c 等转义序列：JSON.parse 解析后值完全不变，但 HTML 解析器
    再也看不到 `</script>` 或 `<!--`，数据就无法"逃出"这个标签。U+2028/2029 顺手也转掉。
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for ch, escaped in _SCRIPT_JSON_ESCAPES.items():
        text = text.replace(ch, escaped)
    return text


def _csp_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def render_html(spans: Iterable[Any], title: str = DEFAULT_TITLE, *, source: str | None = None) -> str:
    """把 span 列表（dict，或 agentkit 的 Span 对象）渲染成自包含的单文件 HTML。"""
    traces = build_traces(spans)
    payload = {
        "title": str(title),
        "source": source,
        "generated_at": time.time(),
        "totals": _totals(traces),
        "traces": traces,
    }
    csp = "; ".join(
        [
            "default-src 'none'",
            f"script-src {_csp_hash(_JS)}",
            f"style-src {_csp_hash(_CSS)}",
            "img-src data:",
            "base-uri 'none'",
            "form-action 'none'",
        ]
    )
    return "".join(
        [
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n',
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
            '<meta name="color-scheme" content="light dark">\n',
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=False)}">\n',
            '<meta name="generator" content="agentkit.viewer">\n',
            f"<title>{html.escape(str(title))}</title>\n",
            f"<style>{_CSS}</style>\n</head>\n<body>\n",
            _BODY,
            '<script type="application/json" id="trace-data">',
            _json_for_script(payload),
            "</script>\n",
            f"<script>{_JS}</script>\n</body>\n</html>\n",
        ]
    )


# ---------------------------------------------------------------------------- 命令行


def _collect_inputs(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            found = sorted(p.glob("*.jsonl"))
            if not found:
                raise FileNotFoundError(f"目录 {p} 里没有 *.jsonl 文件")
            files.extend(found)
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(f"找不到文件：{p}")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentkit.viewer",
        description="把 jsonl_exporter 导出的 span 渲染成单文件 HTML 追踪查看器（零依赖，可离线打开）。",
    )
    parser.add_argument("inputs", nargs="+", help="traces.jsonl 文件，或包含 *.jsonl 的目录（可以给多个）")
    parser.add_argument("-o", "--output", help="输出的 HTML 路径（默认：第一个输入文件同名的 .html）")
    parser.add_argument("--title", default=DEFAULT_TITLE, help=f"页面标题（默认：{DEFAULT_TITLE}）")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = parser.parse_args(argv)

    try:
        files = _collect_inputs(args.inputs)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    spans: list[dict] = []
    for f in files:
        spans.extend(load_spans(f))
    if args.output:
        out = Path(args.output)
    elif Path(args.inputs[0]).is_dir():
        out = Path(args.inputs[0]) / "traces.html"
    else:
        out = files[0].with_suffix(".html")

    source = ", ".join(f.name for f in files[:3]) + (f" 等 {len(files)} 个文件" if len(files) > 3 else "")
    page = render_html(spans, title=args.title, source=source)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    n_traces = len({str(s.get("trace_id")) for s in spans})
    print(f"已生成 {out}（{n_traces} 条 trace，{len(spans)} 个 span）")
    print(f"用浏览器打开：{out.resolve().as_uri()}")
    if args.open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return 0


# ---------------------------------------------------------------------------- 页面资源
# 下面三段是页面的 HTML 骨架、样式和脚本。脚本里所有不可信数据都经 textContent 写入 DOM。

_BODY = """<div class="app" id="app">
  <header class="topbar">
    <div class="brand">
      <svg class="logo" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <rect x="2" y="4" width="11" height="3.2" rx="1.6" fill="currentColor"/>
        <rect x="6" y="10.4" width="12" height="3.2" rx="1.6" fill="currentColor" opacity=".72"/>
        <rect x="11" y="16.8" width="11" height="3.2" rx="1.6" fill="currentColor" opacity=".45"/>
      </svg>
      <div class="brand-text">
        <h1 id="page-title"></h1>
        <div class="brand-sub" id="page-sub"></div>
      </div>
    </div>
    <div class="top-stats" id="top-stats"></div>
    <button class="icon-btn" id="theme-toggle" type="button" title="切换亮色 / 暗色" aria-label="切换亮色 / 暗色">
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
        <circle cx="12" cy="12" r="8.2" fill="none" stroke="currentColor" stroke-width="1.8"/>
        <path d="M12 3.8a8.2 8.2 0 0 1 0 16.4z" fill="currentColor"/>
      </svg>
    </button>
  </header>
  <aside class="sidebar" aria-label="Trace 列表">
    <div class="side-head">
      <div class="side-title"><span>Traces</span><span class="count" id="trace-count"></span></div>
      <div class="side-tools">
        <input id="search" class="search" type="search" placeholder="搜索 trace / run_id / span" autocomplete="off" spellcheck="false" aria-label="搜索 trace">
        <button id="sort-toggle" class="btn" type="button"></button>
      </div>
    </div>
    <ol class="trace-list" id="trace-list"></ol>
  </aside>
  <div class="content">
    <main class="main" id="main">
      <section class="trace-head" id="trace-head"></section>
      <section class="card waterfall-card" id="waterfall-card">
        <div class="wf-toolbar">
          <div class="legend" aria-label="图例">
            <span class="lg"><i class="sw sw-agent"></i>Agent</span>
            <span class="lg"><i class="sw sw-llm"></i>LLM</span>
            <span class="lg"><i class="sw sw-tool"></i>工具</span>
            <span class="lg"><i class="sw sw-other"></i>其他</span>
            <span class="lg"><i class="sw sw-error"></i>错误 / 失败</span>
            <span class="lg"><i class="sw sw-warn"></i>中断 / 暂停</span>
          </div>
          <div class="wf-hint">点击 span 查看详情 · ↑↓ 切换 · ←→ 折叠/展开</div>
        </div>
        <div class="wf-header" aria-hidden="true">
          <div class="wf-name-h">Span</div>
          <div class="wf-axis"><div class="wf-lane" id="wf-axis"></div></div>
        </div>
        <div class="wf-body" id="waterfall" role="tree" tabindex="0" aria-label="Span 瀑布图"></div>
      </section>
    </main>
    <aside class="detail" id="detail" aria-label="Span 详情"></aside>
  </div>
</div>
<noscript><p class="noscript">这个查看器需要启用 JavaScript。</p></noscript>
"""

_CSS = """
:root {
  color-scheme: light;
  --bg: #f5f6f8; --panel: #ffffff; --panel-2: #f2f4f7; --hover: #f4f6f9;
  --border: #e3e6eb; --border-strong: #cfd5de;
  --text: #171a1f; --muted: #5a6472; --faint: #8a93a0;
  --accent: #4152d6; --accent-soft: #eceffd; --focus: rgba(65, 82, 214, .35);
  --grid: rgba(20, 30, 50, .07); --guide: #dde1e7;
  --agent: #7a5cf5; --llm: #2d7ce6; --tool: #0e9c80; --other: #8a93a0;
  --error: #dc3b40; --error-soft: #fdeeee; --error-text: #b3242a;
  --warn: #e19a12; --warn-soft: #fff4dc; --warn-text: #8f5b00;
  --ok: #1d9a55; --ok-soft: #e6f5ec; --ok-text: #16723f;
  --neutral-soft: #eef0f3;
  --j-key: #3848bf; --j-str: #0c7a5c; --j-num: #b0501a; --j-lit: #9336b0;
  --shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 1px 3px rgba(16, 24, 40, .06);
  --name-col: clamp(210px, 36%, 360px);
  --indent-w: 16px;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0e1116; --panel: #151920; --panel-2: #1b2029; --hover: #1b212a;
  --border: #262c36; --border-strong: #353d4a;
  --text: #e4e7ec; --muted: #a0a9b6; --faint: #6f7988;
  --accent: #8d98ff; --accent-soft: #232a4d; --focus: rgba(141, 152, 255, .4);
  --grid: rgba(255, 255, 255, .055); --guide: #2c333e;
  --agent: #9e8cff; --llm: #57a0ff; --tool: #2cc4a2; --other: #7d8795;
  --error: #ff5d63; --error-soft: #3a1c1f; --error-text: #ff8a8e;
  --warn: #f3b440; --warn-soft: #3a2e14; --warn-text: #f7c96b;
  --ok: #3ccf7e; --ok-soft: #15301f; --ok-text: #6fe0a1;
  --neutral-soft: #232932;
  --j-key: #9eaaff; --j-str: #6fd6b4; --j-num: #f2a46e; --j-lit: #d99cf2;
  --shadow: 0 1px 2px rgba(0, 0, 0, .3);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #0e1116; --panel: #151920; --panel-2: #1b2029; --hover: #1b212a;
    --border: #262c36; --border-strong: #353d4a;
    --text: #e4e7ec; --muted: #a0a9b6; --faint: #6f7988;
    --accent: #8d98ff; --accent-soft: #232a4d; --focus: rgba(141, 152, 255, .4);
    --grid: rgba(255, 255, 255, .055); --guide: #2c333e;
    --agent: #9e8cff; --llm: #57a0ff; --tool: #2cc4a2; --other: #7d8795;
    --error: #ff5d63; --error-soft: #3a1c1f; --error-text: #ff8a8e;
    --warn: #f3b440; --warn-soft: #3a2e14; --warn-text: #f7c96b;
    --ok: #3ccf7e; --ok-soft: #15301f; --ok-text: #6fe0a1;
    --neutral-soft: #232932;
    --j-key: #9eaaff; --j-str: #6fd6b4; --j-num: #f2a46e; --j-lit: #d99cf2;
    --shadow: 0 1px 2px rgba(0, 0, 0, .3);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--text);
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", "Noto Sans CJK SC", "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
button, input { font: inherit; color: inherit; }
.mono, pre, code, .wf-dur, .num { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; }
.num, .wf-dur, .stat-value, .ti-dur { font-variant-numeric: tabular-nums; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

.app {
  display: grid; height: 100vh;
  grid-template-columns: 300px minmax(0, 1fr);
  grid-template-rows: auto minmax(0, 1fr);
  grid-template-areas: "top top" "side content";
}
.topbar {
  grid-area: top; display: flex; align-items: center; gap: 16px;
  padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1 1 auto; }
.logo { color: var(--accent); flex: none; }
.brand-text { min-width: 0; }
.brand h1 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: .1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.brand-sub { color: var(--faint); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.top-stats { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.pill {
  display: inline-flex; align-items: baseline; gap: 5px; padding: 3px 9px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--border); color: var(--muted); font-size: 12px; white-space: nowrap;
}
.pill b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
.pill.pill-error b { color: var(--error-text); }
.icon-btn {
  flex: none; display: inline-grid; place-items: center; width: 32px; height: 32px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--panel); color: var(--muted); cursor: pointer;
}
.icon-btn:hover { background: var(--hover); color: var(--text); }
.btn {
  border: 1px solid var(--border); background: var(--panel); color: var(--muted); border-radius: 6px;
  padding: 3px 8px; font-size: 12px; cursor: pointer; white-space: nowrap;
}
.btn:hover { background: var(--hover); color: var(--text); }

/* ---------- 侧栏 ---------- */
.sidebar {
  grid-area: side; display: flex; flex-direction: column; min-height: 0;
  background: var(--panel); border-right: 1px solid var(--border);
}
.side-head { padding: 12px 12px 10px; border-bottom: 1px solid var(--border); }
.side-title { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 8px; }
.count { background: var(--panel-2); border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; font-size: 11px; letter-spacing: 0; color: var(--muted); }
.side-tools { display: flex; gap: 6px; }
.search {
  flex: 1 1 auto; min-width: 0; padding: 5px 9px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); font-size: 12.5px; outline: none;
}
.search:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus); }
.trace-list { list-style: none; margin: 0; padding: 6px; overflow: auto; flex: 1 1 auto; }
.trace-item {
  display: block; width: 100%; text-align: left; border: 1px solid transparent; background: none;
  border-radius: 8px; padding: 9px 10px 9px 12px; cursor: pointer; position: relative; margin-bottom: 2px;
}
.trace-item:hover { background: var(--hover); }
.trace-item.active { background: var(--accent-soft); border-color: var(--focus); }
.trace-item::before {
  content: ""; position: absolute; left: 4px; top: 10px; bottom: 10px; width: 3px; border-radius: 2px; background: var(--border-strong);
}
.trace-item.tone-ok::before { background: var(--ok); }
.trace-item.tone-warn::before { background: var(--warn); }
.trace-item.tone-error::before { background: var(--error); }
.ti-row { display: flex; align-items: center; gap: 6px; min-width: 0; }
.ti-row + .ti-row { margin-top: 3px; }
.ti-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1 1 auto; min-width: 0; }
.ti-dur { color: var(--muted); font-size: 12px; flex: none; }
.ti-meta { color: var(--faint); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1 1 auto; min-width: 0; }
.list-empty { color: var(--faint); padding: 18px 12px; text-align: center; font-size: 12.5px; }

.badge {
  display: inline-flex; align-items: center; gap: 4px; flex: none; font-size: 11px; font-weight: 600; line-height: 16px;
  padding: 1px 7px; border-radius: 999px; white-space: nowrap; background: var(--neutral-soft); color: var(--muted);
}
.badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: .85; }
.badge.tone-ok { background: var(--ok-soft); color: var(--ok-text); }
.badge.tone-warn { background: var(--warn-soft); color: var(--warn-text); }
.badge.tone-error { background: var(--error-soft); color: var(--error-text); }
.badge.plain::before { display: none; }

/* ---------- 主区域 ---------- */
.content { grid-area: content; display: grid; grid-template-columns: minmax(0, 1fr) clamp(320px, 30vw, 460px); min-height: 0; }
.is-empty .content { grid-template-columns: minmax(0, 1fr); }
.main { overflow: auto; padding: 16px 18px 28px; min-width: 0; }
.detail { overflow: auto; background: var(--panel); border-left: 1px solid var(--border); padding: 16px 18px 28px; min-width: 0; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow); }

.trace-head { margin-bottom: 14px; }
.th-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.th-title h2 { margin: 0; font-size: 18px; font-weight: 650; word-break: break-all; }
.th-agent { color: var(--muted); font-size: 12.5px; }
.th-meta { color: var(--faint); font-size: 12px; margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px 14px; }
.th-meta span { white-space: nowrap; }
.th-meta .mono { color: var(--muted); user-select: all; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.stat { flex: 1 1 auto; min-width: 92px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 11px; box-shadow: var(--shadow); }
.stat-label { color: var(--faint); font-size: 11.5px; white-space: nowrap; }
.stat-value { font-size: 16px; font-weight: 650; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat-value small { font-size: 11.5px; color: var(--faint); font-weight: 500; margin-left: 3px; }
.stat.is-error .stat-value { color: var(--error-text); }
.stat.is-warn .stat-value { color: var(--warn-text); }
.related { margin-top: 10px; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.related .btn { display: inline-flex; align-items: center; gap: 6px; }

/* ---------- 瀑布图 ---------- */
.wf-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding: 10px 14px; border-bottom: 1px solid var(--border); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; color: var(--muted); font-size: 12px; }
.lg { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.sw { display: inline-block; width: 14px; height: 8px; border-radius: 2px; background: var(--other); }
.sw-agent { background: var(--agent); } .sw-llm { background: var(--llm); } .sw-tool { background: var(--tool); }
.sw-error { background: var(--error); }
.sw-warn {
  background-color: var(--warn);
  background-image: repeating-linear-gradient(135deg, rgba(255, 255, 255, .38) 0 4px, transparent 4px 8px);
}
.wf-hint { color: var(--faint); font-size: 11.5px; }
.wf-header {
  display: grid; grid-template-columns: var(--name-col) minmax(0, 1fr); position: sticky; top: -16px; z-index: 2;
  background: var(--panel-2); border-bottom: 1px solid var(--border); height: 28px; align-items: center;
  font-size: 11.5px; color: var(--faint);
}
.wf-name-h { padding-left: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: .6px; }
.wf-axis { position: relative; height: 100%; }
.wf-lane { position: absolute; top: 0; bottom: 0; left: 10px; right: 56px; }
.tick { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--border-strong); }
.tick span { position: absolute; top: 6px; left: 4px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.tick.end span { left: auto; right: 4px; }
.tick.end { border-left: 0; border-right: 1px solid var(--border-strong); }

.wf-body { outline: none; padding-bottom: 4px; }
.wf-row {
  display: grid; grid-template-columns: var(--name-col) minmax(0, 1fr); height: 30px; cursor: pointer;
  border-bottom: 1px solid var(--grid); position: relative;
}
.wf-row:hover { background: var(--hover); }
.wf-row.selected { background: var(--accent-soft); }
.wf-row.selected::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--accent); }
.wf-body:focus-visible .wf-row.selected { box-shadow: inset 0 0 0 1px var(--accent); }
.wf-name { display: flex; align-items: center; gap: 6px; min-width: 0; padding: 0 10px 0 8px; border-right: 1px solid var(--border); }
.wf-indent {
  flex: none; align-self: stretch; width: calc(var(--depth, 0) * var(--indent-w));
  background-image: linear-gradient(to right, transparent calc(var(--indent-w) / 2 - 1px), var(--guide) calc(var(--indent-w) / 2 - 1px), var(--guide) calc(var(--indent-w) / 2), transparent calc(var(--indent-w) / 2));
  background-size: var(--indent-w) 100%; background-repeat: repeat-x;
}
.twisty, .twisty-spacer { flex: none; width: 16px; height: 16px; }
.twisty {
  display: inline-grid; place-items: center; padding: 0; border: 0; background: none; color: var(--faint);
  cursor: pointer; border-radius: 4px; font-size: 10px; line-height: 1;
}
.twisty:hover { background: var(--border); color: var(--text); }
.kind-mark { flex: none; width: 8px; height: 8px; border-radius: 2px; background: var(--other); }
.kind-agent .kind-mark { background: var(--agent); } .kind-llm .kind-mark { background: var(--llm); }
.kind-tool .kind-mark { background: var(--tool); }
.span-name { font-weight: 550; white-space: nowrap; flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.state-error .span-name { color: var(--error-text); }
.state-warn .span-name { color: var(--warn-text); }
.span-sub { color: var(--faint); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1 1 0; min-width: 0; }
.flag { flex: none; font-size: 10.5px; font-weight: 650; padding: 0 5px; border-radius: 4px; line-height: 16px; }
.state-error .flag { background: var(--error-soft); color: var(--error-text); }
.state-warn .flag { background: var(--warn-soft); color: var(--warn-text); }
.wf-track {
  position: relative; min-width: 0;
}
.wf-track .wf-lane {
  background-image: linear-gradient(to right, var(--grid) 1px, transparent 1px);
  background-size: 25% 100%;
}
.wf-bar {
  position: absolute; top: 50%; height: 14px; margin-top: -7px; min-width: 2px; border-radius: 3px;
  background: var(--other); opacity: .92;
}
.kind-agent .wf-bar { background: var(--agent); }
.has-children .wf-bar { height: 10px; margin-top: -5px; opacity: .6; }
.kind-llm .wf-bar { background: var(--llm); }
.kind-tool .wf-bar { background: var(--tool); }
.state-error .wf-bar { background: var(--error); opacity: 1; }
.state-warn .wf-bar {
  background-color: var(--warn); opacity: 1;
  background-image: repeating-linear-gradient(135deg, rgba(255, 255, 255, .38) 0 4px, transparent 4px 8px);
}
.wf-row:hover .wf-bar, .wf-row.selected .wf-bar { opacity: 1; }
.wf-row.selected .wf-bar { box-shadow: 0 0 0 2px var(--panel), 0 0 0 3.5px var(--accent); }
.wf-dur {
  position: absolute; top: 50%; transform: translateY(-50%); padding-left: 6px; white-space: nowrap;
  font-size: 11px; color: var(--muted);
}
.wf-row.selected .wf-dur { color: var(--text); }

.empty-state { padding: 48px 24px; text-align: center; color: var(--muted); }
.empty-state h2 { margin: 0 0 8px; font-size: 16px; color: var(--text); }
.empty-state p { margin: 6px auto; max-width: 520px; }
.empty-state code { background: var(--panel-2); border: 1px solid var(--border); border-radius: 5px; padding: 1px 6px; font-size: 12px; }

/* ---------- 详情面板 ---------- */
.detail-empty { color: var(--faint); text-align: center; padding: 40px 12px; }
.d-head { display: flex; align-items: flex-start; gap: 10px; }
.d-kind { flex: none; margin-top: 2px; font-size: 10.5px; font-weight: 700; letter-spacing: .4px; padding: 2px 7px; border-radius: 5px; color: #fff; background: var(--other); }
.d-kind.kind-agent { background: var(--agent); } .d-kind.kind-llm { background: var(--llm); } .d-kind.kind-tool { background: var(--tool); }
.d-title { min-width: 0; flex: 1 1 auto; }
.d-title h2 { margin: 0; font-size: 16px; font-weight: 650; word-break: break-all; }
.d-title .d-sub { color: var(--faint); font-size: 12px; margin-top: 2px; }
.detail h3 {
  margin: 20px 0 8px; font-size: 11.5px; font-weight: 650; color: var(--faint); text-transform: uppercase; letter-spacing: .6px;
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.kv { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 14px; margin: 12px 0 0; }
.kv dt { color: var(--faint); white-space: nowrap; }
.kv dd { margin: 0; min-width: 0; word-break: break-word; overflow-wrap: anywhere; }
.kv dd.mono { font-size: 12px; }
.callout { margin-top: 14px; padding: 9px 12px; border-radius: 8px; font-size: 12.5px; word-break: break-word; overflow-wrap: anywhere; border: 1px solid transparent; }
.callout b { display: block; margin-bottom: 2px; }
.callout.error { background: var(--error-soft); color: var(--error-text); border-color: rgba(220, 59, 64, .25); }
.callout.warn { background: var(--warn-soft); color: var(--warn-text); border-color: rgba(225, 154, 18, .3); }
pre.json {
  margin: 0; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
  font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; tab-size: 2;
}
.j-key { color: var(--j-key); } .j-str { color: var(--j-str); } .j-num { color: var(--j-num); } .j-lit { color: var(--j-lit); font-weight: 600; }
details.raw { margin-top: 16px; }
details.raw summary { cursor: pointer; color: var(--muted); font-size: 12.5px; margin-bottom: 8px; }
.noscript { padding: 24px; text-align: center; }

/* ---------- 响应式 ---------- */
@media (max-width: 1180px) {
  .content { display: block; overflow: auto; }
  .main { overflow: visible; }
  .detail { overflow: visible; border-left: 0; border-top: 1px solid var(--border); }
  .wf-header { top: 0; }
}
@media (max-width: 760px) {
  html, body { height: auto; }
  .app { display: block; height: auto; }
  .topbar { flex-wrap: wrap; gap: 8px 12px; padding: 10px 16px; }
  .brand { flex: 1 1 0; order: 1; }
  .icon-btn { order: 2; }
  .top-stats { order: 3; width: 100%; justify-content: flex-start; gap: 4px; }
  .pill { padding: 2px 8px; font-size: 11.5px; }
  .flag { display: none; }
  :root { --indent-w: 10px; }
  .sidebar { border-right: 0; border-bottom: 1px solid var(--border); }
  .trace-list { max-height: 240px; }
  .side-head { padding: 10px 16px; }
  .content { overflow: visible; }
  .main, .detail { padding: 14px 16px 22px; }
  .wf-header { position: static; }
  .wf-hint { display: none; }
  :root { --name-col: 48%; }
  .span-sub { display: none; }
  .wf-lane { right: 44px; }
  .stat { min-width: calc(50% - 4px); }
}
"""

_JS = r"""
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let DATA = {};
  try { DATA = JSON.parse($("trace-data").textContent || "{}"); } catch (e) { DATA = {}; }
  const TRACES = Array.isArray(DATA.traces) ? DATA.traces : [];
  const TOTALS = DATA.totals || {};
  const KIND_LABEL = { agent: "AGENT", llm: "LLM", tool: "TOOL", other: "SPAN" };
  const AGENT_STATUS = { completed: "完成", paused: "待审批", max_steps: "步数上限", stopped: "已中止", failed: "失败", running: "运行中" };
  const RISK_LABEL = { read: "只读 read", write: "写入 write", dangerous: "高危 dangerous" };
  const state = { trace: -1, span: null, collapsed: new Set(), query: "", newestFirst: false };

  // ---------------------------------------------------------------- 工具函数（只用 textContent 写入文本）
  function h(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function num(v) { const n = Number(v); return Number.isFinite(n) ? n : null; }
  function pad(n, w) { return String(n).padStart(w || 2, "0"); }
  function fmtDur(ms) {
    ms = num(ms);
    if (ms === null) return "–";
    if (ms < 1) return ms.toFixed(2) + "ms";
    if (ms < 10) return ms.toFixed(1) + "ms";
    if (ms < 1000) return Math.round(ms) + "ms";
    if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + "s";
    const m = Math.floor(ms / 60000);
    return m + "m " + Math.round((ms - m * 60000) / 1000) + "s";
  }
  function fmtInt(v) { const n = num(v); return n === null ? "–" : Math.round(n).toLocaleString("en-US"); }
  function fmtCost(v) {
    const c = num(v);
    if (c === null) return "–";
    if (c === 0) return "$0";
    if (c >= 1) return "$" + c.toFixed(2);
    if (c >= 0.01) return "$" + c.toFixed(4);
    return "$" + c.toFixed(6).replace(/0+$/, "");
  }
  function fmtTime(sec, full) {
    const d = new Date(num(sec) * 1000);
    if (isNaN(d.getTime())) return "–";
    const t = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    const day = pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    return full ? d.getFullYear() + "-" + day + " " + t + "." + pad(d.getMilliseconds(), 3) : day + " " + t;
  }
  function short(id, n) { id = String(id == null ? "" : id); return id.length > (n || 8) ? id.slice(0, n || 8) + "…" : id; }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function badge(label, tone, plain) { return h("span", "badge tone-" + (tone || "neutral") + (plain ? " plain" : ""), label); }
  function keyOf(trace, span) { return trace.id + "\u0000" + span.span_id; }

  const JSON_TOKEN = /("(?:\\u[0-9a-fA-F]{4}|\\[^u]|[^\\"])*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  function jsonBlock(value) {
    const pre = h("pre", "json");
    let text;
    try { text = JSON.stringify(value, null, 2); } catch (e) { text = String(value); }
    if (text === undefined) text = String(value);
    let last = 0, m;
    JSON_TOKEN.lastIndex = 0;
    while ((m = JSON_TOKEN.exec(text)) !== null) {
      if (m.index > last) pre.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) {
        pre.appendChild(h("span", m[2] ? "j-key" : "j-str", m[1]));
        if (m[2]) pre.appendChild(document.createTextNode(m[2]));
      } else {
        pre.appendChild(h("span", /^[tfn]/.test(m[0]) ? "j-lit" : "j-num", m[0]));
      }
      last = JSON_TOKEN.lastIndex;
    }
    if (last < text.length) pre.appendChild(document.createTextNode(text.slice(last)));
    return pre;
  }
  function tryParseJSON(s) {
    if (typeof s !== "string") return undefined;
    try { return JSON.parse(s); } catch (e) { return undefined; }
  }
  function compactArgs(s) {
    const v = tryParseJSON(s);
    if (v && typeof v === "object" && !Array.isArray(v)) {
      return Object.keys(v).map((k) => k + "=" + (typeof v[k] === "string" ? v[k] : JSON.stringify(v[k]))).join(", ");
    }
    return s == null ? "" : String(s);
  }

  // ---------------------------------------------------------------- span 文案
  function flagText(s) {
    const a = s.attrs || {};
    if (s.state === "warn") {
      if (a.interrupted === "PauseRun") return "等待审批";
      if (a.interrupted) return "中断";
      return AGENT_STATUS[a["agent.status"]] || "中断";
    }
    if (s.state === "error") {
      if (a["tool.ok"] === false) return a["tool.error_type"] === "denied" ? "被拒绝" : "失败";
      return a["agent.status"] === "failed" ? "失败" : "错误";
    }
    return "";
  }
  function subText(s) {
    const a = s.attrs || {};
    if (s.kind === "llm") {
      const bits = [];
      if (a.step != null) bits.push("第 " + a.step + " 步");
      if (a.result) bits.push(String(a.result).replace(/^tool_calls: /, "→ "));
      if (a["gen_ai.usage.input_tokens"] != null) bits.push(fmtInt(a["gen_ai.usage.input_tokens"]) + "→" + fmtInt(a["gen_ai.usage.output_tokens"]) + " tok");
      if (s.status === "error" && a.error) bits.push(String(a.error));
      return bits.join(" · ");
    }
    if (s.kind === "tool") {
      const args = compactArgs(a["tool.arguments"]);
      if (a["tool.ok"] === false && a["tool.error_type"]) return a["tool.error_type"] + (args ? " · " + args : "");
      return args;
    }
    if (s.kind === "agent") {
      const bits = [];
      if (a["agent.name"]) bits.push(String(a["agent.name"]));
      if (a["agent.steps"] != null) bits.push(a["agent.steps"] + " 步");
      if (s.status === "error" && a.error) bits.push(String(a.error));
      return bits.join(" · ");
    }
    return a.error ? String(a.error) : "";
  }

  // ---------------------------------------------------------------- 顶栏
  function renderTop() {
    const title = DATA.title || "Agent Trace 查看器";
    $("page-title").textContent = title;
    const sub = [];
    if (DATA.source) sub.push("来源：" + DATA.source);
    if (DATA.generated_at) sub.push("生成于 " + fmtTime(DATA.generated_at, false));
    sub.push("agentkit.viewer");
    $("page-sub").textContent = sub.join(" · ");
    const box = $("top-stats");
    box.replaceChildren();
    const pill = (label, value, cls) => { const p = h("span", "pill" + (cls ? " " + cls : "")); p.appendChild(h("b", null, value)); p.appendChild(document.createTextNode(label)); return p; };
    box.appendChild(pill("条 trace", fmtInt(TOTALS.traces || 0)));
    box.appendChild(pill("个 span", fmtInt(TOTALS.spans || 0)));
    box.appendChild(pill("tokens", fmtInt((TOTALS.input_tokens || 0) + (TOTALS.output_tokens || 0))));
    if (TOTALS.cost_usd != null) box.appendChild(pill("成本", fmtCost(TOTALS.cost_usd)));
    if (TOTALS.errors) box.appendChild(pill("个错误", fmtInt(TOTALS.errors), "pill-error"));
  }

  // ---------------------------------------------------------------- trace 列表
  TRACES.forEach((t, i) => {
    t._index = i;
    t._hay = [t.name, t.id, t.run_id, t.agent_name, t.status_label].concat((t.spans || []).map((s) => s.name)).join(" ").toLowerCase();
  });
  function visibleTraces() {
    const q = state.query.trim().toLowerCase();
    const list = TRACES.filter((t) => !q || t._hay.indexOf(q) !== -1);
    if (state.newestFirst) list.reverse();
    return list;
  }
  function renderList() {
    const ol = $("trace-list");
    ol.replaceChildren();
    const list = visibleTraces();
    $("trace-count").textContent = list.length === TRACES.length ? String(TRACES.length) : list.length + " / " + TRACES.length;
    $("sort-toggle").textContent = state.newestFirst ? "最新在前" : "最早在前";
    if (!list.length) {
      ol.appendChild(h("li", "list-empty", TRACES.length ? "没有匹配的 trace" : "没有 trace"));
      return;
    }
    for (const t of list) {
      const li = h("li");
      const b = h("button", "trace-item tone-" + (t.tone || "neutral") + (t._index === state.trace ? " active" : ""));
      b.type = "button";
      b.dataset.index = String(t._index);
      if (t._index === state.trace) b.setAttribute("aria-current", "true");
      const r1 = h("div", "ti-row");
      r1.appendChild(h("span", "ti-name", t.name + (t.agent_name ? " · " + t.agent_name : "")));
      r1.appendChild(h("span", "ti-dur", fmtDur(t.duration_ms)));
      const r2 = h("div", "ti-row");
      r2.appendChild(h("span", "ti-meta", fmtTime(t.start, false) + (t.run_id ? " · run " + short(t.run_id, 8) : "")));
      r2.appendChild(badge(t.status_label, t.tone));
      const r3 = h("div", "ti-row");
      const bits = [fmtInt(t.input_tokens + t.output_tokens) + " tokens"];
      if (t.cost_usd != null) bits.push(fmtCost(t.cost_usd));
      bits.push(t.span_count + " span");
      if (t.error_count) bits.push(t.error_count + " 错误");
      r3.appendChild(h("span", "ti-meta", bits.join(" · ")));
      b.append(r1, r2, r3);
      b.addEventListener("click", () => selectTrace(t._index, null, true));
      li.appendChild(b);
      ol.appendChild(li);
    }
  }
  $("trace-list").addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    const items = Array.from($("trace-list").querySelectorAll(".trace-item"));
    const i = items.indexOf(document.activeElement);
    if (i === -1) return;
    ev.preventDefault();
    const next = items[clamp(i + (ev.key === "ArrowDown" ? 1 : -1), 0, items.length - 1)];
    selectTrace(Number(next.dataset.index), null, true);
    const again = $("trace-list").querySelector('.trace-item[data-index="' + next.dataset.index + '"]');
    if (again) again.focus();
  });
  $("search").addEventListener("input", (ev) => { state.query = ev.target.value; renderList(); });
  $("sort-toggle").addEventListener("click", () => { state.newestFirst = !state.newestFirst; renderList(); });

  // ---------------------------------------------------------------- trace 头部
  function stat(label, value, extra, cls) {
    const d = h("div", "stat" + (cls ? " " + cls : ""));
    d.appendChild(h("div", "stat-label", label));
    const v = h("div", "stat-value", value);
    if (extra) v.appendChild(h("small", null, extra));
    d.appendChild(v);
    return d;
  }
  function renderHead(t) {
    const box = $("trace-head");
    box.replaceChildren();
    const title = h("div", "th-title");
    title.appendChild(h("h2", null, t.name));
    title.appendChild(badge(t.status_label, t.tone));
    if (t.agent_name) title.appendChild(h("span", "th-agent", t.agent_name));
    box.appendChild(title);
    const meta = h("div", "th-meta");
    const m1 = h("span", null, "trace "); m1.appendChild(h("span", "mono", t.id)); meta.appendChild(m1);
    if (t.run_id) { const m2 = h("span", null, "run_id "); m2.appendChild(h("span", "mono", t.run_id)); meta.appendChild(m2); }
    meta.appendChild(h("span", null, fmtTime(t.start, true)));
    box.appendChild(meta);
    const stats = h("div", "stats");
    stats.appendChild(stat("总耗时", fmtDur(t.duration_ms)));
    if (t.steps != null) stats.appendChild(stat("步数", fmtInt(t.steps)));
    stats.appendChild(stat("Tokens 输入 / 输出", fmtInt(t.input_tokens) + " / " + fmtInt(t.output_tokens)));
    if (t.cost_usd != null) {
      const cumulative = t.run_cost_usd != null && Math.abs(t.run_cost_usd - t.cost_usd) > 1e-12;
      const c = stat("成本", fmtCost(t.cost_usd));
      if (cumulative) c.title = "本 trace 的增量成本；整个 run 累计 " + fmtCost(t.run_cost_usd);
      stats.appendChild(c);
    }
    stats.appendChild(stat("LLM / 工具调用", t.llm_count + " / " + t.tool_count));
    stats.appendChild(stat("错误 / 中断", t.error_count + " / " + t.warn_count, "",
      t.error_count ? "is-error" : t.warn_count ? "is-warn" : ""));
    box.appendChild(stats);
    if (t.related && t.related.length) {
      const rel = h("div", "related");
      rel.appendChild(h("span", null, "同一 run 的其他 trace："));
      for (const r of t.related) {
        const b = h("button", "btn");
        b.type = "button";
        b.appendChild(h("span", null, r.name + " · " + fmtTime(r.start, false)));
        b.appendChild(badge(r.status_label, r.tone));
        b.addEventListener("click", () => {
          const idx = TRACES.findIndex((x) => x.id === r.id);
          if (idx !== -1) selectTrace(idx, null, true);
        });
        rel.appendChild(b);
      }
      box.appendChild(rel);
    }
  }

  // ---------------------------------------------------------------- 瀑布图
  function visibleSpans(t) {
    const out = [];
    let hideDepth = Infinity;
    for (const s of t.spans) {
      if (s.depth > hideDepth) continue;
      hideDepth = Infinity;
      out.push(s);
      if (s.child_count && state.collapsed.has(keyOf(t, s))) hideDepth = s.depth;
    }
    return out;
  }
  function renderAxis(total) {
    const axis = $("wf-axis");
    axis.replaceChildren();
    const w = axis.clientWidth || 1000;
    const ticks = w < 170 ? [0, 1] : w < 300 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
    ticks.forEach((p) => {
      const tick = h("div", "tick" + (p === 1 ? " end" : ""));
      if (p === 1) tick.style.right = "0"; else tick.style.left = p * 100 + "%";
      tick.appendChild(h("span", null, p === 0 ? "0" : fmtDur(total * p)));
      axis.appendChild(tick);
    });
  }
  function renderWaterfall(t) {
    const body = $("waterfall");
    body.replaceChildren();
    const total = Math.max(num(t.duration_ms) || 0, 0.001);
    renderAxis(total);
    for (const s of visibleSpans(t)) {
      const collapsed = state.collapsed.has(keyOf(t, s));
      const row = h("div", "wf-row kind-" + s.kind + " state-" + s.state + (s.child_count ? " has-children" : "") + (s.span_id === state.span ? " selected" : ""));
      row.setAttribute("role", "treeitem");
      row.setAttribute("aria-level", String(s.depth + 1));
      row.setAttribute("aria-selected", s.span_id === state.span ? "true" : "false");
      if (s.child_count) row.setAttribute("aria-expanded", collapsed ? "false" : "true");
      row.dataset.span = s.span_id;

      const name = h("div", "wf-name");
      const indent = h("span", "wf-indent");
      indent.style.setProperty("--depth", String(s.depth));
      name.appendChild(indent);
      if (s.child_count) {
        const tw = h("button", "twisty", collapsed ? "▶" : "▼");
        tw.type = "button";
        tw.tabIndex = -1;
        tw.title = collapsed ? "展开 " + s.child_count + " 个子 span" : "折叠";
        tw.addEventListener("click", (ev) => { ev.stopPropagation(); toggle(t, s); });
        name.appendChild(tw);
      } else {
        name.appendChild(h("span", "twisty-spacer"));
      }
      name.appendChild(h("span", "kind-mark"));
      name.appendChild(h("span", "span-name", s.name));
      const flag = flagText(s);
      if (flag) name.appendChild(h("span", "flag", flag));
      const sub = subText(s);
      if (sub) name.appendChild(h("span", "span-sub", sub));
      row.title = s.name + (sub ? "\n" + sub : "") + "\n" + fmtDur(s.duration_ms);

      const track = h("div", "wf-track");
      const lane = h("div", "wf-lane");
      const left = clamp((num(s.offset_ms) || 0) / total * 100, 0, 100);
      const width = clamp((num(s.duration_ms) || 0) / total * 100, 0, 100 - left);
      const bar = h("div", "wf-bar");
      bar.style.left = left + "%";
      bar.style.width = width + "%";
      const dur = h("span", "wf-dur", fmtDur(s.duration_ms));
      dur.style.left = left + width + "%";  // lane 右侧预留了槽位，贴着最右边的条形也放得下
      lane.append(bar, dur);
      track.appendChild(lane);
      row.append(name, track);
      row.addEventListener("click", () => selectSpan(s.span_id, true));
      body.appendChild(row);
    }
  }
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { const t = TRACES[state.trace]; if (t) renderAxis(Math.max(num(t.duration_ms) || 0, 0.001)); }, 120);
  });
  function toggle(t, s, force) {
    const k = keyOf(t, s);
    const collapse = force === undefined ? !state.collapsed.has(k) : force;
    if (collapse) state.collapsed.add(k); else state.collapsed.delete(k);
    renderWaterfall(t);
  }
  $("waterfall").addEventListener("keydown", (ev) => {
    const t = TRACES[state.trace];
    if (!t) return;
    const vis = visibleSpans(t);
    let i = vis.findIndex((s) => s.span_id === state.span);
    const cur = vis[i];
    const go = (j) => { ev.preventDefault(); const s = vis[clamp(j, 0, vis.length - 1)]; if (s) selectSpan(s.span_id, true); };
    switch (ev.key) {
      case "ArrowDown": go(i + 1); break;
      case "ArrowUp": go(i === -1 ? 0 : i - 1); break;
      case "Home": go(0); break;
      case "End": go(vis.length - 1); break;
      case "ArrowLeft":
        if (!cur) return;
        ev.preventDefault();
        if (cur.child_count && !state.collapsed.has(keyOf(t, cur))) toggle(t, cur, true);
        else if (cur.parent_id) { const p = vis.findIndex((s) => s.span_id === cur.parent_id); if (p !== -1) go(p); }
        break;
      case "ArrowRight":
        if (!cur) return;
        ev.preventDefault();
        if (cur.child_count && state.collapsed.has(keyOf(t, cur))) toggle(t, cur, false);
        else if (cur.child_count) go(i + 1);
        break;
      default: return;
    }
  });

  // ---------------------------------------------------------------- 详情面板
  function kvList(pairs) {
    const dl = h("dl", "kv");
    for (const [k, v, cls] of pairs) {
      if (v === undefined || v === null || v === "") continue;
      dl.appendChild(h("dt", null, k));
      const dd = h("dd", cls || null);
      if (v instanceof Node) dd.appendChild(v); else dd.textContent = String(v);
      dl.appendChild(dd);
    }
    return dl;
  }
  function copyButton(getText) {
    const b = h("button", "btn", "复制 JSON");
    b.type = "button";
    b.addEventListener("click", () => {
      const done = (ok) => { b.textContent = ok ? "已复制 ✓" : "复制失败"; setTimeout(() => { b.textContent = "复制 JSON"; }, 1400); };
      try {
        navigator.clipboard.writeText(getText()).then(() => done(true), () => done(false));
      } catch (e) { done(false); }
    });
    return b;
  }
  function keyInfo(s) {
    const a = s.attrs || {};
    const tokens = (i, o) => (i == null && o == null ? null : fmtInt(i) + " → " + fmtInt(o));
    if (s.kind === "llm") {
      const model = a["gen_ai.request.model"];
      const resp = a["gen_ai.response.model"];
      return [
        ["模型", model && resp && resp !== model ? model + "（实际 " + resp + "）" : model || resp],
        ["第几步", a.step],
        ["输入消息数", a.messages],
        ["Token 输入→输出", tokens(a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"])],
        ["结束原因", a.finish_reason],
        ["结果", a.result],
      ];
    }
    if (s.kind === "tool") {
      const parsed = tryParseJSON(a["tool.arguments"]);
      let result = null;
      if (a["tool.ok"] === true) result = badge("成功", "ok");
      else if (a["tool.ok"] === false) result = badge("失败 · " + (a["tool.error_type"] || "unknown"), "error");
      else if (a.interrupted) result = badge("未执行（" + a.interrupted + "）", "warn");
      return [
        ["工具", a["tool.name"]],
        ["风险等级", a["tool.risk"] ? RISK_LABEL[a["tool.risk"]] || a["tool.risk"] : null],
        ["结果", result],
        ["参数", parsed !== undefined ? jsonBlock(parsed) : a["tool.arguments"]],
      ];
    }
    if (s.kind === "agent") {
      const st = a["agent.status"];
      return [
        ["Agent", a["agent.name"]],
        ["run_id", a.run_id, "mono"],
        ["运行状态", st != null ? (AGENT_STATUS[st] ? AGENT_STATUS[st] + "（" + st + "）" : st) : null],
        ["步数", a["agent.steps"]],
        ["Token（run 累计）", tokens(a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"])],
        ["成本（run 累计）", a["agent.cost_usd"] != null ? fmtCost(a["agent.cost_usd"]) : null],
      ];
    }
    return [];
  }
  function renderDetail(t, s) {
    const box = $("detail");
    box.replaceChildren();
    if (!t || !s) {
      box.appendChild(h("div", "detail-empty", t ? "点击瀑布图中的任意 span 查看详情" : ""));
      return;
    }
    const head = h("div", "d-head");
    head.appendChild(h("span", "d-kind kind-" + s.kind, KIND_LABEL[s.kind] || "SPAN"));
    const tt = h("div", "d-title");
    tt.appendChild(h("h2", null, s.name));
    tt.appendChild(h("div", "d-sub", "开始于 +" + fmtDur(s.offset_ms) + " · 耗时 " + fmtDur(s.duration_ms)));
    head.appendChild(tt);
    const flag = flagText(s);
    head.appendChild(badge(flag || (s.status === "ok" ? "正常" : s.status), s.state === "ok" ? "ok" : s.state));
    box.appendChild(head);

    const a = s.attrs || {};
    if (a.error) {
      const c = h("div", "callout error");
      c.appendChild(h("b", null, "错误"));
      c.appendChild(document.createTextNode(String(a.error)));
      box.appendChild(c);
    } else if (a["tool.ok"] === false) {
      const c = h("div", "callout error");
      c.appendChild(h("b", null, "工具调用失败：" + (a["tool.error_type"] || "unknown")));
      c.appendChild(document.createTextNode("失败信息作为观察结果返回给了模型，模型可以据此自我纠正。"));
      box.appendChild(c);
    }
    if (a.interrupted) {
      const c = h("div", "callout warn");
      c.appendChild(h("b", null, "被中断：" + a.interrupted));
      c.appendChild(document.createTextNode(a.interrupted === "PauseRun"
        ? "运行在这里暂停等待人工审批，状态已存入检查点；审批后会在新的 agent.resume trace 中继续。"
        : "这不是故障：运行被主动暂停或中止（hooks 抛出的 PauseRun / StopRun）。"));
      box.appendChild(c);
    }

    box.appendChild(kvList([
      ["状态", s.status, "mono"],
      ["开始时间", fmtTime(s.start, true)],
      ["耗时", fmtDur(s.duration_ms)],
      ["span_id", s.span_id, "mono"],
      ["parent_id", s.parent_id || "（根 span）", "mono"],
      ["trace_id", t.id, "mono"],
    ]));

    const info = keyInfo(s).filter((p) => p[1] !== undefined && p[1] !== null && p[1] !== "");
    if (info.length) {
      box.appendChild(h("h3", null, "关键信息"));
      box.appendChild(kvList(info));
    }
    const h3 = h("h3");
    h3.appendChild(h("span", null, "全部属性 attrs"));
    h3.appendChild(copyButton(() => JSON.stringify(a, null, 2)));
    box.appendChild(h3);
    box.appendChild(jsonBlock(a));

    const raw = h("details", "raw");
    raw.appendChild(h("summary", null, "原始 span JSON"));
    const rawSpan = { name: s.name, trace_id: s.trace_id, span_id: s.span_id, parent_id: s.parent_id, start: s.start, end: s.end, duration_ms: s.duration_ms, status: s.status, attrs: a };
    raw.appendChild(jsonBlock(rawSpan));
    box.appendChild(raw);
  }

  // ---------------------------------------------------------------- 选择与路由
  function writeHash() {
    const t = TRACES[state.trace];
    if (!t) return;
    const hash = "#trace=" + encodeURIComponent(t.id) + (state.span ? "&span=" + encodeURIComponent(state.span) : "");
    try { history.replaceState(null, "", hash); } catch (e) { /* file:// 等环境可能不允许，忽略 */ }
  }
  function readHash() {
    const out = {};
    String(location.hash || "").replace(/^#/, "").split("&").forEach((kv) => {
      const i = kv.indexOf("=");
      if (i > 0) { try { out[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1)); } catch (e) { /* 忽略坏的 hash */ } }
    });
    return out;
  }
  function selectTrace(index, spanId, updateHash) {
    const t = TRACES[index];
    if (!t) return;
    state.trace = index;
    const spans = t.spans || [];
    state.span = spanId && spans.some((s) => s.span_id === spanId) ? spanId : (spans[0] ? spans[0].span_id : null);
    renderList();
    renderHead(t);
    renderWaterfall(t);
    renderDetail(t, spans.find((s) => s.span_id === state.span));
    if (updateHash) writeHash();
  }
  function selectSpan(spanId, fromUser) {
    const t = TRACES[state.trace];
    if (!t) return;
    state.span = spanId;
    for (const row of $("waterfall").querySelectorAll(".wf-row")) {
      const on = row.dataset.span === spanId;
      row.classList.toggle("selected", on);
      row.setAttribute("aria-selected", on ? "true" : "false");
      if (on && fromUser) row.scrollIntoView({ block: "nearest" });
    }
    renderDetail(t, t.spans.find((s) => s.span_id === spanId));
    if (fromUser) {
      writeHash();
      if (window.matchMedia("(max-width: 1180px)").matches && document.activeElement !== $("waterfall")) {
        $("detail").scrollIntoView({ block: "start", behavior: "smooth" });
      }
    }
  }

  // ---------------------------------------------------------------- 主题
  const THEME_KEY = "agentkit-viewer-theme";
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  } catch (e) { /* 无痕模式等场景下 localStorage 不可用 */ }
  $("theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const current = root.getAttribute("data-theme") || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 忽略 */ }
  });

  // ---------------------------------------------------------------- 启动
  renderTop();
  if (!TRACES.length) {
    document.getElementById("app").classList.add("is-empty");
    renderList();
    const main = $("trace-head");
    const empty = h("div", "card empty-state");
    empty.appendChild(h("h2", null, "还没有 trace 数据"));
    const p1 = h("p", null, "给 Agent 配一个导出器：");
    p1.appendChild(h("code", null, 'Tracer(exporter=jsonl_exporter("traces.jsonl"))'));
    const p2 = h("p", null, "运行一次 Agent 后再生成页面：");
    p2.appendChild(h("code", null, "python -m agentkit.viewer traces.jsonl -o trace.html"));
    empty.append(p1, p2);
    main.appendChild(empty);
    $("waterfall-card").hidden = true;
    renderDetail(null, null);
    $("detail").hidden = true;
    return;
  }
  const want = readHash();
  const fromHash = TRACES.findIndex((t) => t.id === want.trace);
  selectTrace(fromHash === -1 ? 0 : fromHash, want.span || null, false);
})();
"""


if __name__ == "__main__":
    raise SystemExit(main())
