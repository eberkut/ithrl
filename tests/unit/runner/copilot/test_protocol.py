from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from typing import Any

import pytest

from ithrl.runner.contracts import TokenUsage, ToolCall
from ithrl.runner.copilot.protocol import (
    MAX_OUTPUT_LINE_BYTES,
    CopilotProtocolError,
    CopilotProtocolOutcome,
    is_denied_tool_completion,
    parse_output,
)

TURN_ID = "turn-fixture"
MODEL = "fixture-model"
TOOL_CALL_ID = "call-fixture"
FINAL_TEXT = "Fixture complete."
type Event = dict[str, Any]


def _event(event_type: str, data: Event) -> Event:
    return {
        "id": f"event-{event_type}",
        "parentId": None,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "type": event_type,
        "data": data,
    }


def _prefix(*, usage: Event | None = None) -> list[Event]:
    events = [
        _event("session.skills_loaded", {"count": 0}),
        _event("session.mcp_servers_loaded", {"count": 0}),
        _event("session.tools_updated", {"tools": ["bash"]}),
        _event("session.info", {"configuration": {"tools": "filtered"}}),
        _event("user.message", {"turnId": TURN_ID, "content": "Fixture prompt."}),
        _event("assistant.turn_start", {"turnId": TURN_ID, "interactionId": "interaction"}),
        _event("model.call_start", {"turnId": TURN_ID, "model": MODEL}),
    ]
    if usage is not None:
        events.append(_event("assistant.usage", usage))
    events.append(
        _event(
            "model.call_finished",
            {"turnId": TURN_ID, "outcome": "success", "dispatchDurationMs": 10},
        )
    )
    return events


def _final_events(*, usage: Event | None = None) -> list[Event]:
    return [
        *_prefix(usage=usage),
        _event(
            "assistant.message",
            {
                "messageId": "message-fixture",
                "apiCallId": "api-call-fixture",
                "model": MODEL,
                "content": FINAL_TEXT,
                "toolRequests": [],
                "turnId": TURN_ID,
            },
        ),
        _event("assistant.turn_end", {"turnId": TURN_ID}),
        {**_event("assistant.idle", {}), "ephemeral": True},
        {
            "type": "result",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "sessionId": "session-fixture",
            "exitCode": 0,
            "usage": {
                "premiumRequests": 0,
                "totalApiDurationMs": 10,
                "sessionDurationMs": 20,
                "codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": 0},
            },
        },
    ]


def _request() -> Event:
    return {
        "toolCallId": TOOL_CALL_ID,
        "name": "bash",
        "arguments": {"command": "pwd", "description": "Show the directory."},
        "type": "function",
    }


def _shell_events() -> list[Event]:
    request = _request()
    return [
        *_prefix(),
        _event(
            "assistant.message",
            {
                "messageId": "message-fixture",
                "apiCallId": "api-call-fixture",
                "model": MODEL,
                "content": "",
                "toolRequests": [request],
                "turnId": TURN_ID,
            },
        ),
        _event(
            "tool.execution_start",
            {
                "toolCallId": TOOL_CALL_ID,
                "toolName": "bash",
                "arguments": request["arguments"],
                "turnId": TURN_ID,
                "model": MODEL,
            },
        ),
        _event(
            "tool.execution_complete",
            {
                "toolCallId": TOOL_CALL_ID,
                "model": MODEL,
                "turnId": TURN_ID,
                "success": False,
                "error": {"message": "Permission denied.", "code": "denied"},
                "toolTelemetry": {"properties": {"shell_error_category": "permission_denied"}},
            },
        ),
    ]


def _jsonl(events: Iterable[object]) -> bytes:
    return b"".join(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for event in events
    )


def test_parse_final_response_proves_normal_completion() -> None:
    outcome = parse_output(_jsonl(_final_events()), expected="final-response")

    assert outcome == CopilotProtocolOutcome(
        type="final-response",
        model_returned=MODEL,
        provider_calls_started=1,
        provider_calls_completed=1,
        final_text=FINAL_TEXT,
    )


def test_parse_final_response_preserves_reported_token_usage() -> None:
    usage = {
        "model": MODEL,
        "inputTokens": 100,
        "outputTokens": 12,
        "cacheReadTokens": 40,
    }

    outcome = parse_output(_jsonl(_final_events(usage=usage)), expected="final-response")

    assert outcome.usage == TokenUsage(
        input_tokens=100,
        output_tokens=12,
        cached_input_tokens=40,
    )


