from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from ithrl.case import CaseExistsError
from ithrl.synthetic import (
    InferenceSelectionError,
    SyntheticTraceFormatError,
    extract_inference,
    list_inferences,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"
INFERENCE_ID = "synthetic-ac-001"


def _fixture_payload() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_list_inferences_summary_fields() -> None:
    summaries = list_inferences(FIXTURE)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.inference_call_id == INFERENCE_ID
    assert summary.provider == "fake"
    assert summary.endpoint == "local"
    assert summary.model_requested == "fake-deterministic-v1"
    assert summary.model_returned == "fake-deterministic-v1"
    assert summary.input_items == 7
    assert summary.observed_tool_names == ("exec_command",)


@pytest.mark.parametrize("inference_call_id", [None, "", "missing-call"])
def test_extract_requires_an_explicit_existing_inference(
    tmp_path: Path,
    inference_call_id: str | None,
) -> None:
    with pytest.raises(InferenceSelectionError, match="available inferences: synthetic-ac-001"):
        extract_inference(
            FIXTURE,
            inference_call_id=inference_call_id,
            case_path=tmp_path / "case",
        )

    assert not (tmp_path / "case").exists()


def test_duplicate_inference_ids_are_ambiguous_and_rejected(tmp_path: Path) -> None:
    payload = _fixture_payload()
    inferences = payload["inferences"]
    assert isinstance(inferences, list)
    inferences.append(inferences[0])
    trace = tmp_path / "duplicate.json"
    trace.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SyntheticTraceFormatError, match="invalid synthetic trace"):
        list_inferences(trace)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    trace = tmp_path / "duplicate-key.json"
    trace.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")

    with pytest.raises(SyntheticTraceFormatError, match="duplicate key 'schema_version'"):
        list_inferences(trace)


def test_invalid_utf8_is_reported_as_a_trace_format_error(tmp_path: Path) -> None:
    trace = tmp_path / "invalid-utf8.json"
    trace.write_bytes(b"\xff\xfe")

    with pytest.raises(SyntheticTraceFormatError, match="cannot read synthetic trace"):
        list_inferences(trace)


def test_duplicate_normalized_marker_sets_are_rejected(tmp_path: Path) -> None:
    payload = _fixture_payload()
    behavior = payload["inferences"][0]["fake_behavior"]
    behavior["alternative_marker_sets"] = [["C", "A"]]
    trace = tmp_path / "duplicate-markers.json"
    trace.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SyntheticTraceFormatError, match="marker sets must be distinct"):
        list_inferences(trace)


@pytest.mark.parametrize(
    "captured_at",
    [
        "2026-09-01 10:00:00+00:00",
        "2026-09-01T10:00:00+00",
        "2026-09-01T10:00:00+00:00:30",
    ],
)
def test_trace_rejects_timestamps_outside_supported_rfc3339_grammar(
    tmp_path: Path,
    captured_at: str,
) -> None:
    payload = _fixture_payload()
    payload["inferences"][0]["captured_at"] = captured_at
    trace = tmp_path / "invalid-timestamp.json"
    trace.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SyntheticTraceFormatError, match="RFC 3339"):
        list_inferences(trace)


def test_oversized_trace_is_rejected_before_reading(tmp_path: Path) -> None:
    trace = tmp_path / "oversized.json"
    with trace.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)

    with pytest.raises(SyntheticTraceFormatError, match=r"exceeds the .*byte limit"):
        list_inferences(trace)


def test_symbolic_link_trace_is_rejected(tmp_path: Path) -> None:
    trace = tmp_path / "trace-link.json"
    trace.symlink_to(FIXTURE)

    with pytest.raises(SyntheticTraceFormatError, match="regular file, not a link"):
        list_inferences(trace)


def test_fake_behavior_cannot_be_smuggled_into_provider_request(tmp_path: Path) -> None:
    payload = _fixture_payload()
    request = payload["inferences"][0]["request"]
    assert isinstance(request, dict)
    request["fake_behavior"] = {"required_markers": ["A", "C"]}
    trace = tmp_path / "reserved-field.json"
    trace.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SyntheticTraceFormatError, match="invalid synthetic trace"):
        extract_inference(
            trace,
            inference_call_id=INFERENCE_ID,
            case_path=tmp_path / "case",
        )

    assert not (tmp_path / "case").exists()


