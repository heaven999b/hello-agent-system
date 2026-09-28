"""策略即代码：用 Cedar 替换 PermissionPolicy 里写死的 RBAC 角色表（第 29 课）。

教学版的局限：PermissionPolicy 的 role_tools 是一个写在 Python 里的 dict。改一条权限 = 改代码 + 发版；
安全团队没法单独评审"谁能做什么"；也表达不了"只能重置自己的密码""不能跨租户"这类看属性、看参数的规则。

Cedar（AWS 开源的授权策略语言，Rust 实现，cedarpy 是它的 Python 绑定）把授权拆成三样东西：

    策略（.cedar）   permit / forbid 规则，带 @id 注解，可评审、可测试、可独立发布
    schema           实体类型、属性类型、动作 —— 用它在上线前静态校验策略（写错属性名直接报错）
    实体（entities） 每次请求时由**可信的服务端身份信息**构造：User(roles, tenant, department)、Tool(risk, tenant)、Tenant(plan)

判定规则（Cedar 官方语义）：任一 forbid 命中 → Deny；否则任一 permit 命中 → Allow；否则默认 Deny。
注意一个坑：**策略求值出错时该策略会被跳过**（skip on error）。如果出错的是一条 forbid，结果可能变成 Allow。
所以本模块把"有求值错误"一律当作拒绝（fail closed），并把错误写进审计。

CedarPolicy 是一个 agentkit Hook，行为与 PermissionPolicy 对齐：
- visible_tools：Action::"call_tool" 不允许的工具不展示给模型；
- before_tool：先问 Action::"call_tool"（不允许 → 拒绝并说明命中的策略），
  再问 ask_action（默认 Action::"call_tool_unattended"，即"无人值守直接执行"）——不允许就走人工审批：
  state.approvals → approver（普通函数或 async 函数）→ PauseRun（落盘等审批人，之后 agent.approve() 继续）。
visible_tools 和 Cedar 判定本身是纯计算（Rust 实现，微秒级），是普通方法；before_tool 是 async，
因为它可能要 await 一个 async 的 approver（例如去审批系统查一条记录）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from agentkit.hooks import Hook, PauseRun
from agentkit.state import RunState
from agentkit.tools import Tool, ToolRegistry, maybe_await
from agentkit.types import ToolCall

from . import require

__all__ = [
    "CedarPolicy",
    "PolicyDecision",
    "build_entities",
    "entity_args_context",
    "entity_ref",
    "tool_catalog",
    "validate",
]

UNKNOWN_RISK = "dangerous"  # 目录里查不到风险等级的工具：按最严的处理（默认拒绝原则）
DEFAULT_PLAN = "free"  # 查不到租户套餐：按最受限的套餐处理

EntitiesFn = Callable[[Mapping[str, Any], Mapping[str, dict]], list]
ContextFn = Callable[[RunState, ToolCall, Tool, dict], dict]
Approver = Callable[[ToolCall, RunState], "bool | Awaitable[bool]"]


def _cedarpy():
    return require("cedarpy", "policy")


def entity_ref(type_: str, id_: Any) -> dict:
    """Cedar 实体 JSON 里引用另一个实体的写法。"""
    return {"__entity": {"type": type_, "id": str(id_)}}


def _uid(type_: str, id_: Any) -> dict:
    return {"type": type_, "id": str(id_)}


def tool_catalog(
    tools: Iterable[Tool] | ToolRegistry | Mapping[str, Mapping] | None,
    tool_tenants: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """把各种形式的工具清单统一成 {工具名: {"risk": ..., "tenant": ...}}。

    tenant 表示"这个工具属于哪个租户"（比如某租户自己接入的 CRM 连接器）；None 表示平台共享工具。
    风险等级来自工具定义（代码评审过的），而不是模型或用户输入。
    """
    catalog: dict[str, dict] = {}
    if tools is None:
        items: Iterable = ()
    elif isinstance(tools, ToolRegistry):
        items = (tools.get(n) for n in tools.names())
    elif isinstance(tools, Mapping):
        items = ({"name": k, **dict(v)} for k, v in tools.items())
    else:
        items = tools
    for t in items:
        if isinstance(t, Tool):
            name, risk, tenant = t.name, t.risk, getattr(t, "tenant", None)
        else:
            name, risk, tenant = t["name"], t.get("risk", UNKNOWN_RISK), t.get("tenant")
        catalog[name] = {"risk": risk or UNKNOWN_RISK, "tenant": tenant}
    for name, tenant in (tool_tenants or {}).items():
        catalog.setdefault(name, {"risk": UNKNOWN_RISK, "tenant": None})["tenant"] = tenant
    return catalog


def build_entities(metadata: Mapping[str, Any], tools: Mapping[str, Mapping] | Iterable[Tool] | ToolRegistry) -> list[dict]:
    """从**可信 metadata**（服务端登录态）构造 Cedar 实体：Tenant、User、Tool。

    metadata: {"tenant_id", "user_id", "roles", "department", "tenant_plan"}
    缺少 tenant_id / user_id 时抛 ValueError —— 没有经过认证的身份，就不应该走到授权这一步（fail closed）。
    """
    tenant_id, user_id = metadata.get("tenant_id"), metadata.get("user_id")
    if not tenant_id or not user_id:
        raise ValueError("缺少可信身份：metadata 必须包含 tenant_id 和 user_id")
    roles = metadata.get("roles") or []
    if isinstance(roles, str):  # "employee" 被当成字符集合会变成 {"e","m",...}，这里显式纠正
        roles = [roles]
    catalog = tools if isinstance(tools, Mapping) else tool_catalog(tools)

    tenants: dict[str, str] = {str(tenant_id): str(metadata.get("tenant_plan") or DEFAULT_PLAN)}
    entities: list[dict] = [
        {
            "uid": _uid("User", user_id),
            "attrs": {
                "roles": sorted({str(r) for r in roles}),
                "tenant": entity_ref("Tenant", tenant_id),
                "department": str(metadata.get("department") or ""),
            },
            "parents": [_uid("Tenant", tenant_id)],
        }
    ]
    for name, info in catalog.items():
        attrs: dict[str, Any] = {"risk": str(info.get("risk") or UNKNOWN_RISK)}
        owner = info.get("tenant")
        if owner:
            attrs["tenant"] = entity_ref("Tenant", owner)
            tenants.setdefault(str(owner), "unknown")  # 别的租户：只放实体本身，不透露它的任何属性
        entities.append({"uid": _uid("Tool", name), "attrs": attrs, "parents": []})
    entities += [{"uid": _uid("Tenant", t), "attrs": {"plan": plan}, "parents": []} for t, plan in tenants.items()]
    return entities


def entity_args_context(fields: Mapping[str, Mapping[str, tuple[str, str]]]) -> ContextFn:
    """把工具参数里的 ID 转成 Cedar context 里的实体引用，用于参数级授权（ABAC）。

    fields = {"reset_password": {"target_user_id": ("target_user", "User")}}
    → 调用 reset_password(target_user_id="bob") 时 context = {"target_user": User::"bob"}，
      策略里就可以写 `context.target_user != principal`。
    """

    def context_fn(state: RunState, call: ToolCall, tool: Tool, args: dict) -> dict:
        ctx = {}
        for arg, (key, type_) in fields.get(call.name, {}).items():
            if args.get(arg) not in (None, ""):
                ctx[key] = entity_ref(type_, args[arg])
        return ctx

    return context_fn


@dataclass
class PolicyDecision:
    """一次授权判定，可直接写进审计日志。"""

    action: str
    principal: str
    resource: str
    allowed: bool
    decision: str  # Allow / Deny / NoDecision（请求或实体不合法）
    policy_ids: list[str] = field(default_factory=list)  # 决定结果的策略（@id 注解；没有注解时是 policy0 这样的解析器 id）
    errors: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    latency_ms: float = 0.0

    def to_record(self) -> dict:
        return asdict(self)


def _read_text(src: str | Path) -> str:
    if isinstance(src, Path):
        return src.read_text(encoding="utf-8")
    if "\n" not in src and len(src) < 1024:
        p = Path(src)
        if p.suffix in (".cedar", ".cedarschema", ".json") and p.is_file():
            return p.read_text(encoding="utf-8")
    return src


def _load_schema(schema: str | Path | dict | None):
    if schema is None:
        return None, None
    cedarpy = _cedarpy()
    if isinstance(schema, dict):
        text = json.dumps(schema)
        return cedarpy.Schema.from_json_str(text), text
    text = _read_text(schema)
    if text.lstrip().startswith("{"):
        return cedarpy.Schema.from_json_str(text), text
    return cedarpy.Schema.from_str(text), text


def validate(policies: str | Path, schema: str | Path | dict) -> list[str]:
    """用 schema 静态校验策略：返回错误列表（空列表 = 通过）。放进 CI，写错属性名/动作名的策略根本合不进主干。"""
    cedarpy = _cedarpy()
    text = _read_text(policies)
    try:
        handle, _ = _load_schema(schema)
        result = cedarpy.validate_policies(text, handle)
    except ValueError as e:  # 语法错误
        return [f"parse error: {e}"]
    names = result.id_annotations_by_policy_id
    return [f"{names.get(e.policy_id, e.policy_id)}: {getattr(e, 'error', e)}" for e in result.errors]


class CedarPolicy(Hook):
    """用 Cedar 策略做工具级 + 参数级授权的 Hook，替代 PermissionPolicy 的 RBAC 部分。

    policies:    策略文本或 .cedar 文件路径。
    schema:      Cedar schema（文本 / .cedarschema 路径 / JSON dict）。提供时会在构造时校验策略，不通过直接抛错。
    entities_fn: (metadata, tool_catalog) -> 实体列表；默认 build_entities。
    ask_action:  "无需审批即可执行"对应的动作名；为 None 时不做审批判定。
    tools:       工具清单（Tool 列表 / ToolRegistry / {名: {"risk","tenant"}}），visible_tools 要用它查风险等级。
    tool_tenants: {工具名: 所属租户}，给租户专属工具打标。
    context_fn:  (state, call, tool, args) -> Cedar context，用于参数级授权；默认空 context。
    approver:    审批函数 approver(call, state) -> bool，普通函数或 async 函数都可以（async 的会被 await，
                 而不是把协程对象当成 True —— 那会把高危操作静默批准）；为 None 时抛 PauseRun，等人工审批后恢复。
    audit:       审计回调 audit(record: dict)；每次判定都会调用（visible_tools 的批量判定除外）。
    """

    def __init__(
        self,
        policies: str | Path,
        schema: str | Path | dict | None = None,
        entities_fn: EntitiesFn | None = None,
        ask_action: str | None = "call_tool_unattended",
        *,
        call_action: str = "call_tool",
        tools: Iterable[Tool] | ToolRegistry | Mapping[str, Mapping] | None = None,
        tool_tenants: Mapping[str, str] | None = None,
        context_fn: ContextFn | None = None,
        approver: Approver | None = None,
        audit: Callable[[dict], None] | None = None,
    ):
        cedarpy = _cedarpy()
        self._cedarpy = cedarpy
        self.policies_text = _read_text(policies)
        self._policy_set = cedarpy.PolicySet.from_str(self.policies_text)  # 语法错误在启动时就暴露
        self._schema, self.schema_text = _load_schema(schema)
        if self._schema is not None:
            errors = validate(self.policies_text, self.schema_text)
            if errors:
                raise ValueError("Cedar 策略没有通过 schema 校验：\n" + "\n".join(errors))
        self.entities_fn = entities_fn or build_entities
        self.call_action = call_action
        self.ask_action = ask_action
        self.tool_tenants = dict(tool_tenants or {})
        self.catalog = tool_catalog(tools, self.tool_tenants)
        self.context_fn = context_fn
        self.approver = approver
        self.audit = audit
        self.decisions: list[PolicyDecision] = []  # 本进程内的判定记录（demo / 测试用；生产写审计系统）

    # ------------------------------------------------------------------ 判定

    def _request(self, metadata: Mapping, tool_name: str, action: str, context: dict | None) -> dict:
        return {
            "principal": _uid("User", metadata.get("user_id")),
            "action": _uid("Action", action),
            "resource": _uid("Tool", tool_name),
            "context": dict(context or {}),
        }

    def _decide_batch(self, metadata: Mapping, catalog: Mapping[str, dict], asks: list[tuple[str, str, dict | None]]) -> list[PolicyDecision]:
        """一次调用评估多个 (tool, action, context)。任何异常 / 求值错误 → 拒绝（fail closed）。"""
        start = time.perf_counter()
        principal = f'User::"{metadata.get("user_id")}"'
        try:
            entities = self.entities_fn(metadata, catalog)
            requests = [self._request(metadata, t, a, c) for t, a, c in asks]
            results = self._cedarpy.is_authorized_batch(requests, self._policy_set, entities, self._schema)
        except Exception as e:  # noqa: BLE001 —— 构造实体失败（如缺身份）也是"拒绝"，而不是让 Agent 崩溃
            ms = (time.perf_counter() - start) * 1000
            return [
                PolicyDecision(a, principal, f'Tool::"{t}"', False, "NoDecision", [], [f"{type(e).__name__}: {e}"], dict(c or {}), ms)
                for t, a, c in asks
            ]
        ms = (time.perf_counter() - start) * 1000 / max(len(asks), 1)
        out = []
        for (t, a, c), r in zip(asks, results):
            diag = r.diagnostics
            names = diag.id_annotations_by_reason
            ids = [names.get(pid, pid) for pid in diag.reasons]
            errors = list(diag.errors)
            decision = r.decision.name
            out.append(PolicyDecision(a, principal, f'Tool::"{t}"', bool(r.allowed) and not errors, decision, ids, errors, dict(c or {}), round(ms, 3)))
        return out

    def authorize(self, metadata: Mapping, tool: str | Tool, action: str | None = None, context: dict | None = None) -> PolicyDecision:
        """单次判定（demo、测试、以及 Agent 之外的服务端点都可以直接用）。判定会记入 decisions 并送审计回调。"""
        name, catalog = self._catalog_for(tool)
        decision = self._decide_batch(metadata, catalog, [(name, action or self.call_action, context)])[0]
        self._record(None, decision)
        return decision

    @staticmethod
    def explain(decision: PolicyDecision) -> list[str]:
        """返回决定这次结果的策略 id（Allow 时是命中的 permit，Deny 时是命中的 forbid；
        都没命中说明是默认拒绝）。写进审计日志，出了问题能直接定位到是哪条策略。"""
        return list(decision.policy_ids)

    @staticmethod
    def describe(decision: PolicyDecision) -> str:
        if decision.errors:
            return "策略求值出错，按拒绝处理：" + "; ".join(e.splitlines()[0] for e in decision.errors)
        if decision.policy_ids:
            return ("命中策略 " if decision.allowed else "被策略拒绝 ") + ", ".join(decision.policy_ids)
        return "没有任何 permit 策略命中（默认拒绝）"

    def _catalog_for(self, tool: str | Tool) -> tuple[str, dict[str, dict]]:
        if isinstance(tool, Tool):
            # 风险等级以工具定义为准（代码评审过）；归属租户按 tool_tenants → 工具清单 → 工具对象属性 的顺序查
            known = self.catalog.get(tool.name, {})
            tenant = self.tool_tenants.get(tool.name) or known.get("tenant") or getattr(tool, "tenant", None)
            return tool.name, {tool.name: {"risk": tool.risk, "tenant": tenant}}
        return tool, {tool: self.catalog.get(tool, {"risk": UNKNOWN_RISK, "tenant": self.tool_tenants.get(tool)})}

    def _record(self, state: RunState | None, decision: PolicyDecision, tool_call_id: str | None = None) -> None:
        self.decisions.append(decision)
        record = {"event": "policy_decision", "call_id": tool_call_id, **decision.to_record()}
        if state is not None:
            record.update(run_id=state.run_id, tenant_id=state.metadata.get("tenant_id"), user_id=state.metadata.get("user_id"))
            state.metadata.setdefault("policy_decisions", []).append(
                {"call_id": tool_call_id, "action": decision.action, "resource": decision.resource,
                 "allowed": decision.allowed, "policy_ids": decision.policy_ids}
            )
        if self.audit is not None:
            self.audit(record)

    # ------------------------------------------------------------------ Hook

    def visible_tools(self, state: RunState, names: list[str]) -> list[str]:
        if not names:
            return names
        catalog = {n: self.catalog.get(n, {"risk": UNKNOWN_RISK, "tenant": self.tool_tenants.get(n)}) for n in names}
        decisions = self._decide_batch(state.metadata, catalog, [(n, self.call_action, None) for n in names])
        return [n for n, d in zip(names, decisions) if d.allowed]

    async def before_tool(self, state: RunState, call: ToolCall, tool: Tool | None) -> str | None:
        if tool is None:
            return None  # 不存在的工具交给 registry 报错
        args, arg_error = tool.parse_arguments(call.arguments)
        context = {}
        if arg_error is None and self.context_fn is not None:
            context = self.context_fn(state, call, tool, args or {})
        name, catalog = self._catalog_for(tool)
        asks = [(name, self.call_action, context)]
        if self.ask_action:
            asks.append((name, self.ask_action, context))
        decisions = self._decide_batch(state.metadata, catalog, asks)
        call_decision = decisions[0]
        self._record(state, call_decision, call.id)
        if not call_decision.allowed:
            return f"拒绝：当前用户无权调用 {call.name}（{self.describe(call_decision)}）。"
        if arg_error is not None:
            return None  # 参数不合法：不打扰审批人，交给 registry 返回校验错误让模型自己改
        if not self.ask_action:
            return None
        ask_decision = decisions[1]
        self._record(state, ask_decision, call.id)
        if ask_decision.allowed:
            return None  # 策略允许无人值守执行
        approved = state.approvals.get(call.id)
        if approved is None and self.approver is not None:
            approved = bool(await maybe_await(self.approver(call, state)))
            state.approvals[call.id] = approved
        if approved is None:
            raise PauseRun(
                call,
                f"操作 {call.name}({call.arguments}) 需要人工审批（{self.ask_action}：{self.describe(ask_decision)}），已提交审批。",
            )
        if not approved:
            return f"拒绝：审批人没有批准 {call.name} 操作。请告知用户该操作未获批准。"
        return None
