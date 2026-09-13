from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ithrl.adapters._jsonl import JsonlDocument, read_jsonl
from ithrl.adapters.codex import PARSER
from ithrl.adapters.codex import decisions as codex_decisions
from ithrl.adapters.codex.context import compile_context
from ithrl.adapters.codex.decisions import enumerate_decisions
from ithrl.adapters.contracts import NativeSession, ParsingLimits, SourceFacts
from ithrl.behavior import FinalResponseTarget
from ithrl.context import ContextNode, JsonObject, SourceRecordSelector, content_digest
from ithrl.decisions import DecisionSummary

FIXTURE = Path(__file__).parents[1] / "fixtures" / "codex-session" / "rollout.jsonl"


@pytest.fixture
def session() -> NativeSession:
    return PARSER.parse(read_jsonl(FIXTURE, limits=ParsingLimits()))


def test_fixture_exposes_one_completed_final_response_decision(
    session: NativeSession,
) -> None:
    (decision,) = enumerate_decisions(session)

    assert decision.case.adapter == "codex-session"
    assert decision.case.adapter_version == "1"
    assert decision.case.provider == "fixture-provider"
    assert decision.case.model_requested == "fixture-model"
    assert decision.case.model_returned is None
    assert decision.case.source_schema == "codex-rollout-jsonl"
    assert decision.selector.harness == "codex"
    assert decision.selector.session_id == "00000000-0000-4000-8000-000000000001"
    assert decision.selector.turn_id == "turn-fixture-001"
    assert decision.selector.kind == "codex-task-complete"
    assert decision.selector.native_id == "turn-fixture-001"
    assert decision.selector.source == "source/rollout.jsonl"
    assert decision.selector.record.line == 13
    assert decision.selector.record.identity_key == "ordinal"
    assert decision.selector.record.identity_value == 12
    assert decision.selector.record_digest == content_digest(session.records[12])
    assert decision.observed_at == "2026-01-01T00:00:12.000Z"
    assert decision.observation.final_response == "Fixture complete."
    assert decision.observation.shell_actions == ()
    assert decision.limitations == (
        "experimental-support-unvalidated",
        "opaque-context-frozen",
    )

    (candidate,) = decision.targets
    assert candidate.evidence_support == "supported"
    assert candidate.experimental_support == "unvalidated"
    assert candidate.target == FinalResponseTarget(text_equals="Fixture complete.")
    assert decision.selector.context_before_line == 10


def test_enumeration_is_deterministic_and_summary_is_content_free(
    session: NativeSession,
) -> None:
    first = enumerate_decisions(session)
    second = enumerate_decisions(session)

    assert first == second
    assert first[0].decision_id == second[0].decision_id
    summary = first[0].summarize().model_dump_json()
    assert "fixture-command" not in summary
    assert "fixture-value" not in summary


def test_decision_context_contains_only_earlier_source_records(
    session: NativeSession,
) -> None:
    decision = enumerate_decisions(session)[0]
    context = compile_context(session, decision.selector)

    assert [_source_selector(node).identity_value for node in context.nodes] == list(range(9))
    assert [_source_selector(node).line for node in context.nodes] == list(range(1, 10))
    assert all(node.source == "source/rollout.jsonl" for node in context.nodes)
    assert [node.digest for node in context.nodes] == [
        content_digest(record) for record in session.records[:9]
    ]
    assert all(_source_selector(node).identity_value != 9 for node in context.nodes)

    by_ordinal = {_source_selector(node).identity_value: node for node in context.nodes}
    assert (by_ordinal[0].protected, by_ordinal[0].mutable) == (True, False)
    assert (by_ordinal[1].protected, by_ordinal[1].mutable) == (True, False)
    assert (by_ordinal[2].protected, by_ordinal[2].mutable) == (True, False)
    assert (by_ordinal[3].protected, by_ordinal[3].mutable) == (False, True)


