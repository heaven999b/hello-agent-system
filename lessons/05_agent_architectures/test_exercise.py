"""第 05 课练习测试：离线、确定性。planner / executor / critique 都是假的 async 函数，不需要 LLM。

运行：make lesson N=05
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/05_agent_architectures

测试函数是 `async def`（pytest-asyncio，asyncio_mode = "auto"），里面直接 await 被测的 async 函数。
并发只用确定性的量证明（同时在途的峰值、谁被取消了），不断言耗时。
"""

from __future__ import annotations

import asyncio

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
Step = ex.Step


# ------------------------------------------------------------------ 假的 planner / executor / replanner


def fixed_planner(*steps):
    calls = []

    async def planner(task):
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

    async def __call__(self, step, results):
        self.calls.append(step.id)
        self.seen_results.append(dict(results))
        if self.fail.get(step.id, 0) > 0:
            self.fail[step.id] -= 1
            raise self.error
        return f"{step.tool}:{step.args.get('city', '')}"


async def never_replan(*args):
    raise AssertionError("这个用例不应该调用 replanner")


def returns(value):
    """把一个固定返回值包成 async 函数（替代以前的 lambda *a: value）。"""

    async def fn(*args):
        return value

    return fn


async def start_until(coro, event: asyncio.Event):
    """把 coro 放进一个 task 跑，直到 event 被设置（它走到了那次慢调用）。

    task 提前结束（比如练习还没写完）时，直接把它的异常抛出来，而不是傻等；
    只靠事件循环的调度推进，不依赖墙钟时间。
    """
    task = asyncio.ensure_future(coro)
    for _ in range(1000):
        if event.is_set() or task.done():
            break
        await asyncio.sleep(0)
    if task.done():
        task.result()
    assert event.is_set(), "被测函数没有走到那次慢调用"
    return task


def weather_plan():
    return [
        Step("s1", "get_weather", {"city": "北京"}),
        Step("s2", "get_weather", {"city": "上海"}),
        Step("s3", "get_weather", {"city": "广州"}),
    ]


def kinds(trace):
    return [e["type"] for e in trace]


# ------------------------------------------------------------------ 任务 1：PlanExecuteAgent


async def test_plan_execute_happy_path_runs_steps_in_order():
    planner, executor = fixed_planner(*weather_plan()), FakeExecutor()
    res = await ex.PlanExecuteAgent(planner, executor, never_replan).run("查三个城市的天气")
    assert planner.calls == ["查三个城市的天气"]
    assert res.status == "completed" and res.stop_reason == "done" and res.ok
    assert executor.calls == ["s1", "s2", "s3"]
    assert list(res.results) == ["s1", "s2", "s3"]
    assert res.results["s3"] == "get_weather:广州"
    assert res.replans == 0
    assert res.trace[0] == {"type": "plan", "steps": ["s1", "s2", "s3"]}
    assert kinds(res.trace) == ["plan", "step", "step", "step", "finish"]
    assert res.trace[-1] == {"type": "finish", "status": "completed", "stop_reason": "done"}


async def test_executor_sees_results_of_previous_steps():
    async def executor(step, results):
        if step.id == "s2":
            return results["s1"] + "→汇总"  # 后面的步骤可以用前面步骤的结果
        return "北京 31°C"

    plan = [Step("s1", "get_weather", {"city": "北京"}), Step("s2", "subtask", {"use": "s1"})]
    res = await ex.PlanExecuteAgent(fixed_planner(*plan), executor, never_replan).run("t")
    assert res.results == {"s1": "北京 31°C", "s2": "北京 31°C→汇总"}


async def test_failure_triggers_replan_without_rerunning_done_steps():
    executor = FakeExecutor(fail={"s2": 1}, error=ValueError("上海站点维护中"))
    seen = {}

    async def replanner(task, results, failed, error, remaining):
        seen.update(task=task, results=dict(results), failed=failed.id, error=error, remaining=[s.id for s in remaining])
        return [Step("s2b", "get_weather_backup", {"city": "上海"}), Step("s3", "get_weather", {"city": "广州"})]

    res = await ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, replanner).run("查天气")
    assert res.status == "completed" and res.replans == 1
    assert executor.calls == ["s1", "s2", "s2b", "s3"], "已成功的 s1 不应该重新执行"
    assert list(res.results) == ["s1", "s2b", "s3"], "失败的 s2 不能写进 results"
    assert seen == {"task": "查天气", "results": {"s1": "get_weather:北京"}, "failed": "s2",
                    "error": "ValueError: 上海站点维护中", "remaining": ["s3"]}
    assert kinds(res.trace) == ["plan", "step", "step", "replan", "step", "step", "finish"]
    assert res.trace[2] == {"type": "step", "id": "s2", "ok": False, "error": "ValueError: 上海站点维护中"}
    assert res.trace[3] == {"type": "replan", "steps": ["s2b", "s3"]}


