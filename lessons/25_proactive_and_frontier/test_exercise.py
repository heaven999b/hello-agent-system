"""第 25 课练习测试：离线、确定、毫秒级。

运行：make lesson N=25    或    .venv/bin/python -m pytest lessons/25_proactive_and_frontier -v
"""

from __future__ import annotations

import math

import pytest

from agentkit.testing import load_exercise

ex = load_exercise(__file__)
Context, Limits, UserModel = ex.Context, ex.Limits, ex.UserModel
FLOOR, CEIL = ex.CONF_FLOOR, ex.CONF_CEIL


def t(hm: str, day: int = 0) -> int:
    h, m = hm.split(":")
    return day * 1440 + int(h) * 60 + int(m)


# ================================================================ (a) update_belief


def test_update_belief_is_log_odds_bayes():
    assert ex.update_belief(0.5, 1.0, True) == pytest.approx(1 / (1 + math.exp(-1)), abs=1e-9)  # ≈ 0.731
    assert ex.update_belief(0.5, 1.0, False) == pytest.approx(1 / (1 + math.exp(1)), abs=1e-9)  # ≈ 0.269
    # 先验几率 1:4 × 似然比 4 = 后验几率 1:1
    assert ex.update_belief(0.2, math.log(4), True) == pytest.approx(0.5, abs=1e-9)


def test_refuting_evidence_lowers_and_zero_weight_keeps():
    assert ex.update_belief(0.8, 0.5, False) < 0.8
    assert ex.update_belief(0.8, 0.5, True) > 0.8
    assert ex.update_belief(0.3, 0.0, True) == pytest.approx(0.3, abs=1e-9)
    assert ex.update_belief(0.3, 0.0, False) == pytest.approx(0.3, abs=1e-9)
    # 权重越大，移动越多
    assert ex.update_belief(0.6, 2.0, False) < ex.update_belief(0.6, 1.0, False)


def test_same_weight_support_then_refute_cancels_out():
    p = ex.update_belief(ex.update_belief(0.3, 0.7, True), 0.7, False)
    assert p == pytest.approx(0.3, abs=1e-9)


def test_certainty_is_clamped_so_it_can_still_be_corrected():
    # prior 恰好是 1.0 / 0.0：不夹住的话 logit = ±∞，再多证据也改不动
    down = ex.update_belief(1.0, 2.0, False)
    up = ex.update_belief(0.0, 2.0, True)
    assert FLOOR <= down < CEIL - 0.001
    assert FLOOR + 0.001 < up <= CEIL
    for p in (0.0, 1e-12, 0.5, 1 - 1e-12, 1.0):
        for sup in (True, False):
            assert FLOOR <= ex.update_belief(p, 0.3, sup) <= CEIL


@pytest.mark.parametrize("weight", [50.0, 1e6, 1e300, math.inf])
def test_huge_weights_are_numerically_stable(weight):
    hi = ex.update_belief(0.5, weight, True)
    lo = ex.update_belief(0.5, weight, False)
    assert not math.isnan(hi) and not math.isnan(lo)
    assert hi == pytest.approx(CEIL) and lo == pytest.approx(FLOOR)


def test_update_belief_rejects_invalid_input():
    with pytest.raises(ValueError):
        ex.update_belief(0.5, -0.1, True)
    with pytest.raises(ValueError):
        ex.update_belief(float("nan"), 1.0, True)
    with pytest.raises(ValueError):
        ex.update_belief(0.5, float("nan"), False)


# ================================================================ (b) should_interrupt

L = Limits()  # threshold 1.0；勿扰 22:00–08:00；每小时 3 次；专注 ×3、开会 ×5；紧急最低置信度 0.5


def test_interrupt_when_worth_it_and_user_is_free():
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("10:00")), [], L)
    assert (d.action, d.reason) == ("interrupt", "worth_it")
    assert d.score == pytest.approx(1.8)


