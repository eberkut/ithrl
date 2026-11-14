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
        ("model-catalog-connection", "model-catalog-connection"),
        ("model-catalog-tls", "model-catalog-tls"),
        ("model-catalog-failed", "model-catalog-failed"),
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


@pytest.mark.parametrize("matching", [False, True])
def test_call_finished_usage_must_agree_with_earlier_native_usage(matching: bool) -> None:
    events = _events(usage=True)
    usage_index = next(i for i, event in enumerate(events) if event["type"] == "assistant.usage")
    usage_event = events.pop(usage_index)
    finish_index = next(
        i for i, event in enumerate(events) if event["type"] == "model.call_finished"
    )
    events[finish_index]["data"]["usage"] = {
        "inputTokens": 100,
        "outputTokens": 12 if matching else 13,
        "cacheReadTokens": 3,
    }
    events.insert(finish_index, usage_event)

    if matching:
        assert _parse(events).usage == TokenUsage(
            input_tokens=100, output_tokens=12, cached_input_tokens=3
        )
    else:
        with pytest.raises(CopilotProtocolError) as caught:
            _parse(events)
        assert caught.value.code == "usage-mismatch"


def test_usage_without_counts_does_not_mask_a_later_token_report() -> None:
    events = _events(usage=True)
    usage_index = next(i for i, event in enumerate(events) if event["type"] == "assistant.usage")
    events.insert(usage_index, _event("assistant.usage", model=MODEL))

    assert _parse(events).usage == TokenUsage(
        input_tokens=100, output_tokens=12, cached_input_tokens=3
    )


@pytest.mark.parametrize("malformed_tail", [False, True])
def test_truncated_native_output_preserves_the_recorded_provider_charge(
    malformed_tail: bool,
) -> None:
    events = _events()[:7] if malformed_tail else _events()[:-1]
    raw = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    if malformed_tail:
        raw += b'{"truncated\n'

    with pytest.raises(CopilotProtocolError) as caught:
        parse_output(raw, expected="final-response", native_session=NATIVE, system_message=SYSTEM)
    assert caught.value.code == ("invalid-json" if malformed_tail else "missing-receipt")
    assert physical_forward_count(raw) == 1


def test_denied_action_can_drain_shutdown_events_without_another_dispatch() -> None:
    events = _events(shell_target=True)
    events[-1:-1] = [
        _event("assistant.turn_end", turnId=TURN),
        _event("assistant.turn_start", turnId="shutdown-turn"),
        _event("assistant.idle"),
        _event("session.idle"),
        _event("session.usage_checkpoint"),
        _event("session.background_tasks_changed"),
        _event("assistant.usage", model=MODEL, inputTokens=5, outputTokens=2),
    ]

    outcome = _parse(events, shell_target=True)
    assert outcome.actions_requested == outcome.actions_denied == 1
    assert outcome.actions_executed == 0
    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert outcome.usage == TokenUsage(input_tokens=5, output_tokens=2)


@pytest.mark.parametrize(
    "followup",
    [
        _event("session.background_tasks_changed", tasks=["unexpected"]),
        _event(
            "assistant.message", messageId="after-denial", turnId=TURN, model=MODEL, content="extra"
        ),
        _event("model.call_start", turnId="next-turn", model=MODEL),
    ],
)
def test_denied_action_refuses_further_work(followup: dict[str, Any]) -> None:
    events = _events(shell_target=True)
    events.insert(-1, followup)
    with pytest.raises(CopilotProtocolError) as caught:
        _parse(events, shell_target=True)
    assert caught.value.code == "continued-after-denial"


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
    ("requests", "code"),
    [
        ("invalid-request-list", "invalid-message"),
        ([None], "invalid-action"),
        ([{"toolCallId": "call-1", "name": "network", "arguments": {}}], "unsupported-action"),
        ([{"toolCallId": "call-1", "name": "bash", "arguments": "pwd"}], "invalid-action"),
        (
            [{"toolCallId": "call-1", "name": "bash", "arguments": {"command": "x" * 70000}}],
            "invalid-action",
        ),
        ([{"toolCallId": "call-1", "name": "bash", "arguments": {}}] * 2, "action-mismatch"),
        ([{"toolCallId": "call-1", "name": "bash", "arguments": {}}] * 65, "action-limit"),
    ],
)
def test_shell_request_validation_refuses_invalid_or_unbounded_actions(
    requests: object, code: str
) -> None:
    events = _events(shell_target=True)
    message = next(event for event in events if event["type"] == "assistant.message")
    message["data"]["toolRequests"] = requests
    with pytest.raises(CopilotProtocolError) as refused:
        _parse(events, shell_target=True)
    assert refused.value.code == code


