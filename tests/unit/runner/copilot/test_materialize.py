from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.adapters.registry import editable_session_text
from ithrl.case import CaseEvidence, CaseFormatError
from ithrl.context import ContextGraph, content_digest
from ithrl.decisions import DecisionEvidence
from ithrl.execution import (
    CapabilityProfile,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.experiment import ExperimentDefinition
from ithrl.extraction import extract_decision
from ithrl.mutation import MutationError, TextAppend, TextMutation
from ithrl.runner import PreparedAttempt, prepare_candidate
from ithrl.runner.copilot.materialize import (
    CopilotMaterializationError,
    CopilotMaterializationErrorCode,
    _captured_system_message,
    _materialize_session,
    _validate_complete_tool_work,
    materialize_copilot_attempt,
)


def test_retained_tool_requests_require_matching_native_lifecycle_evidence() -> None:
    with pytest.raises(CopilotMaterializationError, match="incomplete tool work"):
        _validate_complete_tool_work(tuple(_direct_records(shell_action=True)))


def test_captured_system_message_requires_text_content() -> None:
    records = (_record(1, "system.message", {"role": "system", "content": 42}),)
    with pytest.raises(CopilotMaterializationError, match="unsupported content"):
        _captured_system_message(records)
    assert (
        _captured_system_message(
            (_record(1, "system.message", {"role": "developer", "content": "Fixture guidance"}),)
        )
        is None
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
    tmp_path: Path,
    records: list[dict[str, Any]],
    *,
    decision_index: int = -1,
    line_ending: str = "\n",
) -> CaseEvidence:
    source = tmp_path / "events.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + line_ending for record in records),
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


def _attempt(
    evidence: CaseEvidence, *remove_ids: str, mutation: TextMutation | None = None
) -> PreparedAttempt:
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
        supported_context_operations=(
            "dependency-closed-removal",
            "append-text",
            "remove-text-sections",
        ),
        reconstruction_method="copilot-native-prefix-sdk-v1",
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
        candidate=prepare_candidate(evidence, plan, remove_ids, mutation=mutation),
        target=experiment.target,
        execution_plan=plan,
        safety_policy=policy,
        resource_limits=limits,
        phase="calibration",
        arm="baseline",
    )


def _native_records(result: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in result.splitlines()]


@pytest.mark.parametrize("shell_action", [False, True])
def test_direct_decisions_preserve_the_existing_native_turn_boundary(
    tmp_path: Path, shell_action: bool
) -> None:
    evidence = _evidence(tmp_path, _direct_records(shell_action=shell_action))
    result = materialize_copilot_attempt(_attempt(evidence))

    assert result.native_session == b"".join(
        evidence.source_artifacts[0].content.splitlines(keepends=True)[:3]
    )
    assert result.session_id == "20000000-0000-4000-8000-000000000001"
    assert result.system_message is None
    assert _native_records(result.native_session)[-1]["type"] == "assistant.turn_start"


@pytest.mark.parametrize("older_input", ["user", "tool"])
def test_turn_without_new_native_input_cannot_borrow_an_earlier_turn(
    tmp_path: Path, older_input: str
) -> None:
    records = (
        _direct_records()
        if older_input == "user"
        else [
            record
            for record in _history_records()
            if record["type"] != "user.message"
            or record["data"]["content"] != "Private direct trigger."
        ]
    )
    turn = 2 if older_input == "user" else 3
    records.extend(
        [
            _turn_record(12, turn, start=True),
            _assistant(13, turn, content="Fixture complete.", final=True),
            _turn_record(14, turn, start=False),
        ]
    )
    evidence = _evidence(tmp_path, records)
    with pytest.raises(CaseFormatError, match="editable text cannot be derived safely"):
        evidence.editable_text()
    with pytest.raises(CopilotMaterializationError) as caught:
        materialize_copilot_attempt(_attempt(evidence))
    assert caught.value.code is CopilotMaterializationErrorCode.AMBIGUOUS_BOUNDARY


