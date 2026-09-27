"""第 16 课练习测试：离线、确定性（哈希是确定的，模型是剧本，不调用任何真实服务）。

运行：make lesson N=16    或    .venv/bin/python -m pytest lessons/16_release_ops -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit import Agent, PermissionPolicy, ScriptedLLM, call_tool, reply, tool
from agentkit.testing import load_exercise

ex = load_exercise(__file__)

ROOT = Path(__file__).resolve().parents[2]
USERS = [f"user-{i:05d}" for i in range(10_000)]

# =====================================================================
# (a) bucket
# =====================================================================


def test_bucket_range_type_and_repeatability():
    for u in USERS[:1000]:
        b = ex.bucket(u, "itbuddy.system:v2")
        assert isinstance(b, int) and 0 <= b <= 99
        assert ex.bucket(u, "itbuddy.system:v2") == b, "同一个用户、同一个 salt，结果必须每次都一样"


def test_bucket_is_stable_across_processes():
    """服务重启、请求落到另一台机器：用户不能换桶（所以不能用内置 hash()）。"""
    ids = ["alice", "bob", "user-00042", "张三"]
    here = [ex.bucket(u, "rollout-7") for u in ids]
    code = (
        "from agentkit.testing import load_exercise\n"
        f"ex = load_exercise({str(Path(__file__).resolve())!r})\n"
        f"print([ex.bucket(u, 'rollout-7') for u in {ids!r}])\n"
    )
    for seed in ("1", "2024"):  # 两个不同的哈希种子，模拟两次重启
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "PYTHONHASHSEED": seed}, cwd=ROOT, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == str(here), "换了进程之后桶号变了：是不是用了内置 hash()？"


def test_bucket_is_roughly_uniform():
    counts = [0] * 100
    for u in USERS:
        counts[ex.bucket(u, "salt-A")] += 1
    # 10000 个用户、100 个桶，期望每桶 100 个；好的哈希几乎不可能偏离到这个范围之外
    assert min(counts) >= 50 and max(counts) <= 160, f"分布不均匀：最少 {min(counts)}，最多 {max(counts)}"


def test_bucket_salts_are_independent():
    """两次发布各取 10% 的用户：如果分法相互独立，两次都被选中的应该约为 1%，而不是 10%。"""
    in_a = {u for u in USERS if ex.bucket(u, "release-A") < 10}
    in_b = {u for u in USERS if ex.bucket(u, "release-B") < 10}
    assert 700 <= len(in_a) <= 1300 and 700 <= len(in_b) <= 1300
    overlap = len(in_a & in_b) / len(USERS)
    assert 0.002 <= overlap <= 0.03, f"两次发布选中的用户重叠 {overlap:.1%}，salt 没有起作用"


# =====================================================================
# (b) pick_version
# =====================================================================


def test_pick_version_without_rollout_returns_stable():
    assert ex.pick_version("alice", ex.Rollout(stable=3)) == 3
    zero = ex.Rollout(stable=3, candidate=4, percent=0, salt="p:v4")
    assert all(ex.pick_version(u, zero) == 3 for u in USERS[:500])
    full = ex.Rollout(stable=3, candidate=4, percent=100, salt="p:v4")
    assert all(ex.pick_version(u, full) == 4 for u in USERS[:500])


def test_pick_version_follows_bucket_with_rollout_salt():
    r = ex.Rollout(stable=1, candidate=2, percent=10, salt="itbuddy.system:v2")
    on_candidate = 0
    for u in USERS[:2000]:
        expected = 2 if ex.bucket(u, r.salt) < 10 else 1
        got = ex.pick_version(u, r)
        assert got == expected, f"{u}：桶号 {ex.bucket(u, r.salt)}，期望 v{expected}，实际 v{got}"
        on_candidate += got == 2
    assert 140 <= on_candidate <= 260, f"10% 灰度实际分到了 {on_candidate / 20:.1f}%"


def test_expanding_percent_never_kicks_users_back():
    """1% → 10% → 50%：已经在新版本上的用户，扩量后必须还在新版本上（粘性 + 单调）。"""
    groups = []
    for p in (1, 10, 50):
        r = ex.Rollout(stable=1, candidate=2, percent=p, salt="p:v2")
        groups.append({u for u in USERS if ex.pick_version(u, r) == 2})
    assert groups[0] <= groups[1] <= groups[2]
    assert len(groups[0]) < len(groups[1]) < len(groups[2])


def test_force_lists_priority():
    r = ex.Rollout(stable=1, candidate=2, percent=0, salt="p:v2",
                   force_candidate=frozenset({"dogfood", "vip"}), force_stable=frozenset({"vip"}))
    assert ex.pick_version("dogfood", r) == 2, "percent=0 时，内部员工也应该先用上新版本（第 0 阶段）"
    assert ex.pick_version("vip", r) == 1, "force_stable 优先级最高"
    r100 = ex.Rollout(stable=1, candidate=2, percent=100, salt="p:v2", force_stable=frozenset({"vip"}))
    assert ex.pick_version("vip", r100) == 1
    assert ex.pick_version("dogfood", ex.Rollout(stable=1, force_candidate=frozenset({"dogfood"}))) == 1, \
        "没有进行中的灰度时，谁都只能用 stable"


# =====================================================================
# (c) rollout_decision
# =====================================================================

BASE = ex.StageMetrics(requests=5000, success_rate=0.90, error_rate=0.01, p95_latency_ms=8000, cost_per_task=0.02)
T = ex.Thresholds(min_requests=200, max_error_rate=0.05, max_success_drop=0.03, max_latency_ratio=1.25, max_cost_ratio=1.30)


def stage(**changes) -> "ex.StageMetrics":
    values = dict(requests=1000, success_rate=0.90, error_rate=0.01, p95_latency_ms=8000, cost_per_task=0.02)
    values.update(changes)
    return ex.StageMetrics(**values)


def test_decision_advance_when_healthy():
    assert ex.rollout_decision(stage(), BASE, T) == "advance"
    assert ex.rollout_decision(stage(success_rate=0.92, p95_latency_ms=7000), BASE, T) == "advance"


def test_decision_safety_incident_rolls_back_even_with_tiny_sample():
    assert ex.rollout_decision(stage(requests=3, safety_incidents=1), BASE, T) == "rollback"


def test_decision_holds_when_sample_too_small():
    # 30 个请求里错了 3 个 = 10%，看起来很吓人，但样本太小，只能继续观察
    assert ex.rollout_decision(stage(requests=30, error_rate=0.10, success_rate=0.80), BASE, T) == "hold"


def test_decision_rolls_back_on_quality_regression():
    assert ex.rollout_decision(stage(error_rate=0.08), BASE, T) == "rollback"
    assert ex.rollout_decision(stage(success_rate=0.85), BASE, T) == "rollback"


def test_decision_boundaries_are_strict():
    base = ex.StageMetrics(requests=5000, success_rate=0.75, error_rate=0.0, p95_latency_ms=1000, cost_per_task=0.5)
    t = ex.Thresholds(min_requests=200, max_error_rate=0.25, max_success_drop=0.25, max_latency_ratio=1.5, max_cost_ratio=2.0)
    exactly = ex.StageMetrics(requests=200, success_rate=0.5, error_rate=0.25, p95_latency_ms=1500, cost_per_task=1.0)
    assert ex.rollout_decision(exactly, base, t) == "advance", "恰好等于阈值不算越界（requests 恰好等于 min_requests 也够了）"


def test_decision_holds_on_latency_or_cost_regression_only():
    assert ex.rollout_decision(stage(p95_latency_ms=12000), BASE, T) == "hold"
    assert ex.rollout_decision(stage(cost_per_task=0.03), BASE, T) == "hold"
    # 质量回归优先于成本回归：又贵又差 → 回滚，而不是暂停
    assert ex.rollout_decision(stage(cost_per_task=0.03, success_rate=0.80), BASE, T) == "rollback"


# =====================================================================
# (d) kill switch
# =====================================================================


def test_blocked_reason_global_and_tenant_scopes():
    flags = {"disabled_tools": ["refund"], "tenants": {"acme": {"disabled_tools": {"send_email"}}}}
    r = ex.blocked_reason(flags, "refund", "dangerous", "globex")
    assert isinstance(r, str) and "refund" in r
    r2 = ex.blocked_reason(flags, "send_email", "write", "acme")
    assert isinstance(r2, str) and "send_email" in r2
    assert ex.blocked_reason(flags, "send_email", "write", "globex") is None, "租户级开关不能影响其他租户"
    assert ex.blocked_reason(flags, "send_email", "write", None) is None
    assert ex.blocked_reason(flags, "search_kb", "read", "acme") is None
    assert ex.blocked_reason({}, "refund", "dangerous", "acme") is None, "没有任何开关时一律放行"


def test_blocked_reason_read_only_mode():
    flags = {"read_only": True}
    for name, risk in [("create_ticket", "write"), ("reset_password", "dangerous")]:
        r = ex.blocked_reason(flags, name, risk, "acme")
        assert isinstance(r, str) and name in r and "只读" in r
    assert ex.blocked_reason(flags, "search_kb", "read", "acme") is None, "只读模式下读操作照常可用"
    tenant_only = {"tenants": {"acme": {"read_only": True}}}
    assert "只读" in ex.blocked_reason(tenant_only, "create_ticket", "write", "acme")
    assert ex.blocked_reason(tenant_only, "create_ticket", "write", "globex") is None


def _email_tools(sent: list):
    @tool(risk="write")
    def send_email(to: str, body: str) -> str:
        """给指定地址发送邮件"""
        sent.append(to)
        return f"已发送给 {to}"

    @tool
    def search_kb(query: str) -> str:
        """搜索知识库"""
        return "找到 1 篇文章"

    return [send_email, search_kb]


def test_kill_switch_takes_effect_without_restart():
    sent: list[str] = []
    tools = _email_tools(sent)
    flags = {"disabled_tools": ["send_email"]}  # 模拟配置中心里的一份配置
    switch = ex.KillSwitch(lambda: flags, tools)
    llm = ScriptedLLM([
        call_tool("send_email", to="all@acme.com", body="hi"), reply("抱歉，邮件功能暂时不可用。"),
        call_tool("send_email", to="all@acme.com", body="hi"), reply("已发送。"),
    ])
    agent = Agent(llm, tools, hooks=[switch])

    res = agent.run("给全员发个通知", metadata={"tenant_id": "acme"})
    assert sent == [], "开关打开时，工具绝不能真的执行"
    offered = [t["function"]["name"] for t in (llm.calls[0]["tools"] or [])]
    assert offered == ["search_kb"], "被停用的工具不应再展示给模型"
    denial = next(m for m in res.messages if m["role"] == "tool")
    assert "send_email" in denial["content"]

    flags["disabled_tools"] = []  # 运维在配置中心关掉开关 —— 同一个 Agent 实例，不重启
    res2 = agent.run("给全员发个通知", metadata={"tenant_id": "acme"})
    assert res2.output == "已发送。" and sent == ["all@acme.com"]


@tool(risk="dangerous")
def refund(order_id: str) -> str:
    """给订单退款"""
    REFUNDS.append(order_id)
    return f"订单 {order_id} 已退款"


REFUNDS: list[str] = []


def test_kill_switch_blocks_runs_that_were_paused_before_it_was_flipped():
    """开关打开之前就已暂停等审批的运行：审批通过后恢复，也必须被拦下。"""
    REFUNDS.clear()
    flags: dict = {}
    llm = ScriptedLLM([call_tool("refund", order_id="A1"), reply("退款功能暂时停用，已为您转人工。")])
    agent = Agent(llm, [refund], hooks=[PermissionPolicy(), ex.KillSwitch(lambda: flags, [refund])])
    res = agent.run("订单 A1 退款", metadata={"tenant_id": "acme"})
    assert res.status == "paused"

    flags["disabled_tools"] = ["refund"]  # 发现退款工具有漏洞，紧急停用
    final = agent.approve(res.run_id, approved=True)  # 审批人不知情，点了批准
    assert REFUNDS == [], "审批通过也不能绕过紧急开关"
    assert final.status == "completed"
