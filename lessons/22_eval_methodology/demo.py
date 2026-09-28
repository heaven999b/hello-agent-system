"""第 22 课 Demo：评估方法论 —— 先审查 benchmark，再用统计回答"B 真的比 A 好吗"，最后校准评委。

    python lessons/22_eval_methodology/demo.py --offline     # 离线：预置数据（见 offline_results.json 的说明），几秒
    python lessons/22_eval_methodology/demo.py               # 真实模型：2 个版本 × 16 个任务 × 3 次 = 96 次试验 + 评委 16 次调用
    python lessons/22_eval_methodology/demo.py --model claude-haiku-4-5-20251001   # 换一个被测模型
    python lessons/22_eval_methodology/demo.py --runs 2 --judge-model gpt-5.5       # 每题只跑 2 次；评委换一个模型

七节：
  1. 先审查 benchmark：ABC checklist + 探针 Agent，揭穿 v0 的漏洞，再确认 v1 修好了
  2. 跑评估：两个 prompt 版本 × 16 个任务 × k 次（环境重置、并发 ≤ 2、结果缓存、基础设施错误单独处理）
     harness 是 async 的：所有试验在一个事件循环里并发（asyncio + 并发上限，没有线程）；
     离线模式也会用剧本模型真实跑一遍 harness，打印同时在途峰值，证明并发真的发生、上限守住了
  3. 只看点估计
  4. 加上置信区间 —— 以及同一版本几轮之间的波动
  5. 配对检验：逐任务对比 + 配对 bootstrap + McNemar；样本量；45/50 vs 43/50
  6. 小而准：随机抽样 vs 分层抽样
  7. 成对 LLM 评委：不交换顺序 vs 交换顺序；与人工标注的一致率和 kappa

真实模式的结果缓存在 lessons/22_eval_methodology/runs/：同一天再跑不会重复调用模型（--fresh 强制重跑）。
入口是 asyncio.run(main())。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import random
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Literal

from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
OFFLINE = HERE / "offline_results.json"
MAX_CONCURRENCY = 2  # 本地网关被多人共用：同时在途的模型调用控制在 2 以内


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录模块（与 solution.py 的同名函数一致，保证全进程只有一份）。"""
    key = f"{HERE.name}__{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


evalstats = _load_sibling("evalstats")
rb = _load_sibling("refund_bench")
IMPL = evalstats  # 练习里的三个函数：main() 里会优先换成 exercise.py 的实现


def section(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, w: int) -> str:
    return text + " " * max(0, w - width(text))


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def load_impl():
    """优先用你在 exercise.py 里的实现；还没写完就用 evalstats.py 里的参考实现。"""
    ex = _load_sibling("exercise")
    try:
        ex.wilson_interval(1, 2)
        ex.paired_bootstrap_diff([0], [1], n_boot=1)
        ex.debiased_pairwise(lambda a, b: "tie", "x", "y")
        return ex, "exercise.py（你的实现）"
    except NotImplementedError:
        return evalstats, "evalstats.py（参考实现 —— 完成练习后会自动换成你的实现）"


# ---------------------------------------------------------------- 被测系统：同一个 Agent 的两个 prompt 版本

PROMPT_A = f"""你是云杉家居的售后退款助手，负责批量处理退款工单。

{rb.POLICY}

每个工单必须且只能调用一次决策工具：issue_refund（直接退款）、reject_refund（拒绝）或 escalate（转人工）。
这是离线批处理，客户看不到追问，请直接作出决定。"""

PROMPT_B = (
    PROMPT_A
    + """

决策前请按下面的顺序逐条核对，算清楚之后再调用工具：
1. 历史退款记录为"有" → reject_refund（R7）。
2. 算签收天数 = 今天日期 - 签收日期（跨月时注意上个月有几天）。
3. 有质量问题凭证且天数 ≤ 30 → 应退全额；没有凭证的"质量问题"按非质量问题处理。
4. 否则：定制商品 → reject_refund（R4）；天数 ≤ 7 且未拆封 → 应退全额；天数 ≤ 7 且已拆封 → 应退实付 × 0.8；其余 → reject_refund（R5）。
5. 应退金额 > 2000 → escalate（R6）；否则 issue_refund，金额保留两位小数。"""
)

VERSIONS = {"A": PROMPT_A, "B": PROMPT_B}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


# =====================================================================
# 第 1 节：先审查 benchmark
# =====================================================================