def test_preserves_opaque_records_and_failed_historical_tools_without_normalization(
    tmp_path: Path,
) -> None:
    records = _history_records()
    records[5]["data"].update(success=False, error={"kind": "fixture-failure"})
    records.insert(8, _record(12, "fixture.unknown_event", {"opaque": [1, {"keep": True}]}))
    records.insert(
        8,
        _record(
            13,
            "system.message",
            {"role": "system", "content": "Captured fixture guidance.\r\nKeep exact."},
        ),
    )
    evidence = _evidence(tmp_path, records)

    result = materialize_copilot_attempt(_attempt(evidence))

    assert evidence.decision is not None
    boundary = evidence.decision.selector.context_before_line
    assert result.native_session == b"".join(
        evidence.source_artifacts[0].content.splitlines(keepends=True)[:boundary]
    )
    assert result.system_message == "Captured fixture guidance.\r\nKeep exact."
    assert b"Fixture complete." not in result.native_session


@pytest.mark.parametrize("transformed", [False, True])
def test_append_changes_only_effective_prior_user_native_record_and_preserves_source(
    tmp_path: Path, transformed: bool
) -> None:
    records = _history_records()
    if transformed:
        records[1]["data"]["transformedContent"] = "Effective historical text."
    records[1]["data"]["opaqueMetadata"] = {"keep": True}
    evidence = _evidence(tmp_path, records)
    source = tmp_path / "events.jsonl"
    before = evidence.digest, source.read_bytes(), evidence.source_artifacts
    site = evidence.editable_text()[0]
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id=site.context_id,
                content_index=0,
                original_text_digest=content_digest(site.original_text),
                text="\r\n\nExact appended text. Ω",
            ),
        )
    )
    baseline = materialize_copilot_attempt(_attempt(evidence))

    result = materialize_copilot_attempt(_attempt(evidence, mutation=mutation))

    expected = _native_records(baseline.native_session)
    field = "transformedContent" if transformed else "content"
    assert isinstance(mutation.edits[0], TextAppend)
    expected[1]["data"][field] = site.original_text + mutation.edits[0].text
    assert _native_records(result.native_session) == expected
    old_lines, new_lines = (
        baseline.native_session.splitlines(keepends=True),
        result.native_session.splitlines(keepends=True),
    )
    # The appended value is JSON escaped; exactly one native record changes.
    assert [
        i for i, (old, new) in enumerate(zip(old_lines, new_lines, strict=True)) if old != new
    ] == [1]
    assert (evidence.digest, source.read_bytes(), evidence.source_artifacts) == before


@pytest.mark.parametrize("transformed", [False, True])
@pytest.mark.parametrize("remove_all", [False, True])
@pytest.mark.parametrize("coarse", [False, True])
def test_section_removal_changes_only_effective_retained_field(
    tmp_path: Path, transformed: bool, remove_all: bool, coarse: bool
) -> None:
    records = _history_records()
    mixed = "Keep Ω and e\u0301.\r\n\r\nRemovable paragraph.\r\rFinal paragraph.\n"
    data = records[1]["data"]
    field = "transformedContent" if transformed else "content"
    data[field] = mixed
    data["opaqueMetadata"] = {"keep": [1, True]}
    records.insert(1, _user(20, 1, "Earlier irrelevant context."))
    records.insert(8, _record(21, "fixture.opaque", {"keep": [1, True]}))
    evidence = _evidence(tmp_path, records)
    before = evidence.digest, evidence.source_artifacts, (tmp_path / "events.jsonl").read_bytes()
    original = _attempt(evidence)
    baseline = materialize_copilot_attempt(original)
    catalog = next(
        c for c in evidence.text_sections() if c.original_text_digest == content_digest(mixed)
    )
    sections = catalog.sections if remove_all else catalog.sections[1:2]
    mutation = TextMutation(edits=(catalog.removal(tuple(s.id for s in sections)),))
    early_node = next(
        n
        for n in evidence.context.nodes
        if n.selector and n.selector.identity_value == records[1]["id"]
    )
    changed = _attempt(evidence, *((early_node.id,) if coarse else ()), mutation=mutation)

    result = materialize_copilot_attempt(changed)

    expected = _native_records(baseline.native_session)
    expected[2]["data"][field] = (
        "" if remove_all else "Keep Ω and e\u0301.\r\n\r\nFinal paragraph.\n"
    )
    if coarse:
        del expected[1]
    assert _native_records(result.native_session) == expected
    original_lines = baseline.native_session.splitlines(keepends=True)
    actual_lines = result.native_session.splitlines(keepends=True)
    selected_lines = original_lines[:1] + original_lines[2:] if coarse else original_lines
    assert [
        i for i, (a, b) in enumerate(zip(selected_lines, actual_lines, strict=True)) if a != b
    ] == [1 if coarse else 2]
    assert (
        evidence.digest,
        evidence.source_artifacts,
        (tmp_path / "events.jsonl").read_bytes(),
    ) == before
    assert materialize_copilot_attempt(original) == baseline


