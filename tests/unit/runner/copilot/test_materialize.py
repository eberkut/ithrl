from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence
from ithrl.execution import (
    CapabilityProfile,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.experiment import ExperimentDefinition
from ithrl.extraction import extract_decision
from ithrl.runner import PreparedAttempt, prepare_candidate
from ithrl.runner.copilot.materialize import (
    CopilotMaterializationError,
    CopilotMaterializationErrorCode,
    materialize_copilot_attempt,
)

FIXTURES = Path(__file__).parents[3] / "fixtures"


def _record(
    ordinal: int,
    record_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": f"10000000-0000-4000-8000-{ordinal:012d}",
        "parentId": None,
        "timestamp": f"2026-01-01T00:00:{ordinal:02d}.000Z",
        "type": record_type,
        "data": data,
    }


def _session_start() -> dict[str, Any]:
    return _record(
        0,
        "session.start",
        {
            "sessionId": "20000000-0000-4000-8000-000000000001",
            "version": 1,
            "producer": "copilot-agent",
            "copilotVersion": "1.0.88",
            "startTime": "2026-01-01T00:00:00.000Z",
        },
    )


def _user(ordinal: int, turn: int, content: str) -> dict[str, Any]:
    return _record(
        ordinal,
        "user.message",
        {
            "content": content,
            "turnId": f"30000000-0000-4000-8000-{turn:012d}",
        },
    )


def _turn_record(ordinal: int, turn: int, *, start: bool) -> dict[str, Any]:
    return _record(
        ordinal,
        "assistant.turn_start" if start else "assistant.turn_end",
        {"turnId": f"30000000-0000-4000-8000-{turn:012d}"},
    )


def _assistant(
    ordinal: int,
    turn: int,
    *,
    content: str,
    tool_requests: list[dict[str, Any]] | None = None,
    final: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "messageId": f"70000000-0000-4000-8000-{ordinal:012d}",
        "model": "fixture-model",
        "content": content,
        "toolRequests": tool_requests or [],
        "turnId": f"30000000-0000-4000-8000-{turn:012d}",
    }
    if final:
        data.update({"phase": "final_answer", "parentToolCallId": None})
    return _record(ordinal, "assistant.message", data)


def _direct_records(*, shell_action: bool = False) -> list[dict[str, Any]]:
    request = [
        {
            "toolCallId": "80000000-0000-4000-8000-000000000001",
            "name": "bash",
            "arguments": {"command": "pwd"},
        }
    ]
    return [
        _session_start(),
        _user(1, 1, "Private direct trigger."),
        _turn_record(2, 1, start=True),
        _assistant(
            3,
            1,
            content="" if shell_action else "Fixture complete.",
            tool_requests=request if shell_action else None,
            final=not shell_action,
        ),
        _turn_record(4, 1, start=False),
    ]


def _history_records() -> list[dict[str, Any]]:
    tool_call_id = "80000000-0000-4000-8000-000000000001"
    return [
        _session_start(),
        _user(1, 1, "Private historical prompt."),
        _turn_record(2, 1, start=True),
        _assistant(
            3,
            1,
            content="Private assistant text.",
            tool_requests=[
                {
                    "toolCallId": tool_call_id,
                    "name": "fixture_tool",
                    "arguments": {"private": "argument"},
                }
            ],
        ),
        _record(
            4,
            "tool.execution_start",
            {
                "toolCallId": tool_call_id,
                "toolName": "fixture_tool",
                "arguments": {"private": "argument"},
                "turnId": "30000000-0000-4000-8000-000000000001",
            },
        ),
        _record(
            5,
            "tool.execution_complete",
            {
                "toolCallId": tool_call_id,
                "turnId": "30000000-0000-4000-8000-000000000001",
                "success": True,
                "result": {"content": "Private tool result."},
            },
        ),
        _turn_record(6, 1, start=False),
        _record(7, "model.turn_started", {"kind": "turn_started"}),
        _user(8, 2, "Private direct trigger."),
        _turn_record(9, 2, start=True),
        _assistant(10, 2, content="Fixture complete.", final=True),
        _turn_record(11, 2, start=False),
    ]


def _evidence(
    tmp_path: Path, records: list[dict[str, Any]], *, decision_index: int = -1
) -> CaseEvidence:
    source = tmp_path / "events.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    session = parse_session(source)
    decision = enumerate_decisions(session)[decision_index]
    return extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()


def _attempt(evidence: CaseEvidence, *remove_ids: str) -> PreparedAttempt:
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    capability = CapabilityProfile(
        runner_type="copilot",
        runner_version="test",
        harness="copilot",
        runtime_harness_version="test",
        protocol_version="test",
        source_schema="copilot-events-jsonl",
        deterministic=False,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="copilot-semantic-session-import-v1",
        supported_targets=("final-response", "shell-action"),
        provider_transport="required",
        provider_calls_per_attempt=1,
        tool_network_access="forbidden",
        action_visibility="visible",
        action_denial="enforced",
        action_execution="denied",
        source_store_effect="unchanged",
        original_workspace_effect="unchanged",
        protected_state_enforcement="preventive",
        disposable_state_effect="writes",
        disposable_state_limit_enforcement="enforced",
        process_containment="process-group",
        supports_timeout=True,
        supports_cancellation=True,
        cancellation_grace_seconds=3,
        reports_usage=True,
        provider="fixture-provider",
        endpoint="https://example.invalid/chat/completions",
    )
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider="fixture-provider",
        allowed_endpoint="https://example.invalid/chat/completions",
    )
    limits = ResourceLimits(max_provider_calls=1, max_disposable_state_bytes=1024 * 1024)
    decision = evidence.decision
    assert decision is not None
    plan = ResolvedExecutionPlan(
        case_digest=evidence.digest,
        capture_type=evidence.manifest.capture.request,
        candidate_materialization="source-backed",
        capability=capability,
        harness="copilot",
        source_harness_version=evidence.manifest.harness_version,
        source_schema=evidence.manifest.source_schema,
        model_requested=evidence.manifest.model_requested,
        decision_digest=decision.selector.digest,
        runner_fingerprint="copilot-test",
        safety_policy=policy,
        resource_limits=limits,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        target_type=experiment.target.type,
    )
    return PreparedAttempt(
        evidence=evidence,
        candidate=prepare_candidate(evidence, plan, remove_ids),
        target=experiment.target,
        execution_plan=plan,
        safety_policy=policy,
        resource_limits=limits,
        phase="calibration",
        arm="baseline",
    )