@pytest.mark.parametrize("usage", [None, {"model": MODEL, "inputTokens": 100, "outputTokens": 12}])
def test_optional_completion_and_session_telemetry_do_not_add_calls_or_token_usage(
    usage: Event | None,
) -> None:
    events = _final_events(usage=usage)
    events.insert(
        -3,
        _event("model.call_final_result", {"model": MODEL, "result": "success", "isByok": False}),
    )
    events.insert(
        -2,
        _event(
            "session.usage_checkpoint",
            {"premiumRequests": 10, "inputTokens": 999, "outputTokens": 999, "modelCacheState": []},
        ),
    )

    outcome = parse_output(_jsonl(events), expected="final-response")

    assert outcome.provider_calls_started == outcome.provider_calls_completed == 1
    assert outcome.usage == (TokenUsage(input_tokens=100, output_tokens=12) if usage else None)
    assert outcome.final_text == FINAL_TEXT


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"model": "different-model", "result": "success", "isByok": False}, "model-mismatch"),
        ({"model": MODEL, "result": "failure", "isByok": False}, "provider-failure"),
        ({"model": MODEL, "result": "success", "isByok": True}, "provider-failure"),
    ],
)
def test_optional_call_completion_cannot_override_model_or_provider_evidence(
    fields: Event, code: str
) -> None:
    events = _final_events()
    events.insert(-3, _event("model.call_final_result", fields))

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="final-response")

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("event_type", "position"),
    [
        ("model.call_final_result", len(_prefix()) - 1),
        ("model.call_final_result", -2),
        ("session.usage_checkpoint", -3),
        ("session.usage_checkpoint", -1),
    ],
)
def test_optional_telemetry_requires_its_valid_lifecycle_position(
    event_type: str, position: int
) -> None:
    events = _final_events()
    events.insert(
        position, _event(event_type, {"model": MODEL, "result": "success", "isByok": False})
    )

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="final-response")

    assert raised.value.code == "invalid-sequence"


def test_parse_shell_action_requires_matching_denied_execution() -> None:
    outcome = parse_output(_jsonl(_shell_events()), expected="shell-action")

    assert outcome == CopilotProtocolOutcome(
        type="shell-action",
        model_returned=MODEL,
        provider_calls_started=1,
        provider_calls_completed=1,
        tool_call=ToolCall(
            name="bash",
            arguments={"command": "pwd", "description": "Show the directory."},
        ),
        actions_requested=1,
        actions_denied=1,
    )


def test_parse_shell_action_accepts_passive_background_task_updates() -> None:
    events = _shell_events()
    events.insert(-1, _event("session.background_tasks_changed", {}))

    outcome = parse_output(_jsonl(events), expected="shell-action")

    assert outcome.actions_denied == 1
    assert outcome.actions_executed == 0


def test_outcome_repr_excludes_model_content_and_tool_arguments() -> None:
    final = parse_output(_jsonl(_final_events()), expected="final-response")
    shell = parse_output(_jsonl(_shell_events()), expected="shell-action")

    assert FINAL_TEXT not in repr(final)
    assert "pwd" not in repr(shell)
    assert "description" not in repr(shell)


def test_denial_line_detector_accepts_only_explicit_denial_completion() -> None:
    denial = _jsonl([_shell_events()[-1]])
    success = copy.deepcopy(_shell_events()[-1])
    success["data"] = {
        "toolCallId": TOOL_CALL_ID,
        "success": True,
        "result": {"content": ""},
    }

    assert is_denied_tool_completion(denial)
    assert not is_denied_tool_completion(_jsonl([success]))
    assert not is_denied_tool_completion(b"not json\n")
    assert not is_denied_tool_completion(b'{"type":"future.action"}\n')


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "empty-output"),
        (b"not json\n", "invalid-json"),
        (b"NaN\n", "invalid-number"),
        (b'{"type":"result","type":"result"}\n', "duplicate-field"),
        (_jsonl([["not", "an", "event"]]), "invalid-event"),
        (_jsonl([{"type": "future.action", "data": {}}]), "unsupported-event"),
    ],
)
def test_malformed_and_unknown_output_fails_closed(payload: bytes, code: str) -> None:
    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(payload, expected="final-response")

    assert raised.value.code == code


def test_oversized_line_fails_closed() -> None:
    oversized = b"{" + b" " * MAX_OUTPUT_LINE_BYTES + b"}\n"

    with pytest.raises(CopilotProtocolError, match="byte limit") as raised:
        parse_output(oversized, expected="final-response")

    assert raised.value.code in {"line-limit", "output-limit"}


