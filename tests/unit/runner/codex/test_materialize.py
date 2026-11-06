from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.adapters.contracts import NativeSession
from ithrl.case import CaseEvidence
from ithrl.context import JsonObject, content_digest
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
from ithrl.runner.codex.materialize import (
    CodexMaterializationError,
    CodexMaterializationErrorCode,
    _materialize_session,
    editable_codex_text,
    materialize_codex_attempt,
)

FIXTURES = Path(__file__).parents[3] / "fixtures"


def _evidence(tmp_path: Path) -> CaseEvidence:
    session = parse_session(FIXTURES / "codex-session" / "rollout.jsonl")
    decision = enumerate_decisions(session)[0]
    return extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()


def _evidence_with_field(
    tmp_path: Path,
    *,
    record_type: str,
    field_name: str,
    value: object,
) -> CaseEvidence:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = FIXTURES / "codex-session" / "rollout.jsonl"
    records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    record = next(item for item in records if item["type"] == record_type)
    record["payload"][field_name] = value
    return _evidence_from_records(tmp_path, records)


def _evidence_from_records(tmp_path: Path, records: list[dict[str, object]]) -> CaseEvidence:
    modified = tmp_path / "rollout.jsonl"
    modified.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in records),
        encoding="utf-8",
    )
    session = parse_session(modified)
    decision = enumerate_decisions(session)[0]
    return extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()


@pytest.mark.parametrize("remove_all", [False, True])
@pytest.mark.parametrize("coarse", [False, True])
def test_section_removal_preserves_exact_native_history_and_trigger(
    tmp_path: Path, remove_all: bool, coarse: bool
) -> None:
    records = [
        json.loads(line)
        for line in (FIXTURES / "codex-session/rollout.jsonl").read_bytes().splitlines()
    ]
    mixed = "Keep Ω and e\u0301.\r\n\r\nRemovable paragraph.\r\rFinal paragraph.\n"
    payload = records[3]["payload"]
    payload["content"] = [
        {"type": "input_text", "text": mixed, "opaque": {"keep": True}},
        {"type": "fixture.opaque", "payload": ["unchanged"]},
    ]
    payload["opaqueMetadata"] = {"keep": [1, True]}
    earlier = {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:02.500Z",
        "payload": {
            "type": "message",
            "id": "earlier-user",
            "role": "user",
            "content": [{"type": "input_text", "text": "Earlier irrelevant context."}],
        },
    }
    records.insert(3, earlier)
    evidence = _evidence_from_records(tmp_path, records)
    before = evidence.digest, evidence.source_artifacts, (tmp_path / "rollout.jsonl").read_bytes()
    original = _attempt(evidence)
    baseline = materialize_codex_attempt(original)
    catalog = next(
        c for c in evidence.text_sections() if c.original_text_digest == content_digest(mixed)
    )
    sections = catalog.sections if remove_all else catalog.sections[1:2]
    mutation = TextMutation(edits=(catalog.removal(tuple(s.id for s in sections)),))
    early_node = next(n for n in evidence.context.nodes if n.selector and n.selector.line == 4)
    candidate = prepare_candidate(
        evidence, original.execution_plan, (early_node.id,) if coarse else (), mutation=mutation
    )

    result = materialize_codex_attempt(replace(original, candidate=candidate))

    expected_history = list(baseline.history)
    edited = json.loads(json.dumps(payload))
    edited["content"][0]["text"] = (
        "" if remove_all else "Keep Ω and e\u0301.\r\n\r\nFinal paragraph.\n"
    )
    expected_history[1] = edited
    if coarse:
        del expected_history[0]
    assert result == replace(baseline, history=tuple(expected_history))
    assert (
        evidence.digest,
        evidence.source_artifacts,
        (tmp_path / "rollout.jsonl").read_bytes(),
    ) == before
    assert materialize_codex_attempt(original) == baseline


