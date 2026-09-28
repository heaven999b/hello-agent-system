"""第 23 课 Demo：优化 —— 用数据和预算，系统地让 LM 程序变好。

    python lessons/23_optimization/demo.py             # 真实模型（读取 .env；约 540 次调用、并发 2，实测 14～16 分钟）
    python lessons/23_optimization/demo.py --offline   # 离线确定性模拟（SimulatedLLM），无需 API key，几秒钟

任务：IT 工单分类（7 个类别，8 条"公司特有"的易错规则），train / dev / test 各 20 条。
六个场景，每一步都打印 dev 分数、test 分数、模型调用次数和成本：
  1. 基线：只列了类别名的"偷懒版"指令
  2. BootstrapFewShot：从成功轨迹里收集示例，按 dev 挑组合
  3. OPRO：优化器只看"指令 → 总分"的历史，提出新指令
  4. GEPA 式反思：优化器读失败轨迹 + 文字反馈，针对性改写；帕累托前沿选父代
  5. 汇总 + 显著性：配对 bootstrap 判断 test 上的提升是不是噪声；标出"dev 涨、test 不涨"的过拟合
  6. 测试时计算：同一个基线，采样 5 次做自一致性投票 / best-of-N，和"改提示词"比一比；
     再用固定延迟的剧本模型实测"5 次采样同时发出"和"一个接一个"的延迟差别

整个 Demo 是 async 的（入口 asyncio.run(main())）：每次评估里的多条样本、每条工单的 N 次采样，
都在一个事件循环里并发发出，同时在途的调用数有上限（--workers，默认 2；离线和真实模式走同一条代码路径）。

价格用 agentkit/pricing.py 里的**示例单价**（不是任何厂商的报价），只为了让成本对比有数可算。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from optkit import (  # noqa: E402
    MeteredLLM,
    Program,
    ProgramTask,
    best_of_n,
    bootstrap_fewshot,
    gepa_optimize,
    mean,
    opro_optimize,
    paired_bootstrap,
    per_tag_accuracy,
    sample_n,
    self_consistency,
    verbatim_overlap,
)
from ticket_task import (  # noqa: E402
    BASELINE_INSTRUCTION,
    DEV,
    INPUT_PREFIX,
    OUTPUT_FORMAT,
    TASK_DESCRIPTION,
    TEST,
    TRAIN,
    SimulatedLLM,
    exemplar_line,
    feedback,
    format_verifier,
    metric,
    parse_label,
)

from agentkit import ResilientLLM, ScriptedLLM, default_llm, reply  # noqa: E402
from agentkit.config import env  # noqa: E402
from agentkit.context import estimate_tokens  # noqa: E402

# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 76 + f"\n  {title}\n" + "═" * 76, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, w: int) -> str:
    """按"显示宽度"左对齐：中文字符在终端里占两个英文字符宽。"""
    return text + " " * max(0, w - width(text))


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def show_instruction(text: str, limit: int = 900) -> None:
    body = text if len(text) <= limit else text[:limit] + f"……（共 {len(text)} 字，已截断）"
    for line in body.splitlines():
        info(f"│ {line}")


# ---------------------------------------------------------------- 记账


class Ledger:
    """按"步骤"记录两个模型（任务模型、优化器）各花了多少调用和钱。"""

    def __init__(self, task_llm: MeteredLLM, opt_llm: MeteredLLM):
        self.task_llm, self.opt_llm = task_llm, opt_llm
        self._mark = self._now()

    def _now(self):
        return self.task_llm.snapshot(), self.opt_llm.snapshot()

    def take(self) -> dict:
        (tc0, tcost0), (oc0, ocost0) = self._mark
        (tc1, tcost1), (oc1, ocost1) = self._now()
        self._mark = self._now()
        return {"task_calls": tc1 - tc0, "opt_calls": oc1 - oc0, "cost": (tcost1 - tcost0) + (ocost1 - ocost0)}


def prompt_tokens(prog: Program) -> int:
    """上线后每处理一张工单，发给模型的输入 token（估算）：优化出来的长提示词每次调用都要付钱。"""
    return estimate_tokens(prog.messages(TEST[0].input))


async def ttc_latency_probe(base: Program, n: int, tickets, latency: float = 0.05) -> None:
    """测试时计算的延迟账：同一个程序换成"每次调用固定 latency 秒"的剧本模型，
    每条工单采样 n 次，比较"一个接一个"和"n 次同时发出"用户要等多久。剧本延迟是确定的，数字可以复现。"""
    for label, limit in (("一个接一个（并发 1）", 1), (f"{n} 次同时发出（并发 {n}）", n)):
        llm = ScriptedLLM(responder=lambda m: reply("看起来是设备问题。\n类别：hardware"), latency=latency)
        prog = base.with_(llm=llm)
        t0 = time.perf_counter()
        for ex in tickets:  # 工单一条接一条地来（像线上的请求一样）；每条内部的 n 次采样按 limit 并发
            await sample_n(lambda ex=ex: prog(ex.input), n, max_concurrency=limit)
        per_ticket = (time.perf_counter() - t0) / len(tickets)
        info(f"{pad(label, 26)}每条工单等 {per_ticket * 1000:4.0f}ms   模型调用 {llm.call_count} 次   同时在途峰值 {llm.max_in_flight}")


# ---------------------------------------------------------------- 主流程


async def main() -> None:
    parser = argparse.ArgumentParser(description="第 23 课 Demo：提示词优化、测试时计算与显著性")
    parser.add_argument("--offline", action="store_true", help="使用确定性模拟模型，不调用真实模型")
    parser.add_argument("--task-model", default=None, help="被优化的任务模型（默认 .env 的 LLM_MODEL）")
    parser.add_argument("--optimizer-model", default=None, help="OPRO / GEPA 用的优化器模型（默认同上）")
    parser.add_argument("--workers", type=int, default=2, help="同时在途的模型调用上限（离线和真实模式都用；共享网关时请 ≤ 2）")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    t_start = time.time()
    workers = max(1, args.workers)
    clients = []
    if args.offline:
        task_raw, opt_raw = SimulatedLLM("sim-task"), SimulatedLLM("sim-optimizer")
        print("模式：离线确定性模拟（SimulatedLLM）—— 结果固定、零成本。模拟模型把本课要讲的现象显式写了出来，")
        print(f"      数字只用来演示流程；真实模型的结果见讲义第 3 节。并发 {workers}（和真实模式同一条代码路径）。")
    else:
        try:
            clients = [default_llm(args.task_model), default_llm(args.optimizer_model)]
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        # 熔断阈值调高：几百次调用里偶尔连续失败几次很正常，不能因此让后面的评估全部"快速失败"记 0 分。
        # max_concurrency 是对网关的总闸（舱壁）：不管上面怎么并发，同一时刻最多 workers 个请求在路上
        task_raw = ResilientLLM(clients[0], max_attempts=4, base_delay=1.0, failure_threshold=20, max_concurrency=workers)
        opt_raw = ResilientLLM(clients[1], max_attempts=4, base_delay=1.0, failure_threshold=20, max_concurrency=workers)
        print(f"模式：真实模型（任务模型 {task_raw.model}，优化器 {opt_raw.model}，并发 {workers}）—— 每次运行结果会不同")
        print("      预计约 540 次模型调用；在共享网关上实测 14～16 分钟。")

    task_llm, opt_llm = MeteredLLM(task_raw, "任务模型"), MeteredLLM(opt_raw, "优化器")
    ledger = Ledger(task_llm, opt_llm)
    base = Program(task_llm, BASELINE_INSTRUCTION, output_format=OUTPUT_FORMAT, input_prefix=INPUT_PREFIX)
    task = ProgramTask(base, metric, feedback, max_concurrency=workers)

    async def run_scores(instruction: str, dataset, demos=None) -> list[float]:
        return [r.score for r in await task.run(instruction, dataset, demos=demos)]

    try:
        await run_all_scenarios(args, workers, t_start, task_raw, task_llm, opt_llm, ledger, base, task, run_scores)
    finally:
        for c in clients:
            await c.aclose()  # 关掉 HTTP 连接池


async def run_all_scenarios(args, workers, t_start, task_raw, task_llm, opt_llm, ledger, base, task, run_scores) -> None:
    """六个场景依次跑完（每个场景内部的模型调用是并发的）。"""
    rows: list[dict] = []  # 汇总表

    # ================================================================ 场景 1
    banner("场景 1：基线 —— 只列了类别名的指令")
    info(f"数据：train {len(TRAIN)} / dev {len(DEV)} / test {len(TEST)} 条；每份 8 条易错工单（每条公司规则 1 条）+ 12 条常规工单")
    info("规则：在 train 上找示例/反馈，在 dev 上挑候选，test 只在最后报告 —— 优化过程中一眼都不看")
    step("基线指令")
    show_instruction(BASELINE_INSTRUCTION)
    base_dev_recs = await task.run(BASELINE_INSTRUCTION, DEV)
    base_dev = [r.score for r in base_dev_recs]
    opt_cost = ledger.take()
    base_test = await run_scores(BASELINE_INSTRUCTION, TEST)
    ledger.take()
    info(f"dev {pct(mean(base_dev))}   test {pct(mean(base_test))}   （评估 dev + test 共 {len(DEV) + len(TEST)} 次调用）")
    tags = per_tag_accuracy(DEV, base_dev)
    traps = [(k, v) for k, v in tags.items() if k != "常规"]
    info(f"dev 按类型：常规工单 {tags['常规'][0]}/{tags['常规'][1]}，易错工单 {sum(v[0] for _, v in traps)}/{sum(v[1] for _, v in traps)}")
    step("dev 上答错的工单（优化器可以看 dev 的分数；test 的错题我们不看）")
    for ex, r in zip(DEV, base_dev_recs):
        if r.score < 1.0:
            info(f"✗ {ex.id} {pad(ex.tag, 14)} 标准 {pad(ex.label, 15)} 模型 {pad(parse_label(r.output), 15)} {ex.input[:28]}…")
    rows.append({"name": "基线", "dev": base_dev, "test": base_test, **opt_cost, "prompt_tokens": prompt_tokens(base),
                 "instruction": BASELINE_INSTRUCTION, "demos": []})
    takeaway("常规工单基本都对，错误集中在'公司特有'的边界上 —— 模型再强也不可能天生知道'U 盘用不了要找安全组'。")

    # ================================================================ 场景 2
    banner("场景 2：BootstrapFewShot —— 从成功轨迹里收集示例")
    step("用基线程序跑训练集，把评分通过的 (工单, 模型输出) 收进示例池；再在 dev 上比较 3 组示例组合")
    boot = await bootstrap_fewshot(task, TRAIN, DEV, max_demos=4, num_candidates=3, seed=args.seed, log=info)
    boot_cost = ledger.take()
    trap_inputs = {ex.input for ex in TRAIN if ex.tag != "常规"}
    n_trap_demo = sum(1 for d in boot.demos if d.input in trap_inputs)
    info(f"选中的 {len(boot.demos)} 条示例里，易错工单占 {n_trap_demo} 条（基线本来就会做的题，才会被收进示例池）")
    boot_prog = base.with_(demos=boot.demos)
    boot_test = await run_scores(BASELINE_INSTRUCTION, TEST, demos=boot.demos)
    ledger.take()
    info(f"最佳组合：dev {pct(boot.dev)}   test {pct(mean(boot_test))}   优化花了 {boot_cost['task_calls']} 次调用，${boot_cost['cost']:.4f}")
    rows.append({"name": "BootstrapFewShot", "dev": boot.dev_scores, "test": boot_test, **boot_cost, "prompt_tokens": prompt_tokens(boot_prog),
                 "instruction": BASELINE_INSTRUCTION, "demos": boot.demos})
    takeaway("bootstrap 收集的是'程序已经做对的题'：它擅长教格式和推理风格，却很难教会程序本来不会的规则。")

    # ================================================================ 场景 3
    banner("场景 3：OPRO —— LLM 当优化器，只看'指令 → 总分'的历史")
    step("每轮：把历史前 5 名（按分数升序）+ 3 条训练样例写进元提示词 → 优化器一次写 3 条新指令 → 各在 dev 上评估")
    opro = await opro_optimize(
        task,
        opt_llm,
        DEV,
        [BASELINE_INSTRUCTION],
        task_description=TASK_DESCRIPTION,
        exemplars=[exemplar_line(e) for e in TRAIN],
        rounds=2,
        per_round=3,
        seed=args.seed,
        seed_scores={BASELINE_INSTRUCTION.strip(): base_dev},
        log=info,
    )
    opro_cost = ledger.take()
    step("OPRO 选出的指令")
    show_instruction(opro.instruction)
    opro_test = await run_scores(opro.instruction, TEST)
    ledger.take()
    info(f"dev {pct(opro.dev)}   test {pct(mean(opro_test))}   "
         f"优化花了 任务模型 {opro_cost['task_calls']} 次 + 优化器 {opro_cost['opt_calls']} 次，${opro_cost['cost']:.4f}")
    rows.append({"name": "OPRO", "dev": opro.dev_scores, "test": opro_test, **opro_cost,
                 "prompt_tokens": prompt_tokens(base.with_(instruction=opro.instruction)), "instruction": opro.instruction, "demos": []})
    takeaway("OPRO 只知道'这条指令 75 分'，不知道错在哪 —— 改进靠盲试，每一个候选都要在 dev 上完整评估一遍。")

    # ================================================================ 场景 4
    banner("场景 4：GEPA 式反思 —— 读失败轨迹 + 文字反馈，针对性地改写")
    step("每次迭代：帕累托前沿里抽父代 → 训练集取 5 条跑一遍 → 反思模型读轨迹和标注备注 → 新指令在小批上严格变好才上 dev")
    gepa, pool = await gepa_optimize(
        task,
        opt_llm,
        TRAIN,
        DEV,
        BASELINE_INSTRUCTION,
        iterations=4,
        minibatch_size=5,
        task_description=TASK_DESCRIPTION,
        seed=args.seed,
        seed_dev_scores=base_dev,
        log=info,
    )
    gepa_cost = ledger.take()
    step("候选池（dev 分数）")
    for i, c in enumerate(pool):
        parent = "种子" if c.parent is None else f"父代 #{c.parent}"
        info(f"#{i}  dev {pct(c.dev):>4}  （{parent}，第 {c.iteration} 次迭代）")
    step("GEPA 选出的指令")
    show_instruction(gepa.instruction)
    gepa_test = await run_scores(gepa.instruction, TEST)
    ledger.take()
    info(f"dev {pct(gepa.dev)}   test {pct(mean(gepa_test))}   "
         f"优化花了 任务模型 {gepa_cost['task_calls']} 次 + 优化器 {gepa_cost['opt_calls']} 次，${gepa_cost['cost']:.4f}")
    rows.append({"name": "GEPA 式反思", "dev": gepa.dev_scores, "test": gepa_test, **gepa_cost,
                 "prompt_tokens": prompt_tokens(base.with_(instruction=gepa.instruction)), "instruction": gepa.instruction, "demos": []})

    step("加一步：GEPA 的指令 + Bootstrap 的示例（指令和示例一起用，MIPROv2 联合搜索的思路）")
    combo_dev = await run_scores(gepa.instruction, DEV, demos=boot.demos)
    combo_cost = ledger.take()
    combo_test = await run_scores(gepa.instruction, TEST, demos=boot.demos)
    ledger.take()
    info(f"dev {pct(mean(combo_dev))}   test {pct(mean(combo_test))}")
    combo_cost = {k: combo_cost[k] + boot_cost[k] + gepa_cost[k] for k in combo_cost}
    rows.append({"name": "GEPA + 示例", "dev": combo_dev, "test": combo_test, **combo_cost,
                 "prompt_tokens": prompt_tokens(base.with_(instruction=gepa.instruction, demos=boot.demos)),
                 "instruction": gepa.instruction, "demos": boot.demos})

    # ================================================================ 场景 5
    banner("场景 5：汇总 —— dev 上的提升，test 上还剩多少？是不是噪声？")
    header = f"{pad('方法', 18)}{pad('dev', 7)}{pad('test', 7)}{pad('Δdev', 7)}{pad('Δtest', 7)}{pad('优化调用(任务/优化器)', 24)}{pad('优化成本', 10)}{'每单输入token'}"
    info(header)
    info("─" * (width(header) + 2))
    b_dev, b_test = mean(base_dev), mean(base_test)
    for r in rows:
        dd, dt = mean(r["dev"]) - b_dev, mean(r["test"]) - b_test
        calls = "-" if r["name"] == "基线" else f"{r['task_calls']}/{r['opt_calls']}"
        cost = "-" if r["name"] == "基线" else f"${r['cost']:.4f}"
        info(
            f"{pad(r['name'], 18)}{pad(pct(mean(r['dev'])), 7)}{pad(pct(mean(r['test'])), 7)}"
            f"{pad(f'{dd * 100:+.0f}', 7)}{pad(f'{dt * 100:+.0f}', 7)}{pad(calls, 24)}{pad(cost, 10)}{r['prompt_tokens']}"
        )
    step("显著性：test 上逐条配对，bootstrap 10000 次（和基线比）")
    info(f"{pad('方法', 18)}{pad('Δtest', 8)}{pad('95% 置信区间', 18)}{pad('赢/输(条)', 12)}{'P(提升≤0)'}")
    for r in rows[1:]:
        s = paired_bootstrap(base_test, r["test"], seed=args.seed)
        ci = f"[{s['lo'] * 100:+.0f}, {s['hi'] * 100:+.0f}]"
        verdict = "显著" if s["lo"] > 0 else "不显著（区间含 0）"
        diff = f"{s['diff'] * 100:+.0f}"
        wl = f"{s['wins']}/{s['losses']}"
        info(f"{pad(r['name'], 18)}{pad(diff, 8)}{pad(ci, 18)}{pad(wl, 12)}{s['p_le_0']:.3f}  {verdict}")
    step("过拟合检查：dev 提升明显大于 test 提升的方法")
    flagged = False
    for r in rows[1:]:
        dd, dt = round((mean(r["dev"]) - b_dev) * 100), round((mean(r["test"]) - b_test) * 100)
        if dd - dt >= 10:
            flagged = True
            info(f"⚠️  {r['name']}：dev {dd:+d} 个点，test 只有 {dt:+d} 个点 —— 在 20 条 dev 上挑了最高分，挑到的一部分是运气")
    if not flagged:
        info("这次运行没有方法出现 ≥10 个点的 dev/test 落差。")
    step("背题 / 泄漏检查：优化出来的指令里，有没有逐字抄进工单原文（连续 10 个字）？")
    for r in rows[2:4]:
        hits = {name: verbatim_overlap(r["instruction"], data) for name, data in (("train", TRAIN), ("dev", DEV), ("test", TEST))}
        detail = "  ".join(f"{k} {len(v)} 条" + (f"（{'、'.join(e.id for e in v)}）" if v else "") for k, v in hits.items())
        info(f"{pad(r['name'], 18)}{detail}")
    info("OPRO 只见过 3 条训练样例，GEPA 的反思只见过训练集的小批：dev / test 的原文按设计不可能进入指令。")
    info("抄了 train 原文不算泄漏，但说明优化器在'背具体工单'而不是总结规则 —— 这类指令换一批数据往往就不灵了。")
    takeaway("只有 20 条 test 时，±10 个点的差异常常落在置信区间里：想证明'显著变好'，要么效果大，要么样本多（第 22 课）。")

    # ================================================================ 场景 6
    banner("场景 6：测试时计算 —— 不改提示词，多采样几次行不行？")
    n = 5
    step(f"基线程序在 test 上每条采样 {n} 次（共 {n * len(TEST)} 次调用），比较四种用法")
    samples, waits = [], []
    lat0, calls0 = task_llm.latency_s, task_llm.calls
    for ex in TEST:  # 工单一条接一条；每条的 n 次采样同时发出（受并发上限 workers 约束）
        t0 = time.perf_counter()
        samples.append(await sample_n(lambda ex=ex: base(ex.input), n, max_concurrency=workers))
        waits.append(time.perf_counter() - t0)
    per_call = (task_llm.latency_s - lat0) / max(1, task_llm.calls - calls0)
    ttc_cost = ledger.take()
    single = [metric(ex, s[0]) for ex, s in zip(TEST, samples)]
    votes = [self_consistency([parse_label(o) for o in s]) for s in samples]
    sc = [1.0 if v == ex.label else 0.0 for ex, (v, _) in zip(TEST, votes)]
    bon = [metric(ex, best_of_n(s, format_verifier)[0]) for ex, s in zip(TEST, samples)]
    oracle = [1.0 if any(metric(ex, o) for o in s) else 0.0 for ex, s in zip(TEST, samples)]
    unanimous_wrong = sum(1 for ex, (v, agree) in zip(TEST, votes) if agree == 1.0 and v != ex.label)
    info(f"{pad('用法', 40)}{pad('test', 8)}{'每条工单调用数'}")
    info(f"{pad('单次采样', 40)}{pad(pct(mean(single)), 8)}1")
    info(f"{pad(f'自一致性：{n} 次多数投票', 40)}{pad(pct(mean(sc)), 8)}{n}")
    info(f"{pad(f'best-of-{n} + 格式验证器', 40)}{pad(pct(mean(bon)), 8)}{n}")
    info(f"{pad(f'best-of-{n} + 完美验证器（作弊上限 pass@{n}）', 40)}{pad(pct(mean(oracle)), 8)}{n}")
    info(f"{pad('对照：GEPA 优化后的指令，单次采样', 40)}{pad(pct(mean(gepa_test)), 8)}1")
    info(f"采样花了 {ttc_cost['task_calls']} 次调用，${ttc_cost['cost']:.4f}；有 {unanimous_wrong} 条工单 {n} 次全票一致地答错")
    if not args.offline:  # 离线模拟模型没有延迟，这两个数没有意义
        info(f"延迟（真实模型）：单次调用平均 {per_call:.1f}s；每条工单 {n} 次采样（并发 ≤ {workers}）平均等 {mean(waits):.1f}s，"
             f"约为单次的 {mean(waits) / per_call:.1f} 倍（一个接一个就是 {n} 倍）")
    step(f"延迟实测：测试时计算让每个请求的调用次数变成 {n} 倍，等待时间呢？（剧本模型，每次调用固定 50ms，取 test 前 4 条）")
    await ttc_latency_probe(base, n, TEST[:4])
    info(f"{n} 次采样同时发出，用户只等最慢的那一次；调用次数（成本）一点没少。并发受网关配额限制时（比如上限 2），")
    info(f"延迟约为 ceil({n}/2)=3 次调用 —— 这也是为什么测试时计算和并发配额要一起规划（第 12、14 课）。")
    takeaway("投票只能消除'时对时错'的随机错误；模型'稳定地不知道'的公司规则，采样再多次也是全票答错。"
             "best-of-N 的上限由验证器决定：只会查格式的验证器几乎没用，'完美验证器'那一行是你永远拿不到的天花板。")

    out_dir = HERE / "runs"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / ("last_run_offline.json" if args.offline else "last_run.json")
    payload = {
        "mode": "offline" if args.offline else f"real:{task_raw.model}",
        "methods": [
            {"name": r["name"], "instruction": r["instruction"], "demos": [d.__dict__ for d in r["demos"]],
             "dev_scores": r["dev"], "test_scores": r["test"], "task_calls": r["task_calls"], "opt_calls": r["opt_calls"], "cost_usd": r["cost"]}
            for r in rows
        ],
        "gepa_pool": [{"instruction": c.instruction, "dev": c.dev, "parent": c.parent, "iteration": c.iteration} for c in pool],
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    info(f"每个方法选出的指令、示例和逐条分数已保存到 {out_file.relative_to(HERE.parents[1])}")
    if task.errors:
        info(f"⚠️  有 {task.errors} 次任务模型调用在重试后仍然失败，按 0 分计入 —— 解读上面的分数时要把它考虑进去")
    total_calls = task_llm.calls + opt_llm.calls
    total_cost = task_llm.cost_usd + opt_llm.cost_usd
    banner(f"完成 🎉  共 {total_calls} 次模型调用（任务 {task_llm.calls} / 优化器 {opt_llm.calls}），"
           f"${total_cost:.4f}（示例单价），用时 {time.time() - t_start:.0f} 秒")
    print("  接下来：打开 exercise.py 完成三道练习，然后 make lesson N=23")


if __name__ == "__main__":
    asyncio.run(main())
