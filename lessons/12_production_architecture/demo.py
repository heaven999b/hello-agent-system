"""第 12 课 Demo：给 Agent 套上"生产外壳"。

场景：一家 HR SaaS 公司，同一套 HR 助手服务多家企业客户（租户），套餐不同。
  1. 多租户限流：一个租户的脚本失控狂刷接口 —— 全局一个桶 vs 每租户一个桶
  2. 模型路由：不同请求选不同模型，和"全部用最强模型"比一比每天的账单
  3. 迷你网关端到端：鉴权身份 → 限流 → 路由 → 调模型 → 按租户记账
  4. 无状态 worker：worker A 跑到一半暂停等审批 → 状态进检查点 → worker B 接手完成

运行：
    python lessons/12_production_architecture/demo.py            # 第 3、4 节调用真实模型
    python lessons/12_production_architecture/demo.py --offline  # 全部离线，无需 API key

第 1、2 节是纯模拟（用假时钟），两种模式输出相同。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from typing import Annotated

from pydantic import Field

from agentkit import (
    Agent,
    FileCheckpointer,
    PermissionPolicy,
    ScriptedLLM,
    ToolContext,
    call_tool,
    default_llm,
    reply,
    tool,
)

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"


def section(title: str, component: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(f"对应参考架构中的：{component}\n")


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def load_impl():
    """优先用你在 exercise.py 里的实现；还没写完就用参考答案。"""
    sys.path.insert(0, str(HERE))
    import exercise  # noqa: E402

    try:
        exercise.TokenBucket(1, 1).try_acquire()
        exercise.choose_model({"input_tokens": 1}, [exercise.ModelSpec("m", 10, True, True, 1, 1)])
        return exercise, "exercise.py（你的实现）"
    except NotImplementedError:
        import solution  # noqa: E402

        return solution, "solution.py（参考答案 —— 完成练习后会自动换成你的实现）"


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------- 1. 多租户限流


def demo_rate_limit(impl) -> None:
    section("1. 多租户限流：吵闹的邻居（noisy neighbor）", "API 网关 → 限流")
    plans = {
        "free": impl.Plan("free", capacity=5, refill_rate=1),
        "pro": impl.Plan("pro", capacity=20, refill_rate=5),
        "enterprise": impl.Plan("enterprise", capacity=50, refill_rate=20),
    }
    tenants = {  # 租户: (套餐, 每秒请求数)
        "acme": ("enterprise", 10),
        "globex": ("pro", 4),
        "initech": ("free", 0.5),
        "hooli": ("free", 50),  # 客户的脚本有 bug，死循环调用接口
    }
    print("模拟 10 秒流量（每 0.1 秒一个时间片，同一时间片内请求随机到达）：")
    for t, (plan, rate) in tenants.items():
        note = "   ← 脚本失控" if t == "hooli" else ""
        print(f"  {t:<9} 套餐 {plan:<11} 每秒 {rate:>4} 个请求{note}")

    def simulate(acquire) -> dict[str, list[int]]:
        clock_state["t"] = 0.0
        rng = random.Random(42)
        credit = {t: 0.0 for t in tenants}
        stats = {t: [0, 0] for t in tenants}  # [发出, 放行]
        for _ in range(100):
            arrivals = []
            for t, (_, rate) in tenants.items():
                credit[t] += rate * 0.1
                n = int(credit[t])
                credit[t] -= n
                arrivals += [t] * n
            rng.shuffle(arrivals)
            for t in arrivals:
                stats[t][0] += 1
                stats[t][1] += acquire(t)
            clock_state["t"] += 0.1
        return stats

    clock_state = {"t": 0.0}

    def clock():
        return clock_state["t"]

    global_bucket = impl.TokenBucket(capacity=60, refill_rate=30, clock=clock)
    shared = simulate(lambda t: global_bucket.try_acquire())
    limiter = impl.TenantRateLimiter(plans, {t: p for t, (p, _) in tenants.items()}, "free", clock=clock)
    isolated = simulate(lambda t: limiter.try_acquire(t))

    print(f"\n  {pad('租户', 10)}{pad('发出', 8)}{pad('A. 全局一个桶（容量 60，30/秒）', 34)}B. 每租户一个桶（按套餐）")
    for t in tenants:
        (sent, ok_a), (_, ok_b) = shared[t], isolated[t]
        print(f"  {pad(t, 10)}{pad(str(sent), 8)}{pad(f'放行 {ok_a:>3}（{ok_a / sent:>4.0%}）', 34)}放行 {ok_b:>3}（{ok_b / sent:>4.0%}）")
    while limiter.try_acquire("hooli"):
        pass
    print(f"\n  方案 B 下 hooli 的桶空了之后，429 响应头 Retry-After: {math.ceil(limiter.retry_after('hooli'))}"
          "（free 套餐每秒补 1 个令牌；HTTP 的 Retry-After 只接受整数秒，所以向上取整）")
    print(
        "\n观察：方案 A 里，hooli 一个租户就把全局桶抽干了，付费最多的 acme 也有一半请求被拒；"
        "\n      方案 B 里，hooli 只会把自己的桶抽干，其他租户完全不受影响。"
        "\n      生产中两层都要：每租户的桶保证公平，外层的全局桶保护上游模型的总配额。"
    )


# ---------------------------------------------------------------- 2. 模型路由


def catalog(impl) -> list:
    # 虚构的模型目录：名字和价格都是示例（美元 / 百万 token），不对应任何真实产品
    return [
        impl.ModelSpec("lite", context_window=32_000, supports_tools=False, strong=False, input_price=0.05, output_price=0.2),
        impl.ModelSpec("mini", context_window=128_000, supports_tools=True, strong=False, input_price=0.15, output_price=0.6),
        impl.ModelSpec("pro", context_window=200_000, supports_tools=True, strong=True, input_price=3.0, output_price=15.0),
        impl.ModelSpec("long", context_window=1_000_000, supports_tools=True, strong=True, input_price=5.0, output_price=20.0),
    ]


REQUESTS = [  # (说明, task, 每天次数)
    ("FAQ：年假有几天", {"input_tokens": 1_200, "output_tokens": 150, "complexity": "low"}, 5000),
    ("查我的请假余额", {"input_tokens": 2_500, "output_tokens": 200, "needs_tools": True, "complexity": "low"}, 3000),
    ("提交 3 天年假申请", {"input_tokens": 3_000, "output_tokens": 150, "needs_tools": True}, 1000),
    ("把制度翻译成英文", {"input_tokens": 800, "output_tokens": 800, "complexity": "low"}, 800),
    ("对比两版劳动合同的风险", {"input_tokens": 60_000, "output_tokens": 2_000, "complexity": "high"}, 50),
    ("分析上季度 20 个投诉的根因", {"input_tokens": 30_000, "output_tokens": 4_000, "needs_tools": True,
                             "complexity": "high"}, 20),
    ("总结 300 页的尽调报告", {"input_tokens": 400_000, "output_tokens": 3_000, "complexity": "high"}, 10),
    ("检索全公司 5 年的邮件归档", {"input_tokens": 3_000_000, "output_tokens": 2_000}, 2),
]


def demo_routing(impl) -> None:
    section("2. 模型路由：够用就好，别什么都上最强的模型", "模型网关 → 路由")
    models = catalog(impl)
    by_name = {m.name: m for m in models}
    print("模型目录（示例价格，美元 / 百万 token）：")
    for m in models:
        print(f"  {m.name:<5} 上下文 {m.context_window:>9,}  工具 {'✓' if m.supports_tools else '✗'}  "
              f"强模型 {'✓' if m.strong else '✗'}  输入 ${m.input_price:<5} 输出 ${m.output_price}")

    print(f"\n  {pad('请求', 28)}{pad('选中', 7)}{pad('每天次数', 10)}{pad('路由后/天', 12)}全用强模型/天")
    total_routed = total_strong = 0.0
    for label, task, per_day in REQUESTS:
        try:
            name = impl.choose_model(task, models)
        except impl.NoModelAvailable as e:
            print(f"  {pad(label, 28)}{pad('拒绝', 7)}{pad(str(per_day), 10)}→ {e}")
            continue
        strong = impl.choose_model({**task, "complexity": "high"}, models)  # 同样条件下最便宜的强模型
        cost = by_name[name].estimated_cost(task["input_tokens"], task.get("output_tokens", 0)) * per_day
        cost_strong = by_name[strong].estimated_cost(task["input_tokens"], task.get("output_tokens", 0)) * per_day
        total_routed += cost
        total_strong += cost_strong
        print(f"  {pad(label, 28)}{pad(name, 7)}{pad(str(per_day), 10)}{pad(f'${cost:,.2f}', 12)}${cost_strong:,.2f}")
    print(f"\n  合计：路由后每天 ${total_routed:,.2f}，全用强模型每天 ${total_strong:,.2f}，"
          f"节省 {1 - total_routed / total_strong:.0%}")
    print(
        "\n观察：绝大多数流量是简单请求，交给便宜模型；少数难题才用强模型。"
        "\n      最后一个请求超出了所有模型的上下文，路由器直接拒绝，而不是硬塞进去（那样会报错或被截断）。"
        "\n      注意：路由改变了'谁来回答'，每个被路由到的模型都要单独跑评估集（第 11 课）。"
    )


# ---------------------------------------------------------------- 3. 迷你网关


LEAVE_BALANCE = {("acme", "u-alice"): 7, ("globex", "u-bob"): 12, ("hooli", "u-carl"): 3}


@tool
def get_leave_balance(ctx: ToolContext) -> str:
    """查询当前员工的剩余年假天数。"""
    days = LEAVE_BALANCE.get((ctx.tenant_id, ctx.user_id))
    return f"剩余年假 {days} 天" if days is not None else "没有找到该员工的假期记录"


@tool
def search_policy(query: Annotated[str, Field(description="检索关键词")]) -> str:
    """检索本公司的 HR 制度。"""
    return "【年假】入职满 1 年 5 天，满 10 年 10 天，满 20 年 15 天；可分次使用，当年未休可顺延至次年 3 月底。"


HR_TOOLS = [get_leave_balance, search_policy]
HR_PROMPT = "你是企业 HR 助手。制度问题先用 search_policy 检索；个人数据用工具查询，不要编造。回答不超过 3 句话。"

GATEWAY_REQUESTS = [  # (租户, 用户, 问题, 路由用的任务画像)
    ("acme", "u-alice", "公司的年假制度是怎样的？", {"input_tokens": 600, "output_tokens": 100, "needs_tools": True, "complexity": "low"}),
    ("globex", "u-bob", "我还剩几天年假？", {"input_tokens": 600, "output_tokens": 80, "needs_tools": True, "complexity": "low"}),
    ("hooli", "u-carl", "我还剩几天年假？", {"input_tokens": 600, "output_tokens": 80, "needs_tools": True, "complexity": "low"}),
    ("hooli", "u-carl", "我还剩几天年假？？", {"input_tokens": 600, "output_tokens": 80, "needs_tools": True, "complexity": "low"}),
    ("hooli", "u-carl", "我还剩几天年假？？？", {"input_tokens": 600, "output_tokens": 80, "needs_tools": True, "complexity": "low"}),
    ("acme", "u-alice", "新员工入职满 1 年、中途调岗，年假怎么算？请分情况说明。",
     {"input_tokens": 800, "output_tokens": 400, "needs_tools": True, "complexity": "high"}),
]


def offline_gateway_script(tenant: str, question: str) -> list:
    if "剩几天" in question:
        days = {"globex": 12, "hooli": 3}.get(tenant, 7)
        return [call_tool("get_leave_balance", input_tokens=380, output_tokens=15),
                reply(f"你还剩 {days} 天年假。", 420, 12)]
    if "分情况" in question:
        return [call_tool("search_policy", query="年假 调岗", input_tokens=400, output_tokens=20),
                reply("入职满 1 年享 5 天年假；调岗不影响工龄累计，年假按总工龄计算；当年未休可顺延至次年 3 月底。", 520, 60)]
    return [call_tool("search_policy", query="年假", input_tokens=380, output_tokens=18),
            reply("入职满 1 年 5 天，满 10 年 10 天，满 20 年 15 天。", 470, 30)]


def demo_gateway(impl, offline: bool) -> None:
    section("3. 迷你网关端到端：身份 → 限流 → 路由 → 调模型 → 记账",
            "API 网关 → Agent 运行时 → 模型网关 → 可观测性 / 计费")
    models = catalog(impl)
    by_name = {m.name: m for m in models}
    # 为了在几个请求里就看到限流效果，这里 free 套餐设得很紧：突发 1 次，之后每分钟补 1 次
    plans = {"free": impl.Plan("free", 1, 1 / 60), "pro": impl.Plan("pro", 10, 1), "enterprise": impl.Plan("enterprise", 50, 5)}
    limiter = impl.TenantRateLimiter(plans, {"acme": "enterprise", "globex": "pro"}, default_plan="free")
    ledger: dict[str, dict] = {}
    if not offline:
        print("本机只配置了一个真实模型，所以 lite / mini / pro 这些逻辑模型都映射到它（.env 里的 LLM_MODEL）。"
              "\n生产中模型网关会把逻辑名映射到不同的真实模型；记账按逻辑模型的示例价目表计算。\n")

    for tenant, user, question, task in GATEWAY_REQUESTS:
        head = f"[{tenant}/{user}] {question[:14]}"
        # ① 身份：生产中来自网关验证过的 JWT，这里直接给定。绝不能让模型或客户端随便声明自己是谁。
        # ② 限流
        if not limiter.try_acquire(tenant):
            print(f"  {pad(head, 46)}→ 429 Too Many Requests（Retry-After: {math.ceil(limiter.retry_after(tenant))}）")
            ledger.setdefault(tenant, {"ok": 0, "rejected": 0, "tokens": 0, "cost": 0.0})["rejected"] += 1
            continue
        # ③ 路由
        model = impl.choose_model(task, models)
        # ④ 调用：逻辑模型 → 真实模型
        llm = ScriptedLLM(offline_gateway_script(tenant, question), model=model) if offline else default_llm()
        agent = Agent(llm, HR_TOOLS, system_prompt=HR_PROMPT, name="hr-assistant", max_steps=4)
        t0 = time.time()
        res = agent.run(question, metadata={"tenant_id": tenant, "user_id": user, "roles": ["employee"]})
        # ⑤ 记账：按租户归因（按逻辑模型的价目表）
        cost = by_name[model].estimated_cost(res.usage.input_tokens, res.usage.output_tokens)
        row = ledger.setdefault(tenant, {"ok": 0, "rejected": 0, "tokens": 0, "cost": 0.0})
        row["ok"] += 1
        row["tokens"] += res.usage.total
        row["cost"] += cost
        answer = " ".join((res.output or "").split())[:60]
        print(f"  {pad(head, 46)}→ {model:<5} {res.status} {time.time() - t0:>5.1f}s  {answer}")

    print(f"\n  {pad('租户', 10)}{pad('成功', 6)}{pad('被限流', 8)}{pad('tokens', 9)}成本（示例价格）")
    for tenant, row in ledger.items():
        print(f"  {pad(tenant, 10)}{pad(str(row['ok']), 6)}{pad(str(row['rejected']), 8)}{pad(str(row['tokens']), 9)}${row['cost']:.6f}")
    print(
        "\n观察：hooli（免费套餐）连发 3 次，只有第 1 次放行；同一个问题，不同租户查到的是各自员工的数据"
        "\n      （工具通过 ctx 拿到网关注入的 tenant_id / user_id，而不是让模型填）；每一分钱都能归到具体租户。"
    )


# ---------------------------------------------------------------- 4. 无状态 worker


@tool(risk="write")
def submit_leave(
    start_date: Annotated[str, Field(description="开始日期，YYYY-MM-DD")],
    days: Annotated[int, Field(ge=1, le=30, description="请假天数")],
    ctx: ToolContext,
) -> str:
    """提交年假申请（需要主管审批）。"""
    return f"已为 {ctx.tenant_id}/{ctx.user_id} 提交年假：{start_date} 起 {days} 天，审批单号 LV-{ctx.run_id[:6]}"


def make_worker(llm, checkpoint_dir: Path) -> Agent:
    """每个 worker 都是一个"全新"的 Agent 实例：不在内存里保留任何运行状态，状态全在检查点里。"""
    return Agent(
        llm,
        [get_leave_balance, submit_leave],
        system_prompt="你是企业 HR 助手。用户要请假时，直接用 submit_leave 提交申请。",
        name="hr-assistant",
        hooks=[PermissionPolicy(ask_risks={"write"})],  # 写操作需要主管审批
        checkpointer=FileCheckpointer(checkpoint_dir),
        max_steps=4,
    )


def demo_stateless_workers(offline: bool) -> None:
    section("4. 无状态 worker：A 暂停等审批，B 从检查点接手", "Agent 运行时 worker + 状态存储（检查点）")
    ckpt_dir = RUNS_DIR / "checkpoints"
    shutil.rmtree(ckpt_dir, ignore_errors=True)
    question = "帮我申请从 2026-10-08 开始的 3 天年假"
    meta = {"tenant_id": "acme", "user_id": "u-alice", "roles": ["employee"]}

    llm_a = ScriptedLLM([call_tool("submit_leave", start_date="2026-10-08", days=3)]) if offline else default_llm()
    worker_a = make_worker(llm_a, ckpt_dir)
    res = worker_a.run(question, metadata=meta)
    print(f"worker A：{question}")
    print(f"  状态 = {res.status}，run_id = {res.run_id}")
    if res.status != "paused":
        print(f"  （模型这次没有发起提交，而是回复了：{(res.output or '')[:80]}）"
              "\n  真实模型有时会先追问细节；想稳定看到暂停 / 恢复流程，请用 --offline 运行。")
        return
    ckpt = ckpt_dir / f"{res.run_id}.json"
    saved = json.loads(ckpt.read_text(encoding="utf-8"))
    print(f"  等待审批：{res.pending_approval.name}({res.pending_approval.arguments})")
    print(f"  检查点：{ckpt.relative_to(HERE.parent.parent)}（{ckpt.stat().st_size} 字节）")
    print(f"    status={saved['status']}  step={saved['step']}  pending={saved['pending']['name']}  "
          f"metadata={saved['metadata']}")
    del worker_a  # 模拟：worker A 所在的 Pod 被滚动发布替换掉了，内存里什么都没留下

    print("\n…… 3 小时后，主管在审批系统里点了「批准」，请求被负载均衡到了另一台机器上的 worker B ……\n")
    llm_b = ScriptedLLM([reply("已提交：2026-10-08 起 3 天年假，主管已批准，审批单号见系统通知。")]) if offline else default_llm()
    worker_b = make_worker(llm_b, ckpt_dir)
    done = worker_b.approve(res.run_id, approved=True, by="mgr-zhao", comment="同意，请做好工作交接")
    tool_msgs = [m["content"] for m in done.messages if m["role"] == "tool"]
    print(f"worker B：从检查点恢复 run_id = {done.run_id}")
    print(f"  工具执行结果：{tool_msgs[-1] if tool_msgs else '（无）'}")
    print(f"  最终状态 = {done.status}，回答：{' '.join((done.output or '').split())[:80]}")
    log = json.loads(ckpt.read_text(encoding="utf-8"))["approval_log"][-1]
    print(f"  审批记录（写在检查点里，供审计）：by={log['by']}  approved={log['approved']}  "
          f"comment={log['comment']}  tool={log['tool']}")
    print(
        "\n观察：worker B 从没见过这次运行，却能接着做完 —— 因为全部状态（消息历史、待审批的调用、"
        "\n      租户和用户身份）都在检查点里。worker 无状态，才能随意扩缩容、滚动发布、崩溃重启。"
        "\n      生产中检查点放 Postgres / Redis，而不是本地文件；两个 worker 同时抢同一个 run 时还要加锁。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="第 3、4 节用 ScriptedLLM 剧本代替真实模型")
    args = parser.parse_args()
    impl, impl_name = load_impl()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"（限流器和路由器的实现来自 {impl_name}）")
    demo_rate_limit(impl)
    demo_routing(impl)
    demo_gateway(impl, args.offline)
    demo_stateless_workers(args.offline)


if __name__ == "__main__":
    main()
