from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from ithrl.runner.contracts import TokenUsage, ToolCall
from ithrl.runner.copilot.protocol import CopilotProtocolError, parse_output
from ithrl.runner.copilot.protocol.contracts import MAX_OUTPUT_EVENTS, MAX_OUTPUT_LINE_BYTES
from ithrl.runner.copilot.protocol.parser import physical_forward_count

MODEL = "fixture-model"
TURN = "fixture-turn"
NATIVE = b'{"type":"session.start"}\n'
SYSTEM = "Captured fixture guidance"


def _event(kind: str, **data: object) -> dict[str, Any]:
    return {"type": kind, "data": data}


def _receipt(kind: str, **fields: object) -> dict[str, Any]:
    return {"type": f"ithrl.copilot.{kind}", "schema_version": 1, **fields}


def _input() -> dict[str, Any]:
    return _receipt(
        "input",
        native_session_sha256=hashlib.sha256(NATIVE).hexdigest(),
        native_session_bytes=len(NATIVE),
        system_message_sha256=hashlib.sha256(SYSTEM.encode()).hexdigest(),
        new_user_messages=0,
    )


def _complete() -> dict[str, Any]:
    return _receipt(
        "complete",
        provider_calls_started=1,
        requests_rejected=0,
        websockets_rejected=0,
        permissions_denied=0,
        request_sha256="a" * 64,
        success=True,
    )


def _events(*, shell_target: bool = False, usage: bool = False) -> list[dict[str, Any]]:
    events = [
        _input(),
        _event("session.resume", eventCount=3),
        _event("system.message", content=SYSTEM),
        _event("pending_messages.modified"),
        _event("assistant.turn_start", turnId=TURN),
        _event("model.call_start", turnId=TURN, model=MODEL),
        _receipt("forward", provider_calls_started=1, request_sha256="a" * 64),
        _event("model.call_finished", turnId=TURN, outcome="success"),
    ]
    if usage:
        events.append(
            _event(
                "assistant.usage", model=MODEL, inputTokens=100, outputTokens=12, cacheReadTokens=3
            )
        )
    request = {"name": "bash", "toolCallId": "call-1", "arguments": {"command": "pwd"}}
    events.append(
        _event(
            "assistant.message",
            messageId="message-1",
            turnId=TURN,
            model=MODEL,
            content="" if shell_target else "Fixture complete.",
            toolRequests=[request] if shell_target else [],
        )
    )
    events.append(_event("model.call_final_result", model=MODEL, result="success", isByok=False))
    if shell_target:
        events.extend(
            [
                _event(
                    "tool.execution_start",
                    toolCallId="call-1",
                    toolName="bash",
                    turnId=TURN,
                    model=MODEL,
                    arguments=request["arguments"],
                ),
                _event(
                    "tool.execution_complete",
                    toolCallId="call-1",
                    turnId=TURN,
                    model=MODEL,
                    success=False,
                    error={"code": "denied"},
                ),
            ]
        )
    else:
        events.extend(
            [
                _event("assistant.turn_end", turnId=TURN),
                _event("assistant.idle"),
                _event("session.idle"),
            ]
        )
    return [*events, _complete()]


def _parse(events: list[dict[str, Any]], *, shell_target: bool = False):
    return parse_output(
        b"".join(json.dumps(e).encode() + b"\n" for e in events),
        expected="shell-action" if shell_target else "final-response",
        native_session=NATIVE,
        system_message=SYSTEM,
    )


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("native-context-unverified", "native-context-unverified"),
        ("private-provider-error", "incomplete-dispatch"),
        (["untrusted"], "incomplete-dispatch"),
    ],
)
def test_worker_error_reports_only_known_diagnostic_codes(reason: object, expected: str) -> None:
    events = _events()
    events[-1].update(success=False, failure_code=reason)
    with pytest.raises(CopilotProtocolError) as caught:
        _parse(events)
    assert caught.value.code == expected
    assert "private-provider-error" not in str(caught.value)


