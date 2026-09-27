"""第 08 课练习测试：离线、确定性（假时钟、假 sleep、固定随机种子）。

运行：make lesson N=08    或    .venv/bin/python -m pytest lessons/08_reliability -v
"""

from __future__ import annotations

import json
import random

import pytest

from agentkit import Agent, InMemoryCheckpointer, LLMError, RunState, ScriptedLLM, StopRun, ToolCall, call_tool, reply, tool
from agentkit.reliability import CircuitOpenError
from agentkit.testing import load_exercise

ex = load_exercise(__file__)


def _call(name: str, arguments: str, call_id: str = "c") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


@tool
def check_status(job_id: str) -> str:
    """查询任务状态"""
    return f"{job_id}: 处理中"


# =====================================================================
# (a) LoopGuard
# =====================================================================


def test_normalize_arguments_is_order_and_whitespace_insensitive():
    a = ex.normalize_arguments('{"b": 2, "a": 1}')
    b = ex.normalize_arguments('{"a":1,   "b":2}')
    assert a == b == '{"a":1,"b":2}'
    assert ex.normalize_arguments('{"城市": "北京"}') == '{"城市":"北京"}'  # 中文不转义
    assert ex.normalize_arguments("") == ex.normalize_arguments("{}") == "{}"
    assert ex.normalize_arguments('{"a": 1}') != ex.normalize_arguments('{"a": 2}')


def test_normalize_arguments_survives_invalid_json():
    assert ex.normalize_arguments("  {bad json ") == "{bad json"


def test_loop_guard_warns_first_then_stops():
    guard = ex.LoopGuard(window=6, max_repeats=3)
    state = RunState()
    assert guard.before_tool(state, _call("search", '{"q": "x"}'), None) is None
    assert guard.before_tool(state, _call("search", '{"q":"x"}'), None) is None  # 第 2 次：还没超限
    warning = guard.before_tool(state, _call("search", '{ "q" : "x" }'), None)  # 第 3 次：超限 → 警告
    assert isinstance(warning, str) and "search" in warning
    with pytest.raises(StopRun) as e:
        guard.before_tool(state, _call("search", '{"q": "x"}'), None)  # 再次超限 → 中止
    assert e.value.reason == "loop_detected"


def test_loop_guard_different_arguments_are_not_repeats():
    guard = ex.LoopGuard(window=6, max_repeats=3)
    state = RunState()
    for q in ["a", "b", "c", "d", "e", "f"]:
        assert guard.before_tool(state, _call("search", json.dumps({"q": q})), None) is None
    # 同参数不同工具也不算重复
    for name in ["tool_a", "tool_b", "tool_c"]:
        assert guard.before_tool(state, _call(name, '{"q": "a"}'), None) is None


def test_loop_guard_window_slides():
    guard = ex.LoopGuard(window=3, max_repeats=2)
    state = RunState()
    seq = ["A", "B", "C", "A", "D", "E", "A"]  # 每两次 A 之间都隔了 2 个其他调用，窗口里 A 永远只有 1 个
    for name in seq:
        assert guard.before_tool(state, _call(name, "{}"), None) is None
    assert len(state.metadata[ex.LoopGuard.STATE_KEY]["recent"]) == 3  # 只保留最近 window 个


def test_loop_guard_keeps_counts_on_state_not_on_hook():
    """同一个 Hook 实例服务多个运行（多个用户），彼此的计数不能串。"""
    guard = ex.LoopGuard(window=6, max_repeats=3)
    alice, bob = RunState(), RunState()
    for _ in range(2):
        assert guard.before_tool(alice, _call("search", '{"q": "x"}'), None) is None
    for _ in range(2):  # 如果计数存在 hook 上，bob 这里就会被误判
        assert guard.before_tool(bob, _call("search", '{"q": "x"}'), None) is None


def test_loop_guard_state_survives_checkpoint_roundtrip():
    """计数存在 state.metadata 里 → 必须 JSON 可序列化 → 崩溃/暂停后恢复计数不丢。"""
    guard = ex.LoopGuard(window=6, max_repeats=3)
    cp = InMemoryCheckpointer()
    state = RunState(run_id="r1")
    guard.before_tool(state, _call("search", '{"q": "x"}'), None)
    guard.before_tool(state, _call("search", '{"q": "x"}'), None)
    cp.save(state)  # 内部会 json.dumps，不可序列化就会报错
    restored = cp.load("r1")
    assert isinstance(guard.before_tool(restored, _call("search", '{"q": "x"}'), None), str)


def test_loop_guard_rejects_invalid_config():
    ok = ex.LoopGuard(window=2, max_repeats=2)  # 合法的边界配置：window == max_repeats
    state = RunState()
    assert ok.before_tool(state, _call("search", "{}"), None) is None
    assert isinstance(ok.before_tool(state, _call("search", "{}"), None), str)  # 连续两次就算重复
    with pytest.raises(ValueError):
        ex.LoopGuard(window=6, max_repeats=1)
    with pytest.raises(ValueError):
        ex.LoopGuard(window=2, max_repeats=3)


