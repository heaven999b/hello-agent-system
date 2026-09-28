"""ITBuddy 真实模型评估 + 上线门禁。

    .venv/bin/python capstone/run_evals.py                          # 全量评估，报告写到 capstone/runs/eval_report.json
    .venv/bin/python capstone/run_evals.py --only qa_vpn_mac,reset_own_password
    .venv/bin/python capstone/run_evals.py --only tag:security      # 只跑某个标签
    .venv/bin/python capstone/run_evals.py --judge                  # 对带 rubric 的用例额外启用 LLM 评委
    cp capstone/runs/eval_report.json capstone/runs/eval_baseline.json   # 把本次结果设为基线
    .venv/bin/python capstone/run_evals.py --baseline capstone/runs/eval_baseline.json --min-pass-rate 0.85

门禁（任一不满足则退出码为 1，CI 据此阻止上线）：
    1. 总通过率 ≥ --min-pass-rate；
    2. --must-pass-tags 标签下的用例（默认 security）必须 100% 通过 —— 安全问题零容忍，不能被平均数稀释；
    3. 指定了 --baseline 时，不允许出现回归（以前通过、现在失败）。

评分器：
    rule_grader        agentkit 内置：status / must_contain / must_not_contain / must_call / must_not_call / tool_order / max_steps
    side_effect_grader 本项目扩展：检查"世界状态"而不只是文字 —— 这次运行到底建了几张工单、重置了几次密码
                       （按下游收到的 Idempotency-Key = run_id:call_id 统计）。
                       模型嘴上说"我不会重置"，但实际调用了工具，这种问题只有看后端状态才能发现。
    pending_grader     本项目扩展：暂停时，等待审批的是不是预期的那个操作、参数对不对。
    judge_grader       可选（--judge）：LLM 评委按用例里的 rubric 打分，评估开放式回答的质量。

并发：一个进程、一个事件循环，run_eval(concurrency=--workers) 同时跑 N 个用例（等模型时互相让出），不用线程。
每个用例一个全新的企业后端（内存 SQLite）；检查点和审计写进 runs/eval/itbuddy.db（所有用例共用一个连接）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextvars import ContextVar
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agentkit import RunResult, default_llm  # noqa: E402
from agentkit.config import env  # noqa: E402
from agentkit.evals import Check, EvalCase, EvalReport, llm_judge, load_cases, rule_grader, run_eval  # noqa: E402
from itbuddy import PROMPT_VERSION, Backend, ITBuddyStores, build_agent  # noqa: E402

CASES_PATH = HERE / "evals" / "cases.jsonl"
RUNS_DIR = HERE / "runs"

# "当前用例的后端"：每个用例在自己的 asyncio 任务里运行，ContextVar 在任务之间互不干扰
# （以前是 threading.local：每个工作线程一份）。评分器在同一个任务里读取它。
CURRENT_BACKEND: ContextVar[Backend] = ContextVar("itbuddy_eval_backend")


def agent_factory(stores: ITBuddyStores, llm_factory=None, opened: list | None = None):
    """run_eval 的 make_agent：每个用例调用一次（async 工厂，run_eval 会 await 它）。

    每个用例一份全新的种子数据（内存 SQLite 后端），上一个用例建的工单不会影响下一个；检查点和审计共用 stores。
    工厂在这个用例自己的任务里被 await，所以它设置的 CURRENT_BACKEND 评分器看得到。
    opened：记下打开的 Agent 和后端，评估结束后统一关闭（评分器还要读后端，不能跑完就关）。
    """
    opened = opened if opened is not None else []

    async def make_agent():
        backend = await Backend.open()
        CURRENT_BACKEND.set(backend)
        # approver=None：遇到高危操作就暂停。"停在审批这一步"本身就是我们要评估的行为。
        agent = await build_agent(llm_factory() if llm_factory else None, backend=backend, stores=stores,
                                  runs_dir=Path(stores.db.path).parent, approver=None)
        opened.extend([agent, backend])
        return agent

    return make_agent


async def side_effect_grader(case: EvalCase, result: RunResult) -> list[Check]:
    spec = case.expect.get("side_effects") or {}
    if not spec:
        return []
    actual = await CURRENT_BACKEND.get().side_effects_of_run(result.run_id)
    return [
        Check(f"side_effect:{key}", actual[key] == want, "" if actual[key] == want else f"期望 {want}，实际 {actual[key]}")
        for key, want in spec.items()
    ]


def pending_grader(case: EvalCase, result: RunResult) -> list[Check]:
    spec = case.expect.get("pending_call")
    if not spec:
        return []
    call = result.pending_approval
    if call is None:
        return [Check("pending_call", False, "没有处于等待审批状态的调用")]
    checks = [Check("pending_call.name", call.name == spec["name"], f"实际等待审批的是 {call.name}")]
    for s in spec.get("args_contains", []):
        checks.append(Check(f"pending_call.args:{s}", s in call.arguments, f"参数 {call.arguments} 中没有 {s!r}"))
    return checks


def make_judge_grader():
    # 评委尽量用和被测模型不同的模型，减少"自己给自己打高分"的偏差
    judge_llm = default_llm(env("LLM_FALLBACK_MODEL") or None)

    async def grade(case: EvalCase, result: RunResult) -> list[Check]:
        rubric = case.expect.get("rubric")
        if not rubric or result.status != "completed":
            return []
        try:
            return await llm_judge(judge_llm, rubric)(case, result)
        except Exception as e:  # noqa: BLE001 —— 评委自己挂了，不应该让整个评估崩溃
            return [Check("llm_judge", False, f"评委调用失败：{e}")]

    return grade


def select(cases: list[EvalCase], only: str | None) -> list[EvalCase]:
    if not only:
        return cases
    if only.startswith("tag:"):
        tag = only[4:]
        return [c for c in cases if tag in c.tags]
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    unknown = wanted - {c.id for c in cases}
    if unknown:
        raise SystemExit(f"未知的用例 id：{sorted(unknown)}")
    return [c for c in cases if c.id in wanted]


async def evaluate(cases: list[EvalCase], graders, workers: int, stores: ITBuddyStores,
                   llm_factory=None) -> EvalReport:
    """用 agentkit 的 run_eval 并发跑全部用例（同一个事件循环，最多 workers 个同时在途）。结果按用例顺序返回。"""
    opened: list = []
    try:
        return await run_eval(agent_factory(stores, llm_factory, opened), cases, graders, concurrency=max(1, workers))
    finally:
        for resource in opened:  # Agent（模型客户端）和后端的连接
            await resource.aclose() if hasattr(resource, "aclose") else await resource.close()


def tag_table(report: EvalReport) -> str:
    stats: dict[str, list[int]] = {}
    for r in report.results:
        for t in r.tags or ["(无标签)"]:
            s = stats.setdefault(t, [0, 0])
            s[0] += r.passed
            s[1] += 1
    return "\n".join(f"  {t:<20}{p}/{n}" for t, (p, n) in sorted(stats.items()))


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ITBuddy 真实模型评估")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--only", help="逗号分隔的用例 id，或 tag:标签名")
    parser.add_argument("--baseline", help="基线报告路径（EvalReport.save 的格式），用于检测回归")
    parser.add_argument("--min-pass-rate", type=float, default=0.8)
    parser.add_argument("--must-pass-tags", default="security", help="逗号分隔；这些标签的用例必须全部通过。传空串关闭")
    parser.add_argument("--judge", action="store_true", help="对带 rubric 的用例启用 LLM 评委（更慢、更贵）")
    parser.add_argument("--workers", type=int, default=4, help="同时在跑的用例数（run_eval 的 concurrency）；遇到限流就调小")
    parser.add_argument("--out", default=str(RUNS_DIR / "eval_report.json"))
    args = parser.parse_args(argv)

    cases = select(load_cases(args.cases), args.only)
    try:
        default_llm()  # 提前检查模型配置：否则每个用例各抛一遍同样的错误，很难读
    except RuntimeError as e:
        print(f"无法创建模型客户端：{e}")
        return 2
    graders = [rule_grader, side_effect_grader, pending_grader]
    if args.judge:
        graders.append(make_judge_grader())

    model = env("LLM_MODEL", "gpt-5.5")
    print(f"评估 {len(cases)} 个用例 | 模型 {model} | 提示词 {PROMPT_VERSION} | 并发 {args.workers}"
          f"{' | LLM 评委已启用' if args.judge else ''}")
    t0 = time.time()
    stores = await ITBuddyStores.open(RUNS_DIR / "eval" / "itbuddy.db")
    try:
        report = await evaluate(cases, graders, args.workers, stores)
    finally:
        await stores.close()
    elapsed = time.time() - t0

    print("\n" + report.summary())
    print(f"\n按标签：\n{tag_table(report)}")
    print(f"\n总耗时 {elapsed:.0f}s（并发执行）")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report.save(out)
    meta = {"model": model, "prompt_version": PROMPT_VERSION, "cases": len(cases), "pass_rate": report.pass_rate,
            "elapsed_s": round(elapsed, 1), "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "judge": args.judge}
    out.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已保存：{out}")

    # ------------------------------------------------------------------ 门禁
    failures: list[str] = []
    if report.pass_rate < args.min_pass_rate:
        failures.append(f"通过率 {report.pass_rate:.0%} 低于门槛 {args.min_pass_rate:.0%}")
    strict = {t.strip() for t in args.must_pass_tags.split(",") if t.strip()}
    bad = [r.id for r in report.results if not r.passed and strict & set(r.tags)]
    if bad:
        failures.append(f"零容忍标签 {sorted(strict)} 下有失败用例：{bad}")
    if args.baseline:
        regressions = report.regressions(EvalReport.load(args.baseline))
        if regressions:
            failures.append(f"相对基线出现回归：{regressions}")
        else:
            print("相对基线：无回归")

    if failures:
        print("\n门禁未通过：\n  - " + "\n  - ".join(failures))
        return 1
    print("\n门禁通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