def section_audit(tasks: list) -> None:
    section("第 1 节：先审查 benchmark —— 用 ABC checklist 和探针 Agent 找漏洞")
    print(
        "在拿 benchmark 比较 A 和 B 之前，先确认它量的是我们想量的东西。\n"
        "v0 是一位同事一下午写出来的版本，v1 是按 ABC checklist 修过的版本（本课后面都用 v1）。\n"
        "审查方法：派几个'专门钻空子'的探针 Agent（不调用模型，纯规则）去跑，看它们能拿多少分。\n"
    )
    build = rb.FlawedBenchV0.V0_BUILD_DATE
    later = date(2026, 12, 19)

    def fmt(r: list[bool]) -> str:
        return f"{sum(r)}/{len(r)} {pct(sum(r) / len(r))}"

    def v0(agent, clock=build, after=None) -> str:
        bench = rb.FlawedBenchV0(tasks, clock=lambda: clock)
        if after is not None:
            bench.run(after)  # 同一个环境里先跑过另一个 Agent，而且没有重置
        return fmt(bench.run(agent))

    def v1(agent, as_of=None) -> str:
        return fmt(rb.run_v1(agent, tasks, as_of=as_of))

    rows = [
        ("什么都不做（空回复、不调工具）", v0(rb.do_nothing), v1(rb.do_nothing), "O.g.3 只查'没退款'就算拒绝成功；R.13 没报告这个基线"),
        ("固定话术'很抱歉，暂时无法为您退款…'", v0(rb.canned_reply), v1(rb.canned_reply), "O.b.1/O.b.2 子串匹配：'无法退款'也含'退款'"),
        ("偷看标注（读 get_order 的内部字段）", v0(rb.peeker), v1(rb.peeker), "T.5 Agent 能看到标准答案"),
        ("参考解（把政策写成代码）", v0(rb.oracle_agent), v1(rb.oracle_agent), "T.7/T.9 标注没用参考解核对过"),
        (f"参考解，{later} 再跑一次", v0(rb.oracle_agent, clock=later), v1(rb.oracle_agent, as_of=later), "T.6 按系统日期算天数，结果随时间漂移"),
        ("什么都不做，接在参考解后面跑", v0(rb.do_nothing, after=rb.oracle_agent), v1(rb.do_nothing), "T.4 账本从不重置，上一轮的状态漏进下一轮"),
    ]
    print(f"{pad('探针 Agent', 38)}{pad('v0 得分', 14)}{pad('v1 得分', 14)}暴露的问题（ABC 编号）")
    for name, s0, s1, issue in rows:
        print(f"{pad(name, 38)}{pad(s0, 14)}{pad(s1, 14)}{issue}")

    bench = rb.FlawedBenchV0(tasks, clock=lambda: build)
    print("\n参考解 vs 人工标注（ABC T.7 / T.9）：")
    for t in tasks:
        o = rb.oracle_decide(t)
        if bench.labels[t.id] != o["action"]:
            amount = f" {o['amount']:.2f}" if "amount" in o else ""
            print(f"  v0 {t.id}：标注 {bench.labels[t.id]}，参考解 {o['action']}{amount}（{o['rule']}）"
                  f"—— 签收第 {t.days} 天，政策写的是'含第 7 天'，是标注错了")
    print(f"  v1：参考解与全部 {len(tasks)} 条标注逐条核对，{len(rb.label_mismatches(tasks))} 处不一致")
    print(
        "\n探针查不出、要靠人读代码、读 transcript 才能发现的问题：\n"
        "  - R.10 只报一个通过率、没有置信区间（→ 第 4 节）\n"
        "  - 脚手架不真实：v0 评估时给 Agent 的工具（get_order 带内部字段）和线上不一样（Anthropic 路线图 Step 4）\n"
        "  - 我们自己踩的坑：模型网关会往系统提示里注入真实日期，和任务里写死的日期打架（讲义 5.1）\n"
        "\n启示：v0 上，一个只会说固定话术的 Agent 能拿 81%，一个偷看答案的 Agent 能拿满分 ——\n"
        "      在这样的 benchmark 上比较 A 和 B，比出来的只是'谁更会钻空子'。"
    )


# =====================================================================
# 第 2 节：跑评估（harness）
# =====================================================================


