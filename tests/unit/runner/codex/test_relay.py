from __future__ import annotations

import http.client
import io
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, override
from urllib.parse import urlsplit

import pytest

import ithrl.runner.codex.worker as worker
from ithrl.runner.codex.protocol import ProtocolOutcome, ProtocolUsage
from ithrl.runner.codex.relay import ResponsesRelay
from ithrl.runner.codex.transcript import parse_transcript, physical_request_counts


@contextmanager
def _upstream(
    body: bytes = b'data: {"type":"fixture"}\n\ndata: [DONE]\n\n',
    status: int = 200,
    *,
    declared_length: int | None = None,
    body_gate: threading.Event | None = None,
) -> Generator[tuple[str, list[tuple[str, bytes, dict[str, str]]]]]:
    calls: list[tuple[str, bytes, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            received = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.path, received, dict(self.headers)))
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header(
                "Content-Length", str(len(body) if declared_length is None else declared_length)
            )
            self.send_header("Connection", "X-Hop")
            self.send_header("X-Hop", "must-disappear")
            self.send_header("X-Transport", "fixture")
            if status == 307:
                self.send_header("Location", "/unexpected")
            self.end_headers()
            if body_gate is not None:
                body_gate.wait(timeout=1)
            with suppress(OSError):
                self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixed/responses", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _request(
    relay: ResponsesRelay,
    body: bytes = b'{ "opaque": "bytes" }',
    *,
    path: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    parsed = urlsplit(relay.base_url)
    assert parsed.hostname is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request(
            "POST", path or parsed.path + "/responses", body=body, headers=headers or {}
        )
        response = connection.getresponse()
        return response.status, response.read(), dict(response.headers)
    finally:
        connection.close()


def _relay(endpoint: str, events: list[dict[str, object]], **limits: int) -> ResponsesRelay:
    return ResponsesRelay.for_test_endpoint(
        endpoint,
        max_requests=limits.get("max_requests", 2),
        timeout_seconds=1,
        max_request_bytes=limits.get("max_request_bytes", 1024),
        max_response_bytes=limits.get("max_response_bytes", 1024),
        on_event=events.append,
    )


@pytest.fixture(autouse=True)
def _loopback_proxy_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("no_proxy", "127.0.0.1,::1")


def test_reserves_two_physical_attempts_preserving_bytes_headers_and_sse() -> None:
    events: list[dict[str, object]] = []
    payload = b"data: \xffopaque\r\n\r\ndata: [DONE]\n\n"
    request_body = b'{ "fixture": [1,  2] }'
    with _upstream(payload) as (endpoint, calls), _relay(endpoint, events) as relay:
        for _ in range(2):
            status, body, headers = _request(
                relay,
                request_body,
                headers={
                    "Authorization": "Bearer fixture-only",
                    "X-Route": "fixture",
                    "Host": "not-a-destination.invalid",
                    "Connection": "X-Hop",
                    "X-Hop": "must-disappear",
                },
            )
            assert (status, body) == (200, payload)
            assert headers["Content-Type"] == "text/event-stream"
            assert headers["X-Transport"] == "fixture"
            assert "X-Hop" not in headers
        assert _request(relay)[0] == 429
    assert len(calls) == 2
    assert all(path == "/fixed/responses" and body == request_body for path, body, _ in calls)
    assert calls[0][2]["Authorization"] == "Bearer fixture-only"
    assert calls[0][2]["X-Route"] == "fixture"
    assert "X-Hop" not in calls[0][2]
    assert calls[0][2]["Host"] != "not-a-destination.invalid"
    assert (relay.requests_reserved, relay.requests_started, relay.responses_completed) == (2, 2, 2)
    assert relay.requests_rejected == 1
    assert relay.selected_request_limit_hit
    assert relay.request_bytes == 2 * len(request_body)
    assert relay.response_bytes == 2 * len(payload)
    assert not relay.failed
    assert [event["kind"] for event in events] == [
        "reserved",
        "started",
        "completed",
        "reserved",
        "started",
        "completed",
        "rejected",
    ]
    assert all(
        set(event)
        == {
            "kind",
            "ordinal",
            "count",
            "status",
            "request_bytes",
            "response_bytes",
        }
        for event in events
    )


