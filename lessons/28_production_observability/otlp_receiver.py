"""第 28 课 Demo 用的迷你 OTLP/HTTP 接收端 —— 一个独立的操作系统进程。

    python lessons/28_production_observability/otlp_receiver.py      # 打印 "LISTENING <端口>" 后一直运行

它扮演的是生产里 OTel Collector / Jaeger / Tempo 的位置：API 进程和 worker 进程各自用 OTLP/HTTP（protobuf）
把 span 发到 POST /v1/traces，它们之间没有任何共享内存。它只做两件事：
  - 解析 ExportTraceServiceRequest，记下每次请求（路径、Content-Type、字节数、资源属性、span 名字）和每个 span；
  - 给 demo 一个只读的查询口：GET /requests、GET /spans（JSON），POST /reset 清空。
它不是 Collector：没有采样、没有脱敏、不持久化，进程退出数据就没了。

为什么单独起一个进程，而不是在 demo 里开个线程？线程里的接收端和发送方共享同一个解释器，
"跨进程导出"就只是一句话；独立进程才和生产一样：数据只能经过网络（这里是 127.0.0.1 上的 TCP）到达它。
进程内部用 ThreadingHTTPServer 并发处理请求，这只是 HTTP 服务器的实现方式，不冒充任何别的东西。
"""

from __future__ import annotations

import http.server
import json
import sys
import threading

_KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}

_lock = threading.Lock()
REQUESTS: list[dict] = []
SPANS: list[dict] = []


def _value(v):
    """OTLP AnyValue → Python 值。"""
    kind = v.WhichOneof("value")
    if kind == "array_value":
        return [_value(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _value(kv.value) for kv in v.kvlist_value.values}
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, kind) if kind else None


def _attrs(kvs) -> dict:
    return {kv.key: _value(kv.value) for kv in kvs}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path == "/reset":
            with _lock:
                REQUESTS.clear()
                SPANS.clear()
            return self._reply(200, b"{}", "application/json")
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
            ExportTraceServiceResponse,
        )

        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        req = ExportTraceServiceRequest()
        req.ParseFromString(body)
        spans, resources = [], []
        for rs in req.resource_spans:
            res = _attrs(rs.resource.attributes)
            resources.append(res)
            for ss in rs.scope_spans:
                for sp in ss.spans:
                    spans.append({
                        "name": sp.name,
                        "kind": _KINDS.get(sp.kind, str(sp.kind)),
                        "status": _STATUS.get(sp.status.code, str(sp.status.code)),
                        "trace_id": sp.trace_id.hex(),
                        "span_id": sp.span_id.hex(),
                        "parent_id": sp.parent_span_id.hex() or None,
                        "start": sp.start_time_unix_nano,
                        "attrs": _attrs(sp.attributes),
                        "service": res.get("service.name"),
                        "pid": res.get("process.pid"),
                    })
        with _lock:
            REQUESTS.append({"path": self.path, "content_type": self.headers.get("Content-Type"), "bytes": len(body),
                             "resources": resources, "span_names": [s["name"] for s in spans]})
            SPANS.extend(spans)
        self._reply(200, ExportTraceServiceResponse().SerializeToString(), "application/x-protobuf")

    def do_GET(self):  # noqa: N802
        with _lock:
            data = {"/requests": REQUESTS, "/spans": SPANS}.get(self.path)
            body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
        if body is None:
            return self._reply(404, b"{}", "application/json")
        self._reply(200, body, "application/json")

    def _reply(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 不往终端打每一条访问日志
        pass


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"LISTENING {server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
