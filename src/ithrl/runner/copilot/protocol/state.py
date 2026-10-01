"""Mutable parse state and field invariants for Copilot events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ithrl.runner.contracts import TokenUsage, ToolCall
from ithrl.runner.copilot.protocol.contracts import (
    MAX_ID_LENGTH,
    CopilotProtocolError,
    ExpectedOutcome,
)


@dataclass(slots=True)
class ProtocolState:
    """Evidence accumulated while validating one selected turn."""

    expected: ExpectedOutcome
    turn_id: str | None = None
    model: str | None = None
    user_seen: bool = False
    call_started: bool = False
    call_finished: bool = False
    turn_started: bool = False
    message_seen: bool = False
    turn_ended: bool = False
    idle_seen: bool = False
    result_seen: bool = False
    denial_seen: bool = False
    continuation_started: bool = False
    final_text: str | None = None
    tool_call_id: str | None = None
    tool_call: ToolCall | None = None
    tool_started: bool = False
    usage: TokenUsage | None = None


def parse_usage(value: object) -> TokenUsage | None:
    """Parse token counts when Copilot reports them."""

    if not isinstance(value, dict):
        raise CopilotProtocolError("invalid-usage", "Copilot returned invalid token usage")
    usage = cast(dict[str, object], value)
    input_value = usage.get("inputTokens")
    output_value = usage.get("outputTokens")
    if input_value is None and output_value is None:
        return None
    if not _is_count(input_value) or not _is_count(output_value):
        raise CopilotProtocolError("invalid-usage", "Copilot returned invalid token usage")
    cached = usage.get("cacheReadTokens")
    if cached is not None and not _is_count(cached):
        raise CopilotProtocolError("invalid-usage", "Copilot returned invalid token usage")
    return TokenUsage(
        input_tokens=cast(int, input_value),
        output_tokens=cast(int, output_value),
        cached_input_tokens=cast(int | None, cached),
    )


def required_string(
    value: dict[str, object],
    key: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    """Return one bounded string field."""

    return bounded_string(value.get(key), max_length=max_length, allow_empty=allow_empty)


def bounded_string(value: object, *, max_length: int, allow_empty: bool = False) -> str:
    """Validate one protocol string without exposing it in an error."""

    if not isinstance(value, str) or len(value) > max_length or (not value and not allow_empty):
        raise CopilotProtocolError("invalid-field", "Copilot returned an invalid event field")
    return value


def bind_turn(state: ProtocolState, turn_id: str) -> None:
    """Bind all selected-turn evidence to one identity."""

    if state.turn_id is not None and state.turn_id != turn_id:
        raise CopilotProtocolError(
            "turn-mismatch", "Copilot output contains more than one selected turn"
        )
    state.turn_id = turn_id


def require_matching_turn(state: ProtocolState, data: dict[str, object]) -> None:
    """Require an event to belong to the selected turn."""

    bind_turn(state, required_string(data, "turnId", max_length=MAX_ID_LENGTH))


def bind_model(state: ProtocolState, model: str) -> None:
    """Bind all provider evidence to one returned model identity."""

    if state.model is not None and state.model != model:
        raise CopilotProtocolError(
            "model-mismatch", "Copilot output contains inconsistent model identities"
        )
    state.model = model


def match_tool_identity(state: ProtocolState, data: dict[str, object]) -> None:
    """Require action lifecycle evidence to match the selected request."""

    tool_call_id = required_string(data, "toolCallId", max_length=MAX_ID_LENGTH)
    if tool_call_id != state.tool_call_id:
        raise CopilotProtocolError(
            "action-mismatch", "Copilot action evidence does not match its request"
        )
    tool_name = data.get("toolName")
    if tool_name is not None and (state.tool_call is None or tool_name != state.tool_call.name):
        raise CopilotProtocolError(
            "action-mismatch", "Copilot action evidence does not match its request"
        )


def invalid_sequence() -> None:
    """Reject an event outside the supported lifecycle."""

    raise CopilotProtocolError("invalid-sequence", "Copilot returned events in an invalid sequence")


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
