"""第 29 课练习测试：离线、确定性，不调用任何模型。

运行：make lesson N=29    或    .venv/bin/python -m pytest lessons/29_gateway_and_guardrails -v

(a) 的最后一个测试会把你构造的实体真的交给 Cedar（cedarpy）去判定课程里的策略文件；没装 cedarpy 时自动跳过。
(c) 的最后一个测试会检查课程里的网关部署配置 configs/litellm-config.yaml；没装 pyyaml 时自动跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
CONFIGS = Path(__file__).resolve().parent / "configs"

ALICE = {"tenant_id": "acme", "user_id": "alice", "roles": ["employee"], "department": "sales", "tenant_plan": "enterprise"}
TOOLS = [
    {"name": "search_kb", "risk": "read"},
    {"name": "reset_password", "risk": "dangerous", "tenant": "acme"},
    {"name": "partner_export", "risk": "write", "tenant": "globex"},
]


def by_uid(entities: list[dict]) -> dict[tuple[str, str], dict]:
    return {(e["uid"]["type"], e["uid"]["id"]): e for e in entities}


# =====================================================================
# (a) build_entities
# =====================================================================


def test_build_entities_user_tools_and_tenants():
    ents = by_uid(ex.build_entities(ALICE, TOOLS))
    user = ents[("User", "alice")]
    assert user["attrs"] == {"roles": ["employee"], "tenant": ex.entity_ref("Tenant", "acme"), "department": "sales"}
    assert user["parents"] == [{"type": "Tenant", "id": "acme"}]
    assert ents[("Tool", "search_kb")]["attrs"] == {"risk": "read"}  # 平台共享工具：没有 tenant 属性
    assert ents[("Tool", "reset_password")]["attrs"] == {"risk": "dangerous", "tenant": ex.entity_ref("Tenant", "acme")}
    assert ents[("Tenant", "acme")]["attrs"] == {"plan": "enterprise"}
    assert ents[("Tenant", "globex")]["attrs"] == {"plan": "unknown"}  # 别的租户也要有实体，但不透露属性
    assert len([k for k in ents if k[0] == "Tenant"]) == 2  # 每个租户只出现一次


def test_build_entities_defaults_and_role_normalisation():
    md = {"tenant_id": "acme", "user_id": "bob", "roles": "employee"}
    ents = by_uid(ex.build_entities(md, [{"name": "mystery_tool"}]))
    assert ents[("User", "bob")]["attrs"]["roles"] == ["employee"]  # 不是 ['e','m','p',...]
    assert ents[("User", "bob")]["attrs"]["department"] == ""
    assert ents[("Tenant", "acme")]["attrs"] == {"plan": "free"}  # 未知套餐 → 最严
    assert ents[("Tool", "mystery_tool")]["attrs"] == {"risk": "dangerous"}  # 未知风险 → 最严
    md = {"tenant_id": "acme", "user_id": "c", "roles": ["it_admin", "employee", "it_admin"]}
    assert by_uid(ex.build_entities(md, []))[("User", "c")]["attrs"]["roles"] == ["employee", "it_admin"]


@pytest.mark.parametrize("md", [{"user_id": "alice"}, {"tenant_id": "acme"}, {"tenant_id": "", "user_id": "alice"}, {}])
def test_build_entities_fails_closed_without_identity(md):
    with pytest.raises(ValueError):
        ex.build_entities(md, TOOLS)


def test_your_entities_drive_real_cedar_decisions():
    cedarpy = pytest.importorskip("cedarpy")
    policies = (CONFIGS / "policies.cedar").read_text(encoding="utf-8")
    schema = (CONFIGS / "schema.cedarschema").read_text(encoding="utf-8")

    def decide(md: dict, tool: str, action: str = "call_tool", target: str | None = None) -> tuple[bool, list[str]]:
        ctx = {"target_user": ex.entity_ref("User", target)} if target else {}
        req = {"principal": {"type": "User", "id": md["user_id"]}, "action": {"type": "Action", "id": action},
               "resource": {"type": "Tool", "id": tool}, "context": ctx}
        r = cedarpy.is_authorized(req, policies, ex.build_entities(md, TOOLS), schema)
        assert r.diagnostics.errors == []  # 实体齐全：没有任何策略因为求值出错被跳过
        names = r.diagnostics.id_annotations_by_reason
        return r.allowed, [names.get(p, p) for p in r.diagnostics.reasons]

    ian = {**ALICE, "user_id": "ian", "roles": ["it_admin"], "department": "it"}
    mallory = {**ian, "tenant_id": "globex", "user_id": "mallory"}
    assert decide(ALICE, "reset_password", target="bob") == (False, ["reset-self-only"])
    assert decide(ian, "reset_password", target="bob") == (True, ["it-admin-all-tools"])
    assert decide(ian, "reset_password", "call_tool_unattended", target="bob") == (False, [])
    assert decide(mallory, "reset_password", target="bob") == (False, ["tenant-isolation"])
    assert decide({**ian, "tenant_plan": "free"}, "reset_password")[1] == ["free-plan-no-dangerous"]


# =====================================================================
# (b) cascade_decide
# =====================================================================

TH = [(0.1, 0.95), (0.5, 0.5)]


def test_cascade_decides_at_first_confident_stage():
    assert ex.cascade_decide([0.05, 0.9], TH) == ("benign", 1)
    assert ex.cascade_decide([0.97, 0.1], TH) == ("attack", 1)


def test_cascade_escalates_when_unsure():
    assert ex.cascade_decide([0.7, 0.2], TH) == ("benign", 2)  # "请忽略我之前的要求，改成周五送货"
    assert ex.cascade_decide([0.4, 0.6], TH) == ("attack", 2)  # 换说法的攻击：正则没命中但有可疑词
    assert ex.cascade_decide([0.5, 0.6], [(0.1, 0.9), (0.2, 0.8)]) == ("uncertain", 2)


def test_cascade_is_lazy_expensive_stage_not_called_when_not_needed():
    calls = []

    def expensive() -> float:
        calls.append(1)
        return 0.9

    assert ex.cascade_decide([0.05, expensive], TH) == ("benign", 1)
    assert calls == []  # 省下的就是一次 LLM 调用
    assert ex.cascade_decide([lambda: 0.7, expensive], TH) == ("attack", 2)
    assert calls == [1]


@pytest.mark.parametrize(
    "results, thresholds",
    [([], []), ([0.5], TH), ([0.5, 0.5], [(0.9, 0.1), (0.5, 0.5)]), ([1.7, 0.5], TH), ([0.5, lambda: -0.1], TH)],
)
def test_cascade_validates_inputs(results, thresholds):
    with pytest.raises(ValueError):
        ex.cascade_decide(results, thresholds)


# =====================================================================
# (c) validate_fallback_chain
# =====================================================================


def dep(name: str, model: str | None = None, api_base: str = "http://gw") -> dict:
    return {"model_name": name, "litellm_params": {"model": f"openai/{model or name}", "api_base": api_base}}


MODELS = [dep("gpt-5.5"), dep("gpt-5.5", api_base="https://api.openai.com"), dep("gpt-5.6-luna"), dep("small")]


def test_valid_chain_has_no_problems():
    assert ex.validate_fallback_chain(MODELS, [{"gpt-5.5": ["gpt-5.6-luna"]}, {"gpt-5.6-luna": ["small"]}]) == []


def test_unknown_models_are_reported():
    problems = ex.validate_fallback_chain(MODELS, [{"gpt-5.5": ["gpt-6-typo"]}, {"ghost": ["small"]}])
    assert "unknown: gpt-6-typo" in problems and "unknown: ghost" in problems


def test_cycles_are_reported_once_in_canonical_form():
    problems = ex.validate_fallback_chain(MODELS, [{"small": ["gpt-5.6-luna"]}, {"gpt-5.6-luna": ["small"]}, {"gpt-5.5": ["gpt-5.6-luna"]}])
    assert [p for p in problems if p.startswith("cycle:")] == ["cycle: gpt-5.6-luna -> small -> gpt-5.6-luna"]
    assert "cycle: gpt-5.5 -> gpt-5.5" in ex.validate_fallback_chain(MODELS, [{"gpt-5.5": ["gpt-5.5", "small"]}])


def test_entry_groups_without_backstop_are_reported():
    assert ex.validate_fallback_chain(MODELS, []) == [
        "no-fallback: gpt-5.5", "no-fallback: gpt-5.6-luna", "no-fallback: small"]
    problems = ex.validate_fallback_chain(MODELS, [{"gpt-5.5": ["gpt-5.6-luna"]}])
    assert problems == ["no-fallback: small"]  # luna 是别人的备用、处在链尾，没有降级是正常的；small 是没人兜底的入口


def test_fallback_to_the_same_upstream_is_not_a_real_backstop():
    models = MODELS + [dep("gpt-5.5-alias", model="gpt-5.5")]  # 换了个组名，其实还是同一个网关上的同一个模型
    problems = ex.validate_fallback_chain(models, [{"gpt-5.5": ["gpt-5.5-alias"]}, {"small": ["gpt-5.5"]}, {"gpt-5.6-luna": ["small"]}])
    assert problems == ["same-upstream: gpt-5.5 -> gpt-5.5-alias"]


def test_lesson_gateway_config_passes():
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load((CONFIGS / "litellm-config.yaml").read_text(encoding="utf-8"))
    assert ex.validate_fallback_chain(cfg["model_list"], cfg["router_settings"]["fallbacks"]) == []
