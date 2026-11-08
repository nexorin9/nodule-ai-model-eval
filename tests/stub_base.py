"""本地协议替身的公共底座（**只在测试层**）。

三份替身（RIS / 推理服务 / 科研平台）共用这一套底座：真监听 socket、真收请求、
真按 ``spec.md`` 的字段规范回响应。产品的出站代码（``requests.Session``、URL 拼接、
Bearer 鉴权头、请求体字段、响应解析）**一行都不改**就走完一遍 ——
所以它验证的是**协议面**，不是「把 HTTP 层 mock 掉」的假通路。

**纪律**：本模块只允许出现在 ``tests/`` 下。产品代码（``src/``）不 import 它，
默认出站路径也永远不会命中它 —— 除非显式开启替身门（``--fixture`` /
``IMAGING_EVAL_FIXTURE=1`` + 替身地址变量）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit


class StubHandler(BaseHTTPRequestHandler):
    """把请求记到替身上，再按脚本回响应。"""

    protocol_version = "HTTP/1.1"
    #: 关掉 Nagle 算法。响应头与响应体分两次写，开了 Nagle 就会撞上
    #: 「延迟 ACK」，客户端每条请求白等 40 毫秒 —— 那是**测具**的延迟，
    #: 不是被测服务的延迟，压测报告里必须把它摘掉，否则测的是 socket。
    disable_nagle_algorithm = True

    def _record(self, method: str, body: Any = None) -> dict[str, Any]:
        """把这次请求记到替身上，返回这条请求记录。"""

        stub: BaseStub = self.server.stub  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        record = {
            "method": method,
            "path": parsed.path,
            "query": {key: values[0] for key, values in parse_qs(parsed.query).items()},
            "headers": {key: value for key, value in self.headers.items()},
            "body": body,
        }
        stub.requests.append(record)
        return record

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

    def _respond(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str, body: Any) -> None:
        """记请求 → 出响应 → 收尾（含在途计数的加减）。

        **在途计数是并发断言的独立证据**：客户端说「我限了 N 个并发」，
        那是它自己的说法；服务端记下的**同时在途请求数**才是真的。

        **响应必须按「这一次请求」来组**：并发时 ``stub.requests[-1]``
        很可能不是当前这条请求 —— 那样回出去的响应会跟请求对不上号
        （把 A 例的标识回给 B 例）。所以请求记录直接传给 ``next_response``。

        **客户端断开不算错**：超时场景下客户端先走了，服务端再写响应会撞
        ``BrokenPipeError`` —— 那不是被测代码的问题，吞掉即可，
        别在测试输出里刷一屏 traceback。
        """

        stub = self.server.stub  # type: ignore[attr-defined]
        stub.enter()
        try:
            record = self._record(method, body)
            status, payload = stub.next_response(record)
            stub.responses.append(
                {
                    "status": status,
                    "payload": payload,
                    "request_index": len(stub.requests) - 1,
                }
            )
            try:
                self._respond(status, payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端已断开，没什么可回的了
        finally:
            stub.leave()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self._handle("POST", self._read_body())

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET", None)

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静音默认访问日志
        return


class BaseStub:
    """可编排响应的本地替身基类。

    参数
    ----
    status_sequence
        依次返回的 HTTP 状态码；用尽后重复最后一个。``[500, 500, 200]``
        表示「前两次服务端报错、第三次成功」。
    """

    #: 子类覆写：替身对外的路径。
    endpoint_path: str = ""

    def __init__(self, *, status_sequence: Sequence[int] | None = None) -> None:
        self.status_sequence = list(status_sequence or [200])
        self.requests: list[dict[str, Any]] = []
        #: **回过的响应**（状态码 + 响应体）—— 与 ``requests`` 一一对应。
        #: 契约回放要用它：把**真回过的**响应样本存成黄金文件，
        #: 之后离线喂给解析器，验「字段缺了会不会被静默默认」。
        self.responses: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._in_flight_lock = threading.Lock()
        self._in_flight = 0
        self._peak_in_flight = 0

    # ---- 在途并发计数 ----
    def enter(self) -> None:
        """有一个请求进来了（服务端侧计数）。"""

        with self._in_flight_lock:
            self._in_flight += 1
            if self._in_flight > self._peak_in_flight:
                self._peak_in_flight = self._in_flight

    def leave(self) -> None:
        """一个请求结束了。"""

        with self._in_flight_lock:
            self._in_flight -= 1

    @property
    def peak_in_flight(self) -> int:
        """服务端观察到的**同时在途请求数**峰值。"""

        with self._in_flight_lock:
            return self._peak_in_flight

    @property
    def in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    # ---- 生命周期 ----
    def start(self, port: int = 0) -> str:
        """启动服务并返回基址。

        ``port`` 缺省为 0（由系统分配一个可用端口）——
        **命令行入口要把它传进来**：端口写死一个数却绑到别处，
        是那种「看起来启动了、其实连不上」的坑。
        """

        if self._server is not None:
            return self.base_url
        self._server = ThreadingHTTPServer(("127.0.0.1", int(port)), StubHandler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        """停止服务。"""

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("替身尚未启动")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ---- 响应编排 ----
    def next_response(self, request: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        """给出下一次请求的（状态码, 响应体）。子类可覆写。

        ``request`` 是**这一次**请求的记录（method / path / query / headers / body）——
        并发时必须按它来组响应，不能拿「最后一条」凑。
        """

        index = min(len(self.requests) - 1, len(self.status_sequence) - 1)
        status = self.status_sequence[index]
        if status >= 400:
            return status, {"error": "stubbed failure", "status": status}
        return status, self.build_payload(request)

    def build_payload(self, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """正常响应体（子类覆写）。"""

        return {}

    # ---- 上下文管理器 ----
    def __enter__(self) -> "BaseStub":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


def echo_status(requests: Sequence[Mapping[str, Any]], status_sequence: Sequence[int]) -> int:
    """按第几次请求挑状态码（用尽后重复最后一个）。"""

    index = min(len(requests) - 1, len(status_sequence) - 1)
    return int(status_sequence[index])
