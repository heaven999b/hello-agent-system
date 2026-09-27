"""第 02 课参考答案：电商客服的两个工具。接口（函数名、参数名、默认值、返回格式）与 exercise.py 完全一致。

对照要点：
- 类型注解就是写给模型的说明书：Annotated + Field(description=...) / Literal 枚举 / ge、le 范围；
- 函数 docstring 就是工具描述：做什么、什么时候用、返回什么、有什么限制；
- 身份只从 ctx 来，缺失时"失败即关闭"（fail closed）；
- 返回值只挑模型需要的字段，带上 order_id；
- 业务错误用 ToolError，写成模型能据此行动的话；"不存在"和"不是你的"说同一句话。
"""

from __future__ import annotations

import copy
from typing import Annotated, Literal

from pydantic import Field

from agentkit import ToolContext, ToolError, tool

# ---------------------------------------------------------------------------
# 内存假数据库（与 exercise.py 相同）
# ---------------------------------------------------------------------------

ORDER_STATUSES = ("pending_payment", "pending_shipment", "shipped", "delivered", "cancelled")
PUBLIC_FIELDS = ("order_id", "item", "status", "amount", "created_at")  # 允许返回给模型的字段
MAX_LIMIT = 20

_FIXTURE: dict[str, dict] = {
    o["order_id"]: o
    for o in [
        # 每行都带着内部字段：user_id、成本价、仓库、风控分 —— 这些绝不能出现在工具返回值里
        {"order_id": "A1001", "user_id": "u_alice", "item": "机械键盘", "amount": 399.0, "status": "delivered",
         "created_at": "2026-08-02", "cost_price": 210.0, "warehouse": "WH-SH-03", "risk_score": 0.02},
        {"order_id": "A1002", "user_id": "u_alice", "item": "显示器支架", "amount": 159.0, "status": "shipped",
         "created_at": "2026-09-10", "cost_price": 72.0, "warehouse": "WH-SH-03", "risk_score": 0.01},
        {"order_id": "A1003", "user_id": "u_alice", "item": "USB-C 扩展坞", "amount": 289.0, "status": "pending_shipment",
         "created_at": "2026-09-20", "cost_price": 131.0, "warehouse": "WH-GZ-01", "risk_score": 0.03},
        {"order_id": "A1004", "user_id": "u_alice", "item": "降噪耳机", "amount": 1299.0, "status": "pending_payment",
         "created_at": "2026-09-25", "cost_price": 802.0, "warehouse": "WH-GZ-01", "risk_score": 0.20},
        {"order_id": "A1005", "user_id": "u_alice", "item": "鼠标垫", "amount": 39.0, "status": "cancelled",
         "created_at": "2026-07-15", "cost_price": 9.0, "warehouse": "WH-SH-03", "risk_score": 0.01},
        {"order_id": "A1006", "user_id": "u_alice", "item": "笔记本电脑包", "amount": 199.0, "status": "delivered",
         "created_at": "2026-06-30", "cost_price": 88.0, "warehouse": "WH-BJ-02", "risk_score": 0.02},
        {"order_id": "A1007", "user_id": "u_alice", "item": "人体工学椅", "amount": 1899.0, "status": "pending_shipment",
         "created_at": "2026-09-22", "cost_price": 1105.0, "warehouse": "WH-BJ-02", "risk_score": 0.05},
        {"order_id": "B2001", "user_id": "u_bob", "item": "咖啡机", "amount": 899.0, "status": "pending_shipment",
         "created_at": "2026-09-21", "cost_price": 530.0, "warehouse": "WH-SH-03", "risk_score": 0.02},
        {"order_id": "B2002", "user_id": "u_bob", "item": "电竞显示器", "amount": 2399.0, "status": "shipped",
         "created_at": "2026-09-18", "cost_price": 1650.0, "warehouse": "WH-GZ-01", "risk_score": 0.04},
    ]
}

ORDERS: dict[str, dict] = {}


def reset_db() -> None:
    """把数据库恢复到初始状态（测试之间互不影响）。"""
    ORDERS.clear()
    ORDERS.update(copy.deepcopy(_FIXTURE))


reset_db()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _current_user(ctx: ToolContext | None) -> str:
    """身份只能来自系统注入的 ctx。拿不到就拒绝（fail closed），绝不"默认查全部"。"""
    if ctx is None or not ctx.user_id:
        raise ToolError("无法确认当前用户身份，不能访问订单。请提示用户重新登录后再试。")
    return ctx.user_id


