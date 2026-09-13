from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ithrl.adapters._jsonl import JsonlDocument, read_jsonl
from ithrl.adapters.contracts import ParsingLimits, SourceFacts
from ithrl.adapters.copilot import PARSER
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import JsonObject

FIXTURE = Path(__file__).parents[1] / "fixtures/copilot-session/events.jsonl"


def _fixture_document() -> JsonlDocument:
    return read_jsonl(FIXTURE, limits=ParsingLimits())


def _changed_document(
    *,
    records: tuple[JsonObject, ...] | None = None,
) -> JsonlDocument:
    selected = records or _fixture_document().records
    raw = b"".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for record in selected
    )
    return JsonlDocument(
        raw=raw,
        records=selected,
        source=SourceFacts(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)),
    )


def _mutable_records() -> list[dict[str, Any]]:
    return copy.deepcopy(list(_fixture_document().records))


def test_parser_detects_and_preserves_copilot_fixture() -> None:
    document = _fixture_document()

    assert PARSER.format_name == "copilot-events-jsonl"
    assert PARSER.matches(document)

    session = PARSER.parse(document)

    assert session.metadata.session_id == "20000000-0000-4000-8000-000000000001"
    assert session.metadata.captured_at == "2026-01-01T00:00:00.000Z"
    assert session.metadata.format.harness == "copilot"
    assert session.metadata.format.name == "copilot-events-jsonl"
    assert session.metadata.format.producer_version == "1.0.82"
    assert session.metadata.format.record_schema_version == 1
    assert session.record_count == 27
    assert session.native_bytes == document.raw
    assert session.source == document.source
    assert session.records == document.records
    assert any(record["type"] == "fixture.unknown_event" for record in session.records)


def test_native_records_are_defensive_copies() -> None:
    session = PARSER.parse(_fixture_document())

    first_read = session.records
    first_read[0]["type"] = "changed"

    assert session.records[0]["type"] == "session.start"


def test_detector_does_not_claim_another_jsonl_format() -> None:
    records = _mutable_records()
    records[0]["type"] = "another.start"
    document = _changed_document(records=tuple(records))

    assert not PARSER.matches(document)
    with pytest.raises(AdapterError, match="not a GitHub Copilot CLI event log") as caught:
        PARSER.parse(document)
    assert caught.value.code is AdapterErrorCode.UNSUPPORTED_FORMAT


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 2, "record schema is not supported"),
        ("version", True, "record schema is not supported"),
    ],
)
def test_unsupported_record_schemas_fail_closed(field: str, value: object, message: str) -> None:
    records = _mutable_records()
    records[0]["data"][field] = value

    with pytest.raises(AdapterError, match=message) as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.code is AdapterErrorCode.UNSUPPORTED_VERSION
    assert caught.value.line == 1


def test_parser_accepts_another_producer_version_with_the_supported_record_schema() -> None:
    records = _mutable_records()
    records[0]["data"]["copilotVersion"] = "1.1.0"

    session = PARSER.parse(_changed_document(records=tuple(records)))

    assert session.metadata.format.producer_version == "1.1.0"
    assert session.metadata.format.record_schema_version == 1


def test_duplicate_record_ids_are_rejected() -> None:
    records = _mutable_records()
    records[1]["id"] = records[0]["id"]

    with pytest.raises(AdapterError, match="record IDs must be unique") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.code is AdapterErrorCode.INVALID_SESSION
    assert caught.value.line == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", None, "record ID is missing"),
        ("parentId", 42, "parent ID must be a non-empty string or null"),
        ("timestamp", None, "record timestamp is missing"),
        ("type", "", "record type is missing"),
        ("data", [], "record data must be an object"),
    ],
)
def test_invalid_record_envelopes_are_rejected(field: str, value: object, message: str) -> None:
    records = _mutable_records()
    records[1][field] = value

    with pytest.raises(AdapterError, match=message) as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.code is AdapterErrorCode.INVALID_SESSION
    assert caught.value.line == 2


def test_invalid_timestamps_are_rejected_but_record_order_is_authoritative() -> None:
    records = _mutable_records()
    records[1]["timestamp"] = "private timestamp"
    with pytest.raises(AdapterError, match="not valid RFC 3339") as malformed:
        PARSER.parse(_changed_document(records=tuple(records)))
    assert malformed.value.line == 2
    assert "private timestamp" not in str(malformed.value)

    records = _mutable_records()
    records[2]["timestamp"] = "2025-12-31T23:59:59Z"
    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 27


def test_invalid_session_start_time_is_rejected_without_echoing_it() -> None:
    records = _mutable_records()
    private_value = "private start time"
    records[0]["data"]["startTime"] = private_value

    with pytest.raises(AdapterError, match="start time is not valid RFC 3339") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert private_value not in str(caught.value)


def test_session_start_must_be_unique() -> None:
    records = _mutable_records()
    records[1]["type"] = "session.start"
    records[1]["data"] = copy.deepcopy(records[0]["data"])

    with pytest.raises(AdapterError, match=r"exactly one session\.start") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 1


def test_forward_and_external_parent_references_are_preserved() -> None:
    records = _mutable_records()
    records[1]["parentId"] = records[2]["id"]
    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 27

    records = _mutable_records()
    records[1]["parentId"] = "90000000-0000-4000-8000-000000000001"
    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 27


