"""第 05 课 Demo：可靠性工程 —— 让 Agent 在失败中存活。

    python lessons/05_reliability/demo.py             # 真实模型（读取 .env）
    python lessons/05_reliability/demo.py --offline   # 离线剧本（ScriptedLLM），无需 API key

四个场景：
  1. 重试：模型连续两次 429，第三次成功；以及"为什么一定要加抖动"的 1000 客户端模拟
  2. 熔断 + 降级：主模型不可用 → 熔断器打开 → 走备用模型；主模型恢复 → 半开试探 → 关闭
  3. 崩溃恢复：真的杀掉一个进程，再从检查点恢复；对比"没有幂等 → 重复建工单"和"有幂等 → 不会"
  4. 预算：BudgetHook 截停一个停不下来的 Agent

运行产物（检查点、模拟工单库、幂等存储）写在 runs/05_reliability/ 下。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from agentkit import (
    Agent,
    BudgetHook,
    FileCheckpointer,
    Hook,
    IdempotencyStore,
    LLMError,
    ResilientLLM,
    ScriptedLLM,
    ToolResult,
    call_tool,
    default_llm,
    render_tree,
    reply,
    tool,
)
from agentkit.config import env

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs" / "05_reliability"
MAIN_MODEL = env("LLM_MODEL", "gpt-5.5")


# ---------------------------------------------------------------- 打印小工具


def banner(title: str) -> None:
    print("\n" + "═" * 72 + f"\n  {title}\n" + "═" * 72, flush=True)


def step(msg: str) -> None:
    print(f"\n▶ {msg}", flush=True)


def info(msg: str = "") -> None:
    print(f"   {msg}", flush=True)


def takeaway(msg: str) -> None:
    print(f"\n   💡 {msg}", flush=True)


def short(text: str | None, n: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def answered_by(result) -> str:
    """从 trace 里读出最后一次模型调用实际用的是哪个模型。"""
    llm_spans = [s for s in result.trace.walk() if s.name == "llm.chat"]
    return llm_spans[-1].attrs.get("gen_ai.response.model", "?") if llm_spans else "（没有调用模型）"


# ---------------------------------------------------------------- 故障注入


class FlakyLLM:
    """故障注入包装器：前几次调用按剧本抛错，之后透传给内层模型。

    这就是混沌工程（chaos engineering）的最小形态：不等故障自然发生，主动制造故障，
    验证你的重试 / 熔断 / 降级代码真的会按预期工作。
    """

    def __init__(self, inner, errors: list[Exception]):
        self.inner = inner
        self.errors = list(errors)
        self.model = inner.model
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.inner.chat(messages, tools, **kwargs)


def rate_limited() -> LLMError:
    return LLMError("Error code: 429 - Rate limit reached, please retry later", status_code=429, retryable=True)


def model_not_found(name: str) -> LLMError:
    # 与真实接口返回的错误同形：400 属于"重试也没用"的错误
    return LLMError(f"Error code: 400 - unknown provider for model {name}", status_code=400, retryable=False)


# =====================================================================
# 场景 1：重试
# =====================================================================


def scenario_retry(offline: bool) -> None:
    banner("场景 1：重试 —— 模型连续两次 429（限流），第三次成功")
    inner = (
        ScriptedLLM([reply("因为失败往往是暂时的：等待时间指数增长给服务端恢复的时间，随机抖动避免所有客户端同时重试。")])
        if offline
        else default_llm()
    )
    flaky = FlakyLLM(inner, [rate_limited(), rate_limited()])
    llm = ResilientLLM(flaky, max_attempts=4, base_delay=0.4)  # 用真实 sleep，让你感受到退避的等待

    step("Agent → ResilientLLM → FlakyLLM（前两次抛 429）→ 模型")
    t0 = time.time()
    res = Agent(llm, [], system_prompt="你是简洁的技术讲师，回答不超过 60 个字。").run(
        "用一句话解释：调用模型 API 时，为什么要用'指数退避 + 随机抖动'来重试？"
    )
    for e in llm.events:
        info(f"📝 {short(e)}")
    info(f"✅ 状态={res.status}，底层共调用 {flaky.calls} 次，总耗时 {time.time() - t0:.1f}s")
    info(f"🤖 回答：{short(res.output, 200)}")
    takeaway("Agent 完全感知不到这两次失败 —— 重试被封装在 LLM 装饰器里，业务代码零改动。")

    step("对照：如果错误是 400（参数错误），重试有用吗？")
    bad = FlakyLLM(ScriptedLLM([]), [LLMError("Error code: 400 - invalid 'messages' format", status_code=400, retryable=False)])
    llm2 = ResilientLLM(bad, max_attempts=4, base_delay=0.4)
    res2 = Agent(llm2, []).run("你好")
    info(f"底层调用次数：{bad.calls}（没有重试），状态={res2.status}，给用户的回复：{res2.output}")
    takeaway("只重试'可能成功'的错误：429/5xx/超时/断连。400/401/403 重试一万次也是同样的结果，只会浪费配额。")

    step("为什么一定要加抖动？模拟 1000 个客户端在 t=0 同时收到 429")
    jitter_simulation()


def jitter_simulation(n: int = 1000, base: float = 1.0, bins: int = 10) -> None:
    rng = random.Random(7)
    no_jitter = [base] * n  # 固定退避：大家都在 1.0s 整点重试
    full_jitter = [rng.uniform(0, base) for _ in range(n)]  # 全抖动：在 [0, 1.0s] 内均匀散开

    def histogram(times: list[float], title: str) -> None:
        counts = [0] * bins
        for t in times:
            counts[min(int(t / base * bins), bins - 1)] += 1
        info(title)
        for i, c in enumerate(counts):
            bar = "█" * max(1 if c else 0, round(c / n * 40))
            info(f"  {i * base / bins:.1f}-{(i + 1) * base / bins:.1f}s │{bar} {c}")
        info(f"  → 最拥挤的 100ms 里有 {max(counts)} 个重试请求同时到达\n")

    histogram(no_jitter, "无抖动（每个客户端都等 1.0s 后重试）：")
    histogram(full_jitter, "全抖动（每个客户端在 [0, 1.0s] 内随机等待）：")
    takeaway("没有抖动，1000 个重试会在同一瞬间砸向刚刚过载的服务，让它再次过载（惊群效应），下一轮又是同样的整齐冲锋。")


# =====================================================================
# 场景 2：熔断 + 降级
# =====================================================================


def scenario_fallback(offline: bool) -> None:
    banner("场景 2：熔断 + 降级 —— 主模型挂了，Agent 还能继续服务吗？")
    fallback_name = env("LLM_FALLBACK_MODEL") or MAIN_MODEL
    broken_name = f"{MAIN_MODEL}-does-not-exist"  # 故意写一个不存在的模型名，模拟"主模型不可用"
    if offline:
        primary = ScriptedLLM([model_not_found(broken_name), model_not_found(broken_name),
                               reply("幂等：同一个操作执行一次和执行多次，结果完全一样。")], model=broken_name)
        backup = ScriptedLLM([reply("熔断器：下游持续失败时直接快速失败，给它恢复的时间。"),
                              reply("降级：主方案不可用时，切换到能力稍弱但可用的备选方案。"),
                              reply("重试预算：限制重试占总请求的比例，防止重试风暴。")], model=fallback_name)
    else:
        primary = default_llm(broken_name)
        backup = default_llm(fallback_name)

    now = [0.0]  # 假时钟：不用真等 30 秒
    llm = ResilientLLM(primary, [backup], max_attempts=2, base_delay=0.2,
                       failure_threshold=2, reset_timeout=30, clock=lambda: now[0])
    breaker = llm.chain[0][1]
    agent = Agent(llm, [], system_prompt="你是分布式系统方向的讲师，从软件工程角度回答，不超过 30 个字。")
    info(f"主模型：{broken_name}（不存在）   备用模型：{fallback_name}")
    info("熔断器配置：连续失败 2 次打开，打开 30 秒后进入半开（half_open）")

    questions = ["用一句话解释：什么是熔断器？", "用一句话解释：什么是降级？",
                 "用一句话解释：什么是重试预算？", "用一句话解释：什么是幂等？"]
    for i, q in enumerate(questions, 1):
        if i == 4:
            now[0] += 31
            if not offline:
                primary.model = MAIN_MODEL  # 模拟"运维修好了主模型"
            step("⏩ 31 秒过去了，主模型已经修好。熔断器进入 half_open，放一个试探请求过去……")
        before = breaker.state
        n = len(llm.events)
        t0 = time.time()
        res = agent.run(q)
        step(f"请求 {i}：{q}")
        info(f"熔断器：{before} → {breaker.state}    实际回答的模型：{answered_by(res)}    耗时 {time.time() - t0:.1f}s")
        for e in llm.events[n:]:
            info(f"📝 {short(e)}")
        info(f"🤖 {short(res.output, 120)}")

    takeaway("请求 3 里主模型根本没有被调用（'快速失败'）：熔断器打开时，不再浪费时间和配额去撞一堵墙。")
    takeaway("请求 4 是半开状态下的试探：成功 → 熔断器关闭，流量回到主模型。用户全程没有看到一次报错。")


# =====================================================================
# 场景 3：崩溃恢复 + 幂等
# =====================================================================

RUN_ID = "ticket-demo"
TICKET_PROMPT = "请帮我提交一个 IT 工单：3 楼打印机卡纸了，优先级高。"
TICKET_SYSTEM = "你是公司 IT 服务台助手。用户报告故障时，直接调用 create_ticket 建工单（不要追问），然后把工单号告诉用户。"


class TicketSystem:
    """模拟一个外部工单系统。数据存在 JSON 文件里：它在 Agent 进程之外，Agent 崩溃不会让它"回滚"。"""

    def __init__(self, path: Path):
        self.path = path

    def all(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def create(self, title: str, priority: str) -> str:
        tickets = self.all()
        tid = f"T-{1001 + len(tickets)}"
        tickets.append({"id": tid, "title": title, "priority": priority})
        self.path.write_text(json.dumps(tickets, ensure_ascii=False, indent=2), encoding="utf-8")
        return tid


class FileIdempotencyStore(IdempotencyStore):
    """持久化的幂等存储。

    agentkit 自带的 IdempotencyStore 存在内存里 —— 进程一崩就没了，恰恰在最需要它的时候失效。
    这里用 JSON 文件演示原理；生产中用 Redis（SET NX + 过期时间）或数据库唯一索引。
    """

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, key: str) -> ToolResult | None:
        record = self._load().get(key)
        return ToolResult(**record) if record else None

    def put(self, key: str, result: ToolResult) -> None:
        data = self._load()
        data[key] = asdict(result)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换，和 FileCheckpointer 同样的手法


class CrashAfterTool(Hook):
    """故障注入：工具执行完毕、结果还没写进检查点的那一刻，进程被杀死。"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def after_tool(self, state, call, result):
        if call.name == self.tool_name:
            print(f"      [子进程] 工具已执行：{result.content}", flush=True)
            print("      [子进程] 💥 就在此刻进程被 kill -9 —— 工具结果还没来得及写进检查点", flush=True)
            os._exit(137)  # 真正的进程死亡：不执行 finally、不保存状态、不做任何清理


