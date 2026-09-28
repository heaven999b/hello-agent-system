"""真实的多进程竞争：同时拉起 N 个 python 进程，各用各的 sqlite3 连接去抢同一个数据库文件。

测试（test_exercise.py）和 demo（场景 1）都用它来检验你写的 claim_job / update_session_with_retry。

每个子进程（python race.py ...）：
  1. 按文件路径加载要检验的实现（exercise.py / solution.py，或本文件里的反面教材 naive_claim_job）；
  2. 连上数据库，写一个"我准备好了"的文件，然后等父进程创建"开跑"文件 —— 所有进程站在同一条起跑线上；
  3. 按模式循环调用练习函数，最后把结果以一行 JSON 打到标准输出（出错时打出异常类型和 traceback）。
父进程（run_race）收集结果；子进程里的异常在父进程里重新抛出（NotImplementedError 原样抛出，其余变成 AssertionError）。

为什么是进程而不是线程？进程之间不共享任何内存：没有 GIL、没有共用的 Python 对象、没有"同一个连接上的锁"，
和多台机器上的 worker 一样，只能通过数据库协作。谁先抢到、谁被挡住，全是操作系统调度和 SQLite 锁决定的真实结果。
这些子进程里没有事件循环，所以直接调用阻塞的 sqlite3 完全没问题（在事件循环里就不行了，见 jobqueue.AsyncJobQueue）。

widen=True 时，每条 SQL 执行前先停 1 毫秒（sqlite3 的 trace 回调）：竞态窗口被放大，
"先 SELECT、再无条件 UPDATE"这类有竞态的写法会稳定地暴露出来。它只是把真实存在的窗口拉长，不制造本来没有的交错。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent


def _load(path: Path, key: str) -> ModuleType:
    """按文件路径加载模块。key 与 exercise.py 的 _load_sibling 一致（"目录名__模块名"），整个进程只有一份。"""
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


jobqueue = _load(HERE / "jobqueue.py", f"{HERE.name}__jobqueue")


def naive_claim_job(conn, worker_id: str, lease_seconds: float, *, now: float | None = None):
    """❌ 反面教材：先 SELECT 出候选，再**无条件** UPDATE。单进程里完全正常，多进程下同一个任务会被领两次。"""
    now = time.time() if now is None else now
    row = conn.execute(
        f"SELECT j.id FROM jobs AS j WHERE {jobqueue.CLAIMABLE_WHERE} ORDER BY j.id LIMIT 1", {"now": now}
    ).fetchone()
    if row is None:
        return None
    conn.execute(  # ← 这里没有任何条件：在我 SELECT 之后别人已经领走了，我照样覆盖
        """UPDATE jobs SET status = 'leased', worker_id = :worker, lease_until = :until,
                          attempts = attempts + 1, fence = fence + 1, updated_at = :now
           WHERE id = :id""",
        {"worker": worker_id, "until": now + lease_seconds, "now": now, "id": row["id"]},
    )
    return jobqueue.get_job(conn, row["id"])  # 两个进程都拿着"同一个任务"离开，都以为它归自己


def load_impl(impl: str) -> tuple[ModuleType | None, dict]:
    """impl：exercise.py / solution.py 的路径，或 "naive"。返回 (模块, 函数表)。"""
    if impl == "naive":
        return None, {"claim_job": naive_claim_job}
    path = Path(impl).resolve()
    module = _load(path, f"{path.parent.name}__{path.stem}")
    return module, {name: getattr(module, name) for name in ("claim_job", "complete_job", "update_session_with_retry")}


# =====================================================================================
# 子进程
# =====================================================================================


def _widen(_sql: str) -> None:
    time.sleep(0.001)


def _wait_for_start(ready: Path, go: Path, timeout: float = 60.0) -> None:
    ready.write_text(str(os.getpid()))
    deadline = time.monotonic() + timeout
    while not go.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("等了太久也没等到开跑信号")
        time.sleep(0.001)


def child_claim(args, fns) -> dict:
    """循环领取任务。两种停法：领到 None 为止（抢完一批）；或者跑满 duration 秒（租约翻车测试：领了就不管，等它过期再被别人领）。"""
    conn = jobqueue.connect(args.db)
    if args.widen:
        conn.set_trace_callback(_widen)
    claim = fns["claim_job"]
    _wait_for_start(Path(args.ready), Path(args.go))
    claims = []
    deadline = time.time() + args.duration if args.duration else None
    while True:
        now = time.time()
        if deadline is not None and now >= deadline:
            break
        job = claim(conn, args.worker, args.lease, now=now)
        if job is None:
            if deadline is None:
                break
            time.sleep(0.002)
            continue
        claims.append({"id": job.id, "fence": job.fence, "attempts": job.attempts, "now": now,
                       "lease_until": job.lease_until, "worker_id": job.worker_id})
    conn.close()
    return {"claims": claims}


def child_increment(args, fns) -> dict:
    """在同一个会话上做 times 次"读 → 想一会儿 → 写回 +1"，用 update_session_with_retry 保证不丢更新。"""
    session_store = _load(HERE / "session_store.py", f"{HERE.name}__session_store")
    store = session_store.SessionStore(args.db)
    update = fns["update_session_with_retry"]
    conflicts = 0

    def count_conflict(_attempt, _err):
        nonlocal conflicts
        conflicts += 1

    def increment(data: dict) -> dict:
        time.sleep(args.think)  # "读完之后要处理一会儿"（对 Agent 来说是调一次模型）：窗口里别人会插进来
        data["count"] = data.get("count", 0) + 1
        return data

    _wait_for_start(Path(args.ready), Path(args.go))
    for _ in range(args.times):
        update(store, "counter", increment, max_attempts=args.max_attempts, backoff_s=args.backoff,
               on_conflict=count_conflict)
    store.close()
    return {"conflicts": conflicts}


def child_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["claim", "increment"])
    ap.add_argument("--db", required=True)
    ap.add_argument("--impl", required=True)
    ap.add_argument("--worker", required=True)
    ap.add_argument("--ready", required=True)
    ap.add_argument("--go", required=True)
    ap.add_argument("--lease", type=float, default=30.0)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--widen", action="store_true")
    ap.add_argument("--times", type=int, default=10)
    ap.add_argument("--think", type=float, default=0.001)
    ap.add_argument("--max-attempts", type=int, default=200)
    ap.add_argument("--backoff", type=float, default=0.001)
    args = ap.parse_args(argv)
    out: dict = {"worker": args.worker, "pid": os.getpid()}
    try:
        _, fns = load_impl(args.impl)
        out.update(child_claim(args, fns) if args.mode == "claim" else child_increment(args, fns))
        out["ok"] = True
    except BaseException as e:  # noqa: BLE001 —— 原样报告给父进程
        out.update(ok=False, type=type(e).__name__, message=str(e), traceback=traceback.format_exc())
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0


# =====================================================================================
# 父进程
# =====================================================================================


def run_race(n: int, mode: str, *, db: str | Path, impl: str | Path, timeout: float = 90.0, **opts) -> list[dict]:
    """同时拉起 n 个子进程，等它们都准备好后一起开跑，返回每个进程的结果（按 worker 名排序）。

    opts：lease / duration / widen（claim 模式），times / think / max_attempts / backoff（increment 模式）。
    """
    tag = uuid.uuid4().hex[:8]
    base = Path(db).resolve().parent / f".race-{tag}"
    base.mkdir()
    go = base / "go"
    procs = []
    for i in range(n):
        cmd = [sys.executable, str(Path(__file__).resolve()), mode, "--db", str(db), "--impl", str(impl),
               "--worker", f"p{i + 1}", "--ready", str(base / f"ready-{i}"), "--go", str(go)]
        for k, v in opts.items():
            flag = "--" + k.replace("_", "-")
            if v is True:
                cmd.append(flag)
            elif v not in (False, None):
                cmd += [flag, str(v)]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    try:
        deadline = time.monotonic() + timeout
        while sum((base / f"ready-{i}").exists() for i in range(n)) < n:
            dead = [p for p in procs if p.poll() is not None]
            if dead:  # 有进程还没准备好就退出了（比如加载实现时就出错）
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout}s 内 {n} 个进程没有全部就绪")
            time.sleep(0.005)
        go.touch()  # 起跑：所有子进程在 1 毫秒内看到它
        results = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=max(1.0, deadline - time.monotonic()))
            line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                raise AssertionError(f"子进程 {p.pid} 没有正常输出结果（退出码 {p.returncode}）：\n{stdout}\n{stderr}") from None
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        for f in base.iterdir():
            f.unlink()
        base.rmdir()
    for r in results:
        if not r["ok"]:
            if r["type"] == "NotImplementedError":
                raise NotImplementedError(f"[进程 {r['worker']}] {r['message']}")
            raise AssertionError(f"进程 {r['worker']}（pid {r['pid']}）出错：{r['type']}: {r['message']}\n{r['traceback']}")
    return sorted(results, key=lambda r: int(r["worker"][1:]))


if __name__ == "__main__":
    sys.exit(child_main())
