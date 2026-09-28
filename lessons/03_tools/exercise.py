"""第 03 课练习：为电商客服 Agent 设计两个工具。

背景：你在给一个电商平台做客服 Agent。用户登录后可以问"我的订单到哪了""帮我取消订单"。
你要写两个工具：
    search_orders   查询当前用户的订单（只读）
    cancel_order    取消当前用户的一个订单（写操作）

这个练习有两半，同样重要：
  ① "说明书"：类型注解 + 描述 —— 它们会被自动转成 JSON Schema 发给模型，模型只能看到这些；
  ② "实现"：身份、越权、错误信息、返回值设计。

TODO 清单：
  TODO 1  OrderStatus：把 str 改成 Literal 枚举
  TODO 2  search_orders 的参数说明书和工具描述
  TODO 3  search_orders 的实现
  TODO 4  cancel_order 的风险等级、参数说明书和工具描述
  TODO 5  cancel_order 的实现

验证：make lesson N=03   或   .venv/bin/python -m pytest lessons/03_tools
随时查看你的工具在模型眼中的样子：.venv/bin/python lessons/03_tools/exercise.py

两个工具都写成普通 def 就好：它们只读写内存里的字典，不等任何 I/O，不需要 async
（agentkit 会把同步工具放进线程池执行）。要调 HTTP API / 数据库的工具才值得写成 async def，见 README 2.7 节。
测试里经过注册表和 Agent 的用例是 async 的：await registry.execute(...)、await agent.run(...)。
"""

from __future__ import annotations

import copy
from typing import Annotated, Literal  # noqa: F401  完成 TODO 时会用到

from pydantic import Field  # noqa: F401  完成 TODO 时会用到

from agentkit import ToolContext, ToolError, tool  # noqa: F401

# ---------------------------------------------------------------------------
# 内存假数据库（已提供，不需要修改）
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
# TODO 1：订单状态枚举
# ---------------------------------------------------------------------------
# 现在 status 是自由字符串，模型只能猜 "unshipped"、"未发货"、"pending"……猜错了就查不到数据（沉默的失败）。
# 把它改成 Literal 枚举，取值就是 ORDER_STATUSES 里的 5 个。

OrderStatus = str  # TODO 1：改成 Literal["pending_payment", ...]


# ---------------------------------------------------------------------------
# TODO 2 + TODO 3：search_orders
# ---------------------------------------------------------------------------
# TODO 2 —— 说明书（测试会检查生成的 JSON Schema，用 search_orders.schema() 查看）：
#   - status：用 Annotated[OrderStatus | None, Field(description=...)]，写清每个状态值的含义；默认 None
#   - limit ：用 Annotated[int, Field(ge=1, le=MAX_LIMIT, description=...)]；默认 5
#   - ctx   ：保持这个参数名，agentkit 会自动把它从 schema 中去掉，并在运行时注入可信身份
#   - 工具描述（函数的 docstring）：做什么 / 什么时候用 / 返回什么 / 有什么限制。不能再包含 "TODO"
#     注意：docstring 会原样发给模型，所以练习说明写在这里的注释里，而不是 docstring 里。
#
# TODO 3 —— 行为要求（测试会逐条检查）：
#   1. 身份只从 ctx 取：ctx 为 None 或 ctx.user_id 为空 → raise ToolError（失败即关闭，绝不"默认查全部"）
#   2. 只返回 user_id == ctx.user_id 的订单；status 不为 None 时再按状态过滤
#   3. 按 created_at 从新到旧排序，最多返回 limit 条
#   4. 返回 {"orders": [...], "total": 符合条件的总数（截断前）}
#      每条订单只包含 PUBLIC_FIELDS 里的 5 个字段 —— 绝不能带 user_id、cost_price、warehouse 等内部字段
#   5. 如果 total 大于实际返回的条数，额外返回 "note"：一句话告诉模型结果被截断了、怎样查到更多


@tool
def search_orders(
    status: OrderStatus | None = None,  # TODO 2：改成 Annotated[..., Field(description=...)]
    limit: int = 5,  # TODO 2：加上 ge / le 范围和描述
    ctx: ToolContext | None = None,
) -> dict:
    """TODO 2：把这一行改写成写给模型看的工具描述。"""
    raise NotImplementedError("TODO 3: 实现 search_orders —— 身份来自 ctx、只查本人、过滤、排序、只返回公开字段")


# ---------------------------------------------------------------------------
# TODO 4 + TODO 5：cancel_order
# ---------------------------------------------------------------------------
# TODO 4 —— 说明书：
#   - 这是写操作：@tool(risk="write")（第 09 课的权限系统会据此决定是否需要审批）
#   - order_id：用 Annotated[str, Field(description=...)]，给出格式示例，并告诉模型不知道订单号时该怎么办
#   - 工具描述：做什么 / 哪些状态可以取消 / 这是写操作，只在用户明确要求时调用
#
# TODO 5 —— 行为要求：
#   1. 身份同上：拿不到 ctx.user_id → raise ToolError
#   2. 订单不存在，或者订单不属于当前用户 → raise ToolError，
#      而且两种情况的错误措辞必须完全相同（订单号除外）。想一想：如果措辞不同，攻击者能借此得到什么信息？
#   3. 已经是 cancelled → 不报错（幂等），返回 {"order_id": ..., "status": "cancelled", "message": ...}
#   4. shipped / delivered → raise ToolError，消息中必须包含 "无法取消" 和 "退货"
#      （错误信息是写给模型看的：要让它知道下一步该怎么跟用户说）
#   5. pending_payment / pending_shipment → 把 ORDERS 里该订单的 status 改为 "cancelled"，
#      返回 {"order_id": ..., "status": "cancelled", "message": 给用户的一句说明}


@tool  # TODO 4：标注风险等级
def cancel_order(
    order_id: str,  # TODO 4：加上描述
    ctx: ToolContext | None = None,
) -> dict:
    """TODO 4：把这一行改写成写给模型看的工具描述。"""
    raise NotImplementedError("TODO 5: 实现 cancel_order —— 越权检查、状态校验、可行动的错误信息、幂等")


if __name__ == "__main__":  # 查看你的工具在模型眼中的样子：.venv/bin/python lessons/03_tools/exercise.py
    import json

    for t in (search_orders, cancel_order):
        print(f"# {t.name}   risk={t.risk}")
        print(json.dumps(t.schema(), ensure_ascii=False, indent=2))
