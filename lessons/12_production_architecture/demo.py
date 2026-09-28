"""第 12 课 Demo：给 Agent 套上"生产外壳" —— 在一台机器上用真实的进程搭一个迷你部署。

场景：一家 HR SaaS 公司，同一套 HR 助手服务多家企业客户（租户），套餐不同。

    0. 模型路由算账（纯计算，不涉及并发）：够用就好，和"全部用最强模型"比一比每天的账单
    1. 起一个迷你部署：4 个 API 进程（uvicorn）+ 2 个 worker 进程（WorkerPool），共享一个 SQLite 文件
    2. 长任务 vs HTTP 超时：同步接口被客户端 2 秒超时掐断（服务端照样跑完、白跑）；异步接口 202 + 轮询
    3. 无状态 worker：kill -9 正在跑长任务的 worker，另一个 worker 从检查点接着跑完同一个 run
    4. 跨进程限流：两个 API 进程各用各的内存桶 vs 共用一个 SQLiteTokenBucket，实测放行数
    5. 按租户的舱壁：进程内 KeyedLimiter + 跨进程 SQLiteSemaphore，吵闹的租户挤不垮别人；最后按租户记账

运行（需要 pip install -e ".[server]"：FastAPI、uvicorn、httpx）：
    python lessons/12_production_architecture/demo.py --offline   # 剧本模型，约 15 秒
    python lessons/12_production_architecture/demo.py             # worker 和同步接口调用真实模型（.env），约 60 次模型调用

运行产物（SQLite 文件、各进程日志）在 lessons/12_production_architecture/runs/mini/ 下。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
sys.path.insert(0, str(HERE))

import deployment  # noqa: E402  同目录模块

KEYS = {"acme": "key-acme-alice", "globex": "key-globex-bob", "hooli": "key-hooli-carl"}
LONG_TASK = "帮我汇总部门上季度的请假情况并生成报告"


def section(title: str, component: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")
    print(f"对应参考架构中的：{component}\n")


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def load_impl():
    """优先用你在 exercise.py 里的实现；还没写完就用参考答案。返回 (模块, 文件路径, 说明)。"""
    import exercise  # noqa: E402

    try:
        exercise.TokenBucket(1, 1).try_acquire()
        exercise.TenantRateLimiter({"f": exercise.Plan("f", 1, 1)}, {}, "f").try_acquire("t")
        exercise.choose_model({"input_tokens": 1}, [exercise.ModelSpec("m", 10, True, True, 1, 1)])
        return exercise, HERE / "exercise.py", "exercise.py（你的实现）"
    except NotImplementedError:
        import solution  # noqa: E402

        return solution, HERE / "solution.py", "solution.py（参考答案 —— 完成练习后会自动换成你的实现）"


# ---------------------------------------------------------------- 0. 模型路由算账（纯计算）


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
    section("0. 模型路由算账：够用就好，别什么都上最强的模型（纯计算，不涉及并发）", "模型网关 → 路由")
    models = catalog(impl)
    by_name = {m.name: m for m in models}
    print(f"  {pad('请求', 28)}{pad('选中', 7)}{pad('每天次数', 10)}{pad('路由后/天', 12)}全用强模型/天")
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
    print("\n观察：绝大多数流量是简单请求，交给便宜模型；最后一个请求超出了所有模型的上下文，路由器直接拒绝。"
          "\n      下面的迷你部署里，API 进程对每个请求调用同一个 choose_model，把选中的逻辑模型写进任务。")


# ---------------------------------------------------------------- 迷你部署里的客户端


class Client:
    """模拟调用方：用真实的 HTTP 请求打到各个 API 进程（没有负载均衡器，由客户端轮流发）。"""

    def __init__(self, http, dep):
        self.http, self.dep = http, dep

    def url(self, api: str, path: str) -> str:
        return f"{self.dep.apis[api].url}{path}"

    async def submit(self, api: str, tenant: str, message: str, complexity: str = "medium", **kw):
        return await self.http.post(self.url(api, "/runs"), json={"message": message, "complexity": complexity},
                                    headers={"Authorization": f"Bearer {KEYS[tenant]}"}, **kw)

    async def get(self, api: str, tenant: str, run_id: str) -> dict:
        r = await self.http.get(self.url(api, f"/runs/{run_id}"), headers={"Authorization": f"Bearer {KEYS[tenant]}"})
        r.raise_for_status()
        return r.json()

    async def wait_done(self, tenant: str, run_id: str, apis: list[str], timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        i = 0
        while time.monotonic() < deadline:
            info = await self.get(apis[i % len(apis)], tenant, run_id)
            i += 1
            if info["job_status"] in ("succeeded", "failed", "dead"):
                return info
            await asyncio.sleep(0.1)
        raise TimeoutError(f"{run_id} 在 {timeout}s 内没有完成")


def peak(intervals: list[tuple[float, float]]) -> int:
    points = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
    level = best = 0
    for _, d in points:
        level += d
        best = max(best, level)
    return best


# ---------------------------------------------------------------- 2 + 3. 长任务、HTTP 超时、无状态 worker


async def demo_long_task_and_stateless(client: Client, dep) -> str:
    import httpx

    section("2. 长任务 vs HTTP 超时：同步接口 vs 202 + 轮询", "API 网关 → 任务队列 → Agent 运行时 worker")
    print(f"请求：{LONG_TASK}（HR 助手要调 3 次模型，离线剧本里每次 1.2~1.5 秒）")
    print("客户端（相当于浏览器前面的网关）的超时设为 2 秒。\n")
    started = time.monotonic()
    gave_up_at = None
    try:
        await client.http.post(client.url("api-1", "/runs/sync"), json={"message": LONG_TASK, "complexity": "high"},
                               headers={"Authorization": f"Bearer {KEYS['acme']}", "X-Request-Id": "demo-1"}, timeout=2.0)
        print("  同步接口：居然在 2 秒内返回了（真实模型这次很快）")
    except httpx.ReadTimeout:
        gave_up_at = time.time()
        print(f"  ❌ POST /runs/sync → {time.monotonic() - started:.1f} 秒后客户端超时（httpx.ReadTimeout），用户只看到失败")

    started = time.monotonic()
    r = await client.submit("api-1", "acme", LONG_TASK, "high", timeout=5.0)
    body = r.json()
    run_id = body["run_id"]
    print(f"  ✅ POST /runs → {r.status_code}，{(time.monotonic() - started) * 1000:.0f} 毫秒返回（{r.headers['x-served-by']}）："
          f"run_id={run_id}，路由到逻辑模型 {body['model']}")

    section("3. 无状态 worker：kill -9 正在跑这个 run 的 worker，另一个 worker 从检查点接着跑", "Agent 运行时 worker + 状态存储（检查点）")
    print("客户端每 0.3 秒轮询一次 GET /runs/{id}，轮流发给 api-1 / api-2：状态都在共享的 SQLite 里，哪个 API 进程都答得上来。\n")
    last, killed, apis = None, None, ["api-1", "api-2"]
    deadline = time.monotonic() + 120
    i = 0
    while time.monotonic() < deadline:
        api = apis[i % 2]
        i += 1
        info = await client.get(api, "acme", run_id)
        view = (info["job_status"], info["run_status"], info["step"], info["writer"], info["fence"], info["attempts"])
        if view != last:
            print(f"  [+{time.monotonic() - started:4.1f}s] {pad(api, 6)} 任务 {info['job_status']:<9} run {str(info['run_status']):<9} "
                  f"第 {info['step'] or 0} 步  检查点最后写入者 {str(info['writer']):<9} fence={info['fence']}  第 {info['attempts']} 次领取")
            last = view
        if killed is None and info["job_status"] == "leased" and (info["step"] or 0) >= 1:
            idx = next(k for k, w in enumerate(dep.pool.workers) if w.worker_id == info["worker_id"])
            killed = info["worker_id"]
            pid = dep.pool.workers[idx].pid
            dep.pool.kill(idx)
            print(f"  [+{time.monotonic() - started:4.1f}s] 💥 kill -9 {killed}（pid {pid}，退出码 {dep.pool.workers[idx].returncode}）："
                  f"它正跑到一半，内存里的一切都没了")
            new = dep.pool.add()
            print(f"  [+{time.monotonic() - started:4.1f}s]    K8s 会拉起一个新 Pod 补上：{new.worker_id}（pid {new.pid}）")
        if info["job_status"] in ("succeeded", "failed", "dead"):
            break
        await asyncio.sleep(0.3)
    print(f"\n  最终回答：{' '.join((info['output'] or '').split())[:60]}")
    sync = await client.get("api-1", "acme", "sync-demo-1")
    if gave_up_at is not None and sync["run_status"] == "completed":
        print(f"  回头看那个同步请求：服务端的 run {sync['run_id']} 在客户端放弃 {sync['updated_at'] - gave_up_at:.1f} 秒之后跑完了"
              f"（状态 {sync['run_status']}，{sync['step']} 步）—— 算力和模型费用都花了，却没有人拿到结果；用户多半还会再点一次")
    else:
        print(f"  回头看那个同步请求：服务端的 run {sync['run_id']} 状态 = {sync['run_status']}")
    print("\n观察：202 让请求和执行解耦，任务多长都不怕连接超时；worker 被 kill -9 后，接手的 worker 从检查点继续"
          "\n      （fence 变大、第 2 次领取、检查点写入者换人），前面已经做完的步骤不会重做。worker 无状态，才能随意替换。")
    return run_id


# ---------------------------------------------------------------- 4 + 5. 限流与舱壁


async def burst(client: Client, apis: list[str], tenant: str, seconds: float, interval: float) -> dict:
    """在 seconds 秒里每 interval 秒发一个请求，轮流发给 apis（像负载均衡那样）。返回每个 API 的 202 / 429 计数和 run_id。"""
    results: dict = {"accepted": Counter(), "rejected": Counter(), "run_ids": [], "retry_after": set()}

    async def one(api: str, n: int):
        r = await client.submit(api, tenant, f"我还剩几天年假？（第 {n} 次）", "low", timeout=10.0)
        if r.status_code == 202:
            results["accepted"][api] += 1
            results["run_ids"].append(r.json()["run_id"])
        elif r.status_code == 429:
            results["rejected"][api] += 1
            results["retry_after"].add(r.headers.get("retry-after"))
        else:
            raise RuntimeError(f"{api} 返回 {r.status_code}: {r.text}")

    tasks, start, n = [], time.monotonic(), 0
    while time.monotonic() - start < seconds:
        tasks.append(asyncio.create_task(one(apis[n % len(apis)], n)))
        n += 1
        await asyncio.sleep(interval)
    await asyncio.gather(*tasks)
    results["elapsed"] = time.monotonic() - start
    results["sent"] = n
    return results


async def demo_limits(client: Client, dep, impl, long_run: str) -> dict:
    section("4. 跨进程限流：每个进程一个内存桶 vs 所有进程共用一个桶", "API 网关 → 按租户限流")
    plan = {"capacity": 5, "rate": 2}
    print(f"hooli（free 套餐：桶容量 {plan['capacity']}、每秒补 {plan['rate']} 个）的脚本失控：每 50 毫秒发一个请求，持续 2 秒。")
    print("同时打两组 API（每组约 40 个请求），每组两个进程，客户端在组内轮流发（相当于负载均衡）：")
    print("  mem-1 / mem-2：每个进程里一个 TenantRateLimiter（你在练习里写的，状态在进程内存里）")
    print("  api-1 / api-2：共用 agentkit.distributed.SQLiteTokenBucket（状态在共享的 SQLite 里）\n")
    mem, shared = await asyncio.gather(burst(client, ["mem-1", "mem-2"], "hooli", 2.0, 0.05),
                                       burst(client, ["api-1", "api-2"], "hooli", 2.0, 0.05))
    bound = plan["capacity"] + plan["rate"] * max(mem["elapsed"], shared["elapsed"])
    for label, r, apis in (("进程内的桶（mem-1 + mem-2）", mem, ["mem-1", "mem-2"]), ("共享的桶（api-1 + api-2）", shared, ["api-1", "api-2"])):
        total = sum(r["accepted"].values())
        per = "，".join(f"{a} 放行 {r['accepted'][a]}" for a in apis)
        print(f"  {pad(label, 30)} 发出 {r['sent']:>3}，放行 {total:>2}（{per}），其余 429（Retry-After: {'/'.join(sorted(r['retry_after']))}）")
    print(f"\n  配置的上限：一个桶 {plan['capacity']} + {plan['rate']}/秒 × {max(mem['elapsed'], shared['elapsed']):.1f} 秒 ≈ {bound:.0f} 个。"
          f"\n  进程内的桶：每个进程各有一个满桶、各按自己的速率补充，两个进程放行约 2 倍；进程越多，实际速率越高。"
          f"\n  共享的桶：两个进程每次取令牌都在同一个 SQLite 写事务里'补充 + 扣减'，守住了配置的上限。")

    section("5. 按租户的舱壁：吵闹的租户挤不垮别人", "Agent 运行时 worker → 并发配额")
    print("上面被放行的 hooli 请求都进了队列。worker 在交给 Agent 之前先拿两层槽位：")
    print("  进程内 KeyedLimiter：每个租户在一个 worker 里最多 2 个同时在跑；")
    print("  跨进程 SQLiteSemaphore：每个租户在所有 worker 加起来最多 3 个。拿不到 → RetryLater，任务放回队列、不消耗重试次数。")
    print("这时 acme 的员工来问 3 个问题，看它们要等多久：\n")
    acme_started = time.monotonic()
    acme_runs = [(await client.submit(f"api-{k % 2 + 1}", "acme", "公司的年假制度是怎样的？", "low", timeout=10.0)).json()["run_id"]
                 for k in range(3)]
    acme_done = await asyncio.gather(*(client.wait_done("acme", rid, ["api-1", "api-2"]) for rid in acme_runs))
    acme_latency = time.monotonic() - acme_started
    hooli_runs = mem["run_ids"] + shared["run_ids"]
    hooli_waited = time.monotonic()
    await asyncio.gather(*(client.wait_done("hooli", rid, ["api-1", "api-2"], timeout=180) for rid in hooli_runs))
    conn = sqlite3.connect(dep.db, timeout=30)
    rows = conn.execute("SELECT tenant, worker, pid, start, end FROM slot_log").fetchall()
    conn.close()
    by_tenant: dict[str, list] = defaultdict(list)
    by_worker: dict[tuple, list] = defaultdict(list)
    for tenant, worker, pid, start, end in rows:
        by_tenant[tenant].append((start, end))
        by_worker[(tenant, worker, pid)].append((start, end))
    deferred = [e for e in dep.pool.events("deferred")]
    hooli_workers = {f"{w}(pid {pid})": peak(iv) for (t, w, pid), iv in by_worker.items() if t == "hooli"}
    print(f"  acme 的 3 个问题全部完成用了 {acme_latency:.1f} 秒（每个问题本身 2 次模型调用，约 0.5 秒），"
          f"状态 {', '.join(d['job_status'] for d in acme_done)}")
    print(f"  hooli 的 {len(hooli_runs)} 个任务又过了 {time.monotonic() - hooli_waited:.1f} 秒才全部做完；"
          f"期间因为舱壁满了被推迟（RetryLater）{len(deferred)} 次")
    print(f"  hooli 同时在跑的任务数峰值：所有 worker 加起来 {peak(by_tenant['hooli'])}（跨进程上限 3），"
          f"每个 worker 进程里 {max(hooli_workers.values())}（进程内上限 2）："
          + "，".join(f"{k} {v}" for k, v in sorted(hooli_workers.items())))
    print("\n观察：只有进程内的舱壁时，两个 worker 加起来 hooli 最多能同时跑 2 × 2 = 4 个；跨进程的槽位把它压到 3。"
          "\n      被挡住的任务放回队列（不消耗重试次数），一个租户占不满 worker 的并发名额，别的租户的任务照常被领走；"
          "\n      hooli 同时最多只占 3 个模型调用，也就吃不光整个平台共用的模型配额。")
    return {"hooli": hooli_runs, "acme": [long_run, *acme_runs],
            "rejected": {"hooli": sum(mem["rejected"].values()) + sum(shared["rejected"].values())}}


async def ledger(client: Client, impl, runs: dict) -> None:
    section("记账：每一分钱都归到租户", "可观测性 / 计费")
    prices = {m.name: m for m in catalog(impl)}
    print(f"  {pad('租户', 10)}{pad('放行', 6)}{pad('被限流', 8)}{pad('完成', 6)}{pad('tokens', 9)}成本（示例价格，按逻辑模型）")
    for tenant, run_ids in runs.items():
        if tenant == "rejected":
            continue
        infos = [await client.get("api-1", tenant, rid) for rid in run_ids]
        tokens = sum(i["usage"]["input_tokens"] + i["usage"]["output_tokens"] for i in infos if i["usage"])
        cost = sum(prices[i["model"]].estimated_cost(i["usage"]["input_tokens"], i["usage"]["output_tokens"])
                   for i in infos if i["usage"])
        done = sum(1 for i in infos if i["job_status"] == "succeeded")
        print(f"  {pad(tenant, 10)}{pad(str(len(run_ids)), 6)}{pad(str(runs['rejected'].get(tenant, 0)), 8)}"
              f"{pad(str(done), 6)}{pad(str(tokens), 9)}${cost:.6f}")


async def run(args) -> int:
    impl, impl_path, impl_name = load_impl()
    print(f"（限流器和路由器的实现来自 {impl_name}）")
    demo_routing(impl)
    missing = deployment.missing_server_deps()
    if missing:
        print(f"\n❌ 迷你部署需要可选依赖 {', '.join(missing)}：请在仓库根目录执行  {deployment.INSTALL_HINT}")
        return 1
    import httpx

    section("1. 起一个迷你部署：4 个 API 进程 + 2 个 worker 进程，共享一个 SQLite 文件", "整张参考架构的骨架")
    workdir = RUNS_DIR / "mini"
    shutil.rmtree(workdir, ignore_errors=True)
    dep = deployment.MiniDeployment(
        workdir, apis=[("api-1", "sqlite"), ("api-2", "sqlite"), ("mem-1", "memory"), ("mem-2", "memory")],
        workers=2, lease=1.5, offline=args.offline, router=impl_path,
        worker_options={"latency": 0.25, "long_latency": 1.2, "tenant_local": 2, "tenant_shared": 3},
    )
    started = time.monotonic()
    await dep.start()  # start() 失败时会先停掉已经拉起的进程
    try:
        for api in dep.apis.values():
            print(f"  {pad(api.name, 6)} pid {api.popen.pid:<6} {api.url}  限流后端：{'共享 SQLiteTokenBucket' if api.backend == 'sqlite' else '进程内存（TenantRateLimiter）'}")
        for w in dep.pool.workers:
            print(f"  {pad(w.worker_id, 9)} pid {w.pid:<6} python -m agentkit.distributed.worker（并发 8，租约 {dep.lease} 秒）")
        print(f"  共享状态：{dep.db.relative_to(HERE.parents[1])}（{time.monotonic() - started:.1f} 秒全部就绪）")
        async with httpx.AsyncClient() as http:
            client = Client(http, dep)
            long_run = await demo_long_task_and_stateless(client, dep)
            runs = await demo_limits(client, dep, impl, long_run)
            await ledger(client, impl, runs)
    finally:
        print("\n停机：先对 API 进程发 SIGTERM（不再接新请求），再对 worker 发 SIGTERM（排空在途任务），超时未退出的 SIGKILL")
        codes = await dep.stop()
    print("  退出码：" + "，".join(f"{name}={code}" for name, code in codes.items()))
    graceful = [a.name for a in dep.apis.values() if "Application shutdown complete" in a.log_path.read_text(errors="replace")]
    print(f"  API 进程：uvicorn 收到 SIGTERM 后先优雅停机（日志里有 'Application shutdown complete' 的：{', '.join(graceful) or '无'}），"
          "\n            再按惯例用收到的信号结束自己，所以退出码是 -15；worker：排空后正常退出是 0，被 kill -9 的是 -9。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="worker 和同步接口用剧本模型代替真实模型")
    args = parser.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    t = time.time()
    code = asyncio.run(run(args))
    print(f"\n总耗时 {time.time() - t:.0f} 秒。运行产物在 {(RUNS_DIR / 'mini').relative_to(HERE.parents[1])}/")
    sys.exit(code)


if __name__ == "__main__":
    main()
