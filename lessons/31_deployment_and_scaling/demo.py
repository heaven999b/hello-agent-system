"""第 31 课 Demo：把参考服务（production/）整套拉起来，压一轮、中途 kill -9 一个 worker、再滚动重启一个，然后逐项核对。

    python lessons/31_deployment_and_scaling/demo.py --offline     # 离线：带延迟的剧本模型，约 30 秒
    python lessons/31_deployment_and_scaling/demo.py               # 真实模型：走 .env 里的本地网关，模型并发 ≤ 2，约 15–25 次调用

不需要 Docker：run_local.LocalStack 起嵌入式 Postgres（pgserver）+ fakeredis TCP 服务 + 1 个 uvicorn API 进程 + 3 个 worker 进程，
压测和故障注入用 production/loadtest.py。缺少可选依赖时打印安装命令、以退出码 0 结束。

离线模式输出：
  1. 压测结果：吞吐、各类请求的 p50/p95/p99、首 token 延迟、后台任务的排队时间、错误率、429 比例；
  2. 利特尔法则：worker 的并发名额 L、实测平均服务时间 W → 饱和吞吐上限 L/W，和实测吞吐对比；
  3. 故障注入时间线：kill -9（租约过期后被接手）、滚动重启（排空 → 取消 → 归还 → 接手）；
  4. 验证：全部运行到达终态、没有重复副作用、断开的交互式运行记为 cancelled、指标与数据库一致。
真实模式跑一个小场景（2 个 worker × 并发 1）：4 个后台任务 + kill -9 + 2 个交互式请求，同样逐项核对。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def banner(text: str) -> None:
    print(f"\n{'=' * 8} {text} {'=' * 8}", flush=True)


# ----------------------------------------------------------------------------------- 离线


def offline(users: int, duration: float) -> int:
    from production.loadtest import print_report, run_load
    from production.run_local import LocalStack

    banner("启动参考服务（嵌入式 Postgres + fakeredis + API + 3 个 worker）")
    stack = LocalStack(workers=3, concurrency=4, latency_ms=200)
    with stack:
        print(f"就绪：{stack.startup_seconds:.1f}s，API {stack.api_url}，日志 {stack.run_dir / 'logs'}", flush=True)
        banner(f"压测 {users} 个用户 × {duration:.0f}s；35% 处 kill -9 一个 worker，65% 处滚动重启一个")
        report = asyncio.run(run_load(stack, users=users, duration=duration, faults=True))
    print_report(report)
    banner("怎么读")
    s = report["summary"]["by_kind"]
    diag = s.get("bg_diagnose", {})
    print(f"- bg_diagnose 的 p50 {diag.get('p50_s')}s、p99 {diag.get('p99_s')}s：p99 里是被 kill -9 的那几个任务 ——"
          f"它们要等租约（{report['config']['lease_s']}s）过期才被接手。租约越短恢复越快，但越容易把“卡了一下”误判成“死了”。")
    print("- 滚动重启那一行的 cancelled：宽限期内做不完的任务被取消、立刻归还，别的 worker 用同一个 call_id 重放，"
          "“没有重复副作用”一项里的 replays_deduplicated 就是被数据库唯一约束挡住的重放。")
    print("- 429 全部来自“吵闹租户”（配额 1 个/秒）；其他租户的请求不受影响，这就是按租户限流的意义。")
    return 0 if report["verification"]["all_ok"] else 1


# ----------------------------------------------------------------------------------- 真实模型


async def real_scenario(stack) -> dict:
    from production.loadtest import ApiClient, FaultLog, Load, parse_metrics, verify, wait_drained

    client = ApiClient(stack.api_url, stack.keys, timeout=180)
    load = Load(client)
    faults = FaultLog()
    t0 = time.monotonic()
    rows = []
    try:
        messages = ["三楼打印机一直卡纸，帮我提个工单", "VPN 连不上，提示错误 809，怎么办？",
                    "显示器有一条竖线，帮我报修", "打印机找不到了怎么办？"]
        run_ids = []
        for m in messages:
            r = await client.submit("acme-alice", m, idem=uuid.uuid4().hex)
            run_ids.append(r.json()["run_id"])
            load.accepted_runs.add(run_ids[-1])

        # 等第一个任务被领取，就 kill -9 它所在的 worker
        first = await client.follow("acme-alice", run_ids[0], until={"claimed"}, timeout=60)
        victim_name = [e for e in first if e["event"] == "claimed"][-1]["data"]["worker"]
        victim = stack.worker_by_name(victim_name)
        await asyncio.sleep(1.0)  # 让它先跑一会儿（模型调用进行中）
        stack.kill_worker(victim)
        faults.add(t0, "kill -9", worker=victim_name)
        replacement = stack.start_worker()
        await asyncio.to_thread(stack.wait_ready, 60, [replacement])
        faults.add(t0, "replacement ready", worker=replacement.name)

        for run_id, m in zip(run_ids, messages):
            t = time.perf_counter()
            events = await client.follow("acme-alice", run_id, until={"completed", "failed"}, timeout=240)
            claims = [e["data"]["worker"] for e in events if e["event"] == "claimed"]
            state = await client.get_run("acme-alice", run_id)
            rows.append({"message": m, "status": state["status"], "workers": claims, "attempts": state["job"]["attempts"],
                         "output": " ".join((state["output"] or "").split())[:60], "waited_s": round(time.perf_counter() - t, 1)})
        await wait_drained(stack, timeout=120)

        # 交互式：一次完整的流式对话 + 一次中途断开
        s = await load.interactive("acme-alice", 1, disconnect=False)
        load.samples.append(s)
        d = await load.interactive("acme-alice", 2, disconnect=True)
        load.samples.append(d)
        await asyncio.sleep(1.0)
        metrics = parse_metrics(await client.metrics())
    finally:
        await client.aclose()
    stack.retire_if_exited()
    return {"background": rows, "interactive": s, "disconnect": d, "faults": faults.events,
            "verification": verify(stack, load, metrics, [])}


def real(workers: int) -> int:
    from production.run_local import LocalStack

    banner(f"启动参考服务（真实模型，经 LiteLLM Router；{workers} 个 worker × 并发 1，每个进程模型并发 ≤ 1）")
    stack = LocalStack(workers=workers, concurrency=1, llm="litellm",
                       env={"LLM_MAX_CONCURRENCY": "1", "WORKER_LEASE_SECONDS": "15", "WORKER_HEARTBEAT_SECONDS": "5",
                            "RUN_TIMEOUT_SECONDS": "120", "TOOL_LATENCY_MS": "200", "DIAGNOSTICS_LATENCY_MS": "500"})
    with stack:
        print(f"就绪：{stack.startup_seconds:.1f}s，日志 {stack.run_dir / 'logs'}", flush=True)
        banner("4 个后台任务；第一个任务开始后 1 秒 kill -9 它所在的 worker（租约 15s）")
        result = asyncio.run(real_scenario(stack))
    for r in result["background"]:
        print(f"  [{r['status']}] {r['message']}  worker={r['workers']} attempts={r['attempts']} 等待 {r['waited_s']}s → {r['output']}")
    s, d = result["interactive"], result["disconnect"]
    print(f"  交互式：{'成功' if s.ok else '失败'}，总耗时 {s.latency_s:.2f}s，首个文本片段 {s.ttft_s and round(s.ttft_s, 2)}s")
    print(f"  交互式中途断开：run {d.run_id}（读到第一个工具结果就关连接）")
    print("故障注入：" + json.dumps(result["faults"], ensure_ascii=False))
    print("验证：")
    for c in result["verification"]["checks"]:
        detail = {k: v for k, v in c.items() if k not in ("check", "ok")}
        print(f"  {'✅' if c['ok'] else '❌'} {c['check']}  " + json.dumps(detail, ensure_ascii=False, default=str)[:500])
    return 0 if result["verification"]["all_ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="用带延迟的剧本模型，不调用真实模型")
    parser.add_argument("--users", type=int, default=12)
    parser.add_argument("--duration", type=float, default=20)
    args = parser.parse_args()

    from production.run_local import INSTALL_HINT, missing_dependencies

    missing = missing_dependencies()
    if missing:
        print(f"本 Demo 需要可选依赖：{', '.join(missing)} 未安装。\n请运行：{INSTALL_HINT}")
        return 0
    if not args.offline:
        from agentkit.config import env

        if not env("LLM_API_KEY"):
            print("没有配置 LLM_API_KEY（.env），改用 --offline 模式运行。")
            args.offline = True
    try:
        return offline(args.users, args.duration) if args.offline else real(workers=2)
    except (OSError, RuntimeError, TimeoutError) as e:
        print(f"本机基础设施启动或运行失败：{type(e).__name__}: {e}\n请确认已安装：{INSTALL_HINT}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
