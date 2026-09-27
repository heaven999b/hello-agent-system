"""agentkit.viewer 的测试：全部离线、确定性。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time

import pytest

from agentkit import (
    Agent,
    PermissionPolicy,
    ScriptedLLM,
    ToolError,
    Tracer,
    call_tool,
    call_tools,
    jsonl_exporter,
    reply,
    tool,
)
from agentkit.viewer import build_traces, load_spans, render_html

DATA_RE = re.compile(r'<script type="application/json" id="trace-data">(.*?)</script>', re.S)


def embedded_data(page: str) -> dict:
    m = DATA_RE.search(page)
    assert m, "页面里没有找到嵌入的 trace 数据"
    return json.loads(m.group(1))


@tool
def search_kb(query: str) -> str:
    """搜索知识库"""
    return f"关于 {query} 的文章"


@tool
def get_order(order_id: str) -> str:
    """查询订单"""
    raise ToolError(f"订单 {order_id} 不存在")


@tool(risk="dangerous")
def refund(order_id: str) -> str:
    """退款（需要审批）"""
    return f"已退款 {order_id}"


@pytest.fixture
def traces_file(tmp_path):
    """一次真实的多步运行：并行工具调用 + 工具失败 + 审批暂停 → 恢复。"""
    path = tmp_path / "traces.jsonl"
    tracer = Tracer(exporter=jsonl_exporter(path))
    llm = ScriptedLLM([
        call_tools(("search_kb", {"query": "退款"}), ("get_order", {"order_id": "A-1"})),
        call_tool("refund", order_id="A-2"),
        reply("已退款"),
    ])
    agent = Agent(llm, [search_kb, get_order, refund], hooks=[PermissionPolicy()], tracer=tracer, name="support")
    res = agent.run("帮我退款")
    assert res.status == "paused"
    assert agent.approve(res.run_id).ok
    return path


# ------------------------------------------------------------------ 基本渲染


def test_render_html_has_key_elements(traces_file):
    page = render_html(load_spans(traces_file), title="第 07 课")
    assert page.startswith("<!DOCTYPE html>")
    assert "<title>第 07 课</title>" in page
    for element_id in ("trace-list", "waterfall", "detail", "trace-head", "theme-toggle", "trace-data"):
        assert f'id="{element_id}"' in page
    assert "prefers-color-scheme: dark" in page
    assert '<meta name="viewport"' in page


def test_page_is_self_contained(traces_file):
    page = render_html(load_spans(traces_file))
    assert "<link" not in page
    assert "@import" not in page
    assert not re.search(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""", page)
    assert "http://" not in page and "https://" not in page
    assert "default-src 'none'" in page  # CSP：页面不能加载任何外部资源


