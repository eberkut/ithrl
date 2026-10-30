"""A bounded, byte-preserving Responses relay for the isolated worker.

This module deliberately uses only the standard library so the worker can copy it.
It is a request gate for a trusted native binary, not a network sandbox. Socket
timeouts bound individual operations. The owning worker process deadline bounds
total duration, including DNS resolution and a peer that keeps trickling bytes.
"""

from __future__ import annotations

import math
import secrets
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import TracebackType
from typing import override
from urllib.parse import urlsplit

_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class _Server(HTTPServer):
    relay: ResponsesRelay

    @override
    def handle_error(self, request: object, client_address: object) -> None:
        # BaseServer's default prints tracebacks, which can contain private data.
        self.relay._failed = True


class ResponsesRelay:
    """Forward only POSTs to one fixed endpoint, reserving each attempt first.

    ``on_event`` receives only ``kind``, ``ordinal``, ``count``, ``status``,
    ``request_bytes`` and ``response_bytes``. Kinds are reserved, started,
    completed, rejected and failed. A callback failure fails closed. ``started``
    counts upstream open attempts, including ambiguous transport failures;
    ``completed`` requires the entire response to have reached the client.
    No upstream retries, redirects, WebSockets or JSON interpretation occur.
    """

    def __init__(
        self,
        endpoint: str,
        max_requests: int,
        timeout_seconds: float,
        max_request_bytes: int,
        max_response_bytes: int,
        on_event: Callable[[dict[str, object]], None],
    ) -> None:
        self._initialize(
            endpoint,
            max_requests,
            timeout_seconds,
            max_request_bytes,
            max_response_bytes,
            on_event,
            allow_test_http=False,
        )

    @classmethod
    def for_test_endpoint(
        cls,
        endpoint: str,
        max_requests: int,
        timeout_seconds: float,
        max_request_bytes: int,
        max_response_bytes: int,
        on_event: Callable[[dict[str, object]], None],
    ) -> ResponsesRelay:
        """Explicitly allow HTTP to a numeric loopback address in transport tests."""
        relay = cls.__new__(cls)
        relay._initialize(
            endpoint,
            max_requests,
            timeout_seconds,
            max_request_bytes,
            max_response_bytes,
            on_event,
            allow_test_http=True,
        )
        return relay

    def _initialize(
        self,
        endpoint: str,
        max_requests: int,
        timeout_seconds: float,
        max_request_bytes: int,
        max_response_bytes: int,
        on_event: Callable[[dict[str, object]], None],
        *,
        allow_test_http: bool,
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
            valid = parsed.scheme == "https" or (
                allow_test_http
                and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "::1"}
            )
            valid = valid and bool(parsed.hostname) and parsed.port != 0
            valid = valid and not (
                parsed.username or parsed.password or parsed.query or parsed.fragment
            )
            valid = valid and not any(char in endpoint for char in ("?", "#", "\\"))
            valid = valid and "@" not in parsed.netloc
            valid = valid and not any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("relay endpoint is invalid")
        if (
            any(
                type(value) is not int or value < 1
                for value in (max_requests, max_request_bytes, max_response_bytes)
            )
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("relay limits are invalid")
        self._endpoint = endpoint
        self._max_requests = max_requests
        self._timeout = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._on_event = on_event
        self._reserved = self._started = self._completed = self._rejected = 0
        self._request_bytes = self._response_bytes = 0
        self._failed = False
        self._selected_request_limit_hit = False
        self._path = "/" + secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._client: socket.socket | None = None
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        # Explicit handlers preserve standard proxy environment and verified TLS.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _NoRedirect())

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("relay is not running")
        return f"http://127.0.0.1:{self._server.server_port}{self._path}"

    @property
    def requests_reserved(self) -> int:
        return self._reserved

    @property
    def requests_started(self) -> int:
        return self._started

    @property
    def responses_completed(self) -> int:
        return self._completed

    @property
    def requests_rejected(self) -> int:
        return self._rejected

    @property
    def request_bytes(self) -> int:
        return self._request_bytes

    @property
    def response_bytes(self) -> int:
        return self._response_bytes

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def selected_request_limit_hit(self) -> bool:
        """Whether a request was refused specifically by the chosen call budget."""
        return self._selected_request_limit_hit

    def _emit(self, kind: str, ordinal: int = 0, status: int = 0) -> None:
        self._on_event(
            {
                "kind": kind,
                "ordinal": ordinal,
                "count": self._started,
                "status": status,
                "request_bytes": self._request_bytes,
                "response_bytes": self._response_bytes,
            }
        )

    def _interrupt(self) -> None:
        with self._lock:
            if self._client is not None:
                with suppress(OSError):
                    self._client.shutdown(socket.SHUT_RDWR)

    def __enter__(self) -> ResponsesRelay:
        if self._thread is not None:
            raise RuntimeError("relay cannot be restarted")
        relay = self

        class Handler(BaseHTTPRequestHandler):
            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

            @override
            def handle(self) -> None:
                self.connection.settimeout(relay._timeout)
                with relay._lock:
                    relay._client = self.connection
                try:
                    super().handle()
                finally:
                    with relay._lock:
                        relay._client = None

            @override
            def send_error(
                self, code: int, message: str | None = None, explain: str | None = None
            ) -> None:
                self.send_response_only(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def do_POST(self) -> None:
                relay._forward(self)

        server = _Server(("127.0.0.1", 0), Handler)
        self._server = server
        server.relay = self
        server.timeout = 0.05

        def serve() -> None:
            while not self._stop.is_set():
                server.handle_request()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._interrupt()
        if self._server is not None:
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=min(1.0, self._timeout + 0.1))
            if self._thread.is_alive():
                self._failed = True

    def _reject(self, handler: BaseHTTPRequestHandler, status: int) -> None:
        self._rejected += 1
        try:
            self._emit("rejected", status=status)
        finally:
            handler.send_error(status)

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        ordinal = 0
        response_started = False
        try:
            lengths = handler.headers.get_all("Content-Length") or ()
            if handler.path != self._path + "/responses":
                self._reject(handler, 404)
                return
            if (
                handler.headers.get("Transfer-Encoding") is not None
                or handler.headers.get("Upgrade") is not None
                or len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdecimal()
            ):
                self._reject(handler, 400)
                return
            length = int(lengths[0])
            if length > self._max_request_bytes:
                self._reject(handler, 413)
                return
            if self._failed or self._stop.is_set():
                self._reject(handler, 429)
                return
            if self._reserved >= self._max_requests:
                self._selected_request_limit_hit = True
                self._reject(handler, 429)
                return
            body = handler.rfile.read(length)
            if len(body) != length:
                self._reject(handler, 400)
                return
            # One HTTPServer thread serializes admission. The reservation event
            # must succeed durably before urllib is permitted to touch the network.
            self._reserved += 1
            ordinal = self._reserved
            self._request_bytes += length
            self._emit("reserved", ordinal)
            if self._failed or self._stop.is_set():
                raise RuntimeError("relay is stopping")
            request = urllib.request.Request(  # noqa: S310 - endpoint schemes validated at construction
                self._endpoint, data=body, method="POST"
            )
            blocked = _HOP_HEADERS | {
                name.strip().lower()
                for value in handler.headers.get_all("Connection") or ()
                for name in value.split(",")
            }
            for name, value in handler.headers.items():
                if name.lower() not in blocked:
                    request.add_header(name, value)
            if self._failed or self._stop.is_set():
                raise RuntimeError("relay is stopping")
            self._started += 1
            self._emit("started", ordinal)
            if self._failed or self._stop.is_set():
                raise RuntimeError("relay is stopping")
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                if self._failed or self._stop.is_set():
                    raise RuntimeError("relay is stopping")
                status = response.status
                if not isinstance(status, int):
                    raise RuntimeError("relay status is invalid")
                response_lengths = response.headers.get_all("Content-Length") or ()
                if response_lengths and (
                    len(response_lengths) != 1
                    or not response_lengths[0].isascii()
                    or not response_lengths[0].isdecimal()
                ):
                    raise ValueError("relay response length is invalid")
                expected_length = int(response_lengths[0]) if response_lengths else None
                handler.send_response_only(status)
                blocked = _HOP_HEADERS | {
                    name.strip().lower()
                    for value in response.headers.get_all("Connection") or ()
                    for name in value.split(",")
                }
                if 300 <= status < 400:
                    blocked = blocked | {"location"}
                for name, value in response.headers.items():
                    if name.lower() not in blocked:
                        handler.send_header(name, value)
                handler.send_header("Connection", "close")
                handler.end_headers()
                response_started = True
                received = 0
                while True:
                    chunk = response.read1(min(65536, self._max_response_bytes - received + 1))
                    if not chunk:
                        if expected_length is not None and received != expected_length:
                            raise ValueError("relay response is incomplete")
                        break
                    received += len(chunk)
                    if received > self._max_response_bytes:
                        raise ValueError("relay response limit exceeded")
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    self._response_bytes += len(chunk)
                self._completed += 1
                self._emit("completed", ordinal, status)
        except Exception:
            self._failed = True
            with suppress(Exception):
                self._emit("failed", ordinal)
            if not response_started:
                handler.send_error(502)
        finally:
            handler.close_connection = True
