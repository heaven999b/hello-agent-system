"""第 06 课练习测试：离线、确定性。模型被替换成普通函数，所以不需要 ScriptedLLM。

运行：make lesson N=06
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/06_orchestration
"""

from __future__ import annotations

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)

RULES = {
    "billing": ["退款", "扣费", "发票", "refund"],
    "tech": ["报错", "登录", "VPN", "bug"],
    "sales": ["报价", "购买", "续费"],
}


class SpyLLM:
    """记录调用次数的假"模型路由"。"""

    def __init__(self, answer=None, error: Exception | None = None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, text: str):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.answer


# ------------------------------------------------------------------ 任务 1：hybrid_route


def test_route_rule_hit_does_not_call_llm():
    llm = SpyLLM("sales")
    assert ex.hybrid_route("上个月被重复扣费了", RULES, llm) == ("billing", "rule")
    assert llm.calls == [], "规则命中时不应该再花钱调用模型"


def test_route_multiple_hits_use_rules_order():
    text = "续费后一直登录报错，能退款吗"  # 同时命中 billing / tech / sales
    assert ex.hybrid_route(text, RULES, SpyLLM()) == ("billing", "rule")
    reordered = {"sales": RULES["sales"], "tech": RULES["tech"], "billing": RULES["billing"]}
    assert ex.hybrid_route(text, reordered, SpyLLM()) == ("sales", "rule")


def test_route_keywords_are_case_insensitive():
    assert ex.hybrid_route("My vpn keeps disconnecting", RULES, SpyLLM()) == ("tech", "rule")
    assert ex.hybrid_route("I want a REFUND", RULES, SpyLLM()) == ("billing", "rule")


def test_route_falls_back_to_llm_and_normalizes_answer():
    llm = SpyLLM(" Tech\n")
    assert ex.hybrid_route("页面一直转圈圈", RULES, llm) == ("tech", "llm")
    assert llm.calls == ["页面一直转圈圈"]


def test_route_llm_may_choose_default_category():
    assert ex.hybrid_route("今天天气不错", RULES, SpyLLM("OTHER")) == ("other", "llm")
    assert ex.hybrid_route("今天天气不错", RULES, SpyLLM("human"), default="human") == ("human", "llm")


def test_route_degrades_to_default_on_llm_failure_or_garbage():
    text = "页面一直转圈圈"
    assert ex.hybrid_route(text, RULES, SpyLLM(error=TimeoutError("模型超时"))) == ("other", "default")
    assert ex.hybrid_route(text, RULES, SpyLLM(error=RuntimeError("500"))) == ("other", "default")
    assert ex.hybrid_route(text, RULES, SpyLLM("weather")) == ("other", "default")
    assert ex.hybrid_route(text, RULES, SpyLLM("我觉得是 tech 类")) == ("other", "default")
    assert ex.hybrid_route(text, RULES, SpyLLM(None)) == ("other", "default")
    assert ex.hybrid_route(text, RULES, SpyLLM("x"), default="human") == ("human", "default")


def test_route_ignores_empty_keywords():
    rules = {"billing": ["", "  "], "tech": ["报错"]}
    assert ex.hybrid_route("系统报错了", rules, SpyLLM()) == ("tech", "rule")
    llm = SpyLLM("billing")
    assert ex.hybrid_route("你好", rules, llm) == ("billing", "llm")
    assert len(llm.calls) == 1, "空关键词不能让所有请求都命中第一个类别"


# ------------------------------------------------------------------ 任务 2：vote_with_quorum


def test_vote_normalizes_and_returns_first_spelling():
    assert ex.vote_with_quorum(["Yes", " yes", "NO"], 0.6) == "Yes"
    assert ex.vote_with_quorum(["  APPROVE ", "approve", "Approve", "reject"], 0.75) == "APPROVE"


def test_vote_below_quorum_returns_none():
    assert ex.vote_with_quorum(["A", "B", "C"], 0.5) is None
    assert ex.vote_with_quorum(["A", "A", "B", "C"], 0.6) is None  # 0.5 < 0.6


def test_vote_tie_returns_none_even_if_quorum_met():
    assert ex.vote_with_quorum(["A", "B"], 0.5) is None
    assert ex.vote_with_quorum(["a", "A", "b", "B"], 0.5) is None


