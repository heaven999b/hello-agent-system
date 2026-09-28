"""网络故障注入：一个可以随时"拔网线"的 TCP 代理（第 26 课）。

kill -9 / SIGSTOP（processes.py）模拟的是**进程**出事；多机部署还有一类更常见、也更阴险的故障 ——
**网络分区**：worker 进程好好地活着、还在干活，只是连不上数据库了。它的心跳发不出去，租约到期，
别的机器接手；网络恢复后，它还以为任务归自己，拿着过期的状态继续写。

TcpProxy 放在 worker 和数据库之间，真实地转发 TCP 字节流，并且可以随时：

    proxy = TcpProxy("127.0.0.1", 5432)
    await proxy.start()                  # 监听 127.0.0.1:proxy.listen_port，worker 连这个端口而不是数据库
    proxy.latency = 0.05                 # 每段数据延迟 50ms 再转发（慢网络 / 跨机房）
    proxy.cut()                          # 分区：掐断所有现有连接（双方都收到 RST），之后的新连接一接上就被掐断
    proxy.heal()                         # 恢复：新连接照常转发（被掐断的旧连接不会复活，客户端要自己重连）
    await proxy.close()

它只作用于经过它的连接：同一个测试里，一个 worker 经过代理、另一个直连数据库，
就是"一台机器和数据库之间断网、另一台正常"的真实场景 —— 没有 mock 任何网络调用。

局限（如实说明）：
- cut() 用的是 RST（连接被重置），客户端会**立刻**收到错误。真实的分区更常见的表现是"包被静默丢弃"，
  客户端要等 TCP 超时才发现 —— 所以生产环境要给数据库连接配 connect_timeout、TCP keepalive、
  statement_timeout，否则一个断掉的连接可能让协程挂很久；
- 只处理一个方向结束就关闭整条连接（不支持 TCP 半关闭），对 Postgres / Redis 这类请求-响应协议足够；
- 代理跑在调用方的事件循环里：事件循环被阻塞时代理也停止转发（测试里等待子进程要用 asyncio.to_thread）。
"""

from __future__ import annotations

import asyncio
import contextlib


class _Link:
    """一条被代理的连接：客户端 ↔ 代理 ↔ 目标。"""

    def __init__(self, client: asyncio.StreamWriter, upstream: asyncio.StreamWriter):
        self.client, self.upstream = client, upstream
        self.tasks: list[asyncio.Task] = []

    def abort(self) -> None:
        """立刻丢弃连接：不发 FIN、不等缓冲区写完，对端收到 RST。"""
        for w in (self.client, self.upstream):
            w.transport.abort()
        for t in self.tasks:
            t.cancel()

    def close(self) -> None:
        for w in (self.client, self.upstream):
            if not w.is_closing():
                w.close()


class TcpProxy:
    """异步 TCP 代理：把 listen_host:listen_port 收到的连接转发到 target_host:target_port，可随时断网 / 加延迟。

    - listen_port=0（默认）表示让操作系统分配一个空闲端口，start() 之后从 listen_port 读出来；
    - latency：每段数据转发前等待的秒数（两个方向都加，近似单程时延），可以在运行中随时修改；
    - stats：accepted（建立的连接数）、refused（断网期间被拒的新连接数）、dropped（cut() 掐断的连接数）、
      bytes_up / bytes_down（客户端→目标 / 目标→客户端 转发的字节数）。
    """

    def __init__(
        self,
        target_host: str,
        target_port: int,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        latency: float = 0.0,
    ):
        self.target_host, self.target_port = target_host, int(target_port)
        self.listen_host = listen_host
        self.latency = float(latency)
        self._requested_port = int(listen_port)
        self._server: asyncio.AbstractServer | None = None
        self._cut = False
        self._links: set[_Link] = set()
        self._handlers: set[asyncio.Task] = set()
        self.stats = {"accepted": 0, "refused": 0, "dropped": 0, "bytes_up": 0, "bytes_down": 0}

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> "TcpProxy":
        self._server = await asyncio.start_server(self._on_client, self.listen_host, self._requested_port)
        return self

    @property
    def listen_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("代理还没有启动：先 await proxy.start()")
        return self._server.sockets[0].getsockname()[1]

    @property
    def is_cut(self) -> bool:
        return self._cut

    @property
    def open_connections(self) -> int:
        return len(self._links)

    async def close(self) -> None:
        """停止监听并丢弃所有连接。"""
        self._drop_all()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for t in list(self._handlers):
            t.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)

    async def __aenter__(self) -> "TcpProxy":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------ 故障注入

    def cut(self) -> int:
        """网络分区：掐断所有现有连接，并拒绝之后的新连接，直到 heal()。返回被掐断的连接数。"""
        self._cut = True
        return self._drop_all()

    def heal(self) -> None:
        """网络恢复：新连接照常转发。"""
        self._cut = False

    def _drop_all(self) -> int:
        links = list(self._links)
        for link in links:
            link.abort()
        self._links.clear()
        self.stats["dropped"] += len(links)
        return len(links)

    # ------------------------------------------------------------------ 转发

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        finally:
            self._handlers.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._cut:  # 断网期间：连接一建立就被重置，客户端看到的是"服务器意外关闭了连接"
            self.stats["refused"] += 1
            writer.transport.abort()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError:
            self.stats["refused"] += 1
            writer.transport.abort()
            return
        if self._cut:  # 连目标的这段时间里断网了
            self.stats["refused"] += 1
            writer.transport.abort()
            up_writer.transport.abort()
            return
        link = _Link(writer, up_writer)
        self._links.add(link)
        self.stats["accepted"] += 1
        link.tasks = [
            asyncio.create_task(self._pump(reader, up_writer, "bytes_up")),
            asyncio.create_task(self._pump(up_reader, writer, "bytes_down")),
        ]
        try:
            await asyncio.wait(link.tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in link.tasks:
                t.cancel()
            await asyncio.gather(*link.tasks, return_exceptions=True)
            self._links.discard(link)
            link.close()

    async def _pump(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, counter: str) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            while True:
                data = await src.read(65536)
                if not data:  # 对端关闭
                    return
                if self.latency > 0:
                    await asyncio.sleep(self.latency)
                if self._cut:  # 延迟期间被断网：这段数据丢掉
                    return
                dst.write(data)
                self.stats[counter] += len(data)
                await dst.drain()
