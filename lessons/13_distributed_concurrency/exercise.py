"""第 13 课练习：高并发与分布式执行。

三道题，全都围绕"多个 worker 同时操作同一份数据"：
  (a) claim_job                  原子领取任务：租约 + fencing token 递增（8 个线程同时抢，不能有任务被领两次）
  (b) complete_job               提交结果时校验 fencing token（租约被别人接手后，旧 worker 的提交必须被拒绝）
  (c) update_session_with_retry  乐观并发控制：版本号 CAS + 有限次重试（100 次并发自增，一次都不能丢）

开始之前，先读两个文件（都不长）：
  - jobqueue.py      表结构 SCHEMA、CLAIMABLE_WHERE、connect()、Job、LeaseLostError，以及完整版的 JobQueue
  - session_store.py SessionStore.get / compare_and_set、ConflictError

把每个 `raise NotImplementedError("TODO: ...")` 换成你的实现，然后运行：

    make lesson N=13
    # 或者：.venv/bin/python -m pytest lessons/13_distributed_concurrency -v

测试会用多个线程制造**真实的**竞争（还会故意把每条 SQL 拖慢 1 毫秒，放大竞态窗口），
所以"单线程下看起来能跑"的实现不一定能通过。卡住了？先重读 README 第 3 节，再看 solution.py。
"""

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
    """按文件路径加载同目录的模块（课程目录名以数字开头，没法写普通的 import）。

    用"目录名__模块名"注册进 sys.modules：只加载一次，也不会和其他课程的同名文件冲突。
    """
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
# 练习 (a)：claim_job —— 原子领取
# =====================================================================


def claim_job(conn: sqlite3.Connection, worker_id: str, lease_seconds: float, *, now: float | None = None) -> Job | None:
    """从 jobs 表里原子地领取**最老的**（id 最小的）一个可领取任务；没有就返回 None。

    conn 是 jobqueue.connect() 打开的连接（自动提交模式、WAL、busy_timeout 都已设好）。
    now 为 None 时用 time.time()；测试会传入固定的 now，让结果完全确定。

    "可领取"的定义已经写好在 jobqueue.CLAIMABLE_WHERE 里（表别名 j，命名参数 :now），可以直接拼进 SQL：
        SELECT j.id, j.fence FROM jobs AS j WHERE {CLAIMABLE_WHERE} ORDER BY j.id LIMIT 1

    领取 = 一次 UPDATE，把这些列改掉：
        status='leased', worker_id=你, lease_until=now+lease_seconds,
        attempts=attempts+1, fence=fence+1, updated_at=now
    返回领取后的 Job（可以用 get_job(conn, job_id) 重新读一遍）。

    ⚠️ 本题的全部难点：多个 worker 同时来抢时，**同一个任务只能被一个 worker 领到**。
    ❌ 错误写法：先 SELECT 出 id，再无条件 UPDATE ... WHERE id = ?
       两个 worker 可能 SELECT 到同一个 id，然后都 UPDATE 成功 → 同一个任务被处理两次。
    ✅ 正确写法二选一（测试都认）：
       写法 A（悲观）：conn.execute("BEGIN IMMEDIATE") 先拿写锁，再 SELECT + UPDATE，最后 COMMIT
                      （出异常要 ROLLBACK）。jobqueue.JobQueue.claim 就是这么写的。
       写法 B（乐观，推荐你试试这个）：不开事务。SELECT 出候选的 id 和 fence，然后
                      UPDATE ... WHERE id = :id AND fence = :fence AND <仍然可领取>
                      （CLAIMABLE_WHERE 用的是别名 j，想在 UPDATE 里复用它，可以写成
                       WHERE id IN (SELECT j.id FROM jobs AS j WHERE j.id = :id AND j.fence = :fence AND {CLAIMABLE_WHERE})）
                      单条 UPDATE 本身是原子的。看 cursor.rowcount：
                        1 → 抢到了；
                        0 → 在你 SELECT 之后别人抢先领走了（或状态变了）→ 回到开头重新 SELECT 下一个候选。
       为什么写法 B 里 fence 条件很关键？fence 每次领取都 +1，"fence 没变"就说明"从我看到它到现在，没人领过它"。
    """
    raise NotImplementedError("TODO: 练习 (a) —— 原子领取：租约 + attempts+1 + fence+1")


# =====================================================================
# 练习 (b)：complete_job —— 带 fencing token 的提交
# =====================================================================


def complete_job(conn: sqlite3.Connection, job_id: int, fence: int, result: str, *, now: float | None = None) -> None:
    """把任务标记为成功并保存结果。**只有** status='leased' 且 fence 与传入值相等时才允许。

    成功：status='succeeded', result=result, lease_until=NULL, updated_at=now。
    失败：抛 LeaseLostError（消息可以用 explain_lost(conn, job_id, fence) 生成），数据库里什么都不改。
    会失败的情况：
      - 租约过期后任务被别人重新领取了（fence 已经变大）→ 你是"僵尸 worker"，你的结果必须作废；
      - 任务已经 succeeded / failed / dead（重复提交）；
      - 任务不存在。

    注意：只校验 fence，**不**校验租约有没有过期。租约过期、但还没有人接手时，迟到的提交仍然有效 ——
    fencing 的判定标准是"你是不是最新的持有者"，而不是"你的租约有没有过期"。

    提示：一条带条件的 UPDATE + 检查 rowcount 就够了（和练习 (a) 写法 B 是同一个套路）。
    """
    raise NotImplementedError("TODO: 练习 (b) —— 校验 fencing token 后再提交")


# =====================================================================
# 练习 (c)：update_session_with_retry —— CAS + 有限次重试
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
    """用乐观并发控制安全地修改一个会话，返回写入成功后的 Session。

    每一次尝试（attempt 从 1 开始）：
      1. current = store.get(session_id)             —— **每次都重新读**最新版本
      2. new_data = update_fn(current.data)           —— 在最新数据上重新应用修改
      3. store.compare_and_set(session_id, current.version, new_data)
         - 成功 → 返回它的结果（Session）
         - 抛 ConflictError → 说明读完之后有人写过：
             * 如果传了 on_conflict，调用 on_conflict(attempt, error)（每次冲突都要调用，包括最后一次）；
             * 如果 attempt == max_attempts → 把这个 ConflictError 原样抛出去（放弃，不要无限重试）；
             * 否则等待 (rng or random).uniform(0, backoff_s * 2 ** min(attempt - 1, 5)) 秒
               （用参数里的 sleep 函数，测试会替换它；backoff_s <= 0 时不用等），然后进入下一次尝试。

    ⚠️ 两个最常见的错误：
      - 只在循环外 get 一次，重试时拿旧数据再 CAS —— 版本号永远对不上，或者更糟：
        拿着旧 version 去 CAS 恰好成功时，会把别人的修改覆盖掉；
      - 用 store.put_unsafe 直接覆盖 —— 那就又回到"丢失更新"了。
    为什么要有上限？冲突说明有竞争；无限重试会让所有人一起空转（活锁），还会放大下游压力。
    为什么要随机退避？几个 worker 同时冲突后如果立刻一起重试，大概率再次一起冲突。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts 至少为 1")
    raise NotImplementedError("TODO: 练习 (c) —— 读最新版本 → 应用修改 → CAS；冲突则退避后重试")