def test_section_empty_selection_is_identical_and_tampered_identity_is_terminal(
    tmp_path: Path,
) -> None:
    original = _attempt(_evidence(tmp_path))
    catalog = original.evidence.text_sections()[0]
    empty = _with_mutation(original, TextMutation(edits=(catalog.removal(()),)))
    assert empty.candidate == original.candidate
    assert materialize_codex_attempt(empty) == materialize_codex_attempt(original)
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[0].id,)),))
    changed = _with_mutation(original, mutation)
    for field, value in (("original_text_digest", "f" * 64), ("policy_digest", "0" * 64)):
        stale = mutation.edits[0].model_copy(update={field: value})
        with pytest.raises(MutationError):
            prepare_candidate(
                original.evidence, original.execution_plan, mutation=TextMutation(edits=(stale,))
            )
    object.__setattr__(
        changed,
        "candidate",
        changed.candidate.model_copy(update={"source_artifact_digests": ("0" * 64,)}),
    )
    with pytest.raises(CodexMaterializationError) as caught:
        materialize_codex_attempt(changed)
    assert caught.value.code is CodexMaterializationErrorCode.CHANGED_EVIDENCE
    assert "Inspect" not in str(caught.value)


def test_sections_refuse_removed_parent_and_preserve_fixed_tool_input(tmp_path: Path) -> None:
    original = _attempt(_evidence(tmp_path))
    catalog = original.evidence.text_sections()[0]
    mutation = TextMutation(edits=(catalog.removal((catalog.sections[0].id,)),))
    with pytest.raises(MutationError, match="whole-record dependency closure"):
        prepare_candidate(
            original.evidence,
            original.execution_plan,
            (catalog.context_id,),
            mutation=mutation,
        )
    trigger = next(
        node
        for node in original.evidence.context.nodes
        if node.kind == "response_item.custom_tool_call_output"
    )
    removed_trigger = prepare_candidate(
        original.evidence, original.execution_plan, (trigger.id,), mutation=mutation
    )
    with pytest.raises(CodexMaterializationError) as caught:
        materialize_codex_attempt(replace(original, candidate=removed_trigger))
    assert caught.value.code is CodexMaterializationErrorCode.MISSING_TRIGGER


def _attempt(evidence: CaseEvidence, *remove_ids: str) -> PreparedAttempt:
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    capability = CapabilityProfile(
        runner_type="codex",
        runner_version="test",
        harness="codex",
        runtime_harness_version="test",
        protocol_version="test",
        source_schema="codex-rollout-jsonl",
        deterministic=False,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=(
            "dependency-closed-removal",
            "append-text",
            "remove-text-sections",
        ),
        reconstruction_method="codex-app-server-inject-items-v1",
        supported_targets=("final-response",),
        provider_transport="required",
        provider_calls_per_attempt=1,
        tool_network_access="forbidden",
        action_visibility="not-applicable",
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
        endpoint="https://example.invalid/responses",
    )
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider="fixture-provider",
        allowed_endpoint="https://example.invalid/responses",
    )
    limits = ResourceLimits(
        max_provider_calls=1,
        max_disposable_state_bytes=1024 * 1024,
    )
    decision = evidence.decision
    assert decision is not None
    plan = ResolvedExecutionPlan(
        case_digest=evidence.digest,
        capture_type=evidence.manifest.capture.request,
        candidate_materialization="source-backed",
        capability=capability,
        harness="codex",
        source_harness_version=evidence.manifest.harness_version,
        source_schema=evidence.manifest.source_schema,
        model_requested=evidence.manifest.model_requested,
        decision_digest=decision.selector.digest,
        runner_fingerprint="codex-test",
        safety_policy=policy,
        resource_limits=limits,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        target_type="final-response",
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


def test_materializes_verified_history_and_separates_tool_output_trigger(
    tmp_path: Path,
) -> None:
    result = materialize_codex_attempt(_attempt(_evidence(tmp_path)))

    assert result.model == "fixture-model"
    assert result.provider == "fixture-provider"
    assert result.effort == "low"
    assert result.personality == "none"
    assert result.base_instructions == "Fixture instructions."
    assert result.developer_instructions is None
    assert [item["type"] for item in result.history] == [
        "message",
        "reasoning",
        "custom_tool_call",
    ]
    assert result.turn_input.items == ()
    assert result.turn_input.tool_output == {
        "name": "exec",
        "namespace": None,
        "output": [{"type": "input_text", "text": "Fixture result."}],
    }


def test_materializes_dependency_valid_removal_without_reordering(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    user_message = next(
        node for node in evidence.context.nodes if node.kind == "response_item.message"
    )

    result = materialize_codex_attempt(_attempt(evidence, user_message.id))

    assert [item["type"] for item in result.history] == ["reasoning", "custom_tool_call"]


def _with_mutation(attempt: PreparedAttempt, mutation: TextMutation) -> PreparedAttempt:
    return replace(
        attempt,
        candidate=prepare_candidate(attempt.evidence, attempt.execution_plan, mutation=mutation),
    )


def test_text_append_changes_only_copied_prior_user_block(tmp_path: Path) -> None:
    attempt = _attempt(_evidence(tmp_path))
    before = materialize_codex_attempt(attempt)
    source = tuple(artifact.content for artifact in attempt.evidence.source_artifacts)
    sites = editable_codex_text(attempt)
    assert sites == attempt.evidence.editable_text()
    assert len(sites) == 1
    assert sites[0].original_text == "Inspect the fixture context."
    site = sites[0]
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id=site.context_id,
                content_index=site.content_index,
                original_text_digest=content_digest(site.original_text),
                text="\n\nSynthetic archived note.",
            ),
        )
    )
    result = materialize_codex_attempt(_with_mutation(attempt, mutation))
    assert result.history != before.history
    assert result.history[1:] == before.history[1:]
    assert result.turn_input == before.turn_input
    assert (
        result.base_instructions,
        result.developer_instructions,
        result.model,
        result.provider,
        result.effort,
        result.personality,
    ) == (
        before.base_instructions,
        before.developer_instructions,
        before.model,
        before.provider,
        before.effort,
        before.personality,
    )
    assert tuple(a.content for a in attempt.evidence.source_artifacts) == source
    assert materialize_codex_attempt(attempt) == before