def test_native_final_response_needs_no_invented_user_or_cli_result() -> None:
    outcome = _parse(_events())
    assert outcome.final_text == "Fixture complete."
    assert outcome.model_returned == MODEL
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert outcome.usage is None


@pytest.mark.parametrize("shell_target", [False, True])
def test_one_inference_can_emit_multiple_distinct_assistant_messages(shell_target: bool) -> None:
    events = _events(shell_target=shell_target)
    index = next(i for i, event in enumerate(events) if event["type"] == "assistant.message")
    events[index:index] = [
        _event(
            "assistant.message",
            messageId="reasoning-message",
            turnId=TURN,
            model=MODEL,
            content="",
            reasoningText="Consider the request.",
        ),
        _event(
            "assistant.message",
            messageId="commentary-message",
            turnId=TURN,
            model=MODEL,
            content="Checking the result.",
        ),
    ]
    outcome = _parse(events, shell_target=shell_target)
    assert outcome.provider_calls_completed == 1
    if shell_target:
        assert outcome.actions_denied == 1
    else:
        assert outcome.final_text == "Fixture complete."


def test_native_usage_is_preserved_and_optional_counts_remain_unknown() -> None:
    assert _parse(_events(usage=True)).usage == TokenUsage(
        input_tokens=100, output_tokens=12, cached_input_tokens=3
    )
    events = _events()
    events.insert(-1, _event("assistant.usage", model=MODEL))
    assert _parse(events).usage is None


@pytest.mark.parametrize("blocked_followups", [0, 1])
def test_visible_native_denial_proves_no_action_execution(blocked_followups: int) -> None:
    events = _events(shell_target=True)
    events[-1]["requests_rejected"] = blocked_followups
    outcome = _parse(events, shell_target=True)
    assert outcome.tool_calls == (ToolCall(name="bash", arguments={"command": "pwd"}),)
    assert outcome.actions_requested == outcome.actions_denied == 1
    assert outcome.actions_executed == 0
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1


@pytest.mark.parametrize("missing_denial", [False, True])
def test_parallel_shell_actions_require_every_explicit_denial(missing_denial: bool) -> None:
    events = _events(shell_target=True)
    message = next(event["data"] for event in events if event["type"] == "assistant.message")
    message["content"] = "Inspect both parts of the working changes."
    message["toolRequests"].append(
        {"name": "bash", "toolCallId": "call-2", "arguments": {"command": "git diff"}}
    )
    start = next(i for i, event in enumerate(events) if event["type"] == "tool.execution_start")
    events.insert(
        start + 1,
        _event(
            "tool.execution_start",
            toolCallId="call-2",
            toolName="bash",
            turnId=TURN,
            model=MODEL,
            arguments={"command": "git diff"},
        ),
    )
    if not missing_denial:
        # Completions may arrive in a different order from the requests.
        events.insert(
            start + 2,
            _event(
                "tool.execution_complete",
                toolCallId="call-2",
                turnId=TURN,
                model=MODEL,
                success=False,
                error={"code": "denied"},
            ),
        )
        outcome = _parse(events, shell_target=True)
        assert outcome.tool_calls == (
            ToolCall(name="bash", arguments={"command": "pwd"}),
            ToolCall(name="bash", arguments={"command": "git diff"}),
        )
        assert outcome.actions_requested == outcome.actions_denied == 2
        assert outcome.actions_executed == 0
    else:
        with pytest.raises(CopilotProtocolError, match="complete denied shell action"):
            _parse(events, shell_target=True)


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (0, "native_session_sha256", "a" * 64),
        (0, "native_session_bytes", 999),
        (0, "system_message_sha256", "b" * 64),
        (0, "new_user_messages", 1),
        (0, "schema_version", 2),
        (0, "schema_version", True),
        (0, "schema_version", 1.0),
        (0, "new_user_messages", False),
        (0, "new_user_messages", 0.0),
        (0, "native_session_bytes", float(len(NATIVE))),
        (6, "schema_version", True),
        (6, "schema_version", 1.0),
        (6, "provider_calls_started", True),
        (6, "provider_calls_started", 1.0),
        (-1, "schema_version", True),
        (-1, "schema_version", 1.0),
        (-1, "provider_calls_started", True),
        (-1, "provider_calls_started", 1.0),
        (-1, "websockets_rejected", False),
        (-1, "websockets_rejected", 0.0),
        (-1, "provider_calls_started", 2),
        (-1, "requests_rejected", 1),
        (-1, "websockets_rejected", 1),
        (-1, "success", False),
    ],
)
def test_input_and_dispatch_receipts_are_bound_and_strict(
    index: int, field: str, value: object
) -> None:
    events = _events()
    events[index][field] = value
    with pytest.raises(CopilotProtocolError):
        _parse(events)


