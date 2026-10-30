from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ithrl.runner.copilot import worker


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
    assert options["autopilot"] == {"continuation_enabled": False, "max_continues": 0}
    for name in (
        "enable_config_discovery",
        "enable_managed_settings",
        "request_extensions",
        "request_canvas_renderer",
    ):
        assert options[name] is False
    assert options["capi"]["enable_web_socket_responses"] is False


def _outgoing(*, url: str = "https://api.githubcopilot.com/responses") -> httpx.Request:
    # Managed provider encoding belongs to the SDK, including non-JSON bodies.
    return httpx.Request("POST", url, content=b"opaque SDK provider encoding")


def _context(**changes: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{"session_id": "session-1", "parent_agent_id": None, "transport": "http", **changes}
    )


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
            options["on_event"](
                SimpleNamespace(
                    to_dict=lambda: {
                        "type": "session.managed_settings_resolved",
                        "data": {
                            "clientManaged": policy_verified,
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
                )
            )
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
            if event_kind.startswith(("model.", "session.")) or event_kind == "sandbox.decision":
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

    class Connection:
        @staticmethod
        def for_stdio(**options: object) -> dict[str, object]:
            return options

    monkeypatch.setitem(
        sys.modules,
        "copilot",
        SimpleNamespace(
            CopilotClient=Client,
            CopilotRequestHandler=Handler,
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
