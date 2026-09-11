from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from ithrl.adapters._jsonl import JsonlDocument, read_jsonl
from ithrl.adapters.codex import PARSER
from ithrl.adapters.contracts import ParsingLimits, SourceFacts
from ithrl.adapters.errors import AdapterError, AdapterErrorCode
from ithrl.context import JsonObject

FIXTURE = Path(__file__).parents[1] / "fixtures" / "codex-session" / "rollout.jsonl"


@pytest.fixture
def document() -> JsonlDocument:
    return read_jsonl(FIXTURE, limits=ParsingLimits())


def test_parser_recognizes_and_preserves_supported_fixture(document: JsonlDocument) -> None:
    session = PARSER.parse(document)

    assert PARSER.format_name == "codex-rollout-jsonl"
    assert PARSER.matches(document)
    assert session.metadata.format.harness == "codex"
    assert session.metadata.format.name == PARSER.format_name
    assert session.metadata.format.producer_version == "0.153.4"
    assert session.metadata.format.record_schema_version is None
    assert session.metadata.session_id == "00000000-0000-4000-8000-000000000001"
    assert session.metadata.captured_at == "2026-01-01T00:00:00.000Z"
    assert session.source == document.source
    assert session.native_bytes == document.raw
    assert session.records == document.records
    assert session.record_count == 13
    assert session.records[10]["type"] == "fixture.unknown_record"


def test_native_records_are_defensive_copies(document: JsonlDocument) -> None:
    session = PARSER.parse(document)

    returned = session.records
    returned[0]["type"] = "changed"

    assert session.records[0]["type"] == "session_meta"


def test_parser_does_not_match_an_unrelated_envelope(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    records[0]["type"] = "session.start"

    assert not PARSER.matches(_document(records))


def test_parser_rejects_an_empty_document() -> None:
    document = JsonlDocument(
        raw=b"\n",
        records=(),
        source=SourceFacts(sha256=hashlib.sha256(b"\n").hexdigest(), size_bytes=1),
    )

    assert not PARSER.matches(document)
    with pytest.raises(AdapterError) as raised:
        PARSER.parse(document)

    assert raised.value.code is AdapterErrorCode.UNSUPPORTED_FORMAT


@pytest.mark.parametrize(
    ("record_index", "field", "value"),
    [
        (1, "ordinal", True),
        (1, "ordinal", 4),
        (1, "timestamp", "not-a-timestamp"),
        (1, "type", 1),
        (1, "payload", []),
    ],
)
def test_parser_rejects_invalid_record_envelopes(
    document: JsonlDocument,
    record_index: int,
    field: str,
    value: object,
) -> None:
    records = copy.deepcopy(document.records)
    records[record_index][field] = value  # type: ignore[assignment]

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == record_index + 1


def test_parser_rejects_timestamps_out_of_order(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    records[2]["timestamp"] = "2025-12-31T23:59:59.000Z"

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 3


def test_parser_rejects_unsupported_version_without_echoing_it(
    document: JsonlDocument,
) -> None:
    private_value = "private-version-value"
    records = copy.deepcopy(document.records)
    payload = _payload(records[0])
    payload["cli_version"] = private_value

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.UNSUPPORTED_VERSION
    assert raised.value.line == 1
    assert private_value not in str(raised.value)


@pytest.mark.parametrize("field", ["id", "session_id", "timestamp"])
def test_parser_requires_session_metadata_fields(
    document: JsonlDocument,
    field: str,
) -> None:
    records = copy.deepcopy(document.records)
    del _payload(records[0])[field]

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 1


def test_parser_rejects_inconsistent_session_metadata(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[0])["session_id"] = "different-session"

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 1


def test_parser_rejects_inconsistent_capture_timestamp(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[0])["timestamp"] = "2026-01-01T00:00:00.001Z"

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 1


def test_parser_rejects_duplicate_session_metadata(document: JsonlDocument) -> None:
    records = list(copy.deepcopy(document.records))
    duplicate = copy.deepcopy(records[0])
    duplicate["ordinal"] = 1
    duplicate["timestamp"] = records[1]["timestamp"]
    records[1] = duplicate

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(tuple(records)))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION


def test_parser_rejects_duplicate_response_item_identifier(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[4])["id"] = _payload(records[3])["id"]

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 5


def test_parser_rejects_tool_output_before_its_call(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    call = copy.deepcopy(_payload(records[5]))
    output = copy.deepcopy(_payload(records[8]))
    records[5]["payload"] = output
    records[8]["payload"] = call

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 6


def test_parser_rejects_duplicate_tool_output(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    records[10]["type"] = "response_item"
    records[10]["payload"] = {
        "type": "custom_tool_call_output",
        "id": "item-fixture-output-002",
        "call_id": "call-fixture-001",
        "output": [{"type": "input_text", "text": "Duplicate fixture result."}],
    }

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 11


def test_parser_rejects_duplicate_tool_call_identifier(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    records[10]["type"] = "response_item"
    records[10]["payload"] = {
        "type": "custom_tool_call",
        "id": "item-fixture-call-002",
        "call_id": "call-fixture-001",
        "name": "exec_command",
        "input": "{}",
    }

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 11


def test_parser_accepts_function_call_relationships(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[5])["type"] = "function_call"
    _payload(records[8])["type"] = "function_call_output"

    session = PARSER.parse(_document(records))

    assert session.record_count == len(records)


@pytest.mark.parametrize(
    ("call_type", "output_type"),
    [
        ("custom_tool_call", "function_call_output"),
        ("function_call", "custom_tool_call_output"),
    ],
)
def test_parser_rejects_output_type_that_does_not_match_call(
    document: JsonlDocument,
    call_type: str,
    output_type: str,
) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[5])["type"] = call_type
    _payload(records[8])["type"] = output_type

    with pytest.raises(AdapterError, match="output type must match") as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 9


def test_parser_accepts_incomplete_trailing_call_and_turn(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records[:6])

    session = PARSER.parse(_document(records))

    assert session.record_count == 6


def test_parser_rejects_task_completion_without_start(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[2])["type"] = "fixture.event"

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 13


def test_parser_rejects_overlapping_task_turns(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    records[3]["type"] = "event_msg"
    records[3]["payload"] = {"type": "task_started", "turn_id": "turn-fixture-002"}

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 4


def test_parser_rejects_completion_for_a_different_turn(document: JsonlDocument) -> None:
    records = copy.deepcopy(document.records)
    _payload(records[12])["turn_id"] = "turn-fixture-002"

    with pytest.raises(AdapterError) as raised:
        PARSER.parse(_document(records))

    assert raised.value.code is AdapterErrorCode.INVALID_SESSION
    assert raised.value.line == 13


def _payload(record: JsonObject) -> JsonObject:
    payload = record["payload"]
    assert isinstance(payload, dict)
    return payload


def _document(records: tuple[JsonObject, ...]) -> JsonlDocument:
    raw = b"\n".join(
        json.dumps(record, separators=(",", ":"), sort_keys=False).encode() for record in records
    )
    raw += b"\n"
    return JsonlDocument(
        raw=raw,
        records=records,
        source=SourceFacts(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)),
    )
