"""第 10 课练习参考答案。先自己做，再来对照。"""

from __future__ import annotations

import importlib.util
import random
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Callable


def _load_sibling(name: str) -> ModuleType:
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。"""
    here = Path(__file__).resolve().parent
    key = f"{here.name}__{name}"  # 例如 "10_distributed_concurrency__jobqueue"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, here / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


jobqueue = _load_sibling("jobqueue")
session_store = _load_sibling("session_store")

Job = jobqueue.Job
LeaseLostError = jobqueue.LeaseLostError
CLAIMABLE_WHERE = jobqueue.CLAIMABLE_WHERE
get_job = jobqueue.get_job
explain_lost = jobqueue.explain_lost
ConflictError = session_store.ConflictError
Session = session_store.Session
SessionStore = session_store.SessionStore


# =====================================================================
# (a) claim_job：写法 B —— 乐观领取（条件 UPDATE + 检查 rowcount）
# =====================================================================


def claim_job(conn: sqlite3.Connection, worker_id: str, lease_seconds: float, *, now: float | None = None) -> Job | None:
    now = time.time() if now is None else now
    while True:
        # 1) 不加锁地看一眼：现在最老的可领取任务是谁、它的 fence 是多少
        row = conn.execute(
            f"SELECT j.id, j.fence FROM jobs AS j WHERE {CLAIMABLE_WHERE} ORDER BY j.id LIMIT 1", {"now": now}
        ).fetchone()
        if row is None:
            return None
        # 2) 条件 UPDATE = CAS：只有"fence 没变 + 仍然可领取"时才生效。
        #    单条语句在 SQLite 里是原子的（执行时持有写锁，WHERE 在最新数据上重新求值）。
        #    CLAIMABLE_WHERE 用的是表别名 j，所以放进一个子查询里复用。
        cur = conn.execute(
            f"""UPDATE jobs
                SET status = 'leased', worker_id = :worker, lease_until = :until,
                    attempts = attempts + 1, fence = fence + 1, updated_at = :now
                WHERE id IN (SELECT j.id FROM jobs AS j
                             WHERE j.id = :id AND j.fence = :fence AND {CLAIMABLE_WHERE})""",
            {"worker": worker_id, "until": now + lease_seconds, "now": now, "id": row["id"], "fence": row["fence"]},
        )
        if cur.rowcount == 1:
            return get_job(conn, row["id"])
        # 3) rowcount == 0：在我 SELECT 之后、UPDATE 之前，别人抢先领走了它（或者它的状态变了）。
        #    说明别人取得了进展，不是死循环 —— 重新 SELECT 下一个候选即可。


# =====================================================================
# (b) complete_job：只认最新的 fencing token
# =====================================================================


def complete_job(conn: sqlite3.Connection, job_id: int, fence: int, result: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    cur = conn.execute(
        """UPDATE jobs SET status = 'succeeded', result = ?, lease_until = NULL, updated_at = ?
           WHERE id = ? AND fence = ? AND status = 'leased'""",
        (result, now, job_id, fence),
    )
    if cur.rowcount != 1:
        raise LeaseLostError(explain_lost(conn, job_id, fence))


# =====================================================================
# (c) update_session_with_retry：读最新 → 应用修改 → CAS；冲突则退避重试
# =====================================================================


def update_session_with_retry(
    store: SessionStore,
    session_id: str,
    update_fn: Callable[[dict], dict],
    *,
    max_attempts: int = 10,
    backoff_s: float = 0.005,
    on_conflict: Callable[[int, ConflictError], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Session:
    if max_attempts < 1:
        raise ValueError("max_attempts 至少为 1")
    rng = rng or random
    for attempt in range(1, max_attempts + 1):
        current = store.get(session_id)  # 每次都重新读最新版本
        new_data = update_fn(current.data)  # 在最新数据上重新应用修改
        try:
            return store.compare_and_set(session_id, current.version, new_data)
        except ConflictError as e:
            if on_conflict is not None:
                on_conflict(attempt, e)
            if attempt == max_attempts:
                raise
            if backoff_s > 0:
                sleep(rng.uniform(0, backoff_s * 2 ** min(attempt - 1, 5)))
    raise AssertionError("unreachable")
