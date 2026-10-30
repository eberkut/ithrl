from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

import ithrl.runner.codex.worker as worker
from ithrl.runner.codex.protocol import ProtocolOutcome, ProtocolUsage
from ithrl.runner.codex.transcript import parse_transcript, physical_request_counts


def _native_outcome() -> ProtocolOutcome:
    return ProtocolOutcome(
        status="completed",
        error_code=None,
        final_text="Sanitized final response.",
        model_returned="fixture-model",
        service_tier=None,
        usage=ProtocolUsage(200, 50, 10),
        provider_acceptance="accepted",
        provider_calls_started=0,
        provider_calls_completed=0,
        actions_requested=0,
        actions_denied=0,
    )


def _request() -> dict[str, Any]:
    return {
        "provider": "ithrl",
        "model": "fixture-model",
        "transport": {
            "endpoint": "https://fixture.invalid/v1/responses",
            "max_requests": 4,
            "timeout_seconds": 120,
            "max_response_bytes": 1024,
        },
    }


class _Relay:
    def __init__(self, **kwargs: Any) -> None:
        self.arguments = kwargs
        self.base_url = "http://127.0.0.1:12345/private-fixture-nonce"
        self.requests_reserved = 0
        self.requests_started = 0
        self.responses_completed = 0
        self.requests_rejected = 0
        self.request_bytes = 0
        self.response_bytes = 0
        self.failed = False
        self.selected_request_limit_hit = False
        self.closed = False

    def __enter__(self) -> _Relay:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def event(self, kind: str, status: int = 0) -> None:
        self.arguments["on_event"](
            {
                "kind": kind,
                "ordinal": 0 if kind == "rejected" else self.requests_reserved,
                "count": self.requests_started,
                "status": status,
                "request_bytes": self.request_bytes,
                "response_bytes": self.response_bytes,
            }
        )

    def response(self) -> None:
        self.requests_reserved += 1
        self.request_bytes += 32
        self.event("reserved")
        self.requests_started += 1
        self.event("started")
        self.response_bytes += 64
        self.responses_completed += 1
        self.event("completed", 200)


def test_worker_attaches_only_relay_counts_and_publishes_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_Relay] = []
    audit = io.StringIO()
    request = _request()

    def relay_factory(**kwargs: Any) -> _Relay:
        relay = _Relay(**kwargs)
        created.append(relay)
        return relay

    def exchange(
        executable: Path, native_request: dict[str, object], base_url: str, **_kwargs: Any
    ) -> ProtocolOutcome:
        relay = created[0]
        assert not relay.closed
        assert executable == Path("/fixture/codex")
        assert "transport" not in native_request
        assert native_request == {"provider": "ithrl", "model": "fixture-model"}
        assert base_url == relay.base_url
        for _ in range(3):
            relay.response()
        assert not any(
            json.loads(line)["kind"] == "summary" for line in audit.getvalue().splitlines()
        )
        return _native_outcome()

    monkeypatch.setattr(worker, "ResponsesRelay", relay_factory)
    monkeypatch.setattr(worker, "_exchange", exchange)
    outcome = worker._run_app_server(Path("/fixture/codex"), request, audit=audit)

    assert outcome.provider_calls_started == outcome.provider_calls_completed == 3
    assert created[0].closed
    assert created[0].arguments["endpoint"] == request["transport"]["endpoint"]
    assert request == _request()
    assert parse_transcript(audit.getvalue().encode()) == outcome
    records = [json.loads(line) for line in audit.getvalue().splitlines()]
    assert records[-2]["value"]["kind"] == "closed"
    assert records[-1]["kind"] == "summary"


