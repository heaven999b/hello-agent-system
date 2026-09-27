"""第 03 课练习测试。离线、确定性。

运行：make lesson N=03
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/03_tools
"""

from __future__ import annotations

import json

import pytest

from agentkit import Agent, ScriptedLLM, ToolCall, ToolContext, ToolError, ToolRegistry, call_tool, reply
from agentkit.testing import load_exercise

ex = load_exercise(__file__)

ALICE = ToolContext(run_id="r1", call_id="c1", user_id="u_alice")
BOB = ToolContext(run_id="r1", call_id="c1", user_id="u_bob")


@pytest.fixture(autouse=True)
def fresh_db():
    ex.reset_db()
    yield
    ex.reset_db()


def params_of(t) -> dict:
    return t.schema()["function"]["parameters"]


def enum_of(prop: dict) -> list | None:
    """Optional[Literal[...]] 生成的 schema 是 anyOf: [{enum: [...]}, {type: null}]，两种形式都要认。"""
    if "enum" in prop:
        return prop["enum"]
    for sub in prop.get("anyOf", []):
        if "enum" in sub:
            return sub["enum"]
    return None


def error_of(fn) -> str:
    with pytest.raises(ToolError) as info:
        fn()
    return str(info.value)


# ================================================================== ① 说明书：Schema 质量


def test_search_orders_schema_status_is_enum():
    props = params_of(ex.search_orders)["properties"]
    assert "status" in props
    assert enum_of(props["status"]) is not None, "status 应该是 Literal 枚举，而不是自由字符串（TODO 1）"
    assert set(enum_of(props["status"])) == set(ex.ORDER_STATUSES)
    assert props["status"].get("default", "missing") is None, "status 默认应为 None（不过滤）"


def test_search_orders_schema_limit_has_range():
    limit = params_of(ex.search_orders)["properties"]["limit"]
    assert limit.get("minimum") == 1 and limit.get("maximum") == ex.MAX_LIMIT, "limit 要用 ge=1, le=MAX_LIMIT 限定范围"
    assert limit.get("default") == 5


def test_every_parameter_has_a_description():
    for t in (ex.search_orders, ex.cancel_order):
        for name, prop in params_of(t)["properties"].items():
            assert len(prop.get("description", "").strip()) >= 5, f"{t.name}.{name} 缺少描述（用 Annotated + Field(description=...)）"


def test_tool_descriptions_are_real():
    for t in (ex.search_orders, ex.cancel_order):
        desc = t.schema()["function"]["description"]
        assert "TODO" not in desc, f"{t.name} 的工具描述还是占位符，请改写 docstring"
        assert len(desc) >= 20, f"{t.name} 的描述太短：说清 做什么 / 什么时候用 / 返回什么 / 限制"


def test_identity_is_never_a_model_parameter():
    for t in (ex.search_orders, ex.cancel_order):
        props = params_of(t)["properties"]
        assert "ctx" not in props, "ctx 由系统注入，不能暴露给模型"
        assert "user_id" not in props, "身份绝不能由模型填写"
        assert params_of(t).get("additionalProperties") is False


def test_required_fields():
    assert params_of(ex.search_orders).get("required", []) == [], "search_orders 的参数都有默认值"
    assert params_of(ex.cancel_order).get("required") == ["order_id"]


def test_risk_levels():
    assert ex.search_orders.risk == "read"
    assert ex.cancel_order.risk == "write", "取消订单是写操作，要标注 risk='write'（TODO 4）"


# ================================================================== ② search_orders 行为


def test_search_requires_identity_fail_closed():
    error_of(lambda: ex.search_orders(ctx=None))
    error_of(lambda: ex.search_orders(ctx=ToolContext(user_id=None)))


