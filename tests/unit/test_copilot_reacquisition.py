"""Sanitized Copilot passive evidence profiles, independent of experimental parsing."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from ithrl.adapters._jsonl import read_jsonl_bytes
from ithrl.adapters.contracts import ParsingLimits
from ithrl.adapters.copilot.parser import PARSER
from ithrl.adapters.copilot.reacquisition import project_reacquisition
from ithrl.adapters.errors import AdapterError


def _records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def event(kind: str, data: dict[str, Any]) -> None:
        records.append(
            {
                "id": f"event-{len(records)}",
                "parentId": f"event-{len(records) - 1}" if records else None,
                "timestamp": "2026-01-01T00:00:00Z",
                "type": kind,
                "data": data,
            }
        )

    event(
        "session.start",
        {
            "producer": "copilot-agent",
            "sessionId": "fixture-session",
            "version": 1,
            "copilotVersion": "fixture-version",
            "startTime": "2026-01-01T00:00:00Z",
        },
    )
    for index in range(2):
        call = f"call-{index}"
        event(
            "assistant.message",
            {
                "toolRequests": [
                    {
                        "toolCallId": call,
                        "name": "view",
                        "arguments": {"path": "/fixture/source.py"},
                    }
                ]
            },
        )
        event(
            "tool.execution_start",
            {"toolCallId": call, "toolName": "view", "arguments": {"path": "/fixture/source.py"}},
        )
        event(
            "tool.execution_complete",
            {
                "toolCallId": call,
                "success": True,
                "result": {"content": "return 1", "detailedContent": "1: return 1"},
            },
        )
        if index == 0:
            event("session.compaction_start", {"compactionAttemptId": "compaction-1"})
            event(
                "session.compaction_complete",
                {"compactionAttemptId": "compaction-1", "success": True, "outcome": "applied"},
            )
    return records


def _document(records: list[dict[str, Any]]):
    return read_jsonl_bytes(
        "\n".join(json.dumps(item) for item in records).encode(), limits=ParsingLimits()
    )


def test_completed_compaction_keeps_native_scopes_and_distinct_call_anchors() -> None:
    projection = project_reacquisition(_document(_records()))
    assert projection.harness == "copilot"
    assert len(projection.boundaries) == 1
    boundary = projection.boundaries[0]
    assert boundary.start.line == 5
    assert boundary.end.line == 6
    assert len(projection.operations) == 2
    before, after = projection.operations
    assert before.call.line == 2
    assert before.completion is not None
    assert before.completion.line == 4
    assert after.call.line == 7
    assert after.completion is not None
    assert after.completion.line == 9
    assert before.tool == after.tool == "view"
    assert before.arguments == after.arguments == {"path": "/fixture/source.py"}
    assert before.category == "file-read"
    assert [(r.scope, r.value) for r in before.results] == [
        ("content", "return 1"),
        ("detailedContent", "1: return 1"),
    ]
    assert "missing-turn-ids-native-call-identities-used" in projection.limitations
    assert projection.barriers == ()
    assert "/fixture" not in repr(projection)


def test_missing_turn_ids_are_a_passive_profile_only() -> None:
    document = _document(_records())
    with pytest.raises(AdapterError, match="turn ID"):
        PARSER.parse(document)
    assert len(project_reacquisition(document).operations) == 2


def test_known_turn_ids_must_agree_even_when_request_turn_is_missing() -> None:
    records = _records()
    records[2]["data"]["turnId"] = "turn-1"
    records[3]["data"]["turnId"] = "turn-2"
    with pytest.raises(AdapterError, match="different turns"):
        project_reacquisition(_document(records))


@pytest.mark.parametrize(
    "variant",
    [
        "different-arguments",
        "different-tool",
        "missing-start",
        "duplicate-start",
        "duplicate-completion",
        "duplicate-call",
        "orphan-completion",
        "malformed-success",
        "missing-arguments",
    ],
)
def test_invalid_tool_identities_and_relationships_refuse(variant: str) -> None:
    records = _records()
    if variant == "different-arguments":
        records[2]["data"]["arguments"] = {"path": "/fixture/other.py"}
    elif variant == "different-tool":
        records[2]["data"]["toolName"] = "shell"
    elif variant == "missing-start":
        del records[2]
    elif variant == "duplicate-start":
        records.insert(3, copy.deepcopy(records[2]) | {"id": "new-id"})
    elif variant == "duplicate-completion":
        records.insert(4, copy.deepcopy(records[3]) | {"id": "new-id"})
    elif variant == "duplicate-call":
        records[6]["data"]["toolRequests"][0]["toolCallId"] = "call-0"
    elif variant == "orphan-completion":
        records[3]["data"]["toolCallId"] = "unknown"
    elif variant == "malformed-success":
        records[3]["data"]["success"] = "true"
    else:
        del records[1]["data"]["toolRequests"][0]["arguments"]
    with pytest.raises(AdapterError):
        project_reacquisition(_document(records))


@pytest.mark.parametrize("success", [False, None])
def test_failed_or_unknown_compaction_is_a_barrier(success: bool | None) -> None:
    records = _records()
    records[5]["data"]["success"] = success
    projection = project_reacquisition(_document(records))
    assert projection.boundaries == ()
    assert [b.line for b in projection.barriers] == [5, 6]
    assert "failed-or-unapplied-compaction-attempts" in projection.limitations


def test_incomplete_compaction_retains_a_barrier() -> None:
    records = _records()
    del records[5]
    projection = project_reacquisition(_document(records))
    assert projection.boundaries == ()
    assert [b.line for b in projection.barriers] == [5]
    assert "incomplete-compaction-attempts" in projection.limitations


def test_unpaired_and_unidentified_compactions_are_not_successes() -> None:
    records = _records()
    del records[4]
    projection = project_reacquisition(_document(records))
    assert projection.boundaries == ()
    assert len(projection.barriers) == 1
    records = _records()
    del records[4]["data"]["compactionAttemptId"]
    projection = project_reacquisition(_document(records))
    assert projection.boundaries == ()
    assert len(projection.barriers) == 2


def test_failed_truncated_empty_and_missing_results_remain_explicit() -> None:
    records = _records()
    records[3]["data"].update(success=False, result={"content": "failed", "truncated": True})
    records[8]["data"]["result"] = {"content": "", "status": "completed"}
    projection = project_reacquisition(_document(records))
    assert projection.operations[0].status == "failed"
    assert projection.operations[0].results[0].completeness == "truncated"
    assert projection.operations[1].results[0].value == ""
    records[8]["data"]["result"] = {"status": "completed"}
    assert project_reacquisition(_document(records)).operations[1].results == ()


def test_incomplete_call_is_retained_without_inventing_completion() -> None:
    records = _records()
    del records[8]
    operation = project_reacquisition(_document(records)).operations[1]
    assert operation.status == "incomplete"
    assert operation.completion is None
    assert operation.results == ()


def test_nested_copied_history_is_opaque() -> None:
    records = _records()
    records.append(
        {
            "id": "snapshot",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "model.messages_snapshot",
            "data": {"messages": copy.deepcopy(records[1:4])},
        }
    )
    projection = project_reacquisition(_document(records))
    assert len(projection.operations) == 2
    records[-1]["type"] = "future.event"
    assert project_reacquisition(_document(records)).unknown_records == 1


@pytest.mark.parametrize("changed", ["producer", "version", "duplicate-record"])
def test_producer_schema_and_record_identity_validation_are_reused(changed: str) -> None:
    records = _records()
    if changed == "duplicate-record":
        records[1]["id"] = records[0]["id"]
    else:
        records[0]["data"][changed] = "unsupported"
    with pytest.raises(AdapterError):
        project_reacquisition(_document(records))


def test_recorded_workspace_is_retained_without_opening_resources() -> None:
    records = _records()
    for index in (1, 6):
        records[index]["data"]["toolRequests"][0]["arguments"]["path"] = "source.py"
    for index in (2, 7):
        records[index]["data"]["arguments"]["path"] = "source.py"
    projection = project_reacquisition(_document(records))
    assert projection.operations[0].category == "unclassified"
    assert projection.operations[0].workspace_digest is None
    records[0]["data"]["context"] = {"cwd": "/fixture/workspace"}
    projection = project_reacquisition(_document(records))
    assert projection.operations[0].category == "file-read"
    assert projection.operations[0].workspace_digest is not None
    assert projection.operations[0].workspace_digest == projection.operations[1].workspace_digest
    assert "captured-output-completeness-unverified" in projection.limitations
    assert "/fixture/workspace" not in repr(projection)


def test_unknown_compaction_and_duplicate_completions_are_barriers() -> None:
    records = _records()
    records.insert(6, copy.deepcopy(records[5]) | {"id": "duplicate-compaction-complete"})
    projection = project_reacquisition(_document(records))
    assert len(projection.boundaries) == 1
    assert [anchor.line for anchor in projection.barriers] == [7]
    records[4]["type"] = "session.compaction_future"
    projection = project_reacquisition(_document(records))
    assert not projection.boundaries
    assert [anchor.line for anchor in projection.barriers] == [5, 6, 7]


@pytest.mark.parametrize("container", ["data", "result"])
@pytest.mark.parametrize("value", ["true", 1, None])
def test_malformed_truncation_flags_cannot_imply_complete_capture(
    container: str, value: object
) -> None:
    records = _records()
    target = records[3]["data"] if container == "data" else records[3]["data"]["result"]
    target["truncated"] = value
    with pytest.raises(AdapterError, match="truncated flag must be a boolean"):
        project_reacquisition(_document(records))


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": 123},
        {"path": ""},
        {"path": "/fixture/\x00file"},
        {"path": "/fixture/file", "view_range": [True, 4]},
        {"path": "/fixture/file", "view_range": [4, 1]},
        {"path": "/fixture/file", "view_range": "1-4"},
    ],
)
def test_malformed_native_view_arguments_remain_unclassified(arguments: dict[str, Any]) -> None:
    records = _records()
    records[1]["data"]["toolRequests"][0]["arguments"] = arguments
    records[2]["data"]["arguments"] = arguments
    assert project_reacquisition(_document(records)).operations[0].category == "unclassified"


def test_boundary_identity_includes_its_source_position() -> None:
    projection = project_reacquisition(_document(_records()))
    boundary = projection.boundaries[0]
    assert boundary.id == f"boundary-{boundary.end.line}-{boundary.end.digest}"
