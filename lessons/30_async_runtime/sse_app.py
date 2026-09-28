"""第 30 课场景 4 的服务端：一个最小的 Agent SSE 服务。demo.py 用**真实的子进程**启动它：

    python -m uvicorn --app-dir lessons/30_async_runtime sse_app:app --fd <监听 socket> --lifespan on

它是一个独立的操作系统进程（uvicorn 自己的事件循环），demo 只通过 HTTP 和它交流（httpx）：

    GET /chat/stream?q=&run_id=&variant=   SSE 流式对话（每个事件带 id: run_id:序号）
    GET /chat/resume?run_id=&variant=      从检查点恢复，同样是 SSE
    GET /runs/{run_id}?variant=            服务端视角：检查点状态、模型在途数、工具真正执行完的次数
    GET /stats                             服务端日志里的 "Task exception was never retrieved" 条数等
    GET /health                            启动探针

检查点全部是真实的异步数据库，带版本号 CAS（写冲突会抛 CheckpointConflict，而不是悄悄覆盖）：
    - SQLiteCheckpointer（agentkit.distributed）：环境变量 LESSON30_SQLITE 指定数据库文件；
    - PostgresCheckpointer（agentkit.contrib.postgres）：环境变量 LESSON30_PG_URI，demo 用嵌入式 Postgres 启动时才有。
模型是剧本模型 PacedLLM（asyncio.sleep 扮演"等模型"），这样断开的时刻可以精确控制；除此之外都是真的：
真实的 TCP 连接、真实的 uvicorn / Starlette 断开检测、真实的数据库写入。
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from agentkit import Agent, RunFinished, RunStarted, StreamDone, TextDelta, ToolContext, ToolFinished, ToolStarted, call_tool, reply, tool
from agentkit.distributed import SQLiteCheckpointer, SQLiteDB
from agentkit.state import RunState


class _Count(logging.Handler):
    """统计某个 logger 的记录（按关键词），给 /stats 用。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


ASYNCIO_LOG, AGENTKIT_LOG = _Count(), _Count()
logging.getLogger("asyncio").addHandler(ASYNCIO_LOG)
logging.getLogger("agentkit").addHandler(AGENTKIT_LOG)
for _name in ("psycopg", "psycopg.pool"):  # 查询进行到一半被取消时 psycopg 会打 WARNING：这里正是在制造这种情况
    logging.getLogger(_name).setLevel(logging.ERROR)


class PacedLLM:
    """剧本模型：还没有工具结果时，很快返回一次工具调用；看到工具结果后给出回答，前 slow_answers 次回答很慢（30 秒）。
    按对话内容决定快慢，所以同一个 Agent 可以反复做"断开"实验。calls / in_flight 是服务端自己的计数。"""

    model = "scripted"

    def __init__(self, slow_answers: int | None = None, slow: float = 30.0, fast: float = 0.05):
        self.slow_answers, self.slow, self.fast = slow_answers, slow, fast
        self.calls = self.in_flight = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.in_flight += 1
        try:
            if messages[-1]["role"] != "tool":
                await asyncio.sleep(self.fast)
                return call_tool("check_vpn_cert", user="alice")
            slow = self.slow_answers is None or self.slow_answers > 0
            if self.slow_answers:
                self.slow_answers -= 1
            await asyncio.sleep(self.slow if slow else self.fast)  # 被取消时 CancelledError 从这里抛出
            return reply("您的 VPN 证书已过期。请打开自助门户 → 证书 → 重新签发，完成后重连即可。")
        finally:
            self.in_flight -= 1

    async def stream(self, messages, tools=None, **kwargs):
        response = await self.chat(messages, tools, **kwargs)
        text = response.content or ""
        for i in range(0, len(text), 6):
            await asyncio.sleep(0)
            yield TextDelta(text[i : i + 6])
        yield StreamDone(response)


completed: dict[str, int] = {}  # run_id → 工具真正执行完的次数（被取消在半路的不算）


@tool
async def check_vpn_cert(user: str, ctx: ToolContext) -> str:
    """检查用户的 VPN 证书（只读，要 0.5 秒）"""
    await asyncio.sleep(0.5)
    completed[ctx.run_id] = completed.get(ctx.run_id, 0) + 1
    return f"{user} 的 VPN 证书已于 3 天前过期"


@tool(name="check_vpn_cert")
async def check_vpn_cert_fast(user: str, ctx: ToolContext) -> str:
    """检查用户的 VPN 证书（只读）"""
    await asyncio.sleep(0.05)
    completed[ctx.run_id] = completed.get(ctx.run_id, 0) + 1
    return f"{user} 的 VPN 证书已于 3 天前过期"