def test_search_only_returns_current_users_orders():
    alice = ex.search_orders(limit=20, ctx=ALICE)
    assert alice["total"] == 7
    assert {o["order_id"] for o in alice["orders"]} == {"A1001", "A1002", "A1003", "A1004", "A1005", "A1006", "A1007"}
    bob = ex.search_orders(limit=20, ctx=BOB)
    assert {o["order_id"] for o in bob["orders"]} == {"B2001", "B2002"}


def test_search_sorted_newest_first_limited_with_note():
    r = ex.search_orders(ctx=ALICE)  # 默认 limit=5
    assert [o["order_id"] for o in r["orders"]] == ["A1004", "A1007", "A1003", "A1002", "A1001"]
    assert r["total"] == 7, "total 是截断前的总数，模型据此知道还有更多"
    assert isinstance(r.get("note"), str) and r["note"].strip(), "结果被截断时要用 note 告诉模型"


def test_search_filters_by_status():
    r = ex.search_orders(status="pending_shipment", ctx=ALICE)
    assert [o["order_id"] for o in r["orders"]] == ["A1007", "A1003"]
    assert r["total"] == 2 and all(o["status"] == "pending_shipment" for o in r["orders"])


def test_search_empty_result():
    r = ex.search_orders(status="cancelled", ctx=BOB)
    assert r["orders"] == [] and r["total"] == 0


def test_search_returns_only_public_fields():
    r = ex.search_orders(limit=20, ctx=ALICE)
    for o in r["orders"]:
        assert set(o) == set(ex.PUBLIC_FIELDS), f"只返回模型需要的字段，多余字段：{set(o) - set(ex.PUBLIC_FIELDS)}"
    dumped = json.dumps(r, ensure_ascii=False)
    for secret in ("cost_price", "warehouse", "risk_score", "u_alice", "WH-"):
        assert secret not in dumped, f"内部信息 {secret!r} 泄露给了模型"


# ================================================================== ③ cancel_order 行为


def test_cancel_pending_shipment_order():
    r = ex.cancel_order(order_id="A1003", ctx=ALICE)
    assert r["order_id"] == "A1003" and r["status"] == "cancelled" and r["message"].strip()
    assert ex.ORDERS["A1003"]["status"] == "cancelled"


def test_cancel_pending_payment_order():
    assert ex.cancel_order(order_id="A1004", ctx=ALICE)["status"] == "cancelled"
    assert ex.ORDERS["A1004"]["status"] == "cancelled"


@pytest.mark.parametrize("order_id", ["A1002", "A1001"])  # shipped / delivered
def test_cancel_shipped_or_delivered_gives_actionable_error(order_id):
    before = ex.ORDERS[order_id]["status"]
    msg = error_of(lambda: ex.cancel_order(order_id=order_id, ctx=ALICE))
    assert "无法取消" in msg and "退货" in msg, "错误信息要告诉模型为什么不行、下一步能做什么"
    assert ex.ORDERS[order_id]["status"] == before


def test_cancel_nonexistent_order():
    error_of(lambda: ex.cancel_order(order_id="Z9999", ctx=ALICE))


def test_cancel_other_users_order_is_indistinguishable_from_missing():
    msg_other = error_of(lambda: ex.cancel_order(order_id="B2001", ctx=ALICE))
    msg_missing = error_of(lambda: ex.cancel_order(order_id="Z9999", ctx=ALICE))
    assert msg_other.replace("B2001", "<ID>") == msg_missing.replace("Z9999", "<ID>"), (
        "'不是你的订单'和'订单不存在'必须说同一句话，否则攻击者能借此探测哪些订单号存在"
    )
    assert "u_bob" not in msg_other
    assert ex.ORDERS["B2001"]["status"] == "pending_shipment", "越权取消必须被阻止"


def test_cancel_is_idempotent():
    assert ex.cancel_order(order_id="A1005", ctx=ALICE)["status"] == "cancelled"  # 本来就是已取消
    ex.cancel_order(order_id="A1003", ctx=ALICE)
    again = ex.cancel_order(order_id="A1003", ctx=ALICE)  # 重复取消：不报错，如实返回
    assert again["status"] == "cancelled" and again["order_id"] == "A1003"