def test_vote_exact_boundary_passes():
    assert ex.vote_with_quorum(["A", "A", "B"], 2 / 3) == "A"
    answers = ["A"] * 14 + ["B"] * 11  # 14/25 恰好 = 0.56（0.56 * 25 在浮点数里略大于 14）
    assert ex.vote_with_quorum(answers, 0.56) == "A"
    assert ex.vote_with_quorum(["A"] * 3, 1.0) == "A"
    assert ex.vote_with_quorum(["A", "A", "B"], 1.0) is None


def test_vote_abstentions_count_in_denominator():
    assert ex.vote_with_quorum(["A", "", None], 0.5) is None  # 1/3
    assert ex.vote_with_quorum(["A", "A", "   "], 0.6) == "A"  # 2/3
    assert ex.vote_with_quorum([None, "", " "], 0.1) is None  # 全部弃权


def test_vote_empty_and_invalid_quorum():
    assert ex.vote_with_quorum([], 0.5) is None
    for bad in (0, -0.1, 1.5):
        with pytest.raises(ValueError):
            ex.vote_with_quorum(["A"], bad)


# ------------------------------------------------------------------ 任务 3：run_with_gates


def make_steps(log: list):
    def draft(s):
        log.append("draft")
        return s + "→草稿"

    def translate(s):
        log.append("translate")
        return s + "→译文"

    def polish(s):
        log.append("polish")
        return s + "→润色"

    return [("draft", draft), ("translate", translate), ("polish", polish)]


def test_gates_all_pass():
    log = []
    res = ex.run_with_gates(make_steps(log), "原文", {"draft": lambda s: "草稿" in s, "polish": lambda s: len(s) < 100})
    assert res.ok and res.output == "原文→草稿→译文→润色"
    assert res.completed == ["draft", "translate", "polish"]
    assert res.failed_step is None and res.kind is None and res.reason is None
    assert res.last_good_output == res.output
    assert log == ["draft", "translate", "polish"]


def test_gate_rejection_stops_pipeline_with_structured_info():
    log = []
    res = ex.run_with_gates(make_steps(log), "原文", {"translate": lambda s: "英文" in s})
    assert not res.ok and res.output is None
    assert res.failed_step == "translate" and res.kind == "gate_rejected"
    assert "translate" in res.reason
    assert res.completed == ["draft"]
    assert res.last_good_output == "原文→草稿"  # 失败步骤的输入 = 最后一个可信结果
    assert log == ["draft", "translate"], "失败后不能继续执行后面的步骤"


def test_step_exception_becomes_step_error_not_raise():
    def boom(s):
        raise ValueError("模型返回了空内容")

    res = ex.run_with_gates([("draft", str.upper), ("translate", boom), ("polish", str.lower)], "abc")
    assert not res.ok and res.kind == "step_error" and res.failed_step == "translate"
    assert res.reason == "ValueError: 模型返回了空内容"
    assert res.completed == ["draft"] and res.last_good_output == "ABC"


def test_first_step_failure_keeps_original_input():
    res = ex.run_with_gates([("a", lambda s: 1 / 0)], "原始输入")
    assert res.kind == "step_error" and res.completed == [] and res.last_good_output == "原始输入"
    assert res.reason.startswith("ZeroDivisionError")


def test_gate_that_raises_fails_closed():
    def broken_gate(s):
        raise KeyError("score")

    res = ex.run_with_gates([("a", str.upper), ("b", str.lower)], "x", {"a": broken_gate})
    assert not res.ok and res.kind == "gate_rejected" and res.failed_step == "a"
    assert res.completed == [] and res.last_good_output == "x"


def test_config_errors_raise_before_running_anything():
    log = []
    with pytest.raises(ValueError):
        ex.run_with_gates(make_steps(log), "原文", {"transalte": lambda s: True})  # 拼写错误
    assert log == [], "配置错误必须在执行任何步骤之前发现"
    with pytest.raises(ValueError):
        ex.run_with_gates([("a", str.upper), ("a", str.lower)], "x")


def test_empty_steps_and_no_gates():
    res = ex.run_with_gates([], "原样返回")
    assert res.ok and res.output == "原样返回" and res.completed == []
    res = ex.run_with_gates([("up", str.upper)], "abc", None)
    assert res.ok and res.output == "ABC"