def test_section_empty_selection_and_forged_source_are_checked(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, _history_records())
    original = _attempt(evidence)
    catalog = evidence.text_sections()[0]
    empty = _attempt(evidence, mutation=TextMutation(edits=(catalog.removal(()),)))
    assert empty.candidate == original.candidate
    assert materialize_copilot_attempt(empty) == materialize_copilot_attempt(original)
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[0].id,)),))
    for field, value in (("original_text_digest", "f" * 64), ("policy_digest", "0" * 64)):
        stale = mutation.edits[0].model_copy(update={field: value})
        with pytest.raises(MutationError):
            prepare_candidate(
                evidence, original.execution_plan, mutation=TextMutation(edits=(stale,))
            )
    changed = _attempt(evidence, mutation=mutation)
    object.__setattr__(
        changed,
        "candidate",
        changed.candidate.model_copy(update={"source_artifact_digests": ("0" * 64,)}),
    )
    with pytest.raises(CopilotMaterializationError) as caught:
        materialize_copilot_attempt(changed)
    assert caught.value.code is CopilotMaterializationErrorCode.CHANGED_EVIDENCE
    assert "Private" not in str(caught.value)


def test_section_removal_refuses_shell_target_even_with_forged_capability(tmp_path: Path) -> None:
    records = _history_records()
    records[-2]["data"].update(
        content="",
        toolRequests=[
            {
                "toolCallId": "80000000-0000-4000-8000-000000000099",
                "name": "bash",
                "arguments": {"command": "pwd"},
            }
        ],
    )
    records[-2]["data"].pop("phase")
    evidence = _evidence(tmp_path, records)
    original = _attempt(evidence)
    assert original.target.type == "shell-action"
    catalog = evidence.text_sections()[0]
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[0].id,)),))
    with pytest.raises(CopilotMaterializationError, match="final-response target"):
        materialize_copilot_attempt(_attempt(evidence, mutation=mutation))


def test_sections_refuse_removed_parent_and_preserve_fixed_user_input(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, _history_records())
    original = _attempt(evidence)
    catalog = evidence.text_sections()[0]
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[0].id,)),))
    with pytest.raises(MutationError, match="whole-record dependency closure"):
        prepare_candidate(
            evidence, original.execution_plan, (catalog.context_id,), mutation=mutation
        )
    trigger = evidence.context.nodes[-1]
    with pytest.raises(CopilotMaterializationError) as caught:
        materialize_copilot_attempt(_attempt(evidence, trigger.id, mutation=mutation))
    assert caught.value.code is CopilotMaterializationErrorCode.MISSING_TRIGGER


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_section_removal_preserves_native_record_line_endings(
    tmp_path: Path, line_ending: str
) -> None:
    records = _history_records()
    records[1]["data"]["content"] = "Keep.\n\nRemove."
    evidence = _evidence(tmp_path, records, line_ending=line_ending)
    catalog = evidence.text_sections()[0]
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[1].id,)),))

    result = materialize_copilot_attempt(_attempt(evidence, mutation=mutation))

    lines = result.native_session.splitlines(keepends=True)
    assert evidence.decision is not None
    assert len(lines) == evidence.decision.selector.context_before_line
    assert all(line.endswith(line_ending.encode()) for line in lines)
    assert _native_records(result.native_session)[1]["data"]["content"] == "Keep.\n\n"


def test_post_tool_decision_uses_the_same_native_prefix_and_exposes_prior_user_text(
    tmp_path: Path,
) -> None:
    session = parse_session(FIXTURES / "copilot-session" / "events.jsonl")
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    sites = evidence.editable_text()
    assert [site.original_text for site in sites] == ["Inspect the fixture context."]
    edit = TextAppend(
        context_id=sites[0].context_id,
        content_index=0,
        original_text_digest=content_digest(sites[0].original_text),
        text="\nAdditional fixture context.",
    )

    result = materialize_copilot_attempt(_attempt(evidence, mutation=TextMutation(edits=(edit,))))

    records = _native_records(result.native_session)
    assert records[-1]["type"] == "assistant.turn_start"
    assert records[1]["data"]["transformedContent"] == sites[0].original_text + edit.text
    assert records[1]["data"]["content"] == sites[0].original_text
    assert any(record["type"] == "tool.execution_complete" for record in records)
    assert b'"phase":"final_answer"' not in result.native_session