def test_cancel_requires_identity():
    error_of(lambda: ex.cancel_order(order_id="A1003", ctx=None))
    assert ex.ORDERS["A1003"]["status"] == "pending_shipment"


# ================================================================== ④ 经过 ToolRegistry.execute：校验层


@pytest.fixture
def reg():
    return ToolRegistry([ex.search_orders, ex.cancel_order])


def test_registry_rejects_limit_out_of_range(reg):
    r = reg.execute(ToolCall("c1", "search_orders", '{"limit": 100}'), ALICE)
    assert r.error_type == "invalid_args" and "limit" in r.content


def test_registry_rejects_unknown_status(reg):
    r = reg.execute(ToolCall("c1", "search_orders", '{"status": "unshipped"}'), ALICE)
    assert r.error_type == "invalid_args" and "status" in r.content


def test_registry_rejects_identity_smuggling(reg):
    r = reg.execute(ToolCall("c1", "search_orders", '{"user_id": "u_bob"}'), ALICE)
    assert r.error_type == "invalid_args"
    r = reg.execute(ToolCall("c2", "cancel_order", '{"order_id": "B2001", "user_id": "u_bob"}'), ALICE)
    assert r.error_type == "invalid_args"
    assert ex.ORDERS["B2001"]["status"] == "pending_shipment"


def test_registry_missing_required_argument(reg):
    r = reg.execute(ToolCall("c1", "cancel_order", "{}"), ALICE)
    assert r.error_type == "invalid_args" and "order_id" in r.content


def test_registry_business_error_becomes_observation(reg):
    r = reg.execute(ToolCall("c1", "cancel_order", '{"order_id": "A1002"}'), ALICE)
    assert not r.ok and r.error_type == "tool_error" and "退货" in r.content


def test_registry_missing_identity_is_tool_error(reg):
    r = reg.execute(ToolCall("c1", "search_orders", "{}"))  # 没有传 ctx → 没有 user_id
    assert r.error_type == "tool_error"


def test_registry_success_returns_json(reg):
    r = reg.execute(ToolCall("c1", "search_orders", '{"status": "shipped"}'), ALICE)
    assert r.ok
    data = json.loads(r.content)
    assert [o["order_id"] for o in data["orders"]] == ["A1002"]


# ================================================================== ⑤ 端到端：身份来自 Agent 的 metadata


def test_agent_end_to_end_identity_from_metadata():
    llm = ScriptedLLM([
        call_tool("search_orders", status="pending_shipment"),
        call_tool("cancel_order", order_id="A1003"),
        reply("已为你取消订单 A1003（USB-C 扩展坞）。"),
    ])
    res = Agent(llm, [ex.search_orders, ex.cancel_order]).run("取消我还没发货的扩展坞", metadata={"user_id": "u_alice"})
    assert res.ok and res.tools_called() == ["search_orders", "cancel_order"]
    assert ex.ORDERS["A1003"]["status"] == "cancelled"
    sent = json.dumps(llm.calls[0]["tools"], ensure_ascii=False)
    assert "ctx" not in sent and "user_id" not in sent


def test_agent_cannot_cancel_someone_elses_order():
    llm = ScriptedLLM([call_tool("cancel_order", order_id="A1003"), reply("抱歉，没有找到这个订单。")])
    res = Agent(llm, [ex.search_orders, ex.cancel_order]).run("取消 A1003", metadata={"user_id": "u_bob"})
    tool_msg = next(m for m in res.messages if m["role"] == "tool")
    expected = error_of(lambda: ex.cancel_order(order_id="A1003", ctx=BOB))  # 必须是 ToolError，而不是其他异常
    assert tool_msg["content"] == f"错误：{expected}"
    assert ex.ORDERS["A1003"]["status"] == "pending_shipment"
