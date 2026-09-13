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
from ithrl.adapters.copilot.context import compile_context
from ithrl.adapters.copilot.decisions import _shell_actions, enumerate_decisions
from ithrl.behavior import FinalResponseTarget, ShellActionTarget
from ithrl.context import JsonValue, content_digest

FIXTURE = Path(__file__).parents[1] / "fixtures/copilot-session/events.jsonl"


def _fixture_records() -> list[dict[str, Any]]:
    document = read_jsonl(FIXTURE, limits=ParsingLimits())
    return copy.deepcopy(list(document.records))


def _session(records: list[dict[str, Any]] | None = None):
    if records is None:
        return PARSER.parse(read_jsonl(FIXTURE, limits=ParsingLimits()))
    raw = b"".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for record in records
    )
    document = JsonlDocument(
        raw=raw,
        records=tuple(records),
        source=SourceFacts(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)),
    )
    return PARSER.parse(document)


def test_fixture_exposes_one_content_free_final_response_summary() -> None:
    drafts = enumerate_decisions(_session())

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.observation.final_response == "Fixture complete."
    assert draft.observation.shell_actions == ()
    assert isinstance(draft.targets[0].target, FinalResponseTarget)
    assert draft.targets[0].target.text_equals == "Fixture complete."
    assert draft.targets[0].evidence_support == "supported"
    assert draft.targets[0].experimental_support == "unvalidated"
    assert draft.limitations == (
        "experimental-support-unvalidated",
        "opaque-context-frozen",
    )

    summary = draft.summarize()
    assert summary.decision_id == draft.decision_id
    assert summary.targets[0].type == "final-response"
    assert "Fixture complete." not in summary.model_dump_json()


def test_fixture_skill_request_is_not_mislabeled_as_a_shell_action() -> None:
    records = _fixture_records()
    records[11]["data"]["toolRequests"][0]["arguments"] = {"command": "pwd"}

    drafts = enumerate_decisions(_session(records))

    assert [target.target.type for draft in drafts for target in draft.targets] == [
        "final-response"
    ]


def test_selector_uses_native_session_turn_call_and_record_facts() -> None:
    session = _session()
    record = session.records[24]
    selector = enumerate_decisions(session)[0].selector

    assert selector.harness == "copilot"
    assert selector.session_id == "20000000-0000-4000-8000-000000000001"
    assert selector.turn_id == "30000000-0000-4000-8000-000000000001"
    assert selector.kind == "copilot-assistant-message"
    assert selector.native_id == "70000000-0000-4000-8000-000000000099"
    assert selector.source == "source/events.jsonl"
    assert selector.context_before_line == 19
    assert selector.record.line == 25
    assert selector.record.identity_key == "id"
    assert selector.record.identity_value == record["id"]
    assert selector.record_digest == content_digest(record)


def test_enumeration_is_deterministic_and_does_not_change_source_evidence() -> None:
    session = _session()
    original_bytes = session.native_bytes
    original_records = session.records

    first = enumerate_decisions(session)
    second = enumerate_decisions(session)

    assert first == second
    assert session.native_bytes == original_bytes
    assert session.records == original_records


def test_incomplete_trailing_turn_is_not_enumerated() -> None:
    records = _fixture_records()[:23]

    assert enumerate_decisions(_session(records)) == ()


def test_message_after_completed_turn_is_not_enumerated() -> None:
    records = _fixture_records()
    late = copy.deepcopy(records[24])
    late["id"] = "10000000-0000-4000-8000-000000000098"
    late["parentId"] = records[25]["id"]
    late["timestamp"] = "2026-01-01T00:00:26.500Z"
    late["data"]["messageId"] = "70000000-0000-4000-8000-000000000098"
    late["data"]["content"] = "Late response."
    records.insert(26, late)

    drafts = enumerate_decisions(_session(records))

    assert len(drafts) == 1
    assert drafts[0].observation.final_response == "Fixture complete."


def test_model_lifecycle_records_are_not_required_for_the_turn_boundary() -> None:
    records = _fixture_records()
    records[17]["type"] = "fixture.model_start"
    records[19]["type"] = "fixture.model_success"

    assert len(enumerate_decisions(_session(records))) == 1


def test_model_call_boundary_uses_record_order_not_unrelated_native_ids() -> None:
    records = _fixture_records()
    records[24]["data"]["apiCallId"] = "unrelated-api-call-id"

    drafts = enumerate_decisions(_session(records))

    assert len(drafts) == 1
    assert drafts[0].selector.context_before_line == 19


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase", "commentary"),
        ("parentToolCallId", "parent-tool-call"),
    ],
)
def test_non_top_level_final_messages_are_not_final_response_targets(
    field: str,
    value: str,
) -> None:
    records = _fixture_records()
    records[24]["data"][field] = value

    assert enumerate_decisions(_session(records)) == ()


