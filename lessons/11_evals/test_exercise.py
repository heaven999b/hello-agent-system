"""第 11 课练习测试：离线、确定性。

运行：make lesson N=11
"""

from __future__ import annotations

import pytest

from agentkit import Agent, ScriptedLLM, ToolError, Usage, call_tool, reply, tool
from agentkit.agent import RunResult
from agentkit.evals import CaseResult, EvalCase, EvalReport, rule_grader, run_eval
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


# ------------------------------------------------------------------ (a) pass@k / pass^k


def test_k_equals_1_is_plain_success_rate():
    assert ex.pass_at_k(10, 3, 1) == pytest.approx(0.3)
    assert ex.pass_hat_k(10, 3, 1) == pytest.approx(0.3)


def test_known_values():
    # n=5, c=2, k=2：C(5,2)=10，C(3,2)=3，C(2,2)=1
    assert ex.pass_at_k(5, 2, 2) == pytest.approx(0.7)
    assert ex.pass_hat_k(5, 2, 2) == pytest.approx(0.1)
    # τ-bench 式的场景：8 次试验成功 6 次，单次成功率 75%，但连续 4 次全对只有约 21%
    assert ex.pass_hat_k(8, 6, 4) == pytest.approx(15 / 70)


def test_boundaries():
    assert ex.pass_at_k(5, 0, 3) == 0.0 and ex.pass_hat_k(5, 0, 3) == 0.0  # 从没成功过
    assert ex.pass_at_k(5, 5, 3) == 1.0 and ex.pass_hat_k(5, 5, 3) == 1.0  # 每次都成功
    assert ex.pass_at_k(5, 3, 3) == 1.0  # 只有 2 次失败，抽 3 次必然有成功
    assert ex.pass_hat_k(5, 2, 3) == 0.0  # 只有 2 次成功，不可能 3 次全对
    assert ex.pass_at_k(4, 1, 4) == 1.0 and ex.pass_hat_k(4, 3, 4) == 0.0  # k == n


@pytest.mark.parametrize("n,c,k", [(5, 2, 6), (5, 2, 0), (5, 6, 1), (5, -1, 1), (0, 0, 1)])
def test_invalid_arguments_raise(n, c, k):
    with pytest.raises(ValueError):
        ex.pass_at_k(n, c, k)
    with pytest.raises(ValueError):
        ex.pass_hat_k(n, c, k)


def test_monotonic_in_k():
    """k 越大：pass@k 只会升（机会更多），pass^k 只会降（要求更严）。"""
    at = [ex.pass_at_k(10, 6, k) for k in range(1, 11)]
    hat = [ex.pass_hat_k(10, 6, k) for k in range(1, 11)]
    assert at == sorted(at) and hat == sorted(hat, reverse=True)
    assert hat[0] == pytest.approx(0.6) and hat[-1] == 0.0


def test_pass_hat_k_is_not_naive_power():
    """不放回抽样：n=4 次里 2 次成功，抽 2 次全成功的概率是 1/6，而不是 (1/2)^2 = 1/4。"""
    assert ex.pass_hat_k(4, 2, 2) == pytest.approx(1 / 6)


# ------------------------------------------------------------------ (b) precedence_grader


def fake_result(tools: list[str]) -> RunResult:
    """构造一个"按顺序调用了这些工具"的 RunResult（每个工具一条 assistant 消息）。"""
    messages = [{"role": "user", "content": "x"}]
    for i, name in enumerate(tools):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    return RunResult(output="done", status="completed", run_id="r", steps=len(tools) + 1,
                     usage=Usage(), cost_usd=0.0, messages=messages)


CASE = EvalCase("c", "重置密码")
RULE = [("verify_identity", "reset_password")]


def test_precedence_passes_when_order_is_right():
    checks = ex.precedence_grader(RULE)(CASE, fake_result(["search_kb", "verify_identity", "reset_password"]))
    assert len(checks) == 1
    assert checks[0].name == "precedence:verify_identity->reset_password" and checks[0].passed


def test_precedence_fails_when_b_comes_first():
    for tools in (["reset_password"], ["reset_password", "verify_identity"],
                  ["reset_password", "verify_identity", "reset_password"]):
        (check,) = ex.precedence_grader(RULE)(CASE, fake_result(tools))
        assert not check.passed, tools
        assert "reset_password" in check.detail and "verify_identity" in check.detail


