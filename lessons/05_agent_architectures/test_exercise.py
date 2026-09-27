"""第 05 课练习测试：离线、确定性。planner / executor / critique 都是假函数，不需要 LLM。

运行：make lesson N=05
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/05_agent_architectures
"""

from __future__ import annotations

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
Step = ex.Step


# ------------------------------------------------------------------ 假的 planner / executor / replanner


def fixed_planner(*steps):
    calls = []

    def planner(task):
        calls.append(task)
        return list(steps)

    planner.calls = calls
    return planner


class FakeExecutor:
    """按工具名返回结果；fail 里的步骤 id 会抛异常（可以指定失败几次）。记录每次调用。"""

    def __init__(self, fail: dict[str, int] | None = None, error: Exception | None = None):
        self.fail = dict(fail or {})
        self.error = error or TimeoutError("天气服务超时")
        self.calls: list[str] = []
        self.seen_results: list[dict] = []

    def __call__(self, step, results):
        self.calls.append(step.id)
        self.seen_results.append(dict(results))
        if self.fail.get(step.id, 0) > 0:
            self.fail[step.id] -= 1
            raise self.error
        return f"{step.tool}:{step.args.get('city', '')}"


def never_replan(*args):
    raise AssertionError("这个用例不应该调用 replanner")


def weather_plan():
    return [
        Step("s1", "get_weather", {"city": "北京"}),
        Step("s2", "get_weather", {"city": "上海"}),
        Step("s3", "get_weather", {"city": "广州"}),
    ]


def kinds(trace):
    return [e["type"] for e in trace]


# ------------------------------------------------------------------ 任务 1：PlanExecuteAgent


def test_plan_execute_happy_path_runs_steps_in_order():
    planner, executor = fixed_planner(*weather_plan()), FakeExecutor()
    res = ex.PlanExecuteAgent(planner, executor, never_replan).run("查三个城市的天气")
    assert planner.calls == ["查三个城市的天气"]
    assert res.status == "completed" and res.stop_reason == "done" and res.ok
    assert executor.calls == ["s1", "s2", "s3"]
    assert list(res.results) == ["s1", "s2", "s3"]
    assert res.results["s3"] == "get_weather:广州"
    assert res.replans == 0
    assert res.trace[0] == {"type": "plan", "steps": ["s1", "s2", "s3"]}
    assert kinds(res.trace) == ["plan", "step", "step", "step", "finish"]
    assert res.trace[-1] == {"type": "finish", "status": "completed", "stop_reason": "done"}


def test_executor_sees_results_of_previous_steps():
    def executor(step, results):
        if step.id == "s2":
            return results["s1"] + "→汇总"  # 后面的步骤可以用前面步骤的结果
        return "北京 31°C"

    plan = [Step("s1", "get_weather", {"city": "北京"}), Step("s2", "subtask", {"use": "s1"})]
    res = ex.PlanExecuteAgent(fixed_planner(*plan), executor, never_replan).run("t")
    assert res.results == {"s1": "北京 31°C", "s2": "北京 31°C→汇总"}


def test_failure_triggers_replan_without_rerunning_done_steps():
    executor = FakeExecutor(fail={"s2": 1}, error=ValueError("上海站点维护中"))
    seen = {}

    def replanner(task, results, failed, error, remaining):
        seen.update(task=task, results=dict(results), failed=failed.id, error=error, remaining=[s.id for s in remaining])
        return [Step("s2b", "get_weather_backup", {"city": "上海"}), Step("s3", "get_weather", {"city": "广州"})]

    res = ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, replanner).run("查天气")
    assert res.status == "completed" and res.replans == 1
    assert executor.calls == ["s1", "s2", "s2b", "s3"], "已成功的 s1 不应该重新执行"
    assert list(res.results) == ["s1", "s2b", "s3"], "失败的 s2 不能写进 results"
    assert seen == {"task": "查天气", "results": {"s1": "get_weather:北京"}, "failed": "s2",
                    "error": "ValueError: 上海站点维护中", "remaining": ["s3"]}
    assert kinds(res.trace) == ["plan", "step", "step", "replan", "step", "step", "finish"]
    assert res.trace[2] == {"type": "step", "id": "s2", "ok": False, "error": "ValueError: 上海站点维护中"}
    assert res.trace[3] == {"type": "replan", "steps": ["s2b", "s3"]}