def test_nested_repeated_turn_intervals_keep_their_own_boundaries() -> None:
    records = _fixture_records()
    inner_start = copy.deepcopy(records[18])
    inner_start["id"] = "10000000-0000-4000-8000-000000000096"
    inner_end = copy.deepcopy(records[25])
    inner_end["id"] = "10000000-0000-4000-8000-000000000097"
    outer_final = copy.deepcopy(records[24])
    outer_final["id"] = "10000000-0000-4000-8000-000000000098"
    outer_final["data"]["messageId"] = "70000000-0000-4000-8000-000000000098"
    records[24:24] = [inner_start]
    records[26:26] = [inner_end, outer_final]

    drafts = enumerate_decisions(_session(records))

    assert len(drafts) == 2
    assert [draft.selector.context_before_line for draft in drafts] == [25, 19]


@pytest.mark.parametrize("content", ["", {"opaque": True}])
def test_only_supported_assistant_messages_are_final_responses(content: object) -> None:
    records = _fixture_records()
    records[24]["data"]["content"] = content

    assert enumerate_decisions(_session(records)) == ()


def test_oversized_final_response_is_omitted() -> None:
    records = _fixture_records()
    records[24]["data"]["content"] = "x" * (64 * 1024 + 1)

    assert enumerate_decisions(_session(records)) == ()


def test_maximum_escaped_response_fits_case_metadata_limit() -> None:
    records = _fixture_records()
    records[24]["data"]["content"] = "\\" * (64 * 1024)
    session = _session(records)
    draft = enumerate_decisions(session)[0]
    selected = draft.select(
        draft.targets[0].target_id,
        context=compile_context(session, draft.selector),
    )

    assert len(selected.evidence.model_dump_json().encode()) < 1024 * 1024


def test_shell_shaped_assistant_request_exposes_an_exact_shell_target() -> None:
    records = _fixture_records()
    records[11]["data"]["toolRequests"][0].update({"name": "bash", "arguments": {"command": "pwd"}})
    records[12]["data"].update({"toolName": "bash", "arguments": {"command": "pwd"}})

    drafts = enumerate_decisions(_session(records))

    assert len(drafts) == 2
    shell = drafts[0]
    assert shell.selector.kind == "copilot-assistant-message"
    assert shell.selector.native_id == "70000000-0000-4000-8000-000000000001"
    assert shell.selector.record.line == 12
    assert shell.observation.shell_actions[0].tool_name == "bash"
    assert shell.observation.shell_actions[0].command == "pwd"
    target = shell.targets[0].target
    assert isinstance(target, ShellActionTarget)
    assert target.tool_name_equals == "bash"
    assert target.command_equals == "pwd"
    assert target.count == 1


def test_excessive_shell_action_count_is_omitted() -> None:
    requests: list[JsonValue] = [
        {
            "toolCallId": f"fixture-shell-{index}",
            "name": "shell",
            "arguments": {"command": "pwd"},
        }
        for index in range(33)
    ]

    assert _shell_actions(requests) == ()


def test_context_precedes_decision_and_classifies_records_conservatively() -> None:
    session = _session()
    draft = enumerate_decisions(session)[0]
    nodes = compile_context(session, draft.selector).nodes

    assert [node.selector.line for node in nodes if node.selector] == list(range(1, 19))
    assert all(
        node.digest == content_digest(session.records[index]) for index, node in enumerate(nodes)
    )

    by_kind = {node.kind: node for node in nodes}
    assert by_kind["user.message"].mutable
    assert not by_kind["session.start"].mutable
    assert by_kind["session.start"].protected
    assert not by_kind["assistant.turn_start"].mutable
    assert by_kind["assistant.turn_start"].protected
    assert not by_kind["model.model_call_started"].mutable


def test_context_preserves_tool_request_start_and_completion_dependencies() -> None:
    session = _session()
    draft = enumerate_decisions(session)[0]
    context = compile_context(session, draft.selector)
    by_line = {node.selector.line: node for node in context.nodes if node.selector}

    request = by_line[12]
    start = by_line[13]
    completion = by_line[14]
    assert request.kind == "assistant.message"
    assert start.depends_on == (request.id,)
    assert completion.depends_on == (start.id,)
    assert by_line[16].mutable
    assert by_line[16].kind == "skill.invoked"
    assert not by_line[17].mutable
    assert by_line[17].kind == "fixture.unknown_event"