def test_loop_guard_stops_runaway_agent():
    executed = []

    @tool
    def poll(job_id: str) -> str:
        """轮询任务"""
        executed.append(job_id)
        return "处理中"

    llm = ScriptedLLM([call_tool("poll", job_id="J1")] * 10)
    res = Agent(llm, [poll], hooks=[ex.LoopGuard(window=6, max_repeats=3)], max_steps=10).run("等任务完成")
    assert res.status == "stopped" and res.stop_reason == "loop_detected"
    assert executed == ["J1", "J1"]  # 第 3 次被拒绝（警告），第 4 次直接中止
    tool_msgs = [m["content"] for m in res.messages if m["role"] == "tool"]
    assert tool_msgs[-2].startswith("拒绝")  # 第 3 次：模型确实收到了"换思路"的提醒
    # 第 4 次被 StopRun 中止：框架给这个没执行的调用补了一条结果，保证消息协议合法（每个 tool_call 都有回应）
    assert tool_msgs[-1].startswith("未执行") and "loop_detected" in tool_msgs[-1]
    assert len(llm.calls) == 4


def test_loop_guard_lets_agent_recover_after_warning():
    llm = ScriptedLLM(
        [call_tool("check_status", job_id="J1")] * 3
        + [call_tool("check_status", job_id="J2"), reply("J1 还在处理中，J2 也在处理中，请稍后再来。")]
    )
    guard = ex.LoopGuard(window=6, max_repeats=3)
    agent = Agent(llm, [check_status], hooks=[guard])
    res = agent.run("查一下任务")
    assert res.ok and res.metadata[ex.LoopGuard.STATE_KEY]["warned"] is True
    # 同一个 Agent（同一个 hook 实例）再跑一次：新运行从零开始计数
    llm.script = [call_tool("check_status", job_id="J1")] * 2 + [reply("处理中")]
    res2 = agent.run("再查一下")
    assert res2.ok and res2.metadata[ex.LoopGuard.STATE_KEY]["warned"] is False


# =====================================================================
# (b) RetryBudget + retry_with_budget
# =====================================================================


def _flaky(n_failures: int, error: Exception | None = None):
    """前 n_failures 次调用失败，之后成功。返回 (fn, 调用计数列表)。"""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= n_failures:
            raise error or LLMError("429 Too Many Requests", status_code=429, retryable=True)
        return "ok"

    return fn, calls


def test_budget_ten_requests_earn_exactly_one_retry():
    budget = ex.RetryBudget(ratio=0.1, initial_tokens=0, max_tokens=10)
    for _ in range(10):
        budget.on_request()
    assert budget.requests == 10 and budget.tokens == 1.0  # 浮点累加会得到 0.9999999999999999
    assert budget.try_acquire() is True
    assert budget.try_acquire() is False
    assert (budget.retries, budget.rejected) == (1, 1)


def test_budget_is_capped_at_max_tokens():
    budget = ex.RetryBudget(ratio=0.5, initial_tokens=2, max_tokens=3)
    for _ in range(100):
        budget.on_request()
    assert budget.tokens == 3.0


def test_retry_with_budget_succeeds_after_transient_errors():
    fn, calls = _flaky(2)
    budget = ex.RetryBudget(ratio=0.1, initial_tokens=3)
    slept, events = [], []
    out = ex.retry_with_budget(
        fn, budget, max_attempts=3, base_delay=1.0, sleep=slept.append, rng=random.Random(42),
        on_event=lambda kind, attempt, e, d: events.append((kind, attempt)),
    )
    assert out == "ok" and len(calls) == 3
    assert events == [("retry", 1), ("retry", 2)]
    assert 0 <= slept[0] <= 1.0 and 0 <= slept[1] <= 2.0  # 全抖动：[0, base * 2^(n-1)]
    assert budget.requests == 1 and budget.retries == 2


def test_non_retryable_error_is_not_retried_and_costs_nothing():
    fn, calls = _flaky(5, LLMError("400 Bad Request", status_code=400, retryable=False))
    budget = ex.RetryBudget(initial_tokens=3)
    with pytest.raises(LLMError, match="400"):
        ex.retry_with_budget(fn, budget, sleep=lambda s: None)
    assert len(calls) == 1 and budget.tokens == pytest.approx(3.1) and budget.retries == 0


def test_gives_up_after_max_attempts_with_original_error():
    fn, calls = _flaky(99)
    with pytest.raises(LLMError, match="429"):
        ex.retry_with_budget(fn, ex.RetryBudget(initial_tokens=10), max_attempts=4, sleep=lambda s: None)
    assert len(calls) == 4