@pytest.mark.parametrize(
    ("site_kind", "index", "digest", "expected"),
    [
        ("user.message", 1, None, CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT),
        ("user.message", 0, "0" * 64, CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT),
        ("assistant.message", 0, None, CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT),
        ("trigger", 0, None, CopilotMaterializationErrorCode.MISSING_TRIGGER),
    ],
)
def test_append_refuses_stale_or_ineligible_native_sites(
    tmp_path: Path,
    site_kind: str,
    index: int,
    digest: str | None,
    expected: CopilotMaterializationErrorCode,
) -> None:
    evidence = _evidence(tmp_path, _history_records())
    node = (
        evidence.context.nodes[-1]
        if site_kind == "trigger"
        else next(node for node in evidence.context.nodes if node.kind == site_kind)
    )
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id=node.id,
                content_index=index,
                original_text_digest=digest or content_digest("Private historical prompt."),
                text="Private append.",
            ),
        )
    )

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence, mutation=mutation))

    assert refused.value.code is expected
    assert "Private" not in str(refused.value)
    assert refused.value.__suppress_context__ or refused.value.__context__ is None


def test_revalidates_candidate_identity_and_excludes_combined_mutation_removal(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path, _history_records())
    site = evidence.editable_text()[0]
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id=site.context_id,
                content_index=0,
                original_text_digest=content_digest(site.original_text),
                text="Private append.",
            ),
        )
    )
    attempt = _attempt(evidence, mutation=mutation)
    object.__setattr__(
        attempt,
        "candidate",
        attempt.candidate.model_copy(update={"removed_context_ids": (site.context_id,)}),
    )
    with pytest.raises(CopilotMaterializationError, match="unchanged original"):
        materialize_copilot_attempt(attempt)
    attempt = _attempt(evidence)
    object.__setattr__(
        attempt,
        "candidate",
        attempt.candidate.model_copy(update={"source_artifact_digests": ("0" * 64,)}),
    )
    with pytest.raises(CopilotMaterializationError, match="does not match"):
        materialize_copilot_attempt(attempt)


@pytest.mark.parametrize("kind", ["user.message", "assistant.message"])
def test_removal_omits_only_selected_records_and_keeps_native_ids_and_parent_metadata(
    tmp_path: Path, kind: str
) -> None:
    records = _history_records()
    records[2]["parentId"] = records[1]["id"]
    evidence = _evidence(tmp_path, records)
    node = next(node for node in evidence.context.nodes if node.kind == kind)
    attempt = _attempt(evidence, node.id)

    result = materialize_copilot_attempt(attempt)

    removed = set(attempt.candidate.removed_context_ids)
    expected = b"".join(
        line
        for node, line in zip(
            evidence.context.nodes,
            evidence.source_artifacts[0].content.splitlines(keepends=True),
            strict=False,
        )
        if node.id not in removed
    )
    assert evidence.decision is not None
    expected += evidence.source_artifacts[0].content.splitlines(keepends=True)[
        evidence.decision.selector.context_before_line - 1
    ]
    assert result.native_session == expected
    assert _native_records(result.native_session)[-1]["id"] == records[9]["id"]


@pytest.mark.parametrize(
    "remove_kind", ["tool.execution_start", "tool.execution_complete", "trigger"]
)
def test_refuses_partial_tool_work_or_removed_current_input(
    tmp_path: Path, remove_kind: str
) -> None:
    evidence = _evidence(tmp_path, _history_records())
    node = (
        evidence.context.nodes[-1]
        if remove_kind == "trigger"
        else next(node for node in evidence.context.nodes if node.kind == remove_kind)
    )

    with pytest.raises(CopilotMaterializationError) as refused:
        materialize_copilot_attempt(_attempt(evidence, node.id))

    expected = (
        CopilotMaterializationErrorCode.MISSING_TRIGGER
        if remove_kind == "trigger"
        else CopilotMaterializationErrorCode.UNSUPPORTED_CONTEXT
    )
    assert refused.value.code is expected