def test_replans_are_capped():
    executor = FakeExecutor(fail={"s1": 99})
    replanner_calls = []

    def replanner(task, results, failed, error, remaining):
        replanner_calls.append(failed.id)
        return [Step("s1", "get_weather", {"city": "北京"})]  # 固执地重试同一步（失败的 id 可以复用）

    res = ex.PlanExecuteAgent(fixed_planner(Step("s1", "get_weather")), executor, replanner, max_replans=2).run("t")
    assert res.status == "failed" and res.stop_reason == "max_replans"
    assert len(replanner_calls) == 2 and res.replans == 2
    assert executor.calls == ["s1", "s1", "s1"], "1 次原计划 + 2 次重规划后的尝试"
    assert res.results == {}
    assert res.trace[-1] == {"type": "finish", "status": "failed", "stop_reason": "max_replans"}


def test_zero_replans_fails_on_first_error():
    executor = FakeExecutor(fail={"s2": 1})
    res = ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, never_replan, max_replans=0).run("t")
    assert res.stop_reason == "max_replans" and res.replans == 0
    assert executor.calls == ["s1", "s2"] and list(res.results) == ["s1"]


def test_replanner_can_give_up():
    executor = FakeExecutor(fail={"s1": 1})
    res = ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, lambda *a: []).run("t")
    assert res.status == "failed" and res.stop_reason == "gave_up" and res.replans == 1
    assert executor.calls == ["s1"]
    assert kinds(res.trace) == ["plan", "step", "finish"]


def test_invalid_initial_plan_executes_nothing():
    for bad in ([], [Step("s1", "a"), Step("s1", "b")], ["查天气"], None):
        executor = FakeExecutor()
        res = ex.PlanExecuteAgent(lambda task, bad=bad: bad, executor, never_replan).run("t")
        assert res.status == "failed" and res.stop_reason == "invalid_plan", f"计划 {bad!r} 应该被判为不合法"
        assert executor.calls == []
        assert kinds(res.trace) == ["finish"]


def test_replanned_ids_must_not_overwrite_done_steps():
    executor = FakeExecutor(fail={"s2": 1})

    def replanner(task, results, failed, error, remaining):
        return [Step("s1", "get_weather", {"city": "上海"})]  # s1 已经成功，复用它的 id 会覆盖结果

    res = ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, replanner).run("t")
    assert res.status == "failed" and res.stop_reason == "invalid_plan"
    assert res.results == {"s1": "get_weather:北京"}


def test_max_steps_guards_against_plan_explosion():
    plan = [Step(f"s{i}", "get_weather") for i in range(1, 6)]
    executor = FakeExecutor()
    res = ex.PlanExecuteAgent(fixed_planner(*plan), executor, never_replan, max_steps=3).run("t")
    assert res.status == "failed" and res.stop_reason == "max_steps"
    assert executor.calls == ["s1", "s2", "s3"]
    exact = ex.PlanExecuteAgent(fixed_planner(*plan[:3]), FakeExecutor(), never_replan, max_steps=3).run("t")
    assert exact.status == "completed", "恰好 max_steps 步的计划应该能跑完"


def test_executor_gets_a_copy_of_results():
    def executor(step, results):
        results["hacked"] = True  # 执行器乱改传进来的 dict，不应影响 Agent 自己的记录
        return step.id

    res = ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, never_replan).run("t")
    assert res.results == {"s1": "s1", "s2": "s2", "s3": "s3"}


def test_plan_execute_config_errors():
    with pytest.raises(ValueError):
        ex.PlanExecuteAgent(fixed_planner(), FakeExecutor(), never_replan, max_replans=-1)
    with pytest.raises(ValueError):
        ex.PlanExecuteAgent(fixed_planner(), FakeExecutor(), never_replan, max_steps=0)


# ------------------------------------------------------------------ 任务 2：reflect_loop