agents: dict[str, Agent] = {}
_closers: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = SQLiteDB(os.environ["LESSON30_SQLITE"])  # 一个连接 + 一个专用线程，几个检查点共用
    cps = {name: SQLiteCheckpointer(db, table=f"runs_{name}") for name in ("a", "b", "sqlite")}
    # 每个实验一个独立的 Agent（各自的模型和检查点表），互不影响
    agents["a"] = Agent(PacedLLM(), [check_vpn_cert], checkpointer=cps["a"])
    agents["b"] = Agent(PacedLLM(slow_answers=1), [check_vpn_cert], checkpointer=cps["b"])  # 只慢一次：4d 恢复时正常回答
    agents["sqlite"] = Agent(PacedLLM(), [check_vpn_cert_fast], checkpointer=cps["sqlite"])
    closers = [db.close]
    pg_uri = os.environ.get("LESSON30_PG_URI")
    if pg_uri:
        from agentkit.contrib.postgres import PostgresCheckpointer

        pg = PostgresCheckpointer(pg_uri, table="lesson30_runs")
        cps["pg"] = pg
        agents["pg"] = Agent(PacedLLM(), [check_vpn_cert_fast], checkpointer=pg)
        closers.insert(0, pg.close)
    for cp in cps.values():
        await cp.setup()
    try:
        yield
    finally:
        for agent in agents.values():
            await agent.aclose()
        for close in closers:
            await close()


app = FastAPI(lifespan=lifespan)


def encode(seq: int, run_id: str, event) -> str:
    if isinstance(event, RunStarted):
        name, data = "run_started", {"run_id": event.run_id}
    elif isinstance(event, ToolStarted):
        name, data = "tool_started", {"tool": event.call.name}
    elif isinstance(event, ToolFinished):
        name, data = "tool_finished", {"tool": event.call.name, "ok": event.result.ok}
    elif isinstance(event, TextDelta):
        name, data = "delta", {"text": event.text}
    elif isinstance(event, RunFinished):
        name, data = "done", {"status": event.result.status, "output": event.result.output}
    else:
        name, data = type(event).__name__, {}
    # id 让浏览器的 EventSource 断线重连时带上 Last-Event-ID；这里用 run_id:序号，服务端据此知道该接着哪个运行
    return f"id: {run_id}:{seq}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_response(events, run_id: str) -> StreamingResponse:
    async def body():
        seq = 0
        async with contextlib.aclosing(events) as stream:  # 客户端断开 → Starlette 取消这个生成器 → aclosing 取消运行
            async for event in stream:
                seq += 1
                yield encode(seq, run_id, event)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 关掉 Nginx 这类代理的缓冲，否则事件会攒着一起到
    return StreamingResponse(body(), media_type="text/event-stream", headers=headers)


@app.get("/health")
async def health():
    return {"ok": True, "pid": os.getpid(), "variants": sorted(agents)}


@app.get("/chat/stream")
async def chat_stream(q: str, run_id: str, variant: str):
    return sse_response(agents[variant].stream(q, run_id=run_id), run_id)


@app.get("/chat/resume")
async def chat_resume(run_id: str, variant: str):
    return sse_response(agents[variant].stream_resume(run_id), run_id)


@app.get("/runs/{run_id}")
async def run_status(run_id: str, variant: str):
    agent = agents[variant]
    # 用只读的 get_run：load 会记住版本号（带 fence 时还会"接管"），在这里调用会干扰正在运行的会话
    row = await agent.checkpointer.get_run(run_id)
    state = RunState.from_dict(row["state"]) if row else None
    return {
        "status": state.status if state else None,
        "stop_reason": state.stop_reason if state else None,
        "version": row["version"] if row else None,
        "tool_started": state.tool_calls_count if state else None,  # agentkit 在执行前计数
        "tool_completed": completed.get(run_id, 0),
        "last_tool_message": next((m["content"] for m in reversed(state.messages) if m["role"] == "tool"), None) if state else None,
        "llm_calls": agent.llm.calls,
        "llm_in_flight": agent.llm.in_flight,
    }


@app.get("/stats")
async def stats():
    gc.collect()  # "never retrieved" 在任务对象被回收时才记录
    return {
        "pid": os.getpid(),
        "never_retrieved": sum("never retrieved" in m for m in ASYNCIO_LOG.messages),
        "agentkit_warnings": list(AGENTKIT_LOG.messages[-5:]),
    }
