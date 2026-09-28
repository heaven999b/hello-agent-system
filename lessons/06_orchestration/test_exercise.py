"""第 06 课练习测试：离线、确定性。模型被替换成假的 async 函数，所以不需要 ScriptedLLM。

运行：make lesson N=06
用参考答案验证测试本身：AGENTKIT_SOLUTION=1 .venv/bin/python -m pytest lessons/06_orchestration

hybrid_route、run_with_gates 是 async 函数，测试函数写成 `async def`（pytest-asyncio，asyncio_mode = "auto"）直接 await；
vote_with_quorum 是普通函数，测试照常调用。
"""

from __future__ import annotations

import asyncio

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)

RULES = {
    "billing": ["退款", "扣费", "发票", "refund"],
    "tech": ["报错", "登录", "VPN", "bug"],
    "sales": ["报价", "购买", "续费"],
}


async def start_until(coro, event: asyncio.Event):
    """把 coro 放进一个 task 跑，直到 event 被设置（它走到了那次慢调用）。

    task 提前结束（比如练习还没写完、抛了 NotImplementedError）时，直接把它的异常抛出来，
    而不是傻等；也不会留下"没人取的异常"。只靠事件循环的调度推进，不依赖墙钟时间。
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


class SpyLLM:
    """记录调用次数的假"模型路由"（async 函数，和真实的模型调用一样要 await）。"""

    def __init__(self, answer=None, error: Exception | None = None, delay: float = 0.0):
        self.answer, self.error, self.delay, self.calls = answer, error, delay, []
        self.started = asyncio.Event()

    async def __call__(self, text: str):
        self.calls.append(text)
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)  # 模拟一次很慢的模型调用；取消会从这里的 await 抛出
        if self.error:
            raise self.error
        return self.answer


# ------------------------------------------------------------------ 任务 1：hybrid_route


async def test_route_rule_hit_does_not_call_llm():
    llm = SpyLLM("sales")
    assert await ex.hybrid_route("上个月被重复扣费了", RULES, llm) == ("billing", "rule")
    assert llm.calls == [], "规则命中时不应该再花钱调用模型"


async def test_route_multiple_hits_use_rules_order():
    text = "续费后一直登录报错，能退款吗"  # 同时命中 billing / tech / sales
    assert await ex.hybrid_route(text, RULES, SpyLLM()) == ("billing", "rule")
    reordered = {"sales": RULES["sales"], "tech": RULES["tech"], "billing": RULES["billing"]}
    assert await ex.hybrid_route(text, reordered, SpyLLM()) == ("sales", "rule")


async def test_route_keywords_are_case_insensitive():
    assert await ex.hybrid_route("My vpn keeps disconnecting", RULES, SpyLLM()) == ("tech", "rule")
    assert await ex.hybrid_route("I want a REFUND", RULES, SpyLLM()) == ("billing", "rule")


async def test_route_falls_back_to_llm_and_normalizes_answer():
    llm = SpyLLM(" Tech\n")
    assert await ex.hybrid_route("页面一直转圈圈", RULES, llm) == ("tech", "llm")
    assert llm.calls == ["页面一直转圈圈"]


async def test_route_llm_may_choose_default_category():
    assert await ex.hybrid_route("今天天气不错", RULES, SpyLLM("OTHER")) == ("other", "llm")
    assert await ex.hybrid_route("今天天气不错", RULES, SpyLLM("human"), default="human") == ("human", "llm")


async def test_route_degrades_to_default_on_llm_failure_or_garbage():
    text = "页面一直转圈圈"
    assert await ex.hybrid_route(text, RULES, SpyLLM(error=TimeoutError("模型超时"))) == ("other", "default")
    assert await ex.hybrid_route(text, RULES, SpyLLM(error=RuntimeError("500"))) == ("other", "default")
    assert await ex.hybrid_route(text, RULES, SpyLLM("weather")) == ("other", "default")
    assert await ex.hybrid_route(text, RULES, SpyLLM("我觉得是 tech 类")) == ("other", "default")
    assert await ex.hybrid_route(text, RULES, SpyLLM(None)) == ("other", "default")
    assert await ex.hybrid_route(text, RULES, SpyLLM("x"), default="human") == ("human", "default")


async def test_route_ignores_empty_keywords():
    rules = {"billing": ["", "  "], "tech": ["报错"]}
    assert await ex.hybrid_route("系统报错了", rules, SpyLLM()) == ("tech", "rule")
    llm = SpyLLM("billing")
    assert await ex.hybrid_route("你好", rules, llm) == ("billing", "llm")
    assert len(llm.calls) == 1, "空关键词不能让所有请求都命中第一个类别"


async def test_route_cancellation_propagates_instead_of_degrading():
    """用户断开 / 上游超时 → 调用方取消。取消不是"模型出错"，不能被吞掉变成 (default, "default")。"""
    llm = SpyLLM("tech", delay=30)
    task = await start_until(ex.hybrid_route("页面一直转圈圈", RULES, llm), llm.started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
    async def draft(s):
        log.append("draft")
        return s + "→草稿"

    async def translate(s):
        log.append("translate")
        return s + "→译文"

    async def polish(s):
        log.append("polish")
        return s + "→润色"

    return [("draft", draft), ("translate", translate), ("polish", polish)]


def step(fn):
    """把一个普通函数（如 str.upper）包成 async 步骤。"""

    async def run(s):
        return fn(s)

    return run


async def test_gates_all_pass():
    log = []
    res = await ex.run_with_gates(make_steps(log), "原文", {"draft": lambda s: "草稿" in s, "polish": lambda s: len(s) < 100})
    assert res.ok and res.output == "原文→草稿→译文→润色"
    assert res.completed == ["draft", "translate", "polish"]
    assert res.failed_step is None and res.kind is None and res.reason is None
    assert res.last_good_output == res.output
    assert log == ["draft", "translate", "polish"]


async def test_gate_rejection_stops_pipeline_with_structured_info():
    log = []
    res = await ex.run_with_gates(make_steps(log), "原文", {"translate": lambda s: "英文" in s})
    assert not res.ok and res.output is None
    assert res.failed_step == "translate" and res.kind == "gate_rejected"
    assert "translate" in res.reason
    assert res.completed == ["draft"]
    assert res.last_good_output == "原文→草稿"  # 失败步骤的输入 = 最后一个可信结果
    assert log == ["draft", "translate"], "失败后不能继续执行后面的步骤"


async def test_step_exception_becomes_step_error_not_raise():
    async def boom(s):
        raise ValueError("模型返回了空内容")

    res = await ex.run_with_gates([("draft", step(str.upper)), ("translate", boom), ("polish", step(str.lower))], "abc")
    assert not res.ok and res.kind == "step_error" and res.failed_step == "translate"
    assert res.reason == "ValueError: 模型返回了空内容"
    assert res.completed == ["draft"] and res.last_good_output == "ABC"


async def test_first_step_failure_keeps_original_input():
    res = await ex.run_with_gates([("a", step(lambda s: 1 / 0))], "原始输入")
    assert res.kind == "step_error" and res.completed == [] and res.last_good_output == "原始输入"
    assert res.reason.startswith("ZeroDivisionError")


async def test_gate_that_raises_fails_closed():
    def broken_gate(s):
        raise KeyError("score")

    res = await ex.run_with_gates([("a", step(str.upper)), ("b", step(str.lower))], "x", {"a": broken_gate})
    assert not res.ok and res.kind == "gate_rejected" and res.failed_step == "a"
    assert res.completed == [] and res.last_good_output == "x"


async def test_config_errors_raise_before_running_anything():
    log = []
    with pytest.raises(ValueError):
        await ex.run_with_gates(make_steps(log), "原文", {"transalte": lambda s: True})  # 拼写错误
    assert log == [], "配置错误必须在执行任何步骤之前发现"
    with pytest.raises(ValueError):
        await ex.run_with_gates([("a", step(str.upper)), ("a", step(str.lower))], "x")


async def test_empty_steps_and_no_gates():
    res = await ex.run_with_gates([], "原样返回")
    assert res.ok and res.output == "原样返回" and res.completed == []
    res = await ex.run_with_gates([("up", step(str.upper))], "abc", None)
    assert res.ok and res.output == "ABC"


async def test_cancellation_propagates_and_later_steps_never_run():
    """"绝不向外抛异常"只针对 Exception：调用方取消时，CancelledError 要原样传出去，后面的步骤不能再执行。"""
    log, started = [], asyncio.Event()

    async def slow(s):
        log.append("slow")
        started.set()
        await asyncio.sleep(30)  # 模拟一次很慢的模型调用
        return s

    async def after(s):
        log.append("after")
        return s

    task = await start_until(ex.run_with_gates([("slow", slow), ("after", after)], "x"), started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == ["slow"], "取消之后不能再执行后面的步骤"
