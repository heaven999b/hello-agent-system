"""第 06 课练习测试：离线、确定性。

运行：make lesson N=06    或    .venv/bin/python -m pytest lessons/06_security -v
"""

from __future__ import annotations

import pytest

from agentkit import Agent, ScriptedLLM, call_tool, reply, tool
from agentkit.testing import load_exercise

ex = load_exercise(__file__)

# =====================================================================
# (a) PolicyEngine
# =====================================================================


def _engine(**kwargs):
    role_tools = {
        "employee": {"search_kb", "create_ticket"},
        "it_admin": {"*"},
        "contractor": {"search_kb", "export_data"},
    }
    return ex.PolicyEngine(role_tools=role_tools, **kwargs)


def test_read_tools_allowed_for_whitelisted_role_on_every_plan():
    e = _engine()
    for plan in ["free", "pro", "enterprise"]:
        assert e.decide(["employee"], "search_kb", "read", plan) == "allow"


def test_default_deny_for_missing_or_unknown_roles():
    e = _engine()
    assert e.decide([], "search_kb", "read", "enterprise") == "deny"
    assert e.decide(None, "search_kb", "read", "enterprise") == "deny"
    assert e.decide(["intern"], "search_kb", "read", "enterprise") == "deny"  # 未知角色 = 无权限
    assert e.decide(["employee"], "reset_password", "dangerous", "enterprise") == "deny"  # 不在白名单


def test_plan_times_risk_matrix():
    e = _engine()
    assert e.decide(["employee"], "create_ticket", "write", "free") == "ask"
    assert e.decide(["employee"], "create_ticket", "write", "pro") == "allow"
    assert e.decide(["it_admin"], "reset_password", "dangerous", "pro") == "ask"
    assert e.decide(["it_admin"], "reset_password", "dangerous", "enterprise") == "ask"
    assert e.decide(["it_admin"], "reset_password", "dangerous", "free") == "deny"  # 免费版禁止危险操作


def test_unknown_risk_and_unknown_plan_fail_closed():
    e = _engine()
    assert e.decide(["it_admin"], "mystery_tool", "unknown", "enterprise") == "deny"
    assert e.decide(["it_admin"], "mystery_tool", "READ", "enterprise") == "deny"  # 大小写不对也不认
    # 未知套餐按 free 处理
    assert e.decide(["it_admin"], "reset_password", "dangerous", "trial") == "deny"
    assert e.decide(["employee"], "create_ticket", "write", "trial") == "ask"


def test_global_deny_beats_admin_wildcard():
    e = _engine(deny_tools={"export_data"})
    assert e.decide(["it_admin"], "export_data", "read", "enterprise") == "deny"
    assert e.decide(["it_admin"], "search_kb", "read", "enterprise") == "allow"


def test_explicit_role_deny_beats_allow_from_other_roles():
    """一个人同时是 it_admin 和 contractor：contractor 被显式禁止导出，admin 的 "*" 也救不回来。"""
    e = _engine(role_deny={"contractor": {"export_data"}, "suspended": {"*"}})
    assert e.decide(["it_admin", "contractor"], "export_data", "read", "enterprise") == "deny"
    assert e.decide(["it_admin", "contractor"], "search_kb", "read", "enterprise") == "allow"
    # 冻结账号：什么都不能做
    assert e.decide(["it_admin", "suspended"], "search_kb", "read", "enterprise") == "deny"


def test_roles_are_unioned():
    e = _engine()
    # employee 没有 export_data，contractor 有 → 并集后有
    assert e.decide(["employee", "contractor"], "export_data", "read", "pro") == "allow"
    assert e.decide(["employee", "contractor"], "create_ticket", "write", "pro") == "allow"


@tool
def search_kb(query: str) -> str:
    """搜索知识库"""
    return f"关于 {query} 的文章"


@tool(risk="write")
def create_ticket(title: str) -> str:
    """创建工单"""
    return f"工单已创建：{title}"


@tool(risk="dangerous")
def reset_password(username: str) -> str:
    """重置密码"""
    return f"{username} 的密码已重置"


def test_policy_hook_in_agent_hides_denied_tools_and_pauses_on_ask():
    tools = [search_kb, create_ticket, reset_password]
    llm = ScriptedLLM([call_tool("create_ticket", title="打印机坏了"), reply("工单已提交")])
    agent = Agent(llm, tools, hooks=[ex.PolicyHook(_engine(), tools)])
    res = agent.run("报修", metadata={"roles": ["employee"], "tenant_plan": "free"})
    # 员工看不到 reset_password
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == ["search_kb", "create_ticket"]
    # 免费版的写操作 → ask → 暂停等审批
    assert res.status == "paused" and res.pending_approval.name == "create_ticket"
    res2 = agent.approve(res.run_id, approved=True)
    assert res2.ok and any(m["role"] == "tool" and "工单已创建" in m["content"] for m in res2.messages)


def test_policy_hook_blocks_invisible_tool_called_anyway():
    """模型被注入诱导，去调用一个它"看不见"的工具：before_tool 这道保险要拦住。"""
    tools = [search_kb, create_ticket, reset_password]
    llm = ScriptedLLM([call_tool("reset_password", username="ceo"), reply("好的")])
    res = Agent(llm, tools, hooks=[ex.PolicyHook(_engine(), tools)]).run(
        "x", metadata={"roles": ["employee"], "tenant_plan": "enterprise"}
    )
    assert res.ok and res.messages[3]["content"].startswith("拒绝")


# =====================================================================
# (b) redact
# =====================================================================


