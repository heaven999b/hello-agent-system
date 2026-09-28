"""ITBuddy 命令行应用：python capstone/app.py

    选择登录身份 → 多轮对话 → 遇到高危操作时暂停，由"审批人"决定 → 恢复运行

单进程、单用户：命令行里申请人和审批人是同一个终端，所以审批直接在终端里问一句 y/n。
等待输入用 terminal_input()：input() 是阻塞调用，放进一个守护线程里等，事件循环不会被卡住。
多人、多进程、审批相隔几小时的版本是 HTTP 服务（deploy.py：API 进程 + worker 进程）。

命令：
    /whoami   当前身份、角色和可用工具
    /trace    最近一轮的链路追踪树
    /cost     本会话的 token 与成本
    /audit    本租户最近 8 条审计记录（审计表在 runs/itbuddy.db，所有进程共享）
    /switch   切换用户（会清空对话历史）
    /help     帮助
    /exit     退出

非交互冒烟测试（管道输入）：
    printf '1\\n公司 VPN 怎么连？\\n/cost\\n/exit\\n' | .venv/bin/python capstone/app.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Union

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 让 `from itbuddy import ...` 在仓库根目录直接可用

from agentkit import RunResult, maybe_await, render_tree  # noqa: E402
from agentkit.tracing import Span  # noqa: E402
from agentkit.types import Message  # noqa: E402
from itbuddy import ITBuddyAgent, build_agent, next_history, visible_tools_for  # noqa: E402

# 演示用的登录身份。真实系统里身份来自 SSO 登录，这里用"选编号"模拟。
# 注意：角色不在这里写死，而是登录后从员工目录（backend.identity）里查 —— 身份和权限的唯一可信来源是目录。
LOGINS = [("acme", "alice"), ("acme", "bob"), ("globex", "carol")]

HELP = "命令：/whoami  /trace  /cost  /audit  /switch  /help  /exit"

Ask = Callable[[str], Union[str, Awaitable[str]]]  # 同步或 async 都行（测试里传一个普通函数）


async def terminal_input(prompt: str) -> str:
    """input() 会阻塞：放进一个守护线程里等，事件循环照常运转（比如后台的检查点保存）。

    为什么不用 asyncio.to_thread(input, prompt)？实测：在等待输入时按 Ctrl-C，asyncio.run 退出前会等
    默认线程池里的线程结束，而那个线程还卡在 input() 上 —— 程序挂住，直到你再按一次回车。
    守护线程不会拖住进程退出。"""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[str] = loop.create_future()

    def deliver(fn, value) -> None:
        if not done.done():
            fn(value)

    def read() -> None:
        try:
            line = input(prompt)
        except BaseException as e:  # noqa: BLE001 —— EOFError（管道输入结束）等要交回事件循环
            loop.call_soon_threadsafe(deliver, done.set_exception, e)
        else:
            loop.call_soon_threadsafe(deliver, done.set_result, line)

    threading.Thread(target=read, name="itbuddy-input", daemon=True).start()
    return await done


@dataclass
class Session:
    identity: dict
    display_name: str
    history: list[Message] = field(default_factory=list)
    costs: dict[str, float] = field(default_factory=dict)  # run_id -> 该 run 的累计成本
    tokens: dict[str, int] = field(default_factory=dict)
    cached: dict[str, int] = field(default_factory=dict)  # 命中提示词缓存的输入 token（网关不支持时为 0）
    turns: int = 0
    last_traces: list[Span] = field(default_factory=list)


async def login(agent: ITBuddyAgent, ask: Ask, say: Callable[[str], None]) -> Session | None:
    backend = agent.backend
    say("\n请选择登录身份：")
    for i, (tenant, uid) in enumerate(LOGINS, 1):
        e = await backend.get_employee(tenant, uid)
        say(f"  {i}) {tenant} / {uid:<6} {e.name}，{e.department} {e.title}")
    while True:
        choice = (await maybe_await(ask("编号> "))).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(LOGINS):
            tenant, uid = LOGINS[int(choice) - 1]
            identity = await backend.identity(tenant, uid)
            e = await backend.get_employee(tenant, uid)
            say(f"已登录：{e.name} @ {tenant}（角色 {identity['roles']}）。{HELP}")
            return Session(identity=identity, display_name=f"{uid}@{tenant}")
        say(f"请输入 1-{len(LOGINS)} 之间的编号。")


async def handle_approvals(agent: ITBuddyAgent, session: Session, result: RunResult, ask: Ask, say) -> RunResult:
    """模拟审批人。真实系统里这一步是异步的：推送到审批系统 / IM，审批人可能几小时后才处理，
    恢复运行的可能是另一个 worker 进程（见 server.py、deploy.py）。命令行里为了演示，直接在终端问一句 y/n。"""
    audit = agent.audit
    while result.status == "paused" and result.pending_approval is not None:
        call = result.pending_approval
        try:
            args = json.dumps(json.loads(call.arguments), ensure_ascii=False)
        except json.JSONDecodeError:
            args = call.arguments
        user_msgs = [m["content"] for m in result.messages if m.get("role") == "user"]
        say("\n" + "=" * 60)
        say("[审批请求] 需要 IT 值班工程师审批的高危操作")
        say(f"  申请人：{session.display_name}")
        say(f"  用户原话：{user_msgs[-1] if user_msgs else '-'}")  # 给审批人看上下文，而不只是一个函数名
        say(f"  操作：{call.name}  参数：{args}")
        say("=" * 60)
        answer = (await maybe_await(ask("[审批人] 是否批准？(y/n)> "))).strip().lower()
        approved = answer in ("y", "yes", "是")
        approver, comment = "cli-approver", "命令行模拟审批"
        if audit:  # 审批决定在恢复执行之前先落审计：即使恢复过程崩溃，"谁在何时批了什么"也有据可查
            await audit.record("approval_decision", run_id=result.run_id, call_id=call.id,
                               tenant_id=session.identity["tenant_id"], approver=approver, approved=approved,
                               tool=call.name, comment=comment)
        say(f"[审批] {'已批准' if approved else '已拒绝'}，继续运行……")
        # by / comment 会写进检查点里的 state.approval_log，工具执行时的审计记录也会带上 approved_by
        result = await agent.approve(result.run_id, approved, by=approver, comment=comment)
        track(session, result)
    return result


def track(session: Session, result: RunResult) -> None:
    # 同一个 run 暂停后恢复，RunResult.cost_usd 是"这个 run 的累计值"，不是增量。
    # 所以按 run_id 记最新值再求和；直接累加每次返回的 cost 会重复计算。
    session.costs[result.run_id] = result.cost_usd
    session.tokens[result.run_id] = result.usage.total
    session.cached[result.run_id] = result.usage.cached_input_tokens
    if result.trace is not None:
        session.last_traces.append(result.trace)


def show_turn(result: RunResult, say) -> None:
    say(f"\nITBuddy> {result.output}")
    tools = result.tools_called()  # 只含本次运行请求过的工具（包括被拒绝的），不含历史消息里的调用
    extra = f"  [停止原因] {result.stop_reason}" if result.status != "completed" else ""
    say(f"  [工具] {' → '.join(tools) if tools else '无'}  [状态] {result.status}  [步数] {result.steps}"
        f"  [tokens] {result.usage.total}  [成本] ${result.cost_usd:.5f}{extra}")


async def amain(argv: list[str] | None = None, *, llm=None, ask: Ask | None = None,
                say: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="ITBuddy —— 企业 IT 服务台 Agent")
    parser.add_argument("--runs-dir", default=None, help="检查点 / 审计（itbuddy.db）与追踪的目录，默认 capstone/runs")
    args = parser.parse_args(argv)

    try:
        # 企业后端：每次启动一份全新的种子数据（内存 SQLite）；检查点和审计写进 runs/itbuddy.db
        agent = await build_agent(llm, runs_dir=args.runs_dir)
    except RuntimeError as e:  # 典型原因：没有配置 .env
        say(f"启动失败：{e}")
        return 1
    if ask is None:
        ask = terminal_input
        if not sys.stdin.isatty():
            # 管道输入（非交互冒烟测试）时回显输入内容，否则输出里只看得到提示符、看不到"用户说了什么"
            async def ask(prompt: str) -> str:
                line = await terminal_input(prompt)
                say(line)
                return line

    say("ITBuddy —— 企业 IT 服务台 Agent（hello-agent-system 毕业项目）")
    say(f"检查点与审计：{agent.stores.db.path}")

    try:
        session = await login(agent, ask, say)
        while True:
            text = (await maybe_await(ask(f"\n{session.display_name}> "))).strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/help":
                say(HELP)
                continue
            if text == "/whoami":
                e = await agent.backend.get_employee(session.identity["tenant_id"], session.identity["user_id"])
                say(f"{e.name}  租户={session.identity['tenant_id']}  角色={session.identity['roles']}")
                say(f"可用工具：{', '.join(visible_tools_for(agent, session.identity))}")
                continue
            if text == "/trace":
                if not session.last_traces:
                    say("还没有追踪记录。")
                for span in session.last_traces:
                    say(render_tree(span))
                continue
            if text == "/cost":
                total_cost, total_tokens = sum(session.costs.values()), sum(session.tokens.values())
                say(f"本会话：{session.turns} 轮对话，{len(session.costs)} 次运行，"
                    f"{total_tokens} tokens（其中输入命中提示词缓存 {sum(session.cached.values())}），"
                    f"估算成本 ${total_cost:.5f}（单价见 agentkit/pricing.py，为示例值）")
                events = getattr(agent.llm, "events", [])
                if events:
                    say(f"模型重试 / 降级事件 {len(events)} 次，最近一次：{events[-1]}")
                continue
            if text == "/audit":
                records = await agent.stores.audit.records(tenant_id=session.identity["tenant_id"], limit=8)
                for r in records:
                    extra = {k: r[k] for k in ("tool", "status", "approved", "approved_by", "approver") if r.get(k) is not None}
                    say(f"  #{r['id']} {r['event']:<18} run={r.get('run_id')}  {extra}")
                if not records:
                    say("还没有审计记录。")
                continue
            if text == "/switch":
                # 切换用户必须清空历史：否则上一个人的对话（可能含工单、个人信息）会被带进下一个人的上下文
                session = await login(agent, ask, say)
                continue
            if text.startswith("/"):
                say(f"未知命令。{HELP}")
                continue

            session.turns += 1
            session.last_traces = []
            result = await agent.run(text, history=session.history, metadata=session.identity)
            track(session, result)
            result = await handle_approvals(agent, session, result, ask, say)
            show_turn(result, say)
            session.history = next_history(session.history, result)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        await agent.aclose()
    say("\n再见！")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:  # 等待输入时按 Ctrl-C：asyncio.run 取消主任务（finally 里照样关闭数据库），再抛出它
        print("\n再见！")
        return 0


if __name__ == "__main__":
    sys.exit(main())