def test_codex_mutation_refuses_trigger_assistant_and_wrong_text(tmp_path: Path) -> None:
    attempt = _attempt(_evidence(tmp_path))
    trigger_id = next(
        node.id
        for node in attempt.evidence.context.nodes
        if node.kind == "response_item.custom_tool_call_output"
    )
    trigger = TextMutation(
        edits=(
            TextAppend(
                context_id=trigger_id,
                content_index=0,
                original_text_digest=content_digest("Fixture result."),
                text="append",
            ),
        )
    )
    with pytest.raises(CodexMaterializationError, match="current decision input"):
        materialize_codex_attempt(_with_mutation(attempt, trigger))
    site = editable_codex_text(attempt)[0]
    bad = TextMutation(
        edits=(
            TextAppend(
                context_id=site.context_id,
                content_index=0,
                original_text_digest="f" * 64,
                text="append",
            ),
        )
    )
    with pytest.raises(CodexMaterializationError, match="original text digest"):
        materialize_codex_attempt(_with_mutation(attempt, bad))
    assistant = _attempt(
        _evidence_with_field(
            tmp_path / "assistant",
            record_type="response_item",
            field_name="role",
            value="assistant",
        )
    )
    assert editable_codex_text(assistant) == ()
    node = next(n for n in assistant.evidence.context.nodes if n.kind == "response_item.message")
    edit = TextMutation(
        edits=(
            TextAppend(
                context_id=node.id,
                content_index=0,
                original_text_digest=content_digest(site.original_text),
                text="append",
            ),
        )
    )
    with pytest.raises(CodexMaterializationError, match="ordinary user message"):
        materialize_codex_attempt(_with_mutation(assistant, edit))


