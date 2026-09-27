"""ITBuddy 的工具集。

每个工具都遵循第 02 课的 7 条原则，这里特别注意三点：

1. 身份只来自 ctx：没有任何工具让模型填 tenant_id / user_id / role。
   模型可以被注入攻击操纵，所以"我是谁、我在哪个公司"绝不能由模型说了算。
2. 风险分级驱动治理：read 直接执行；write（建工单）走幂等；dangerous（重置密码）必须人工审批。
3. 工具返回的内容要"为模型设计"：结构化、简短、带下一步提示；敏感信息在源头就不返回。

工具清单与风险分级（详见 DESIGN.md 第 6 节）：
    search_kb            read       检索本租户知识库
    check_system_status  read       查询本租户系统故障公告
    get_my_tickets       read       查询"我"的工单（身份来自 ctx）
    create_ticket        write      创建工单（两层幂等）
    reset_password       dangerous  发起密码重置（审批 + 参数级授权）
    lookup_employee      read       查询员工信息（仅 it_admin，RBAC）
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from agentkit import Tool, ToolContext, ToolError, tool

from .backend import Backend, mask_phone
from .policies import check_reset_permission


def make_tools(backend: Backend) -> list[Tool]:
    """用闭包把后端"注入"给工具（和 agentkit.memory_tools(store) 同一个模式），方便测试时换成全新的后端。"""

    def _who(ctx: ToolContext) -> tuple[str, str]:
        if not ctx.tenant_id or not ctx.user_id:
            raise ToolError("当前会话没有经过认证的用户身份，无法访问企业数据。")
        return ctx.tenant_id, ctx.user_id

    # ------------------------------------------------------------------ read

    @tool(timeout_s=5)
    def search_kb(
        query: Annotated[str, Field(min_length=1, max_length=200, description="检索关键词，例如“VPN 错误 809”“打印机 卡纸”")],
        ctx: ToolContext,
    ) -> str:
        """在当前公司的 IT 知识库中检索操作指南和常见问题。回答"怎么做 / 为什么 / 报错怎么办"之类的问题前先调用它。返回最相关的至多 3 篇文章全文。"""
        tenant, _ = _who(ctx)
        hits = backend.search_kb(tenant, query, k=3)
        if not hits:
            return "知识库中没有找到相关文章。请如实告诉用户，并询问是否需要提交工单。"
        return "\n\n---\n\n".join(f"[{a.id}] {a.title}（更新于 {a.updated_at}，作者 {a.author}）\n{a.body}" for a in hits)

    @tool(timeout_s=5)
    def check_system_status(
        system: Annotated[
            Literal["vpn", "email", "wifi", "printer", "erp", "all"],
            Field(description="要查询的系统；不确定时用 all"),
        ] = "all",
        ctx: ToolContext = None,
    ) -> dict:
        """查询公司 IT 系统当前是否有已知故障（故障公告）。用户反映某系统"用不了 / 很慢 / 断线"时，先调用它，再决定是否建工单。"""
        tenant, _ = _who(ctx)
        status = backend.system_status(tenant)
        return status if system == "all" else {system: status.get(system, {"status": "unknown"})}

    @tool(timeout_s=5)
    def get_my_tickets(
        status: Annotated[Literal["open", "resolved", "all"], Field(description="open=未解决（含处理中），resolved=已解决")] = "all",
        ctx: ToolContext = None,
    ) -> list[dict] | str:
        """查询当前用户自己提交的工单及其进度。"""
        tenant, uid = _who(ctx)
        tickets = backend.list_tickets(tenant, uid)  # 只按 ctx 身份查询：模型没有办法"查别人的工单"
        if status == "open":
            tickets = [t for t in tickets if t.status != "resolved"]
        elif status == "resolved":
            tickets = [t for t in tickets if t.status == "resolved"]
        if not tickets:
            return "当前用户没有符合条件的工单。"
        return [
            {"ticket_id": t.id, "title": t.title, "status": t.status, "priority": t.priority,
             "created_at": t.created_at, "last_update": t.last_update}
            for t in tickets
        ]

    @tool(timeout_s=5)
    def lookup_employee(
        query: Annotated[str, Field(min_length=1, max_length=50, description="员工 user_id、姓名或部门的一部分")],
        ctx: ToolContext,
    ) -> list[dict] | str:
        """【仅 IT 管理员】在本公司员工目录中查询员工的部门、职位、账号状态和联系方式。"""
        tenant, _ = _who(ctx)
        if "it_admin" not in ctx.roles:  # RBAC 已经在 Hook 里拦过了，这里是第二道防线
            raise ToolError("无权查询员工信息（需要 it_admin 角色）。")
        people = backend.find_employees(tenant, query)
        if not people:
            return f"本公司员工目录中没有匹配 {query!r} 的员工。"
        # 数据最小化：手机号在源头就打码。工具不返回的数据，模型就不可能泄露。
        return [
            {"user_id": e.user_id, "name": e.name, "department": e.department, "title": e.title,
             "account_status": e.account_status, "email": e.email, "phone": mask_phone(e.phone)}
            for e in people
        ]

    # ------------------------------------------------------------------ write

    @tool(risk="write", timeout_s=10)
    def create_ticket(
        title: Annotated[str, Field(min_length=2, max_length=80, description="一句话概括问题，例如“笔记本屏幕闪烁”")],
        description: Annotated[str, Field(min_length=2, max_length=2000, description="问题现象、发生时间、已尝试的操作、用户提供的其他信息")],
        category: Annotated[Literal["hardware", "software", "account", "network", "other"], Field(description="问题分类")],
        priority: Annotated[Literal["low", "medium", "high"], Field(description="影响工作且无替代方案为 high")] = "medium",
        ctx: ToolContext = None,
    ) -> dict:
        """为当前用户创建一张 IT 工单。仅在用户明确要求报修 / 提工单，或同意你的建议后调用。"""
        tenant, uid = _who(ctx)
        # 第二层幂等：把 run_id:call_id 作为幂等键传给后端。
        # 第一层（agentkit 的 IdempotencyStore）在内存里，进程重启就没了；
        # 后端这层落在"数据所在的地方"，重启、多副本部署都有效。两层各管一段，缺一不可。
        ticket, duplicate = backend.create_ticket(
            tenant, uid, title, description, category, priority, idempotency_key=ctx.idempotency_key
        )
        return {
            "ticket_id": ticket.id,
            "status": ticket.status,
            "priority": ticket.priority,
            "duplicate_request": duplicate,
            "next_step": "请把工单号告诉用户，IT 工程师会在 4 个工作小时内响应。",
        }

    # ------------------------------------------------------------------ dangerous

    @tool(risk="dangerous", timeout_s=10)
    def reset_password(
        reason: Annotated[str, Field(min_length=2, max_length=200, description="重置原因（会展示给审批人），如“忘记密码”“账号被锁”")],
        target_user_id: Annotated[
            str | None, Field(description="要重置密码的员工 user_id；为空表示当前用户自己。只有 IT 管理员可以填写他人")
        ] = None,
        ctx: ToolContext = None,
    ) -> str:
        """为员工发起域账号密码重置：生成一次性重置链接并发送到该员工的企业邮箱。高风险操作，系统会自动发起人工审批。你不会、也不需要看到任何密码。"""
        tenant, uid = _who(ctx)
        denial = check_reset_permission(backend, tenant, uid, ctx.roles, target_user_id)
        if denial:
            raise ToolError(denial)
        target = target_user_id or uid
        record = backend.reset_password(tenant, target, requested_by=uid)
        who = "您" if target == uid else f"员工 {target}"
        return (f"已为{who}发起密码重置：一次性重置链接已发送到企业邮箱 {record.delivered_to}，30 分钟内有效。"
                "如该账号此前被锁定，已同时解锁。新密码不会出现在对话中。")

    return [search_kb, check_system_status, get_my_tickets, create_ticket, reset_password, lookup_employee]
