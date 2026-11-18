from __future__ import annotations

import asyncio
import json
import ssl
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ithrl.runner.copilot import worker


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("certificate private-detail", "Copilot TLS certificate validation failed"),
        ("auth private-detail", "Copilot authentication was unavailable to the SDK session"),
        ("model private-detail", "Copilot could not initialize the requested model"),
        ("network private-detail", "Copilot could not establish the required connection"),
        ("private-detail", "Copilot rejected the SDK operation"),
    ],
)
def test_sdk_failures_have_fixed_content_safe_diagnostics(detail: str, reason: str) -> None:
    assert worker._failure_reason(RuntimeError(detail)) == reason


def _request(tmp_path: Path) -> dict[str, Any]:
    return {
        "commandFlags": ["--no-auto-update"],
        "model": "fixture-model",
        "nativeSession": str(tmp_path / "events.jsonl"),
        "sessionId": "session-1",
        "systemMessage": "Captured fixture guidance",
        "target": "final-response",
        "workspace": str(tmp_path),
    }


def test_physical_forward_gate_is_atomic_across_concurrent_callbacks() -> None:
    gate = worker.ForwardGate()

    def reserve(_index: int) -> bool:
        return gate.reserve()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(64)))
    assert sum(results) == gate.forwarded == 1
    assert gate.rejected == 63


def test_request_parser_has_no_trigger_or_semantic_import(tmp_path: Path) -> None:
    value = _request(tmp_path)
    assert worker._parse_request(json.dumps(value).encode()) == value
    value["trigger"] = "invented continuation"
    with pytest.raises(worker.WorkerError, match="unsupported shape"):
        worker._parse_request(json.dumps(value).encode())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sessionId", "../escape"),
        ("nativeSession", "relative"),
        ("model", ""),
        ("systemMessage", "bad\0guidance"),
    ],
)
def test_private_request_refuses_unsafe_fields(tmp_path: Path, field: str, value: str) -> None:
    request = _request(tmp_path)
    request[field] = value
    with pytest.raises(worker.WorkerError, match="invalid fields"):
        worker._parse_request(json.dumps(request).encode())


def test_duplicate_request_fields_and_constants_are_refused(tmp_path: Path) -> None:
    for raw in (b'{"model":"fixture","model":"other"}', b'{"model":NaN}'):
        with pytest.raises(worker.WorkerError, match="invalid JSON"):
            worker._parse_request(raw)