def _public(order: dict) -> dict:
    return {k: order[k] for k in PUBLIC_FIELDS}


# ---------------------------------------------------------------------------
# 工具 1：search_orders
# ---------------------------------------------------------------------------

OrderStatus = Literal["pending_payment", "pending_shipment", "shipped", "delivered", "cancelled"]


@tool
def search_orders(
    status: Annotated[
        OrderStatus | None,
        Field(description=(
            "按订单状态过滤：pending_payment=待付款；pending_shipment=已付款、待发货；shipped=已发货、运输中；"
            "delivered=已签收；cancelled=已取消。不传则返回所有状态。"
        )),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description=f"最多返回几条，按下单时间从新到旧，默认 5，最大 {MAX_LIMIT}")] = 5,
    ctx: ToolContext | None = None,
) -> dict:
    """查询当前登录用户自己的订单，按下单时间从新到旧排列。只能查询本人的订单，无法查询其他用户。
    适用于：用户询问"我的订单""还没发货的订单""最近买了什么"，或需要先拿到订单号（order_id）再做取消等操作时。
    返回：orders（每条含 order_id、商品名、状态、金额、下单日期）和 total（符合条件的订单总数）；结果不完整时附带 note 提示。"""
    user_id = _current_user(ctx)
    matched = [o for o in ORDERS.values() if o["user_id"] == user_id and (status is None or o["status"] == status)]
    matched.sort(key=lambda o: o["created_at"], reverse=True)
    shown = matched[:limit]
    result: dict = {"orders": [_public(o) for o in shown], "total": len(matched)}
    if len(matched) > len(shown):
        result["note"] = (f"只显示了最近 {len(shown)} 条，共 {len(matched)} 条。"
                          f"可以用 status 过滤，或调大 limit（最大 {MAX_LIMIT}）。")
    elif not matched:
        result["note"] = "没有符合条件的订单。如果按状态过滤了，可以去掉 status 再查一次确认。"
    return result


# ---------------------------------------------------------------------------
# 工具 2：cancel_order
# ---------------------------------------------------------------------------

_CANCELLABLE = {"pending_payment", "pending_shipment"}


@tool(risk="write")
def cancel_order(
    order_id: Annotated[str, Field(
        min_length=1,
        description="要取消的订单号，例如 A1003。不确定订单号时，先调用 search_orders 查询，不要猜。",
    )],
    ctx: ToolContext | None = None,
) -> dict:
    """取消当前登录用户自己的一个订单。只有待付款（pending_payment）和待发货（pending_shipment）的订单可以取消；
    已发货或已签收的订单无法取消，需要走退货流程。这是写操作：只在用户明确要求取消某个订单时调用。
    返回取消后的订单号、状态和给用户的说明。"""
    user_id = _current_user(ctx)
    order = ORDERS.get(order_id)
    # "不存在"和"不是你的"必须说同一句话：否则攻击者可以借此探测哪些订单号真实存在
    if order is None or order["user_id"] != user_id:
        raise ToolError(f"未找到订单 {order_id}。请确认订单号是否正确，可以先调用 search_orders 查看用户的订单。")

    status = order["status"]
    if status == "cancelled":  # 幂等：重复取消不报错，如实返回当前状态
        return {"order_id": order_id, "status": "cancelled", "message": "该订单此前已经取消，无需重复操作。"}
    if status not in _CANCELLABLE:
        raise ToolError(
            f"订单 {order_id} 当前状态为 {status}（已发货或已签收），无法取消。"
            "请告诉用户：可以在签收后 7 天内申请退货。"
        )

    order["status"] = "cancelled"
    refund = "款项将在 1-3 个工作日内原路退回。" if status == "pending_shipment" else "该订单尚未付款，无需退款。"
    return {"order_id": order_id, "status": "cancelled", "message": f"订单 {order_id}（{order['item']}）已取消，{refund}"}


if __name__ == "__main__":  # 查看你的工具在模型眼中的样子：.venv/bin/python lessons/02_tools/exercise.py
    import json

    for t in (search_orders, cancel_order):
        print(f"# {t.name}   risk={t.risk}")
        print(json.dumps(t.schema(), ensure_ascii=False, indent=2))