def test_source_content_references_prevent_dangling_frozen_skill_deliveries(tmp_path: Path) -> None:
    import hashlib

    records = _history_records()
    body = "Fixture skill body."
    records.insert(8, _record(12, "skill.invoked", {"content": body, "name": "fixture-skill"}))
    records.insert(
        9,
        _record(
            13,
            "skill.context_delivered_ref",
            {
                "contentId": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
                "prefix": "Fixture prefix.",
                "suffix": "",
            },
        ),
    )
    evidence = _evidence(tmp_path, records)
    body_node = next(node for node in evidence.context.nodes if node.kind == "skill.invoked")
    delivery = next(
        node for node in evidence.context.nodes if node.kind == "skill.context_delivered_ref"
    )
    assert body_node.id in delivery.depends_on

    with pytest.raises(ValueError, match="protected or frozen"):
        _attempt(evidence, body_node.id)


def test_unknown_source_record_references_are_not_silently_orphaned(tmp_path: Path) -> None:
    records = _history_records()
    records.insert(
        8, _record(12, "fixture.unknown_event", {"sourceRecord": records[1]["id"], "opaque": True})
    )
    evidence = _evidence(tmp_path, records)
    user = next(node for node in evidence.context.nodes if node.kind == "user.message")

    with pytest.raises(ValueError, match="protected or frozen"):
        _attempt(evidence, user.id)


def test_materialization_repr_hides_native_session_and_captured_system_content(
    tmp_path: Path,
) -> None:
    records = _history_records()
    records.insert(
        8, _record(12, "system.message", {"role": "system", "content": "Private system guidance."})
    )
    result = materialize_copilot_attempt(_attempt(_evidence(tmp_path, records)))

    rendered = repr(result)
    assert "Private historical prompt" not in rendered
    assert "Private tool result" not in rendered
    assert "Private system guidance" not in rendered


@pytest.mark.parametrize("mismatch", ["boundary", "turn"])
def test_materialization_preserves_boundary_error_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    evidence = _evidence(tmp_path, _direct_records())
    attempt = _attempt(evidence)
    decision = evidence.decision
    assert decision is not None
    update = {"context_before_line": 1} if mismatch == "boundary" else {"turn_id": "other-turn"}
    changed = decision.model_copy(update={"selector": decision.selector.model_copy(update=update)})

    def selected_decision(_evidence: CaseEvidence) -> DecisionEvidence:
        return changed

    monkeypatch.setattr(CaseEvidence, "decision", property(selected_decision))
    session = parse_session(tmp_path / "events.jsonl")
    with pytest.raises(CopilotMaterializationError) as captured:
        _materialize_session(attempt, session)
    assert captured.value.code is CopilotMaterializationErrorCode.AMBIGUOUS_BOUNDARY
    assert str(captured.value) == (
        "the Copilot context graph does not match the selected boundary"
        if mismatch == "boundary"
        else "the selected Copilot decision has no supported native input boundary"
    )
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("graph_change", ["reordered", "missing-earlier", "missing-input", "empty"])
def test_partial_or_reordered_graph_preserves_text_discovery_and_native_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, graph_change: str
) -> None:
    evidence = _evidence(tmp_path, _history_records())
    attempt = _attempt(evidence)
    decision = evidence.decision
    assert decision is not None
    session = parse_session(tmp_path / "events.jsonl")
    nodes = evidence.context.nodes
    input_line = decision.selector.context_before_line - 1
    prior_user_ids = {
        node.id
        for node in nodes
        if node.kind == "user.message"
        and node.selector is not None
        and node.selector.line != input_line
    }
    if graph_change == "reordered":
        changed_nodes = tuple(reversed(nodes))
    elif graph_change == "missing-earlier":
        changed_nodes = nodes[1:]
    elif graph_change == "missing-input":
        changed_nodes = tuple(
            node for node in nodes if node.selector is None or node.selector.line != input_line
        )
    else:
        changed_nodes = ()
    # model_copy deliberately bypasses graph ordering checks to exercise the native boundary.
    changed_context = evidence.context.model_copy(update={"nodes": changed_nodes})
    sites = editable_session_text(session, decision.selector, changed_context)
    assert {site.context_id for site in sites} == (
        set() if graph_change == "empty" else prior_user_ids
    )

    def selected_context(_evidence: CaseEvidence) -> ContextGraph:
        return changed_context

    monkeypatch.setattr(CaseEvidence, "context", property(selected_context))
    with pytest.raises(CopilotMaterializationError) as captured:
        _materialize_session(attempt, session)
    assert captured.value.code is CopilotMaterializationErrorCode.AMBIGUOUS_BOUNDARY
    assert str(captured.value) == (
        "the Copilot context graph does not match its source records"
        if graph_change == "reordered"
        else "the Copilot context graph does not match the selected boundary"
    )
    assert captured.value.__cause__ is None
