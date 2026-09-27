"""第 27 课练习测试：全部离线、确定，不需要 Temporal 服务器，也不需要安装 temporalio。

运行：make lesson N=27    或    .venv/bin/python -m pytest lessons/27_durable_workflows/test_exercise.py -v
（同目录的 test_integration.py 会用真实的开发服务器跑一遍 workflow，没装 temporalio 时自动跳过。）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
E, S = ex.ApprovalEvent, ex.ApprovalState


def run_events(*events, call_id="c1"):
    state = S(call_id)
    for ev in events:
        state = ex.on_event(state, ev)
    return state


# ---------------------------------------------------------------------------------------------
# (a) retry_policy_for
# ---------------------------------------------------------------------------------------------


def test_read_tools_retry_five_times_regardless_of_idempotent_flag():
    for idem in (True, False):
        p = ex.retry_policy_for("read", idem)
        assert p == {
            "maximum_attempts": 5,
            "initial_interval": 1.0,
            "backoff_coefficient": 2.0,
            "non_retryable_error_types": ["InvalidArguments", "PermissionDenied"],
        }


def test_write_tools_only_retry_when_idempotent():
    assert ex.retry_policy_for("write", False)["maximum_attempts"] == 1  # 不幂等：至多执行一次
    assert ex.retry_policy_for("write", True)["maximum_attempts"] == 3
    assert ex.retry_policy_for("dangerous", False)["maximum_attempts"] == 1
    assert ex.retry_policy_for("dangerous", True)["maximum_attempts"] == 3


def test_retry_policy_rejects_unknown_risk_and_returns_fresh_lists():
    with pytest.raises(ValueError):
        ex.retry_policy_for("admin", True)
    p1 = ex.retry_policy_for("write", True)
    p1["non_retryable_error_types"].append("Oops")  # 调用方改了自己拿到的那份
    assert ex.retry_policy_for("write", True)["non_retryable_error_types"] == ["InvalidArguments", "PermissionDenied"]


# ---------------------------------------------------------------------------------------------
# (b) ApprovalState 状态机
# ---------------------------------------------------------------------------------------------


def test_request_then_decision_is_the_normal_path():
    waiting = run_events(E("request", "c1"))
    assert waiting.status == "waiting" and not ex.is_approved(waiting)
    done = ex.on_event(waiting, E("decision", "c1", approved=True, by="alice"))
    assert done.status == "approved" and done.decided_by == "alice" and ex.is_approved(done)
    assert waiting.status == "waiting"  # 纯函数：旧状态不能被改


def test_decision_arriving_before_the_wait_is_kept_and_applied():
    """乱序：审批 signal 比 workflow 走到等待点还早。决定要先存起来，而不是丢掉。"""
    early = run_events(E("decision", "c1", approved=True, by="alice"))
    assert early.status == "idle" and early.early == (True, "alice")
    done = ex.on_event(early, E("request", "c1"))
    assert done.status == "approved" and done.decided_by == "alice" and done.early is None
    rejected = run_events(E("decision", "c1", approved=False, by="bob"), E("request", "c1"))
    assert rejected.status == "rejected" and rejected.decided_by == "bob"


def test_first_decision_wins_and_duplicates_are_recorded():
    s = run_events(
        E("request", "c1"),
        E("decision", "c1", approved=True, by="alice"),
        E("decision", "c1", approved=False, by="bob"),  # 审批界面重试 / 两个人同时点
    )
    assert s.status == "approved" and s.decided_by == "alice" and s.ignored == ("duplicate:bob",)
    early_dup = run_events(E("decision", "c1", approved=True, by="alice"), E("decision", "c1", approved=True, by="alice"))
    assert early_dup.early == (True, "alice") and early_dup.ignored == ("duplicate:alice",)


def test_rejected_cannot_be_approved_later():
    s = run_events(E("request", "c1"), E("decision", "c1", approved=False, by="alice"), E("decision", "c1", approved=True, by="mallory"))
    assert s.status == "rejected" and s.decided_by == "alice" and not ex.is_approved(s)
    assert s.ignored == ("duplicate:mallory",)


def test_timeout_rejects_and_late_approval_is_ignored():
    s = run_events(E("request", "c1"), E("timeout", "c1"), E("decision", "c1", approved=True, by="alice"))
    assert s.status == "timed_out" and s.decided_by == "system:timeout" and not ex.is_approved(s)
    assert s.ignored == ("late:alice",)
    # 过期的定时器（已经批准之后才触发）不改变结果；重复的 request（比如重放）也不改变结果
    ok = run_events(E("request", "c1"), E("decision", "c1", approved=True, by="alice"), E("timeout", "c1"), E("request", "c1"))
    assert ok.status == "approved" and ok.decided_by == "alice"


def test_events_for_other_calls_are_ignored_and_unknown_kind_raises():
    s = run_events(E("request", "c1"), E("decision", "c9", approved=True, by="alice"))
    assert s.status == "waiting" and s.ignored == ("other_call:c9",)
    assert run_events(E("timeout", "c1")).status == "idle"  # 还没开始等，就没有什么可超时的
    with pytest.raises(ValueError):
        ex.on_event(S("c1"), E("cancel", "c1"))


# ---------------------------------------------------------------------------------------------
# (c) is_deterministic_safe
# ---------------------------------------------------------------------------------------------

BAD = '''
import random
import time
import requests
from datetime import datetime
from uuid import uuid4

async def run(inp):
    started = time.time()
    if random.random() < 0.5:
        pass
    now = datetime.now()
    run_id = uuid4()
    r = requests.get("https://example.com")
    return started
'''


def test_detects_common_nondeterministic_calls():
    assert ex.is_deterministic_safe(BAD) == [
        "line 9: time.time",
        "line 10: random.random",
        "line 12: datetime.datetime.now",
        "line 13: uuid.uuid4",
        "line 14: requests.get",
    ]


def test_resolves_import_aliases():
    src = "import time as t\nimport datetime as dt\nimport urllib.request\n\ndef f():\n    t.sleep(1)\n    dt.datetime.utcnow()\n    urllib.request.urlopen('http://x')\n    open('x.txt')\n"
    assert ex.is_deterministic_safe(src) == [
        "line 6: time.sleep",
        "line 7: datetime.datetime.utcnow",
        "line 8: urllib.request.urlopen",
        "line 9: open",
    ]


def test_workflow_safe_apis_are_allowed():
    src = '''
import asyncio
from temporalio import workflow

@workflow.defn
class Good:
    @workflow.run
    async def run(self) -> str:
        now = workflow.now()
        n = workflow.random().randint(1, 6)
        rid = workflow.uuid4()
        await asyncio.sleep(1)
        await workflow.sleep(1)
        return f"{now} {n} {rid}"
'''
    assert ex.is_deterministic_safe(src) == []


def test_only_workflow_classes_are_checked_when_present():
    """同一个文件里的 activity 可以读时钟、发 HTTP；只有 @workflow.defn 类里的代码受确定性约束。"""
    src = '''
import time
import httpx
from temporalio import activity, workflow

@activity.defn
async def fetch(url: str) -> str:
    started = time.time()
    return httpx.get(url).text

@workflow.defn(name="Agent")
class Agent:
    @workflow.run
    async def run(self) -> float:
        return time.monotonic()
'''
    assert ex.is_deterministic_safe(src) == ["line 15: time.monotonic"]


def test_the_contrib_agent_workflow_passes_the_check():
    """用你的检查器审一遍本课的生产代码：AgentWorkflow 里不应该有任何非确定性调用。"""
    source = (Path(__file__).resolve().parents[2] / "agentkit" / "contrib" / "temporal.py").read_text(encoding="utf-8")
    assert ex.is_deterministic_safe(source) == []
