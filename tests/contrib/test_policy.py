"""agentkit.contrib.policy：Cedar 策略即代码（第 29 课）。策略与 schema 用课程目录里的真实配置文件。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

cedarpy = pytest.importorskip("cedarpy")

from agentkit import Agent, ScriptedLLM, call_tool, reply, tool  # noqa: E402
from agentkit.contrib.policy import CedarPolicy, build_entities, entity_args_context, entity_ref, validate  # noqa: E402
from agentkit.state import RunState  # noqa: E402

CONFIGS = Path(__file__).resolve().parents[2] / "lessons" / "29_gateway_and_guardrails" / "configs"
POLICIES, SCHEMA = CONFIGS / "policies.cedar", CONFIGS / "schema.cedarschema"


@tool
def search_kb(query: str) -> str:
    """搜索知识库"""
    return "VPN 指南"


@tool(risk="dangerous")
def reset_password(target_user_id: str) -> str:
    """重置密码"""
    return f"已为 {target_user_id} 发送重置链接"


@tool(risk="write")
def export_payroll(month: str) -> str:
    """导出工资单"""
    return "ok"


TOOLS = [search_kb, reset_password, export_payroll]
ALICE = {"tenant_id": "acme", "user_id": "alice", "roles": ["employee"], "department": "sales", "tenant_plan": "enterprise"}
IAN = {"tenant_id": "acme", "user_id": "ian", "roles": ["it_admin"], "department": "it", "tenant_plan": "enterprise"}
MALLORY = {"tenant_id": "globex", "user_id": "mallory", "roles": ["it_admin"], "department": "it", "tenant_plan": "enterprise"}
BOB = {"target_user": entity_ref("User", "bob")}


def make_policy(**kwargs) -> CedarPolicy:
    kwargs.setdefault("tools", TOOLS)
    kwargs.setdefault("tool_tenants", {"reset_password": "acme", "export_payroll": "acme"})
    kwargs.setdefault("context_fn", entity_args_context({"reset_password": {"target_user_id": ("target_user", "User")}}))
    return CedarPolicy(POLICIES, SCHEMA, **kwargs)


# ------------------------------------------------------------------ 策略文件本身


def test_lesson_policies_validate_against_schema():
    assert validate(POLICIES, SCHEMA) == []


def test_schema_catches_typos_and_constructor_refuses_invalid_policies():
    bad = 'permit (principal, action == Action::"call_tool", resource) when { principal.rolez.contains("x") };'
    errors = validate(bad, SCHEMA)
    assert errors and "rolez" in errors[0]
    with pytest.raises(ValueError, match="schema"):
        CedarPolicy(bad, SCHEMA)
    with pytest.raises(ValueError):  # 语法错误在启动时就失败，而不是等到第一个请求
        CedarPolicy("permit(principal action, resource);")


# ------------------------------------------------------------------ 判定矩阵


def test_same_request_three_principals_three_outcomes():
    p = make_policy()
    # 普通员工重置别人：参数级 forbid
    d = p.authorize(ALICE, reset_password, "call_tool", BOB)
    assert not d.allowed and p.explain(d) == ["reset-self-only"]
    # IT 管理员：可以调用，但危险操作不能无人值守 → 走审批（默认拒绝，没有策略命中）
    d = p.authorize(IAN, reset_password, "call_tool", BOB)
    assert d.allowed and p.explain(d) == ["it-admin-all-tools"]
    d = p.authorize(IAN, reset_password, "call_tool_unattended", BOB)
    assert not d.allowed and p.explain(d) == [] and "默认拒绝" in p.describe(d)
    # 跨租户的 IT 管理员：permit 命中了，但 forbid 一票否决
    d = p.authorize(MALLORY, reset_password, "call_tool", BOB)
    assert not d.allowed and p.explain(d) == ["tenant-isolation"]


def test_self_service_and_explicit_forbid_beats_permit():
    p = make_policy()
    d = p.authorize(ALICE, reset_password, "call_tool", {"target_user": entity_ref("User", "alice")})
    assert d.allowed and p.explain(d) == ["employee-reset-password"]
    assert p.explain(p.authorize(IAN, export_payroll, "call_tool")) == ["payroll-finance-only"]
    fiona = {**ALICE, "user_id": "fiona", "department": "finance"}
    assert p.authorize(fiona, export_payroll, "call_tool").allowed


def test_free_plan_forbids_dangerous_tools_and_unknown_roles_get_nothing():
    p = make_policy()
    d = p.authorize({**IAN, "tenant_plan": "free"}, reset_password, "call_tool", BOB)
    assert not d.allowed and "free-plan-no-dangerous" in p.explain(d)
    d = p.authorize({**ALICE, "roles": ["intern"]}, search_kb, "call_tool")
    assert not d.allowed and p.explain(d) == []  # 默认拒绝
    d = p.authorize({**ALICE, "roles": "employee"}, search_kb, "call_tool")  # 字符串角色被纠正成列表，而不是字符集合
    assert d.allowed


def test_missing_identity_fails_closed():
    d = make_policy().authorize({"roles": ["it_admin"]}, search_kb, "call_tool")
    assert not d.allowed and d.decision == "NoDecision" and "tenant_id" in d.errors[0]


def test_evaluation_error_fails_closed_even_though_cedar_would_allow():
    """Cedar 的 skip-on-error：漏传 Tenant 实体 → free-plan 那条 forbid 求值出错被跳过 → 裸 Cedar 返回 Allow。"""
    md = {**IAN, "tenant_plan": "free"}
    ents = [e for e in build_entities(md, {"reset_password": {"risk": "dangerous"}}) if e["uid"]["type"] != "Tenant"]
    req = {"principal": {"type": "User", "id": "ian"}, "action": {"type": "Action", "id": "call_tool"},
           "resource": {"type": "Tool", "id": "reset_password"}, "context": {}}
    raw = cedarpy.is_authorized(req, POLICIES.read_text(encoding="utf-8"), ents, SCHEMA.read_text(encoding="utf-8"))
    assert raw.allowed and raw.diagnostics.errors  # 这就是坑
    leaky = make_policy(entities_fn=lambda m, cat: [e for e in build_entities(m, cat) if e["uid"]["type"] != "Tenant"])
    d = leaky.authorize(md, reset_password, "call_tool")
    assert not d.allowed and d.errors and "出错" in leaky.describe(d)


def test_unknown_tool_is_treated_as_dangerous():
    p = make_policy()
    d = p.authorize(ALICE, "brand_new_tool", "call_tool")
    assert not d.allowed  # 员工的 permit 要求 risk != dangerous；未登记的工具按最严处理


# ------------------------------------------------------------------ Hook 行为


def test_visible_tools_filters_per_principal():
    p = make_policy()
    names = [t.name for t in TOOLS]
    assert p.visible_tools(RunState(metadata=ALICE), names) == ["search_kb", "reset_password"]
    assert p.visible_tools(RunState(metadata=IAN), names) == ["search_kb", "reset_password"]
    assert p.visible_tools(RunState(metadata=MALLORY), names) == ["search_kb"]
    assert p.visible_tools(RunState(metadata={}), names) == []


async def test_before_tool_pauses_for_approval_then_resumes_with_audit_trail():
    audit = []
    p = make_policy(audit=audit.append)
    llm = ScriptedLLM([call_tool("reset_password", target_user_id="bob"), reply("已发送")])
    agent = Agent(llm, TOOLS, hooks=[p])
    res = await agent.run("帮 bob 重置密码", metadata=IAN)
    assert res.status == "paused" and "call_tool_unattended" in res.output
    res = await agent.approve(res.run_id, True, by="sec-oncall")
    assert res.ok and res.output == "已发送"
    ids = [(r["action"], r["allowed"], tuple(r["policy_ids"])) for r in audit]
    assert ("call_tool", True, ("it-admin-all-tools",)) in ids and ("call_tool_unattended", False, ()) in ids
    assert all(r["run_id"] == res.run_id and r["user_id"] == "ian" for r in audit)
    assert res.metadata["policy_decisions"][0]["policy_ids"] == ["it-admin-all-tools"]


async def test_denied_call_becomes_observation_and_sync_or_async_approver_is_used():
    llm = ScriptedLLM([call_tool("reset_password", target_user_id="bob"), reply("无法操作")])
    res = await Agent(llm, TOOLS, hooks=[make_policy()]).run("帮 bob 重置密码", metadata=ALICE)
    tool_msg = next(m for m in res.messages if m["role"] == "tool")
    assert res.ok and "reset-self-only" in tool_msg["content"]

    seen = []
    p = make_policy(approver=lambda call, state: seen.append(call.name) or False)  # 普通函数
    llm = ScriptedLLM([call_tool("reset_password", target_user_id="bob"), reply("审批没通过")])
    res = await Agent(llm, TOOLS, hooks=[p]).run("帮 bob 重置密码", metadata=IAN)
    assert seen == ["reset_password"] and "没有批准" in next(m for m in res.messages if m["role"] == "tool")["content"]

    async def reject(call, state):  # async 函数：必须 await 它的结果，不能把协程对象当成"批准"
        await asyncio.sleep(0)
        seen.append(f"async:{call.name}")
        return False

    llm = ScriptedLLM([call_tool("reset_password", target_user_id="bob"), reply("审批没通过")])
    res = await Agent(llm, TOOLS, hooks=[make_policy(approver=reject)]).run("帮 bob 重置密码", metadata=IAN)
    assert seen[-1] == "async:reset_password"
    assert "没有批准" in next(m for m in res.messages if m["role"] == "tool")["content"]  # 被拒绝，工具没有执行


async def test_invalid_arguments_are_not_sent_to_approvers():
    seen = []
    p = make_policy(approver=lambda call, state: seen.append(call) or True)
    llm = ScriptedLLM([call_tool("reset_password", wrong_field="bob"), reply("参数错了")])
    res = await Agent(llm, TOOLS, hooks=[p]).run("重置", metadata=IAN)
    assert seen == [] and "参数校验失败" in next(m for m in res.messages if m["role"] == "tool")["content"]


async def test_concurrent_runs_sharing_one_policy_keep_decisions_apart():
    """30 个运行并发共用一个 CedarPolicy：alice / ian / mallory 轮流，都被"诱导"去重置 bob 的密码。
    async approver 每次都让出事件循环，各会话的判定交错进行 —— 但判定结果、审计记录不能串到别人身上。"""
    audit, approved_for = [], []

    async def approver(call, state):
        await asyncio.sleep(0.001)  # 模拟去审批系统查一条记录：此刻别的会话在推进
        approved_for.append(state.metadata["user_id"])
        return True

    p = make_policy(approver=approver, audit=audit.append)
    llm = ScriptedLLM(
        responder=lambda m: reply("结束") if m[-1]["role"] == "tool" else call_tool("reset_password", target_user_id="bob"),
        latency=0.001,
    )
    people = [ALICE, IAN, MALLORY]
    agent = Agent(llm, TOOLS, hooks=[p])
    results = await asyncio.gather(*(agent.run("帮 bob 重置密码", metadata=people[i % 3]) for i in range(30)))

    assert llm.max_in_flight > 1  # 会话之间真的交错了
    expected = {
        "alice": ("reset-self-only", [["reset-self-only"]]),
        "ian": ("已为 bob 发送重置链接", [["it-admin-all-tools"], []]),  # 能调用，但不能无人值守 → 审批通过后执行
        "mallory": ("tenant-isolation", [["tenant-isolation"]]),
    }
    for i, res in enumerate(results):
        who = people[i % 3]["user_id"]
        marker, policy_ids = expected[who]
        assert res.ok and marker in next(m for m in res.messages if m["role"] == "tool")["content"], who
        assert [d["policy_ids"] for d in res.metadata["policy_decisions"]] == policy_ids, who
        mine = [r for r in audit if r["run_id"] == res.run_id]
        assert mine and all(r["user_id"] == who for r in mine)
    assert sorted(approved_for) == ["ian"] * 10  # 只有 ian 的调用走到了审批
    assert len(p.decisions) == 10 * 1 + 10 * 2 + 10 * 1