async def test_replans_are_capped():
    executor = FakeExecutor(fail={"s1": 99})
    replanner_calls = []

    async def replanner(task, results, failed, error, remaining):
        replanner_calls.append(failed.id)
        return [Step("s1", "get_weather", {"city": "北京"})]  # 固执地重试同一步（失败的 id 可以复用）

    res = await ex.PlanExecuteAgent(fixed_planner(Step("s1", "get_weather")), executor, replanner, max_replans=2).run("t")
    assert res.status == "failed" and res.stop_reason == "max_replans"
    assert len(replanner_calls) == 2 and res.replans == 2
    assert executor.calls == ["s1", "s1", "s1"], "1 次原计划 + 2 次重规划后的尝试"
    assert res.results == {}
    assert res.trace[-1] == {"type": "finish", "status": "failed", "stop_reason": "max_replans"}


async def test_zero_replans_fails_on_first_error():
    executor = FakeExecutor(fail={"s2": 1})
    res = await ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, never_replan, max_replans=0).run("t")
    assert res.stop_reason == "max_replans" and res.replans == 0
    assert executor.calls == ["s1", "s2"] and list(res.results) == ["s1"]


async def test_replanner_can_give_up():
    executor = FakeExecutor(fail={"s1": 1})
    res = await ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, returns([])).run("t")
    assert res.status == "failed" and res.stop_reason == "gave_up" and res.replans == 1
    assert executor.calls == ["s1"]
    assert kinds(res.trace) == ["plan", "step", "finish"]


async def test_invalid_initial_plan_executes_nothing():
    for bad in ([], [Step("s1", "a"), Step("s1", "b")], ["查天气"], None):
        executor = FakeExecutor()
        res = await ex.PlanExecuteAgent(returns(bad), executor, never_replan).run("t")
        assert res.status == "failed" and res.stop_reason == "invalid_plan", f"计划 {bad!r} 应该被判为不合法"
        assert executor.calls == []
        assert kinds(res.trace) == ["finish"]


async def test_replanned_ids_must_not_overwrite_done_steps():
    executor = FakeExecutor(fail={"s2": 1})

    async def replanner(task, results, failed, error, remaining):
        return [Step("s1", "get_weather", {"city": "上海"})]  # s1 已经成功，复用它的 id 会覆盖结果

    res = await ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, replanner).run("t")
    assert res.status == "failed" and res.stop_reason == "invalid_plan"
    assert res.results == {"s1": "get_weather:北京"}


async def test_max_steps_guards_against_plan_explosion():
    plan = [Step(f"s{i}", "get_weather") for i in range(1, 6)]
    executor = FakeExecutor()
    res = await ex.PlanExecuteAgent(fixed_planner(*plan), executor, never_replan, max_steps=3).run("t")
    assert res.status == "failed" and res.stop_reason == "max_steps"
    assert executor.calls == ["s1", "s2", "s3"]
    exact = await ex.PlanExecuteAgent(fixed_planner(*plan[:3]), FakeExecutor(), never_replan, max_steps=3).run("t")
    assert exact.status == "completed", "恰好 max_steps 步的计划应该能跑完"


async def test_executor_gets_a_copy_of_results():
    async def executor(step, results):
        results["hacked"] = True  # 执行器乱改传进来的 dict，不应影响 Agent 自己的记录
        return step.id

    res = await ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, never_replan).run("t")
    assert res.results == {"s1": "s1", "s2": "s2", "s3": "s3"}


