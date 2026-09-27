"""第 29 课练习参考答案（接口与 exercise.py 完全一致）。"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence, Union

# =====================================================================
# 练习 (a)：build_entities
# =====================================================================

UNKNOWN_RISK = "dangerous"
DEFAULT_PLAN = "free"


def entity_ref(type_: str, id_: Any) -> dict:
    return {"__entity": {"type": type_, "id": str(id_)}}


def build_entities(metadata: Mapping[str, Any], tools: Sequence[Mapping[str, Any]]) -> list[dict]:
    tenant_id, user_id = metadata.get("tenant_id"), metadata.get("user_id")
    if not tenant_id or not user_id:
        raise ValueError("缺少可信身份：metadata 必须包含 tenant_id 和 user_id")
    roles = metadata.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]

    tenants: dict[str, str] = {str(tenant_id): str(metadata.get("tenant_plan") or DEFAULT_PLAN)}
    entities: list[dict] = [
        {
            "uid": {"type": "User", "id": str(user_id)},
            "attrs": {
                "roles": sorted({str(r) for r in roles}),
                "tenant": entity_ref("Tenant", tenant_id),
                "department": str(metadata.get("department") or ""),
            },
            "parents": [{"type": "Tenant", "id": str(tenant_id)}],
        }
    ]
    for t in tools:
        attrs: dict[str, Any] = {"risk": str(t.get("risk") or UNKNOWN_RISK)}
        owner = t.get("tenant")
        if owner:
            attrs["tenant"] = entity_ref("Tenant", owner)
            tenants.setdefault(str(owner), "unknown")
        entities.append({"uid": {"type": "Tool", "id": str(t["name"])}, "attrs": attrs, "parents": []})
    entities += [{"uid": {"type": "Tenant", "id": t}, "attrs": {"plan": plan}, "parents": []} for t, plan in tenants.items()]
    return entities


# =====================================================================
# 练习 (b)：cascade_decide
# =====================================================================

StageResult = Union[float, Callable[[], float]]


def cascade_decide(stage_results: Sequence[StageResult], thresholds: Sequence[tuple[float, float]]) -> tuple[str, int]:
    if not stage_results or len(stage_results) != len(thresholds):
        raise ValueError("stage_results 与 thresholds 必须一一对应且不能为空")
    for low, high in thresholds:
        if not (0.0 <= low <= high <= 1.0):
            raise ValueError(f"阈值必须满足 0 <= low <= high <= 1：{(low, high)}")
    for i, (item, (low, high)) in enumerate(zip(stage_results, thresholds)):
        score = item() if callable(item) else item  # 惰性求值：只有走到这一级才去算
        if not (0.0 <= score <= 1.0):
            raise ValueError(f"第 {i + 1} 级的分数不在 [0, 1] 之间：{score}")
        if score >= high:
            return "attack", i + 1
        if score <= low:
            return "benign", i + 1
    return "uncertain", len(stage_results)


# =====================================================================
# 练习 (c)：validate_fallback_chain
# =====================================================================


def validate_fallback_chain(model_list: Sequence[Mapping[str, Any]], fallbacks: Sequence[Mapping[str, Sequence[str]]]) -> list[str]:
    groups: dict[str, set[tuple[str, str]]] = {}
    for d in model_list:
        p = d.get("litellm_params") or {}
        groups.setdefault(d["model_name"], set()).add((str(p.get("model")), str(p.get("api_base") or "")))

    edges: dict[str, list[str]] = {}
    for entry in fallbacks:
        for primary, targets in entry.items():
            lst = edges.setdefault(primary, [])
            lst += [t for t in targets if t not in lst]

    problems: set[str] = set()
    # 1. 不存在的模型组
    for primary, targets in edges.items():
        for name in [primary, *targets]:
            if name not in groups:
                problems.add(f"unknown: {name}")

    # 2. 环：深度优先，遇到"正在访问路径上"的节点就是一个环
    def canonical(cycle: list[str]) -> str:
        body = cycle[:-1]  # 去掉重复的结尾节点
        k = body.index(min(body))
        rotated = body[k:] + body[:k]
        return "cycle: " + " -> ".join(rotated + [rotated[0]])

    def dfs(node: str, path: list[str]) -> None:
        for nxt in edges.get(node, []):
            if nxt in path:
                problems.add(canonical(path[path.index(nxt):] + [nxt]))
            else:
                dfs(nxt, path + [nxt])

    for start in edges:
        dfs(start, [start])

    # 3. 入口模型组没有兜底
    targets_all = {t for ts in edges.values() for t in ts}
    for name in groups:
        if name not in targets_all and not edges.get(name):
            problems.add(f"no-fallback: {name}")

    # 4. 降级到同一个上游的同一个模型
    for primary, targets in edges.items():
        for t in targets:
            if primary in groups and t in groups and t != primary and groups[t] <= groups[primary]:
                problems.add(f"same-upstream: {primary} -> {t}")
    return sorted(problems)