def test_returned_system_message_must_match_captured_input() -> None:
    events = _events()
    system = next(event for event in events if event["type"] == "system.message")
    system["data"]["content"] = "Changed fixture guidance"
    with pytest.raises(CopilotProtocolError) as refused:
        _parse(events)
    assert refused.value.code == "input-mismatch"


def test_reasoning_identity_metadata_is_bound_to_the_selected_turn_and_model() -> None:
    events = _events()
    events.insert(
        8,
        _event(
            "assistant.reasoning",
            content="Fixture reasoning",
            reasoningId="reasoning-1",
            model=MODEL,
            turnId=TURN,
        ),
    )
    assert _parse(events).final_text == "Fixture complete."


def test_continuation_refuses_an_unrequested_user_message() -> None:
    events = _events()
    events.insert(4, _event("user.message", content="Private unrequested fixture input"))
    with pytest.raises(CopilotProtocolError) as refused:
        _parse(events)
    assert refused.value.code == "unexpected-user-message"


def test_startup_events_cannot_restart_an_active_turn() -> None:
    events = _events()
    events.insert(8, _event("session.resume", eventCount=3))
    with pytest.raises(CopilotProtocolError) as refused:
        _parse(events)
    assert refused.value.code == "invalid-sequence"


def test_denial_drain_retains_bounded_continuation_lifecycle_without_more_dispatch() -> None:
    events = _events(shell_target=True)
    events[-1:-1] = [
        _event("assistant.turn_end", turnId=TURN),
        _event("assistant.turn_start", turnId="next-turn"),
        _event("session.usage_checkpoint"),
        _event("session.idle"),
    ]
    result = _parse(events, shell_target=True)
    assert result.actions_requested == result.actions_denied == 1
    assert result.provider_calls_started == 1


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
        "assistant.usage",
    ],
)
def test_duplicate_physical_or_native_dispatch_cannot_be_accepted(kind: str) -> None:
    events = _events(usage=kind == "assistant.usage")
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
        b"\n",
        b'{"type":"assistant.idle","agentId":"other-agent"}\n',
        b'{"type":"assistant.idle","data":null}\n',
        b'{"type":"assistant.idle","type":"assistant.idle"}\n',
        b'{"type":"assistant.idle","data":{"value":NaN}}\n',
        b"[]\n",
        b"x" * (MAX_OUTPUT_LINE_BYTES + 1),
    ],
)
def test_decoder_bounds_and_strict_json_fail_safely(raw: bytes) -> None:
    with pytest.raises(CopilotProtocolError):
        parse_output(raw, expected="final-response")


def test_decoder_refuses_output_and_structure_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from ithrl.runner.copilot.protocol import decoding

    raw = b'{"type":"assistant.idle","data":{}}\n'
    assert decoding.decode_events(raw) == [{"type": "assistant.idle", "data": {}}]
    with monkeypatch.context() as context:
        context.setattr(decoding, "MAX_OUTPUT_BYTES", len(raw) - 1)
        with pytest.raises(CopilotProtocolError) as caught:
            decoding.decode_events(raw)
        assert caught.value.code == "output-limit"
    monkeypatch.setattr(decoding, "MAX_EVENT_ITEMS", 1)
    with pytest.raises(CopilotProtocolError) as caught:
        decoding.decode_events(raw)
    assert caught.value.code == "event-limit"


def test_protocol_field_helpers_refuse_malformed_values_without_exposing_them() -> None:
    from ithrl.runner.copilot.protocol.decoding import event_data
    from ithrl.runner.copilot.protocol.state import bounded_string, parse_usage

    assert parse_usage({}) is None
    assert bounded_string("", max_length=1, allow_empty=True) == ""
    for value in (None, "", "private-fixture-value"):
        with pytest.raises(CopilotProtocolError) as caught:
            bounded_string(value, max_length=1)
        assert caught.value.code == "invalid-field"
        assert "private-fixture-value" not in str(caught.value)
    with pytest.raises(CopilotProtocolError) as caught:
        parse_usage(None)
    assert caught.value.code == "invalid-usage"
    with pytest.raises(CopilotProtocolError) as caught:
        event_data({"data": None})
    assert caught.value.code == "invalid-event"


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