@pytest.mark.parametrize("unsafe", ["failed", "rejected", "incomplete", "no-request"])
def test_worker_refuses_completed_native_turn_when_transport_is_unsafe(
    monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    relay = _Relay()

    def relay_factory(**kwargs: Any) -> _Relay:
        relay.arguments = kwargs
        return relay

    def exchange(*_args: object, **_kwargs: object) -> ProtocolOutcome:
        if unsafe != "no-request":
            relay.response()
        if unsafe == "failed":
            relay.failed = True
            relay.event("failed")
        elif unsafe == "rejected":
            relay.requests_rejected = 1
            relay.event("rejected", 429)
        elif unsafe == "incomplete":
            relay.requests_reserved += 1
            relay.request_bytes += 32
            relay.event("reserved")
            relay.requests_started += 1
            relay.event("started")
        return _native_outcome()

    monkeypatch.setattr(worker, "ResponsesRelay", relay_factory)
    monkeypatch.setattr(worker, "_exchange", exchange)
    audit = io.StringIO()
    outcome = worker._run_app_server(Path("/fixture/codex"), _request(), audit=audit)
    assert outcome.status == "error"
    assert outcome.error_code == "provider-transport"
    assert outcome.final_text is None
    assert outcome.provider_calls_started == (
        2 if unsafe == "incomplete" else int(unsafe != "no-request")
    )
    assert parse_transcript(audit.getvalue().encode()) == outcome
    assert physical_request_counts(audit.getvalue().encode()) == (
        outcome.provider_calls_started,
        outcome.provider_calls_completed,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_requests", 17),
        ("max_requests", True),
        ("timeout_seconds", 86401),
        ("max_response_bytes", 0),
    ],
)
def test_worker_refuses_invalid_transport_before_native_start(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    request = _request()
    request["transport"][field] = value

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("native startup occurred before transport validation")

    monkeypatch.setattr(worker, "_exchange", forbidden)
    with pytest.raises(worker.WorkerError, match="transport is invalid"):
        worker._run_app_server(Path("/fixture/codex"), request, audit=io.StringIO())


@pytest.mark.parametrize(
    ("no_proxy", "lower_no_proxy"),
    [
        (None, None),
        ("internal.example", None),
        (None, "localhost,::1"),
        ("internal.example", "localhost,::1"),
    ],
)
def test_native_child_bypasses_loopback_without_changing_parent_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_proxy: str | None,
    lower_no_proxy: str | None,
) -> None:
    commands: list[tuple[str, ...]] = []
    environments: list[dict[str, str]] = []
    request: dict[str, object] = {"provider": "ithrl", "model": "fixture-model"}
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    original_config = (
        'model_provider = "ithrl"\n'
        '[model_providers.ithrl]\nbase_url = "https://fixture.invalid"\n'
        "request_max_retries = 0\nstream_max_retries = 0\n"
    )
    config.write_text(original_config)
    config.chmod(0o600)
    monkeypatch.setattr(worker, "SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    if no_proxy is not None:
        monkeypatch.setenv("NO_PROXY", no_proxy)
    if lower_no_proxy is not None:
        monkeypatch.setenv("no_proxy", lower_no_proxy)
    parent_environment = dict(os.environ)

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self) -> int:
            return 0

    def popen(command: tuple[str, ...], **kwargs: Any) -> Process:
        commands.append(command)
        environments.append(kwargs["env"])
        assert kwargs["cwd"] == worker.SANDBOX_WORKSPACE
        expected = tomllib.loads(original_config)
        expected["model_providers"]["ithrl"]["base_url"] = (
            "http://127.0.0.1:12345/private-fixture-nonce"
        )
        assert tomllib.loads(config.read_text()) == expected
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        return Process()

    def session(
        _reader: object, _writer: object, _audit: object, native: object
    ) -> ProtocolOutcome:
        assert native is request
        return _native_outcome()

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    monkeypatch.setattr(worker, "run_session", session)
    outcome = worker._exchange(
        Path("/fixture/codex"),
        request,
        "http://127.0.0.1:12345/private-fixture-nonce",
        audit=io.StringIO(),
    )
    assert outcome == _native_outcome()
    assert commands == [("/fixture/codex", "app-server")]
    [child_environment] = environments
    assert child_environment["HTTP_PROXY"] == "http://proxy.example:8080"
    effective_no_proxy = no_proxy if no_proxy is not None else lower_no_proxy
    expected_no_proxy = f"{effective_no_proxy},127.0.0.1" if effective_no_proxy else "127.0.0.1"
    assert child_environment["NO_PROXY"] == expected_no_proxy
    assert child_environment["no_proxy"] == expected_no_proxy
    assert dict(os.environ) == parent_environment