def test_worker_entrypoint_uses_owned_paths_and_refuses_caller_selected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    owned_request = tmp_path / "request.json"
    owned_request.write_text(json.dumps(request))
    owned_request.chmod(0o600)
    executable = tmp_path / "runner" / "copilot"
    executable.parent.mkdir()
    executable.write_text("owned executable")
    executable.chmod(0o500)
    foreign = tmp_path / "foreign.json"
    foreign.write_text("private foreign input")
    foreign.chmod(0o600)
    calls: list[tuple[Path, dict[str, Any]]] = []

    async def observe(
        path: Path, settings: dict[str, Any], *, github_token: str | None = None
    ) -> int:
        calls.append((path, settings))
        return 0

    monkeypatch.setattr(worker, "_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setattr(worker, "_run_session", observe)
    assert worker.main([str(foreign), str(foreign)]) == 64
    assert foreign.read_text() == "private foreign input"
    assert owned_request.exists()
    assert not calls
    assert worker.main([]) == 0
    assert calls == [(executable, request)]
    assert not owned_request.exists()
    assert foreign.read_text() == "private foreign input"


@pytest.mark.parametrize(
    ("environment", "selected"),
    [
        ({"COPILOT_GITHUB_TOKEN": "first", "GH_TOKEN": "second", "GITHUB_TOKEN": "third"}, "first"),
        ({"COPILOT_GITHUB_TOKEN": "", "GH_TOKEN": "second", "GITHUB_TOKEN": "third"}, "second"),
        ({"GITHUB_TOKEN": "third"}, "third"),
        ({}, None),
    ],
)
def test_worker_selects_private_session_authentication_only_at_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    selected: str | None,
) -> None:
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    request = _request(tmp_path)
    private_request = tmp_path / "request.json"
    private_request.write_text(json.dumps(request))
    private_request.chmod(0o600)
    executable = tmp_path / "runner" / "copilot"
    executable.parent.mkdir()
    executable.write_text("owned executable")
    executable.chmod(0o500)
    observed: list[str | None] = []

    async def observe(
        path: Path, settings: dict[str, Any], *, github_token: str | None = None
    ) -> int:
        assert path == executable
        assert settings == request
        observed.append(github_token)
        return 0

    monkeypatch.setattr(worker, "_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setattr(worker, "_run_session", observe)
    assert worker.main([]) == 0
    assert observed == [selected]
    assert capsys.readouterr() == ("", "")


def test_private_file_is_bounded_and_unlinked_after_checked_read(tmp_path: Path) -> None:
    source = tmp_path / "request.json"
    source.write_bytes(b"private")
    source.chmod(0o600)
    assert worker._read_private_file(source, maximum=7, unlink=True) == b"private"
    assert not source.exists()
    source.write_bytes(b"too large")
    source.chmod(0o600)
    with pytest.raises(worker.WorkerError, match="unsafe"):
        worker._read_private_file(source, maximum=7)
    assert source.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_private_file_refuses_links_and_public_permissions(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"private")
    source.chmod(0o600)
    path = tmp_path / "request"
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "hardlink":
        path.hardlink_to(source)
    else:
        path.write_bytes(b"private")
        path.chmod(0o644)
    with pytest.raises(worker.WorkerError, match="unsafe"):
        worker._read_private_file(path, maximum=100)
    assert source.read_bytes() == b"private"


@pytest.mark.parametrize(
    ("target", "tools"), [("final-response", []), ("shell-action", ["builtin:bash"])]
)
def test_sdk_restore_options_clear_ambient_surfaces_and_preserve_system(
    tmp_path: Path, target: str, tools: list[str]
) -> None:
    request = _request(tmp_path)
    request["target"] = target
    options = worker._resume_options(request, on_event=None, on_permission_request=None)
    assert options["model"] == "fixture-model"
    assert options["available_tools"] == tools
    assert options["system_message"] == {"mode": "replace", "content": request["systemMessage"]}
    assert options["continue_pending_work"] is options["allow_transcript_recovery"] is False
    assert options["suppress_resume_event"] is True
    assert options["autopilot"] == {"continuation_enabled": False, "max_continues": 0}
    for name in (
        "enable_config_discovery",
        "enable_managed_settings",
        "request_extensions",
        "request_canvas_renderer",
    ):
        assert options[name] is False
    assert options["capi"]["enable_web_socket_responses"] is False


def test_session_auth_options_use_only_explicit_token_and_keep_managed_login_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ambient-fixture-token")
    request = _request(tmp_path)
    options = worker._resume_options(request, on_event=None, on_permission_request=None)
    assert "github_token" not in options
    assert "use_logged_in_user" not in options
    explicit = worker._resume_options(
        request, on_event=None, on_permission_request=None, github_token="selected-fixture-token"
    )
    assert explicit["github_token"] == "selected-fixture-token"
    assert "ambient-fixture-token" not in repr(explicit)
    assert "selected-fixture-token" not in json.dumps(request)
    request["githubToken"] = "selected-fixture-token"
    with pytest.raises(worker.WorkerError, match="unsupported shape"):
        worker._parse_request(json.dumps(request).encode())


def _outgoing(*, url: str = "https://api.githubcopilot.com/responses") -> httpx.Request:
    # Managed provider encoding belongs to the SDK, including non-JSON bodies.
    return httpx.Request("POST", url, content=b"opaque SDK provider encoding")


def _context(**changes: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{"session_id": "session-1", "parent_agent_id": None, "transport": "http", **changes}
    )


def _install_sdk(monkeypatch: pytest.MonkeyPatch, *, client: type[Any], handler: type[Any]) -> None:
    class Connection:
        @staticmethod
        def for_stdio(**options: object) -> dict[str, object]:
            return options

    monkeypatch.setitem(
        sys.modules,
        "copilot",
        SimpleNamespace(
            CopilotClient=client,
            CopilotRequestHandler=handler,
            RuntimeConnection=Connection,
            ManagedSettings=SimpleNamespace,
            ManagedSettingsPermissions=SimpleNamespace,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "copilot.generated.rpc",
        SimpleNamespace(SendMessagesRequest=SimpleNamespace),
    )


def _permission_event(verified: bool = True) -> dict[str, object]:
    return {
        "type": "session.managed_settings_resolved",
        "data": {
            "clientManaged": verified,
            "bypassPermissionsDisabled": True,
            "failClosed": False,
            "permissionsContext": {
                "permissions": {
                    "disableBypassPermissionsMode": "disable",
                    "disableAssistedPermissionsMode": True,
                    "deny": ["Shell(*)"],
                }
            },
        },
    }


def _validate(
    outgoing: httpx.Request,
    *,
    context: SimpleNamespace | None = None,
) -> bytes:
    return worker._managed_request(
        outgoing,
        _context() if context is None else context,
        session_id="session-1",
    )


def test_managed_route_preserves_opaque_sdk_encoding() -> None:
    outgoing = _outgoing()
    assert _validate(outgoing) == outgoing.content


@pytest.mark.parametrize(
    "url",
    [
        "http://api.githubcopilot.com/chat/completions",
        "https://api.githubcopilot.com.attacker.example/chat/completions",
        "https://custom-provider.example/v1/messages",
        "https://user:password@api.githubcopilot.com/chat/completions",
        "https://api.githubcopilot.com:8443/chat/completions",
    ],
)
def test_custom_or_insecure_provider_routes_are_refused(url: str) -> None:
    with pytest.raises(worker.WorkerError, match="routing"):
        _validate(_outgoing(url=url))


@pytest.mark.parametrize(
    "changes", [{"session_id": "other"}, {"parent_agent_id": "parent"}, {"transport": "websocket"}]
)
def test_wrong_session_subagent_and_websocket_context_are_refused(
    changes: dict[str, object],
) -> None:
    with pytest.raises(worker.WorkerError, match="routing"):
        _validate(_outgoing(), context=_context(**changes))


def test_model_catalogue_access_requires_the_managed_read_only_route() -> None:
    context = _context(session_id=None)
    request = httpx.Request("GET", "https://api.githubcopilot.com/models")
    assert worker._is_model_catalog_request(request, context)
    assert not worker._is_model_catalog_request(request, _context())
    for url in (
        "https://api.githubcopilot.com/responses",
        "https://api.githubcopilot.com/models?destination=other",
        "https://api.githubcopilot.com.attacker.example/models",
        "http://api.githubcopilot.com/models",
    ):
        assert not worker._is_model_catalog_request(httpx.Request("GET", url), context)
    assert not worker._is_model_catalog_request(
        httpx.Request("POST", "https://api.githubcopilot.com/models"), context
    )


@pytest.mark.parametrize(
    ("url", "method", "content", "changes", "allowed"),
    [
        ("https://api.enterprise.githubcopilot.com/meta", "GET", b"", {}, True),
        ("https://api.githubcopilot.com:443/meta", "GET", b"", {}, True),
        ("http://api.githubcopilot.com/meta", "GET", b"", {}, False),
        ("https://api.githubcopilot.com:8443/meta", "GET", b"", {}, False),
        ("https://user@api.githubcopilot.com/meta", "GET", b"", {}, False),
        ("https://user:password@api.githubcopilot.com/meta", "GET", b"", {}, False),
        ("https://api.githubcopilot.com.attacker.example/meta", "GET", b"", {}, False),
        ("https://githubcopilot.com/meta", "GET", b"", {}, False),
        ("https://api.githubcopilot.com/meta?destination=other", "GET", b"", {}, False),
        ("https://api.githubcopilot.com/meta#other", "GET", b"", {}, False),
        ("https://api.githubcopilot.com/meta/", "GET", b"", {}, False),
        ("https://api.githubcopilot.com/models", "GET", b"", {}, False),
        ("https://api.githubcopilot.com/meta", "POST", b"", {}, False),
        ("https://api.githubcopilot.com/meta", "GET", b"private", {}, False),
        ("https://api.githubcopilot.com/meta", "GET", b"", {"session_id": None}, False),
        ("https://api.githubcopilot.com/meta", "GET", b"", {"session_id": "other"}, False),
        ("https://api.githubcopilot.com/meta", "GET", b"", {"parent_agent_id": "parent"}, False),
        ("https://api.githubcopilot.com/meta", "GET", b"", {"transport": "websocket"}, False),
    ],
)
def test_session_metadata_access_requires_strict_managed_route_and_selected_context(
    url: str, method: str, content: bytes, changes: dict[str, object], allowed: bool
) -> None:
    assert (
        worker._is_session_metadata_request(
            httpx.Request(method, url, content=content), _context(**changes), session_id="session-1"
        )
        is allowed
    )


@pytest.mark.parametrize(
    ("scenario", "catalog_calls", "inference_calls", "reason", "delays"),
    [
        ("connect-once", 2, 1, None, [0.5]),
        ("timeout-twice", 3, 1, None, [0.5, 1.0]),
        ("exhausted", 3, 0, "model-catalog-connection", [0.5, 1.0]),
        ("certificate", 1, 0, "model-catalog-tls", []),
        ("catalog-refused", 1, 0, "model-catalog-failed", []),
        ("duplicate-catalog", 1, 0, "model-catalog-failed", []),
        ("inference-failed", 1, 1, "inference-request-refused", []),
        ("cancelled", 1, 0, None, [0.5]),
        ("policy-revoked", 1, 0, "managed-policy-unverified", [0.5]),
    ],
)
def test_catalogue_recovery_is_bounded_and_never_replays_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    catalog_calls: int,
    inference_calls: int,
    reason: str | None,
    delays: list[float],
) -> None:
    request = _request(tmp_path)
    native = Path(request["nativeSession"])
    native.write_bytes(b'{"type":"session.start"}\n')
    native.chmod(0o600)
    recorded: dict[str, Any] = {"catalog": 0, "inference": 0, "delays": []}

    def emit(document: dict[str, object]) -> None:
        recorded["on_event"](SimpleNamespace(to_dict=lambda: document))

    async def pause(delay: float) -> None:
        recorded["delays"].append(delay)
        if scenario == "cancelled":
            raise asyncio.CancelledError
        if scenario == "policy-revoked":
            emit(_permission_event(False))

    class Session:
        def __init__(self) -> None:
            self.rpc = self

        async def send_messages(self, message: SimpleNamespace) -> SimpleNamespace:
            assert message.messages == []
            emit(_permission_event())
            handler = recorded["handler"]
            catalog = httpx.Request("GET", "https://api.githubcopilot.com/models")
            await handler.send_request(catalog, _context(session_id=None))
            if scenario == "duplicate-catalog":
                await handler.send_request(catalog, _context(session_id=None))
            emit({"type": "system.message", "data": {"content": request["systemMessage"]}})
            emit({"type": "model.call_start", "data": {"model": request["model"]}})
            await handler.send_request(_outgoing(), _context())
            emit({"type": "assistant.idle", "data": {}})
            return SimpleNamespace(message_ids=[])

    class Client:
        def __init__(self, **options: Any) -> None:
            recorded["handler"] = options["request_handler"]

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            recorded["stopped"] = True

        async def resume_session(self, session_id: str, **options: Any) -> Session:
            recorded["on_event"] = options["on_event"]
            return Session()

    class Handler:
        async def send_request(self, outgoing: httpx.Request, context: object) -> httpx.Response:
            if outgoing.method == "GET":
                recorded["catalog"] += 1
                if scenario == "certificate":
                    try:
                        raise ssl.SSLCertVerificationError("fixture certificate refusal")
                    except ssl.SSLCertVerificationError as error:
                        raise httpx.ConnectError("fixture connection refusal") from error
                if scenario == "catalog-refused":
                    raise worker.WorkerError("fixture catalogue refusal")
                if scenario == "timeout-twice" and recorded["catalog"] < 3:
                    raise httpx.ConnectTimeout("fixture connection timeout")
                if scenario in {"exhausted", "cancelled", "policy-revoked"} or (
                    scenario == "connect-once" and recorded["catalog"] == 1
                ):
                    raise httpx.ConnectError("fixture connection refusal")
            else:
                recorded["inference"] += 1
                if scenario == "inference-failed":
                    raise httpx.ConnectError("fixture inference connection refusal")
            return httpx.Response(200, request=outgoing, content=b"{}")

    _install_sdk(monkeypatch, client=Client, handler=Handler)
    monkeypatch.setattr(worker.asyncio, "sleep", pause)
    invocation = worker._run_session(Path("/fixture/copilot"), request)
    if scenario == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(invocation)
    else:
        assert asyncio.run(invocation) == (0 if reason is None else 70)
    complete = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert recorded["catalog"] == complete["catalog_requests_started"] == catalog_calls
    assert recorded["inference"] == complete["provider_calls_started"] == inference_calls
    assert recorded["delays"] == delays
    assert recorded["stopped"] is True
    if reason is not None:
        assert complete["failure_code"] == reason
        assert complete["failure_stage"] == (
            "managed-policy"
            if scenario == "policy-revoked"
            else "inference-request"
            if scenario == "inference-failed"
            else "model-catalog"
        )


@pytest.mark.parametrize(
    ("scenario", "metadata_calls", "inference_calls", "reason"),
    [
        ("one", 1, 1, None),
        ("two", 2, 1, None),
        ("third", 2, 0, "session-metadata-failed"),
        ("connection", 1, 0, "session-metadata-connection"),
        ("certificate", 1, 0, "session-metadata-tls"),
        ("policy-refused", 0, 0, "managed-policy-unverified"),
        ("wrong-session", 0, 0, "native-context-unverified"),
        ("unknown-get", 0, 0, "native-context-unverified"),
        ("missing-model", 1, 0, "native-context-unverified"),
        ("wrong-system", 1, 0, "native-context-unverified"),
        ("after-inference", 1, 1, "session-metadata-failed"),
        ("second-inference", 1, 1, "inference-request-refused"),
    ],
)
def test_metadata_precedes_context_proof_with_independent_bounded_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    metadata_calls: int,
    inference_calls: int,
    reason: str | None,
) -> None:
    request = _request(tmp_path)
    native = Path(request["nativeSession"])
    native.write_bytes(b'{"type":"session.start"}\n')
    native.chmod(0o600)
    recorded: dict[str, Any] = {"metadata": 0, "inference": 0}
    token = "private-session-fixture-token"

    def emit(document: dict[str, object]) -> None:
        recorded["on_event"](SimpleNamespace(to_dict=lambda: document))

    class Session:
        def __init__(self) -> None:
            self.rpc = self

        async def send_messages(self, message: SimpleNamespace) -> SimpleNamespace:
            assert message.messages == []
            handler = recorded["handler"]
            metadata = httpx.Request("GET", "https://api.enterprise.githubcopilot.com/meta")
            context = _context(session_id="other") if scenario == "wrong-session" else _context()
            if scenario == "unknown-get":
                metadata = httpx.Request("GET", "https://api.githubcopilot.com/unknown")
            pending = asyncio.create_task(handler.send_request(metadata, context))
            await asyncio.sleep(0)
            assert recorded["metadata"] == recorded["inference"] == 0
            emit(_permission_event(scenario != "policy-refused"))
            await pending
            if scenario in {"two", "third"}:
                await handler.send_request(metadata, context)
            if scenario == "third":
                await handler.send_request(metadata, context)
            emit(
                {
                    "type": "system.message",
                    "data": {
                        "content": "wrong guidance"
                        if scenario == "wrong-system"
                        else request["systemMessage"]
                    },
                }
            )
            if scenario != "missing-model":
                emit({"type": "model.call_start", "data": {"model": request["model"]}})
            inference = asyncio.create_task(handler.send_request(_outgoing(), _context()))
            await asyncio.sleep(0)
            if scenario in {"missing-model", "wrong-system"}:
                assert recorded["inference"] == 0
                emit({"type": "assistant.idle", "data": {}})
            await inference
            if scenario == "after-inference":
                await handler.send_request(metadata, context)
            if scenario == "second-inference":
                await handler.send_request(_outgoing(), _context())
            emit({"type": "assistant.idle", "data": {}})
            return SimpleNamespace(message_ids=[])

    class Client:
        def __init__(self, **options: Any) -> None:
            assert "github_token" not in options
            recorded["handler"] = options["request_handler"]

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            recorded["stopped"] = True

        async def resume_session(self, session_id: str, **options: Any) -> Session:
            assert options["github_token"] == token
            assert options["suppress_resume_event"] is True
            recorded["on_event"] = options["on_event"]
            return Session()

    class Handler:
        async def send_request(self, outgoing: httpx.Request, context: object) -> httpx.Response:
            if outgoing.method == "GET":
                recorded["metadata"] += 1
                if scenario == "connection":
                    raise httpx.ConnectError(token)
                if scenario == "certificate":
                    raise ssl.SSLCertVerificationError(token)
            else:
                recorded["inference"] += 1
            return httpx.Response(200, request=outgoing, content=b"{}")

    _install_sdk(monkeypatch, client=Client, handler=Handler)

    async def run() -> int:
        return await asyncio.wait_for(
            worker._run_session(Path("/fixture/copilot"), request, github_token=token), timeout=2
        )

    assert asyncio.run(run()) == (0 if reason is None else 70)
    output = capsys.readouterr().out
    assert token not in output
    complete = json.loads(output.splitlines()[-1])
    assert recorded["metadata"] == complete["metadata_requests_started"] == metadata_calls
    assert recorded["inference"] == complete["provider_calls_started"] == inference_calls
    assert complete["catalog_requests_started"] == 0
    assert complete["failure_code"] == reason
    assert recorded["stopped"] is True
    assert native.read_bytes() == b'{"type":"session.start"}\n'


