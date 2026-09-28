"""第 16 课集成测试：教学实现里"声称做到了"的能力，逐条用确定性的量证明。

这些测试不属于练习——它们不依赖 exercise.py，在练习没写完时也会通过：
  - 配置中心：读-改-写是原子的（并发修改一个都不丢）、每次修改都有审计、读不到时保留最后一次成功的配置；
  - 真 worker 进程（WorkerPool + ops_app.py）：开关 / 灰度配置写进配置中心后，**每个**进程都在轮询间隔内看到新版本，
    之后的请求全部按新配置处理（写工具一次都没执行、分流到新版本）；
  - 灰度控制器：用真实运行结果（有 bug 的 v3 真的抛异常）算指标，自动回滚并把回滚写进配置中心；
  - 在线影子：主路径不等影子；影子有并发上限和超时；停机时被取消；影子出错不影响主路径。
断言的都是次数、版本号、在途数；时间只作宽松上限（机器负载高时也不会误报）。

    .venv/bin/python -m pytest lessons/16_release_ops/test_integration.py -v
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agentkit import Agent, Hook, ScriptedLLM, call_tool, reply, tool
from agentkit.distributed import SQLiteJobQueue, WorkerPool
from agentkit.testing import load_sibling

cc = load_sibling(__file__, "configcenter")
itb = load_sibling(__file__, "itbuddy")
reg_mod = load_sibling(__file__, "registry")
shadow = load_sibling(__file__, "shadow")
ops = load_sibling(__file__, "ops_app")

HERE = Path(__file__).resolve().parent
APP = f"{HERE / 'ops_app.py'}:make_handler"
NAME = "itbuddy.system"
RELEASE = f"release:{NAME}"


def load_rollout():
    """rollout.py 用 `from registry import ...`（Demo 以脚本方式运行时课程目录在 sys.path 上）；测试里临时加上。"""
    import sys

    sys.path.insert(0, str(HERE))
    try:
        return load_sibling(__file__, "rollout")
    finally:
        sys.path.remove(str(HERE))


async def eventually(cond, timeout: float = 30.0, what: str = "条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"{timeout}s 内没有等到{what}")


# ------------------------------------------------------------------ 配置中心（单进程语义）


async def test_update_is_atomic_versioned_and_audited(tmp_path):
    a = cc.ConfigCenter(tmp_path / "c.db")
    b = cc.ConfigCenter(tmp_path / "c.db")  # 两个实例各用自己的连接，和两个进程一样只通过文件共享
    await a.setup()

    def bump(doc):
        doc["n"] = doc.get("n", 0) + 1

    await asyncio.gather(*(c.update("flags", bump, actor="t", reason="压测") for c in [a, b] * 25))
    version, doc = await b.get("flags")
    assert (version, doc["n"]) == (50, 50), "50 次并发读-改-写，一次都不能丢"
    audit = await a.audit("flags")
    assert [e["version"] for e in audit] == list(range(1, 51))
    assert audit[-1]["before"] == {"n": 49} and audit[-1]["after"] == {"n": 50}
    with pytest.raises(ValueError):
        await a.update("flags", bump, actor="t", reason="  ")  # 没有原因的变更直接拒绝
    assert await a.get("missing") == (0, {})
    await a.close()
    await b.close()


async def test_watcher_serves_last_known_config_when_center_is_unreadable(tmp_path):
    center = cc.ConfigCenter(tmp_path / "c.db")
    await center.setup()
    await center.set("flags", {"disabled_tools": ["refund"]}, actor="t", reason="初始")
    seen = []
    watcher = cc.ConfigWatcher(center, ["flags"], poll_interval=0.02, on_change=lambda *a: seen.append(a[2]))
    await watcher.start()
    assert watcher.snapshot("flags") == {"disabled_tools": ["refund"]} and watcher.version("flags") == 1
    await center.set("flags", {}, actor="t", reason="恢复")
    await eventually(lambda: watcher.version("flags") == 2, 10, "看到 v2")
    assert seen == [2] and watcher.snapshot("flags") == {}

    async def broken(names):
        raise OSError("database is locked")

    center.versions = broken  # 配置中心读不到了
    polls = watcher.polls
    await eventually(lambda: watcher.errors >= 2 and watcher.polls > polls, 10, "轮询失败")
    assert watcher.snapshot("flags") == {} and watcher.version("flags") == 2  # fail-static：继续用最后一次读到的
    await watcher.aclose()
    await center.close()


# ------------------------------------------------------------------ 真 worker 进程


async def _fleet(tmp_path, reg, *, poll: float = 0.1):
    db = tmp_path / "ops.db"
    queue = SQLiteJobQueue(db)
    await queue.setup()
    center = cc.ConfigCenter(queue.db)
    await center.setup()
    await queue.db.write(ops.create_tables)
    await center.set(RELEASE, reg.release_doc(NAME), actor="t", reason="初始")
    await center.set("flags", {}, actor="t", reason="初始")
    pool = WorkerPool(f"sqlite:///{db}", APP, n=3, concurrency=8, lease=30, poll=0.02, grace=10,
                      options={"poll": poll, "latency": 0.005}, log_dir=tmp_path / "logs", name="w")
    return queue, center, pool


async def _send(queue, requests: list[dict], phase: str) -> list[dict]:
    for r in requests:
        await queue.enqueue("request", {**r, "phase": phase}, tenant_id=r["tenant"])

    async def rows():
        got = await queue.db.run(lambda c: c.execute("SELECT * FROM requests WHERE phase = ?", (phase,)).fetchall())
        return [dict(x) for x in got] if len(got) >= len(requests) else None

    return await eventually(rows, 60, f"{phase} 处理完")


def _seen_by_all(pool, name: str, version: int) -> dict[str, float] | None:
    got = {}
    for e in pool.events("config_changed"):
        if e.get("name") == name and e.get("version", 0) >= version:
            got.setdefault(e["worker_id"], e["t"])
    return got if len(got) == len(pool.workers) else None


def _registry() -> "reg_mod.PromptRegistry":
    reg = reg_mod.PromptRegistry()
    reg.publish(NAME, itb.PROMPT_V1, model="scripted", author="a", change_note="v1", params={"max_steps": 4})
    reg.publish(NAME, itb.PROMPT_V2, model="scripted", author="b", change_note="v2", params={"max_steps": 4})
    return reg


async def test_kill_switch_flip_reaches_every_worker_process(tmp_path):
    reg = _registry()
    queue, center, pool = await _fleet(tmp_path, reg, poll=0.1)
    repair = [{"user_id": f"u{i:05d}", "tenant": "acme", "input": "打印机坏了，帮我报修"} for i in range(9)]
    try:
        with pool:
            await eventually(lambda: len(pool.events("started")) == 3, 60, "3 个 worker 启动")
            before = await _send(queue, repair, "before")
            assert all(r["blocked"] == 0 for r in before)
            n_tickets = (await queue.db.run(lambda c: c.execute("SELECT count(*) FROM tickets").fetchone()))[0]
            assert n_tickets == 9  # 开关打开前：每个报修都真的建了单

            t_flip = time.time()
            version = await center.update("flags", lambda d: {"disabled_tools": ["create_ticket"]},
                                          actor="oncall", reason="紧急停用")
            seen = await eventually(lambda: _seen_by_all(pool, "flags", version), 30, "3 个进程都看到新开关")
            after = await _send(queue, repair, "after")
            workers = {r["worker"] for r in before + after}
            logs = pool.logs()
        assert len({e["pid"] for e in pool.events("config_changed")}) == 3, logs  # 三个不同的操作系统进程
        assert max(t - t_flip for t in seen.values()) < 5.0  # 宽松上限；Demo 里实测都在一个轮询间隔（0.2s）内
        assert all(r["blocked"] == 1 and r["flags_v"] == version for r in after), "看到新开关之后，所有请求都被拦下"
        n_after = (await queue.db.run(lambda c: c.execute("SELECT count(*) FROM tickets").fetchone()))[0]
        assert n_after == 9, "开关生效后写工具一次都没有再执行"
        assert len(workers) >= 2  # 请求确实分散在多个进程上
    finally:
        await queue.close()


async def test_worker_processes_route_by_the_published_rollout(tmp_path):
    reg = _registry()
    queue, center, pool = await _fleet(tmp_path, reg, poll=0.1)
    traffic = itb.make_traffic(30, seed=1)
    try:
        with pool:
            await eventually(lambda: len(pool.events("started")) == 3, 60, "3 个 worker 启动")
            reg.start_rollout(NAME, 2, 100, actor="bot")  # 100%：所有人都应该走 v2
            v = await center.set(RELEASE, reg.release_doc(NAME), actor="bot", reason="v2 100%")
            await eventually(lambda: _seen_by_all(pool, RELEASE, v), 30, "所有进程看到灰度配置")
            on_v2 = await _send(queue, traffic, "v2")
            reg.rollback(NAME, actor="bot", reason="测试回滚")
            v = await center.set(RELEASE, reg.release_doc(NAME), actor="bot", reason="回滚")
            await eventually(lambda: _seen_by_all(pool, RELEASE, v), 30, "所有进程看到回滚")
            back = await _send(queue, traffic, "back")
    finally:
        await queue.close()
    assert {r["version"] for r in on_v2} == {2} and {r["version"] for r in back} == {1}
    urgent = [r for r in on_v2 if "很急" in next(t["input"] for t in traffic if t["user_id"] == r["user_id"])]
    assert urgent, "流量里应该有紧急请求"


# ------------------------------------------------------------------ 灰度控制器：真实运行 → 指标 → 回滚


async def test_controller_rolls_back_buggy_version_using_real_run_results(tmp_path):
    rollout = load_rollout()
    reg = _registry()
    reg.publish(NAME, itb.PROMPT_V3, model="scripted", author="c", change_note="v3：lookup_asset", params={"max_steps": 4})
    center = cc.ConfigCenter(tmp_path / "c.db")
    await center.setup()
    ctl = rollout.RolloutController(reg, NAME, center=center, stages=(50, 100),
                                    thresholds=rollout.Thresholds(min_requests=40, max_latency_ratio=100))
    await ctl.start(3)
    tools = itb.make_tools()
    llm = ScriptedLLM(responder=itb.scripted_model, model="scripted", keep_calls=0)
    records = {1: [], 3: []}
    for r in itb.make_traffic(200, seed=9):  # 在本进程里跑真实的 Agent（模型是剧本，工具异常是真的）
        version = reg_mod.pick_version(r["user_id"], reg.rollout(NAME))
        pv = reg.get(NAME, version)
        counter = {"errors": 0}

        class Count(Hook):
            def after_tool(self, state, call, result):
                counter["errors"] += result.error_type in ("exception", "timeout")

        res = await Agent(llm, tools, system_prompt=pv.template, hooks=[Count()]).run(r["input"])
        records[version].append({"status": res.status, "errors": counter["errors"], "latency_ms": 1.0,
                                 "cost_usd": res.cost_usd})
    decision = await ctl.step(rollout.metrics_from_runs(records[3]), rollout.metrics_from_runs(records[1]))
    assert decision.action == "rollback", decision.reasons
    assert reg.rollout(NAME).candidate is None
    version, doc = await center.get(RELEASE)
    assert version == ctl.published == 2 and doc["rollout"]["candidate"] is None  # 回滚已经写进配置中心
    assert (await center.audit(RELEASE))[-1]["reason"].startswith("自动回滚")
    assert sum(x["errors"] > 0 for x in records[3]) > sum(x["errors"] > 0 for x in records[1])
    await center.close()


# ------------------------------------------------------------------ 在线影子


def _runner(llm, **agent_kw):
    async def run(text):
        return await Agent(llm, [], **agent_kw).run(text)

    return run


async def test_shadow_never_delays_primary_is_bounded_and_cancelled_on_shutdown():
    primary = ScriptedLLM(responder=lambda m: reply("好的"), latency=0.01)
    slow = ScriptedLLM(responder=lambda m: reply("好的"), latency=30.0)  # 候选版本卡住了
    runner = shadow.ShadowRunner(_runner(slow), max_concurrency=2, timeout_s=60)
    t0 = time.monotonic()
    for i in range(5):
        res = await runner.serve(f"问题 {i}", _runner(primary))
        assert res.output == "好的"
    assert time.monotonic() - t0 < 10  # 宽松上限：主路径没有等那 30 秒
    assert runner.stats["started"] == 2 and runner.stats["dropped"] == 3, "满了就丢弃，不排队"
    assert runner.stats["max_in_flight"] == 2 and slow.in_flight == 2
    assert await runner.aclose() == 2
    assert slow.in_flight == 0 and runner.stats["cancelled"] == 2  # 停机：影子的模型调用真的被取消了


async def test_shadow_timeout_and_errors_stay_off_the_primary_path():
    primary = ScriptedLLM(responder=lambda m: reply("好的"))
    slow = ScriptedLLM(responder=lambda m: reply("x"), latency=30.0)
    runner = shadow.ShadowRunner(_runner(slow), max_concurrency=4, timeout_s=0.05)
    await runner.serve("q", _runner(primary))
    await runner.drain(timeout=10)
    await asyncio.sleep(0)  # 让比较回调跑完
    assert runner.stats["timeout"] == 1 and slow.in_flight == 0
    assert runner.diffs[0].verdict == "candidate_error"

    async def boom(text):
        raise RuntimeError("候选版本崩了")

    runner = shadow.ShadowRunner(boom, max_concurrency=4)
    res = await runner.serve("q", _runner(primary))
    await runner.drain(timeout=10)
    await asyncio.sleep(0)
    assert res.output == "好的" and runner.diffs[0].verdict == "candidate_error"
    assert "崩了" in runner.diffs[0].notes[0]


async def test_shadow_tools_record_async_write_tools_without_running_them():
    ran = []

    @tool(risk="write")
    async def send_email(to: str) -> str:
        """发邮件"""
        ran.append(to)
        return "sent"

    intents = []
    twin = shadow.shadow_tools([send_email], intents)[0]
    llm = ScriptedLLM([call_tool("send_email", to="all@acme.com"), reply("好了")])
    res = await Agent(llm, [twin]).run("发通知")
    assert res.status == "completed" and ran == []
    assert intents == [{"tool": "send_email", "args": {"to": "all@acme.com"}}]


async def test_run_shadow_replays_both_versions_concurrently_in_order():
    stable = ScriptedLLM(responder=lambda m: reply(f"答：{m[-1]['content']}"), latency=0.05)
    cand = ScriptedLLM(responder=lambda m: reply(f"答：{m[-1]['content']}"), latency=0.05)
    diffs = await shadow.run_shadow([f"q{i}" for i in range(6)], run_stable=_runner(stable), run_candidate=_runner(cand),
                                    max_concurrency=3)
    assert [d.input for d in diffs] == [f"q{i}" for i in range(6)]
    assert all(d.verdict == "equivalent" for d in diffs)
    assert stable.max_in_flight == 3 and cand.max_in_flight == 3  # 两个版本并发，输入之间最多 3 个同时进行
