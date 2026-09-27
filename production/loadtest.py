"""压测 + 故障注入 + 逐项验证（httpx async）。

    python production/loadtest.py                               # 起一套 LocalStack，20 个用户压 60 秒，中途 kill -9 与滚动重启
    python production/loadtest.py --users 40 --duration 90 --workers 4 --concurrency 16
    python production/loadtest.py --no-faults --json report.json

做什么：
1. K 个虚拟用户（闭环：每个用户等上一个请求结束再发下一个，没有思考时间），按权重混合五种行为：
   交互式问答（SSE 读到 done）、交互式中途断开（读到第一个工具结果就关连接）、后台建工单、后台长任务（诊断 + 建工单）、
   后台重置密码（等审批 → 审批人**同时点两次**批准 → 等完成）；另有一个"吵闹租户"持续高频提交，专门吃 429。
2. 故障注入（相对压测开始的时间）：35% 处对"手上任务最多"的 worker 发 kill -9，并补一个新 worker（相当于 ReplicaSet 重建）；
   65% 处滚动重启一个 worker：先起新的（maxSurge），就绪后给旧的发 SIGTERM，等它排空退出。
3. 压测结束后等所有任务排空，然后逐项核对（直接查 Postgres + 读 /metrics）：
   - 所有运行都到达终态，或处于可解释的状态（被断开的交互式运行 = cancelled）；
   - 没有重复副作用：每个运行里每个 create_ticket 调用恰好对应一张工单（幂等键 run_id:call_id），不多不少；
   - 被断开的交互式运行在检查点里记为 cancelled；
   - 指标与实际数量一致（完成数、任务成功数、暂停数、取消数、429 数）。

闭环压测的局限（讲义问题卡片 6）：系统变慢时用户发得也慢了，延迟分布会被"协调遗漏"（coordinated omission）美化；
要测"固定到达率下的尾延迟"，应该用开环（按泊松过程发请求，不管上一个是否返回），例如 wrk2、k6 的 arrival-rate 执行器。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TERMINAL_EVENTS = {"completed", "failed"}


# =============================================================================== 客户端


@dataclass
class SseResult:
    status: int
    events: list[dict]
    headers: dict
    disconnected: bool = False
    first_text_s: float | None = None
    body: str = ""


class ApiClient:
    def __init__(self, base_url: str, keys: dict[str, str], timeout: float = 60.0):
        import httpx

        self.keys = keys
        # 连接池要比并发用户数大：SSE 一条流就占一个连接，池满了请求会在客户端排队，测出来的是客户端的瓶颈
        self.http = httpx.AsyncClient(base_url=base_url, timeout=timeout,
                                      limits=httpx.Limits(max_connections=500, max_keepalive_connections=100))

    async def aclose(self) -> None:
        await self.http.aclose()

    def h(self, who: str, **extra) -> dict:
        return {"Authorization": f"Bearer {self.keys[who]}", **extra}

    async def sse(self, method: str, path: str, who: str, *, json_body=None, headers=None, stop=None) -> SseResult:
        """读一条 SSE 流。stop(event) 返回 True 时立刻断开（模拟用户关掉页面）。"""
        t0 = time.perf_counter()
        events, ev, first_text = [], {}, None
        async with self.http.stream(method, path, json=json_body, headers={**self.h(who), **(headers or {})}) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")
                return SseResult(r.status_code, [], dict(r.headers), body=body)
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    ev["data"] = json.loads(line[5:].strip() or "{}")
                elif line.startswith("id:"):
                    ev["id"] = line[3:].strip()
                elif line == "" and ev:
                    events.append(ev)
                    if ev.get("event") == "delta" and first_text is None:
                        first_text = time.perf_counter() - t0
                    if stop is not None and stop(ev):
                        return SseResult(200, events, dict(r.headers), disconnected=True, first_text_s=first_text)
                    ev = {}
        return SseResult(200, events, dict(r.headers), first_text_s=first_text)

    async def submit(self, who: str, message: str, idem: str | None = None):
        headers = {"Idempotency-Key": idem} if idem else {}
        return await self.http.post("/v1/runs", json={"message": message}, headers=self.h(who, **headers))

    async def follow(self, who: str, run_id: str, *, until: set[str], timeout: float = 90.0, after: str | None = None) -> list[dict]:
        """跟随 /v1/runs/{id}/events 直到出现 until 里的事件；连接被关（服务端到时、代理断开）就带 Last-Event-ID 重连。"""
        deadline = time.monotonic() + timeout
        seen: list[dict] = []
        while time.monotonic() < deadline:
            headers = {"Last-Event-ID": after} if after else {}
            try:
                res = await asyncio.wait_for(
                    self.sse("GET", f"/v1/runs/{run_id}/events", who, headers=headers, stop=lambda e: e.get("event") in until),
                    max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001 —— 网络抖动：稍后重连
                await asyncio.sleep(0.2)
                continue
            seen += res.events
            if res.events:
                after = res.events[-1].get("id", after)
            if any(e.get("event") in until for e in res.events):
                return seen
            await asyncio.sleep(0.05)
        raise TimeoutError(f"run {run_id} 在 {timeout}s 内没有等到 {until}，已收到 {[e.get('event') for e in seen]}")

    async def get_run(self, who: str, run_id: str) -> dict:
        r = await self.http.get(f"/v1/runs/{run_id}", headers=self.h(who))
        r.raise_for_status()
        return r.json()

    async def approve(self, who: str, run_id: str, approved: bool = True):
        return await self.http.post(f"/v1/runs/{run_id}/approval", json={"approved": approved}, headers=self.h(who))

    async def metrics(self) -> str:
        return (await self.http.get("/metrics")).text


def parse_metrics(text: str) -> dict[str, float]:
    """把 Prometheus 文本格式解析成 {"name{a="1",b="2"}": value}（标签按名字排序）。"""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+(\S+)", line)
        if not m:
            continue
        labels = ""
        if m.group(2):
            pairs = sorted(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group(2)))
            labels = "{" + ",".join(f'{k}="{v}"' for k, v in pairs) + "}"
        out[m.group(1) + labels] = float(m.group(3))
    return out


def metric_sum(metrics: dict[str, float], name: str, **labels) -> float:
    total = 0.0
    for key, value in metrics.items():
        base, _, rest = key.partition("{")
        if base != name:
            continue
        if all(f'{k}="{v}"' in rest for k, v in labels.items()):
            total += value
    return total


# =============================================================================== 统计


@dataclass
class Sample:
    kind: str
    start: float
    latency_s: float
    ok: bool
    status_code: int
    error: str | None = None
    ttft_s: float | None = None
    run_id: str | None = None
    queue_wait_s: float | None = None  # 后台任务：accepted → 第一次 claimed（Stream 条目 ID 自带毫秒时间戳）


def _id_ms(event_id: str | None) -> int | None:
    try:
        return int(str(event_id).split("-")[0])
    except (TypeError, ValueError):
        return None


def queue_wait(events: list[dict]) -> float | None:
    accepted = next((e for e in events if e.get("event") == "accepted"), None)
    claimed = next((e for e in events if e.get("event") == "claimed"), None)
    a, c = _id_ms((accepted or {}).get("id")), _id_ms((claimed or {}).get("id"))
    return None if a is None or c is None else max(0.0, (c - a) / 1000)


SEGMENT_END = {"completed", "failed", "awaiting_approval", "released", "deferred", "retrying", "ownership_lost"}


def service_segments(events: list[dict]) -> list[float]:
    """从事件流算每一段 worker 执行时间：claimed → 这一段的结束事件（Stream 条目 ID 的毫秒时间戳相减）。"""
    out, start = [], None
    for e in events:
        if e.get("event") == "claimed":
            start = _id_ms(e.get("id"))
        elif e.get("event") in SEGMENT_END and start is not None:
            end = _id_ms(e.get("id"))
            if end is not None:
                out.append(max(0.0, (end - start) / 1000))
            start = None
    return out


def percentile(values: list[float], q: float) -> float | None:
    """最近秩（nearest-rank）百分位：排序后取第 ceil(q/100 × n) 个。样本少时不插值，结论更保守。"""
    if not values:
        return None
    s = sorted(values)
    k = max(1, -(-len(s) * q // 100))
    return s[int(k) - 1]


def summarize(samples: list[Sample], duration: float) -> dict:
    by_kind: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_kind[s.kind].append(s)
    rows = {}
    for kind, ss in sorted(by_kind.items()):
        lat = [s.latency_s for s in ss if s.ok]
        ttft = [s.ttft_s for s in ss if s.ttft_s is not None]
        qwait = [s.queue_wait_s for s in ss if s.queue_wait_s is not None]
        rows[kind] = {
            "n": len(ss), "ok": sum(s.ok for s in ss),
            "rate_limited": sum(s.status_code == 429 for s in ss),
            "errors": sum((not s.ok) and s.status_code != 429 for s in ss),
            "p50_s": _r(percentile(lat, 50)), "p95_s": _r(percentile(lat, 95)), "p99_s": _r(percentile(lat, 99)),
            "ttft_p50_s": _r(percentile(ttft, 50)), "ttft_p95_s": _r(percentile(ttft, 95)),
            "queue_wait_p50_s": _r(percentile(qwait, 50)), "queue_wait_p95_s": _r(percentile(qwait, 95)),
        }
    total = len(samples)
    n429 = sum(s.status_code == 429 for s in samples)
    errors = [s for s in samples if (not s.ok) and s.status_code != 429]
    return {
        "requests": total,
        "throughput_rps": round(total / duration, 2) if duration else None,
        "completed_per_s": round(sum(s.ok for s in samples) / duration, 2) if duration else None,
        "error_rate": round(len(errors) / total, 4) if total else 0.0,
        "rate_limited_ratio": round(n429 / total, 4) if total else 0.0,
        "error_examples": [f"{s.kind}: {s.error}" for s in errors[:5]],
        "by_kind": rows,
    }


def _r(x):
    return None if x is None else round(x, 3)


# =============================================================================== 行为


class Load:
    def __init__(self, client: ApiClient, seed: int = 7):
        self.c = client
        self.samples: list[Sample] = []
        self.disconnected_runs: set[str] = set()
        self.disconnected_at: dict[str, float] = {}  # run_id → 客户端关连接的时刻（time.time）
        self.approval_duplicates = Counter()
        self.observed_429 = Counter()  # 客户端看到的 429，按服务端给的原因分
        self.rng = random.Random(seed)
        self.accepted_runs: set[str] = set()

    def _note_429(self, r_status: int, body: str) -> None:
        if r_status == 429:
            self.observed_429["stream_bulkhead" if "对话流" in body else "api_bucket"] += 1

    async def interactive(self, who: str, n: int, disconnect: bool) -> Sample:
        t0 = time.perf_counter()
        msg = f"VPN 连不上怎么办（第 {n} 次）" if not disconnect else f"VPN 老断线，帮我诊断一下（{n}）"
        stop = (lambda e: e.get("event") == "tool_finished") if disconnect else None
        try:
            res = await self.c.sse("POST", "/v1/chat/stream", who, json_body={"message": msg}, stop=stop)
        except Exception as e:  # noqa: BLE001
            return Sample("interactive_disconnect" if disconnect else "interactive", t0, time.perf_counter() - t0, False, 0, repr(e))
        kind = "interactive_disconnect" if disconnect else "interactive"
        if res.status != 200:
            self._note_429(res.status, res.body)
            return Sample(kind, t0, time.perf_counter() - t0, False, res.status, f"HTTP {res.status}")
        run_id = next((e["data"].get("run_id") for e in res.events if e.get("event") == "accepted"), None)
        if disconnect:
            if res.disconnected:  # 运行可能在第一个工具之前就结束了（例如被限流中止）：那样就谈不上"断开"
                self.disconnected_runs.add(run_id)
                self.disconnected_at[run_id] = time.time()
            done = next((e["data"] for e in res.events if e.get("event") == "done"), None)
            return Sample(kind, t0, time.perf_counter() - t0, res.disconnected, 200,
                          None if res.disconnected else f"断开前运行已结束：{done}", run_id=run_id)
        done = next((e for e in res.events if e.get("event") == "done"), None)
        ok = done is not None and done["data"].get("status") == "completed"
        err = None if ok else f"done={done['data'] if done else None}, last={[e.get('event') for e in res.events[-3:]]}"
        return Sample(kind, t0, time.perf_counter() - t0, ok, 200, err, ttft_s=res.first_text_s, run_id=run_id)

    async def background(self, who: str, kind: str, message: str, approver: str | None = None) -> Sample:
        t0 = time.perf_counter()
        try:
            r = await self.c.submit(who, message, idem=uuid.uuid4().hex)
            if r.status_code != 202:
                if r.status_code == 429:
                    self.observed_429["api_bucket"] += 1
                return Sample(kind, t0, time.perf_counter() - t0, False, r.status_code, f"HTTP {r.status_code}")
            run_id = r.json()["run_id"]
            self.accepted_runs.add(run_id)
            first: list[dict] = []
            if approver is not None:
                first = await self.c.follow(who, run_id, until={"awaiting_approval", *TERMINAL_EVENTS})
                # 审批人"手抖"同时点两次：两个请求都应该返回 202，只有一个真正入队
                a, b = await asyncio.gather(self.c.approve(approver, run_id), self.c.approve(approver, run_id))
                for resp in (a, b):
                    if resp.status_code == 202:
                        self.approval_duplicates["duplicate" if resp.json()["duplicate"] else "enqueued"] += 1
                    elif resp.status_code == 429:
                        self.observed_429["api_bucket"] += 1
                        self.approval_duplicates["rate_limited"] += 1
                if not any(resp.status_code == 202 for resp in (a, b)):
                    return Sample(kind, t0, time.perf_counter() - t0, False, a.status_code, f"approval HTTP {a.status_code}/{b.status_code}", run_id=run_id)
            events = await self.c.follow(who, run_id, until=TERMINAL_EVENTS, after=first[-1]["id"] if first else None)
            final = next(e for e in reversed(events) if e.get("event") in TERMINAL_EVENTS)
            ok = final["event"] == "completed"
            return Sample(kind, t0, time.perf_counter() - t0, ok, 200, None if ok else json.dumps(final["data"], ensure_ascii=False)[:200],
                          run_id=run_id, queue_wait_s=queue_wait(first + events))
        except Exception as e:  # noqa: BLE001
            return Sample(kind, t0, time.perf_counter() - t0, False, 0, f"{type(e).__name__}: {e}"[:300])

    async def noisy(self, stop_at: float) -> None:
        """吵闹租户：每 100ms 提交一个后台任务，配额只有 1 个/秒（突发 2）→ 大部分 429。被接受的任务照常执行。"""
        n = 0
        while time.monotonic() < stop_at:
            n += 1
            t0 = time.perf_counter()
            try:
                r = await self.c.submit("noisy-nick", f"VPN 怎么连（{n}）", idem=uuid.uuid4().hex)
                ok = r.status_code == 202
                if ok:
                    self.accepted_runs.add(r.json()["run_id"])
                if r.status_code == 429:
                    self.observed_429["api_bucket"] += 1
                    assert int(r.headers.get("retry-after", "0")) >= 1, "429 必须带 Retry-After"
                self.samples.append(Sample("noisy_submit", t0, time.perf_counter() - t0, ok, r.status_code,
                                           None if ok else f"HTTP {r.status_code}"))
            except Exception as e:  # noqa: BLE001
                self.samples.append(Sample("noisy_submit", t0, time.perf_counter() - t0, False, 0, repr(e)))
            await asyncio.sleep(0.1)

    async def user(self, i: int, stop_at: float) -> None:
        tenant = ("acme-alice", "acme-bob") if i % 2 == 0 else ("globex-carol", "globex-erin")
        who, approver = tenant
        weights = [("interactive", 35), ("interactive_disconnect", 7), ("bg_ticket", 25), ("bg_diagnose", 18), ("bg_approval", 15)]
        n = 0
        while time.monotonic() < stop_at:
            n += 1
            kind = self.rng.choices([k for k, _ in weights], [w for _, w in weights])[0]
            if kind == "interactive":
                s = await self.interactive(who, n, disconnect=False)
            elif kind == "interactive_disconnect":
                s = await self.interactive(who, n, disconnect=True)
            elif kind == "bg_ticket":
                s = await self.background(who, kind, f"打印机卡纸，帮我提个工单（用户 {i} 第 {n} 次）")
            elif kind == "bg_diagnose":
                s = await self.background(who, kind, f"VPN 老断线，帮我诊断一下并提工单（用户 {i} 第 {n} 次）")
            else:
                s = await self.background(who, kind, f"我忘记密码了，帮我重置密码（用户 {i} 第 {n} 次）", approver=approver)
            self.samples.append(s)


# =============================================================================== 故障注入


@dataclass
class FaultLog:
    events: list[dict] = field(default_factory=list)

    def add(self, t0: float, what: str, **info) -> None:
        self.events.append({"t_s": round(time.monotonic() - t0, 2), "what": what, **info})


async def busiest_worker(stack) -> str | None:
    import psycopg

    def q():
        with psycopg.connect(stack.database_url, autocommit=True) as c:
            rows = c.execute("SELECT worker_id, count(*) FROM agent_jobs WHERE status = 'leased' GROUP BY worker_id ORDER BY 2 DESC").fetchall()
        return rows

    rows = await asyncio.to_thread(q)
    alive = {w.name for w in stack.workers}
    for worker_id, _n in rows:
        if worker_id in alive:
            return worker_id
    return next(iter(alive), None)


def _worker_stats(proc) -> dict | None:
    try:
        for line in reversed(proc.log_path.read_text().splitlines()):
            if '"worker_stopped"' in line:
                return json.loads(line)["stats"]
    except Exception:  # noqa: BLE001
        pass
    return None


async def inject_faults(stack, t0: float, duration: float, log: FaultLog, *, kill_at: float = 0.35, roll_at: float = 0.65) -> None:
    await asyncio.sleep(max(0.0, t0 + duration * kill_at - time.monotonic()))
    name = await busiest_worker(stack)
    victim = stack.worker_by_name(name) if name else None
    if victim is not None:
        leased = await _leased_count(stack, victim.name)
        stack.kill_worker(victim)
        log.add(t0, "kill -9", worker=victim.name, pid=victim.pid, leased_jobs=leased)
        await asyncio.sleep(1.0)  # 相当于 kubelet 发现容器退出、ReplicaSet 补一个 Pod 的时间（真实环境更久）
        new = stack.start_worker()
        await asyncio.to_thread(stack.wait_ready, 60, [new])
        log.add(t0, "replacement ready", worker=new.name)

    await asyncio.sleep(max(0.0, t0 + duration * roll_at - time.monotonic()))
    name = await busiest_worker(stack)  # 挑手上任务最多的那个：最能看出"排空 → 取消 → 归还 → 接手"
    old = stack.worker_by_name(name) if name else None
    if old is not None:
        surge = stack.start_worker()  # maxSurge=1：先起新的
        await asyncio.to_thread(stack.wait_ready, 60, [surge])
        log.add(t0, "surge worker ready", worker=surge.name)
        leased = await _leased_count(stack, old.name)
        t_term = time.monotonic()
        code = await asyncio.to_thread(stack.terminate_worker, old, True, 60)
        log.add(t0, "SIGTERM → exited", worker=old.name, exit_code=code, leased_jobs=leased,
                drain_s=round(time.monotonic() - t_term, 2), stats=_worker_stats(old))


async def _leased_count(stack, worker: str) -> int:
    import psycopg

    def q():
        with psycopg.connect(stack.database_url, autocommit=True) as c:
            return c.execute("SELECT count(*) FROM agent_jobs WHERE status = 'leased' AND worker_id = %s", (worker,)).fetchone()[0]

    return await asyncio.to_thread(q)


# =============================================================================== 验证


async def wait_drained(stack, timeout: float = 90.0) -> float:
    import psycopg

    def pending() -> int:
        with psycopg.connect(stack.database_url, autocommit=True) as c:
            return c.execute("SELECT count(*) FROM agent_jobs WHERE status IN ('queued', 'leased')").fetchone()[0]

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await asyncio.to_thread(pending) == 0:
            return time.monotonic() - t0
        await asyncio.sleep(0.5)
    raise TimeoutError(f"{timeout}s 内任务没有排空")


def verify(stack, load: Load, metrics: dict[str, float], worker_stats: list[dict]) -> dict:
    import psycopg

    checks: list[dict] = []

    def check(name: str, ok: bool, **detail) -> None:
        checks.append({"check": name, "ok": bool(ok), **detail})

    with psycopg.connect(stack.database_url, autocommit=True) as c:
        runs = c.execute(
            "SELECT s.run_id, s.mode, r.status, r.state->>'stop_reason', r.state->'messages', j.status, j.attempts, j.fence "
            "FROM service_runs s LEFT JOIN agent_runs r ON r.run_id = s.run_id "
            "LEFT JOIN agent_jobs j ON j.id = s.job_id").fetchall()
        tickets = defaultdict(set)
        for run_id, key in c.execute("SELECT run_id, idempotency_key FROM it_tickets").fetchall():
            tickets[run_id].add(key)
        attempts = dict(c.execute(
            "SELECT outcome, count(*) FROM side_effect_attempts WHERE kind = 'ticket' GROUP BY outcome").fetchall())
        resets = c.execute("SELECT count(*), count(DISTINCT idempotency_key) FROM it_password_resets").fetchone()
        jobs = dict(c.execute("SELECT status, count(*) FROM agent_jobs GROUP BY status").fetchall())
        n_jobs = c.execute("SELECT count(*) FROM agent_jobs").fetchone()[0]
        ever_paused = c.execute(
            "SELECT count(*) FROM agent_runs WHERE status = 'paused' OR jsonb_array_length(state->'approval_log') > 0").fetchone()[0]
        completed_db = c.execute("SELECT count(*) FROM agent_runs WHERE status = 'completed'").fetchone()[0]

    # 1. 终态
    statuses = Counter()
    unexplained = []
    for run_id, mode, status, reason, _msgs, job_status, _att, _fence in runs:
        key = f"{mode}:{status or 'none'}" + (f":{reason}" if status in ("stopped", "failed") else "")
        statuses[key] += 1
        explained = status == "completed" or (status == "cancelled" and run_id in load.disconnected_runs) or \
            (status == "stopped" and reason in ("rate_limited", "blocked_input"))
        if not explained:
            unexplained.append({"run_id": run_id, "mode": mode, "status": status, "reason": reason, "job": job_status})
    check("所有运行到达终态或可解释的状态", not unexplained, statuses=dict(statuses), unexplained=unexplained[:10])

    # 2. 副作用：每个运行里每个"已答复"的 create_ticket 调用恰好对应一张工单；没有多出来的工单
    mismatches = []
    for run_id, _mode, _status, _reason, msgs, *_ in runs:
        calls = set()
        for m in msgs or []:
            for tc in m.get("tool_calls") or []:
                if tc.get("function", {}).get("name") == "create_ticket":
                    calls.add(tc["id"])
        answered = {m.get("tool_call_id") for m in msgs or [] if m.get("role") == "tool"}
        expected = {f"{run_id}:{cid}" for cid in calls & answered}
        actual = tickets.get(run_id, set())
        if not actual <= {f"{run_id}:{cid}" for cid in calls} or not expected <= actual:
            mismatches.append({"run_id": run_id, "expected": sorted(expected), "actual": sorted(actual)})
    check("没有重复副作用（工单 ⇔ create_ticket 调用一一对应）", not mismatches and resets[0] == resets[1],
          tickets=sum(len(v) for v in tickets.values()), ticket_inserts=attempts.get("inserted", 0),
          replays_deduplicated=attempts.get("deduplicated", 0), password_resets=resets[0], mismatches=mismatches[:5])

    # 3. 被断开的交互式运行记为 cancelled。唯一可解释的例外：服务端在"看到断开"之前运行已经结束
    #    （客户端读事件有延迟：它关连接时，服务端可能早就把后面的事件发完了）。用检查点最后写入时间与断开时刻比对。
    status_of = {r[0]: r[2] for r in runs}
    with psycopg.connect(stack.database_url, autocommit=True) as c:
        ended = dict(c.execute("SELECT run_id, extract(epoch FROM updated_at)::float8 FROM agent_runs WHERE run_id = ANY(%s)",
                               (list(load.disconnected_runs),)).fetchall())
    finished_first, not_cancelled = {}, {}
    for rid in load.disconnected_runs:
        if status_of.get(rid) == "cancelled":
            continue
        lag = ended.get(rid, 0) - load.disconnected_at.get(rid, 0)
        (finished_first if status_of.get(rid) == "completed" and lag <= 0.05 else not_cancelled)[rid] = {
            "status": status_of.get(rid), "ended_minus_disconnect_s": round(lag, 3)}
    check("断开的交互式运行记为 cancelled", not not_cancelled, disconnected=len(load.disconnected_runs),
          finished_before_disconnect=finished_first, not_cancelled=not_cancelled)

    # 4. 指标与实际数量一致
    m_completed = metric_sum(metrics, "agent_runs_total", status="completed")
    m_paused = metric_sum(metrics, "agent_runs_total", status="paused")
    m_cancelled = metric_sum(metrics, "agent_runs_total", status="cancelled")
    m_jobs_ok = metric_sum(metrics, "itdesk_worker_job_events_total", event="completed")
    m_429 = metric_sum(metrics, "itdesk_rate_limited_total")
    shutdown_cancels = sum((s or {}).get("cancelled", 0) for s in worker_stats)
    expected_cancelled = len(load.disconnected_runs) - len(finished_first) + shutdown_cancels
    client_429 = sum(load.observed_429.values())
    pairs = {
        "completed 运行（指标 vs 检查点）": (m_completed, completed_db),
        "成功任务（指标 vs 任务表）": (m_jobs_ok, jobs.get("succeeded", 0)),
        "暂停等审批（指标 vs 曾暂停的运行）": (m_paused, ever_paused),
        "cancelled 运行段（指标 vs 断开数 + 停机取消数）": (m_cancelled, expected_cancelled),
        "429（服务端指标 vs 客户端观测）": (m_429, client_429),
    }
    check("指标与实际数量一致", all(abs(a - b) < 0.5 for a, b in pairs.values()),
          pairs={k: {"metric": a, "actual": b} for k, (a, b) in pairs.items()},
          # 信息项：取消被依赖库吞掉、由 agentkit.aio 在步骤边界补抛的次数（Python < 3.12 上的 redis-py / psycopg_pool，讲义 3.6）
          swallowed_cancellations=metric_sum(metrics, "itdesk_swallowed_cancellations_total"))

    # 被重新领取的次数 = 领取事件总数 - 任务数（kill -9 后被回收、停机时归还、被限流推迟的任务都会再领一次）。
    # fence 来自全局序列，不能再用 "fence > 1" 判断"被接手过"
    extra_claims = metric_sum(metrics, "itdesk_worker_job_events_total", event="claimed") - n_jobs
    check("没有任务进入死信", jobs.get("dead", 0) == 0, jobs=jobs, extra_claims=extra_claims)
    return {"checks": checks, "all_ok": all(c["ok"] for c in checks), "approval_clicks": dict(load.approval_duplicates)}


# =============================================================================== 主流程


def environment() -> dict:
    info = {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(),
            "cpus": os.cpu_count()}
    try:
        import psutil

        info["memory_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "darwin":
        import subprocess

        try:
            info["cpu_model"] = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        except Exception:  # noqa: BLE001
            pass
    try:
        info["loadavg_at_start"] = [round(x, 1) for x in os.getloadavg()]
    except OSError:
        pass
    return info


async def run_load(stack, *, users: int, duration: float, faults: bool = True, noisy: bool = True, seed: int = 7,
                   progress=print) -> dict:
    client = ApiClient(stack.api_url, stack.keys)
    load = Load(client, seed=seed)
    faults_log = FaultLog()
    env = environment()
    t0 = time.monotonic()
    stop_at = t0 + duration
    tasks = [asyncio.create_task(load.user(i, stop_at)) for i in range(users)]
    if noisy:
        tasks.append(asyncio.create_task(load.noisy(stop_at)))
    fault_task = asyncio.create_task(inject_faults(stack, t0, duration, faults_log)) if faults else None
    try:
        await asyncio.gather(*tasks)
        load_s = time.monotonic() - t0
        if fault_task is not None:
            await fault_task
        progress(f"压测结束（{load_s:.1f}s），等待任务排空……")
        drain_s = await wait_drained(stack)
        await asyncio.sleep(1.0)  # 让最后的事件和指标落盘
        metrics = parse_metrics(await client.metrics())
        segments = await collect_segments(client, stack, load.accepted_runs)
    finally:
        if fault_task is not None and not fault_task.done():
            fault_task.cancel()
        await client.aclose()
    stack.retire_if_exited()
    worker_stats = [_worker_stats(p) for p in stack.retired]
    report = {
        "environment": env,
        "config": {"users": users, "duration_s": duration, "workers": stack.n_workers, "concurrency": stack.concurrency,
                   "llm": stack.llm, "llm_latency_ms": stack.latency_ms, "faults": faults,
                   "lease_s": float(stack.base_env["WORKER_LEASE_SECONDS"]), "grace_s": float(stack.base_env["WORKER_GRACE_SECONDS"])},
        "summary": summarize(load.samples, load_s),
        "littles_law": littles_law(segments, stack.n_workers * stack.concurrency, load_s),
        "drain_s": round(drain_s, 2),
        "faults": faults_log.events,
        "verification": verify(stack, load, metrics, worker_stats),
        "rate_limited_seen_by_client": dict(load.observed_429),
    }
    return report


async def collect_segments(client: "ApiClient", stack, run_ids: set[str]) -> list[float]:
    """压测结束后重放每个后台运行的事件流（Last-Event-ID 从头开始），取出每一段 worker 执行时间。"""
    import psycopg

    def owners() -> dict[str, str]:
        with psycopg.connect(stack.database_url, autocommit=True) as c:
            rows = c.execute("SELECT run_id, tenant_id, user_id FROM service_runs WHERE run_id = ANY(%s)", (list(run_ids),)).fetchall()
        ident = {(v["tenant"], v["user"]): k for k, v in _identities().items()}
        return {r: ident.get((t, u)) for r, t, u in rows}

    who = await asyncio.to_thread(owners)
    sem = asyncio.Semaphore(20)

    async def one(run_id: str) -> list[float]:
        async with sem:
            try:
                return service_segments(await client.follow(who[run_id], run_id, until=TERMINAL_EVENTS, timeout=10))
            except Exception:  # noqa: BLE001
                return []

    results = await asyncio.gather(*(one(r) for r in run_ids if who.get(r)))
    return [x for seg in results for x in seg]


def _identities() -> dict:
    from production.service.identity import DEMO_IDENTITIES

    return DEMO_IDENTITIES


def littles_law(segments: list[float], slots: int, load_s: float) -> dict:
    """L = λ × W：worker 的并发名额 L（副本 × 并发）、平均服务时间 W → 饱和吞吐上限 λ = L / W。"""
    if not segments or not load_s:
        return {}
    w = statistics.mean(segments)
    return {
        "job_segments": len(segments),
        "mean_service_s": round(w, 3),
        "p95_service_s": _r(percentile(segments, 95)),
        "slots": slots,
        "predicted_max_jobs_per_s": round(slots / w, 2),
        "measured_jobs_per_s": round(len(segments) / load_s, 2),
        "slot_utilization": round(sum(segments) / (slots * load_s), 3),
    }


def print_report(report: dict, out=print) -> None:
    env, cfg, s = report["environment"], report["config"], report["summary"]
    out(f"\n环境：{env.get('cpu_model', env['machine'])}，{env['cpus']} 核，{env.get('memory_gb', '?')} GB，Python {env['python']}，"
        f"开始时 load average {env.get('loadavg_at_start')}")
    out(f"配置：{cfg['users']} 个用户 × {cfg['duration_s']}s，{cfg['workers']} 个 worker × 并发 {cfg['concurrency']}，"
        f"模型 {cfg['llm']}（{cfg['llm_latency_ms']}ms/次），租约 {cfg['lease_s']}s，宽限 {cfg['grace_s']}s")
    out(f"总请求 {s['requests']}，吞吐 {s['throughput_rps']} 请求/秒（成功 {s['completed_per_s']}/秒），"
        f"错误率 {s['error_rate']:.2%}，429 比例 {s['rate_limited_ratio']:.2%}；排空用时 {report['drain_s']}s")
    out(f"{'类型':<24}{'n':>5}{'成功':>6}{'429':>6}{'错误':>6}{'p50':>8}{'p95':>8}{'p99':>8}{'TTFT p50':>10}{'排队 p50':>10}{'排队 p95':>10}")
    for kind, r in s["by_kind"].items():
        out(f"{kind:<24}{r['n']:>5}{r['ok']:>6}{r['rate_limited']:>6}{r['errors']:>6}"
            f"{_fmt(r['p50_s']):>8}{_fmt(r['p95_s']):>8}{_fmt(r['p99_s']):>8}{_fmt(r['ttft_p50_s']):>10}"
            f"{_fmt(r['queue_wait_p50_s']):>10}{_fmt(r['queue_wait_p95_s']):>10}")
    ll = report.get("littles_law") or {}
    if ll:
        out(f"利特尔法则：worker 名额 L={ll['slots']}，平均服务时间 W={ll['mean_service_s']}s（p95 {ll['p95_service_s']}s）→ "
            f"饱和吞吐上限 L/W={ll['predicted_max_jobs_per_s']} 段/秒；实测 {ll['measured_jobs_per_s']} 段/秒，"
            f"名额利用率 {ll['slot_utilization']:.0%}")
    if s["error_examples"]:
        out("错误示例：" + " | ".join(s["error_examples"]))
    if report["faults"]:
        out("故障注入：")
        for e in report["faults"]:
            out(f"  t={e['t_s']:>6}s  {e['what']}  " + json.dumps({k: v for k, v in e.items() if k not in ('t_s', 'what')}, ensure_ascii=False))
    out("验证：")
    for c in report["verification"]["checks"]:
        detail = {k: v for k, v in c.items() if k not in ("check", "ok")}
        out(f"  {'✅' if c['ok'] else '❌'} {c['check']}  " + json.dumps(detail, ensure_ascii=False, default=str)[:600])
    out(f"审批双击：{report['verification']['approval_clicks']}")


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="压测 + 故障注入 + 验证")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--latency-ms", type=float, default=300)
    parser.add_argument("--no-faults", action="store_true")
    parser.add_argument("--json", help="把完整报告写到这个文件")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="覆盖服务的环境变量，例如 --env WORKER_GRACE_SECONDS=5（可重复）")
    args = parser.parse_args(argv)

    from production.run_local import INSTALL_HINT, LocalStack, missing_dependencies

    missing = missing_dependencies()
    if missing:
        print(f"缺少依赖：{', '.join(missing)}。请先安装：{INSTALL_HINT}")
        return 0
    overrides = dict(item.split("=", 1) for item in args.env)
    stack = LocalStack(workers=args.workers, concurrency=args.concurrency, latency_ms=args.latency_ms, env=overrides)
    print("启动 LocalStack……", flush=True)
    with stack:
        print(f"就绪（{stack.startup_seconds:.1f}s），开始压测：{args.users} 个用户 × {args.duration:.0f}s", flush=True)
        report = asyncio.run(run_load(stack, users=args.users, duration=args.duration, faults=not args.no_faults))
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"完整报告：{args.json}")
    return 0 if report["verification"]["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