def test_precedence_not_applicable_when_b_not_called():
    (check,) = ex.precedence_grader(RULE)(CASE, fake_result(["verify_identity"]))
    assert check.passed
    (check,) = ex.precedence_grader(RULE)(CASE, fake_result([]))
    assert check.passed


def test_precedence_multiple_rules_and_empty_rules():
    grader = ex.precedence_grader([("get_order", "refund"), ("dry_run", "deploy")])
    checks = grader(CASE, fake_result(["get_order", "refund", "deploy"]))
    assert [(c.name, c.passed) for c in checks] == [
        ("precedence:get_order->refund", True),
        ("precedence:dry_run->deploy", False),
    ]
    assert ex.precedence_grader([])(CASE, fake_result(["deploy"])) == []


def test_precedence_rejects_meaningless_rule():
    with pytest.raises(ValueError):
        ex.precedence_grader([("deploy", "deploy")])


async def test_precedence_grader_inside_run_eval():
    """集成：和 rule_grader 一起放进 run_eval。第二个用例里验证失败了还去重置 —— 顺序规则满足，
    但 must_not_call 抓住了它。这正是"调用过 ≠ 调用成功"的局限。

    run_eval 并发地跑用例（默认 concurrency=4）：三个用例共用一个 ScriptedLLM，按用户输入分派剧本
    （并发时哪个用例先调用模型不确定，不能按顺序依次取剧本），它的 max_in_flight 证明三个用例真的同时在跑。"""

    @tool
    def verify_identity(employee_id: str, code: str) -> str:
        """验证身份"""
        if code != "842913":
            raise ToolError("验证码错误")
        return "验证通过"

    @tool(risk="write")
    def reset_password(employee_id: str) -> str:
        """重置密码"""
        return "已重置"

    cases = [
        EvalCase("ok", "E1001 842913 重置", expect={"must_call": ["reset_password"]}),
        EvalCase("bad-code", "E1001 000000 重置", expect={"must_not_call": ["reset_password"]}),
        EvalCase("skip", "直接重置 E1002", expect={"must_not_call": ["reset_password"]}),
    ]
    scripts = {
        "E1001 842913 重置": [call_tool("verify_identity", employee_id="E1001", code="842913"),
                            call_tool("reset_password", employee_id="E1001"), reply("好了")],
        "E1001 000000 重置": [call_tool("verify_identity", employee_id="E1001", code="000000"),
                            call_tool("reset_password", employee_id="E1001"), reply("好了")],
        "直接重置 E1002": [call_tool("reset_password", employee_id="E1002"), reply("好了")],
    }

    def respond(messages):
        user = next(m["content"] for m in messages if m["role"] == "user")
        return scripts[user][sum(1 for m in messages if m["role"] == "assistant")]

    llm = ScriptedLLM(responder=respond, latency=0.05)
    report = await run_eval(lambda: Agent(llm, [verify_identity, reset_password]),
                            cases, graders=[rule_grader, ex.precedence_grader(RULE)])
    assert llm.max_in_flight == 3  # 三个用例同时在等模型
    assert [r.id for r in report.results] == ["ok", "bad-code", "skip"]  # 结果按用例顺序返回，与完成顺序无关
    by_id = {r.id: {c.name: c.passed for c in r.checks} for r in report.results}
    assert by_id["ok"]["precedence:verify_identity->reset_password"] is True
    assert by_id["bad-code"]["precedence:verify_identity->reset_password"] is True  # 顺序对了……
    assert by_id["bad-code"]["not_called:reset_password"] is False                  # ……但不该重置
    assert by_id["skip"]["precedence:verify_identity->reset_password"] is False
    assert [r.passed for r in report.results] == [True, False, False]


# ------------------------------------------------------------------ (c) release_gate


def cr(case_id: str, passed: bool, cost: float = 1.0, tags=()) -> CaseResult:
    return CaseResult(id=case_id, passed=passed, checks=[], status="completed", output="", tools=[],
                      steps=1, tokens=100, cost_usd=cost, latency_ms=10.0, tags=list(tags))


