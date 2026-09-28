"""第 06 课 Demo：用真实模型跑一遍 5 种编排模式，并量出每种模式的"价格"。

    python lessons/06_orchestration/demo.py            # 真实模型（读取 .env）
    python lessons/06_orchestration/demo.py --offline  # 离线剧本，无需 API key

依次演示：
  1. route                客服分流：先分类，再交给专门的处理器
  2. parallel + 投票       三个视角并行评审一段代码，再汇总决定
     附：并发上限和"一个失败、其余取消"量给你看；共享计数器什么时候不需要锁、什么时候会丢更新
  3. orchestrator_workers 编排者动态拆解任务 → 执行者并行 → 汇总
  4. evaluator_optimizer  生成 slogan → 评审（代码检查 + 模型检查）→ 按意见修改
  5. agent_as_tool        主管 Agent 把子问题委派给两个专家 Agent
     附：主管被取消，正在跑的专家跟着停
每个模式结束时打印：耗时、模型调用次数、同时在途的模型调用峰值、token 用量。最后给出对比表。

全部是 async：所有模型调用都在同一个事件循环里 await，parallel 用 asyncio 真并发，不开线程。
离线模式下 ScriptedLLM 每次调用等 0.2 秒（asyncio.sleep），这样并行和串行的耗时差别也看得见。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import unicodedata
from contextlib import contextmanager
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agentkit import Agent, Hook, ScriptedLLM, ToolContext, ToolError, Usage, call_tool, default_llm, reply, tool
from agentkit.tracing import Tracer, render_tree
from agentkit.workflows import (
    Review,
    agent_as_tool,
    complete,
    complete_json,
    evaluator_optimizer,
    majority_vote,
    orchestrator_workers,
    parallel,
    route,
)

# ====================================================================== 计量：包一层 LLM，数调用次数和 token


class Meter:
    """全局计量器：调用次数、token、同时在途的调用数。

    没有锁。所有模型调用都在同一个事件循环（同一个线程）里，事件循环只在 await 处切换协程；
    下面 Metered.chat 里每一段"读-改-写"（+= 1、usage 相加、max）中间都没有 await，
    所以不会有别的协程插进来 —— 这几行在事件循环眼里是原子的。
    反过来，只要读和写之间夹了一个 await，就会丢更新（见 demo_shared_counter）。
    """

    def __init__(self):
        self.calls = 0
        self.usage = Usage()
        self.in_flight = 0
        self.max_in_flight = 0


class Metered:
    """装饰器模式：对外还是一个 LLM，顺手把每次调用记到 Meter 上。"""

    def __init__(self, llm, meter: Meter):
        self.llm, self.meter, self.model = llm, meter, llm.model

    async def chat(self, messages, tools=None, **kwargs):
        m = self.meter
        m.in_flight += 1  # ↓ 这两行之间没有 await
        m.max_in_flight = max(m.max_in_flight, m.in_flight)
        try:
            response = await self.llm.chat(messages, tools, **kwargs)  # 唯一的 await：在这里等模型时，别的协程可以运行
        finally:
            m.in_flight -= 1
        m.calls += 1  # ↓ 同样没有 await：这三行不会被打断
        m.usage = m.usage + response.usage
        return response


METER = Meter()
SUMMARY: list[tuple[str, float, int, int, int, str]] = []
OFFLINE_LATENCY = 0.2  # 离线模式下 ScriptedLLM 每次调用的模拟耗时（秒）


@contextmanager
def measure(name: str, who_decides: str):
    calls0, tokens0, cached0, t0 = METER.calls, METER.usage.total, METER.usage.cached_input_tokens, time.perf_counter()
    METER.max_in_flight = 0  # 各模式依次执行，所以每个模式开始时清零即可
    yield
    elapsed, calls, tokens = time.perf_counter() - t0, METER.calls - calls0, METER.usage.total - tokens0
    cached = METER.usage.cached_input_tokens - cached0
    cache_note = f"（其中 {cached} 个输入 token 命中提示词缓存）" if cached else ""
    print(f"\n  ⏱ 耗时 {elapsed:.1f}s ｜ 模型调用 {calls} 次（同时在途峰值 {METER.max_in_flight}）｜ tokens {tokens}{cache_note}")
    SUMMARY.append((name, elapsed, calls, METER.max_in_flight, tokens, who_decides))


def section(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def note(text: str) -> None:
    for line in text.strip().splitlines():
        print(f"  💡 {line.strip()}")


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def show_block(text: str, max_lines: int = 18, indent: str = "    │ ") -> None:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    for ln in lines[:max_lines]:
        print(f"{indent}{ln}")
    if len(lines) > max_lines:
        print(f"{indent}……（省略 {len(lines) - max_lines} 行）")


def user_prompt(messages) -> str:
    return next(m["content"] for m in reversed(messages) if m["role"] == "user")


# ====================================================================== 1. 路由

ROUTES = {
    "billing": "账单、扣费、退款、发票相关",
    "tech": "登录、报错、故障、功能使用问题",
    "sales": "购买、报价、升级套餐、商务合作",
    "other": "以上都不是，例如闲聊或投诉建议",
}
HANDLERS = {
    "billing": "账单组：专用提示词 + 查账单/退款工具（写操作需审批）",
    "tech": "技术支持：专用提示词 + 查日志/知识库工具",
    "sales": "销售线索：写入 CRM，转人工销售",
    "other": "通用助手：礼貌回复，必要时转人工",
}
TICKETS = ["上个月被重复扣费了两次，能退一笔吗？", "SSO 登录一直提示 token 过期，清了缓存也没用", "我们 200 人的团队想买企业版，怎么报价？"]


def offline_route(messages):
    text = user_prompt(messages).split("请求：", 1)[-1].split("\n\n只输出", 1)[0]
    label = "billing" if "扣费" in text else "tech" if "登录" in text else "sales" if "报价" in text else "other"
    return reply(json.dumps({"route": label, "reason": "（离线剧本）按关键词判断"}, ensure_ascii=False))


async def demo_route(llm) -> None:
    section("模式 1：route 路由 —— 客服工单分流")
    note("先用一次便宜的分类调用决定\"交给谁\"，每个处理器有自己专门的提示词和工具。流程由代码决定。")
    with measure("route 路由", "代码（模型只做分类）"):
        for ticket in TICKETS:  # 三张工单代表三个独立的请求，这里依次处理
            label = await route(llm, ticket, ROUTES)
            print(f"\n  📨 {ticket}\n     → 类别 {label!r} → {HANDLERS[label]}")
    note(
        """route() 内部用 complete_json + Literal 枚举：返回值保证是 ROUTES 的某个 key，下游 HANDLERS[label] 绝不会 KeyError。
        生产中常在前面再加一层关键词规则（本课练习 1 的 hybrid_route）：能用规则分的就不花钱问模型。"""
    )


# ====================================================================== 2. 并行 + 投票

CODE_CHANGE = '''def get_user(name):
    sql = f"SELECT * FROM users WHERE name = '{name}'"
    return db.execute(sql).fetchall()'''

PERSPECTIVES = {
    "安全": "只关注安全问题（注入、越权、敏感信息泄露），其他方面一律不管",
    "性能": "只关注性能问题（查询效率、资源消耗），其他方面一律不管",
    "可读性": "只关注可读性（命名、结构、注释），其他方面一律不管",
}


class Verdict(BaseModel):
    decision: Literal["APPROVE", "REJECT"] = Field(description="APPROVE=可以合并，REJECT=不能合并")
    reason: str = Field(description="一句话理由，不超过 30 字")


def offline_reviewer(messages):
    prompt = user_prompt(messages)
    if "安全问题" in prompt:
        v = {"decision": "REJECT", "reason": "字符串拼接 SQL，存在 SQL 注入"}
    elif "性能问题" in prompt:
        v = {"decision": "APPROVE", "reason": "单表按条件查询，数据量小时可接受"}
    else:
        v = {"decision": "APPROVE", "reason": "函数短小，命名清晰"}
    return reply(json.dumps(v, ensure_ascii=False))


async def demo_parallel(llm) -> None:
    section("模式 2：parallel 并行 + 投票 —— 三个视角同时评审一段代码")
    print("  待评审的改动：")
    show_block(CODE_CHANGE, indent="    ┃ ")
    durations: dict[str, float] = {}

    def reviewer(name: str, focus: str):
        async def run() -> Verdict:  # parallel 要的是"调用后返回协程的函数"
            t0 = time.perf_counter()
            prompt = f"你是代码评审员，{focus}。请评审下面这段 Python 改动，决定能否合并。\n```python\n{CODE_CHANGE}\n```"
            verdict = await complete_json(llm, prompt, Verdict)
            durations[name] = time.perf_counter() - t0
            return verdict

        return run

    with measure("parallel 并行投票", "代码"):
        t0 = time.perf_counter()
        verdicts = await parallel([reviewer(n, f) for n, f in PERSPECTIVES.items()])
        wall = time.perf_counter() - t0
        print()
        for name, v in zip(PERSPECTIVES, verdicts):
            print(f"  [{pad(name, 6)}] {v.decision:<7} {v.reason}   （{durations[name]:.1f}s）")
        decisions = [v.decision for v in verdicts]
        majority = majority_vote(decisions)
        security = verdicts[0].decision
        veto = "REJECT" if "REJECT" == security else majority
        print(
            f"\n  串行需要约 {sum(durations.values()):.1f}s，并行实际 {wall:.1f}s，同时在途的模型调用峰值 {METER.max_in_flight}"
            " —— 延迟取决于最慢的那个，而不是总和。"
        )
        print(f"  少数服从多数（majority_vote）：{majority}")
        print(f"  安全一票否决（业务规则）    ：{veto}")
    if majority == "APPROVE" and veto == "REJECT":
        note("多数票放行了一个有 SQL 注入的改动！投票规则本身是业务决策：安全、合规类检查应该一票否决，而不是少数服从多数。")
    else:
        note("这次多数票和一票否决的结论一致，但别依赖运气：安全、合规类检查应该一票否决，而不是少数服从多数。")
    note("并行的两种用法：分片（sectioning，不同视角/不同数据片）和投票（voting，同一问题问多次取共识）。")


# ====================================================================== 2 附：并行的两条承诺，量给你看


async def demo_parallel_mechanics() -> None:
    section("模式 2 附：并行真的发生了吗？一路失败时其余怎么办？（ScriptedLLM 机制演示，不调用真实模型）")
    note("用延迟可控的 ScriptedLLM（每次调用 0.2s，asyncio.sleep）把 parallel 的承诺量出来。max_in_flight 是 ScriptedLLM 自己数的在途峰值。")
    lat = 0.2
    print()
    for cap in (1, 4, 10):
        llm = ScriptedLLM(responder=lambda m: reply("APPROVE"), latency=lat)
        t0 = time.perf_counter()
        await parallel([lambda i=i: complete(llm, f"评审第 {i} 个文件") for i in range(10)], max_concurrency=cap)
        wall = time.perf_counter() - t0
        print(
            f"  10 次调用，max_concurrency={cap:<2} → 同时在途峰值 {llm.max_in_flight:<2}，用时 {wall:.2f}s"
            f"（理论值 {math.ceil(10 / cap)} 波 × {lat}s = {math.ceil(10 / cap) * lat:.1f}s）"
        )
    note("峰值恰好等于上限：并发是真的，上限也是真的。上限就是背压 —— 一个请求扇出成几十个调用时，别一次全压到模型网关上。")

    events: list[str] = []
    slow = ScriptedLLM(responder=lambda m: reply("APPROVE"), latency=1.0)

    def reviewer(name: str):
        async def run() -> str:
            try:
                out = await complete(slow, f"从{name}角度评审")
                events.append(f"{name}：完成")
                return out
            except asyncio.CancelledError:
                events.append(f"{name}：被取消（模型调用中途停下，不会在后台继续跑）")
                raise

        return run

    async def broken() -> str:
        await asyncio.sleep(0.1)
        raise ValueError("可读性评审的提示词模板渲染失败")

    print("\n  三路并行：安全、性能两路的模型调用各要 1.0s；第三路 0.1s 时抛异常。")
    t0 = time.perf_counter()
    try:
        await parallel([reviewer("安全"), reviewer("性能"), broken])
    except ValueError as e:
        print(f"  parallel 在 {time.perf_counter() - t0:.2f}s 时把异常抛给调用方：{e}")
    for ev in events:
        print(f"    · {ev}")
    print(f"  此刻模型在途调用 {slow.in_flight} 个；发出过 {slow.call_count} 次调用，一次都没有跑完")

    async def tolerant(fn):
        try:
            return await fn()
        except Exception:  # noqa: BLE001  失败的一路当弃权票；CancelledError 是 BaseException，照常向外传播
            return None

    ok = ScriptedLLM(responder=lambda m: reply("REJECT"), latency=lat)
    answers = await parallel([
        lambda: tolerant(lambda: complete(ok, "安全")),
        lambda: tolerant(lambda: complete(ok, "性能")),
        lambda: tolerant(broken),
    ])
    print(f"\n  换一种写法：每一路自己兜底，失败 = 弃权票 None → {answers}（另外两路照常完成）")
    note(
        """parallel 的默认语义是"一个失败、其余立刻取消"：结果反正拿不全了，就别让其余几路在后台继续花钱。
        想要"部分失败也能汇总"（投票里失败的一路算弃权），就让每一路自己 try/except Exception —— parallel 看不到异常，也就不会取消别人。
        弃权票 None 正好交给练习 2 的 vote_with_quorum：它计入总票数、拉低把握程度。"""
    )


async def demo_shared_counter() -> None:
    section("模式 2 附：计量器为什么不需要锁 —— 以及什么时候会丢更新")
    note("Meter 被三路并行的评审同时更新，却没有加锁。原因：一个事件循环一次只运行一个协程，只在 await 处切换。")
    n = 1000

    class Counter:
        value = 0

    ok, racy, locked = Counter(), Counter(), Counter()
    lock = asyncio.Lock()

    async def add_ok():
        await asyncio.sleep(0)  # 先 await（相当于等模型回复）……
        ok.value += 1  # ……再读-改-写，中间没有 await：别的协程插不进来

    async def add_racy():
        v = racy.value  # 读
        await asyncio.sleep(0)  # 读和写之间 await 了一下（比如 await 写一条审计日志、await 查一次 Redis）
        racy.value = v + 1  # 写：用的是 await 之前读到的旧值，别的协程在这期间的更新被覆盖了

    async def add_locked():
        async with lock:  # 读和写之间非 await 不可时，用 asyncio.Lock 把整段包起来
            v = locked.value
            await asyncio.sleep(0)
            locked.value = v + 1

    await asyncio.gather(*(add_ok() for _ in range(n)))
    await asyncio.gather(*(add_racy() for _ in range(n)))
    await asyncio.gather(*(add_locked() for _ in range(n)))
    print(f"\n  {n} 个协程各加 1：")
    print(f"    读-改-写之间没有 await        → {ok.value}")
    print(f"    读和写之间有一个 await        → {racy.value}（丢了 {n - racy.value} 次更新）❌")
    print(f"    同样有 await，但用 asyncio.Lock → {locked.value}")
    note(
        """所以 Meter 不需要锁：Metered.chat 里每段"读-改-写"之间都没有 await。以前用线程并发时，+= 本身就可能被另一个线程打断，才必须加 threading.Lock。
        这条规则只在一个事件循环（一个进程）里成立：多个 worker 进程之间共享计数，要靠数据库的原子更新（第 13 课）。"""
    )


# ====================================================================== 3. 编排者-执行者

ORCH_TASK = "我们要在公司内部上线一个「报销助手」Agent（能识别发票、填写报销单、提交审批）。请给出上线前的检查清单：最终不超过 8 条，每条一句话。"
OFFLINE_PLAN = ["梳理报销助手的安全与权限风险及对策", "梳理成本与稳定性方面的上线检查项", "梳理可观测性与效果评估方面的上线检查项"]


def offline_orchestrator(messages):
    prompt = user_prompt(messages)
    if "拆解成" in prompt:
        return reply(json.dumps({"subtasks": OFFLINE_PLAN}, ensure_ascii=False))
    if prompt.startswith("原始任务："):
        return reply(
            "报销助手上线前检查清单：\n"
            "1. 安全与权限：只能读写本人报销单；提交审批前人工确认；发票内容按不可信数据处理。\n"
            "2. 成本与稳定性：单次运行设置 token/金额上限；模型超时重试与降级；幂等提交防重复报销。\n"
            "3. 可观测与评估：全链路 trace；准备 50 张真实发票的评估集；上线后抽检识别准确率。"
        )
    if "安全" in prompt:
        return reply("1. 按用户隔离报销单读写\n2. 提交审批前人工确认\n3. 发票文字当不可信数据")
    if "成本" in prompt:
        return reply("1. 单次运行 token 上限\n2. 超时重试加降级模型\n3. 提交接口幂等防重复")
    return reply("1. 每步记录 trace\n2. 建发票识别评估集\n3. 上线后抽检准确率")


async def demo_orchestrator(llm) -> None:
    section("模式 3：orchestrator_workers 编排者-执行者 —— 动态拆解任务")
    print(f"  任务：{ORCH_TASK}")
    received: list[str] = []

    async def worker(subtask: str) -> str:  # 执行者是 async 函数：orchestrator_workers 用 parallel 同时跑它们
        received.append(subtask)
        return await complete(llm, f"{subtask}\n\n要求：只给 3 条要点，每条不超过 30 字。", system="你是企业 AI 平台的资深工程师，回答务实、具体。")

    with measure("orchestrator_workers", "模型（拆解）+ 代码（执行）"):
        final = await orchestrator_workers(llm, ORCH_TASK, worker, max_subtasks=3)
        print("\n  编排者拆出的子任务（每个交给一个执行者并行处理）：")
        for s in received:
            print(f"    • {s}")
        print("\n  汇总后的最终答复：")
        show_block(final)
    note(
        """和 parallel 的区别：子任务不是写死在代码里的，而是模型看了具体输入后决定的 —— 更灵活，但也更难预测。
        调用次数 = 1（拆解）+ N（执行）+ 1（汇总）；用 max_subtasks 给 N 设上限，否则成本不可控。
        汇总步骤的输出也要限长：任务里去掉"不超过 8 条"再跑一次，汇总可能写出几百行、耗时一分钟以上。"""
    )


# ====================================================================== 4. 评估-优化

PRODUCT = "智能手表 T1：一次充电续航 30 天；支持心率、血氧、睡眠监测；50 米防水"
SELLING_POINT = "续航长（一次充电能用 30 天）"
MAX_CHARS = 15


def count_chars(text: str) -> int:
    """数"字"：汉字、字母、数字各算 1 个，标点和空格不算。"""
    return sum(1 for ch in text if ch.isalnum())


def offline_copywriter(messages):
    prompt = user_prompt(messages)
    if prompt.startswith("你是广告评审"):
        return reply(json.dumps({"passed": True, "feedback": ""}, ensure_ascii=False))
    if "上一版评审意见" in prompt:
        return reply("一次充电，畅用三十天")
    return reply("告别每天充电的焦虑，T1 一次充电陪你整整 30 天，心率血氧睡眠全都懂")


async def demo_evaluator(llm) -> None:
    section("模式 4：evaluator_optimizer 评估-优化 —— 写一句产品 slogan")
    print(f"  产品：{PRODUCT}")
    print(f"  验收标准（在评审器里）：不超过 {MAX_CHARS} 字，且体现核心卖点「{SELLING_POINT}」")
    note("生成器只拿到产品介绍（模拟\"需求没写全\"的真实情况），看评审器如何把它拉回来。")
    checked_by: list[str] = []

    async def generate(task: str, feedback: str | None) -> str:
        prompt = f"为下面的产品写一句中文广告 slogan，只输出 slogan 本身。\n产品：{task}"
        if feedback:
            prompt += f"\n\n上一版评审意见：{feedback}\n请按意见重写。"
        return (await complete(llm, prompt)).strip().strip("\"'“”「」")

    async def evaluate(candidate: str) -> Review:
        n = count_chars(candidate)
        if n > MAX_CHARS:  # 能用代码判断的，就不要花钱问模型：确定、免费、不会被说服
            checked_by.append(f"代码检查（{n} 字）")
            return Review(passed=False, feedback=f"当前 {n} 字，超过 {MAX_CHARS} 字上限。请压缩到 {MAX_CHARS} 字以内，并保留核心卖点「{SELLING_POINT}」。")
        checked_by.append(f"模型检查（{n} 字）")
        return await complete_json(llm, f"你是广告评审。判断这句 slogan 是否清楚体现了核心卖点「{SELLING_POINT}」。没体现就给出具体修改意见。\nslogan：{candidate}", Review)

    with measure("evaluator_optimizer", "代码（循环）+ 模型（生成/评审）"):
        final, reviews = await evaluator_optimizer(generate, evaluate, PRODUCT, max_rounds=3)
        # evaluator_optimizer 只返回最终稿；为了展示过程，我们从评审记录里还原每一轮
        for i, (review, who) in enumerate(zip(reviews, checked_by), 1):
            status = "✅ 通过" if review.passed else f"❌ 退回：{review.feedback}"
            print(f"\n  第 {i} 轮 · {who} → {status}")
        print(f"\n  最终 slogan：「{final}」（{count_chars(final)} 字，{'合格' if reviews[-1].passed else '达到轮数上限仍未合格 → 应转人工'}）")
    note(
        """评审标准必须具体、可检查；"写得更好一点"这种反馈会让循环原地打转。
        max_rounds 是必需的刹车：模型可能永远改不到合格，达到上限要有兜底（人工、降级、返回最好的一版）。"""
    )


# ====================================================================== 5. 多 Agent：Agent 即工具

ORDERS = {
    "A1001": {"owner": "u_1001", "product": "入耳式降噪耳机 X1", "category": "耳机", "price": 899,
              "signed_at": "2026-09-22", "opened": True},
}
POLICIES = [
    "【无理由退货】大部分商品签收后 7 天内可无理由退货，商品需完好、不影响二次销售。",
    "【贴身商品】入耳式耳机等贴身商品一经拆封，因卫生原因不支持无理由退货。",
    "【质量问题】签收后 15 天内出现性能故障，可凭检测结果申请退货或换货。",
]


@tool
def get_order(order_id: Annotated[str, Field(description="订单号，如 A1001")], ctx: ToolContext) -> str:
    """查询订单详情（商品、品类、价格、签收日期、是否已拆封）。只能查询当前登录用户自己的订单。"""
    order = ORDERS.get(order_id)
    if order is None:
        raise ToolError(f"订单 {order_id} 不存在")
    if order["owner"] != ctx.user_id:  # 归属校验用系统注入的身份，而不是模型传的参数
        raise ToolError("无权查看该订单：它不属于当前登录用户")
    return json.dumps({k: v for k, v in order.items() if k != "owner"}, ensure_ascii=False)


@tool
def search_policy(query: Annotated[str, Field(description="检索关键词，多个词用空格分隔，如'耳机 拆封 退货'")]) -> str:
    """检索售后政策条款原文。"""
    words = query.split()
    hits = [p for p in POLICIES if any(w in p for w in words)]
    return "\n".join(hits or POLICIES)


SUPERVISOR_PROMPT = """你是售后客服主管。你自己不直接查询任何系统，而是把子问题委派给专家：
- 订单事实（商品、签收日期、是否拆封）→ ask_order_expert
- 售后政策条款 → ask_policy_expert
注意：专家看不到用户的原话，委派时要把必要的上下文写进 task。
拿到专家结论后，给用户一个明确、简短（不超过 5 行）的最终答复，并说明依据。"""
ORDER_EXPERT_PROMPT = "你是订单查询专家。用 get_order 查询订单，只陈述查到的事实（商品、品类、签收日期、是否拆封），不做政策判断。"
POLICY_EXPERT_PROMPT = "你是售后政策专家。用 search_policy 检索条款，引用条款原文回答；如果不知道订单细节，分情况说明。"
QUESTION = "我的订单 A1001 还能退货吗？今天是 2026-09-27。"


def build_agents(make_llm):
    # 三个 Agent 共用一个 Tracer：专家的 Span 才能挂到主管的 tool span 下面，一次请求 = 一棵 trace 树
    tracer = Tracer()
    order_expert = Agent(make_llm("order"), [get_order], system_prompt=ORDER_EXPERT_PROMPT, name="order_expert",
                         max_steps=4, tracer=tracer)
    policy_expert = Agent(make_llm("policy"), [search_policy], system_prompt=POLICY_EXPERT_PROMPT, name="policy_expert",
                          max_steps=4, tracer=tracer)
    supervisor = Agent(
        make_llm("supervisor"),
        [
            agent_as_tool(order_expert, "ask_order_expert", "订单专家：查询某个订单的事实信息。task 里要写清订单号和要查什么。"),
            agent_as_tool(policy_expert, "ask_policy_expert", "售后政策专家：解答退换货政策。task 里要写清商品品类、签收时间、是否拆封等情况。"),
        ],
        system_prompt=SUPERVISOR_PROMPT,
        name="supervisor",
        max_steps=6,
        tracer=tracer,
    )
    return supervisor


def offline_multi_agent_scripts() -> dict[str, list]:
    def order_answer(messages):
        return reply(f"订单事实：{messages[-1]['content']}")

    def policy_answer(messages):
        return reply("相关条款：\n" + messages[-1]["content"])

    def final(messages):
        return reply(
            "很抱歉，A1001 不能无理由退货：它是入耳式耳机且已拆封，按【贴身商品】条款不支持无理由退货。\n"
            "不过签收（9/22）至今 5 天，仍在 15 天质量问题期内：如果耳机有性能故障，可以申请退货或换货。"
        )

    return {
        "order": [call_tool("get_order", order_id="A1001"), order_answer],
        "policy": [call_tool("search_policy", query="耳机 拆封 退货"), policy_answer],
        # 主管先查订单事实，再带着事实去问政策 —— 第二次委派依赖第一次的结果，所以是串行的
        "supervisor": [
            call_tool("ask_order_expert", task="查询订单 A1001 的商品、品类、签收日期、是否已拆封"),
            call_tool("ask_policy_expert", task="入耳式耳机，2026-09-22 签收、已拆封，今天 2026-09-27，能否退货？"),
            final,
        ],
    }


async def demo_multi_agent(make_llm) -> None:
    section("模式 5：agent_as_tool 多 Agent —— 主管 + 订单专家 + 政策专家")
    print(f"  用户（tenant=shop, user=u_1001）：{QUESTION}")
    supervisor = build_agents(make_llm)
    with measure("agent_as_tool 多 Agent", "模型（主管决定委派谁）"):
        result = await supervisor.run(QUESTION, metadata={"tenant_id": "shop", "user_id": "u_1001"})
        print("\n  主管的委派过程：")
        calls = {c["id"]: c for m in result.messages if m.get("tool_calls") for c in m["tool_calls"]}
        for m in result.messages:
            if m["role"] == "tool":
                c = calls[m["tool_call_id"]]
                task = json.loads(c["function"]["arguments"]).get("task", "")
                print(f"\n    → {c['function']['name']}(task={task!r})")
                show_block(m["content"], max_lines=6, indent="      ← ")
        print(f"\n  🤖 最终答复（status={result.status}）：")
        show_block(result.output or "", max_lines=10)
        print("\n  一次请求的完整链路追踪（专家 Agent 的每一步都嵌套在主管对应的 tool span 下）：")
        show_block(render_tree(result.trace), max_lines=20, indent="    ")
    note(
        """专家有独立的提示词、工具和上下文窗口：主管的上下文里只有"委派了什么、专家答了什么"，不会被专家的中间步骤撑爆。
        代价：专家看不到用户原话（上下文割裂），委派描述写漏了，专家就会答偏；调用次数和 token 成倍增加。
        身份（tenant/user）通过 ctx 透传给专家，get_order 用它做订单归属校验 —— 模型无法冒充别人查订单。
        trace 能嵌套需要两个条件：三个 Agent 共用同一个 Tracer；专家运行时能看到主管的"当前 Span"（记在 contextvars 里）。
        agent_as_tool 是 async 工具，直接在主管的 task 里 await，contextvars 自然跟着走；同步工具在线程池里跑，agentkit 用 copy_context 带过去。
        少了任何一个，每个专家都会变成一条孤立的 trace，线上排查时就拼不回"这次请求到底发生了什么"。"""
    )


# ====================================================================== 5 附：主管被取消，专家跟着停


class RecordEnd(Hook):
    """每次运行结束（包括被取消）时记下最终状态。on_run_end 在收尾阶段一定会被调用。"""

    def __init__(self, who: str, log: list[str]):
        self.who, self.log = who, log

    def on_run_end(self, state) -> None:
        self.log.append(f"{self.who}：status={state.status}，stop_reason={state.stop_reason}")


async def demo_cancel_propagation() -> None:
    section("模式 5 附：主管被取消，专家跟着停（ScriptedLLM 机制演示，不调用真实模型）")
    note("agent_as_tool 生成的是 async 工具：专家的 agent.run 就在主管的 task 里被 await。取消主管，取消会一路传进专家正在等的那次模型调用。")
    ends: list[str] = []
    expert_llm = ScriptedLLM([call_tool("get_order", order_id="A1001"), reply("订单事实：……")], latency=1.0)
    expert = Agent(expert_llm, [get_order], system_prompt=ORDER_EXPERT_PROMPT, name="order_expert",
                   hooks=[RecordEnd("订单专家", ends)])
    boss_llm = ScriptedLLM([call_tool("ask_order_expert", task="查询订单 A1001 的签收日期和是否拆封"), reply("（不会走到这里）")])
    supervisor = Agent(boss_llm, [agent_as_tool(expert, "ask_order_expert", "订单专家：查询订单事实。")],
                       system_prompt=SUPERVISOR_PROMPT, name="supervisor", hooks=[RecordEnd("主管", ends)])

    task = asyncio.ensure_future(supervisor.run(QUESTION, metadata={"tenant_id": "shop", "user_id": "u_1001"}))
    while expert_llm.in_flight == 0 and not task.done():  # 等到专家的第一次模型调用（要 1.0s）已经发出
        await asyncio.sleep(0.01)
    print(f"\n  专家的模型调用在途 {expert_llm.in_flight} 个 → 用户断开连接，取消主管的运行（task.cancel()）")
    t0 = time.perf_counter()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print(f"  {(time.perf_counter() - t0) * 1000:.1f}ms 后，主管的 run() 抛出 CancelledError（取消照常向外传播，没有被吞掉）")
    for line in ends:
        print(f"    · {line}")
    print(f"  专家模型此刻在途 {expert_llm.in_flight} 个；它一共被调用 {expert_llm.call_count} 次，剧本里的第 2 次调用没有发生，get_order 也没有执行")
    note(
        """先结束的是专家，再是主管：取消从最里层的 await（专家正在等的模型调用）开始，一层层往外收尾，每一层都把检查点记为 cancelled。
        同步写法里做不到这一点：专家在线程里跑，线程杀不掉，用户走了它还会把模型调用和工具调用跑完。"""
    )


# ====================================================================== main


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="使用 ScriptedLLM 剧本，不调用真实模型")
    args = parser.parse_args()

    real = None
    if args.offline:
        print(f"🧪 离线模式：使用 ScriptedLLM 剧本（输出是预先写好的，调用次数和数据流与真实运行一致；每次调用模拟耗时 {OFFLINE_LATENCY}s）")

        # 剧本项是函数：根据收到的提示词决定回什么 —— 这样并行调用的先后顺序不影响结果
        def scripted(brain, n: int = 20):
            return Metered(ScriptedLLM([brain] * n, latency=OFFLINE_LATENCY), METER)

        llms = {
            "route": scripted(offline_route),
            "parallel": scripted(offline_reviewer),
            "orchestrator": scripted(offline_orchestrator),
            "evaluator": scripted(offline_copywriter),
        }
        agent_scripts = offline_multi_agent_scripts()

        def make_agent_llm(role: str):
            return Metered(ScriptedLLM(agent_scripts[role], latency=OFFLINE_LATENCY), METER)
    else:
        real = default_llm()
        metered = Metered(real, METER)
        print(f"🌐 真实模型模式：{real.model}（如需离线运行，加 --offline）")
        llms = dict.fromkeys(["route", "parallel", "orchestrator", "evaluator"], metered)

        def make_agent_llm(role: str):
            return metered

    try:
        await demo_route(llms["route"])
        await demo_parallel(llms["parallel"])
        await demo_parallel_mechanics()
        await demo_shared_counter()
        await demo_orchestrator(llms["orchestrator"])
        await demo_evaluator(llms["evaluator"])
        await demo_multi_agent(make_agent_llm)
        await demo_cancel_propagation()
    finally:
        if real is not None:
            await real.aclose()

    section("对比：同样是\"用 LLM 完成任务\"，不同编排模式的价格")
    print(f"  {pad('模式', 24)}{pad('耗时', 9)}{pad('调用', 7)}{pad('峰值并发', 10)}{pad('tokens', 9)}流程由谁决定")
    for name, elapsed, calls, peak, tokens, who in SUMMARY:
        print(f"  {pad(name, 24)}{pad(f'{elapsed:.1f}s', 9)}{pad(str(calls), 7)}{pad(str(peak), 10)}{pad(str(tokens), 9)}{who}")
    note(
        """从上到下，灵活性越来越高，调用次数、token、延迟和不确定性也越来越高。
        峰值并发一栏：parallel 和 orchestrator_workers 的执行者是真的同时在等模型；多 Agent 这次是串行委派（第二次依赖第一次的结果）。
        选型原则：先用能解决问题的最简单模式；只有当简单模式明显不够用时，才往下走一级。"""
    )
    print("\n  下一步：完成 exercise.py，然后运行 make lesson N=06")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "LLM_API_KEY" not in str(e):
            raise
        print(f"\n❌ {e}\n提示：没有 API key 时可以加 --offline 运行。")
        sys.exit(1)