def _semantic_records(result: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in result.splitlines()]


def test_materializes_direct_final_response_with_separate_trigger(tmp_path: Path) -> None:
    result = materialize_copilot_attempt(_attempt(_evidence(tmp_path, _direct_records())))

    assert result.trigger == "Private direct trigger."
    assert _semantic_records(result.semantic_session) == [
        {
            "type": "session",
            "version": 1,
            "externalId": "20000000-0000-4000-8000-000000000001",
            "createdAt": "2026-01-01T00:00:00.000Z",
        }
    ]


def test_materializes_shell_action_decision(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, _direct_records(shell_action=True))

    result = materialize_copilot_attempt(_attempt(evidence))

    assert result.trigger == "Private direct trigger."
    assert len(_semantic_records(result.semantic_session)) == 1


def test_preserves_historical_messages_and_complete_tool_work_in_order(tmp_path: Path) -> None:
    native_records = _history_records()
    native_records[1]["data"]["transformedContent"] = "Model-visible historical prompt."
    native_records[8]["data"]["transformedContent"] = "Model-visible direct trigger."
    result = materialize_copilot_attempt(_attempt(_evidence(tmp_path, native_records)))

    records = _semantic_records(result.semantic_session)
    assert [record["type"] for record in records] == [
        "session",
        "message",
        "message",
        "tool-call",
        "tool-result",
    ]
    assert records[1]["role"] == "user"
    assert records[1]["content"][0]["text"] == "Model-visible historical prompt."
    assert records[2]["role"] == "assistant"
    assert records[3]["id"] == "80000000-0000-4000-8000-000000000001"
    assert records[3]["arguments"] == {"private": "argument"}
    assert records[4]["toolCallId"] == records[3]["id"]
    assert result.trigger == "Private direct trigger."


def test_omits_only_recognized_operational_records(tmp_path: Path) -> None:
    result = materialize_copilot_attempt(_attempt(_evidence(tmp_path, _history_records())))

    assert all(
        record["type"] != "model.turn_started"
        for record in _semantic_records(result.semantic_session)
    )


@pytest.mark.parametrize("record_type", ["skill.invoked", "fixture.unknown_event"])
def test_refuses_retained_unsupported_records(tmp_path: Path, record_type: str) -> None:
    records = _history_records()
    records.insert(8, _record(12, record_type, {"content": "Private unsupported content."}))
    evidence = _evidence(tmp_path, records)

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence))

    assert refused.value.code is CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT
    assert "Private" not in str(refused.value)


def test_refuses_incomplete_retained_tool_sequence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, _history_records())
    completion = next(
        node for node in evidence.context.nodes if node.kind == "tool.execution_complete"
    )

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence, completion.id))

    assert refused.value.code is CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT


def test_refuses_tool_start_that_changes_the_requested_action(tmp_path: Path) -> None:
    records = _history_records()
    records[4]["data"]["arguments"] = {"private": "changed"}
    evidence = _evidence(tmp_path, records)

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence))

    assert refused.value.code is CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT
    assert "changed" not in str(refused.value)


def test_refuses_historical_assistant_message_without_its_user(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, _history_records())
    historical_user = next(node for node in evidence.context.nodes if node.kind == "user.message")

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence, historical_user.id))

    assert refused.value.code is CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT


def test_refuses_invalid_transformed_historical_content_without_echoing_it(tmp_path: Path) -> None:
    records = _history_records()
    records[1]["data"]["transformedContent"] = {"private": "unsupported"}
    evidence = _evidence(tmp_path, records)

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence))

    assert refused.value.code is CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT
    assert "private" not in str(refused.value)


def test_refuses_candidate_without_direct_trigger(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, _direct_records())
    trigger = next(node for node in evidence.context.nodes if node.kind == "user.message")

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence, trigger.id))

    assert refused.value.code is CopilotMaterializationErrorCode.MISSING_TRIGGER
    assert "Private" not in str(refused.value)


def test_refuses_tool_continuation_boundary_from_saved_fixture(tmp_path: Path) -> None:
    session = parse_session(FIXTURES / "copilot-session" / "events.jsonl")
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence))

    assert refused.value.code is CopilotMaterializationErrorCode.MISSING_TRIGGER


def test_materialization_repr_hides_session_and_trigger_content(tmp_path: Path) -> None:
    result = materialize_copilot_attempt(_attempt(_evidence(tmp_path, _history_records())))

    rendered = repr(result)
    assert "Private historical prompt" not in rendered
    assert "Private tool result" not in rendered
    assert "Private direct trigger" not in rendered