@pytest.mark.parametrize(
    "kind",
    [
        "ithrl.copilot.input",
        "ithrl.copilot.forward",
        "ithrl.copilot.complete",
        "model.call_start",
        "model.call_finished",
        "assistant.message",
        "assistant.turn_end",
        "assistant.idle",
    ],
)
def test_missing_dispatch_or_native_completion_fails_closed(kind: str) -> None:
    events = [e for e in _events() if e["type"] != kind]
    with pytest.raises(CopilotProtocolError):
        _parse(events)


@pytest.mark.parametrize(
    "kind",
    [
        "ithrl.copilot.forward",
        "model.call_start",
        "model.call_finished",
        "assistant.message",
        "assistant.turn_start",
    ],
)
def test_duplicate_physical_or_native_dispatch_cannot_be_accepted(kind: str) -> None:
    events = _events()
    index = next(i for i, e in enumerate(events) if e["type"] == kind)
    events.insert(index, events[index])
    with pytest.raises(CopilotProtocolError):
        _parse(events)


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("user.message", {"content": "invented"}),
        ("session.handoff", {}),
        ("tool.execution_start", {}),
        ("arbitrary.native.side-effect", {}),
    ],
)
def test_new_user_messages_and_unknown_essential_events_are_refused(
    kind: str, data: dict[str, object]
) -> None:
    events = _events()
    events.insert(-1, {"type": kind, "data": data})
    with pytest.raises(CopilotProtocolError):
        _parse(events)


@pytest.mark.parametrize("wrong_type", [bool, float])
@pytest.mark.parametrize(
    ("index", "field", "calls", "value"),
    [
        (0, "schema_version", 1, 1),
        (1, "schema_version", 1, 1),
        (1, "provider_calls_started", 1, 1),
        (-1, "schema_version", 0, 1),
        (-1, "provider_calls_started", 0, 0),
    ],
)
def test_recovery_counters_require_exact_integers(
    index: int, field: str, calls: int, value: int, wrong_type: type[bool] | type[float]
) -> None:
    events = [_input()]
    if calls:
        events.append(_receipt("forward", provider_calls_started=1, request_sha256="a" * 64))
    events.append(_complete())
    events[-1]["provider_calls_started"] = calls

    def output() -> bytes:
        return b"".join(json.dumps(event).encode() + b"\n" for event in events)

    assert physical_forward_count(output()) == calls
    events[index][field] = wrong_type(value)
    assert physical_forward_count(output()) is None


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("model.call_finished", "outcome", "failure"),
        ("assistant.message", "turnId", "different"),
        ("assistant.message", "model", "different"),
        ("model.call_final_result", "isByok", True),
        ("model.call_final_result", "result", "failure"),
    ],
)
def test_provider_and_selected_turn_identity_are_native_evidence(
    kind: str, field: str, value: object
) -> None:
    events = _events()
    next(e for e in events if e["type"] == kind)["data"][field] = value
    with pytest.raises(CopilotProtocolError):
        _parse(events)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"errorCode": "quota_exceeded", "statusCode": 402}, "provider-quota-exceeded"),
        ({"statusCode": 401}, "provider-authentication-required"),
        ({"statusCode": 402}, "provider-payment-required"),
        ({"statusCode": 403}, "provider-access-denied"),
        ({"statusCode": 429}, "provider-rate-limited"),
        ({"statusCode": 503}, "provider-unavailable"),
        ({"errorCode": "private-identifier"}, "session-error"),
        ({"errorCode": "quota_exceeded", "statusCode": "402"}, "session-error"),
        ({"statusCode": True}, "session-error"),
    ],
)
def test_provider_failure_diagnostics_preserve_dispatch_and_exclude_native_text(
    data: dict[str, object], expected: str
) -> None:
    events = _events()[:7]
    complete = _complete()
    complete["success"] = False
    events.extend(
        [
            _event("model.call_failure", **data, errorMessage="private-error-message"),
            _event("session.error", message="private-session-message"),
            complete,
        ]
    )
    with pytest.raises(CopilotProtocolError) as error:
        _parse(events)
    assert error.value.code == expected
    assert "private" not in str(error.value)
    output = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    assert physical_forward_count(output) == 1


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("tool.execution_start", "arguments", {"command": "changed"}),
        ("tool.execution_start", "toolName", "network"),
        ("tool.execution_complete", "success", True),
        ("tool.execution_complete", "error", {"code": "other"}),
        ("tool.execution_complete", "toolCallId", "different"),
        ("tool.execution_complete", "turnId", "different"),
    ],
)
def test_action_identity_and_explicit_denial_are_required(
    kind: str, field: str, value: object
) -> None:
    events = _events(shell_target=True)
    next(e for e in events if e["type"] == kind)["data"][field] = value
    with pytest.raises(CopilotProtocolError):
        _parse(events, shell_target=True)