def build_ticket_agent(workdir: Path, offline: bool, idempotent: bool, script: list, crash: bool = False) -> Agent:
    tickets = TicketSystem(workdir / "tickets.json")

    @tool(risk="write")  # 写操作：IdempotencyStore 只对 write / dangerous 工具生效
    def create_ticket(
        title: Annotated[str, Field(description="工单标题，简要描述故障")],
        priority: Annotated[Literal["low", "medium", "high"], Field(description="优先级")] = "medium",
    ) -> str:
        """在 IT 工单系统中创建一张工单，返回工单号。"""
        tid = tickets.create(title, priority)
        return f"工单已创建：{tid}（{title}，优先级 {priority}）"

    return Agent(
        ScriptedLLM(script) if offline else default_llm(),
        [create_ticket],
        system_prompt=TICKET_SYSTEM,
        checkpointer=FileCheckpointer(workdir / "checkpoints"),
        idempotency_store=FileIdempotencyStore(workdir / "idempotency.json") if idempotent else None,
        hooks=[CrashAfterTool("create_ticket")] if crash else [],
    )


def crash_child(workdir: Path, idempotent: bool, offline: bool) -> None:
    """在子进程里运行：Agent 调用 create_ticket 之后立刻"崩溃"。"""
    script = [call_tool("create_ticket", title="3 楼打印机卡纸", priority="high")]
    agent = build_ticket_agent(workdir, offline, idempotent, script, crash=True)
    res = agent.run(TICKET_PROMPT, run_id=RUN_ID)
    print(f"      [子进程] 模型这次没有调用 create_ticket，而是直接回答：{short(res.output)}", flush=True)


