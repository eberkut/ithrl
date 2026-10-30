"""Validate one GitHub Copilot CLI event sequence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import cast

from ithrl.behavior import MAX_BEHAVIOR_TEXT_LENGTH, MAX_SHELL_ACTIONS
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


def parse_output(
    output: bytes,
    *,
    expected: ExpectedOutcome,
    native_session: bytes | None = None,
    system_message: str | None = None,
) -> CopilotProtocolOutcome:
    """Validate SDK-native events and trusted physical-forward receipts."""

    events = decode_events(output)
    state = ProtocolState(expected=expected)
    if (
        events[0].get("type") != "ithrl.copilot.input"
        or events[-1].get("type") != "ithrl.copilot.complete"
    ):
        raise CopilotProtocolError(
            "missing-receipt", "Copilot output lacks worker input or completion evidence"
        )
    receipt = events[0]
    request_digest = None
    if receipt.get("schema_version") != 1 or receipt.get("new_user_messages") != 0:
        invalid_sequence()
    if native_session is not None and (
        receipt.get("native_session_sha256") != hashlib.sha256(native_session).hexdigest()
        or receipt.get("native_session_bytes") != len(native_session)
        or receipt.get("system_message_sha256")
        != (None if system_message is None else hashlib.sha256(system_message.encode()).hexdigest())
    ):
        raise CopilotProtocolError(
            "input-mismatch", "Copilot worker input differs from materialized evidence"
        )
    for event in events[1:-1]:
        event_type = cast(str, event["type"])
        if event_type == "system.message" and system_message is not None:
            data = event_data(event)
            if data.get("role", "system") == "system" and data.get("content") != system_message:
                raise CopilotProtocolError(
                    "input-mismatch",
                    "Copilot native system guidance differs from preserved evidence",
                )
        if event_type == "ithrl.copilot.forward":
            if (
                state.forwarded
                or event.get("provider_calls_started") != 1
                or event.get("schema_version") != 1
            ):
                invalid_sequence()
            request_digest = event.get("request_sha256")
            if (
                not isinstance(request_digest, str)
                or len(request_digest) != 64
                or any(c not in "0123456789abcdef" for c in request_digest)
            ):
                invalid_sequence()
            state.forwarded = True
            continue
        if event_type in {"pending_messages.modified", "session.usage_info", "sandbox.decision"}:
            event_data(event)
            continue
        if event_type == "session.idle" and state.idle_seen:
            event_data(event)
            continue
        if event_type == "session.error":
            raise CopilotProtocolError(
                _provider_failure_code(event_data(event))
                or state.provider_failure_code
                or "session-error",
                "Copilot reported a native session error",
            )
        if event_type == "model.call_failure":
            state.provider_failure_code = _provider_failure_code(event_data(event))
            continue
        if event_type == "session.handoff":
            raise CopilotProtocolError("context-changed", "Copilot handed off the selected session")
        if (
            event_type.startswith(("model.", "session."))
            and event_type not in _EVENT_HANDLERS
            and event_type not in STARTUP_EVENTS
        ):
            event_data(event)  # Telemetry does not change action or dispatch evidence.
            continue
        if state.denial_seen:
            _handle_post_denial_event(state, event_type, event_data(event))
            continue
        _handle_event(state, event_type, event)
    complete = events[-1]
    failure_code = complete.get("failure_code")
    if (
        complete.get("success") is False
        and isinstance(failure_code, str)
        and failure_code
        in {
            "managed-policy-unverified",
            "native-context-unverified",
            "inference-request-refused",
        }
    ):
        raise CopilotProtocolError(failure_code, "Copilot worker refused native execution")
    rejected = complete.get("requests_rejected")
    if (
        complete.get("schema_version") != 1
        or complete.get("success") is not True
        or complete.get("provider_calls_started") != 1
        or not state.forwarded
        or type(rejected) is not int
        or rejected < 0
        or (rejected != 0 and not state.denial_seen)
        or complete.get("request_sha256") != request_digest
        or complete.get("websockets_rejected") != 0
    ):
        raise CopilotProtocolError(
            "incomplete-dispatch", "Copilot output lacks one successful physical inference dispatch"
        )
    state.result_seen = True
    return _finish(state)


def _provider_failure_code(data: dict[str, object]) -> str | None:
    """Classify structured provider failures without exposing native error text or IDs."""

    status = data.get("statusCode")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        return None
    if data.get("errorCode") == "quota_exceeded":
        return "provider-quota-exceeded"
    if type(status) is not int:
        return None
    if 500 <= status <= 599:
        return "provider-unavailable"
    return {
        401: "provider-authentication-required",
        402: "provider-payment-required",
        403: "provider-access-denied",
        429: "provider-rate-limited",
    }.get(status)


def physical_forward_count(output: bytes) -> int | None:
    """Recover trusted physical-dispatch evidence even from a failed native turn."""

    lines = output.splitlines()
    if not lines:
        return None
    try:
        first = decode_line(lines[0])
        if first.get("type") != "ithrl.copilot.input" or first.get("schema_version") != 1:
            return None
        forwards = []
        complete = None
        for line in lines[1:]:
            try:
                event = decode_line(line)
            except CopilotProtocolError:
                continue  # Native protocol failure does not erase a worker receipt.
            if event.get("type") == "ithrl.copilot.forward":
                forwards.append(event)
            if event.get("type") == "ithrl.copilot.complete":
                complete = event
        if (
            len(forwards) == 1
            and forwards[0].get("schema_version") == 1
            and forwards[0].get("provider_calls_started") == 1
        ):
            return 1
        if (
            not forwards
            and complete is not None
            and complete.get("schema_version") == 1
            and complete.get("provider_calls_started") == 0
        ):
            return 0
    except CopilotProtocolError:
        pass
    return None


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
    if event_type in {"assistant.idle", "session.idle"} and state.turn_ended:
        return
    if event_type == "assistant.usage":
        _handle_usage(state, data)
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


def _handle_event(state: ProtocolState, event_type: str, event: dict[str, object]) -> None:
    if event_type in STARTUP_EVENTS:
        _handle_startup_event(state, event)
        return
    if event_type == "result":
        _handle_result(state, event)
        return
    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        invalid_sequence()
    handler(state, event_data(event))


def _handle_startup_event(state: ProtocolState, event: dict[str, object]) -> None:
    if state.turn_started or state.call_started:
        invalid_sequence()
    event_data(event)


def _handle_user_message(state: ProtocolState, data: dict[str, object]) -> None:
    raise CopilotProtocolError(
        "unexpected-user-message", "Copilot continuation added a user message"
    )


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
        raise CopilotProtocolError(
            _provider_failure_code(data) or state.provider_failure_code or "provider-failure",
            "Copilot reported a failed provider call",
        )
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
    if state.turn_started or state.message_seen or state.turn_ended:
        invalid_sequence()
    bind_turn(state, required_string(data, "turnId", max_length=MAX_ID_LENGTH))
    state.turn_started = True


def _handle_usage(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_started or state.usage is not None:
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
    if not state.call_finished or state.tool_calls or state.turn_ended:
        invalid_sequence()
    require_matching_turn(state, data)
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    message_id = required_string(data, "messageId", max_length=MAX_ID_LENGTH)
    if message_id in state.message_ids:
        invalid_sequence()
    state.message_ids.add(message_id)
    # Optional opaque provider metadata is not part of identity or completion proof.
    if "apiCallId" in data:
        required_string(data, "apiCallId", max_length=MAX_OUTPUT_LINE_BYTES, allow_empty=True)
    requests = data.get("toolRequests")
    if requests is None:
        requests = cast(list[object], [])
    if not isinstance(requests, list):
        raise CopilotProtocolError("invalid-message", "Copilot returned an invalid message")
    if state.expected == "final-response" or not requests:
        # One inference can emit reasoning, commentary and a final answer as
        # distinct messages. The last message before turn completion is the
        # selected response; each earlier message still undergoes action checks.
        _record_final_response(state, data, cast(list[object], requests))
    else:
        _record_shell_request(state, data, cast(list[object], requests))
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
    required_string(data, "content", max_length=MAX_BEHAVIOR_TEXT_LENGTH, allow_empty=True)
    if not 1 <= len(requests) <= MAX_SHELL_ACTIONS:
        raise CopilotProtocolError(
            "action-limit", "Copilot exceeded the supported shell action count"
        )
    for request in requests:
        _record_tool_request(state, request)
    state.final_text = None


def _record_tool_request(state: ProtocolState, value: object) -> None:
    if not isinstance(value, dict):
        raise CopilotProtocolError("invalid-action", "Copilot returned an invalid action")
    request = cast(dict[str, object], value)
    tool_call_id = required_string(request, "toolCallId", max_length=MAX_ID_LENGTH)
    if tool_call_id in state.tool_calls:
        raise CopilotProtocolError("action-mismatch", "Copilot returned duplicate action IDs")
    name = required_string(request, "name", max_length=256)
    if name not in {"bash", "shell"} or request.get("type", "function") != "function":
        raise CopilotProtocolError("unsupported-action", "Copilot returned a non-shell action")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        raise CopilotProtocolError("invalid-action", "Copilot returned invalid action arguments")
    try:
        tool_call = ToolCall(name=name, arguments=cast(JsonObject, arguments))
    except (TypeError, ValueError):
        raise CopilotProtocolError(
            "invalid-action", "Copilot returned invalid action arguments"
        ) from None
    state.tool_calls[tool_call_id] = tool_call


def _handle_tool_start(state: ProtocolState, data: dict[str, object]) -> None:
    if (
        state.expected != "shell-action"
        or not state.message_seen
        or not state.tool_calls
        or state.denial_seen
    ):
        invalid_sequence()
    tool_call_id = match_tool_identity(state, data)
    if tool_call_id in state.started_tools:
        invalid_sequence()
    require_matching_turn(state, data)
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    arguments = data.get("arguments")
    if arguments != state.tool_calls[tool_call_id].arguments:
        raise CopilotProtocolError(
            "action-mismatch", "Copilot action evidence does not match its request"
        )
    state.started_tools.add(tool_call_id)


def _handle_tool_complete(state: ProtocolState, data: dict[str, object]) -> None:
    if state.expected != "shell-action" or state.denial_seen:
        invalid_sequence()
    tool_call_id = match_tool_identity(state, data)
    if tool_call_id not in state.started_tools or tool_call_id in state.denied_tools:
        invalid_sequence()
    require_matching_turn(state, data)
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    error = data.get("error")
    if (
        data.get("success") is not False
        or not isinstance(error, dict)
        or cast(dict[str, object], error).get("code") != "denied"
        or "result" in data
    ):
        raise CopilotProtocolError(
            "action-not-denied", "Copilot did not prove that action execution was denied"
        )
    state.denied_tools.add(tool_call_id)
    state.denial_seen = len(state.denied_tools) == len(state.tool_calls)


def _handle_background_tasks(state: ProtocolState, data: dict[str, object]) -> None:
    if state.expected != "shell-action" or not state.started_tools or state.denial_seen or data:
        invalid_sequence()


def _handle_turn_end(state: ProtocolState, data: dict[str, object]) -> None:
    if state.final_text is None or state.tool_calls or not state.message_seen or state.turn_ended:
        invalid_sequence()
    require_matching_turn(state, data)
    state.turn_ended = True


def _handle_call_final_result(state: ProtocolState, data: dict[str, object]) -> None:
    if not state.call_finished or state.turn_ended:
        invalid_sequence()
    bind_model(state, required_string(data, "model", max_length=MAX_MODEL_LENGTH))
    if data.get("result") != "success" or data.get("isByok") is not False:
        raise CopilotProtocolError(
            _provider_failure_code(data) or state.provider_failure_code or "provider-failure",
            "Copilot did not report a successful managed provider call",
        )


def _handle_usage_checkpoint(state: ProtocolState, _data: dict[str, object]) -> None:
    if not state.turn_ended or state.idle_seen:
        invalid_sequence()
    # Session billing/cache telemetry is not per-call token or provider accounting.


def _handle_idle(state: ProtocolState, _data: dict[str, object]) -> None:
    if state.final_text is None or state.tool_calls or not state.turn_ended or state.idle_seen:
        invalid_sequence()
    state.idle_seen = True


def _handle_result(state: ProtocolState, event: dict[str, object]) -> None:
    if state.final_text is None or state.tool_calls or not state.idle_seen or state.result_seen:
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
    if not state.tool_calls:
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
    if not state.denial_seen or not state.tool_calls:
        raise CopilotProtocolError(
            "incomplete-denial", "Copilot output lacks a complete denied shell action"
        )
    return CopilotProtocolOutcome(
        type="shell-action",
        model_returned=model,
        provider_calls_started=1,
        provider_calls_completed=1,
        tool_calls=tuple(state.tool_calls.values()),
        usage=state.usage,
        actions_requested=len(state.tool_calls),
        actions_denied=len(state.denied_tools),
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


__all__ = ["parse_output"]
