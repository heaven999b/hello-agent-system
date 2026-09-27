"""第 27 课 Demo：用 Temporal 持久化工作流运行 Agent。

    python lessons/27_durable_workflows/demo.py                  # 真实模型（gpt-5.5，经 agentkit.aio 的异步客户端）
    python lessons/27_durable_workflows/demo.py --offline        # 离线：AsyncScriptedLLM，不调模型
    python lessons/27_durable_workflows/demo.py --offline --only 4,6
    python lessons/27_durable_workflows/demo.py --offline --hold 300   # 跑完后保留服务器 300 秒，去 Web UI 看事件历史

WorkflowEnvironment.start_local() 拉起一个真正的 Temporal 开发服务器（Temporal CLI 的 start-dev，内存 SQLite），
不需要 Docker。第一次运行会下载 CLI（约 150 MB）并缓存。开发服务器 ≠ 生产集群：单进程、不持久化、没有高可用。

七个场景：
  1. 正常执行：LLM activity 与工具 activity 交替运行
  2. Activity 自动重试：库存服务第一次连接失败，RetryPolicy 1 秒后重试，模型只看到成功的结果
  3. 人工审批：dangerous 工具触发审批，workflow 挂起；稍后用 update 批准，workflow 继续
  4. 中途 kill -9 worker 进程：新的 worker 从断点继续，已完成的 activity 不重跑（打印事件历史作为证据）
  5. 审批超时：没人批 → 定时器触发 → 自动拒绝；迟到的审批被拒收
  6. 异步并发：一个 worker 同时推进 20 个 workflow，对比串行、限流、以及"在 async 里阻塞"的反模式
  7. 改了 workflow 代码之后重放旧历史：直接改会失败，用 workflow.patched 版本化就能通过

场景 6 在两种模式下都用带延迟的离线模型：并发打 20 个请求会超出本课对真实网关的并发约定（≤ 2）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL_HINT = 'pip install -e ".[prod,prod-local]"'
META = {"tenant_id": "acme", "user_id": "cs-agent-7", "roles": ["support"]}


def _load_scenario():
    from agentkit.testing import load_sibling

    return load_sibling(__file__, "scenario")


def _deps_ok() -> bool:
    try:
        import temporalio  # noqa: F401

        import agentkit.aio  # noqa: F401
        import agentkit.contrib.temporal  # noqa: F401
    except ImportError as e:
        print(f"缺少可选依赖（{e}）。请先安装：\n    {INSTALL_HINT}")
        return False
    return True


def _quiet_sdk_logs() -> None:
    """演示里会故意制造失败（工具异常、心跳超时、不兼容的重放），SDK 默认会把它们作为 WARN 连同堆栈打出来。
    这里只保留 ERROR，让输出聚焦在"发生了什么"上；排障时把这两行去掉即可看到完整日志。"""
    import logging

    from temporalio.runtime import LoggingConfig, Runtime, TelemetryConfig, TelemetryFilter

    logging.getLogger("temporalio").setLevel(logging.ERROR)
    quiet = TelemetryConfig(logging=LoggingConfig(filter=TelemetryFilter(core_level="ERROR", other_level="ERROR")))
    Runtime.set_default(Runtime(telemetry=quiet), error_if_already_set=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def banner(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


class CountingLLM:
    """包一层计数：看这个 worker 实际调了几次模型（真实模式下也能用）。"""

    def __init__(self, inner):
        self.inner = inner
        self.model = inner.model
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return await self.inner.chat(messages, tools, **kwargs)


# =============================================================================================
# 场景
# =============================================================================================


class Demo:
    def __init__(self, env, offline: bool, ui_url: str | None):
        from agentkit.contrib import temporal as kt

        self.kt = kt
        self.env = env
        self.client = env.client
        self.offline = offline
        self.ui_url = ui_url
        self.sc = _load_scenario()
        self.handles: dict[str, object] = {}

    def factory(self, holder: list | None = None):
        base = self.sc.offline_llm_factory() if self.offline else self.sc.real_llm_factory

        def make():
            llm = CountingLLM(base())
            if holder is not None:
                holder.append(llm)
            return llm

        return make

    async def start(self, text: str, task_queue: str, **fields):
        return await self.kt.start_agent(
            self.client, text, META, task_queue=task_queue, system_prompt=self.sc.SYSTEM_PROMPT, **fields
        )

    async def show_history(self, handle, only=("ActivityTaskStarted",), title="事件历史摘要"):
        lines = self.kt.summarize_history(await handle.fetch_history())
        print(f"  {title}（共 {len(lines)} 个事件，下面只列出 {', '.join(only)}）：")
        print(self.kt.format_history(lines, only=only))

    async def wait_status(self, handle, pred, timeout=120.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                st = await self.kt.agent_status(self.client, handle.id)
                if pred(st):
                    return st
            except Exception:  # noqa: BLE001 —— worker 还没接手时 query 会失败
                pass
            await asyncio.sleep(0.2)
        raise TimeoutError("等待 workflow 状态超时")

    # ------------------------------------------------------------------ 1
    async def s1_normal(self):
        banner("场景 1：正常执行 —— LLM activity 与工具 activity 交替运行")
        tq = f"demo-1-{uuid.uuid4().hex[:6]}"
        async with self.kt.make_worker(self.client, tq, self.factory(), self.sc.TOOLS, identity="worker-1"):
            h = await self.start("帮我查一下订单 A1001 现在是什么状态", tq, workflow_id=f"order-status-A1001-{tq}")
            r = await h.result()
            st = await self.kt.agent_status(self.client, h.id)
        self.handles["s1"] = h
        print(f"  输出：{r.output}")
        print(f"  状态 {r.status}，{r.steps} 步，工具 {r.tools_called}，token {r.usage.input_tokens}+{r.usage.output_tokens}")
        print(f"  事件历史：{st.history_length} 个事件，{st.history_size_bytes} 字节（每次 llm_step 的输入都带着完整对话）")
        seq = " → ".join(name for name, _ in self.kt.activity_attempts(await h.fetch_history()))
        print(f"  activity 顺序：{seq}")

    # ------------------------------------------------------------------ 2
    async def s2_retry(self):
        banner("场景 2：Activity 自动重试 —— 工具第一次失败、第二次成功")
        tq = f"demo-2-{uuid.uuid4().hex[:6]}"
        async with self.kt.make_worker(self.client, tq, self.factory(), self.sc.TOOLS, identity="worker-2"):
            h = await self.start("SKU-42 这个商品还有库存吗？请用 check_inventory 查一下", tq)
            r = await h.result()
        print(f"  输出：{r.output}")
        attempts = [(n, a) for n, a in self.kt.activity_attempts(await h.fetch_history()) if n == "execute_tool"]
        if any(a > 1 for _, a in attempts):
            print("  第 1 次：ConnectionError → registry 变成 error_type=exception → activity 抛 ToolTransientError")
            print("  RetryPolicy（read 工具：最多 5 次，初始间隔 1 秒）→ 第 2 次成功。模型只看到成功的结果：")
        else:
            print("  （模型这次没有调用 check_inventory，看不到重试。换 --offline 可以稳定复现。）")
        await self.show_history(h)

    # ------------------------------------------------------------------ 3
    async def s3_approval(self):
        banner("场景 3：人工审批 —— workflow 挂起等待，不占 worker 资源；update 批准后继续")
        tq = f"demo-3-{uuid.uuid4().hex[:6]}"
        before = dict(self.sc.LEDGER)
        async with self.kt.make_worker(self.client, tq, self.factory(), self.sc.TOOLS, identity="worker-3"):
            h = await self.start("订单 A1001 的耳机收到就坏了，申请全额退款", tq, workflow_id=f"refund-A1001-{tq}")
            try:
                st = await self.wait_status(h, lambda s: s.status == "waiting_approval" or s.status in ("completed", "failed"), 90)
            except TimeoutError:
                st = None
            if st is None or st.status != "waiting_approval":
                print("  （模型这次没有发起退款，没有触发审批。换 --offline 可以稳定复现。）")
                await h.result()
                return
            p = st.pending_approvals[0]
            print(f"  status() 查询：{st.status}，第 {st.step} 步，已调用 {st.tools_called}")
            print(f"  等待审批：{p['name']}({p['arguments']})，call_id={p['call_id']}，开始等待于 {p['since']}")
            print(f"  此时事件历史 {st.history_length} 个事件；审批定时器已启动（默认 24 小时，超时按拒绝处理）")
            print("  ……审批人 3 小时后才看到通知（这里缩短成 2 秒）……")
            await asyncio.sleep(2)
            res = await self.kt.approve(self.client, h.id, p["call_id"], True, by="zhang.manager", comment="核对无误", wait=True)
            dup = await self.kt.approve(self.client, h.id, p["call_id"], False, by="li.intern", wait=True)
            print(f"  update decide → {res!r}；另一个人又点了一次“拒绝” → {dup!r}（第一个决定生效）")
            r = await h.result()
        print(f"  输出：{r.output}")
        print(f"  审批记录：{r.approval_log}")
        new = {k: v for k, v in self.sc.LEDGER.items() if k not in before}
        print(f"  退款流水新增 {len(new)} 笔：{list(new)}（幂等键 = workflow_id:call_id）")
        await self.show_history(h, only=("TimerStarted", "TimerCanceled", "WorkflowExecutionUpdateAccepted", "ActivityTaskStarted"))

    # ------------------------------------------------------------------ 4
    async def s4_crash(self):
        banner("场景 4：kill -9 worker 进程 —— 新 worker 从断点继续，已完成的 activity 不重跑")
        tq = f"demo-4-{uuid.uuid4().hex[:6]}"
        target = self.client.service_client.config.target_host
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker-process", target, tq, "--identity", "worker-A"]
        if self.offline:
            cmd.append("--offline")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        try:
            line = await asyncio.to_thread(proc.stdout.readline)
            if "READY" not in line:
                print(f"  worker-A 子进程没有启动成功：{line!r}")
                return
            print(f"  worker-A 是一个独立的子进程（pid={proc.pid}），和本进程只通过 Temporal 服务器联系")
            h = await self.start("帮我生成订单 A1001 的对账报表", tq, heartbeat_timeout_s=2)
            killed = await self._kill_when_report_running(h, proc)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        holder: list = []
        async with self.kt.make_worker(self.client, tq, self.factory(holder), self.sc.TOOLS, identity="worker-B"):
            print("  worker-B 在本进程启动，接手同一个 task queue")
            r = await h.result()
        print(f"  输出：{r.output}")
        llm_b = holder[0].calls if holder else 0
        print(f"  worker-B 一共只调了 {llm_b} 次模型：前面几步的模型回复都从事件历史里重放得到，没有重新花钱")
        await self.show_history(h)
        if killed:
            print("  看 generate_report 那一行：attempt=2、由 worker-B 执行，上一次失败原因是心跳超时 ——")
            print("  服务端不会“知道”进程被 kill 了，它只是在 heartbeat_timeout（这里 2 秒）内没收到心跳。")
        done = self.kt.activity_attempts(await h.fetch_history())
        print(f"  已完成的 activity（名称, 最终尝试次数）：{done}")

    async def _kill_when_report_running(self, h, proc) -> bool:
        from temporalio.api.enums.v1 import PendingActivityState
        from temporalio.client import WorkflowExecutionStatus

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            desc = await h.describe()
            if desc.status != WorkflowExecutionStatus.RUNNING:
                print("  （模型这次没有调用 generate_report，workflow 已经结束。换 --offline 可以稳定复现。）")
                return False
            started = [
                p for p in desc.raw_description.pending_activities if p.state == PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
            ]
            st = await self.kt.agent_status(self.client, h.id)
            if started and st.tools_called and st.tools_called[-1] == "generate_report":
                os.kill(proc.pid, signal.SIGKILL)
                print(f"  generate_report 正在 worker-A 上执行（第 {st.step} 步，已调用 {st.tools_called}）→ kill -9 {proc.pid}")
                return True
            await asyncio.sleep(0.2)
        return False

    # ------------------------------------------------------------------ 5
    async def s5_timeout(self):
        banner("场景 5：审批超时 —— 没人批就自动拒绝（fail closed），迟到的审批被拒收")
        tq = f"demo-5-{uuid.uuid4().hex[:6]}"
        before = dict(self.sc.LEDGER)
        async with self.kt.make_worker(self.client, tq, self.factory(), self.sc.TOOLS, identity="worker-5"):
            h = await self.start("订单 A1002 的手机壳不要了，退款 20 元", tq, approval_timeout_s=3)
            print("  approval_timeout_s=3（生产里通常是几小时到几天）……")
            r = await h.result()
        print(f"  输出：{r.output}")
        print(f"  审批记录：{r.approval_log}")
        new = [k for k in self.sc.LEDGER if k not in before]
        print(f"  退款流水新增 {len(new)} 笔（超时 = 拒绝，退款没有执行）")
        if r.approval_log:
            try:
                await self.kt.approve(self.client, h.id, r.approval_log[0]["call_id"], True, by="zhang.manager")
                print("  迟到的审批 signal 居然发出去了？")
            except Exception as e:  # noqa: BLE001
                print(f"  workflow 结束后才到的审批：服务端直接拒收（{type(e).__name__}）")
        await self.show_history(h, only=("TimerStarted", "TimerFired", "ActivityTaskStarted"))

    # ------------------------------------------------------------------ 6
    async def s6_concurrency(self):
        banner("场景 6：异步并发 —— 一个 worker 同时推进 20 个 workflow（模型延迟 0.15 秒，离线模型）")
        from agentkit.aio import AsyncScriptedLLM

        n, latency = 20, 0.15
        sc = self.sc

        def blocking_brain(messages):
            time.sleep(latency)  # 反模式：在 async activity 里做阻塞 IO（requests、同步数据库驱动……）
            return sc.offline_brain(messages)

        async def batch(concurrent: bool, blocking: bool = False, **worker_kwargs):
            holder: list = []

            def factory():
                llm = AsyncScriptedLLM(responder=blocking_brain if blocking else sc.offline_brain, latency=0 if blocking else latency)
                holder.append(llm)
                return llm

            tq = f"demo-6-{uuid.uuid4().hex[:6]}"
            async with self.kt.make_worker(self.client, tq, factory, sc.TOOLS, **worker_kwargs):
                t0 = time.perf_counter()
                if concurrent:
                    hs = await asyncio.gather(*(self.start(f"查一下订单 A1001（第 {i} 位客户）", tq) for i in range(n)))
                    await asyncio.gather(*(h.result() for h in hs))
                else:
                    for i in range(n):
                        await (await self.start(f"查一下订单 A1001（第 {i} 位客户）", tq)).result()
                return time.perf_counter() - t0, holder[0].max_in_flight

        rows = [
            ("串行：一个接一个", await batch(False)),
            ("并发：max_concurrent_activities=100（默认）", await batch(True)),
            ("并发：max_concurrent_activities=4", await batch(True, max_concurrent_activities=4)),
            ("并发，但模型客户端在 async 里阻塞（time.sleep）", await batch(True, blocking=True)),
        ]
        print(f"  每个 workflow 调 2 次模型；纯模型等待时间：串行下限 {n * 2 * latency:.1f} 秒\n")
        print(f"  {'方式':<44}{'总耗时':>8}{'模型调用峰值并发':>18}")
        for name, (elapsed, peak) in rows:
            print(f"  {name:<44}{elapsed:>7.2f}s{peak:>14}")
        print("\n  · 等模型时 async activity 不占线程，事件循环去推进别的 workflow → 20 个一起等")
        print("  · max_concurrent_activities 是这个 worker 同时执行 activity 的上限，常用来对齐模型网关的并发配额")
        print("  · 在 async 里阻塞：事件循环被卡住，并发退化成 1，和串行一样慢；心跳也发不出去（长了会被判超时重试）")

    # ------------------------------------------------------------------ 7
    async def s7_replay(self):
        banner("场景 7：改了 workflow 代码之后，旧的执行还能重放吗？")
        from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

        h = self.handles.get("s1")
        if h is None:
            print("  （需要先运行场景 1）")
            return
        history = await h.fetch_history()
        await Replayer(workflows=[self.kt.AgentWorkflow], workflow_runner=self.kt.sandbox_runner()).replay_workflow(history)
        print("  原版代码重放场景 1 的历史：通过（确定性：同样的历史 → 同样的命令序列）")
        try:
            await Replayer(workflows=[self.sc.ChangedAgentWorkflow], workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(
                history
            )
            print("  直接改代码（开始前加 workflow.sleep）：居然通过了？")
        except Exception as e:  # noqa: BLE001
            print(f"  直接改代码（开始前加 workflow.sleep）：重放失败 → {type(e).__name__}: {str(e)[:110]}")
        await Replayer(workflows=[self.sc.PatchedAgentWorkflow], workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(history)
        print('  用 if workflow.patched("throttle-before-start") 包起来：重放通过（旧执行走旧路径，新执行走新路径）')
        print("  发版前把生产里抽样的历史拿来跑 Replayer，就是 workflow 代码的“兼容性测试”。")


# =============================================================================================
# 入口
# =============================================================================================


async def worker_process(target: str, task_queue: str, identity: str, offline: bool) -> None:
    """场景 4 用：在独立进程里跑一个 worker，打印 READY 后一直运行，直到被 kill。"""
    from temporalio.client import Client

    from agentkit.contrib import temporal as kt

    _quiet_sdk_logs()
    sc = _load_scenario()
    client = await Client.connect(target)
    factory = sc.offline_llm_factory() if offline else sc.real_llm_factory
    async with kt.make_worker(client, task_queue, factory, sc.TOOLS, identity=identity):
        print("READY", flush=True)
        await asyncio.Event().wait()


async def main(args) -> None:
    from temporalio.testing import WorkflowEnvironment

    _quiet_sdk_logs()
    only = {int(x) for x in args.only.split(",")} if args.only else set(range(1, 8))
    ui_port = _free_port()
    try:
        env = await WorkflowEnvironment.start_local(ui=True, ui_port=ui_port)
    except Exception as e:  # noqa: BLE001 —— 比如第一次运行需要下载 CLI，但没有网络
        print(f"无法启动 Temporal 开发服务器：{type(e).__name__}: {e}")
        print("首次运行需要联网下载 Temporal CLI；也可以自己装好 CLI 后运行 `temporal server start-dev`。")
        return
    async with env:
        ui_url = f"http://127.0.0.1:{ui_port}"
        try:
            urllib.request.urlopen(ui_url, timeout=3)
            print(f"Temporal Web UI：{ui_url}  （Namespace default 里能看到下面每个 workflow 的事件历史）")
        except Exception:  # noqa: BLE001
            ui_url = None
            print("Temporal Web UI 没有响应（开发服务器的 UI 可能被禁用），不影响演示。")
        print(f"gRPC 地址：{env.client.service_client.config.target_host}；模式：{'离线（AsyncScriptedLLM）' if args.offline else '真实模型'}")
        demo = Demo(env, args.offline, ui_url)
        steps = [demo.s1_normal, demo.s2_retry, demo.s3_approval, demo.s4_crash, demo.s5_timeout, demo.s6_concurrency, demo.s7_replay]
        for i, step in enumerate(steps, 1):
            if i in only:
                await step()
        if args.hold:
            print(f"\n保留开发服务器 {args.hold} 秒，可以在 {ui_url} 里查看事件历史（Ctrl-C 结束）……")
            await asyncio.sleep(args.hold)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用 AsyncScriptedLLM，不调用真实模型")
    parser.add_argument("--only", default="", help="只运行这些场景，例如 1,3,4")
    parser.add_argument("--hold", type=int, default=0, help="跑完后保留开发服务器 N 秒，方便看 Web UI")
    parser.add_argument("--worker-process", nargs=2, metavar=("TARGET", "TASK_QUEUE"), help=argparse.SUPPRESS)
    parser.add_argument("--identity", default="worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not _deps_ok():
        sys.exit(0)
    if args.worker_process:
        asyncio.run(worker_process(*args.worker_process, args.identity, args.offline))
    else:
        try:
            asyncio.run(main(args))
        except KeyboardInterrupt:
            pass