def run_crash_case(offline: bool, idempotent: bool) -> list[dict]:
    label = "有幂等保护（持久化的 IdempotencyStore）" if idempotent else "没有幂等保护"
    workdir = RUNS / ("crash_idempotent" if idempotent else "crash_plain")
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)

    step(f"【{label}】启动子进程运行 Agent")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--crash-child", str(workdir)]
    cmd += ["--idempotent"] if idempotent else []
    cmd += ["--offline"] if offline else []
    sys.stdout.flush()
    proc = subprocess.run(cmd)
    info(f"子进程退出码：{proc.returncode}" + ("（被杀死）" if proc.returncode == 137 else ""))

    tickets = TicketSystem(workdir / "tickets.json")
    saved = FileCheckpointer(workdir / "checkpoints").load(RUN_ID)
    if proc.returncode != 137 or saved is None:
        info("（这次没有触发崩溃，跳过恢复步骤）")
        return tickets.all()
    info(f"外部工单系统里已有 {len(tickets.all())} 张工单；检查点里最后一条消息是："
         f"role={saved.messages[-1]['role']}，带 {len(saved.messages[-1].get('tool_calls') or [])} 个工具调用、没有工具结果")
    info("→ 从检查点看，这个工具调用'还没执行'。但真实世界里它已经执行了。这就是崩溃窗口。")

    step("新进程接手：用同一个 run_id 从检查点恢复（agent.resume）")
    resume_script = [lambda msgs: reply(f"已为您提交工单。{msgs[-1]['content']}")]
    agent = build_ticket_agent(workdir, offline, idempotent, resume_script)
    res = agent.resume(RUN_ID)
    info(f"恢复后状态：{res.status}    🤖 {short(res.output, 120)}")
    all_tickets = tickets.all()
    mark = "✅" if len(all_tickets) == 1 else "❌"
    info(f"{mark} 外部工单系统里现在一共有 {len(all_tickets)} 张工单：{', '.join(t['id'] for t in all_tickets)}")
    return all_tickets