@pytest.mark.parametrize(
    ("input_items", "message"),
    [
        (
            [
                {"type": "function_call", "id": "call-1", "name": "exec_command"},
                {"type": "function_call", "id": "call-1", "name": "exec_command"},
            ],
            "duplicate function call ID",
        ),
        (
            [{"type": "function_call_output", "call_id": "missing", "output": "done"}],
            "has no preceding call",
        ),
        (
            [
                {"type": "function_call_output", "call_id": "call-1", "output": "done"},
                {"type": "function_call", "id": "call-1", "name": "exec_command"},
            ],
            "has no preceding call",
        ),
    ],
)
def test_extract_rejects_invalid_function_call_dependencies(
    tmp_path: Path, input_items: list[dict[str, Any]], message: str
) -> None:
    payload = _fixture_payload()
    inference = payload["inferences"][0]
    inference["request"]["input"] = input_items
    inference["fake_behavior"]["required_markers"] = [
        "exec_command" if "exec_command" in json.dumps(input_items) else "done"
    ]
    trace = tmp_path / "invalid-dependencies.json"
    trace.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SyntheticTraceFormatError, match=message):
        extract_inference(trace, inference_call_id=INFERENCE_ID, case_path=tmp_path / "case")


def test_extract_preserves_exact_request_and_excludes_fake_behavior(tmp_path: Path) -> None:
    workspace = extract_inference(
        FIXTURE,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "case",
    )
    payload = _fixture_payload()
    inference = payload["inferences"][0]
    expected_request = inference["request"]

    evidence = workspace.load()
    assert evidence.request == expected_request
    assert evidence.materialize() == expected_request
    assert json.loads((workspace.root / "request.json").read_text()) == expected_request
    assert "fake_behavior" not in (workspace.root / "request.json").read_text()


def test_extract_creates_stable_context_and_experiment_metadata(tmp_path: Path) -> None:
    first = extract_inference(
        FIXTURE,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "first",
    ).load()
    second = extract_inference(
        FIXTURE,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "second",
    ).load()

    assert first.digest == second.digest
    assert first.context.nodes == second.context.nodes
    input_nodes = [node for node in first.context.nodes if node.path.startswith("/input/")]
    assert [node.path for node in input_nodes] == [f"/input/{index}" for index in range(7)]
    assert all(node.mutable and not node.protected for node in input_nodes)

    tool_node = next(node for node in first.context.nodes if node.path == "/tools/0")
    assert tool_node.kind == "tool_schema"
    assert not tool_node.mutable
    assert tool_node.protected
    frozen_paths = {node.path for node in first.context.nodes if not node.mutable}
    assert frozen_paths == {"/model", "/tools/0", "/store"}

    experiment = tomllib.loads(first.default_experiment)
    assert experiment["target"] == {
        "type": "shell-action",
        "tool_name_equals": "exec_command",
    }
    assert experiment["oracle"] == {
        "type": "structural",
        "protected_context_ids": [tool_node.id],
    }
    assert experiment["runner"] == {
        "type": "fake",
        "required_markers": ["A", "C"],
        "alternative_marker_sets": [],
        "tool_name": "exec_command",
    }


def test_extract_groups_function_output_with_its_preceding_call(tmp_path: Path) -> None:
    payload = _fixture_payload()
    inference = payload["inferences"][0]
    inference["request"]["input"] = [
        {"role": "user", "content": "A"},
        {"type": "function_call", "id": "call-1", "name": "exec_command"},
        {"type": "function_call_output", "call_id": "call-1", "output": "C"},
    ]
    trace = tmp_path / "function-dependency.json"
    trace.write_text(json.dumps(payload), encoding="utf-8")

    evidence = extract_inference(
        trace,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "case",
    ).load()

    call_node = next(node for node in evidence.context.nodes if node.path == "/input/1")
    output_node = next(node for node in evidence.context.nodes if node.path == "/input/2")
    assert call_node.kind == "function_call"
    assert call_node.depends_on == ()
    assert output_node.kind == "function_call_output"
    assert output_node.depends_on == (call_node.id,)


def test_extract_refuses_to_overwrite_existing_case(tmp_path: Path) -> None:
    target = tmp_path / "case"
    extract_inference(FIXTURE, inference_call_id=INFERENCE_ID, case_path=target)

    with pytest.raises(CaseExistsError, match="already exists"):
        extract_inference(FIXTURE, inference_call_id=INFERENCE_ID, case_path=target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_extract_delegates_private_workspace_permissions_to_core(tmp_path: Path) -> None:
    workspace = extract_inference(
        FIXTURE,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "case",
    )

    assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((workspace.root / "experiments").stat().st_mode) == 0o700
    for path in (
        workspace.root / "case.toml",
        workspace.root / "request.json",
        workspace.root / "context.jsonl",
        workspace.root / "experiments/default.toml",
        workspace.root / ".gitignore",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