async def test_cancellation_propagates_and_is_not_treated_as_a_failed_step():
    """调用方取消（用户关掉页面）不是"这一步失败"：CancelledError 必须原样传出去，不能拿去重规划、更不能继续执行后面的步骤。"""
    started, finished = asyncio.Event(), []

    async def executor(step, results):
        if step.id == "s2":
            started.set()
            await asyncio.sleep(30)  # 模拟一次很慢的工具调用；取消会从这里的 await 抛出来
        finished.append(step.id)
        return step.id

    agent = ex.PlanExecuteAgent(fixed_planner(*weather_plan()), executor, never_replan)
    task = await start_until(agent.run("t"), started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["s1"], "取消之后不应该再执行任何步骤（s3），也不应该调用 replanner"


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

    async def __call__(self, draft, feedback):
        self.calls.append((draft, feedback))
        return f"v{len(self.calls)}"


def scripted_critic(*comments):
    queue = list(comments)

    async def critic(draft):
        return queue.pop(0)

    return critic


def is_lgtm(comment: str) -> bool:
    return comment.strip().upper().startswith("LGTM")


async def test_reflect_accepts_first_draft():
    writer = Writer()
    res = await ex.reflect_loop(writer, scripted_critic("LGTM"), 3, is_lgtm)
    assert (res.draft, res.rounds, res.stop_reason) == ("v1", 1, "accepted")
    assert writer.calls == [(None, None)]
    assert res.history == [("v1", "LGTM")]


async def test_reflect_revises_with_previous_draft_and_feedback():
    writer = Writer()
    res = await ex.reflect_loop(writer, scripted_critic("没提广州的台风预警", "LGTM，很好"), 3, is_lgtm)
    assert res.stop_reason == "accepted" and res.draft == "v2" and res.rounds == 2
    assert writer.calls == [(None, None), ("v1", "没提广州的台风预警")]


async def test_reflect_stops_at_max_rounds():
    writer = Writer()
    res = await ex.reflect_loop(writer, scripted_critic("问题 A", "问题 B", "问题 C"), 3, is_lgtm)
    assert res.stop_reason == "max_rounds" and res.rounds == 3 and res.draft == "v3"
    assert len(writer.calls) == 3, "最后一条批评不应该再拿去调用 generate"


async def test_reflect_stops_early_on_repeated_critique():
    writer = Writer()
    critic = scripted_critic("没有提到 广州 的台风预警", "  没有提到   广州 的台风预警\n", "LGTM")  # 只差空白
    res = await ex.reflect_loop(writer, critic, 5, is_lgtm)
    assert res.stop_reason == "repeated_critique" and res.rounds == 2
    assert res.draft == "v2" and len(writer.calls) == 2, "意见重复时不应该再继续改"


async def test_reflect_detects_oscillation_and_ignores_case():
    writer = Writer()
    critic = scripted_critic("Too long", "太口语化", "too   LONG", "LGTM")
    res = await ex.reflect_loop(writer, critic, 10, is_lgtm)
    assert res.stop_reason == "repeated_critique" and res.rounds == 3
    assert [c for _, c in res.history] == ["Too long", "太口语化", "too   LONG"]


async def test_reflect_accepted_takes_priority_over_repeat():
    writer = Writer()
    res = await ex.reflect_loop(writer, scripted_critic("OK", "OK"), 3, lambda c: c == "OK" and len(writer.calls) == 2)
    assert res.stop_reason == "accepted" and res.rounds == 2


async def test_reflect_rejects_bad_max_rounds():
    writer = Writer()
    with pytest.raises(ValueError):
        await ex.reflect_loop(writer, scripted_critic(), 0, is_lgtm)
    assert writer.calls == [], "参数不合法时一次模型调用都不应该发生"


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


# ------------------------------------------------------------------ 附：run_independent_steps（已提供，不是练习）


class InFlightExecutor:
    """假的慢执行器：每一步"调用工具"要等一会儿，记录同时在途的步骤数和每一步的结局。"""

    def __init__(self, delay: float = 0.05, fail: dict[str, float] | None = None):
        self.delay, self.fail = delay, dict(fail or {})
        self.in_flight = self.peak = 0
        self.outcome: dict[str, str] = {}
        self.seen_results: dict[str, dict] = {}

    async def __call__(self, step, results):
        self.seen_results[step.id] = dict(results)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if step.id in self.fail:
                await asyncio.sleep(self.fail[step.id])
                self.outcome[step.id] = "failed"
                raise TimeoutError(f"{step.id} 超时")
            await asyncio.sleep(self.delay)
            self.outcome[step.id] = "done"
            return f"{step.tool}:{step.args.get('city', '')}"
        except asyncio.CancelledError:
            self.outcome[step.id] = "cancelled"
            raise
        finally:
            self.in_flight -= 1


def read_only(step) -> bool:
    return step.tool.startswith("get_")


async def test_independent_read_only_steps_really_run_concurrently():
    executor = InFlightExecutor()
    out = await ex.run_independent_steps(weather_plan(), executor, {"s0": "已有结果"}, is_read_only=read_only)
    assert executor.peak == 3, "三个互不依赖的查询应该同时在途"
    assert list(out) == ["s1", "s2", "s3"] and out["s2"] == "get_weather:上海", "结果按计划顺序返回"
    assert all(seen == {"s0": "已有结果"} for seen in executor.seen_results.values()), "批内每一步看到的是同一份快照"

    capped = InFlightExecutor()
    await ex.run_independent_steps(weather_plan(), capped, {}, is_read_only=read_only, max_concurrency=2)
    assert capped.peak == 2, "max_concurrency 是真正的在途上限"


async def test_steps_with_side_effects_are_refused_before_anything_runs():
    executor = InFlightExecutor()
    plan = [Step("s1", "book_flight", {"flight": "CZ3101"}), Step("s2", "cancel_booking", {"booking": "B42"})]
    with pytest.raises(ValueError):
        await ex.run_independent_steps(plan, executor, {}, is_read_only=read_only)
    assert executor.outcome == {}, "有写操作时一步都不能执行"


async def test_one_failure_cancels_the_other_steps():
    executor = InFlightExecutor(delay=30, fail={"s2": 0.01})
    with pytest.raises(TimeoutError):
        await ex.run_independent_steps(weather_plan(), executor, {}, is_read_only=read_only)
    assert executor.outcome == {"s1": "cancelled", "s2": "failed", "s3": "cancelled"}
    assert executor.in_flight == 0, "返回之前，被取消的步骤都已经真正停下来了"