def scenario_crash(offline: bool) -> None:
    banner("场景 3：崩溃恢复 —— '工具执行了，但没来得及记下来'")
    info("流程：子进程里的 Agent 调用 create_ticket → 工具执行成功 → 写检查点之前进程被杀")
    info("      → 新进程从检查点恢复。框架会补执行'没有结果的工具调用'。问题是：它其实已经执行过了。")
    plain = run_crash_case(offline, idempotent=False)
    safe = run_crash_case(offline, idempotent=True)
    step("结果对比")
    info(f"没有幂等保护：{len(plain)} 张工单  {'❌ 同一个故障被报了两次' if len(plain) > 1 else ''}")
    info(f"有幂等保护：  {len(safe)} 张工单  {'✅ 恢复时命中幂等键 run_id:call_id，直接返回上次的结果' if len(safe) == 1 else ''}")
    takeaway("检查点保证'不丢进度'，幂等键保证'不重复执行'。两者合起来才是'恰好一次'的效果。")
    takeaway("还剩一个更窄的窗口：工具副作用完成后、幂等记录写入前崩溃。彻底解决要把幂等键传给下游系统，"
             "让它在同一个事务里去重（见 README 问题 5）。")


# =====================================================================
# 场景 4：预算
# =====================================================================


def scenario_budget(offline: bool) -> None:
    banner("场景 4：预算 —— 截停一个停不下来的 Agent")
    polled: list[str] = []

    @tool
    def check_report_status(report_id: Annotated[str, Field(description="报表编号，如 R-42")]) -> str:
        """查询报表的生成状态。"""
        polled.append(report_id)
        return f"报表 {report_id} 状态：生成中（进度 99%），请稍后再次查询。"

    system = ("你是报表助手。只有当报表状态变为'已完成'时才能回答用户；"
              "在此之前必须持续调用 check_report_status 查询，不要放弃，也不要询问用户。")
    # 离线剧本：输入 token 随对话历史变长而增长（和真实模型的计费方式一致）
    script = [lambda msgs: call_tool("check_report_status", report_id="R-42", input_tokens=60 * len(msgs) + 300,
                                     output_tokens=22)] * 30
    llm = ScriptedLLM(script) if offline else default_llm()
    budget = BudgetHook(max_tool_calls=3, max_cost_usd=0.05, max_seconds=180)
    agent = Agent(llm, [check_report_status], system_prompt=system, hooks=[budget], max_steps=30)

    step("工具永远返回'生成中 99%'，系统提示又要求'不许放弃'—— 一个典型的失控场景")
    info("预算：最多 3 次工具调用 / $0.05 / 180 秒；max_steps=30 作为最后一道保险")
    res = agent.run("帮我拿一下报表 R-42 的结果。")
    info(f"状态={res.status}  stop_reason={res.stop_reason}  模型调用 {res.steps} 次  工具执行 {len(polled)} 次")
    info(f"tokens={res.usage.total}  估算成本=${res.cost_usd:.5f}")
    info(f"给用户的回复：{short(res.output, 120)}")
    step("链路追踪（第 07 课详讲）—— 一眼看出它在原地打转：")
    for line in render_tree(res.trace).splitlines():
        info(line)
    per_step = [s.attrs.get("gen_ai.usage.input_tokens", 0) for s in res.trace.walk() if s.name == "llm.chat"]
    info(f"每一步的输入 token：{' → '.join(map(str, per_step))}")
    takeaway("每一轮都要把完整历史重新发给模型：步数越多，每一步越贵。循环 N 步的总成本近似按 N² 增长。")
    if res.stop_reason != "budget_exceeded":
        takeaway("模型这次自己停了下来。但你不能指望每次都这样 —— 预算是给'最坏情况'准备的。")
    takeaway("没有预算，它会一直跑到 max_steps=30。线上一个用户触发 30 次模型调用还算便宜，"
             "如果是 30 万个并发会话呢？预算要按步数、token、金额、时长、工具次数多维度设置。")
    takeaway("它每次都用完全相同的参数调用同一个工具 —— 练习 (a) 的 LoopGuard 能比预算更早、更准地识别这种循环。")


# =====================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="第 05 课 Demo：可靠性工程")
    parser.add_argument("--offline", action="store_true", help="使用离线剧本（ScriptedLLM），不调用真实模型")
    parser.add_argument("--crash-child", metavar="DIR", help=argparse.SUPPRESS)  # 场景 3 内部使用
    parser.add_argument("--idempotent", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.crash_child:
        crash_child(Path(args.crash_child), args.idempotent, args.offline)
        return

    RUNS.mkdir(parents=True, exist_ok=True)
    if args.offline:
        print("模式：离线剧本（ScriptedLLM）—— 结果确定、零成本")
    else:
        try:
            default_llm()
        except RuntimeError as e:
            sys.exit(f"❌ {e}\n   没有 API key 也没关系：加上 --offline 参数运行离线版本。")
        print(f"模式：真实模型（{MAIN_MODEL}）—— 每次运行结果可能略有不同")

    scenario_retry(args.offline)
    scenario_fallback(args.offline)
    scenario_crash(args.offline)
    scenario_budget(args.offline)
    banner("完成 🎉  检查点、模拟工单库等运行产物在 runs/05_reliability/ 下，可以打开看看")


if __name__ == "__main__":
    main()