@pytest.mark.parametrize(
    ("event_kind", "exit_code", "forwards"),
    [
        ("assistant.idle", 0, 0),
        ("assistant.idle", 0, 1),
        ("assistant.idle", 70, 2),
        ("tool.execution_complete", 0, 1),
        ("shell-two", 0, 1),
        ("shell-followup-race", 0, 1),
        ("shell-no-action", 0, 1),
        ("shell-32", 0, 1),
        ("shell-missing", 70, 1),
        ("shell-unmatched", 70, 1),
        ("shell-success", 70, 1),
        ("shell-duplicate", 70, 1),
        ("shell-duplicate-request", 70, 1),
        ("shell-limit", 70, 1),
        ("future.essential", 70, 0),
        ("model.future_telemetry", 0, 0),
        ("session.future_telemetry", 0, 0),
        ("prompt_cache_break", 0, 1),
        ("sandbox.decision", 0, 0),
        ("user.message", 70, 0),
    ],
)
@pytest.mark.parametrize("policy_verified", [True, False])
@pytest.mark.parametrize(
    "native_context", ["verified", "missing-system", "wrong-system", "missing-model", "wrong-model"]
)
def test_sdk_worker_empty_batch_adds_no_messages_and_emits_native_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event_kind: str,
    exit_code: int,
    forwards: int,
    policy_verified: bool,
    native_context: str,
) -> None:
    context_verified = native_context == "verified"
    if (
        not policy_verified
        or native_context.startswith("wrong-")
        or (forwards and not context_verified)
    ):
        exit_code = 70
    request = _request(tmp_path)
    shell_event = event_kind == "tool.execution_complete" or event_kind.startswith("shell-")
    if shell_event:
        request["target"] = "shell-action"
    native = Path(request["nativeSession"])
    native.write_bytes(b'{"type":"session.start"}\n')
    native.chmod(0o600)
    recorded: dict[str, Any] = {}

    class Session:
        rpc: Session

        def __init__(self, callback: Callable[[SimpleNamespace], None]) -> None:
            self.rpc = self
            self.callback = callback

        async def send_messages(self, message: SimpleNamespace) -> SimpleNamespace:
            recorded["messages"] = message.messages
            outgoing = _outgoing()
            dispatch = (
                asyncio.create_task(
                    recorded["client"]["request_handler"].send_request(outgoing, _context())
                )
                if forwards
                else None
            )
            await asyncio.sleep(0)
            assert recorded.get("physical_forwards", 0) == 0
            options = recorded["resume"]
            options["on_event"](SimpleNamespace(to_dict=lambda: _permission_event(policy_verified)))
            await asyncio.sleep(0)
            proof_events = []
            if native_context != "missing-system":
                content = (
                    request["systemMessage"] if native_context != "wrong-system" else "changed"
                )
                proof_events.append(
                    {"type": "system.message", "data": {"content": content, "role": "system"}}
                )
            if native_context != "missing-model":
                model = request["model"] if native_context != "wrong-model" else "other"
                proof_events.append(
                    {"type": "model.call_start", "data": {"model": model, "turnId": "turn-1"}}
                )
            if not policy_verified:
                proof_events.reverse()
            for index, proof in enumerate(proof_events):
                self.callback(SimpleNamespace(to_dict=lambda proof=proof: proof))
                if index == 0:
                    await asyncio.sleep(0)
                    assert recorded.get("physical_forwards", 0) == 0
                    if dispatch is not None and native_context.startswith("verified"):
                        assert not dispatch.done()
            if native_context.startswith("missing-"):
                self.callback(
                    SimpleNamespace(to_dict=lambda: {"type": "assistant.idle", "data": {}})
                )
            if dispatch is not None:
                if policy_verified and context_verified:
                    await dispatch
                else:
                    with pytest.raises(worker.WorkerError):
                        await dispatch
            if forwards and policy_verified and context_verified:
                if event_kind == "shell-followup-race":
                    with pytest.raises(worker.WorkerError):
                        await recorded["client"]["request_handler"].send_request(
                            outgoing, _context()
                        )
                for _ in range(forwards - 1):
                    with pytest.raises(worker.WorkerError):
                        await recorded["client"]["request_handler"].send_request(
                            outgoing, _context()
                        )

            def emit(kind: str, data: dict[str, object]) -> None:
                self.callback(SimpleNamespace(to_dict=lambda: {"type": kind, "data": data}))

            if event_kind == "shell-no-action":
                emit(
                    "assistant.message",
                    {"content": "No shell action needed.", "toolRequests": None},
                )
                emit("assistant.idle", {})
            elif shell_event:
                count = (
                    33
                    if event_kind == "shell-limit"
                    else (
                        32
                        if event_kind == "shell-32"
                        else (2 if event_kind.startswith("shell-") else 1)
                    )
                )
                ids = [f"tool-{index}" for index in range(count)]
                if event_kind == "shell-duplicate-request":
                    ids[1] = ids[0]
                emit(
                    "assistant.message",
                    {
                        "toolRequests": [
                            {
                                "toolCallId": identity,
                                "name": "bash",
                                "arguments": {"command": "fixture"},
                            }
                            for identity in ids
                        ]
                    },
                )
                for identity in ids:
                    emit("tool.execution_start", {"toolCallId": identity, "toolName": "bash"})
                for index, identity in enumerate(ids):
                    if event_kind == "shell-missing" and index == 1:
                        continue
                    emit(
                        "tool.execution_complete",
                        {
                            "toolCallId": "unmatched"
                            if event_kind == "shell-unmatched"
                            else identity,
                            "success": event_kind == "shell-success",
                            "error": {"code": "denied"},
                        },
                    )
                    if event_kind == "shell-duplicate" and index == 0:
                        emit(
                            "tool.execution_complete",
                            {"toolCallId": identity, "success": False, "error": {"code": "denied"}},
                        )
                if event_kind in {"tool.execution_complete", "shell-two", "shell-32"}:
                    with pytest.raises(worker.WorkerError):
                        await recorded["client"]["request_handler"].send_request(
                            outgoing, _context()
                        )
                    emit("session.error", {})
                else:
                    emit("assistant.idle", {})
            else:
                emit(event_kind, {})
            if event_kind.startswith(("model.", "session.")) or event_kind in {
                "sandbox.decision",
                "prompt_cache_break",
            }:
                self.callback(
                    SimpleNamespace(to_dict=lambda: {"type": "assistant.idle", "data": {}})
                )
            return SimpleNamespace(message_ids=[])

    class Client:
        def __init__(self, **options: object) -> None:
            recorded["client"] = options

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def resume_session(self, session_id: str, **options: Any) -> Session:
            recorded["resume"] = options
            recorded["session_id"] = session_id
            return Session(options["on_event"])

    class Handler:
        async def send_request(self, request: object, context: object) -> None:
            recorded["physical_forwards"] = recorded.get("physical_forwards", 0) + 1

    _install_sdk(monkeypatch, client=Client, handler=Handler)
    assert asyncio.run(worker._run_session(Path("/fixture/copilot"), request)) == exit_code
    assert recorded["messages"] == []
    assert recorded["session_id"] == "session-1"
    assert recorded["client"]["mode"] == "empty"
    permissions = recorded["resume"]["managed_settings"].permissions
    assert permissions.disable_bypass_permissions_mode == "disable"
    assert permissions.disable_assisted_permissions_mode is True
    assert permissions.deny == ["Shell(*)"]
    assert permissions.allow == permissions.limit_to == []
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "ithrl.copilot.input"
    assert events[-1]["type"] == "ithrl.copilot.complete"
    assert events[-1]["success"] is (exit_code == 0)
    if exit_code == 0 and event_kind in {"shell-two", "shell-32"}:
        completions = [event for event in events if event["type"] == "tool.execution_complete"]
        assert len(completions) == (32 if event_kind == "shell-32" else 2)
        assert len({event["data"]["toolCallId"] for event in completions}) == len(completions)
    actual_forwards = min(forwards, 1) if policy_verified and context_verified else 0
    assert events[-1]["provider_calls_started"] == actual_forwards
    assert recorded.get("physical_forwards", 0) == actual_forwards
    rejected = (
        max(forwards - 1, 0)
        + (event_kind in {"tool.execution_complete", "shell-two", "shell-32"})
        + (event_kind == "shell-followup-race")
    )
    assert events[-1]["requests_rejected"] == (
        rejected if policy_verified and context_verified else 0
    )
    assert "Captured fixture guidance" not in repr(
        [event for event in events if event["type"] != "system.message"]
    )