@pytest.mark.parametrize("selected_limit", [4, 5, 16])
def test_worker_receipts_five_requests_or_reports_selected_limit_without_forwarding_extra(
    monkeypatch: pytest.MonkeyPatch, selected_limit: int
) -> None:
    relays: list[ResponsesRelay] = []

    def relay_factory(**kwargs: Any) -> ResponsesRelay:
        relay = ResponsesRelay.for_test_endpoint(**kwargs)
        relays.append(relay)
        return relay

    def exchange(*_args: object, **_kwargs: object) -> ProtocolOutcome:
        for ordinal in range(1, 6):
            status, _, _ = _request(relays[0])
            assert status == (200 if ordinal <= selected_limit else 429)
        return ProtocolOutcome(
            status="completed",
            error_code=None,
            final_text="Sanitized fixture response.",
            model_returned="fixture-model",
            service_tier=None,
            usage=ProtocolUsage(200, 50, 10),
            provider_acceptance="accepted",
            provider_calls_started=0,
            provider_calls_completed=0,
            actions_requested=0,
            actions_denied=0,
        )

    monkeypatch.setattr(worker, "ResponsesRelay", relay_factory)
    monkeypatch.setattr(worker, "_exchange", exchange)
    audit = io.StringIO()
    with _upstream() as (endpoint, calls):
        outcome = worker._run_app_server(
            Path("/fixture/codex"),
            {
                "provider": "ithrl",
                "model": "fixture-model",
                "transport": {
                    "endpoint": endpoint,
                    "max_requests": selected_limit,
                    "timeout_seconds": 1,
                    "max_response_bytes": 1024,
                },
            },
            audit=audit,
        )
    count = min(selected_limit, 5)
    assert len(calls) == count
    assert outcome.provider_calls_started == outcome.provider_calls_completed == count
    assert outcome.error_code == ("provider-call-limit" if selected_limit == 4 else None)
    assert outcome.provider_acceptance == ("unknown" if selected_limit == 4 else "accepted")
    assert relays[0].selected_request_limit_hit is (selected_limit == 4)
    assert not relays[0].failed
    assert parse_transcript(audit.getvalue().encode()) == outcome
    assert physical_request_counts(audit.getvalue().encode()) == (count, count)


@pytest.mark.parametrize("path", ["/responses", "/wrong/responses", "http://127.0.0.1/responses"])
def test_unknown_paths_cannot_select_upstream_destination(path: str) -> None:
    with _upstream() as (endpoint, calls), _relay(endpoint, []) as relay:
        assert _request(relay, path=path)[0] == 404
    assert calls == []
    assert relay.requests_reserved == 0
    assert not relay.selected_request_limit_hit


def test_redirect_status_is_preserved_without_following_or_exposing_location() -> None:
    with _upstream(b"redirect", 307) as (endpoint, calls), _relay(endpoint, []) as relay:
        status, body, headers = _request(relay)
    assert (status, body) == (307, b"redirect")
    assert "Location" not in headers
    assert len(calls) == 1
    assert relay.responses_completed == 1


def test_oversized_request_never_reaches_upstream() -> None:
    with _upstream() as (endpoint, calls), _relay(endpoint, [], max_request_bytes=3) as relay:
        assert _request(relay, b"four")[0] == 413
    assert calls == []
    assert relay.requests_started == 0


@pytest.mark.parametrize("declared_length", [None, 100])
def test_oversized_or_truncated_response_never_counts_completed(
    declared_length: int | None,
) -> None:
    with (
        _upstream(b"sixsix", declared_length=declared_length) as (endpoint, calls),
        _relay(endpoint, [], max_response_bytes=5 if declared_length is None else 100) as relay,
    ):
        _request(relay)
    assert len(calls) == 1
    assert relay.requests_started == 1
    assert relay.responses_completed == 0
    assert relay.failed
    assert not relay.selected_request_limit_hit
    assert relay.response_bytes <= 100


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1/responses",
        "https://user:pass@example.invalid/responses",
        "https://example.invalid/responses?route=other",
        "https://example.invalid/responses#other",
        "file:///responses",
        "https://example.invalid/\nresponses",
    ],
)
def test_production_endpoint_validation_rejects_unsafe_routes(endpoint: str) -> None:
    with pytest.raises(ValueError, match=r"^relay endpoint is invalid$"):
        ResponsesRelay(endpoint, 2, 1, 1024, 1024, lambda _event: None)


def test_reservation_callback_failure_prevents_upstream_attempt() -> None:
    def fail(_event: dict[str, object]) -> None:
        raise RuntimeError("fixture callback failure")

    with (
        _upstream() as (endpoint, calls),
        ResponsesRelay.for_test_endpoint(endpoint, 2, 1, 1024, 1024, fail) as relay,
    ):
        assert _request(relay)[0] == 502
    assert calls == []
    assert relay.requests_reserved == 1
    assert relay.requests_started == 0
    assert relay.failed


def test_stalled_stream_is_interrupted_and_shutdown_joins_relay_thread() -> None:
    release_body = threading.Event()
    with _upstream(body_gate=release_body) as (endpoint, calls):
        started = time.monotonic()
        try:
            with ResponsesRelay.for_test_endpoint(
                endpoint, 2, 0.1, 1024, 1024, lambda _: None
            ) as relay:
                assert _request(relay)[1] == b""
            assert time.monotonic() - started < 0.75
            assert relay._thread is not None
            assert not relay._thread.is_alive()
            assert relay.failed
            assert relay.responses_completed == 0
            assert len(calls) == 1
        finally:
            release_body.set()
