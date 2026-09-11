"""Validate the supported GitHub Copilot CLI session format."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Never, cast

from ithrl.adapters._jsonl import JsonlDocument
from ithrl.adapters.contracts import NativeSession, SessionFormat, SessionMetadata
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import JsonObject, JsonValue
from ithrl.timestamps import validate_rfc3339_timestamp

_FORMAT_NAME = "copilot-events-jsonl"
_PRODUCER = "copilot-cli"
_SUPPORTED_PRODUCER_VERSION = "1.0.82"
_SUPPORTED_RECORD_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class _Record:
    line: int
    record_id: str
    parent_id: str | None
    record_type: str
    data: JsonObject


@dataclass(slots=True)
class _ToolCall:
    turn_id: str
    started: bool = False
    completed: bool = False


@dataclass(slots=True)
class _RelationshipState:
    open_turns: set[str] = field(default_factory=set)
    ended_turns: set[str] = field(default_factory=set)
    tools: dict[str, _ToolCall] = field(default_factory=dict)
    pending_tools: dict[str, int] = field(default_factory=dict)


class _CopilotParser:
    format_name = _FORMAT_NAME

    def matches(self, document: JsonlDocument) -> bool:
        """Return whether the first record has the Copilot session signature."""

        if not document.records:
            return False
        first = document.records[0]
        data = first.get("data")
        return (
            first.get("type") == "session.start"
            and isinstance(data, dict)
            and data.get("producer") == _PRODUCER
            and "sessionId" in data
            and "version" in data
            and "copilotVersion" in data
        )

    def parse(self, document: JsonlDocument) -> NativeSession:
        """Validate and preserve one bounded Copilot session document."""

        if not self.matches(document):
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED_FORMAT,
                "session source is not a GitHub Copilot CLI event log",
            )

        records = _validate_envelopes(document.records)
        metadata = _parse_metadata(records)
        _validate_parent_order(records)
        _validate_turns_and_tools(records)
        return NativeSession(
            metadata=metadata,
            source=document.source,
            native_bytes=document.raw,
            records=document.records,
        )


PARSER = _CopilotParser()


def _validate_envelopes(records: tuple[JsonObject, ...]) -> tuple[_Record, ...]:
    validated: list[_Record] = []
    record_ids: set[str] = set()
    previous_instant: datetime | None = None

    for line, record in enumerate(records, start=1):
        record_id = _required_string(record.get("id"), line, "session record ID is missing")
        if record_id in record_ids:
            _invalid("session record IDs must be unique", line)
        record_ids.add(record_id)

        parent = record.get("parentId")
        if parent is not None and (not isinstance(parent, str) or not parent):
            _invalid("session record parent ID must be a non-empty string or null", line)

        timestamp = _required_string(
            record.get("timestamp"), line, "session record timestamp is missing"
        )
        try:
            validate_rfc3339_timestamp(timestamp, field="session record timestamp")
        except ValueError:
            raise AdapterError(
                AdapterErrorCode.INVALID_SESSION,
                "session record timestamp is not valid RFC 3339",
                line=line,
            ) from None
        instant = _timestamp_instant(timestamp)
        if previous_instant is not None and instant < previous_instant:
            _invalid("session record timestamps are out of order", line)
        previous_instant = instant

        record_type = _required_string(record.get("type"), line, "session record type is missing")
        data = record.get("data")
        if not isinstance(data, dict):
            _invalid("session record data must be an object", line)

        validated.append(
            _Record(
                line=line,
                record_id=record_id,
                parent_id=cast(str | None, parent),
                record_type=record_type,
                data=cast(JsonObject, data),
            )
        )
    return tuple(validated)


def _parse_metadata(records: tuple[_Record, ...]) -> SessionMetadata:
    starts = [record for record in records if record.record_type == "session.start"]
    if len(starts) != 1 or starts[0].line != 1:
        _invalid("session must begin with exactly one session.start record", 1)
    start = starts[0]

    session_id = _required_string(start.data.get("sessionId"), start.line, "session ID is missing")
    record_schema = start.data.get("version")
    if type(record_schema) is not int or record_schema != _SUPPORTED_RECORD_SCHEMA:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_VERSION,
            "GitHub Copilot CLI record schema is not supported",
            line=start.line,
        )
    producer_version = _required_string(
        start.data.get("copilotVersion"), start.line, "producer version is missing"
    )
    if producer_version != _SUPPORTED_PRODUCER_VERSION:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_VERSION,
            "GitHub Copilot CLI producer version is not supported",
            line=start.line,
        )
    captured_at = _required_string(
        start.data.get("startTime"), start.line, "session start time is missing"
    )
    try:
        validate_rfc3339_timestamp(captured_at, field="session start time")
    except ValueError:
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            "session start time is not valid RFC 3339",
            line=start.line,
        ) from None

    return SessionMetadata(
        format=SessionFormat(
            harness="copilot",
            name=_FORMAT_NAME,
            producer_version=producer_version,
            record_schema_version=record_schema,
        ),
        session_id=session_id,
        captured_at=captured_at,
    )


def _validate_parent_order(records: tuple[_Record, ...]) -> None:
    positions = {record.record_id: index for index, record in enumerate(records)}
    for index, record in enumerate(records):
        if record.parent_id in positions and positions[record.parent_id] >= index:
            _invalid("session record parent must precede its child", record.line)


def _validate_turns_and_tools(records: tuple[_Record, ...]) -> None:
    state = _RelationshipState()
    for record in records:
        if validator := _RELATIONSHIP_VALIDATORS.get(record.record_type):
            validator(record, state)


def _start_turn(record: _Record, state: _RelationshipState) -> None:
    turn_id = _turn_id(record)
    if turn_id in state.open_turns or turn_id in state.ended_turns:
        _invalid("assistant turn starts more than once", record.line)
    state.open_turns.add(turn_id)


def _end_turn(record: _Record, state: _RelationshipState) -> None:
    turn_id = _turn_id(record)
    if turn_id not in state.open_turns:
        _invalid("assistant turn ends before it starts", record.line)
    if state.pending_tools.get(turn_id, 0):
        _invalid("assistant turn ends before its requested tools complete", record.line)
    state.open_turns.remove(turn_id)
    state.ended_turns.add(turn_id)
    state.pending_tools.pop(turn_id, None)


def _add_tool_requests(
    record: _Record,
    state: _RelationshipState,
) -> None:
    requests = record.data.get("toolRequests")
    if requests is None:
        return
    if not isinstance(requests, list):
        _invalid("assistant tool requests must be an array", record.line)
    if not requests:
        return

    turn_id = _turn_id(record)
    _require_open_turn(turn_id, record.line, state.open_turns)
    for request in requests:
        if not isinstance(request, dict):
            _invalid("assistant tool request must be an object", record.line)
        tool_call_id = _required_string(
            request.get("toolCallId"), record.line, "assistant tool call ID is missing"
        )
        if tool_call_id in state.tools:
            _invalid("assistant tool call IDs must be unique", record.line)
        state.tools[tool_call_id] = _ToolCall(turn_id=turn_id)
        state.pending_tools[turn_id] = state.pending_tools.get(turn_id, 0) + 1


def _start_tool(
    record: _Record,
    state: _RelationshipState,
) -> None:
    tool_call_id = _required_string(
        record.data.get("toolCallId"), record.line, "tool execution call ID is missing"
    )
    turn_id = _turn_id(record)
    tool = state.tools.get(tool_call_id)
    if tool is None:
        _invalid("tool execution starts before its assistant request", record.line)
    if tool.turn_id != turn_id:
        _invalid("tool execution and assistant request use different turns", record.line)
    _require_open_turn(turn_id, record.line, state.open_turns)
    if tool.started:
        _invalid("tool execution starts more than once", record.line)
    tool.started = True


def _complete_tool(
    record: _Record,
    state: _RelationshipState,
) -> None:
    tool_call_id = _required_string(
        record.data.get("toolCallId"), record.line, "tool completion call ID is missing"
    )
    turn_id = _turn_id(record)
    tool = state.tools.get(tool_call_id)
    if tool is None or not tool.started:
        _invalid("tool execution completes before it starts", record.line)
    if tool.turn_id != turn_id:
        _invalid("tool completion and assistant request use different turns", record.line)
    _require_open_turn(turn_id, record.line, state.open_turns)
    if tool.completed:
        _invalid("tool execution completes more than once", record.line)
    tool.completed = True
    state.pending_tools[turn_id] -= 1


def _require_open_turn(
    turn_id: str,
    line: int,
    open_turns: set[str],
) -> None:
    if turn_id not in open_turns:
        _invalid("tool relationship is outside its assistant turn", line)


def _turn_id(record: _Record) -> str:
    return _required_string(record.data.get("turnId"), record.line, "turn ID is missing")


def _timestamp_instant(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def _required_string(value: JsonValue | None, line: int, message: str) -> str:
    if not isinstance(value, str) or not value:
        _invalid(message, line)
    return value


def _invalid(message: str, line: int) -> Never:
    raise AdapterError(AdapterErrorCode.INVALID_SESSION, message, line=line)


type _RelationshipValidator = Callable[[_Record, _RelationshipState], None]

_RELATIONSHIP_VALIDATORS: dict[str, _RelationshipValidator] = {
    "assistant.turn_start": _start_turn,
    "assistant.message": _add_tool_requests,
    "tool.execution_start": _start_tool,
    "tool.execution_complete": _complete_tool,
    "assistant.turn_end": _end_turn,
}
