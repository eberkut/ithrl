"""Validate the supported Codex CLI rollout JSONL format."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Never

from ithrl.adapters._jsonl import JsonlDocument
from ithrl.adapters.contracts import NativeSession, SessionFormat, SessionMetadata
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import JsonObject
from ithrl.timestamps import validate_rfc3339_timestamp

_FORMAT_NAME = "codex-rollout-jsonl"
_SUPPORTED_VERSION = "0.153.4"
_CALL_OUTPUT_TYPES = {
    "custom_tool_call": "custom_tool_call_output",
    "function_call": "function_call_output",
}
_OUTPUT_TYPES = frozenset({"custom_tool_call_output", "function_call_output"})


@dataclass(frozen=True, slots=True)
class _Record:
    line: int
    timestamp: datetime
    type: str
    payload: JsonObject


class _CodexSessionParser:
    format_name = _FORMAT_NAME

    def matches(self, document: JsonlDocument) -> bool:
        """Return whether the document has the observed Codex rollout signature."""

        if not document.records:
            return False
        first = document.records[0]
        payload = first.get("payload")
        return (
            first.get("type") == "session_meta"
            and isinstance(payload, dict)
            and "cli_version" in payload
            and ("id" in payload or "session_id" in payload)
        )

    def parse(self, document: JsonlDocument) -> NativeSession:
        """Validate and preserve one supported Codex rollout document."""

        if not self.matches(document):
            raise AdapterError(
                AdapterErrorCode.UNSUPPORTED_FORMAT,
                "session source is not a Codex rollout",
            )

        records = _validate_envelopes(document.records)
        metadata = _session_metadata(records)
        _validate_response_items(records)
        _validate_task_turns(records)
        return NativeSession(
            metadata=metadata,
            source=document.source,
            native_bytes=document.raw,
            records=document.records,
        )


PARSER = _CodexSessionParser()


def _validate_envelopes(records: tuple[JsonObject, ...]) -> tuple[_Record, ...]:
    validated: list[_Record] = []
    previous_timestamp: datetime | None = None
    for line, record in enumerate(records, start=1):
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or ordinal != line - 1:
            _invalid("Codex records must have contiguous ordinals starting at zero", line)

        timestamp_text = _required_string(record, "timestamp", line, "record timestamp")
        timestamp = _parse_timestamp(timestamp_text, line, "record timestamp")
        if previous_timestamp is not None and timestamp < previous_timestamp:
            _invalid("Codex record timestamps must be ordered", line)

        record_type = _required_string(record, "type", line, "record type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            _invalid("Codex record payload must be an object", line)

        validated.append(_Record(line=line, timestamp=timestamp, type=record_type, payload=payload))
        previous_timestamp = timestamp
    return tuple(validated)


def _session_metadata(records: tuple[_Record, ...]) -> SessionMetadata:
    metadata_records = tuple(record for record in records if record.type == "session_meta")
    if len(metadata_records) != 1 or metadata_records[0].line != 1:
        _invalid("Codex session metadata must be the first and only metadata record")

    record = metadata_records[0]
    session_id = _required_string(record.payload, "session_id", record.line, "session identifier")
    alternate_id = _required_string(record.payload, "id", record.line, "session identifier")
    if session_id != alternate_id:
        _invalid("Codex session identifiers must agree", record.line)

    captured_at = _required_string(record.payload, "timestamp", record.line, "capture timestamp")
    captured_timestamp = _parse_timestamp(captured_at, record.line, "capture timestamp")
    if captured_timestamp != record.timestamp:
        _invalid("Codex metadata and record timestamps must agree", record.line)

    producer_version = _required_string(
        record.payload,
        "cli_version",
        record.line,
        "producer version",
    )
    if producer_version != _SUPPORTED_VERSION:
        raise AdapterError(
            AdapterErrorCode.UNSUPPORTED_VERSION,
            "Codex session producer version is not supported",
            line=record.line,
        )

    return SessionMetadata(
        format=SessionFormat(
            harness="codex",
            name=_FORMAT_NAME,
            producer_version=producer_version,
        ),
        session_id=session_id,
        captured_at=captured_at,
    )


def _validate_response_items(records: tuple[_Record, ...]) -> None:
    item_ids: set[str] = set()
    calls: dict[str, str] = {}
    outputs: set[str] = set()

    for record in records:
        if record.type != "response_item":
            continue
        item_type = _required_string(record.payload, "type", record.line, "response item type")
        item_id = _required_string(record.payload, "id", record.line, "response item identifier")
        if item_id in item_ids:
            _invalid("Codex response item identifiers must be unique", record.line)
        item_ids.add(item_id)

        _validate_tool_relationship(record, item_type, calls, outputs)


def _validate_tool_relationship(
    record: _Record,
    item_type: str,
    calls: dict[str, str],
    outputs: set[str],
) -> None:
    expected_output = _CALL_OUTPUT_TYPES.get(item_type)
    if expected_output is not None:
        call_id = _required_string(record.payload, "call_id", record.line, "tool call identifier")
        if call_id in calls:
            _invalid("Codex tool call identifiers must be unique", record.line)
        calls[call_id] = expected_output
        return
    if item_type not in _OUTPUT_TYPES:
        return

    call_id = _required_string(record.payload, "call_id", record.line, "tool call identifier")
    if call_id not in calls:
        _invalid("Codex tool output must follow its call", record.line)
    if item_type != calls[call_id]:
        _invalid("Codex tool output type must match its call type", record.line)
    if call_id in outputs:
        _invalid("Codex tool calls must have at most one output", record.line)
    outputs.add(call_id)


def _validate_task_turns(records: tuple[_Record, ...]) -> None:
    active_turn: str | None = None
    for record in records:
        if record.type != "event_msg":
            continue
        event_type = _required_string(record.payload, "type", record.line, "event type")
        if event_type == "task_started":
            turn_id = _required_string(record.payload, "turn_id", record.line, "turn identifier")
            if active_turn is not None:
                _invalid("Codex task turns must not overlap", record.line)
            active_turn = turn_id
        elif event_type == "task_complete":
            turn_id = _required_string(record.payload, "turn_id", record.line, "turn identifier")
            if active_turn is None:
                _invalid("Codex task completion must follow a task start", record.line)
            if turn_id != active_turn:
                _invalid("Codex task completion must match the active turn", record.line)
            active_turn = None


def _required_string(
    value: JsonObject,
    field: str,
    line: int,
    description: str,
) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        _invalid(f"Codex {description} must be a non-empty string", line)
    return item


def _parse_timestamp(value: str, line: int, description: str) -> datetime:
    try:
        validate_rfc3339_timestamp(value, field=description)
        return datetime.fromisoformat(f"{value[:-1]}+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise AdapterError(
            AdapterErrorCode.INVALID_SESSION,
            f"Codex {description} must be a valid RFC 3339 timestamp",
            line=line,
        ) from None


def _invalid(message: str, line: int | None = None) -> Never:
    raise AdapterError(AdapterErrorCode.INVALID_SESSION, message, line=line)
