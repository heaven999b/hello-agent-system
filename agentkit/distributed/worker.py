"""worker 进程的入口：python -m agentkit.distributed.worker --queue sqlite:///runs/jobs.db --app app.py:make_handler

和生产部署里的 worker 一模一样：一个独立的操作系统进程，从命令行拿配置，连上共享的队列，
用 run_worker 循环领任务；收到 SIGTERM 就停止领取、把在途任务做完（最多 --grace 秒）再退出。
K8s 里每个 Pod 跑的就是这样一条命令（第 31 课），本机用 WorkerPool 一次拉起 N 个（第 13 课）。

--app 指向一个工厂函数，签名 factory(ctx: WorkerContext) -> handler（可以是 async 函数）。
它可以写成模块路径 "pkg.module:make_handler"，也可以写成文件路径 "lessons/13_xxx/app.py:make_handler"。
handler 如果有 aclose() 方法，进程退出前会调用它（关闭模型客户端的连接池等）。

每个事件以一行 JSON 打到标准输出（event、worker_id、pid、t 和事件自己的字段），方便收集和断言：
    {"event": "claimed", "worker_id": "w1", "pid": 4242, "t": 1727500000.12, "job": 7, "fence": 12, ...}
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import socket
import sys
import time

from .app import WorkerContext, load_attr, open_queue
from .jobs import run_worker, stop_on_signals


def _print_event(name: str, info: dict) -> None:
    line = {"event": name, "pid": os.getpid(), "t": round(time.time(), 4), **info}
    print(json.dumps(line, ensure_ascii=False, default=str), flush=True)


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentkit.distributed.worker", description="agentkit worker 进程")
    ap.add_argument("--queue", required=True, help="sqlite:///path/to/jobs.db 或 postgresql://...")
    ap.add_argument("--app", required=True, help="handler 工厂：module:attr 或 file.py:attr")
    ap.add_argument("--worker-id", default=None, help="默认 主机名-pid")
    ap.add_argument("--concurrency", type=int, default=8, help="这个进程同时处理的任务数上限（背压）")
    ap.add_argument("--lease", type=float, default=30.0, help="租约秒数；心跳默认每 1/3 租约续一次")
    ap.add_argument("--poll", type=float, default=0.5, help="队列为空时的轮询间隔")
    ap.add_argument("--heartbeat", type=float, default=None, help="心跳间隔秒数（默认租约的 1/3）")
    ap.add_argument("--grace", type=float, default=25.0, help="收到 SIGTERM 后等在途任务的最长秒数")
    ap.add_argument("--release-on-cancel", action="store_true",
                    help="停机时被取消的任务立刻归还队列（别人马上能领），而不是等租约过期")
    ap.add_argument("--kinds", default=None, help="只领取这些 kind（逗号分隔）")
    ap.add_argument("--max-jobs", type=int, default=None, help="领取这么多个任务后退出（测试用）")
    ap.add_argument("--opt", action="append", default=[], help="传给工厂的选项 key=value，可重复")
    ap.add_argument("--queue-opt", action="append", default=[],
                    help="传给队列构造函数的数值选项 key=value，例如 base_backoff=0.2、max_attempts=3，可重复")
    args = ap.parse_args(argv)

    worker_id = args.worker_id or f"{socket.gethostname()}-{os.getpid()}"
    queue_kwargs = {k: (float(v) if "." in v else int(v)) for k, v in (o.split("=", 1) for o in args.queue_opt)}
    queue, db = await open_queue(args.queue, **queue_kwargs)
    options = dict(o.split("=", 1) for o in args.opt)
    stop = asyncio.Event()
    stop_on_signals(stop)
    ctx = WorkerContext(queue_url=args.queue, worker_id=worker_id, queue=queue, db=db, options=options, stop_event=stop)
    handler = load_attr(args.app)(ctx)
    if inspect.isawaitable(handler):
        handler = await handler

    def on_event(name: str, info: dict) -> None:
        _print_event(name, info)
        if ctx.on_event is not None:
            ctx.on_event(name, info)

    try:
        stats = await run_worker(
            queue, handler, worker_id=worker_id, stop_event=stop, concurrency=args.concurrency,
            lease_seconds=args.lease, poll_interval=args.poll, heartbeat_interval=args.heartbeat,
            grace_period=args.grace, kinds=args.kinds.split(",") if args.kinds else None, on_event=on_event,
            max_jobs=args.max_jobs, release_on_cancel=args.release_on_cancel,
        )
    finally:
        close = getattr(handler, "aclose", None)
        if close is not None:
            await close()
        await queue.close()
        if db is not None:
            await db.close()
    return 0 if stats is not None else 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
