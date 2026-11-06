"""Interpretation and opt-in display of already validated reduction evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaptureFacts, CaseEvidence, CaseManifest, ProvenanceFacts, ReplayFacts
from ithrl.context import ContextGraph, ContextNode, JsonValue, content_digest
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy, resolve_execution_plan
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
    StructuralOracleConfig,
)
from ithrl.inspection import _confirmation, _section_fields, build_retained_inspection
from ithrl.mutation import TextMutation
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    SamplingAttempt,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    sampling_schedule,
)
from ithrl.reduce import (
    ALGORITHM_DIGEST,
    CandidateTrial,
    ReductionCheckpoint,
    SampledCandidateTrial,
    SampledComparison,
    SampledConfirmation,
    SampledReductionCheckpoint,
)
from ithrl.reduce.contracts import trial_key
from ithrl.results import ResultSnapshot, ResultSummary, SampledReductionEvidence
from ithrl.results.inspection import RetainedResult
from ithrl.runner import (
    AttemptEvidence,
    Candidate,
    ProviderAcceptanceState,
    RunAccounting,
    prepare_candidate,
)
from ithrl.text_sections import MAX_TEXT_SECTION_BYTES, TextSectionRemoval

_TIME = datetime(2026, 10, 4, tzinfo=UTC)
_PRIVATE = "PRIVATE_CONTEXT_NOT_FOR_DEFAULT_OUTPUT"


def _case() -> tuple[CaseEvidence, ExperimentDefinition]:
    experiment = ExperimentDefinition(
        target=FinalResponseTarget(text_equals="PRIVATE_PREDICATE"),
        oracle=StructuralOracleConfig(),
        runner=FakeRunnerConfig(required_markers=("keep",), final_text="PRIVATE_PREDICATE"),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )
    ids = ("ctx-a", "ctx-b", "ctx-c", "ctx-d", "ctx-frozen", "ctx-protected", "ctx-untested")
    values: list[JsonValue] = [
        {"role": "user", "content": _PRIVATE + "[red] café\u202e\x1b[31m\n\nExtra background."},
        *({"text": i} for i in ids[1:]),
    ]
    evidence = CaseEvidence(
        manifest=CaseManifest(
            adapter="synthetic",
            adapter_version="1",
            captured_at="2026-01-01T00:00:00+00:00",
            provider="fake",
            endpoint="local",
            model_requested="fake",
            capture=CaptureFacts(request="provider-request", completeness="complete"),
            replay=ReplayFacts(state="self-contained", environment="local"),
            provenance=ProvenanceFacts(source="synthetic.json", trace_schema="synthetic-v1"),
        ),
        request={"input": values},
        context=ContextGraph(
            nodes=tuple(
                ContextNode(
                    id=node_id,
                    kind="message",
                    path=f"/input/{index}",
                    mutable=node_id != "ctx-frozen",
                    protected=node_id == "ctx-protected",
                    depends_on=("ctx-a",) if node_id == "ctx-d" else (),
                    source="request.json",
                    digest=content_digest(values[index]),
                )
                for index, node_id in enumerate(ids)
            )
        ),
        default_experiment=experiment.to_toml(),
    )
    return evidence, experiment


def _deterministic(*, historical_only: bool = False) -> RetainedResult:
    evidence, experiment = _case()
    preflight = resolve_execution_plan(
        CapabilityProfile.deterministic_fake(),
        experiment,
        evidence=evidence,
        runner_fingerprint="fixture",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=ResourceLimits.credential_free(),
    )
    assert preflight.plan is not None
    plan = preflight.plan
    removed = ("ctx-b",)

    def candidate(removals: tuple[str, ...]) -> str:
        return Candidate(
            case_digest=evidence.digest,
            execution_plan_digest=plan.digest,
            retained_context_ids=tuple(
                n.id for n in evidence.context.nodes if n.id not in removals
            ),
            removed_context_ids=tuple(n.id for n in evidence.context.nodes if n.id in removals),
            materialization="exact-request",
        ).digest

    specs: list[
        tuple[
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
            Literal["loses", "preserves", "invalid"],
        ]
    ] = [
        ((), ("ctx-c", "ctx-d"), ("ctx-c", "ctx-d"), "loses"),
        ((), ("ctx-c",), ("ctx-c",), "loses"),
        ((), removed, removed, "preserves"),
        (removed, ("ctx-a",), ("ctx-a", "ctx-b", "ctx-d"), "loses"),
        (removed, ("ctx-frozen",), removed, "invalid"),
    ]
    if not historical_only:
        specs.append((removed, ("ctx-c",), ("ctx-b", "ctx-c"), "loses"))
    trials = tuple(
        CandidateTrial(
            trial_key=trial_key("chunk", base, requested),
            candidate_digest=candidate(effective) if classification != "invalid" else None,
            base_removals=base,
            requested_removals=requested,
            effective_removals=effective,
            target_observed=classification == "preserves" if classification != "invalid" else None,
            classification=classification,
            phase="chunk",
            runner_executed=classification != "invalid",
            observation_id=str(index) if classification != "invalid" else None,
            observation_attempt=1 if classification != "invalid" else None,
        )
        for index, (base, requested, effective, classification) in enumerate(specs)
    )
    runs = sum(trial.runner_executed for trial in trials)
    checkpoint = ReductionCheckpoint(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint="fixture",
        execution_plan_digest=plan.digest,
        target_digest=experiment.target.digest,
        removed_context_ids=removed,
        trials=trials,
        candidate_runs_used=runs,
    )
    retained = tuple(n.id for n in evidence.context.nodes if n.id not in removed)
    summary = ResultSummary(
        run_id="fixture",
        status="stopped",
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        execution_plan_digest=plan.digest,
        candidate_digest=candidate(removed),
        candidate_materialization="exact-request",
        reduced_request_digest=content_digest(evidence.materialize(removed)),
        retained_context_digest=content_digest([]),
        provenance_digest=content_digest({}),
        checkpoint_digest=content_digest(checkpoint.model_dump(mode="json")),
        runner_fingerprint="fixture",
        algorithm_digest=ALGORITHM_DIGEST,
        original_context_count=len(evidence.context.nodes),
        retained_context_count=len(retained),
        original_context_ids=tuple(n.id for n in evidence.context.nodes),
        retained_context_ids=retained,
        removed_context_ids=removed,
        calibration_result="observed",
        calibration_observation_ids=("calibration",),
        search_result="search-incomplete",
        confirmation_result="not-run",
        candidate_runs_used=runs,
        observations_recorded=runs + 1,
        source_case="/private/not-followed",
        next_action="continue",
    )
    snapshot = ResultSnapshot(
        root=Path("/private/not-followed"),
        summary=summary,
        reduced_request=evidence.materialize(removed),
        retained_context_lines=(),
        experiment_source=experiment.to_toml(),
        experiment=experiment,
        provenance=evidence.manifest.provenance,
        execution_plan=plan,
        checkpoint=checkpoint,
    )
    # Loader integrity is exercised separately; these in-memory fixtures isolate interpretation.
    return RetainedResult(
        evidence=evidence,
        experiment=experiment,
        retained_ids=retained,
        execution_plan=plan,
        deterministic=snapshot,
        validation_limits=("Observation database was not opened.",),
    )


def test_joint_dependency_and_historical_losses_do_not_explain_individual_retention() -> None:
    report = build_retained_inspection(_deterministic())
    nodes = {node.id: node for node in report.nodes}
    assert nodes["ctx-a"].removal_evidence == "joint-or-historical-only"
    assert nodes["ctx-a"].required_by == ("ctx-d",)
    assert not nodes["ctx-a"].final_single_removal_trial_indexes
    assert nodes["ctx-c"].removal_evidence == "single-removal-tested"
    assert nodes["ctx-c"].final_single_removal_trial_indexes == (6,)
    assert report.trials[0].outcome == "deterministic-target-not-observed"
    assert not report.trials[0].final_parent
    assert not report.trials[3].single_removal
    assert report.trials[4].outcome == "invalid"
    assert nodes["ctx-frozen"].removal_evidence == "frozen"
    assert nodes["ctx-protected"].removal_evidence == "protected"
    assert nodes["ctx-untested"].removal_evidence == "untested"
    assert not report.final_sweep_complete


def test_historical_single_removal_is_not_final_context_evidence() -> None:
    report = build_retained_inspection(_deterministic(historical_only=True))
    node = next(n for n in report.nodes if n.id == "ctx-c")
    assert node.removal_evidence == "joint-or-historical-only"
    assert not node.final_single_removal_trial_indexes
    assert report.trials[1].single_removal
    assert not report.trials[1].final_parent
    assert report.trials[1].parent_digest == report.original_candidate_digest
    assert report.trials[3].parent_digest == report.candidate_digest


def test_default_report_omits_content_and_predicate() -> None:
    report = build_retained_inspection(_deterministic())
    output = report.model_dump_json(exclude_none=True)
    assert _PRIVATE not in output
    assert "PRIVATE_PREDICATE" not in output
    assert "not-followed" not in output
    assert not report.text_requested
    assert report.text_bytes == 0
    assert all(node.text is None for node in report.nodes)
    assert (
        report.retained_context_count + report.removed_context_count
        == report.original_context_count
    )
    assert report.retained_canonical_json_bytes < report.original_canonical_json_bytes
    assert report.byte_measure == "canonical-json-utf8-not-provider-tokens"
    assert report.provider_calls == report.actions_executed == 0


def test_inspected_section_ids_agree_with_source_validated_persisted_candidate() -> None:
    loaded = _deterministic()
    evidence = loaded.evidence
    before = evidence.request, evidence.digest
    report = build_retained_inspection(loaded)
    node = next(node for node in report.nodes if node.id == "ctx-a")
    (section_field,) = node.text_section_fields
    assert section_field.status == "available-untested"
    assert section_field.unavailable_reason is None
    (catalog,) = evidence.text_sections()
    assert section_field.catalog == catalog
    assert len(catalog.sections) == 2
    assert node.removal_evidence == "joint-or-historical-only"
    assert node.trial_indexes
    assert any("do not establish section necessity" in line for line in report.limitations)

    assert loaded.execution_plan is not None
    candidate = prepare_candidate(
        evidence,
        loaded.execution_plan,
        remove_ids=("ctx-b",),
        mutation=TextMutation(edits=(catalog.removal((catalog.sections[1].id,)),)),
    )
    persisted = Candidate.model_validate_json(candidate.model_dump_json())
    (edit,) = persisted.mutation.edits
    assert isinstance(edit, TextSectionRemoval)
    assert evidence.validate_text_sections(edit) == section_field.catalog
    assert persisted.digest == candidate.digest
    assert (evidence.request, evidence.digest) == before
    assert _PRIVATE not in report.model_dump_json()
    assert _PRIVATE not in repr(report)


def test_inspection_catalogs_only_eligible_retained_prior_user_text() -> None:
    evidence, _ = _case()
    values: list[JsonValue] = [
        {"role": "user", "content": "Eligible.\n\nBackground."},
        {"role": "user", "content": "Removed."},
        {"role": "assistant", "content": "Opaque assistant text."},
        {"role": "user", "content": [{"type": "input_image", "image_url": "opaque"}]},
        {"role": "user", "content": "Frozen."},
        {"role": "user", "content": "Protected."},
        {"role": "user", "content": "Fixed triggering input."},
    ]
    changed = CaseEvidence(
        manifest=evidence.manifest,
        request={"input": values},
        context=ContextGraph(
            nodes=tuple(
                node.model_copy(update={"digest": content_digest(value)})
                for node, value in zip(evidence.context.nodes, values, strict=True)
            )
        ),
        default_experiment=evidence.default_experiment,
    )
    retained = frozenset(node.id for node in changed.context.nodes if node.id != "ctx-b")
    before = changed.request, changed.digest
    fields = _section_fields(changed, retained)
    assert tuple(fields) == ("ctx-a",)
    assert fields["ctx-a"][0].status == "available-untested"
    assert (changed.request, changed.digest) == before


def test_section_inspection_indexes_context_once_for_multiple_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence, _ = _case()
    request = evidence.request
    inputs = request["input"]
    assert isinstance(inputs, list)
    inputs[0] = {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "First field.\n\nBackground."},
            {"type": "input_text", "text": "Second field.\n\nOther background."},
        ],
    }
    evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=request,
        context=ContextGraph(
            nodes=tuple(
                node.model_copy(update={"digest": content_digest(inputs[0])})
                if node.id == "ctx-a"
                else node
                for node in evidence.context.nodes
            )
        ),
        default_experiment=evidence.default_experiment,
    )
    retained = frozenset(node.id for node in evidence.context.nodes)
    expected = _section_fields(evidence, retained)
    assert len(expected["ctx-a"]) == 2
    index_builds = 0

    def counted_index(graph: ContextGraph) -> dict[str, ContextNode]:
        nonlocal index_builds
        index_builds += 1
        return {node.id: node for node in graph.nodes}

    monkeypatch.setattr(ContextGraph, "by_id", property(counted_index))
    assert _section_fields(evidence, retained) == expected
    assert index_builds == 1


@pytest.mark.parametrize("limited_by", ["policy", "inspection"])
def test_unavailable_section_fields_have_no_partial_catalog(
    limited_by: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence, _ = _case()
    if limited_by == "policy":
        request = evidence.request
        inputs = request["input"]
        assert isinstance(inputs, list)
        inputs[0] = {"role": "user", "content": "x" * (MAX_TEXT_SECTION_BYTES + 1)}
        nodes = tuple(
            node.model_copy(update={"digest": content_digest(inputs[0])})
            if node.id == "ctx-a"
            else node
            for node in evidence.context.nodes
        )
        evidence = CaseEvidence(
            manifest=evidence.manifest,
            request=request,
            context=ContextGraph(nodes=nodes),
            default_experiment=evidence.default_experiment,
        )
    else:
        monkeypatch.setattr("ithrl.inspection.MAX_INSPECTION_SECTION_UNITS", 1)
    fields = _section_fields(evidence, frozenset(node.id for node in evidence.context.nodes))
    (field,) = fields["ctx-a"]
    assert field.status == "unavailable"
    assert field.catalog is None
    assert field.unavailable_reason == (
        "section-policy-limit" if limited_by == "policy" else "inspection-section-limit"
    )


@pytest.mark.parametrize("allowance", [0, 1, 6, 80, 512])
def test_opt_in_text_has_one_utf8_bound_after_control_and_markup_escaping(allowance: int) -> None:
    report = build_retained_inspection(_deterministic(), show_text=True, max_text_bytes=allowance)
    texts = [node.text or "" for node in report.nodes]
    combined = "".join(texts)
    assert len(combined.encode("utf-8")) == report.text_bytes <= allowance
    assert "\x1b" not in combined
    assert "\u202e" not in combined
    assert "[" not in combined
    assert "]" not in combined
    assert combined.isprintable() or not combined
    assert report.text_truncated == any(node.text_truncated for node in report.nodes)
    assert all(_PRIVATE not in repr(node) for node in report.nodes)
    if allowance == 512:
        assert "\\u005bred\\u005d" in combined
        assert "\\u202e" in combined
        assert "café" in combined


def test_targeted_text_skips_earlier_records_without_spending_the_allowance() -> None:
    loaded = _deterministic()
    plain = build_retained_inspection(loaded)
    report = build_retained_inspection(
        loaded, show_text=True, max_text_bytes=24, text_node_id="ctx-untested"
    )
    assert report.text_node_id == "ctx-untested"
    assert len(report.nodes) == len(plain.nodes)
    assert report.trials == plain.trials
    assert report.retained_canonical_json_bytes == plain.retained_canonical_json_bytes
    chosen = report.nodes[-1]
    assert chosen.id == "ctx-untested"
    assert chosen.text == '{"text":"ctx-untested"}'
    assert report.text_bytes == len(chosen.text.encode("utf-8"))
    assert not report.text_truncated
    assert all(node.text is None for node in report.nodes[:-1])
    assert all(not node.text_truncated for node in report.nodes[:-1])


@pytest.mark.parametrize("allowance", [0, 1, 6, 80])
def test_targeted_text_retains_global_escaped_utf8_bound(allowance: int) -> None:
    report = build_retained_inspection(
        _deterministic(), show_text=True, max_text_bytes=allowance, text_node_id="ctx-a"
    )
    combined = "".join(node.text or "" for node in report.nodes)
    assert len(combined.encode("utf-8")) == report.text_bytes <= allowance
    assert "\x1b" not in combined
    assert "\u202e" not in combined
    assert "[" not in combined
    assert "]" not in combined
    assert all(node.text is None for node in report.nodes if node.id != "ctx-a")


@pytest.mark.parametrize("node_id", ["ctx-b", "missing", "PRIVATE_INVALID_SELECTOR\x1b"])
def test_targeted_text_refuses_removed_or_unknown_ids_without_echoing_them(node_id: str) -> None:
    with pytest.raises(ValueError, match=r"^text selector must identify a retained context unit$"):
        build_retained_inspection(_deterministic(), show_text=True, text_node_id=node_id)


def test_targeted_text_requires_explicit_content_opt_in() -> None:
    with pytest.raises(ValueError, match=r"^targeted text requires explicit text display$"):
        build_retained_inspection(_deterministic(), text_node_id="ctx-a")


@pytest.mark.parametrize("allowance", [-1, 65_537, True])
def test_report_refuses_invalid_text_allowances(allowance: int) -> None:
    with pytest.raises(ValueError, match="allowance"):
        build_retained_inspection(_deterministic(), max_text_bytes=allowance)


def _sampled(*, incomplete: bool = False) -> tuple[RetainedResult, SamplingBlockEvidence]:
    evidence, _ = _case()
    reduction = SampledReductionPlan(
        search=SampledSearchPhase(
            original=SamplingArmTarget(required_valid=1, max_attempts=1),
            candidate=SamplingArmTarget(required_valid=1, max_attempts=1),
        ),
        confirmation=SampledConfirmationPhase(
            original=SamplingArmTarget(required_valid=20, max_attempts=20),
            candidate=SamplingArmTarget(required_valid=20, max_attempts=20),
            one_sided_confidence=0.95,
        ),
        margin=0.2,
        freshness_seconds=600,
    )
    experiment = SampledExperimentDefinition(
        target=FinalResponseTarget(text_equals="PRIVATE_PREDICATE"),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(type="codex", required_capability_digest="d" * 64),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
        reduction=reduction,
    )
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint="fixture",
        execution_plan_digest="e" * 64,
        target_digest=experiment.target.digest,
        algorithm_digest=ALGORITHM_DIGEST,
    )

    def candidate(removals: tuple[str, ...]) -> str:
        return Candidate(
            case_digest=evidence.digest,
            execution_plan_digest=identity.execution_plan_digest,
            retained_context_ids=tuple(
                n.id for n in evidence.context.nodes if n.id not in removals
            ),
            removed_context_ids=removals,
            materialization="exact-request",
        ).digest

    original = candidate(())
    plan = SamplingBlockPlan(
        block_id="screen",
        role="search",
        procedure=reduction,
        original_identity=identity,
        candidate_identity=identity,
        original_candidate_digest=original,
        candidate_digest=candidate(("ctx-c",)),
        frozen_at=_TIME,
        schedule_seed=0,
        schedule=sampling_schedule(1, 1, 0),
        provider_calls_per_attempt=1,
    )
    attempts = []
    for position, arm in enumerate(() if incomplete else plan.schedule):
        observation = Observation(
            observation_id=f"obs-{position}",
            recorded_at=_TIME + timedelta(seconds=position + 1),
            **identity.model_dump(),
            candidate_digest=original if arm == "baseline" else plan.candidate_digest,
            role=ObservationRole.SEARCH,
            arm=ObservationArm(arm),
            attempt=1,
            outcome=ObservationOutcome.MATCH if arm == "baseline" else ObservationOutcome.NO_MATCH,
            attempt_evidence=AttemptEvidence(
                attempt_id=f"attempt-{position:032x}",
                attempt_digest=content_digest(position),
                termination_reason="completed",
                provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                effective_policy_digest="f" * 64,
                accounting=RunAccounting(),
            ),
        )
        attempts.append(SamplingAttempt(schedule_position=position, observation=observation))
    block = SamplingBlockEvidence(plan=plan, attempts=tuple(attempts))
    trial = SampledCandidateTrial(
        trial_key=trial_key("chunk", (), ("ctx-c",)),
        base_removals=(),
        requested_removals=("ctx-c",),
        effective_removals=("ctx-c",),
        phase="chunk",
        comparison=SampledComparison.from_evidence(block),
    )
    checkpoint = SampledReductionCheckpoint(
        reduction=reduction,
        original_identity=identity,
        original_candidate_digest=original,
        best_candidate_digest=original,
        removed_context_ids=(),
        trials=(trial,),
    )
    result = SampledReductionEvidence(
        checkpoint=checkpoint,
        search_result="search-incomplete",
        selected_candidate_digest=original,
        selected_at=_TIME + timedelta(seconds=10),
    )
    return (
        RetainedResult(
            evidence=evidence,
            experiment=experiment,
            retained_ids=tuple(n.id for n in evidence.context.nodes),
            sampled=result,
        ),
        block,
    )


def test_sampled_screen_rejection_is_not_a_confirmed_behavioral_loss() -> None:
    loaded, _ = _sampled()
    report = build_retained_inspection(loaded)
    assert report.trials[0].outcome == "exploratory-screen-rejected"
    assert report.trials[0].baseline_digest == report.original_candidate_digest
    assert report.trials[0].original is not None
    assert report.trials[0].original.matches == 1
    assert report.trials[0].candidate is not None
    assert report.trials[0].candidate.no_matches == 1
    assert report.confirmation.status == "not-run"
    assert report.failed_candidate_observation_count is None
    assert report.reconstruction_method is None
    assert any("do not establish confirmed loss" in line for line in report.limitations)


def test_incomplete_comparison_does_not_count_as_a_completed_single_removal() -> None:
    loaded, _ = _sampled(incomplete=True)
    report = build_retained_inspection(loaded)
    node = next(n for n in report.nodes if n.id == "ctx-c")
    assert report.trials[0].outcome == "incomplete"
    assert node.removal_evidence == "incomplete-or-ineligible"
    assert node.final_single_removal_trial_indexes == ()


@pytest.mark.parametrize("incomplete", [False, True])
def test_unfinished_sampled_result_keeps_policy_without_inventing_confirmation(
    incomplete: bool,
) -> None:
    loaded, _ = _sampled(incomplete=incomplete)
    report = build_retained_inspection(loaded)
    confirmation = report.confirmation
    assert confirmation.status == "not-run"
    assert confirmation.margin == 0.2
    assert confirmation.one_sided_confidence == 0.95
    assert confirmation.original is None
    assert confirmation.candidate is None
    assert confirmation.difference is None
    assert confirmation.lower_bound is None
    assert confirmation.upper_bound is None


def test_incomplete_confirmation_has_no_interval() -> None:
    _, block = _sampled(incomplete=True)
    confirmation_plan = block.plan.model_copy(
        update={"role": "confirmation", "schedule": sampling_schedule(20, 20, 0)}
    )
    confirmation = SampledConfirmation.from_evidence(
        SamplingBlockEvidence(plan=confirmation_plan, attempts=())
    )
    report = _confirmation("incomplete", confirmation, confirmation.evidence.plan.preservation)
    assert report.margin == 0.2
    assert report.one_sided_confidence == 0.95
    assert report.lower_bound is report.upper_bound is report.difference is None
    assert report.original is not None
    assert report.original.valid == 0
    assert report.candidate is not None
    assert report.candidate.valid == 0


@pytest.mark.parametrize(
    ("original_matches", "candidate_matches", "status", "difference", "interval"),
    [
        (20, 20, "confirmed-preserving", 0.0, (-0.11915783736096439, 0.11915783736096439)),
        (10, 20, "confirmed-preserving", 0.5, (0.29026671808036014, 0.672596232114844)),
        (20, 10, "failed", -0.5, (-0.672596232114844, -0.29026671808036014)),
    ],
)
def test_complete_confirmation_reports_fresh_arm_counts_and_recorded_policy(
    original_matches: int,
    candidate_matches: int,
    status: str,
    difference: float,
    interval: tuple[float, float],
) -> None:
    _, search = _sampled()
    plan = search.plan.model_copy(
        update={
            "role": "confirmation",
            "block_id": "confirmation",
            "schedule": sampling_schedule(20, 20, 0),
        }
    )
    counts = {"baseline": 0, "candidate": 0}
    matches = {"baseline": original_matches, "candidate": candidate_matches}
    attempts = []
    template = search.attempts[0].observation
    for index, arm in enumerate(plan.schedule):
        counts[arm] += 1
        observation = Observation.model_validate(
            {
                **template.model_dump(),
                "observation_id": f"confirmation-{index}",
                "recorded_at": _TIME + timedelta(seconds=index + 1),
                "candidate_digest": plan.original_candidate_digest
                if arm == "baseline"
                else plan.candidate_digest,
                "role": ObservationRole.CONFIRMATION,
                "arm": ObservationArm(arm),
                "attempt": counts[arm],
                "outcome": ObservationOutcome.MATCH
                if counts[arm] <= matches[arm]
                else ObservationOutcome.NO_MATCH,
                "attempt_evidence": AttemptEvidence(
                    attempt_id=f"attempt-{index:032x}",
                    attempt_digest=content_digest(index),
                    termination_reason="completed",
                    provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                    effective_policy_digest="f" * 64,
                    accounting=RunAccounting(),
                ),
            }
        )
        attempts.append(SamplingAttempt(schedule_position=index, observation=observation))
    confirmation = SampledConfirmation.from_evidence(
        SamplingBlockEvidence(plan=plan, attempts=tuple(attempts))
    )
    report = _confirmation(status, confirmation, confirmation.evidence.plan.preservation)
    assert report.status == status
    assert report.original is not None
    assert report.original.matches == original_matches
    assert report.original.valid == 20
    assert report.candidate is not None
    assert report.candidate.matches == candidate_matches
    assert report.candidate.valid == 20
    assert report.difference == pytest.approx(difference)
    assert report.lower_bound is not None
    assert report.lower_bound == pytest.approx(interval[0])
    assert report.upper_bound is not None
    assert report.upper_bound == pytest.approx(interval[1])
    assert report.lower_bound < report.upper_bound


def test_partial_active_block_has_no_invented_node_attribution() -> None:
    loaded, partial = _sampled(incomplete=True)
    assert loaded.sampled is not None
    result = loaded.sampled
    checkpoint = SampledReductionCheckpoint.model_validate(
        {**result.checkpoint.model_dump(), "trials": (), "active_block": partial}
    )
    result = SampledReductionEvidence.model_validate(
        {**result.model_dump(), "checkpoint": checkpoint}
    )
    report = build_retained_inspection(
        RetainedResult(
            evidence=loaded.evidence,
            experiment=loaded.experiment,
            retained_ids=loaded.retained_ids,
            sampled=result,
        )
    )
    assert not report.trials
    assert report.active_comparison is not None
    assert report.active_comparison.outcome == "incomplete"
    node = next(node for node in report.nodes if node.id == "ctx-c")
    assert node.removal_evidence == "untested"
    assert not node.trial_indexes


def test_text_payload_is_absent_from_json_by_default() -> None:
    result = build_retained_inspection(_deterministic()).model_dump(mode="json", exclude_none=True)
    assert all("text" not in node for node in result["nodes"])
    assert _PRIVATE not in json.dumps(result)