def test_structured_overload_retains_all_physical_requests_and_unknown_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = _Relay()

    def relay_factory(**kwargs: Any) -> _Relay:
        relay.arguments = kwargs
        return relay

    def exchange(*_args: object, **_kwargs: object) -> ProtocolOutcome:
        for _ in range(4):
            relay.response()
        return replace(
            _native_outcome(),
            status="error",
            error_code="provider-overloaded",
            final_text=None,
            provider_acceptance="unknown",
            usage=None,
            behavior_output_observed=False,
        )

    monkeypatch.setattr(worker, "ResponsesRelay", relay_factory)
    monkeypatch.setattr(worker, "_exchange", exchange)
    audit = io.StringIO()
    outcome = worker._run_app_server(Path("/fixture/codex"), _request(), audit=audit)
    assert outcome.error_code == "provider-overloaded"
    assert outcome.provider_acceptance == "unknown"
    assert not outcome.behavior_output_observed
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 4
    assert parse_transcript(audit.getvalue().encode()) == outcome


def test_unexpected_worker_error_is_content_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def private_failure(_path: Path) -> dict[str, object]:
        raise ValueError("Synthetic private transport details.")

    monkeypatch.setattr(worker, "_read_private_request", private_failure)
    assert worker.main(["/fixture/request"]) == 70
    captured = capsys.readouterr()
    assert captured.err == "Codex worker failed\n"
    assert captured.out == ""


def test_worker_entrypoint_uses_only_the_mounted_codex_executable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    validated: list[Path] = []
    invoked: list[Path] = []
    mounted = Path(worker.SANDBOX_ROOT) / "runner" / "codex"

    def require_executable(path: Path) -> Path:
        validated.append(path)
        return path

    def run(executable: Path, _request: object, *, audit: object) -> ProtocolOutcome:
        invoked.append(executable)
        return _native_outcome()

    def read_request(_path: Path) -> dict[str, Any]:
        return _request()

    monkeypatch.setattr(worker, "_read_private_request", read_request)
    monkeypatch.setattr(worker, "_require_executable", require_executable)
    monkeypatch.setattr(worker, "_run_app_server", run)

    assert worker.main(["/fixture/request"]) == 0
    assert worker.main(["/fixture/request", "/fixture/other-executable"]) == 64
    assert validated == invoked == [mounted]
    assert capsys.readouterr().err == "Codex worker requires a request\n"


def test_inner_shutdown_timeout_preserves_receipts_for_outer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops: list[str] = []
    relays: list[_Relay] = []

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            stops.append("terminate")

        def kill(self) -> None:
            stops.append("kill")

        def wait(self, *, timeout: float) -> None:
            stops.append("wait")
            raise subprocess.TimeoutExpired("fixture", timeout)

    class ReaderThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, *, timeout: float) -> None:
            assert timeout == worker._SHUTDOWN_SECONDS

        def is_alive(self) -> bool:
            return True

    def relay_factory(**kwargs: Any) -> _Relay:
        relay = _Relay(**kwargs)
        relays.append(relay)
        return relay

    def session(*_args: object) -> ProtocolOutcome:
        relays[0].response()
        return _native_outcome()

    def configure(_provider: object, _base_url: str) -> None:
        pass

    process = Process()

    def popen(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    monkeypatch.setattr(worker.threading, "Thread", ReaderThread)
    monkeypatch.setattr(worker, "run_session", session)
    monkeypatch.setattr(worker, "_configure_relay", configure)
    monkeypatch.setattr(worker, "ResponsesRelay", relay_factory)
    audit = io.StringIO()
    outcome = worker._run_app_server(Path("/fixture/codex"), _request(), audit=audit)
    assert parse_transcript(audit.getvalue().encode()) == outcome
    assert outcome.status == "completed"
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert relays[0].closed
    assert stops == ["terminate", "wait", "kill", "wait"]
    assert process.stdin.closed
    assert process.stdout.closed
    assert not process.stderr.closed
