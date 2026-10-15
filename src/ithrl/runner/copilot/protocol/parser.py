"""Validate one GitHub Copilot CLI event sequence."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from pydantic import ValidationError

from ithrl.behavior import MAX_BEHAVIOR_TEXT_LENGTH
from ithrl.context import JsonObject
from ithrl.runner.contracts import ToolCall
from ithrl.runner.copilot.protocol.contracts import (
    MAX_ID_LENGTH,
    MAX_MODEL_LENGTH,
    MAX_OUTPUT_LINE_BYTES,
    STARTUP_EVENTS,
    CopilotProtocolError,
    CopilotProtocolOutcome,
    ExpectedOutcome,
)
from ithrl.runner.copilot.protocol.decoding import decode_events, decode_line, event_data
from ithrl.runner.copilot.protocol.state import (
    ProtocolState,
    bind_model,
    bind_turn,
    bounded_string,
    invalid_sequence,
    match_tool_identity,
    parse_usage,
    require_matching_turn,
    required_string,
)


def parse_output(output: bytes, *, expected: ExpectedOutcome) -> CopilotProtocolOutcome:
    """Parse one bounded ``--output-format json`` stream and prove its outcome."""

    events = decode_events(output)
    state = ProtocolState(expected=expected)
    for index, event in enumerate(events):
        event_type = cast(str, event["type"])
        if state.denial_seen:
            _handle_post_denial_event(state, event_type, event_data(event))
            continue
        _handle_event(state, event_type, event)
        if event_type == "result" and index != len(events) - 1:
            raise CopilotProtocolError(
                "continued-after-result",
                "Copilot emitted events after its result",
            )
    return _finish(state)


def _handle_post_denial_event(
    state: ProtocolState, event_type: str, data: dict[str, object]
) -> None:
    # Bounded draining can retain lifecycle records emitted during group shutdown.
    # They do not invalidate the observed denial or prove another provider call.
    if event_type == "assistant.turn_end" and not state.turn_ended:
        require_matching_turn(state, data)
        state.turn_ended = True
        return
    if event_type == "assistant.turn_start" and state.turn_ended and not state.continuation_started:
        required_string(data, "turnId", max_length=MAX_ID_LENGTH)
        state.continuation_started = True
        return
    if event_type == "session.background_tasks_changed" and not data:
        return
    if event_type == "model.call_final_result" and not state.turn_ended:
        _handle_call_final_result(state, data)
        return
    if event_type == "session.usage_checkpoint" and state.turn_ended:
        return
    raise CopilotProtocolError(
        "continued-after-denial",
        "Copilot continued after the denied action",
    )


def is_denied_tool_completion(line: bytes) -> bool:
    """Return whether one bounded line is an explicit denied tool completion."""

    try:
        event = decode_line(line)
        if event.get("type") != "tool.execution_complete":
            return False
        data = event_data(event)
        error = data.get("error")
        return (
            data.get("success") is False
            and isinstance(error, dict)
            and error.get("code") == "denied"
        )
    except CopilotProtocolError:
        return False


def _handle_event(state: ProtocolState, event_type: str, event: dict[str, object]) -> None:
    if event_type in STARTUP_EVENTS:
        _handle_startup_event(state, event)
        return
    if event_type == "result":
        _handle_result(state, event)
        return
    _EVENT_HANDLERS[event_type](state, event_data(event))


def _handle_startup_event(state: ProtocolState, event: dict[str, object]) -> None:
    if state.user_seen or state.turn_started or state.call_started:
        invalid_sequence()
    event_data(event)


def _handle_user_message(state: ProtocolState, data: dict[str, object]) -> None:
    if state.user_seen or state.turn_started or state.call_started:
        invalid_sequence()
    bind_turn(state, required_string(data, "turnId", max_length=MAX_ID_LENGTH))
    required_string(
        data,
        "content",
        max_length=MAX_BEHAVIOR_TEXT_LENGTH,
        allow_empty=True,
    )
    state.user_seen = True


def _handle_call_start(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.turn_started or state.call_started or state.message_seen or state.turn_ended:
        invalid_sequence()
    turn_id = required_string(data, "turnId", max_length=MAX_ID_LENGTH)
    model = required_string(data, "model", max_length=MAX_MODEL_LENGTH)
    bind_turn(state, turn_id)
    bind_model(state, model)
    state.call_started = True


def _handle_call_finished(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_started or state.call_finished or state.message_seen:
        invalid_sequence()
    require_matching_turn(state, data)
    if data.get("outcome") != "success":
        raise CopilotProtocolError("provider-failure", "Copilot reported a failed provider call")
    if (model := data.get("model")) is not None:
        bind_model(state, bounded_string(model, max_length=MAX_MODEL_LENGTH))
    if "usage" in data:
        reported_usage = parse_usage(data["usage"])
        if state.usage is not None and reported_usage is not None and reported_usage != state.usage:
            raise CopilotProtocolError(
                "usage-mismatch", "Copilot output contains inconsistent token usage"
            )
        state.usage = reported_usage or state.usage
    state.call_finished = True


def _handle_turn_start(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.user_seen or state.turn_started or state.message_seen or state.turn_ended:
        invalid_sequence()
    bind_turn(state, required_string(data, "turnId", max_length=MAX_ID_LENGTH))
    state.turn_started = True


def _handle_usage(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_started or state.message_seen or state.usage is not None:
        invalid_sequence()
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    state.usage = parse_usage(data)


def _handle_reasoning(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_started or not state.turn_started or state.turn_ended:
        invalid_sequence()
    required_string(data, "content", max_length=MAX_OUTPUT_LINE_BYTES, allow_empty=True)
    required_string(data, "reasoningId", max_length=MAX_OUTPUT_LINE_BYTES)
    if "model" in data:
        bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    if "turnId" in data:
        require_matching_turn(state, data)
    # Opaque timeline fields remain bounded by the decoder's encoded-line limit.
    # Timeline content and rte metadata establish no outcome or accounting evidence.


def _handle_message(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_finished or state.message_seen or state.turn_ended:
        invalid_sequence()
    require_matching_turn(state, data)
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    required_string(data, "messageId", max_length=MAX_ID_LENGTH)
    # Optional opaque provider metadata is not part of identity or completion proof.
    if "apiCallId" in data:
        required_string(data, "apiCallId", max_length=MAX_OUTPUT_LINE_BYTES, allow_empty=True)
    requests = data.get("toolRequests")
    if not isinstance(requests, list):
        raise CopilotProtocolError("invalid-message", "Copilot returned an invalid message")
    if state.expected == "final-response" or not requests:
        _record_final_response(state, data, requests)
    else:
        _record_shell_request(state, data, requests)
    state.message_seen = True


def _record_final_response(
    state: ProtocolState,
    data: dict[str, object],
    requests: list[object],
) -> None:
    if requests:
        raise CopilotProtocolError(
            "unexpected-action", "Copilot requested an action instead of a final response"
        )
    state.final_text = required_string(
        data,
        "content",
        max_length=MAX_BEHAVIOR_TEXT_LENGTH,
        allow_empty=True,
    )


def _record_shell_request(
    state: ProtocolState,
    data: dict[str, object],
    requests: list[object],
) -> None:
    if data.get("content") != "" or len(requests) != 1:
        raise CopilotProtocolError(
            "ambiguous-action", "Copilot did not return exactly one shell action"
        )
    _record_tool_request(state, requests[0])


def _record_tool_request(state: ProtocolState, value: object) -> None:
    if not isinstance(value, dict):
        raise CopilotProtocolError("invalid-action", "Copilot returned an invalid action")
    request = cast(dict[str, object], value)
    tool_call_id = required_string(request, "toolCallId", max_length=MAX_ID_LENGTH)
    name = required_string(request, "name", max_length=256)
    if name not in {"bash", "shell"} or request.get("type", "function") != "function":
        raise CopilotProtocolError("unsupported-action", "Copilot returned a non-shell action")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        raise CopilotProtocolError("invalid-action", "Copilot returned invalid action arguments")
    try:
        tool_call = ToolCall(name=name, arguments=cast(JsonObject, arguments))
    except (TypeError, ValueError, ValidationError):
        raise CopilotProtocolError(
            "invalid-action", "Copilot returned invalid action arguments"
        ) from None
    state.tool_call_id = tool_call_id
    state.tool_call = tool_call


def _handle_tool_start(state: ProtocolState, data: dict[str, object]) -> None:
    if (
        state.expected != "shell-action"
        or not state.message_seen
        or state.tool_call is None
        or state.tool_started
    ):
        invalid_sequence()
    match_tool_identity(state, data)
    require_matching_turn(state, data)
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    arguments = data.get("arguments")
    if state.tool_call is None or arguments != state.tool_call.arguments:
        raise CopilotProtocolError(
            "action-mismatch", "Copilot action evidence does not match its request"
        )
    state.tool_started = True


def _handle_tool_complete(state: ProtocolState, data: dict[str, object]) -> None:
    if state.expected != "shell-action" or not state.tool_started or state.denial_seen:
        invalid_sequence()
    match_tool_identity(state, data)
    require_matching_turn(state, data)
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    error = data.get("error")
    if (
        data.get("success") is not False
        or not isinstance(error, dict)
        or error.get("code") != "denied"
        or "result" in data
    ):
        raise CopilotProtocolError(
            "action-not-denied", "Copilot did not prove that action execution was denied"
        )
    state.denial_seen = True


def _handle_background_tasks(state: ProtocolState, data: dict[str, object]) -> None:
    if state.expected != "shell-action" or not state.tool_started or state.denial_seen or data:
        invalid_sequence()


def _handle_turn_end(state: ProtocolState, data: dict[str, object]) -> None:
    if (
        state.final_text is None
        or state.tool_call is not None
        or not state.message_seen
        or state.turn_ended
    ):
        invalid_sequence()
    require_matching_turn(state, data)
    state.turn_ended = True


def _handle_call_final_result(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_finished or state.turn_ended:
        invalid_sequence()
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    if data.get("result") != "success" or data.get("isByok") is not False:
        raise CopilotProtocolError(
            "provider-failure", "Copilot did not report a successful managed provider call"
        )


def _handle_usage_checkpoint(state: ProtocolState, _data: dict[str, object]) -> None:
    if not state.turn_ended or state.idle_seen:
        invalid_sequence()
    # Session billing/cache telemetry is not per-call token or provider accounting.


def _handle_idle(state: ProtocolState, _data: dict[str, object]) -> None:
    if (
        state.final_text is None
        or state.tool_call is not None
        or not state.turn_ended
        or state.idle_seen
    ):
        invalid_sequence()
    state.idle_seen = True


def _handle_result(state: ProtocolState, event: dict[str, object]) -> None:
    if (
        state.final_text is None
        or state.tool_call is not None
        or not state.idle_seen
        or state.result_seen
    ):
        invalid_sequence()
    exit_code = event.get("exitCode")
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or exit_code != 0
        or not isinstance(event.get("usage"), dict)
    ):
        raise CopilotProtocolError("invalid-result", "Copilot returned an unsuccessful result")
    required_string(event, "sessionId", max_length=MAX_ID_LENGTH)
    state.result_seen = True


def _finish(state: ProtocolState) -> CopilotProtocolOutcome:
    if not state.call_started or not state.call_finished or state.model is None:
        raise CopilotProtocolError(
            "missing-provider-evidence", "Copilot output lacks complete provider-call evidence"
        )
    if state.expected == "final-response" or state.final_text is not None:
        return _finish_final_response(state, state.model)
    return _finish_shell_action(state, state.model)


def _finish_final_response(state: ProtocolState, model: str) -> CopilotProtocolOutcome:
    if not (
        state.message_seen
        and state.turn_ended
        and state.idle_seen
        and state.result_seen
        and state.final_text is not None
    ):
        raise CopilotProtocolError(
            "incomplete-final-response", "Copilot output lacks a complete final response"
        )
    return CopilotProtocolOutcome(
        type="final-response",
        model_returned=model,
        provider_calls_started=1,
        provider_calls_completed=1,
        final_text=state.final_text,
        usage=state.usage,
    )


def _finish_shell_action(state: ProtocolState, model: str) -> CopilotProtocolOutcome:
    if not state.denial_seen or state.tool_call is None:
        raise CopilotProtocolError(
            "incomplete-denial", "Copilot output lacks a complete denied shell action"
        )
    return CopilotProtocolOutcome(
        type="shell-action",
        model_returned=model,
        provider_calls_started=1,
        provider_calls_completed=1,
        tool_call=state.tool_call,
        usage=state.usage,
        actions_requested=1,
        actions_denied=1,
    )


_EVENT_HANDLERS: dict[str, Callable[[ProtocolState, dict[str, object]], None]] = {
    "user.message": _handle_user_message,
    "model.call_start": _handle_call_start,
    "model.call_finished": _handle_call_finished,
    "model.call_final_result": _handle_call_final_result,
    "assistant.turn_start": _handle_turn_start,
    "assistant.usage": _handle_usage,
    "assistant.message": _handle_message,
    "assistant.reasoning": _handle_reasoning,
    "tool.execution_start": _handle_tool_start,
    "tool.execution_complete": _handle_tool_complete,
    "session.background_tasks_changed": _handle_background_tasks,
    "session.usage_checkpoint": _handle_usage_checkpoint,
    "assistant.turn_end": _handle_turn_end,
    "assistant.idle": _handle_idle,
}


__all__ = ["is_denied_tool_completion", "parse_output"]