class Writer:
    """假的生成器：记录收到的 (draft, feedback)，每次返回 v1、v2、v3……"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, draft, feedback):
        self.calls.append((draft, feedback))
        return f"v{len(self.calls)}"


def scripted_critic(*comments):
    queue = list(comments)
    return lambda draft: queue.pop(0)


def is_lgtm(comment: str) -> bool:
    return comment.strip().upper().startswith("LGTM")


def test_reflect_accepts_first_draft():
    writer = Writer()
    res = ex.reflect_loop(writer, scripted_critic("LGTM"), 3, is_lgtm)
    assert (res.draft, res.rounds, res.stop_reason) == ("v1", 1, "accepted")
    assert writer.calls == [(None, None)]
    assert res.history == [("v1", "LGTM")]


def test_reflect_revises_with_previous_draft_and_feedback():
    writer = Writer()
    res = ex.reflect_loop(writer, scripted_critic("没提广州的台风预警", "LGTM，很好"), 3, is_lgtm)
    assert res.stop_reason == "accepted" and res.draft == "v2" and res.rounds == 2
    assert writer.calls == [(None, None), ("v1", "没提广州的台风预警")]


def test_reflect_stops_at_max_rounds():
    writer = Writer()
    res = ex.reflect_loop(writer, scripted_critic("问题 A", "问题 B", "问题 C"), 3, is_lgtm)
    assert res.stop_reason == "max_rounds" and res.rounds == 3 and res.draft == "v3"
    assert len(writer.calls) == 3, "最后一条批评不应该再拿去调用 generate"


def test_reflect_stops_early_on_repeated_critique():
    writer = Writer()
    critic = scripted_critic("没有提到 广州 的台风预警", "  没有提到   广州 的台风预警\n", "LGTM")  # 只差空白
    res = ex.reflect_loop(writer, critic, 5, is_lgtm)
    assert res.stop_reason == "repeated_critique" and res.rounds == 2
    assert res.draft == "v2" and len(writer.calls) == 2, "意见重复时不应该再继续改"


def test_reflect_detects_oscillation_and_ignores_case():
    writer = Writer()
    critic = scripted_critic("Too long", "太口语化", "too   LONG", "LGTM")
    res = ex.reflect_loop(writer, critic, 10, is_lgtm)
    assert res.stop_reason == "repeated_critique" and res.rounds == 3
    assert [c for _, c in res.history] == ["Too long", "太口语化", "too   LONG"]


def test_reflect_accepted_takes_priority_over_repeat():
    writer = Writer()
    res = ex.reflect_loop(writer, scripted_critic("OK", "OK"), 3, lambda c: c == "OK" and len(writer.calls) == 2)
    assert res.stop_reason == "accepted" and res.rounds == 2


def test_reflect_rejects_bad_max_rounds():
    with pytest.raises(ValueError):
        ex.reflect_loop(Writer(), scripted_critic(), 0, is_lgtm)


# ------------------------------------------------------------------ 任务 3：choose_architecture

P = ex.TaskProfile


@pytest.mark.parametrize(
    "profile, expected",
    [
        (P("fixed"), "workflow"),
        (P("fixed", high_reliability=True), "workflow+hitl"),
        (P("plannable", parallelizable=True), "rewoo"),
        (P("plannable"), "plan_execute"),
        (P("plannable", verifiable=True, high_reliability=True), "plan_execute+reflection+hitl"),
        (P("unknown"), "react"),
        (P("unknown", needs_exploration=True), "react"),  # 没有打分信号，没法树搜索
        (P("unknown", needs_exploration=True, verifiable=True), "tree_search"),
        (P("unknown", needs_exploration=True, verifiable=True, high_reliability=True), "react+reflection+hitl"),
        (P("unknown", verifiable=True), "react+reflection"),
        (P("unknown", parallelizable=True), "react"),
    ],
)
def test_choose_architecture_rules(profile, expected):
    assert ex.choose_architecture(profile) == expected


def test_choose_architecture_rejects_unknown_predictability():
    with pytest.raises(ValueError):
        ex.choose_architecture(P("sometimes"))