def test_native_usage_can_arrive_after_the_message() -> None:
    events = _events()
    events.insert(-1, _event("assistant.usage", model=MODEL, inputTokens=42, outputTokens=7))
    assert _parse(events).usage == TokenUsage(input_tokens=42, output_tokens=7)


@pytest.mark.parametrize(
    "usage",
    [
        {"inputTokens": True, "outputTokens": 0},
        {"inputTokens": 1},
        {"inputTokens": 1, "outputTokens": -1},
        {"inputTokens": 1, "outputTokens": 0, "cacheReadTokens": True},
    ],
)
def test_malformed_usage_is_not_replaced_with_zero(usage: dict[str, object]) -> None:
    events = _events()
    events.insert(-1, _event("assistant.usage", model=MODEL, **usage))
    with pytest.raises(CopilotProtocolError, match="usage"):
        _parse(events)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'{"type":"assistant.idle","type":"assistant.idle"}\n',
        b'{"type":"assistant.idle","data":{"value":NaN}}\n',
        b"[]\n",
        b"x" * (MAX_OUTPUT_LINE_BYTES + 1),
    ],
)
def test_decoder_bounds_and_strict_json_fail_safely(raw: bytes) -> None:
    with pytest.raises(CopilotProtocolError):
        parse_output(raw, expected="final-response")


def test_model_telemetry_is_preserved_without_substituting_lifecycle_evidence() -> None:
    events = _events()
    events.insert(4, _event("model.turn_started", model=MODEL, turn=1))
    events.insert(5, _event("sandbox.decision", decision="informational"))
    events.insert(6, _event("model.call_failure", statusCode=503))
    events.insert(-1, _event("model.future_telemetry", opaque=True))
    assert _parse(events).final_text == "Fixture complete."
    events = [event for event in events if event["type"] != "model.call_finished"]
    with pytest.raises(CopilotProtocolError):
        _parse(events)


def test_verbose_native_telemetry_keeps_a_finite_record_bound() -> None:
    events = _events()
    events[-1:-1] = [_event("model.telemetry") for _ in range(MAX_OUTPUT_EVENTS - len(events))]
    assert _parse(events).final_text == "Fixture complete."
    events.insert(-1, _event("model.telemetry"))
    with pytest.raises(CopilotProtocolError) as refused:
        _parse(events)
    assert refused.value.code == "event-limit"
