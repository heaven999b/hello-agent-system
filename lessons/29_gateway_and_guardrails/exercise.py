"""第 29 课练习：模型网关、策略即代码与护栏服务。

一共三题，分别对应本课的三块成熟组件：
  (a) build_entities           —— 从可信 metadata 构造 Cedar 实体（策略即代码的"输入"）
  (b) cascade_decide           —— 级联分类器的决策：便宜的先判，拿不准再交给贵的
  (c) validate_fallback_chain  —— 上线前检查网关的降级链：引用了不存在的模型、循环降级、没有兜底

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=29
    # 或者：.venv/bin/python -m pytest lessons/29_gateway_and_guardrails -v

卡住了？先重读 README 对应小节，再看 solution.py。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence, Union

# =====================================================================
# 练习 (a)：build_entities —— 从可信 metadata 构造 Cedar 实体
# =====================================================================

UNKNOWN_RISK = "dangerous"  # 工具没写风险等级：按最严的处理
DEFAULT_PLAN = "free"  # 查不到租户套餐：按最受限的套餐处理


def entity_ref(type_: str, id_: Any) -> dict:
    """Cedar 实体 JSON 里"引用另一个实体"的写法（已写好）。"""
    return {"__entity": {"type": type_, "id": str(id_)}}


def build_entities(metadata: Mapping[str, Any], tools: Sequence[Mapping[str, Any]]) -> list[dict]:
    """把服务端登录态（可信 metadata）和工具清单转换成 Cedar 的实体列表。

    metadata 形如：
        {"tenant_id": "acme", "user_id": "alice", "roles": ["employee"], "department": "sales", "tenant_plan": "enterprise"}
    tools 形如：
        [{"name": "search_kb", "risk": "read"}, {"name": "reset_password", "risk": "dangerous", "tenant": "acme"}]

    返回的每个实体都是 {"uid": {"type": ..., "id": ...}, "attrs": {...}, "parents": [...]}，要求：

    1. User 实体：uid = {"type": "User", "id": user_id}
       attrs = {"roles": 去重并排序后的角色列表, "tenant": entity_ref("Tenant", tenant_id), "department": 部门（缺省为 ""）}
       parents = [{"type": "Tenant", "id": tenant_id}]
       注意：roles 如果是一个字符串（如 "employee"），要当成 ["employee"]，而不是拆成字符。
    2. 每个工具一个 Tool 实体：uid = {"type": "Tool", "id": name}
       attrs = {"risk": risk（缺省为 UNKNOWN_RISK）}；工具带 "tenant" 时再加 attrs["tenant"] = entity_ref("Tenant", 该租户)
       parents = []
    3. Tenant 实体：用户自己的租户 attrs = {"plan": tenant_plan（缺省为 DEFAULT_PLAN）}；
       工具引用到的其他租户也要有实体，attrs = {"plan": "unknown"}（只证明它存在，不透露任何属性）。
       parents = []。每个租户只出现一次。
    4. metadata 缺少 tenant_id 或 user_id（None 或空字符串）→ 抛 ValueError（fail closed：没有经过认证的身份不该走到授权这一步）。

    为什么"其他租户"也要有实体？Cedar 求值时如果某条策略读了一个不存在的实体的属性，
    这条策略会被**跳过**（skip on error）——如果它恰好是一条 forbid，结果就可能从 Deny 变成 Allow。
    """
    raise NotImplementedError("TODO: 练习 (a) —— 从可信 metadata 构造 Cedar 实体")


# =====================================================================
# 练习 (b)：cascade_decide —— 级联决策
# =====================================================================

StageResult = Union[float, Callable[[], float]]


def cascade_decide(stage_results: Sequence[StageResult], thresholds: Sequence[tuple[float, float]]) -> tuple[str, int]:
    """按从便宜到贵的顺序逐级判定，返回 (label, stages_used)。

    stage_results[i] 是第 i 级分类器给出的"属于攻击"的分数（0~1），
    或者一个无参函数 —— 调用它才会真正去算这一级（比如发一次 LLM 请求）。**不需要的级别绝不能调用。**
    thresholds[i] = (low, high)：
        score >= high → ("attack", i + 1)
        score <= low  → ("benign", i + 1)
        其余          → 拿不准，交给下一级
    所有级别都拿不准 → ("uncertain", 级数)

    校验（任一不满足就抛 ValueError）：
        - stage_results 不能为空，且和 thresholds 一样长；
        - 每个阈值满足 0 <= low <= high <= 1；
        - 实际算出来的分数在 [0, 1] 之间。

    例：
        cascade_decide([0.05, 0.9], [(0.1, 0.95), (0.5, 0.5)]) == ("benign", 1)    # 第一级有把握
        cascade_decide([0.7, 0.2],  [(0.1, 0.95), (0.5, 0.5)]) == ("benign", 2)    # 正则命中但拿不准，LLM 说是正常
        cascade_decide([0.7, 0.6],  [(0.1, 0.95), (0.5, 0.5)]) == ("attack", 2)
    """
    raise NotImplementedError("TODO: 练习 (b) —— 级联决策")


# =====================================================================
# 练习 (c)：validate_fallback_chain —— 检查网关的降级链
# =====================================================================


def validate_fallback_chain(model_list: Sequence[Mapping[str, Any]], fallbacks: Sequence[Mapping[str, Sequence[str]]]) -> list[str]:
    """上线前检查 LiteLLM 的降级配置，返回问题列表（空列表 = 没问题）。

    model_list：LiteLLM 格式，每项 {"model_name": 模型组名, "litellm_params": {"model": ..., "api_base": ...（可选）}}；
                同名的多项属于同一个模型组。
    fallbacks： [{"主模型组": ["备用组1", "备用组2"]}, ...]，同一个主模型组可能出现在多个 dict 里（合并看）。

    需要发现四类问题，每条问题是一个字符串，以固定前缀开头（测试按前缀判断）：

    1. "unknown: <名字>"         降级配置里出现的模型组（无论作为主模型还是备用）不在 model_list 里。
    2. "cycle: a -> b -> a"      沿着降级边走会回到自己（包括 a 降级到 a 自己）。同一个环只报一次；
                                 为了结果确定，环从字典序最小的节点开始写，例如 b→a→b 写成 "cycle: a -> b -> a"。
    3. "no-fallback: <名字>"     "入口"模型组没有任何降级。入口 = 不是任何其他组的降级目标的模型组
                                 （只作为别人的备用、处在链条末端的组，没有降级是正常的）。
                                 fallbacks 为空时，每个模型组都是没有兜底的入口。
    4. "same-upstream: a -> b"   备用组的所有部署，在主模型组里都有 (litellm_params.model, api_base) 完全相同的部署：
                                 降级到"同一个上游的同一个模型"，主模型挂的时候它也一起挂，不是真正的兜底。

    返回值去重后按字符串排序，保证结果确定。
    """
    raise NotImplementedError("TODO: 练习 (c) —— 检查降级链")