def test_drop_when_not_worth_it_including_exactly_at_threshold():
    d = ex.should_interrupt(1.0, 0.2, 0.6, Context(now=t("10:00")), [], L)
    assert (d.action, d.reason) == ("drop", "not_worth_it")
    assert d.score == pytest.approx(-0.4)
    d = ex.should_interrupt(2.0, 0.5, 0.0, Context(now=t("10:00")), [], L)  # 净收益正好 1.0：不算"值得"
    assert d.action == "drop"


def test_focus_and_meeting_defer_instead_of_interrupting():
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("10:00"), focus=True), [], L)
    assert (d.action, d.reason) == ("defer", "busy")
    assert d.score == pytest.approx(2.4 - 0.6 * 3)
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("10:00"), in_meeting=True), [], L)
    assert (d.action, d.reason) == ("defer", "busy")
    assert d.score == pytest.approx(2.4 - 0.6 * 5)
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("10:00"), focus=True, in_meeting=True), [], L)
    assert d.score == pytest.approx(2.4 - 0.6 * 5)  # 两者都有：取较大的倍数，不是相乘
    # 收益足够大时，专注中也值得打断
    d = ex.should_interrupt(8.0, 0.9, 0.6, Context(now=t("10:00"), focus=True), [], L)
    assert d.action == "interrupt"


def test_quiet_hours_wrap_around_midnight():
    for now in (t("22:00"), t("23:30"), t("03:00"), t("07:59"), t("23:30", day=1)):
        d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=now), [], L)
        assert (d.action, d.reason) == ("defer", "quiet_hours"), now
        assert d.score == pytest.approx(1.8)
    for now in (t("08:00"), t("21:59"), t("12:00", day=1)):
        assert ex.should_interrupt(3.0, 0.8, 0.6, Context(now=now), [], L).action == "interrupt", now
    # 勿扰时段优先于"忙"：深夜在专注，理由也是 quiet_hours
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("23:00"), focus=True), [], L)
    assert d.reason == "quiet_hours"
    # 不跨午夜的勿扰时段（午休 12:00–13:00）；起止相同 = 没有勿扰时段
    lunch = Limits(quiet_start=t("12:00"), quiet_end=t("13:00"))
    assert ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("12:30")), [], lunch).reason == "quiet_hours"
    assert ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("23:30")), [], lunch).action == "interrupt"
    none = Limits(quiet_start=0, quiet_end=0)
    assert ex.should_interrupt(3.0, 0.8, 0.6, Context(now=t("03:00")), [], none).action == "interrupt"


def test_urgent_overrides_quiet_hours_focus_and_rate_limit():
    now = t("03:00")
    recent = [now - 50, now - 30, now - 10, now - 5]
    d = ex.should_interrupt(8.0, 0.9, 0.6, Context(now=now, focus=True, in_meeting=True, urgent=True), recent, L)
    assert (d.action, d.reason) == ("interrupt", "urgent_override")
    assert d.score == pytest.approx(8.0 * 0.9 - 0.6)  # 紧急事件不乘情境倍数


def test_urgent_with_low_confidence_or_low_value_does_not_page():
    d = ex.should_interrupt(8.0, 0.3, 0.6, Context(now=t("03:00"), urgent=True), [], L)
    assert (d.action, d.reason) == ("defer", "urgent_low_confidence")
    d = ex.should_interrupt(1.0, 0.9, 0.6, Context(now=t("03:00"), urgent=True), [], L)
    assert (d.action, d.reason) == ("drop", "not_worth_it")


def test_rate_limit_counts_only_the_last_60_minutes():
    now = t("16:50")
    # now-60 不在窗口里（窗口是 (now-60, now]）；窗口里只有 2 次 → 还能打扰
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=now), [now - 120, now - 60, now - 30, now - 5], L)
    assert d.action == "interrupt"
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=now), [now - 59, now - 30, now - 1], L)
    assert (d.action, d.reason) == ("defer", "rate_limited")
    assert d.score == pytest.approx(1.8)
    # 上限可配置
    d = ex.should_interrupt(3.0, 0.8, 0.6, Context(now=now), [now - 1], Limits(max_per_hour=1))
    assert d.reason == "rate_limited"


