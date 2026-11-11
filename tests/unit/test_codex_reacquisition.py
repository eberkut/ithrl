"""Passive Codex projection keeps native calls separate from compacted history."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from ithrl.adapters._jsonl import read_jsonl_bytes
from ithrl.adapters.codex.reacquisition import project_reacquisition
from ithrl.adapters.contracts import ParsingLimits
from ithrl.adapters.errors import AdapterError
from ithrl.adapters.reacquisition import PassiveProjection
from ithrl.context import JsonObject, JsonValue, content_digest
from ithrl.reacquisition import compare_reacquisition


def _record(kind: str, payload: JsonObject) -> JsonObject:
    return {"timestamp": "2026-01-01T00:00:00Z", "type": kind, "payload": payload}


def _meta() -> JsonObject:
    return _record(
        "session_meta",
        {
            "id": "fixture-session",
            "session_id": "fixture-family",
            "timestamp": "2026-01-01T00:00:00Z",
            "cli_version": "0.153.4",
            "cwd": "/workspace/fixture",
        },
    )


def _call(call_id: str, *, kind: str = "function_call", **extra: JsonValue) -> JsonObject:
    return _record(
        "response_item",
        {
            "type": kind,
            "id": f"item-{call_id}",
            "call_id": call_id,
            "name": "read_file",
            "arguments" if kind == "function_call" else "input": '{"path":"fixture.txt"}',
            "status": "completed",
            **extra,
        },
    )


def _output(call_id: str, value: JsonValue = "fixture text", **extra: JsonValue) -> JsonObject:
    return _record(
        "response_item",
        {
            "type": "function_call_output",
            "id": f"result-{call_id}",
            "call_id": call_id,
            "output": value,
            **extra,
        },
    )


def _compacted(**extra: JsonValue) -> JsonObject:
    return _record("compacted", {"message": "Fixture summary", "replacement_history": [], **extra})


def _project(*records: JsonObject) -> PassiveProjection:
    raw = b"\n".join(json.dumps(record).encode() for record in (_meta(), *records)) + b"\n"
    return project_reacquisition(read_jsonl_bytes(raw, limits=ParsingLimits()))


def test_source_bound_completed_calls_and_boundary_ignore_nested_history() -> None:
    before, output = _call("before"), _output("before")
    copied = deepcopy(before)
    boundary = _compacted(replacement_history=[copied, deepcopy(output)])
    projection = _project(before, output, boundary, _call("after"), _output("after"))
    assert projection.harness == "codex"
    assert projection.record_count == 6
    assert len(projection.operations) == 2
    assert len(projection.boundaries) == 1
    (selected,) = projection.boundaries
    assert selected.start == selected.end
    assert selected.start.line == 4
    assert selected.id == f"boundary-4-{content_digest(boundary)}"
    first, second = projection.operations
    assert first.call.line == 2
    assert first.completion is not None
    assert first.completion.line == 3
    assert first.status == second.status == "completed"
    assert first.category == "unclassified"
    assert first.workspace_digest == second.workspace_digest == content_digest("/workspace/fixture")
    assert first.results[0].scope == "output"
    assert first.results[0].value == "fixture text"
    assert first.arguments == second.arguments
    assert "fixture text" not in repr(projection)
    assert "fixture.txt" not in repr(projection)
    assert "resource-state-unknown" in projection.limitations
    assert "native-output-completeness-unverified" in projection.limitations


def test_workspace_context_and_native_call_kinds_remain_part_of_identity() -> None:
    custom = _call("custom", kind="custom_tool_call", name="exec", namespace="fixture")
    output = _output("custom")
    payload = output["payload"]
    assert isinstance(payload, dict)
    payload["type"] = "custom_tool_call_output"
    projection = _project(
        _call("before"),
        _output("before"),
        _compacted(),
        _record("turn_context", {"cwd": "/workspace/other"}),
        custom,
        output,
    )
    first, second = projection.operations
    assert first.workspace_digest != second.workspace_digest
    assert second.arguments == {
        "type": "custom_tool_call",
        "namespace": "fixture",
        "input": '{"path":"fixture.txt"}',
    }
    assert second.category == "unclassified"


@pytest.mark.parametrize(
    ("call_status", "output_extra", "expected"),
    [
        ("completed", {}, "completed"),
        (None, {}, "completed"),
        ("in_progress", {}, "incomplete"),
        ("failed", {}, "failed"),
        ("completed", {"status": "failed"}, "failed"),
        ("completed", {"status": "incomplete"}, "incomplete"),
        ("completed", {"is_error": True}, "failed"),
        ("completed", {"error": {"code": "fixture"}}, "failed"),
        ("completed", {"status": []}, "incomplete"),
    ],
)
def test_native_completion_and_failures_are_explicit(
    call_status: JsonValue, output_extra: dict[str, JsonValue], expected: str
) -> None:
    projection = _project(_call("one", status=call_status), _output("one", **output_extra))
    assert projection.operations[0].status == expected


def test_missing_result_stays_incomplete_and_explicit_truncation_is_retained() -> None:
    projection = _project(
        _call("pending"),
        _call("truncated"),
        _output("truncated", truncated=True),
        _call("nested"),
        _output("nested", {"text": "partial", "truncated": True}),
    )
    pending, truncated, nested = projection.operations
    assert pending.completion is None
    assert pending.status == "incomplete"
    assert not pending.results
    assert truncated.results[0].completeness == "truncated"
    assert nested.results[0].completeness == "truncated"


@pytest.mark.parametrize(
    "record",
    [
        _compacted(status="failed"),
        _compacted(success=False),
        _compacted(error={"code": "fixture"}),
        _compacted(status=[]),
        _record("compacted", {"message": "summary"}),
        _compacted(message=""),
        _record("event_msg", {"type": "compaction_started"}),
        _record("context_compaction", {}),
    ],
)
def test_failed_partial_or_unrecognized_compaction_is_an_epoch_barrier(record: JsonObject) -> None:
    projection = _project(record)
    assert not projection.boundaries
    assert len(projection.barriers) == 1
    assert "unverified-compaction-transitions" in projection.limitations


@pytest.mark.parametrize(
    "records",
    [
        [_call("same"), _call("same")],
        [_call("one"), _output("one"), _output("one")],
        [_output("missing")],
        [_meta()],
        [_call("one", name=None)],
        [_call("one", arguments={"path": "fixture.txt"})],
    ],
)
def test_malformed_native_relationships_are_refused(records: list[JsonObject]) -> None:
    with pytest.raises(AdapterError):
        _project(*records)


def test_unknown_records_are_opaque_and_cannot_inject_retrievals() -> None:
    projection = _project(
        _record("fixture.unknown", {"nested": _call("copied")}),
        _record("event_msg", {"type": "fixture_unknown_event"}),
    )
    assert projection.unknown_records == 2
    assert not projection.operations


def test_lifecycle_brackets_fold_into_successful_compaction_interval() -> None:
    projection = _project(
        _call("before"),
        _output("before"),
        _record("event_msg", {"type": "compaction_start"}),
        _compacted(),
        _record("event_msg", {"type": "compaction_complete"}),
        _call("after"),
        _output("after"),
    )
    (boundary,) = projection.boundaries
    assert boundary.start.line == 4
    assert boundary.end.line == 6
    assert not projection.barriers


def test_failed_lifecycle_invalidates_its_compacted_record_but_not_an_earlier_epoch() -> None:
    projection = _project(
        _compacted(),
        _call("between"),
        _output("between"),
        _record("event_msg", {"type": "compaction_start"}),
        _compacted(),
        _record("event_msg", {"type": "compaction_complete", "status": "failed"}),
    )
    assert len(projection.boundaries) == 1
    assert projection.boundaries[0].start.line == 2
    assert projection.barriers


def test_unknown_word_containing_compact_does_not_invent_a_transition() -> None:
    projection = _project(_record("event_msg", {"type": "fixture_compact_word"}))
    assert not projection.barriers
    assert projection.unknown_records == 1


def test_identical_compacted_records_have_distinct_selectable_epoch_boundaries() -> None:
    compacted = _compacted()
    projection = _project(
        _call("first"),
        _output("first", "first epoch"),
        compacted,
        _call("middle"),
        _output("middle", "middle epoch"),
        deepcopy(compacted),
        _call("last"),
        _output("last", "last epoch"),
    )
    first, second = projection.boundaries
    assert first.id != second.id
    assert first.start.digest == second.start.digest
    report = compare_reacquisition(projection, boundary_id=second.id)
    assert report["boundary"]["start"]["line"] == 7
    (group,) = report["groups"]
    assert group["before"]["call"]["line"] == 5
    assert group["after"]["call"]["line"] == 8


@pytest.mark.parametrize("success", [False, 0, 1, "", [], {}, None, "true"])
@pytest.mark.parametrize("terminal", [False, True])
def test_explicit_compaction_success_requires_boolean_true(
    success: JsonValue, terminal: bool
) -> None:
    records = (
        [_compacted(), _record("event_msg", {"type": "compaction_complete", "success": success})]
        if terminal
        else [_compacted(success=success)]
    )
    projection = _project(*records)
    assert not projection.boundaries
    assert projection.barriers


def test_boolean_success_is_accepted_for_compacted_and_terminal_lifecycle() -> None:
    projection = _project(
        _compacted(success=True),
        _record("event_msg", {"type": "compaction_complete", "success": True}),
    )
    assert len(projection.boundaries) == 1
    assert not projection.barriers


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("flag", [0, 1, "", [], {}, None, "false"])
def test_malformed_truncation_flag_never_claims_a_captured_result(
    flag: JsonValue, nested: bool
) -> None:
    output = (
        _output("one", {"text": "fixture", "truncated": flag})
        if nested
        else _output("one", truncated=flag)
    )
    projection = _project(_call("one"), output)
    assert projection.operations[0].results[0].completeness == "unknown"


@pytest.mark.parametrize("exit_code", [0, 1, -9, 127])
@pytest.mark.parametrize("marker", ["Output:", "Final output:"])
def test_native_exec_exit_header_controls_matching(exit_code: int, marker: str) -> None:
    from ithrl.reacquisition import compare_reacquisition

    value = (
        "Chunk ID: fixture\nWall time: 0.1000 seconds\n"
        f"Process exited with code {exit_code}\nOriginal token count: 8\n{marker}\nsubstantive text"
    )
    projection = _project(
        _call("before", name="exec_command"),
        _output("before", value),
        _compacted(),
        _call("after", name="exec_command"),
        _output("after", value),
    )
    assert projection.operations[0].status == ("completed" if exit_code == 0 else "failed")
    report = compare_reacquisition(projection, boundary_id=projection.boundaries[0].id)
    assert bool(report["groups"]) is (exit_code == 0)


def test_exit_code_text_in_command_body_is_not_native_failure_metadata() -> None:
    value = (
        "Wall time: 0.1000 seconds\nProcess exited with code 0\nOutput:\n"
        "Process exited with code 1\n"
    )
    projection = _project(_call("one", name="exec_command"), _output("one", value))
    assert projection.operations[0].status == "completed"
    # A read operation can return a file containing a native-looking header verbatim.
    value = "Wall time: 0.1000 seconds\nProcess exited with code 1\nOutput:\nexample"
    projection = _project(_call("one"), _output("one", value))
    assert projection.operations[0].status == "completed"


@pytest.mark.parametrize("container", ["string", "items"])
@pytest.mark.parametrize(
    "prefix", ["", "Wall time: 0.1000 seconds\nProcess exited with code 0\nOutput:\n"]
)
def test_native_truncation_warning_excludes_positive_matches(container: str, prefix: str) -> None:
    from ithrl.reacquisition import compare_reacquisition

    text = (
        prefix
        + "Warning: truncated output (original token count: 9000)\nTotal output lines: 800\n\npartial content"
    )
    value: JsonValue = text if container == "string" else [{"type": "input_text", "text": text}]
    projection = _project(
        _call("before"),
        _output("before", value, truncated=False),
        _compacted(),
        _call("after"),
        _output("after", value),
    )
    assert projection.operations[0].results[0].completeness == "truncated"
    assert projection.operations[0].status == "completed"
    assert projection.operations[0].results[0].value == value
    assert (
        compare_reacquisition(projection, boundary_id=projection.boundaries[0].id)["groups"] == []
    )


def test_ordinary_warning_prose_is_not_a_native_truncation_warning() -> None:
    projection = _project(
        _call("one"),
        _output("one", "The guide mentions Warning: truncated output (original token count: 9)."),
    )
    assert projection.operations[0].results[0].completeness == "captured"


@pytest.mark.parametrize(
    "value",
    [
        "unrecognized future exec format",
        "Wall time: 0.1 seconds\nProcess running with session ID 42\nOutput:\npending",
        "Wall time: 0.1 seconds\nProcess exited with code invalid\nOutput:\ntext",
    ],
)
def test_unrecognized_running_or_malformed_exec_output_is_unresolved(value: str) -> None:
    projection = _project(_call("one", name="exec_command"), _output("one", value))
    assert projection.operations[0].status == "incomplete"


def test_native_exec_text_content_items_use_the_same_header_parser() -> None:
    value: JsonValue = [
        {
            "type": "input_text",
            "text": "Wall time: 0.1 seconds\nProcess exited with code 1\nOutput:\nfailed output",
        }
    ]
    projection = _project(_call("one", name="write_stdin"), _output("one", value))
    assert projection.operations[0].status == "failed"


def test_first_output_delimiter_ends_header_even_with_another_delimiter_in_body() -> None:
    value = (
        "Wall time: 0.1 seconds\nProcess running with session ID 42\nFinal output:\n"
        "Process exited with code 0\nOutput:\ncommand body"
    )
    projection = _project(_call("one", name="write_stdin"), _output("one", value))
    assert projection.operations[0].status == "incomplete"


@pytest.mark.parametrize(
    "extra",
    ["Process exited with code 1", "Original token count: invalid", "Future metadata: value"],
)
def test_ambiguous_or_unrecognized_exec_header_metadata_is_not_success(extra: str) -> None:
    value = f"Wall time: 0.1 seconds\nProcess exited with code 0\n{extra}\nOutput:\ntext"
    projection = _project(_call("one", name="exec_command"), _output("one", value))
    assert projection.operations[0].status == "incomplete"
