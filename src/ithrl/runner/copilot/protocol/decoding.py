"""Bounded JSONL decoding for GitHub Copilot CLI events."""

from __future__ import annotations

import json
from typing import cast

from ithrl.context import validate_json_limits
from ithrl.runner.copilot.protocol.contracts import (
    MAX_EVENT_DEPTH,
    MAX_EVENT_ITEMS,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_EVENTS,
    MAX_OUTPUT_LINE_BYTES,
    SUPPORTED_EVENTS,
    CopilotProtocolError,
)


def decode_events(output: bytes) -> list[dict[str, object]]:
    """Decode one bounded native JSONL stream."""

    if not output:
        raise CopilotProtocolError("empty-output", "Copilot returned no JSONL output")
    if len(output) > MAX_OUTPUT_BYTES:
        raise CopilotProtocolError("output-limit", "Copilot output exceeded the byte limit")
    lines = output.splitlines()
    if len(lines) > MAX_OUTPUT_EVENTS:
        raise CopilotProtocolError("event-limit", "Copilot output exceeded the event limit")
    if not lines or any(not line.strip() for line in lines):
        raise CopilotProtocolError("invalid-jsonl", "Copilot returned invalid JSONL output")
    return [decode_line(line) for line in lines]


def decode_line(line: bytes) -> dict[str, object]:
    """Decode and bound one native event line."""

    if not line or len(line) > MAX_OUTPUT_LINE_BYTES:
        raise CopilotProtocolError("line-limit", "A Copilot output line exceeded the byte limit")
    try:
        value = json.loads(
            line,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError):
        raise CopilotProtocolError("invalid-json", "Copilot returned invalid JSON") from None
    if not isinstance(value, dict):
        raise CopilotProtocolError("invalid-event", "Copilot returned an invalid event")
    try:
        validate_json_limits(value, max_depth=MAX_EVENT_DEPTH, max_items=MAX_EVENT_ITEMS)
    except ValueError:
        raise CopilotProtocolError(
            "event-limit", "A Copilot event exceeded structural limits"
        ) from None
    event = cast(dict[str, object], value)
    event_type = event.get("type")
    if not isinstance(event_type, str) or (
        event_type not in SUPPORTED_EVENTS and not event_type.startswith(("model.", "session."))
    ):
        raise CopilotProtocolError(
            "unsupported-event", "Copilot returned an unsupported event type"
        )
    if event.get("agentId") is not None:
        raise CopilotProtocolError(
            "subagent-event", "Copilot returned a subagent event in the selected turn"
        )
    return event


def event_data(event: dict[str, object]) -> dict[str, object]:
    """Return the object payload from one decoded event."""

    data = event.get("data")
    if not isinstance(data, dict):
        raise CopilotProtocolError("invalid-event", "Copilot returned an invalid event payload")
    return cast(dict[str, object], data)


def _reject_json_constant(_value: str) -> None:
    raise CopilotProtocolError("invalid-number", "Copilot returned a non-finite JSON number")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise CopilotProtocolError(
                "duplicate-field", "Copilot returned a JSON object with duplicate fields"
            )
        value[key] = item
    return value


__all__ = ["decode_events", "decode_line", "event_data"]