def load_jsonl(path: Path) -> dict[str, dict]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                out[row["key"]] = row
    return out


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def run_real(args, tasks: list, llm) -> tuple[list[dict], dict]:
    """真实运行：缓存命中的直接复用，其余并发（≤ 2）执行；基础设施错误重试一轮，仍失败就中止。"""
    cache_path = RUNS / "trials.jsonl"
    cache = {} if args.fresh else load_jsonl(cache_path)
    as_of = date.today()  # 请求里的日期按运行当天渲染（签收天数不变），所以它也是缓存键的一部分
    rows, jobs = [], []
    for v, prompt in VERSIONS.items():
        fp = fingerprint(prompt)
        for t in tasks:
            for k in range(args.runs):
                key = f"{llm.model}|{fp}|{as_of}|{t.id}|{k}"
                if key in cache:
                    rows.append({**cache[key], "version": v})
                else:
                    jobs.append(rb.TrialJob(key, v, prompt, t, k))
    hits = len(rows)
    print(f"缓存命中 {hits} 次，需要新跑 {len(jobs)} 次试验（并发 {MAX_CONCURRENCY}）……")
    t0 = time.time()

    def progress(i: int, total: int) -> None:
        if i % 16 == 0 or i == total:
            print(f"  进度 {i}/{total}  已用 {time.time() - t0:.0f}s")

    for attempt in (1, 2):
        # 所有试验在一个事件循环里并发，同时在途 ≤ MAX_CONCURRENCY；返回的行按 jobs 的顺序
        results = await rb.run_trials(llm, jobs, as_of=as_of, concurrency=MAX_CONCURRENCY, on_progress=progress)
        new_rows = [r for r in results if not r["error"]]
        errors = [r for r in results if r["error"]]
        append_jsonl(cache_path, new_rows)  # 只缓存成功的试验；出错的下次还会重跑
        rows += new_rows
        if not errors:
            break
        print(f"  ⚠️ {len(errors)} 次试验因基础设施错误失败（例：{errors[0]['error'][:120]}）")
        err_keys = {e["key"] for e in errors}
        jobs = [j for j in jobs if j.key in err_keys]
        if attempt == 2:
            sys.exit(
                "评估中止：重试后仍有基础设施错误。不要把'模型 API 不可用'记成'Agent 失败'（ABC T.3）。\n"
                "修好环境后重跑即可（成功的试验已缓存，不会重复调用）。"
            )
        print("  重试这些试验……")
    meta = {
        "model": llm.model,
        "runs": args.runs,
        "new_trials": len(rows) - hits,
        "cache_hits": hits,
        "tokens": sum(r["tokens"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 5),
    }
    return rows, meta


async def harness_smoke(tasks: list) -> None:
    """离线模式也把 harness 真实跑一遍：剧本模型按参考解作答（每次调用 50ms），2 个版本 × 16 个任务 × 1 次。
    统计用的是构造数据，但调度、环境重置、停止条件、评分这条代码路径和真实模式完全相同。"""
    llm = rb.scripted_oracle_llm(tasks, latency=0.05)
    jobs = [rb.TrialJob(f"smoke|{v}|{t.id}", v, prompt, t, 0) for v, prompt in VERSIONS.items() for t in tasks]
    t0 = time.perf_counter()
    rows = await rb.run_trials(llm, jobs, concurrency=MAX_CONCURRENCY)
    secs = time.perf_counter() - t0
    print(
        f"[离线] harness 冒烟：剧本模型按参考解作答（每次调用 {llm.latency * 1000:.0f}ms），{len(jobs)} 次试验 → "
        f"{sum(r['passed'] for r in rows)}/{len(rows)} 通过；模型调用 {llm.call_count} 次（作出决定即停：每次试验 1 次）；\n"
        f"       同时在途峰值 {llm.max_in_flight}（上限 {MAX_CONCURRENCY}）；耗时 {secs:.2f}s"
        f"（一个接一个跑至少要 {len(jobs) * llm.latency:.1f}s，上限 2 时至少 {len(jobs) * llm.latency / MAX_CONCURRENCY:.1f}s）"
    )


async def section_run(args, tasks: list, llm) -> tuple[dict, dict]:
    section("第 2 节：跑评估 —— 同一个 Agent 的两个 prompt 版本，每题跑 k 次")
    print(
        "四元组：请求 = 客户留言 + 订单快照；环境 = 每次试验一个新账本 + 3 个决策工具 + 政策；\n"
        f"        停止条件 = 作出决定即停，最多 {rb.MAX_STEPS} 步；评分器 = 账本里恰好一个决定，类型和金额都对。\n"
        "A = 只给政策；B = 政策 + 一份'逐条核对'的决策清单。\n"
    )
    if args.offline:
        data = json.loads(OFFLINE.read_text(encoding="utf-8"))["trials"]
        rows, meta = data["rows"], {"model": data["model"], "runs": data["runs"]}
        await harness_smoke(tasks)
        print(f"[离线] {data['note']}")
    else:
        rows, meta = await run_real(args, tasks, llm)
    k = meta["runs"]
    outcomes: dict[str, dict[str, list[bool]]] = {v: {t.id: [False] * k for t in tasks} for v in VERSIONS}
    decisions: dict[str, dict[str, list[str]]] = {v: {t.id: [""] * k for t in tasks} for v in VERSIONS}
    for r in rows:
        if r["trial"] < k and r["task_id"] in outcomes[r["version"]]:
            outcomes[r["version"]][r["task_id"]][r["trial"]] = r["passed"]
            decisions[r["version"]][r["task_id"]][r["trial"]] = "" if r["passed"] else r["decision"]
    print(f"被测模型 {meta['model']} ｜ 每个版本 {len(tasks)} 个任务 × {k} 次 = {len(tasks) * k} 次试验")
    if not args.offline:
        print(
            f"本次新跑 {meta['new_trials']} 次、缓存命中 {meta['cache_hits']} 次；"
            f"总 token {meta['tokens']:,}，估算成本 ${meta['cost_usd']:.4f}（按 agentkit/pricing.py 的占位价格）\n"
            "harness 做了什么：每次试验新建账本和 Agent（环境重置）；一个事件循环里并发，同时在途 ≤ 2（共享网关）；\n"
            "按（模型, prompt 指纹, 日期, 任务, 第几次）缓存到 runs/trials.jsonl —— 缓存用来'断点续跑'和'重新分析'，\n"
            "不是用来'少采样'：第几次也是键的一部分，所以 3 次就是 3 次独立的采样。"
        )
    return outcomes, decisions


# =====================================================================
# 第 3–5 节：统计
# =====================================================================


def section_point(outcomes: dict) -> None:
    section("第 3 节：只看点估计")
    rates = {}
    for v, per_task in outcomes.items():
        s = sum(sum(r) for r in per_task.values())
        n = sum(len(r) for r in per_task.values())
        rates[v] = s / n
        print(f"版本 {v}：{s}/{n} = {pct(s / n)}")
    diff = rates["B"] - rates["A"]
    if diff > 0:
        print(f"→ B 比 A 高 {diff * 100:.1f} 个百分点。评估报告如果只写到这里，结论多半是'B 更好，上线！'")
    elif diff < 0:
        print(f"→ B 比 A 低 {-diff * 100:.1f} 个百分点。评估报告如果只写到这里，结论多半是'清单没用，别上线'")
    elif rates["A"] == 1.0:
        print(
            "→ 两个版本都是 100%：这个 benchmark 对该模型已经饱和，比不出任何差别（Anthropic 路线图 Step 7）。\n"
            "  要么换更难的任务，要么换一个更弱的被测模型（如 --model claude-haiku-4-5-20251001）再比。"
        )
    else:
        print("→ 两个版本的通过率一模一样。")


def section_ci(outcomes: dict, tasks: list, k: int) -> None:
    section("第 4 节：加上置信区间")
    print(f"{pad('', 44)}{pad('版本 A', 26)}版本 B")
    line1 = pad("按'运行'算 Wilson（把每次试验当独立样本）", 44)
    line2 = pad("按'任务'算 bootstrap（任务是独立单元）", 44)
    cis = {}
    for v in VERSIONS:
        runs_all = [x for r in outcomes[v].values() for x in r]
        lo, hi = IMPL.wilson_interval(sum(runs_all), len(runs_all))
        line1 += pad(f"{pct(sum(runs_all) / len(runs_all))} [{pct(lo)}, {pct(hi)}]", 26)
        per_task = [sum(r) / len(r) for r in outcomes[v].values()]
        cis[v] = evalstats.bootstrap_ci(per_task, n_boot=4000, seed=0)
        line2 += pad(f"{pct(cis[v].estimate)} [{pct(cis[v].low)}, {pct(cis[v].high)}]", 26)
    print(line1)
    print(line2)
    overlap = cis["A"].high >= cis["B"].low and cis["B"].high >= cis["A"].low
    if all(ci.low == ci.high for ci in cis.values()):
        n_ok = sum(1 for r in outcomes["A"].values() if all(r))
        lo, hi = IMPL.wilson_interval(n_ok, len(tasks))
        print(
            "→ 所有任务的得分都一样时（这里是全对），bootstrap 重采样不出任何波动，区间宽度为 0 —— 这不代表'很确定'，\n"
            f"  只代表这个方法失效了。改用按任务数算的 Wilson：{n_ok}/{len(tasks)} → [{pct(lo)}, {pct(hi)}]，\n"
            f"  不确定性依然很大：{len(tasks)} 个任务全对，并不能排除真实通过率只有八成左右。"
        )
    else:
        if overlap:
            print("→ 两个版本的区间大幅重叠：单看各自的区间，说不清谁更好。")
        else:
            print("→ 两个版本的区间已经分开：差距大到'各算各的区间'都看得出来（仍要看第 5 节，确认差距来自哪些任务）。")
        print(
            f"  第二行更诚实：同一个任务的 {k} 次运行是相关的（难题 {k} 次都容易错），\n"
            f"  真正独立的样本是 {len(tasks)} 个任务，不是 {len(tasks) * k} 次运行。"
        )

    print("\n多次运行的方差：同一版本、同一批任务，每一轮单独算通过率：")
    spread = 0.0
    for v in VERSIONS:
        rounds = [sum(outcomes[v][t.id][i] for t in tasks) / len(tasks) for i in range(k)]
        flaky = [t.id for t in tasks if 0 < sum(outcomes[v][t.id]) < k]
        spread = max(spread, max(rounds) - min(rounds))
        print(
            f"  {v}：" + "  ".join(f"第 {i + 1} 轮 {pct(r)}" for i, r in enumerate(rounds))
            + f"   （极差 {(max(rounds) - min(rounds)) * 100:.1f} 个点；{len(flaky)} 个任务 {k} 次结果不一致"
            + (f"：{', '.join(flaky)}）" if flaky else "）")
        )
    if spread > 0:
        print(f"→ 同一个版本'自己和自己比'就能差 {spread * 100:.1f} 个点。比这个还小的'提升'，不值得相信。")
    else:
        print("→ 这次每一轮的结果完全一样：模型在这批任务上很稳定（也可能是任务太容易）。")


def mark(results: list[bool]) -> str:
    return "".join("✅" if x else "❌" for x in results)


def section_paired(outcomes: dict, tasks: list, k: int, decisions: dict) -> None:
    section("第 5 节：配对检验 —— 同一批任务，逐个比较")
    print(f"{pad('任务', 10)}{pad('类别', 10)}{pad('A', 2 * k + 3)}{pad('B', 2 * k + 3)}{pad('B-A', 7)}失败时的决定（A | B）")
    a_mean, b_mean, a_major, b_major = [], [], [], []
    for t in tasks:
        a, b = outcomes["A"][t.id], outcomes["B"][t.id]
        a_mean.append(sum(a) / k)
        b_mean.append(sum(b) / k)
        a_major.append(sum(a) * 2 > k)
        b_major.append(sum(b) * 2 > k)
        fa = ", ".join(sorted({x for x in decisions["A"][t.id] if x})) or "-"
        fb = ", ".join(sorted({x for x in decisions["B"][t.id] if x})) or "-"
        note = "" if fa == fb == "-" else f"{fa} | {fb}"
        print(f"{pad(t.id, 10)}{pad(t.category, 10)}{pad(mark(a), 2 * k + 3)}{pad(mark(b), 2 * k + 3)}{pad(f'{b_mean[-1] - a_mean[-1]:+.2f}', 7)}{note}")

    est, low, high = IMPL.paired_bootstrap_diff(a_mean, b_mean, n_boot=4000, seed=0)
    sig = low > 0 or high < 0
    print(
        f"\n配对 bootstrap（以任务为单位重采样；每个任务的得分 = {k} 次的平均）：\n"
        f"  B - A = {est * 100:+.1f}%  95% 区间 [{low * 100:+.1f}%, {high * 100:+.1f}%]  → "
        + ("区间不包含 0" if sig else "区间包含 0")
    )
    m_exact = evalstats.mcnemar(a_major, b_major, exact=True)
    m_chi2 = evalstats.mcnemar(a_major, b_major, exact=False)
    print(
        f"McNemar（每个任务按 {k} 次多数票算通过/失败）：只有 A 过 {m_exact.a_only} 个任务，只有 B 过 {m_exact.b_only} 个\n"
        f"  精确二项检验 p = {m_exact.p_value:.3f}   ← 不一致任务只有 {m_exact.discordant} 个（< 25），应该看这个\n"
        f"  卡方近似     p = {m_chi2.p_value:.3f}   ← 仅供对照：不一致任务这么少时，卡方近似不可靠"
    )
    better = "B 更好" if est > 0 else "B 更差"
    if sig and m_exact.p_value < 0.05:
        concl = f"两种配对检验都支持'{better}'。任务只有十几个，差距集中在哪几类任务上，要回到上表逐个看。"
    elif sig:
        concl = (
            f"配对 bootstrap 支持'{better}'，McNemar 还不显著：多数票把'{k} 次里错 1 次'这类信息丢掉了，\n"
            f"      而且任务很少时 bootstrap 区间偏乐观。证据偏向'{better}'，但还不够下定论 —— 加任务。"
        )
    elif m_exact.discordant == 0 and est == 0:
        concl = "两个版本逐任务的结果完全一样，没有任何差异可以检验。"
    else:
        concl = "配对检验也不支持'A、B 有差别'。点估计上的差距，很可能只是噪声。"
    print(f"结论：{concl}")

    # 样本量：用逐次配对（第 i 次对第 i 次）估计不一致比例 ψ 和差值 δ
    pairs = [(x, y) for t in tasks for x, y in zip(outcomes["A"][t.id], outcomes["B"][t.id])]
    n = len(pairs)
    pa = sum(x for x, _ in pairs) / n
    pb = sum(y for _, y in pairs) / n
    a_only = sum(x and not y for x, y in pairs)
    b_only = sum(y and not x for x, y in pairs)
    psi, delta = (a_only + b_only) / n, (b_only - a_only) / n  # 用整数计数算，避免浮点误差让 |δ| 略大于 ψ
    print(f"\n样本量：要在 α=0.05、80% 功效下可靠检测'A {pct(pa)} → B {pct(pb)}'这么大的差异，需要多少任务？")
    if delta != 0 and psi >= abs(delta):
        print(
            f"  两个版本各用一批不同的任务（独立样本）：每个版本约 {evalstats.min_sample_size(pa, pb)} 个任务\n"
            f"  同一批任务配对比较（本次逐次配对的不一致比例 ψ={pct(psi)}）：约 {evalstats.min_sample_size_paired(psi, delta)} 个任务\n"
            f"  → 本课只有 {len(tasks)} 个任务：能看出方向，远不够下精确的结论。"
        )
    else:
        print("  两个版本没有差异，无从估算。")

    print("\n另一个天天发生的场景：新版本 45/50，旧版本 43/50，PR 描述写着'通过率 +4 个点'。")
    lo1, hi1 = IMPL.wilson_interval(43, 50)
    lo2, hi2 = IMPL.wilson_interval(45, 50)
    old = [True] * 42 + [True] + [False] * 3 + [False] * 4  # 新版修好 3 个、弄坏 1 个
    new = [True] * 42 + [False] + [True] * 3 + [False] * 4
    typical = evalstats.mcnemar(old, new)
    print(
        f"  Wilson 区间：旧 86.0% [{pct(lo1)}, {pct(hi1)}]   新 90.0% [{pct(lo2)}, {pct(hi2)}]\n"
        f"  逐任务看，典型情况是新版修好 3 个、弄坏 1 个：McNemar 精确 p = {typical.p_value:.3f}\n"
        f"  要可靠检测 86% → 90%：独立样本每个版本约 {evalstats.min_sample_size(0.86, 0.90)} 个任务；"
        f"即使配对（ψ=8%），也要约 {evalstats.min_sample_size_paired(0.08, 0.04)} 个\n"
        "→ 50 个任务上差 2 个，几乎说明不了任何问题。要么加任务，要么只看'修好了哪几个、弄坏了哪几个'。"
    )


# =====================================================================
# 第 6 节：小而准
# =====================================================================


def section_small() -> None:
    section("第 6 节：小而准 —— 从 1000 个任务里抽 40 个，随机抽 vs 分层抽")
    strata = [("常规", 600, 0.95), ("边界", 250, 0.70), ("对抗", 100, 0.40), ("安全", 50, 0.20)]
    rng = random.Random(0)
    pool = []
    for name, size, p in strata:
        ok = round(size * p)
        flags = [1] * ok + [0] * (size - ok)
        rng.shuffle(flags)
        pool += [(name, f) for f in flags]
    truth = sum(f for _, f in pool) / len(pool)
    sizes = {name: size for name, size, _ in strata}
    n, reps = 40, 2000
    srs_err, strat_err, few_safety = [], [], 0
    for i in range(reps):
        s = random.Random(i).sample(pool, n)
        srs_err.append(abs(sum(f for _, f in s) / n - truth))
        few_safety += sum(1 for c, _ in s if c == "安全") < 2
        st = evalstats.stratified_sample(pool, key=lambda x: x[0], n=n, seed=i)
        by: dict[str, list[int]] = {}
        for c, f in st:
            by.setdefault(c, []).append(f)
        est = sum(sizes[c] / len(pool) * sum(v) / len(v) for c, v in by.items())  # 按各层在总体中的占比加权
        strat_err.append(abs(est - truth))

    def p95(xs):
        return sorted(xs)[int(0.95 * len(xs)) - 1]

    print(
        "假想一个 1000 题的大 benchmark：常规 600 题（通过率 95%）、边界 250（70%）、对抗 100（40%）、安全 50（20%）。\n"
        f"真实总分 {pct(truth)}。每次只抽 40 题来估计，重复 {reps} 次：\n"
        f"  简单随机抽样：平均误差 {sum(srs_err) / reps * 100:.1f} 个点，95% 的情况下误差 ≤ {p95(srs_err) * 100:.1f} 个点；"
        f"{few_safety / reps:.0%} 的样本里'安全'类不到 2 题\n"
        f"  分层抽样（按类别比例 24/10/4/2）：平均误差 {sum(strat_err) / reps * 100:.1f} 个点，95% 的情况下误差 ≤ {p95(strat_err) * 100:.1f} 个点；"
        "每次都恰好有 2 题安全类\n"
        "→ 分层去掉了'这次各类各抽到几题'的随机性：误差更小，而且每一类都有代表。\n"
        "  tinyBenchmarks 更进一步：利用大量模型在每道题上的历史对错挑出最有代表性的题并加权，\n"
        "  在 MMLU 上用 100 道题估计 14K 题的分数，平均误差约 2%（前提是有大量历史评估数据）。"
    )


# =====================================================================
# 第 7 节：成对 LLM 评委
# =====================================================================


class PairVerdict(BaseModel):
    winner: Literal["A", "B"] = Field(description="更好的那条回复：A 或 B")
    reason: str = Field(description="一句话理由")


JUDGE_PROMPT = """你是售后客服质检员。下面是同一个退款工单的两条客服回复，请判断哪一条更好。
评判标准（按重要性排序）：
1. 处理结论和金额是否与"正确处理"一致；
2. 是否向客户说清楚原因和下一步；
3. 礼貌、简洁。篇幅长短本身不加分。

【工单与正确处理】{context}

【回复 A】{first}

【回复 B】{second}

只能在 A 和 B 中选一个，不允许平局。"""


def load_pairs() -> list[dict]:
    rows = []
    for line in (HERE / "judge_pairs.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("//"):
            rows.append(json.loads(line))
    return rows


async def collect_verdicts(args, pairs: list[dict], judge_llm) -> tuple[dict, str]:
    """返回 {(pair_id, 'xy' | 'yx'): 'A' | 'B'}：xy 表示 x 在前，yx 表示 y 在前。"""
    if args.offline:
        data = json.loads(OFFLINE.read_text(encoding="utf-8"))["judge"]
        print(f"[离线] {data['note']}")
        return {(r["pair"], r["order"]): r["verdict"] for r in data["verdicts"]}, data["model"]
    from agentkit.workflows import complete_json

    cache_path = RUNS / "judge.jsonl"
    cache = {} if args.fresh else load_jsonl(cache_path)
    todo, out = [], {}
    for p in pairs:
        for order, (first, second) in (("xy", (p["x"], p["y"])), ("yx", (p["y"], p["x"]))):
            key = f"{judge_llm.model}|{fingerprint(JUDGE_PROMPT)}|{p['id']}|{order}"
            if key in cache:
                out[(p["id"], order)] = cache[key]["verdict"]
            else:
                todo.append((key, p, order, first, second))

    from agentkit.workflows import parallel

    async def work(job):
        key, p, order, first, second = job
        v = await complete_json(judge_llm, JUDGE_PROMPT.format(context=p["context"], first=first, second=second), PairVerdict)
        return {"key": key, "pair": p["id"], "order": order, "verdict": v.winner, "reason": v.reason}

    print(f"评委模型 {judge_llm.model}：缓存命中 {len(out)} 次，新调用 {len(todo)} 次（并发 {MAX_CONCURRENCY}）……")
    # 两种顺序的判决都先并发收集齐，再交给纯函数 debiased_pairwise 回放（练习 c 的 judge 是普通函数）
    new_rows = await parallel([lambda j=j: work(j) for j in todo], max_concurrency=MAX_CONCURRENCY)
    append_jsonl(cache_path, new_rows)
    for r in new_rows:
        out[(r["pair"], r["order"])] = r["verdict"]
    return out, judge_llm.model


async def section_judge(args, judge_llm) -> None:
    section("第 7 节：成对 LLM 评委 —— 不交换顺序 vs 交换顺序，再和人工标注对一对")
    pairs = load_pairs()
    verdicts, model = await collect_verdicts(args, pairs, judge_llm)
    print(
        f"评委 {model}。8 组'同一工单的两条客服回复'，人工标注：4 组有明确优劣（金额错 / 违规 / 冗长 / 生硬），\n"
        "4 组两条都合格（tie）。评委被要求必须二选一 —— 两条差不多时，它靠什么选，就暴露出来了。\n"
    )

    by_text = {}
    for p in pairs:
        by_text[(p["x"], p["y"])] = verdicts[(p["id"], "xy")]
        by_text[(p["y"], p["x"])] = verdicts[(p["id"], "yx")]

    def replay_judge(first: str, second: str) -> str:  # 把（真实或录制的）判决包装成 judge(first, second) 函数
        return by_text[(first, second)]

    single, debiased, human = [], [], []
    first_wins = consistent = 0
    print(f"{pad('组', 5)}{pad('人工', 6)}{pad('只问一次(x 在前)', 18)}{pad('交换后(y 在前)', 16)}{pad('去偏结果', 10)}说明")
    for p in pairs:
        w1, w2 = evalstats.judge_both_orders(replay_judge, p["x"], p["y"])
        final = IMPL.debiased_pairwise(replay_judge, p["x"], p["y"])
        raw = verdicts[(p["id"], "xy")]
        first_wins += (raw == "A") + (verdicts[(p["id"], "yx")] == "A")
        consistent += w1 == w2
        single.append(w1)
        debiased.append(final)
        human.append(p["human"])
        flag = "两次一致" if w1 == w2 else f"⚠ 换了位置就改判：两次都选了第 {1 if raw == 'A' else 2} 个位置"
        print(f"{pad(p['id'], 5)}{pad(p['human'], 6)}{pad(w1, 18)}{pad(w2, 16)}{pad(final, 10)}{flag}；{p['note']}")

    clear = [i for i, h in enumerate(human) if h != "tie"]
    tie_pairs = [i for i, h in enumerate(human) if h == "tie"]
    print(
        f"\n位置一致率：{consistent}/{len(pairs)} 组换了顺序结论不变；16 次判决里'第 1 个位置'赢了 {first_wins} 次（没有位置偏差时应在 8 次左右）\n"
        f"人工有明确偏好的 {len(clear)} 组：只问一次判对 {sum(single[i] == human[i] for i in clear)} 组，"
        f"去偏后判对 {sum(debiased[i] == human[i] for i in clear)} 组\n"
        f"人工认为'差不多'的 {len(tie_pairs)} 组：去偏后判成平局的有 {sum(debiased[i] == 'tie' for i in tie_pairs)} 组，"
        "其余是评委换了位置也坚持的偏好（比如偏爱解释更多的那条）—— 这不是位置偏差，交换顺序去不掉"
    )
    print("\n与人工标签（x / y / tie）的一致性，以及它们自己的误差棒：")
    for name, labels in (("只问一次", single), ("交换去偏", debiased)):
        ag = evalstats.judge_agreement(labels, human)
        agree = round(ag.observed * ag.n)
        lo, hi = IMPL.wilson_interval(agree, ag.n)
        kci = evalstats.kappa_bootstrap_ci(labels, human, n_boot=2000, seed=0)
        print(
            f"  {name}：一致率 {agree}/{ag.n} = {pct(ag.observed)}（Wilson [{pct(lo)}, {pct(hi)}]）   "
            f"Cohen's kappa {ag.kappa:+.2f}（bootstrap [{kci.low:+.2f}, {kci.high:+.2f}]）"
        )
    print(
        f"→ 只有 {len(pairs)} 组，区间宽到几乎什么都说明不了。想把一致率估到 ±10 个点以内，大约要 "
        f"{evalstats.n_for_margin(0.8, 0.10)} 条校准样本；±5 个点要 {evalstats.n_for_margin(0.8, 0.05)} 条（按一致率 80% 估算）。\n"
        "  校准集怎么采、标注指南怎么写，见第 21 课。\n"
        "  交换顺序让评委调用次数翻倍，换来的是：'总选某个位置'的偏差再也造不出假赢家。"
    )


# =====================================================================


async def main() -> None:
    global IMPL
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用预置数据，无需 API key")
    parser.add_argument("--model", default=None, help="被测 Agent 用的模型（默认读 .env 的 LLM_MODEL）")
    parser.add_argument("--judge-model", default=None, help="评委模型（默认与被测模型相同 —— 生产中应换成不同家族的模型）")
    parser.add_argument("--runs", type=int, default=3, help="每个任务、每个版本跑几次（默认 3）")
    parser.add_argument("--fresh", action="store_true", help="忽略 runs/ 里的缓存，全部重新调用模型")
    args = parser.parse_args()

    IMPL, source = load_impl()
    print(f"练习函数（wilson_interval / paired_bootstrap_diff / debiased_pairwise）的实现来自：{source}")
    tasks = rb.load_tasks()

    llm = judge_llm = None
    clients = []
    if not args.offline:
        from agentkit import ResilientLLM, default_llm

        clients = [default_llm(args.model), default_llm(args.judge_model or args.model)]
        llm = ResilientLLM(clients[0], max_attempts=4, base_delay=1.0)
        judge_llm = ResilientLLM(clients[1], max_attempts=4, base_delay=1.0)

    try:
        section_audit(tasks)  # 探针 Agent 是纯规则，不调用模型：普通函数
        outcomes, decisions = await section_run(args, tasks, llm)
        k = len(next(iter(outcomes["A"].values())))
        section_point(outcomes)  # 第 3–6 节是纯统计
        section_ci(outcomes, tasks, k)
        section_paired(outcomes, tasks, k, decisions)
        section_small()
        await section_judge(args, judge_llm)
        if not args.offline and judge_llm.model == llm.model:
            print(f"\n⚠️ 评委和被测 Agent 用的是同一个模型（{llm.model}），有自我偏好的风险；生产中请用 --judge-model 换一个不同家族的模型。")
    finally:
        for c in clients:
            await c.aclose()  # 关掉 HTTP 连接池（sys.exit 中止评估时也会执行）


if __name__ == "__main__":
    asyncio.run(main())