def test_retry_budget_prevents_retry_storm():
    """下游彻底宕机：100 个请求、每个最多 3 次尝试。没有预算要打 300 次，有预算只多打很少的重试。"""
    budget = ex.RetryBudget(ratio=0.1, initial_tokens=3, max_tokens=10)
    total_calls = []
    events = []

    def down():
        total_calls.append(1)
        raise LLMError("503 Service Unavailable", status_code=503, retryable=True)

    for _ in range(100):
        with pytest.raises(LLMError):
            ex.retry_with_budget(down, budget, max_attempts=3, sleep=lambda s: None,
                                 on_event=lambda kind, *a: events.append(kind))
    assert budget.requests == 100
    assert budget.retries <= 3 + 0.1 * 100  # 长期重试比例 <= 10%（加上初始的 3 个）
    assert len(total_calls) == 100 + budget.retries < 120  # 而不是 300
    assert "give_up_budget" in events and budget.rejected > 0


def test_budget_refills_with_healthy_traffic():
    budget = ex.RetryBudget(ratio=0.1, initial_tokens=0, max_tokens=10)
    fn, calls = _flaky(1)
    with pytest.raises(LLMError):  # 桶是空的：第一次失败就放弃
        ex.retry_with_budget(fn, budget, sleep=lambda s: None)
    for _ in range(9):  # 9 个健康请求 + 上面 1 个 = 10 个请求 → 攒够 1 个令牌
        ex.retry_with_budget(lambda: "ok", budget, sleep=lambda s: None)
    fn2, calls2 = _flaky(1)
    assert ex.retry_with_budget(fn2, budget, sleep=lambda s: None) == "ok"  # 这次有 1 次重试机会
    assert len(calls2) == 2


def test_deadline_stops_retry_without_spending_budget():
    now = [100.0]
    fn, calls = _flaky(5)
    budget = ex.RetryBudget(initial_tokens=3)
    events = []
    with pytest.raises(LLMError):
        ex.retry_with_budget(
            fn, budget, max_attempts=5, base_delay=4.0, max_delay=4.0,
            deadline=100.01, clock=lambda: now[0], sleep=lambda s: None,
            rng=random.Random(1), on_event=lambda kind, *a: events.append(kind),
        )
    # 退避时间在 [0, 4] 内随机（这个种子下约 0.54s），而剩余时间只有 0.01s：等不起了，放弃
    assert events == ["give_up_deadline"] and len(calls) == 1
    assert budget.retries == 0 and budget.tokens == pytest.approx(3.1)  # 放弃的重试不消耗令牌


def test_expired_deadline_does_not_call_downstream():
    called = []
    with pytest.raises(ex.DeadlineExceeded):
        ex.retry_with_budget(lambda: called.append(1), ex.RetryBudget(), deadline=10.0, clock=lambda: 10.0)
    assert called == []
    assert not issubclass(ex.DeadlineExceeded, TimeoutError)  # 否则会被外层当作可重试错误


# =====================================================================
# (c) 加分题：SingleProbeCircuitBreaker（未实现时自动跳过）
# =====================================================================


def _bonus(fn):
    try:
        return fn()
    except NotImplementedError:
        pytest.skip("加分题 (c) 尚未实现，跳过")


def _tripped_breaker(now):
    cb = ex.SingleProbeCircuitBreaker(name="primary", failure_threshold=2, reset_timeout=10, clock=lambda: now[0])

    def fail():
        raise LLMError("503", retryable=True)

    for _ in range(2):
        with pytest.raises(LLMError):
            _bonus(lambda: cb.call(fail))
    assert cb.state == "open"
    return cb


def test_bonus_open_breaker_fails_fast():
    now = [0.0]
    cb = _tripped_breaker(now)
    called = []
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: called.append(1))
    assert called == []


def test_bonus_half_open_allows_only_one_probe():
    now = [0.0]
    cb = _tripped_breaker(now)
    now[0] = 11  # 过了 reset_timeout → half_open
    inner = {}

    def probe():
        # 试探请求进行中，另一个请求到达（这里用"嵌套调用"确定性地模拟并发）
        try:
            cb.call(lambda: "second")
            inner["second"] = "放行了"
        except CircuitOpenError:
            inner["second"] = "被拒绝"
        return "probe-ok"

    assert _bonus(lambda: cb.call(probe)) == "probe-ok"
    assert inner["second"] == "被拒绝"
    assert cb.state == "closed" and cb.call(lambda: "normal") == "normal"


def test_bonus_failed_probe_reopens_and_resets_timer():
    now = [0.0]
    cb = _tripped_breaker(now)
    now[0] = 11

    def fail():
        raise LLMError("still down", retryable=True)

    with pytest.raises(LLMError):
        _bonus(lambda: cb.call(fail))
    assert cb.state == "open"  # 重新计时：从 t=11 起再等 10 秒
    now[0] = 20
    assert cb.state == "open"
    now[0] = 21.5
    assert cb.state == "half_open"
    assert cb.call(lambda: "recovered") == "recovered" and cb.state == "closed"