def test_final_response_context_preserves_call_output_dependency(
    session: NativeSession,
) -> None:
    final = enumerate_decisions(session)[0]
    context = compile_context(session, final.selector)

    assert final.selector.kind == "codex-task-complete"
    assert final.selector.record.line == 13
    assert final.observation.final_response == "Fixture complete."
    assert final.targets[0].target == FinalResponseTarget(text_equals="Fixture complete.")
    assert [_source_selector(node).identity_value for node in context.nodes] == list(range(9))

    by_ordinal = {_source_selector(node).identity_value: node for node in context.nodes}
    call = by_ordinal[5]
    output = by_ordinal[8]
    assert call.mutable
    assert output.mutable
    assert output.depends_on == (call.id,)
    assert not by_ordinal[6].mutable
    assert 9 not in by_ordinal


def test_incomplete_trailing_turn_is_omitted(session: NativeSession) -> None:
    records = list(copy.deepcopy(session.records))
    records.extend(
        [
            _record(
                13,
                "turn_context",
                {"turn_id": "turn-fixture-002", "model": "fixture-model-2"},
            ),
            _record(
                14,
                "event_msg",
                {"type": "task_started", "turn_id": "turn-fixture-002"},
            ),
            _record(
                15,
                "response_item",
                {
                    "type": "custom_tool_call",
                    "id": "item-fixture-call-002",
                    "call_id": "call-fixture-002",
                    "name": "exec_command",
                    "input": '{"cmd":"unfinished-command"}',
                },
            ),
        ]
    )

    decisions = enumerate_decisions(PARSER.parse(_document(tuple(records))))

    assert len(decisions) == 1
    assert decisions[0].selector.turn_id == "turn-fixture-001"


def test_overlapping_turns_complete_independently(session: NativeSession) -> None:
    records = list(copy.deepcopy(session.records[:3]))
    records.extend(
        [
            _record(3, "event_msg", {"type": "task_started", "turn_id": "turn-a"}),
            _record(4, "event_msg", {"type": "task_started", "turn_id": "turn-b"}),
            _record(
                5,
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "turn-b",
                    "last_agent_message": "B complete.",
                },
            ),
            _record(
                6,
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "turn-a",
                    "last_agent_message": "A complete.",
                },
            ),
        ]
    )

    decisions = enumerate_decisions(PARSER.parse(_document(tuple(records))))

    assert [decision.selector.turn_id for decision in decisions] == ["turn-b", "turn-a"]
    assert [decision.observation.final_response for decision in decisions] == [
        "B complete.",
        "A complete.",
    ]


def test_same_turn_id_uses_lifo_intervals(session: NativeSession) -> None:
    records = list(copy.deepcopy(session.records[:3]))
    records.extend(
        [
            _record(3, "event_msg", {"type": "task_started", "turn_id": "turn-a"}),
            _record(4, "event_msg", {"type": "task_started", "turn_id": "turn-a"}),
            _record(
                5,
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "turn-a",
                    "last_agent_message": "Inner complete.",
                },
            ),
            _record(
                6,
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": "turn-a",
                    "last_agent_message": "Outer complete.",
                },
            ),
        ]
    )

    decisions = enumerate_decisions(PARSER.parse(_document(tuple(records))))

    assert [decision.selector.context_before_line for decision in decisions] == [6, 5]
    assert [decision.observation.final_response for decision in decisions] == [
        "Inner complete.",
        "Outer complete.",
    ]