def test_boolean_result_exit_code_is_not_zero() -> None:
    events = _final_events()
    events[-1]["exitCode"] = False

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="final-response")

    assert raised.value.code == "invalid-result"


def test_multiple_tool_requests_are_ambiguous() -> None:
    events = _shell_events()
    message = events[-3]["data"]
    message["toolRequests"].append(copy.deepcopy(message["toolRequests"][0]))

    with pytest.raises(CopilotProtocolError, match="exactly one") as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert raised.value.code == "ambiguous-action"


@pytest.mark.parametrize(
    ("event_index", "field", "value", "code"),
    [
        (-2, "toolCallId", "different-call", "action-mismatch"),
        (-1, "toolCallId", "different-call", "action-mismatch"),
        (-2, "turnId", "different-turn", "turn-mismatch"),
        (-1, "model", "different-model", "model-mismatch"),
    ],
)
def test_mismatched_action_evidence_fails_closed(
    event_index: int,
    field: str,
    value: str,
    code: str,
) -> None:
    events = _shell_events()
    events[event_index]["data"][field] = value

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert raised.value.code == code


def test_successful_tool_execution_is_never_a_denied_action() -> None:
    events = _shell_events()
    completion = events[-1]["data"]
    completion.update({"success": True, "result": {"content": "private"}})
    completion.pop("error")

    with pytest.raises(CopilotProtocolError, match="execution was denied") as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert raised.value.code == "action-not-denied"


@pytest.mark.parametrize(
    "event_type", ["model.call_start", "tool.execution_start", "assistant.message"]
)
def test_provider_call_or_action_after_denial_is_rejected_as_continuation(event_type: str) -> None:
    events = [
        *_shell_events(),
        _event(event_type, {"turnId": TURN_ID, "model": MODEL}),
    ]

    with pytest.raises(CopilotProtocolError, match="continued") as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert raised.value.code == "continued-after-denial"


def test_shutdown_lifecycle_after_denial_preserves_the_observed_action() -> None:
    events = [
        *_shell_events(),
        _event("model.call_final_result", {"model": MODEL, "result": "success", "isByok": False}),
        _event("assistant.turn_end", {"turnId": TURN_ID}),
        _event("session.usage_checkpoint", {"premiumRequests": 10}),
        _event("session.background_tasks_changed", {}),
        _event("assistant.turn_start", {"turnId": "continuation-turn"}),
    ]

    result = parse_output(_jsonl(events), expected="shell-action")

    assert result.actions_requested == result.actions_denied == 1
    assert result.actions_executed == 0
    assert result.provider_calls_started == result.provider_calls_completed == 1


def test_shutdown_turn_end_must_match_the_selected_turn() -> None:
    events = [*_shell_events(), _event("assistant.turn_end", {"turnId": "different-turn"})]

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert raised.value.code == "turn-mismatch"


def test_multiple_continuation_turns_after_denial_are_rejected() -> None:
    events = [
        *_shell_events(),
        _event("assistant.turn_end", {"turnId": TURN_ID}),
        _event("assistant.turn_start", {"turnId": "continuation-turn"}),
        _event("assistant.turn_start", {"turnId": "another-turn"}),
    ]

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert raised.value.code == "continued-after-denial"


@pytest.mark.parametrize("missing_type", ["model.call_start", "model.call_finished"])
def test_missing_provider_call_evidence_is_rejected(missing_type: str) -> None:
    events = [event for event in _final_events() if event["type"] != missing_type]

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="final-response")

    assert raised.value.code in {"invalid-sequence", "missing-provider-evidence"}


@pytest.mark.parametrize("missing_type", ["assistant.turn_end", "assistant.idle", "result"])
def test_final_response_requires_normal_turn_and_process_completion(missing_type: str) -> None:
    events = [event for event in _final_events() if event["type"] != missing_type]

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="final-response")

    assert raised.value.code in {"invalid-sequence", "incomplete-final-response"}


def test_errors_do_not_echo_model_or_tool_content() -> None:
    secret = "do-not-echo-this-value"
    events = _shell_events()
    request = events[-3]["data"]["toolRequests"][0]
    request["arguments"] = {"command": secret}

    with pytest.raises(CopilotProtocolError) as raised:
        parse_output(_jsonl(events), expected="shell-action")

    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
