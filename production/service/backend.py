"""IT 服务台的"企业后端"，数据全部在 Postgres 里（多个 API / worker 进程共享同一份）。

capstone 的 Backend 用一个 SQLite 文件模拟企业系统，API 进程和各 worker 进程共享它（同一台机器上真的跨进程），
约束也是同一套：每个方法带 tenant_id、UNIQUE (tenant_id, idempotency_key)。但 SQLite 只能在一台机器上共享、
同一时刻只有一个写者；这里把同样的约束落到 Postgres，多台机器上的进程也能共享：

1. 租户隔离：每个查询都带 tenant_id，没有跨租户的入口。
2. 幂等下沉到数据所在的地方：it_tickets / it_password_resets 上有 UNIQUE (tenant_id, idempotency_key)，
   副作用和去重在**同一条 INSERT** 里完成。Redis 幂等缓存（第 26 课）只是省一次调用；
   worker 被 kill -9、租约过期、任务被接手重放时，真正挡住重复工单的是这条唯一约束。
3. 每次副作用尝试都记一行 side_effect_attempts（inserted / deduplicated）：压测后据此证明"重放发生过、而且被挡住了"。

幂等键 = ToolContext.idempotency_key = f"{run_id}:{call_id}"。Agent 在执行工具之前已经把带 call_id 的
模型回复落盘；取消 / 超时时写工具保持未回答，resume 时用**同一个 call_id** 重放（第 30 课），所以幂等键在重放时不变。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agentkit.memory import tokenize

SCHEMA: list[str] = [
    # 运行登记表：API 创建每个运行时写一行。所有权（租户、用户）以它为准 —— 任务还没被领取、检查点还不存在时，
    # 也能判断"这个 run 是谁的"。UNIQUE (tenant_id, idempotency_key)：客户端带同一个 Idempotency-Key 重试只建一个运行。
    """CREATE TABLE IF NOT EXISTS service_runs (
        run_id          text PRIMARY KEY,
        tenant_id       text NOT NULL,
        user_id         text NOT NULL,
        mode            text NOT NULL,
        idempotency_key text,
        job_id          bigint,
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, idempotency_key))""",
    "CREATE INDEX IF NOT EXISTS service_runs_owner_idx ON service_runs (tenant_id, user_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS kb_articles (
        tenant_id text NOT NULL, id text NOT NULL, title text NOT NULL, body text NOT NULL,
        PRIMARY KEY (tenant_id, id))""",
    """CREATE TABLE IF NOT EXISTS system_status (
        tenant_id text NOT NULL, system text NOT NULL, status text NOT NULL, note text,
        PRIMARY KEY (tenant_id, system))""",
    """CREATE TABLE IF NOT EXISTS employees (
        tenant_id text NOT NULL, user_id text NOT NULL, email text NOT NULL, status text NOT NULL DEFAULT 'active',
        PRIMARY KEY (tenant_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS it_tickets (
        id              bigserial PRIMARY KEY,
        ticket_no       text GENERATED ALWAYS AS (upper(tenant_id) || '-' || (1000 + id)::text) STORED,
        tenant_id       text NOT NULL,
        requester       text NOT NULL,
        title           text NOT NULL,
        description     text NOT NULL,
        category        text NOT NULL,
        priority        text NOT NULL,
        status          text NOT NULL DEFAULT 'open',
        run_id          text,
        idempotency_key text NOT NULL,
        created_by_pid  int,
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, idempotency_key))""",
    "CREATE INDEX IF NOT EXISTS it_tickets_requester_idx ON it_tickets (tenant_id, requester, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS it_password_resets (
        id              bigserial PRIMARY KEY,
        tenant_id       text NOT NULL,
        target_user     text NOT NULL,
        requested_by    text NOT NULL,
        run_id          text,
        idempotency_key text NOT NULL,
        created_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, idempotency_key))""",
    """CREATE TABLE IF NOT EXISTS side_effect_attempts (
        id              bigserial PRIMARY KEY,
        kind            text NOT NULL,
        tenant_id       text NOT NULL,
        idempotency_key text NOT NULL,
        run_id          text,
        outcome         text NOT NULL,
        pid             int,
        at              timestamptz NOT NULL DEFAULT now())""",
]

SEED_ARTICLES = [
    ("acme", "KB-001", "VPN 远程访问指南",
     "安装 AcmeConnect，服务器 vpn.acme.example，登录需要多因素认证。错误 809：网络屏蔽了 UDP 500/4500，"
     "切换为 TCP 443 模式。频繁断线：先查看系统状态是否有 VPN 故障公告。"),
    ("acme", "KB-002", "密码策略与密码重置",
     "密码每 90 天修改一次，至少 12 位。忘记密码可以让 IT 助手发起重置，需要 IT 值班工程师审批，"
     "重置链接发送到企业邮箱，30 分钟内有效。IT 永远不会索要密码。"),
    ("acme", "KB-003", "打印机常见问题",
     "提示卡纸但没有纸：清理出纸口传感器后长按取消键 5 秒复位。找不到打印机：安装打印驱动包，地址 print.acme.example。"),
    ("globex", "KB-101", "Globex VPN 使用说明", "安装 GlobexVPN，服务器 vpn.globex.example，登录后输入短信验证码。"),
    ("globex", "KB-102", "报销系统登录问题", "报销模块提示 500 错误：清除浏览器缓存或使用无痕窗口重试。"),
    ("noisy", "KB-201", "VPN 连接说明", "安装客户端后连接 vpn.noisy.example。"),
]
SEED_STATUS = [
    ("acme", "vpn", "degraded", "INC-2041：上海办公室 VPN 间歇性断连，预计 18:00 前恢复"),
    ("acme", "email", "operational", None),
    ("acme", "printer", "operational", None),
    ("globex", "vpn", "operational", None),
    ("globex", "erp", "degraded", "INC-3107：ERP 报销模块响应缓慢"),
    ("noisy", "vpn", "operational", None),
]
SEED_EMPLOYEES = [
    ("acme", "alice", "alice@acme.example"), ("acme", "bob", "bob@acme.example"),
    ("acme", "dave", "dave@acme.example"), ("globex", "carol", "carol@globex.example"),
    ("globex", "erin", "erin@globex.example"), ("noisy", "nick", "nick@noisy.example"),
]


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


async def migrate(pool, *, seed: bool = True) -> None:
    """建表 + 种子数据（幂等，可重复执行）。生产中用迁移工具（Alembic / Flyway / Atlas）在发布流水线里执行一次，
    K8s 里是一个 Job（deploy/k8s/migrate-job.yaml），而不是每个 Pod 启动时各跑一遍。"""
    async with pool.connection() as c, c.transaction():
        # 多个进程同时启动时，并发的 CREATE TABLE IF NOT EXISTS 会撞车（第 26 课实测），用 advisory lock 排队
        await c.execute("SELECT pg_advisory_xact_lock(hashtext('itdesk.migrate'))")
        for stmt in SCHEMA:
            await c.execute(stmt)
        if seed:
            for row in SEED_ARTICLES:
                await c.execute("INSERT INTO kb_articles VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", row)
            for row in SEED_STATUS:
                await c.execute("INSERT INTO system_status VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", row)
            for row in SEED_EMPLOYEES:
                await c.execute("INSERT INTO employees (tenant_id, user_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", row)


class Backend:
    """所有方法都是 async，共用进程里的 AsyncConnectionPool：借一个连接、执行一条语句、立刻归还。
    模型调用的几秒钟里不占连接（第 26 课问题 7：池大小按"持有连接的时间占比"估算，而不是按并发数）。"""

    def __init__(self, pool, *, tool_latency_s: float = 0.05, diagnostics_latency_s: float = 1.5):
        self.pool = pool
        self.tool_latency_s = tool_latency_s
        self.diagnostics_latency_s = diagnostics_latency_s

    async def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        async with self.pool.connection() as c:
            cur = await c.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    # ---------------------------------------------------------------- 只读

    async def search_kb(self, tenant_id: str, query: str, k: int = 3) -> list[dict]:
        rows = await self._rows("SELECT id, title, body FROM kb_articles WHERE tenant_id = %s", (tenant_id,))
        q = set(tokenize(query))
        scored = []
        for aid, title, body in rows:
            score = 3 * len(q & set(tokenize(title))) + len(q & set(tokenize(body)))
            if score >= 2:
                scored.append((score, {"id": aid, "title": title, "body": body}))
        scored.sort(key=lambda x: -x[0])
        return [a for _, a in scored[:k]]

    async def system_status(self, tenant_id: str) -> dict:
        rows = await self._rows("SELECT system, status, note FROM system_status WHERE tenant_id = %s", (tenant_id,))
        return {s: {"status": st, **({"note": n} if n else {})} for s, st, n in rows}

    async def list_tickets(self, tenant_id: str, requester: str, limit: int = 10) -> list[dict]:
        rows = await self._rows(
            "SELECT ticket_no, title, status, priority, created_at FROM it_tickets "
            "WHERE tenant_id = %s AND requester = %s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, requester, limit),
        )
        return [{"ticket_no": r[0], "title": r[1], "status": r[2], "priority": r[3], "created_at": r[4].isoformat()} for r in rows]

    async def employee_email(self, tenant_id: str, user_id: str) -> str | None:
        rows = await self._rows("SELECT email FROM employees WHERE tenant_id = %s AND user_id = %s", (tenant_id, user_id))
        return rows[0][0] if rows else None

    async def run_diagnostics(self, tenant_id: str, system: str) -> dict:
        """模拟一个慢的诊断接口（远程探测、日志检索）：压测时它制造"任务跑到一半"的窗口，故障注入落在这里。"""
        await asyncio.sleep(self.diagnostics_latency_s)
        status = (await self.system_status(tenant_id)).get(system, {"status": "unknown"})
        return {"system": system, "probe": "ok" if status["status"] == "operational" else "packet_loss_12%", **status}

    # ---------------------------------------------------------------- 写（副作用 + 去重在同一条 INSERT 里）

    async def _log_attempt(self, kind: str, tenant_id: str, key: str, run_id: str | None, outcome: str) -> None:
        await self._rows(
            "INSERT INTO side_effect_attempts (kind, tenant_id, idempotency_key, run_id, outcome, pid) VALUES (%s,%s,%s,%s,%s,%s)",
            (kind, tenant_id, key, run_id, outcome, os.getpid()),
        )

    async def create_ticket(self, tenant_id: str, requester: str, title: str, description: str, category: str,
                            priority: str, *, idempotency_key: str, run_id: str | None) -> tuple[dict, bool]:
        """返回 (工单, 是否重复请求)。

        INSERT ... ON CONFLICT DO NOTHING RETURNING：插进去了就是新单；0 行说明这个幂等键已经建过单，
        再用**一条新语句**把它查出来（READ COMMITTED 下同一条语句的快照看不到并发事务刚提交的行）。
        """
        rows = await self._rows(
            "INSERT INTO it_tickets (tenant_id, requester, title, description, category, priority, run_id, idempotency_key, created_by_pid) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING "
            "RETURNING ticket_no, status, priority",
            (tenant_id, requester, title, description, category, priority, run_id, idempotency_key, os.getpid()),
        )
        duplicate = not rows
        if duplicate:
            rows = await self._rows(
                "SELECT ticket_no, status, priority FROM it_tickets WHERE tenant_id = %s AND idempotency_key = %s",
                (tenant_id, idempotency_key),
            )
        await self._log_attempt("ticket", tenant_id, idempotency_key, run_id, "deduplicated" if duplicate else "inserted")
        # 下游系统的响应时间：副作用已经提交、响应还在路上。kill -9 落在这里 → 工单已建、检查点里没有结果 → 重放
        await asyncio.sleep(self.tool_latency_s)
        no, status, prio = rows[0]
        return {"ticket_no": no, "status": status, "priority": prio}, duplicate

    async def reset_password(self, tenant_id: str, target_user: str, requested_by: str, *,
                             idempotency_key: str, run_id: str | None) -> tuple[str, bool]:
        """发起密码重置：记录一次，返回打码后的邮箱。新密码 / 链接不回传给 Agent（第 09 课：秘密不进上下文）。"""
        email = await self.employee_email(tenant_id, target_user)
        if email is None:
            raise KeyError(target_user)
        rows = await self._rows(
            "INSERT INTO it_password_resets (tenant_id, target_user, requested_by, run_id, idempotency_key) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id",
            (tenant_id, target_user, requested_by, run_id, idempotency_key),
        )
        duplicate = not rows
        await self._log_attempt("password_reset", tenant_id, idempotency_key, run_id, "deduplicated" if duplicate else "inserted")
        await asyncio.sleep(self.tool_latency_s)
        return mask_email(email), duplicate


async def side_effect_report(pool) -> dict[str, Any]:
    """压测 / 测试后的核对：每个幂等键是否只产生了一次副作用，以及重放被挡住了多少次。"""
    async with pool.connection() as c:
        dup_tickets = await (await c.execute(
            "SELECT run_id, count(*) FROM it_tickets GROUP BY run_id HAVING count(*) > 1")).fetchall()
        attempts = dict(await (await c.execute(
            "SELECT outcome, count(*) FROM side_effect_attempts GROUP BY outcome")).fetchall())
        tickets = (await (await c.execute("SELECT count(*) FROM it_tickets")).fetchone())[0]
        resets = (await (await c.execute("SELECT count(*) FROM it_password_resets")).fetchone())[0]
    return {"tickets": tickets, "password_resets": resets, "runs_with_duplicate_tickets": [r[0] for r in dup_tickets],
            "attempts_inserted": attempts.get("inserted", 0), "attempts_deduplicated": attempts.get("deduplicated", 0)}