def test_fixture_preserves_repeated_turn_id_intervals() -> None:
    session = PARSER.parse(_fixture_document())

    turn_events = [
        record
        for record in session.records
        if record["type"] in {"assistant.turn_start", "assistant.turn_end"}
    ]
    assert [record["type"] for record in turn_events] == [
        "assistant.turn_start",
        "assistant.turn_end",
        "assistant.turn_start",
        "assistant.turn_end",
    ]


def test_tool_execution_must_follow_its_request_and_start() -> None:
    records = _mutable_records()
    records[12], records[13] = records[13], records[12]
    records[12]["timestamp"], records[13]["timestamp"] = (
        records[13]["timestamp"],
        records[12]["timestamp"],
    )
    records[12]["parentId"] = records[11]["id"]
    records[13]["parentId"] = records[12]["id"]

    with pytest.raises(AdapterError, match="completes before it starts") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 13


def test_tool_start_requires_a_preceding_request() -> None:
    records = _mutable_records()
    records[12]["data"]["toolCallId"] = "90000000-0000-4000-8000-000000000001"

    with pytest.raises(AdapterError, match="starts before its assistant request") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 13


def test_tool_relationships_must_use_an_open_matching_turn() -> None:
    records = _mutable_records()
    private_value = "private-turn-value"
    records[13]["data"]["turnId"] = private_value

    with pytest.raises(AdapterError, match="outside its assistant turn") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 14
    assert private_value not in str(caught.value)


def test_turn_can_end_after_a_tool_request_without_a_completion_record() -> None:
    records = _mutable_records()
    del records[12:14]

    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 25


@pytest.mark.parametrize("tool_requests", [None, []])
def test_assistant_messages_without_tool_requests_are_valid(
    tool_requests: list[object] | None,
) -> None:
    records = _mutable_records()[:12]
    if tool_requests is None:
        records[11]["data"].pop("toolRequests")
    else:
        records[11]["data"]["toolRequests"] = tool_requests

    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 12


@pytest.mark.parametrize(
    ("tool_requests", "message"),
    [
        ({}, "tool requests must be an array"),
        ([42], "tool request must be an object"),
        ([{}], "tool call ID is missing"),
    ],
)
def test_malformed_tool_requests_are_rejected(tool_requests: object, message: str) -> None:
    records = _mutable_records()[:12]
    records[11]["data"]["toolRequests"] = tool_requests

    with pytest.raises(AdapterError, match=message) as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 12


def test_tool_calls_cannot_be_requested_twice() -> None:
    records = _mutable_records()[:12]
    request = records[11]["data"]["toolRequests"][0]
    records[11]["data"]["toolRequests"].append(copy.deepcopy(request))

    with pytest.raises(AdapterError, match="tool call IDs must be unique") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 12


def test_tool_execution_cannot_start_or_complete_twice() -> None:
    records = _mutable_records()
    records[13]["type"] = "tool.execution_start"
    records[13]["data"] = copy.deepcopy(records[12]["data"])
    with pytest.raises(AdapterError, match="starts more than once") as duplicate_start:
        PARSER.parse(_changed_document(records=tuple(records)))
    assert duplicate_start.value.line == 14

    records = _mutable_records()
    records[14]["type"] = "tool.execution_complete"
    records[14]["data"] = copy.deepcopy(records[13]["data"])
    with pytest.raises(AdapterError, match="completes more than once") as duplicate_completion:
        PARSER.parse(_changed_document(records=tuple(records)))
    assert duplicate_completion.value.line == 15


def test_tool_request_must_belong_to_an_open_turn() -> None:
    records = _mutable_records()[:12]
    records[2]["type"] = "fixture.uninterpreted"

    with pytest.raises(AdapterError, match="outside its assistant turn") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 12


@pytest.mark.parametrize("last_index", [12, 13])
def test_incomplete_trailing_tool_work_is_accepted(last_index: int) -> None:
    records = tuple(_mutable_records()[:last_index])

    session = PARSER.parse(_changed_document(records=records))

    assert session.record_count == last_index


def test_turn_end_requires_a_preceding_turn_start() -> None:
    records = _mutable_records()
    records = [*records[:2], records[25]]

    with pytest.raises(AdapterError, match="turn ends before it starts") as caught:
        PARSER.parse(_changed_document(records=tuple(records)))

    assert caught.value.line == 3


def test_same_turn_id_can_be_nested() -> None:
    records = _mutable_records()
    records[3]["type"] = "assistant.turn_start"
    records[3]["data"] = copy.deepcopy(records[2]["data"])

    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 27


def test_outer_tool_can_remain_pending_while_nested_same_id_turn_closes() -> None:
    records = _mutable_records()
    nested_start = copy.deepcopy(records[2])
    nested_start["id"] = "10000000-0000-4000-8000-000000000096"
    nested_end = copy.deepcopy(records[14])
    nested_end["id"] = "10000000-0000-4000-8000-000000000097"
    records[12:12] = [nested_start]
    records[14:14] = [nested_end]

    assert PARSER.parse(_changed_document(records=tuple(records))).record_count == 29
