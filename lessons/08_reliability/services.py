"""第 08 课 Demo 用的"外部服务"：一个独立的操作系统进程，扮演 Agent 依赖的三个下游。

    python lessons/08_reliability/services.py --capacity 50 --service-time 0.1

demo.py 会自动把它作为子进程拉起来，演示结束时发 SIGTERM 关掉。启动后第一行打印 {"port": ..., "pid": ...}。

三个下游：
    POST /gateway               模型网关：同一时刻最多处理 capacity 个请求，每个耗时 service_time 秒；
                                满了就立刻返回 429（场景 1：惊群实验）
    POST /v1/chat/completions   OpenAI 兼容的"主模型"：默认是坏的（503），POST /admin/heal 之后恢复（场景 2）
    POST /tickets               工单系统：每调用一次就建一张新工单；带 Idempotency-Key 头时，
                                同一个 key 只建一次，第二次直接返回第一次的工单（场景 3）

观测接口 —— 数字来自服务端自己的记录，而不是客户端的说法：
    GET  /gateway/log    取走网关日志：每个请求的到达时间、第几次尝试、返回码
    GET  /stats          主模型被调用了几次、现在是否健康；工单列表
    POST /admin/heal     修好主模型
    POST /admin/reset    清空工单和计数（demo 在两个用例之间调用）

为什么用标准库 asyncio 手写 HTTP，而不用 FastAPI / uvicorn？
    惊群实验要看"同一个 10ms 里到达了多少请求"。服务端每处理一个请求多花 1ms，500 个同时到达的请求
    就会被它自己摊开到半秒，测到的就不再是客户端的行为了。这里解析一个请求只要几十微秒。
    代价是只支持本 demo 用到的最小 HTTP/1.1 子集：keep-alive、Content-Length，不支持分块传输。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time

REASONS = {200: "OK", 201: "Created", 404: "Not Found", 429: "Too Many Requests", 503: "Service Unavailable"}


class Services:
    def __init__(self, capacity: int, service_time: float):
        self.capacity = capacity
        self.service_time = service_time
        self.in_flight = 0
        self.gateway_log: list[tuple[float, int, int]] = []  # (到达时间, 第几次尝试, 返回码)
        self.model_healthy = False
        self.model_calls = 0
        self.tickets: list[dict] = []
        self.by_key: dict[str, dict] = {}

    # ------------------------------------------------------------------ 三个下游

    async def gateway(self, query: dict) -> tuple[int, dict]:
        arrived = time.time()
        attempt = int(query.get("attempt", 1))
        if self.in_flight >= self.capacity:  # 满了：不排队，立刻拒绝（和真实网关的限流一样）
            self.gateway_log.append((arrived, attempt, 429))
            return 429, {"error": {"message": "Rate limit reached, please retry later"}}
        self.in_flight += 1
        self.gateway_log.append((arrived, attempt, 200))
        try:
            await asyncio.sleep(self.service_time)  # 模拟模型生成的耗时：这段时间里占着一个处理槽位
        finally:
            self.in_flight -= 1
        return 200, {"ok": True}

    def chat_completion(self, body: bytes) -> tuple[int, dict]:
        self.model_calls += 1
        if not self.model_healthy:
            return 503, {"error": {"message": "primary model is down (injected fault)", "type": "server_error"}}
        req = json.loads(body or b"{}")
        return 200, {
            "id": f"chatcmpl-{self.model_calls}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", "primary"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "（主模型已恢复）熔断器半开时放一个试探请求，成功就关闭。"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }

    def create_ticket(self, headers: dict, body: bytes) -> tuple[int, dict]:
        req = json.loads(body or b"{}")
        key = headers.get("idempotency-key")
        if key and key in self.by_key:  # 同一个 key 第二次到来：返回第一次的结果，不再建单
            return 200, {**self.by_key[key], "replayed": True}
        ticket = {"id": f"T-{1001 + len(self.tickets)}", "title": req.get("title"), "priority": req.get("priority"),
                  "idempotency_key": key, "created_at": time.time()}
        self.tickets.append(ticket)
        if key:
            self.by_key[key] = ticket
        return 201, {**ticket, "replayed": False}

    # ------------------------------------------------------------------ 路由

    async def route(self, method: str, path: str, query: dict, headers: dict, body: bytes) -> tuple[int, dict]:
        if path == "/gateway" and method == "POST":
            return await self.gateway(query)
        if path == "/v1/chat/completions" and method == "POST":
            return self.chat_completion(body)
        if path == "/tickets" and method == "POST":
            return self.create_ticket(headers, body)
        if path == "/gateway/log":
            log, self.gateway_log = self.gateway_log, []
            return 200, {"log": log}
        if path == "/stats":
            return 200, {"model_healthy": self.model_healthy, "model_calls": self.model_calls, "tickets": self.tickets}
        if path == "/admin/heal" and method == "POST":
            self.model_healthy = True
            return 200, {"model_healthy": True}
        if path == "/admin/reset" and method == "POST":
            self.tickets, self.by_key, self.model_calls = [], {}, 0
            return 200, {"reset": True}
        return 404, {"error": f"no route {method} {path}"}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """一条 TCP 连接。keep-alive：同一条连接上可以连续发很多个请求（惊群实验的客户端就是这样复用连接的）。"""
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                request_line, *header_lines = head.decode("latin-1").split("\r\n")
                method, target, _ = request_line.split(" ", 2)
                headers = {}
                for line in header_lines:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                body = await reader.readexactly(int(headers.get("content-length") or 0))
                path, _, qs = target.partition("?")
                query = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                status, payload = await self.route(method, path, query, headers, body)
                data = json.dumps(payload, ensure_ascii=False).encode()
                writer.write(
                    f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n".encode() + data
                )
                await writer.drain()
                if headers.get("connection", "").lower() == "close":
                    break
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass  # 客户端关了连接
        finally:
            writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description="第 08 课 demo 的外部服务（模型网关 / 主模型 / 工单系统）")
    ap.add_argument("--port", type=int, default=0, help="0 = 让操作系统挑一个空闲端口")
    ap.add_argument("--capacity", type=int, default=50, help="网关同时处理的请求数上限")
    ap.add_argument("--service-time", type=float, default=0.1, help="网关处理一个请求的耗时（秒）")
    args = ap.parse_args()

    svc = Services(args.capacity, args.service_time)
    # backlog：内核里"已完成握手、等待 accept"的连接队列长度（macOS 默认上限 128，demo 分批建连接）
    server = await asyncio.start_server(svc.handle, "127.0.0.1", args.port, backlog=1024)
    print(json.dumps({"port": server.sockets[0].getsockname()[1], "pid": os.getpid()}), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()  # 不再接受新连接；还开着的连接由 asyncio.run 退出时取消


if __name__ == "__main__":
    asyncio.run(main())