def test_nested_turns_are_processed_in_one_pass(
    session: NativeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nesting = 128
    records = list(copy.deepcopy(session.records[:2]))
    ordinal = len(records)
    for _ in range(nesting):
        records.append(_record(ordinal, "event_msg", {"type": "task_started", "turn_id": "x"}))
        ordinal += 1
    for item in range(nesting):
        records.append(
            _record(
                ordinal,
                "response_item",
                {
                    "type": "message",
                    "id": f"item-{item}",
                    "role": "user",
                    "content": "context",
                },
            )
        )
        ordinal += 1
    for _ in range(nesting):
        records.append(_record(ordinal, "event_msg", {"type": "task_complete", "turn_id": "x"}))
        ordinal += 1
    for record in records:
        record["timestamp"] = "2026-01-01T00:00:00.000Z"

    calls = 0
    original = codex_decisions._is_model_input

    def counted(payload: JsonObject) -> bool:
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(codex_decisions, "_is_model_input", counted)

    assert enumerate_decisions(PARSER.parse(_document(tuple(records)))) == ()
    assert calls == nesting


def test_decision_summary_rejects_an_oversized_timestamp(session: NativeSession) -> None:
    summary = enumerate_decisions(session)[0].summarize()

    with pytest.raises(ValidationError):
        DecisionSummary.model_validate(
            {
                **summary.model_dump(mode="python"),
                "observed_at": "2026-01-01T00:00:00." + "0" * 65_000 + "Z",
            }
        )


def test_later_turn_context_does_not_relabel_completed_decisions(
    session: NativeSession,
) -> None:
    records = list(copy.deepcopy(session.records))
    records.append(
        _record(
            13,
            "turn_context",
            {"turn_id": "turn-fixture-001", "model": "later-model"},
        )
    )

    decisions = enumerate_decisions(PARSER.parse(_document(tuple(records))))

    assert [decision.case.model_requested for decision in decisions] == [
        "fixture-model",
    ]


def test_turn_context_change_applies_only_to_following_decisions(
    session: NativeSession,
) -> None:
    records = list(copy.deepcopy(session.records))
    records.insert(
        8,
        _record(
            8,
            "turn_context",
            {"turn_id": "turn-fixture-001", "model": "updated-model"},
        ),
    )
    for ordinal, record in enumerate(records):
        record["ordinal"] = ordinal

    decisions = enumerate_decisions(PARSER.parse(_document(tuple(records))))

    assert [decision.case.model_requested for decision in decisions] == [
        "updated-model",
    ]


def test_intermediate_assistant_message_is_not_a_separate_decision(
    session: NativeSession,
) -> None:
    records = copy.deepcopy(session.records)
    records[11]["type"] = "response_item"
    records[11]["payload"] = {
        "type": "message",
        "id": "message-fixture-assistant-001",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Intermediate text."}],
    }

    decisions = enumerate_decisions(PARSER.parse(_document(records)))

    assert [decision.selector.kind for decision in decisions] == ["codex-task-complete"]
    assert all(
        decision.observation.final_response != "Intermediate text." for decision in decisions
    )


def test_oversized_task_complete_response_is_omitted(session: NativeSession) -> None:
    records = copy.deepcopy(session.records)
    _payload(records[12])["last_agent_message"] = "x" * (64 * 1024 + 1)

    decisions = enumerate_decisions(PARSER.parse(_document(records)))

    assert decisions == ()


def test_enumeration_does_not_change_source_or_parsed_records(session: NativeSession) -> None:
    source_before = FIXTURE.read_bytes()
    records_before = session.records
    native_before = session.native_bytes

    enumerate_decisions(session)

    assert FIXTURE.read_bytes() == source_before
    assert session.native_bytes == native_before
    assert session.records == records_before


def test_wrong_harness_is_rejected(session: NativeSession) -> None:
    metadata = session.metadata.model_copy(
        update={
            "format": session.metadata.format.model_copy(update={"harness": "copilot"}),
        }
    )
    other = NativeSession(
        metadata=metadata,
        source=session.source,
        native_bytes=session.native_bytes,
        records=session.records,
    )

    with pytest.raises(ValueError, match="not a Codex"):
        enumerate_decisions(other)


def _payload(record: JsonObject) -> JsonObject:
    payload = record["payload"]
    assert isinstance(payload, dict)
    return payload


def _source_selector(node: ContextNode) -> SourceRecordSelector:
    assert node.selector is not None
    return node.selector


def _record(ordinal: int, record_type: str, payload: JsonObject) -> JsonObject:
    return {
        "ordinal": ordinal,
        "timestamp": f"2026-01-01T00:00:{ordinal:02d}.000Z",
        "type": record_type,
        "payload": payload,
    }


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