def test_refuses_candidate_that_removes_the_selected_trigger(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    trigger = next(
        node
        for node in evidence.context.nodes
        if node.kind == "response_item.custom_tool_call_output"
    )

    with pytest.raises(CodexMaterializationError) as refused:
        materialize_codex_attempt(_attempt(evidence, trigger.id))

    assert refused.value.code is CodexMaterializationErrorCode.MISSING_TRIGGER
    assert "Fixture" not in str(refused.value)


def test_materialization_objects_hide_captured_content_from_repr(tmp_path: Path) -> None:
    results = (
        materialize_codex_attempt(
            _attempt(
                _evidence_with_field(
                    tmp_path / "effort",
                    record_type="turn_context",
                    field_name="effort",
                    value="private-effort",
                )
            )
        ),
        materialize_codex_attempt(
            _attempt(
                _evidence_with_field(
                    tmp_path / "personality",
                    record_type="turn_context",
                    field_name="personality",
                    value="private-personality",
                )
            )
        ),
    )

    for result in results:
        rendered = repr(result)
        assert "Fixture instructions" not in rendered
        assert "fixture-command" not in rendered
        assert "Fixture result" not in rendered
        assert "private-effort" not in rendered
        assert "private-personality" not in rendered


@pytest.mark.parametrize(
    ("record_type", "field_name", "value"),
    [
        ("session_meta", "base_instructions", {"unexpected": "shape"}),
        ("turn_context", "developer_instructions", ["unsupported"]),
        ("turn_context", "effort", {"unexpected": "shape"}),
        ("turn_context", "personality", 42),
    ],
)
def test_refuses_present_unsupported_instruction_or_model_setting(
    tmp_path: Path,
    record_type: str,
    field_name: str,
    value: object,
) -> None:
    evidence = _evidence_with_field(
        tmp_path,
        record_type=record_type,
        field_name=field_name,
        value=value,
    )

    with pytest.raises(CodexMaterializationError) as refused:
        materialize_codex_attempt(_attempt(evidence))

    assert refused.value.code is CodexMaterializationErrorCode.UNSUPPORTED_CONTEXT
    assert "unexpected" not in str(refused.value)


def test_materializes_direct_user_trigger_without_history(tmp_path: Path) -> None:
    source = FIXTURES / "codex-session" / "rollout.jsonl"
    records = [json.loads(line) for line in source.read_text().splitlines()]
    records = [
        record
        for record in records
        if record["type"] != "response_item" or record["payload"].get("type") == "message"
    ]
    selected = tmp_path / "rollout.jsonl"
    selected.write_text("".join(json.dumps(record) + "\n" for record in records))
    session = parse_session(selected)
    decision = enumerate_decisions(session)[0]
    evidence = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    ).load()
    materialized = materialize_codex_attempt(_attempt(evidence))
    assert materialized.history == ()
    assert materialized.turn_input.items == (
        {"type": "text", "text": "Inspect the fixture context."},
    )
    assert editable_codex_text(_attempt(evidence)) == ()


@pytest.mark.parametrize("boundary", [1, 10_000])
def test_invalid_boundary_precedes_missing_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int
) -> None:
    attempt = _attempt(_evidence(tmp_path))
    decision = attempt.evidence.decision
    assert decision is not None
    changed_decision = decision.model_copy(
        update={"selector": decision.selector.model_copy(update={"context_before_line": boundary})}
    )

    def selected_decision(_evidence: CaseEvidence) -> DecisionEvidence:
        return changed_decision

    monkeypatch.setattr(CaseEvidence, "decision", property(selected_decision))
    session = parse_session(FIXTURES / "codex-session" / "rollout.jsonl")
    with pytest.raises(CodexMaterializationError) as captured:
        _materialize_session(attempt, session)
    assert captured.value.code is CodexMaterializationErrorCode.AMBIGUOUS_BOUNDARY
    assert str(captured.value) == "the selected Codex decision has an invalid context boundary"


def test_missing_native_input_retains_materialization_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(_evidence(tmp_path))
    session = parse_session(FIXTURES / "codex-session" / "rollout.jsonl")
    records: tuple[JsonObject, ...] = tuple(
        {**record, "payload": {"type": "reasoning"}}
        if record.get("type") == "response_item"
        else record
        for record in session.records
    )

    def selected_records(_session: NativeSession) -> tuple[JsonObject, ...]:
        return records

    monkeypatch.setattr(NativeSession, "records", property(selected_records))
    with pytest.raises(CodexMaterializationError) as captured:
        _materialize_session(attempt, session)
    assert captured.value.code is CodexMaterializationErrorCode.MISSING_TRIGGER
    assert str(captured.value) == "the selected Codex decision has no supported triggering input"
    assert captured.value.__cause__ is None