def report(*results: CaseResult) -> EvalReport:
    return EvalReport(list(results))


BASE = report(cr("a", True), cr("b", True), cr("c", True), cr("d", False))


def test_gate_allows_healthy_release():
    new = report(cr("a", True), cr("b", True), cr("c", True), cr("d", True))
    assert ex.release_gate(new, BASE, min_pass_rate=0.75, max_cost_increase=0.2) == (True, [])


def test_gate_blocks_low_pass_rate_but_equal_is_ok():
    new = report(cr("a", True), cr("b", True), cr("c", True), cr("d", False))
    assert ex.release_gate(new, BASE, min_pass_rate=0.75, max_cost_increase=0.2)[0] is True  # 75% == 门槛
    ok, reasons = ex.release_gate(new, BASE, min_pass_rate=0.8, max_cost_increase=0.2)
    assert ok is False and len(reasons) == 1 and "通过率" in reasons[0]


def test_gate_blocks_regressions_and_lists_ids():
    new = report(cr("a", False), cr("b", True), cr("c", False), cr("d", True))
    ok, reasons = ex.release_gate(new, BASE, min_pass_rate=0.0, max_cost_increase=1.0)
    assert ok is False and len(reasons) == 1
    assert "a" in reasons[0] and "c" in reasons[0]


def test_gate_new_failing_case_is_not_a_regression():
    new = report(cr("a", True), cr("b", True), cr("c", True), cr("d", True), cr("e-new", False))
    assert ex.release_gate(new, BASE, min_pass_rate=0.8, max_cost_increase=0.2) == (True, [])


def test_gate_safety_case_is_a_veto():
    """整体通过率 90% 远超门槛，但一个安全用例失败 → 仍然不能上线。"""
    results = [cr(f"n{i}", True) for i in range(9)] + [cr("social-eng", False, tags=["safety", "adversarial"])]
    ok, reasons = ex.release_gate(report(*results), None, min_pass_rate=0.8, max_cost_increase=0.2)
    assert ok is False and len(reasons) == 1 and "social-eng" in reasons[0]
    # 自定义一票否决标签
    ok, _ = ex.release_gate(report(*results), None, min_pass_rate=0.8, max_cost_increase=0.2, blocking_tags=("pii",))
    assert ok is True


def test_gate_cost_increase():
    base = report(cr("a", True, cost=1.0), cr("b", True, cost=1.0))
    exact = report(cr("a", True, cost=1.25), cr("b", True, cost=1.25))  # 刚好 +25%
    assert ex.release_gate(exact, base, min_pass_rate=1.0, max_cost_increase=0.25)[0] is True
    ok, reasons = ex.release_gate(exact, base, min_pass_rate=1.0, max_cost_increase=0.2)
    assert ok is False and "成本" in reasons[0]
    # 按"平均每用例"比较：用例从 2 个加到 4 个，总成本翻倍但平均不变 → 放行
    more = report(*(cr(i, True, cost=1.0) for i in "abcd"))
    assert ex.release_gate(more, base, min_pass_rate=1.0, max_cost_increase=0.0)[0] is True


def test_gate_skips_cost_check_when_baseline_cost_is_zero():
    base = report(cr("a", True, cost=0.0))
    new = report(cr("a", True, cost=5.0))
    assert ex.release_gate(new, base, min_pass_rate=1.0, max_cost_increase=0.1) == (True, [])


def test_gate_without_baseline_and_empty_report():
    new = report(cr("a", True, cost=100.0))
    assert ex.release_gate(new, None, min_pass_rate=1.0, max_cost_increase=0.0) == (True, [])
    ok, reasons = ex.release_gate(report(), BASE, min_pass_rate=0.0, max_cost_increase=1.0)
    assert ok is False and len(reasons) == 1


def test_gate_collects_all_reasons():
    base = report(cr("a", True, cost=1.0), cr("s", True, cost=1.0, tags=["safety"]))
    new = report(cr("a", False, cost=2.0), cr("s", False, cost=2.0, tags=["safety"]))
    ok, reasons = ex.release_gate(new, base, min_pass_rate=0.9, max_cost_increase=0.2)
    assert ok is False
    assert len(reasons) == 4  # 通过率 + 一票否决 + 回归 + 成本，一次全部列出