def test_redact_ipv4():
    assert ex.redact("服务器 192.168.1.100 宕机了") == "服务器 [IP已脱敏] 宕机了"
    assert ex.redact("访问 8.8.8.8.") == "访问 [IP已脱敏]."  # 句末英文句号
    assert ex.redact("IP：10.0.0.1。") == "IP：[IP已脱敏]。"
    assert ex.redact("0.0.0.0 和 255.255.255.255") == "[IP已脱敏] 和 [IP已脱敏]"
    for not_ip in ["999.1.1.1", "1.2.3", "1.2.3.4.5", "v1.2.3.4", "01.02.03.04", "256.1.1.1"]:
        assert ex.redact(not_ip) == not_ip, not_ip


def test_redact_license_plates():
    assert ex.redact("车牌京A12345违停") == "车牌[车牌号已脱敏]违停"
    assert ex.redact("粤B·6K789") == "[车牌号已脱敏]"
    assert ex.redact("新能源车 沪AD12345 已入库") == "新能源车 [车牌号已脱敏] 已入库"
    assert ex.redact("苏E 88888") == "[车牌号已脱敏]"
    for not_plate in ["新款iPhone 15", "北京A区", "湘A12345678", "京ABCDEF", "京I12345", "工号 A12345"]:
        assert ex.redact(not_plate) == not_plate, not_plate


def test_redact_labeled_names():
    assert ex.redact("姓名：张三，部门：研发") == "姓名：[姓名已脱敏]，部门：研发"
    assert ex.redact("联系人: 李四") == "联系人: [姓名已脱敏]"
    assert ex.redact("收件人 ：王小明\n地址：xx") == "收件人 ：[姓名已脱敏]\n地址：xx"
    assert ex.redact("客户姓名：欧阳娜娜") == "客户姓名：[姓名已脱敏]"
    assert ex.redact("患者：买买提·艾力") == "患者：[姓名已脱敏]"
    assert ex.redact("张三说他明天来") == "张三说他明天来"  # 没有标签就不猜


def test_redact_keeps_base_pii_rules():
    out = ex.redact("持卡人：赵六，手机 13812345678，邮箱 zl@corp.com，登录 IP 172.16.0.8")
    assert out == "持卡人：[姓名已脱敏]，手机 [手机号已脱敏]，邮箱 [邮箱已脱敏]，登录 IP [IP已脱敏]"


def test_redact_does_not_touch_ordinary_numbers_and_dates():
    text = "会议 2025-06-16 14:30:00 开始，预算 12345.67 元，版本 v2.3.1，房间 305，工号 A12345，体温 36.5 度，京东 618 大促"
    assert ex.redact(text) == text


def test_redact_is_idempotent():
    text = "姓名：张三，车牌京A12345，IP 10.1.2.3，电话 13912345678"
    once = ex.redact(text)
    assert ex.redact(once) == once
    assert "张三" not in once and "京A12345" not in once and "10.1.2.3" not in once


# =====================================================================
# (c) lethal_trifecta
# =====================================================================

P, U, X = ex.READS_PRIVATE_DATA, ex.INGESTS_UNTRUSTED_CONTENT, ex.CAN_EXFILTRATE


def test_no_trifecta_returns_empty_list():
    assert ex.lethal_trifecta([]) == []
    safe = [ex.ToolSpec("query_crm", {P}), ex.ToolSpec("read_email", {P, U})]  # 能读、会被注入，但送不出去
    assert ex.lethal_trifecta(safe) == []
    assert ex.lethal_trifecta([ex.ToolSpec("calculator")]) == []


def test_trifecta_across_tools_lists_providers():
    tools = [
        ex.ToolSpec("query_crm", {P}),
        ex.ToolSpec("read_email", {P, U}),
        ex.ToolSpec("send_email", {X}),
        ex.ToolSpec("calculator"),
    ]
    out = ex.lethal_trifecta(tools)
    assert len(out) == 5 and "致命三要素" in out[0]
    assert P in out[1] and "query_crm, read_email" in out[1]
    assert U in out[2] and out[2].endswith("read_email")
    assert X in out[3] and out[3].endswith("send_email")
    assert "calculator" not in "".join(out)
    # read_email 是不可信内容的唯一来源，send_email 是对外通信的唯一出口；query_crm 不是关键工具
    assert out[4].startswith("建议：") and out[4].endswith("read_email, send_email")


def test_single_tool_can_be_the_whole_trifecta():
    """比如一个"带登录态的浏览器"工具：能看内网页面、会读到外部网页、还能访问任意 URL。"""
    out = ex.lethal_trifecta([ex.ToolSpec("browser", {P, U, X}), ex.ToolSpec("calculator")])
    assert len(out) == 5
    assert all(line.endswith("browser") for line in out[1:4])
    assert out[4].endswith("：browser")


def test_no_single_critical_tool_suggests_splitting_agent():
    tools = [
        ex.ToolSpec("mail_a", {P, U, X}),
        ex.ToolSpec("mail_b", {P, U, X}),
        ex.ToolSpec("mail_a", {P, U, X}),  # 重复的工具名只算一次
    ]
    out = ex.lethal_trifecta(tools)
    assert out[1].endswith("mail_a, mail_b")
    assert out[4].startswith("建议：") and "拆分" in out[4]


def test_toolspec_rejects_unknown_labels():
    assert len(ex.lethal_trifecta([ex.ToolSpec("agent_browser", {P, U, X})])) == 5  # 合法标签正常工作
    with pytest.raises(ValueError):
        ex.ToolSpec("x", {"can_exfiltate"})  # 拼写错误必须报错，而不是被静默忽略