def test_should_interrupt_validates_inputs():
    with pytest.raises(ValueError):
        ex.should_interrupt(3.0, 1.2, 0.6, Context(now=600), [], L)
    with pytest.raises(ValueError):
        ex.should_interrupt(3.0, -0.1, 0.6, Context(now=600), [], L)
    with pytest.raises(ValueError):
        ex.should_interrupt(3.0, 0.5, -1.0, Context(now=600), [], L)


# ================================================================ (c) forget / explain


def _model() -> UserModel:
    m = UserModel("u1")
    m.add("likes_digest", "非紧急消息你更喜欢攒到早上一起看")
    # 故意乱序写入：explain 要按时间排序
    m.observe("likes_digest", weight=0.8, supports=True, t=900, source="feedback", note="你点开了早间摘要")
    m.observe("likes_digest", weight=0.5, supports=False, t=300, source="feedback", note="你秒回了一条深夜消息")
    m.observe("likes_digest", weight=1.0, supports=True, t=600, source="activity", note="你把 IM 设成了夜间免打扰")
    m.add("is_oncall", "本周你是值班工程师")
    m.observe("is_oncall", weight=3.0, supports=True, t=0, source="calendar", note="值班表")
    return m


def test_explain_lists_sorted_evidence_with_counts():
    m = _model()
    e = ex.explain(m, "likes_digest")
    assert e["key"] == "likes_digest"
    assert e["statement"] == "非紧急消息你更喜欢攒到早上一起看"
    assert e["confidence"] == pytest.approx(round(m.beliefs["likes_digest"].confidence, 3))
    assert e["pinned"] is False
    assert (e["n_support"], e["n_refute"]) == (2, 1)
    assert [x["t"] for x in e["evidence"]] == [300, 600, 900]
    assert set(e["evidence"][0]) >= {"t", "source", "note", "weight", "supports"}
    assert e["evidence"][0]["supports"] is False and e["evidence"][0]["weight"] == 0.5
    pct = f"{round(m.beliefs['likes_digest'].confidence * 100)}%"
    assert pct in e["summary"] and "非紧急消息你更喜欢攒到早上一起看" in e["summary"]


def test_explain_returns_copies_not_live_data():
    m = _model()
    e = ex.explain(m, "likes_digest")
    e["evidence"][0]["note"] = "被调用方篡改"
    e["evidence"].clear()
    again = ex.explain(m, "likes_digest")
    assert len(again["evidence"]) == 3
    assert again["evidence"][0]["note"] == "你秒回了一条深夜消息"


def test_explain_reflects_user_correction():
    m = _model()
    m.correct("likes_digest", False, t=1000)
    e = ex.explain(m, "likes_digest")
    assert e["pinned"] is True
    assert e["confidence"] == pytest.approx(0.03)
    assert e["evidence"][-1]["source"] == "user_correction"
    with pytest.raises(KeyError):
        ex.explain(m, "no_such_key")


def test_forget_removes_belief_and_blocks_relearning():
    m = _model()
    assert ex.forget(m, "likes_digest") is True
    assert "likes_digest" not in m.beliefs
    with pytest.raises(KeyError):
        ex.explain(m, "likes_digest")
    # 同样的信号再来一次：不能被"学"回来
    assert m.observe("likes_digest", weight=2.0, supports=True, t=1200, source="feedback", note="x",
                     statement="非紧急消息你更喜欢攒到早上一起看") is None
    assert m.add("likes_digest", "换个说法也不行") is None
    assert "likes_digest" not in m.beliefs
    assert ex.forget(m, "likes_digest") is False  # 已经没有了


def test_forget_unknown_key_still_blocks_and_leaves_others_alone():
    m = _model()
    before = ex.explain(m, "is_oncall")
    assert ex.forget(m, "house_hunting") is False
    assert "house_hunting" in m.blocked
    assert m.observe("house_hunting", weight=1.0, supports=True, t=5, source="email", note="租房邮件",
                     statement="你最近在找房子") is None
    assert ex.explain(m, "is_oncall") == before  # 其他推断不受影响