def test_csp_hashes_match_inline_code(traces_file):
    """CSP 只放行本页内联脚本/样式的哈希：改了代码却没更新哈希，页面会整个空白。"""
    page = render_html(load_spans(traces_file))
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)

    def sha(text: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"

    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
    csp = csp.replace("&#x27;", "'")
    assert f"script-src {sha(script)}" in csp
    assert f"style-src {sha(style)}" in csp


def test_empty_input():
    assert build_traces([]) == []
    page = render_html([])
    data = embedded_data(page)
    assert data["traces"] == [] and data["totals"]["traces"] == 0
    assert 'id="waterfall"' in page


def test_accepts_span_objects():
    tracer = Tracer()
    Agent(ScriptedLLM([call_tool("search_kb", query="x"), reply("ok")]), [search_kb], tracer=tracer).run("hi")
    traces = build_traces(tracer.traces)  # 直接传根 Span，会自动展开子 span
    assert [s["name"] for s in traces[0]["spans"]] == ["agent.run", "llm.chat", "tool.search_kb", "llm.chat"]


# ------------------------------------------------------------------ 数据模型


def test_agent_run_model(traces_file):
    traces = build_traces(load_spans(traces_file))
    assert [t["name"] for t in traces] == ["agent.run", "agent.resume"]
    run, resume = traces

    assert run["status_label"] == "待审批" and run["tone"] == "warn"
    assert resume["status_label"] == "完成" and resume["tone"] == "ok"
    names = [s["name"] for s in run["spans"]]
    assert names == ["agent.run", "llm.chat", "tool.search_kb", "tool.get_order", "llm.chat", "tool.refund"]
    by_name = {s["name"]: s for s in run["spans"]}
    assert by_name["tool.get_order"]["state"] == "error"  # 工具失败标红
    assert by_name["tool.refund"]["state"] == "warn"  # 审批暂停（attrs.interrupted）标黄
    assert by_name["tool.refund"]["attrs"]["interrupted"] == "PauseRun"
    assert by_name["tool.search_kb"]["state"] == "ok"
    assert {s["kind"] for s in run["spans"]} == {"agent", "llm", "tool"}
    assert run["error_count"] == 1 and run["warn_count"] == 2  # 暂停的根 span 也算"中断"
    assert run["llm_count"] == 2 and run["tool_count"] == 3
    assert run["input_tokens"] == 40 and run["output_tokens"] == 20

    # 同一个 run_id 的两条 trace 互相关联；成本拆成每段的增量
    assert run["run_id"] == resume["run_id"]
    assert [r["id"] for r in run["related"]] == [resume["id"]]
    assert resume["run_cost_usd"] > run["run_cost_usd"]
    assert resume["cost_usd"] == pytest.approx(resume["run_cost_usd"] - run["run_cost_usd"])


def test_nested_spans_depth_order_and_offsets():
    tracer = Tracer()
    with tracer.span("workflow.report"):
        with tracer.span("step.a"):
            time.sleep(0.002)
            with tracer.span("step.a.inner"):
                pass
        with tracer.span("step.b", **{"tool.name": "x"}):
            pass
    (trace,) = build_traces(tracer.traces)
    spans = trace["spans"]
    assert [(s["name"], s["depth"], s["child_count"]) for s in spans] == [
        ("workflow.report", 0, 2),
        ("step.a", 1, 1),
        ("step.a.inner", 2, 0),
        ("step.b", 1, 0),
    ]
    assert spans[0]["offset_ms"] == 0
    assert all(0 <= s["offset_ms"] <= trace["duration_ms"] for s in spans)
    assert spans[3]["offset_ms"] >= spans[1]["offset_ms"] + spans[1]["duration_ms"] - 0.01
    assert spans[0]["kind"] == "other" and spans[3]["kind"] == "tool"  # 按属性推断类型
    assert trace["status_label"] == "正常"


def test_multiple_traces_sorted_by_start():
    spans = [
        {"name": "agent.run", "trace_id": "t2", "span_id": "b", "parent_id": None, "start": 20.0, "end": 21.0},
        {"name": "agent.run", "trace_id": "t1", "span_id": "a", "parent_id": None, "start": 10.0, "end": 12.5,
         "status": "error", "attrs": {"error": "RuntimeError: boom"}},
        {"name": "llm.chat", "trace_id": "t1", "span_id": "a1", "parent_id": "a", "start": 10.5, "end": 11.0},
    ]
    traces = build_traces(spans)
    assert [t["id"] for t in traces] == ["t1", "t2"]
    assert traces[0]["duration_ms"] == pytest.approx(2500)
    assert traces[0]["status_label"] == "异常" and traces[0]["tone"] == "error"
    assert traces[0]["spans"][1]["offset_ms"] == pytest.approx(500)


def test_tolerates_messy_data():
    spans = [
        # 父 span 不在文件里（孤儿）→ 当作根
        {"name": "orphan", "trace_id": "t", "span_id": "o", "parent_id": "missing", "start": 1.0, "end": 1.1},
        # 父子成环 → 不死循环、不丢数据
        {"name": "x", "trace_id": "c", "span_id": "x", "parent_id": "y", "start": 1.0, "end": 2.0},
        {"name": "y", "trace_id": "c", "span_id": "y", "parent_id": "x", "start": 1.5, "end": 2.0},
        # 缺字段、没有 end、attrs 不是 dict、NaN
        {"trace_id": "m", "start": "3", "duration_ms": 250, "attrs": {"v": float("nan"), "b": b"raw"}},
        {"name": "weird", "trace_id": "m", "span_id": "w", "start": None, "attrs": [1, 2]},
    ]
    traces = {t["id"]: t for t in build_traces(spans)}
    assert traces["t"]["spans"][0]["depth"] == 0
    assert sorted(s["name"] for s in traces["c"]["spans"]) == ["x", "y"]
    m = traces["m"]["spans"]
    assert any(s["duration_ms"] == pytest.approx(250) for s in m)
    page = render_html(spans)

    def no_constants(name):
        raise AssertionError(f"嵌入的 JSON 里出现了非标准常量 {name}")

    json.loads(DATA_RE.search(page).group(1), parse_constant=no_constants)  # 浏览器的 JSON.parse 不认 NaN


def test_load_spans_skips_blank_and_broken_lines(tmp_path, traces_file):
    good = traces_file.read_text(encoding="utf-8").splitlines()
    path = tmp_path / "broken.jsonl"
    path.write_text("\n".join([good[0], "", "{not json", "[1, 2]", *good[1:], '{"name": "trunc']), encoding="utf-8")
    with pytest.warns(UserWarning):
        spans = load_spans(path)
    assert len(spans) == len(good)


# ------------------------------------------------------------------ 安全


def test_xss_payload_cannot_escape_script_tag():
    payload = "</script><script>alert(1)</script><!--    & <img src=x onerror=alert(2)>"
    tracer = Tracer()
    with tracer.span("agent.run", **{"user_input": payload, payload: "key 也可能是恶意的"}):
        with tracer.span("tool.search_kb", **{"tool.arguments": json.dumps({"q": payload})}):
            pass
    title = "<img src=x onerror=alert(3)></title><script>alert(4)</script>"
    page = render_html(tracer.traces, title=title)
    lower = page.lower()

    # 只有我们自己的两个 <script>：数据块 + 查看器脚本
    assert lower.count("<script") == 2
    assert lower.count("</script") == 2
    assert "alert(1)" not in page.split('id="trace-data">', 1)[0]
    assert "<script>alert(1)" not in page
    assert "<img src=x" not in page
    assert "<!--" not in page
    assert " " not in page and " " not in page
    assert "&lt;img src=x onerror=alert(3)&gt;" in page  # 标题经过 HTML 转义

    # 转义不改变数据本身：浏览器 JSON.parse 后拿到的仍是原始字符串
    data = embedded_data(page)
    root = data["traces"][0]["spans"][0]
    assert root["attrs"]["user_input"] == payload
    assert payload in root["attrs"]
    assert data["title"] == title


def test_viewer_script_never_uses_html_sinks():
    """渲染不可信数据只能用 textContent；这里防止以后有人顺手改成 innerHTML。"""
    from agentkit import viewer

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in viewer._JS
    assert "</script" not in viewer._JS.lower() and "<!--" not in viewer._JS


@pytest.mark.skipif(shutil.which("node") is None, reason="没有安装 Node.js")
def test_embedded_script_is_valid_javascript(tmp_path):
    from agentkit import viewer

    js = tmp_path / "viewer.js"
    js.write_text(viewer._JS, encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ------------------------------------------------------------------ 命令行


def test_cli_writes_html(traces_file, tmp_path):
    out = tmp_path / "out" / "trace.html"
    proc = subprocess.run(
        [sys.executable, "-m", "agentkit.viewer", str(traces_file), "-o", str(out), "--title", "CLI 测试"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    page = out.read_text(encoding="utf-8")
    assert "<title>CLI 测试</title>" in page
    assert len(embedded_data(page)["traces"]) == 2
    assert "2 条 trace" in proc.stdout


def test_cli_default_output_and_directory_input(traces_file):
    proc = subprocess.run([sys.executable, "-m", "agentkit.viewer", str(traces_file.parent)], capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert (traces_file.parent / "traces.html").exists()
    proc = subprocess.run([sys.executable, "-m", "agentkit.viewer", str(traces_file)], capture_output=True)
    assert proc.returncode == 0
    assert traces_file.with_suffix(".html").exists()


def test_cli_missing_file(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "agentkit.viewer", str(tmp_path / "nope.jsonl")], capture_output=True, text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 2
    assert "找不到" in proc.stderr
