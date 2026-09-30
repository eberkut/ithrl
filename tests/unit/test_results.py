from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from ithrl.case import CaptureFacts, CaseEvidence, CaseManifest, ProvenanceFacts, ReplayFacts
from ithrl.context import ContextGraph, ContextNode, JsonObject, content_digest
from ithrl.execution import (
    CapabilityProfile,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
    resolve_execution_plan,
)
from ithrl.experiment import (
    ExperimentDefinition,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
)
from ithrl.observations import Observation, ObservationArm, ObservationOutcome, ObservationRole
from ithrl.reduce import ALGORITHM_DIGEST, CandidateTrial, ReductionCheckpoint
from ithrl.results import (
    CalibrationSummary,
    ResultSnapshotError,
    ResultSummary,
    calibration_summary_from_observations,
    load_calibration_snapshot,
    load_result_snapshot,
    write_calibration_snapshot,
    write_result_snapshot,
)
from ithrl.runner import (
    AttemptEvidence,
    Candidate,
    ProviderAcceptanceState,
    RunAccounting,
    TokenUsage,
)

_EXPERIMENT = """\
schema_version = 1
kind = "deterministic"

[target]
type = "shell-action"
tool_name_equals = "shell"
command_equals = "pwd"

[oracle]
type = "structural"
protected_context_ids = []

[runner]
type = "fake"
required_markers = ["keep"]
alternative_marker_sets = []
tool_name = "shell"
command = "pwd"

[calibration]
role = "calibration"
runs = 1

[search]
role = "search"
runs = 1

[confirmation]
role = "confirmation"
runs = 1
"""


def _experiment() -> ExperimentDefinition:
    return ExperimentDefinition.from_toml(_EXPERIMENT)


def _evidence() -> CaseEvidence:
    return CaseEvidence(
        manifest=CaseManifest(
            adapter="synthetic",
            adapter_version="1",
            captured_at="2026-01-01T00:00:00+00:00",
            provider="fake",
            endpoint="local",
            model_requested="fake",
            capture=CaptureFacts(request="provider-request", completeness="complete"),
            replay=ReplayFacts(state="self-contained", environment="local"),
            provenance=ProvenanceFacts(
                source="synthetic.json",
                inference_call_id="inference-001",
                trace_schema="synthetic-v1",
            ),
        ),
        request=_request(),
        context=ContextGraph(nodes=()),
        default_experiment=_EXPERIMENT,
    )


def _plan() -> ResolvedExecutionPlan:
    report = resolve_execution_plan(
        CapabilityProfile.deterministic_fake(),
        _experiment(),
        evidence=_evidence(),
        runner_fingerprint="fake:v1",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=ResourceLimits.credential_free(),
    )
    assert report.plan is not None
    return report.plan


def _node(node_id: str, path: str) -> ContextNode:
    return ContextNode(
        id=node_id,
        kind="message",
        path=path,
        mutable=True,
        source="request.json",
        digest=content_digest({"marker": node_id}),
    )


def _request() -> JsonObject:
    return {"model": "fake", "input": [{"marker": "keep"}]}


def _checkpoint(**overrides: object) -> ReductionCheckpoint:
    specifications = (
        ("chunk", (), ("ctx-a", "ctx-b"), ("ctx-a", "ctx-b"), False),
        ("chunk", (), ("ctx-b",), ("ctx-b",), True),
        ("final-sweep", ("ctx-b",), ("ctx-a",), ("ctx-a", "ctx-b"), False),
        ("final-sweep", ("ctx-b",), ("ctx-c",), ("ctx-b", "ctx-c"), False),
    )
    trials = tuple(
        CandidateTrial(
            trial_key=content_digest(
                {
                    "phase": phase,
                    "base_removals": sorted(base),
                    "requested_removals": list(requested),
                }
            ),
            candidate_digest=content_digest({"candidate": number}),
            base_removals=base,
            requested_removals=requested,
            effective_removals=effective,
            target_observed=preserves,
            classification="preserves" if preserves else "loses",
            phase=phase,
            runner_executed=True,
            observation_id=f"obs-search-{number}",
            observation_attempt=1,
        )
        for number, (phase, base, requested, effective, preserves) in enumerate(
            specifications, start=1
        )
    )
    values: dict[str, object] = {
        "case_digest": _plan().case_digest,
        "experiment_digest": _experiment().digest,
        "runner_fingerprint": _plan().runner_fingerprint,
        "execution_plan_digest": _plan().digest,
        "target_digest": _plan().target_digest,
        "algorithm_digest": ALGORITHM_DIGEST,
        "removed_context_ids": ("ctx-b",),
        "trials": trials,
        "candidate_runs_used": 4,
        "final_sweep_complete": True,
    }
    values.update(overrides)
    return ReductionCheckpoint.model_validate(values)


def _summary(
    **overrides: object,
) -> ResultSummary:
    plan = _plan()
    request = _request()
    checkpoint = _checkpoint()
    retained_context = [
        _node("ctx-a", "/input/0").model_dump(mode="json"),
        _node("ctx-c", "/input/2").model_dump(mode="json"),
    ]
    provenance = {
        "schema_version": 1,
        "source": "synthetic.json",
        "inference_call_id": "inference-001",
        "trace_schema": "synthetic-v1",
    }
    candidate = Candidate(
        case_digest=plan.case_digest,
        execution_plan_digest=plan.digest,
        retained_context_ids=("ctx-a", "ctx-c"),
        removed_context_ids=("ctx-b",),
        materialization="exact-request",
    )
    values: dict[str, object] = {
        "run_id": "run-001",
        "status": "reduced",
        "case_digest": plan.case_digest,
        "experiment_digest": _experiment().digest,
        "target_digest": plan.target_digest,
        "execution_plan_digest": plan.digest,
        "candidate_digest": candidate.digest,
        "candidate_materialization": "exact-request",
        "reduced_request_digest": content_digest(request),
        "retained_context_digest": content_digest(retained_context),
        "provenance_digest": content_digest(provenance),
        "checkpoint_digest": content_digest(checkpoint.model_dump(mode="json")),
        "runner_fingerprint": plan.runner_fingerprint,
        "algorithm_digest": ALGORITHM_DIGEST,
        "original_context_count": 3,
        "retained_context_count": 2,
        "original_context_ids": ("ctx-a", "ctx-b", "ctx-c"),
        "retained_context_ids": ("ctx-a", "ctx-c"),
        "removed_context_ids": ("ctx-b",),
        "calibration_result": "observed",
        "calibration_observation_ids": ("obs-calibration-1",),
        "search_result": "search-1-minimal",
        "confirmation_result": "confirmed-preserving",
        "confirmation_observation_ids": ("obs-confirmation-1",),
        "candidate_runs_used": 4,
        "max_candidate_runs": 10,
        "observations_recorded": 7,
        "source_case": "/private/cases/case-001",
    }
    values.update(overrides)
    return ResultSummary.model_validate(values)


def _write(destination: Path, *, summary: ResultSummary | None = None) -> Path:
    experiment_path = destination.parent / "default.toml"
    experiment_path.parent.mkdir(parents=True, exist_ok=True)
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")
    nodes = (_node("ctx-a", "/input/0"), _node("ctx-c", "/input/2"))
    checkpoint = _checkpoint()
    return write_result_snapshot(
        destination=destination,
        summary=summary or _summary(),
        request=_request(),
        retained_context_lines=tuple(node.model_dump_json() for node in nodes),
        experiment_source=experiment_path,
        provenance={
            "source": "synthetic.json",
            "inference_call_id": "inference-001",
            "trace_schema": "synthetic-v1",
        },
        execution_plan=_plan(),
        checkpoint=checkpoint,
    )


def _calibration_base(**overrides: object) -> dict[str, object]:
    plan = _sampled_plan()
    nodes = tuple(
        _node(node_id, f"/input/{index}")
        for index, node_id in enumerate(("ctx-a", "ctx-b", "ctx-c"))
    )
    candidate = Candidate(
        case_digest=plan.case_digest,
        execution_plan_digest=plan.digest,
        retained_context_ids=tuple(node.id for node in nodes),
        removed_context_ids=(),
        materialization="exact-request",
    )
    provenance = {
        "schema_version": 1,
        "source": "synthetic.json",
        "inference_call_id": "inference-001",
        "trace_schema": "synthetic-v1",
    }
    outcome = overrides.get("outcome", "incomplete")
    stop_reason = (
        "valid-sample-limit"
        if outcome in {"measurable", "not-measurable", "inconclusive"}
        else "safety-refusal"
        if outcome == "refused"
        else "user-interrupt"
    )
    values: dict[str, object] = {
        "run_id": "calibration-001",
        "outcome": outcome,
        "stop_reason": stop_reason,
        "case_digest": plan.case_digest,
        "experiment_digest": _sampled_experiment().digest,
        "target_digest": plan.target_digest,
        "execution_plan_digest": plan.digest,
        "candidate_digest": candidate.digest,
        "candidate_materialization": "exact-request",
        "request_digest": content_digest(_request()),
        "retained_context_digest": content_digest([node.model_dump(mode="json") for node in nodes]),
        "provenance_digest": content_digest(provenance),
        "runner_fingerprint": plan.runner_fingerprint,
        "algorithm_digest": ALGORITHM_DIGEST,
        "original_context_ids": tuple(node.id for node in nodes),
        "retained_context_ids": tuple(node.id for node in nodes),
        "source_case": "/private/cases/case-001",
    }
    values.update(overrides)
    return values


def _sampled_experiment() -> SampledExperimentDefinition:
    capability = _plan().capability.model_copy(
        update={
            "runner_type": "codex",
            "deterministic": False,
            "provider_transport": "required",
            "provider_calls_per_attempt": 1,
            "provider": "fixture-provider",
            "endpoint": "https://provider.invalid",
            "reports_usage": True,
        }
    )
    return SampledExperimentDefinition(
        schema_version=1,
        target=_experiment().target,
        oracle=_experiment().oracle,
        runner=NativeRunnerConfig(type="codex", required_capability_digest=capability.digest),
        calibration=SampledCalibrationPhase(required_valid=2, minimum_matches=2, max_attempts=3),
    )


def _sampled_plan() -> ResolvedExecutionPlan:
    base = _plan()
    capability = base.capability.model_copy(
        update={
            "runner_type": "codex",
            "deterministic": False,
            "provider_transport": "required",
            "provider_calls_per_attempt": 1,
            "provider": "fixture-provider",
            "endpoint": "https://provider.invalid",
            "reports_usage": True,
        }
    )
    plan = base.model_copy(
        update={
            "capability": capability,
            "runner_fingerprint": "fixture-codex:v1",
            "safety_policy": SafetyPolicy(
                provider_transport="allowed",
                allowed_provider="fixture-provider",
                allowed_endpoint="https://provider.invalid",
            ),
            "resource_limits": ResourceLimits(max_provider_calls=1),
            "experiment_digest": _sampled_experiment().digest,
        }
    )
    return ResolvedExecutionPlan.model_validate(plan.model_dump(mode="python"))


def _calibration_observation(
    attempt: int,
    outcome: ObservationOutcome,
    *,
    accounting: RunAccounting | None = None,
    usage: TokenUsage | None = None,
    acceptance: ProviderAcceptanceState = ProviderAcceptanceState.NOT_STARTED,
    possible: int = 0,
    policy_known: bool = True,
) -> Observation:
    base = _calibration_base()
    return Observation(
        observation_id=f"obs-calibration-{attempt}",
        case_digest=str(base["case_digest"]),
        candidate_digest=str(base["candidate_digest"]),
        experiment_digest=str(base["experiment_digest"]),
        runner_fingerprint=str(base["runner_fingerprint"]),
        execution_plan_digest=str(base["execution_plan_digest"]),
        target_digest=str(base["target_digest"]),
        algorithm_digest=str(base["algorithm_digest"]),
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
        attempt=attempt,
        outcome=outcome,
        error_category="runner-error" if outcome is ObservationOutcome.RUNNER_ERROR else None,
        attempt_evidence=AttemptEvidence(
            attempt_id=f"attempt-{attempt:032x}",
            attempt_digest=content_digest({"attempt": attempt}),
            termination_reason=(
                "failed" if outcome is ObservationOutcome.RUNNER_ERROR else "completed"
            ),
            provider_acceptance_state=acceptance,
            provider_calls_possible=possible,
            effective_policy_digest=_sampled_plan().safety_policy.digest if policy_known else None,
            accounting=accounting,
            usage=usage,
        ),
    )


def _write_calibration(destination: Path, summary: CalibrationSummary) -> Path:
    nodes = tuple(
        _node(node_id, f"/input/{index}")
        for index, node_id in enumerate(("ctx-a", "ctx-b", "ctx-c"))
    )
    return write_calibration_snapshot(
        destination=destination,
        summary=summary,
        request=_request(),
        retained_context_lines=tuple(node.model_dump_json() for node in nodes),
        experiment_source=_sampled_experiment().to_toml(),
        provenance={
            "source": "synthetic.json",
            "inference_call_id": "inference-001",
            "trace_schema": "synthetic-v1",
        },
        execution_plan=_sampled_plan(),
    )


def test_calibration_snapshot_round_trip_preserves_unknown_accounting(tmp_path: Path) -> None:
    complete = _calibration_observation(
        1,
        ObservationOutcome.MATCH,
        accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
        usage=TokenUsage(input_tokens=8, output_tokens=3),
        acceptance=ProviderAcceptanceState.ACCEPTED,
    )
    ambiguous = _calibration_observation(
        2,
        ObservationOutcome.RUNNER_ERROR,
        acceptance=ProviderAcceptanceState.UNKNOWN,
        possible=1,
        policy_known=False,
    )
    summary = calibration_summary_from_observations(
        base=_calibration_base(),
        observations=(complete, ambiguous),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    destination = tmp_path / "results" / "calibration"

    assert _write_calibration(destination, summary) == destination
    loaded = load_calibration_snapshot(destination)

    assert loaded.summary == summary
    assert loaded.summary.schema_version == 1
    assert loaded.summary.kind == "calibration"
    assert loaded.summary.stop_reason == "user-interrupt"
    assert loaded.summary.terminal_error_category == "runner-error"
    assert loaded.summary.calibration_observation_ids == ("obs-calibration-1", "obs-calibration-2")
    assert (loaded.summary.matches, loaded.summary.runner_errors) == (1, 1)
    assert loaded.summary.provider_calls_started is None
    assert loaded.summary.provider_calls_completed is None
    assert loaded.summary.provider_calls_possible == 1
    assert loaded.summary.provider_calls_may_have_started == 2
    assert loaded.summary.actions_requested is None
    assert loaded.summary.actions_denied is None
    assert loaded.summary.actions_executed is None
    assert loaded.summary.tool_network_requests is None
    assert loaded.summary.usage is None
    assert loaded.summary.elapsed_milliseconds is None
    assert loaded.summary.effective_policy_digest is None
    assert loaded.summary.source_store_effect == "unknown"
    assert loaded.summary.original_workspace_effect == "unknown"
    assert "checkpoint.json" not in {path.name for path in destination.iterdir()}


def test_calibration_ambiguous_provider_acceptance_does_not_double_count_known_starts() -> None:
    ambiguous = _calibration_observation(
        1,
        ObservationOutcome.RUNNER_ERROR,
        accounting=RunAccounting(provider_calls_started=1),
        acceptance=ProviderAcceptanceState.UNKNOWN,
        possible=1,
    )

    summary = calibration_summary_from_observations(
        base=_calibration_base(stop_reason="unsafe-retry"),
        observations=(ambiguous,),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )

    assert summary.provider_calls_started == 1
    assert summary.provider_calls_completed == 0
    assert summary.provider_calls_possible == 1
    assert summary.provider_calls_may_have_started == 1


def test_calibration_accepted_call_keeps_possible_continuation_in_upper_bound() -> None:
    completed = _calibration_observation(
        1,
        ObservationOutcome.MATCH,
        accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
        acceptance=ProviderAcceptanceState.ACCEPTED,
    )

    summary = calibration_summary_from_observations(
        base=_calibration_base(),
        observations=(completed,),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=2,
    )

    assert summary.provider_calls_started == 1
    assert summary.provider_calls_possible == 0
    assert summary.provider_calls_may_have_started == 2


def test_calibration_snapshot_rejects_nonrefused_provider_bound_over_plan(tmp_path: Path) -> None:
    completed = _calibration_observation(
        1,
        ObservationOutcome.MATCH,
        accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
        acceptance=ProviderAcceptanceState.ACCEPTED,
    )
    summary = calibration_summary_from_observations(
        base=_calibration_base(),
        observations=(completed,),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )

    with pytest.raises(ResultSnapshotError, match="provider-call bound exceeds"):
        _write_calibration(
            tmp_path / "over-plan",
            summary.model_copy(update={"provider_calls_may_have_started": 2}),
        )


def test_calibration_snapshot_rejects_wrong_kind_and_tampering(tmp_path: Path) -> None:
    summary = calibration_summary_from_observations(
        base=_calibration_base(),
        observations=(),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    destination = tmp_path / "results" / "calibration"
    _write_calibration(destination, summary)

    with pytest.raises(ResultSnapshotError, match="kind"):
        load_result_snapshot(destination)
    (destination / "checkpoint.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ResultSnapshotError, match="unexpected entries"):
        load_calibration_snapshot(destination)
    (destination / "checkpoint.json").unlink()
    summary_data = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    summary_data["candidate_digest"] = "f" * 64
    (destination / "summary.json").write_text(json.dumps(summary_data), encoding="utf-8")
    with pytest.raises(ResultSnapshotError, match="candidate_digest"):
        load_calibration_snapshot(destination)


@pytest.mark.parametrize("kind", ["reduction", "calibration"])
@pytest.mark.parametrize(
    ("field", "value", "remove"),
    [
        ("kind", None, True),
        ("kind", "unknown", False),
        ("kind", 1, False),
        ("schema_version", None, True),
        ("schema_version", 2, False),
        ("schema_version", True, False),
        ("schema_version", "1", False),
    ],
)
def test_snapshot_loaders_require_current_version_and_explicit_kind(
    tmp_path: Path, kind: str, field: str, value: object, remove: bool
) -> None:
    destination = tmp_path / "result"
    if kind == "reduction":
        _write(destination)
        loader = load_result_snapshot
    else:
        summary = calibration_summary_from_observations(
            base=_calibration_base(),
            observations=(),
            expected_policy_digest=_sampled_plan().safety_policy.digest,
            max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
        )
        _write_calibration(destination, summary)
        loader = load_calibration_snapshot
    summary_path = destination / "summary.json"
    summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
    if remove:
        summary_data.pop(field)
    else:
        summary_data[field] = value
    summary_path.write_text(json.dumps(summary_data), encoding="utf-8")

    with pytest.raises(ResultSnapshotError, match=field):
        loader(destination)


def test_calibration_loader_rejects_reduction_result_kind(tmp_path: Path) -> None:
    destination = tmp_path / "reduction"
    _write(destination)

    with pytest.raises(ResultSnapshotError, match="kind"):
        load_calibration_snapshot(destination)


def test_calibration_snapshot_rejects_inconsistent_stop_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="completed calibration requires"):
        calibration_summary_from_observations(
            base=_calibration_base(outcome="measurable", stop_reason="attempt-limit"),
            observations=(),
            expected_policy_digest=_sampled_plan().safety_policy.digest,
            max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
        )
    summary = calibration_summary_from_observations(
        base=_calibration_base(stop_reason="attempt-limit"),
        observations=(),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    with pytest.raises(ResultSnapshotError, match="attempt-limit stop reason"):
        _write_calibration(tmp_path / "early-limit", summary)
    refused = calibration_summary_from_observations(
        base=_calibration_base(outcome="refused"),
        observations=(),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    with pytest.raises(ResultSnapshotError, match="safety-refusal stop reason"):
        _write_calibration(tmp_path / "unsupported-refusal", refused)
    generic_error = _calibration_observation(
        1, ObservationOutcome.RUNNER_ERROR, accounting=RunAccounting()
    )
    refused_for_generic_error = calibration_summary_from_observations(
        base=_calibration_base(outcome="refused"),
        observations=(generic_error,),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    with pytest.raises(ResultSnapshotError, match="safety-refusal stop reason"):
        _write_calibration(tmp_path / "generic-error-refusal", refused_for_generic_error)
    unsafe = calibration_summary_from_observations(
        base=_calibration_base(stop_reason="unsafe-retry"),
        observations=(),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    with pytest.raises(ResultSnapshotError, match="unsafe-retry stop reason"):
        _write_calibration(tmp_path / "unsupported-retry", unsafe)


def test_calibration_aggregate_uses_only_complete_reported_usage() -> None:
    observations = (
        _calibration_observation(
            1,
            ObservationOutcome.MATCH,
            accounting=RunAccounting(
                elapsed_milliseconds=11, actions_requested=1, actions_denied=1
            ),
            usage=TokenUsage(input_tokens=3, output_tokens=2, cached_input_tokens=1),
        ),
        _calibration_observation(
            2,
            ObservationOutcome.NO_MATCH,
            accounting=RunAccounting(elapsed_milliseconds=17),
            usage=TokenUsage(input_tokens=5, output_tokens=4),
        ),
    )
    summary = calibration_summary_from_observations(
        base=_calibration_base(),
        observations=observations,
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )

    assert summary.provider_calls_started == 0
    assert summary.provider_calls_completed == 0
    assert (summary.actions_requested, summary.actions_denied, summary.actions_executed) == (
        1,
        1,
        0,
    )
    assert summary.tool_network_requests == 0
    assert summary.elapsed_milliseconds == 28
    assert summary.usage == TokenUsage(input_tokens=8, output_tokens=6)
    assert summary.effective_policy_digest == _sampled_plan().safety_policy.digest


def test_calibration_aggregate_preserves_may_change_protected_effect() -> None:
    observation = _calibration_observation(
        1,
        ObservationOutcome.RUNNER_ERROR,
        accounting=RunAccounting(
            source_store_effect="may-change",
            original_workspace_effect="may-change",
        ),
    )
    summary = calibration_summary_from_observations(
        base=_calibration_base(outcome="refused"),
        observations=(observation,),
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )

    assert summary.source_store_effect == "may-change"
    assert summary.original_workspace_effect == "may-change"


def test_calibration_aggregate_rejects_mismatched_or_unordered_observations() -> None:
    valid = _calibration_observation(1, ObservationOutcome.MATCH, accounting=RunAccounting())
    wrong_arm = valid.model_copy(update={"arm": ObservationArm.CANDIDATE})
    with pytest.raises(ValueError, match="role, arm, or candidate"):
        calibration_summary_from_observations(
            base=_calibration_base(),
            observations=(wrong_arm,),
            expected_policy_digest=_sampled_plan().safety_policy.digest,
            max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
        )
    with pytest.raises(ValueError, match="unique and ordered"):
        calibration_summary_from_observations(
            base=_calibration_base(),
            observations=(valid, valid),
            expected_policy_digest=_sampled_plan().safety_policy.digest,
            max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
        )


@pytest.mark.parametrize(
    ("outcome", "observed", "accepted"),
    [
        ("measurable", (ObservationOutcome.MATCH, ObservationOutcome.MATCH), True),
        ("not-measurable", (ObservationOutcome.NO_MATCH, ObservationOutcome.NO_MATCH), True),
        ("inconclusive", (ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH), True),
        ("measurable", (ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH), False),
        ("not-measurable", (ObservationOutcome.MATCH, ObservationOutcome.NO_MATCH), False),
        ("incomplete", (ObservationOutcome.MATCH, ObservationOutcome.MATCH), False),
    ],
)
def test_calibration_outcome_uses_sampled_experiment_thresholds(
    tmp_path: Path,
    outcome: str,
    observed: tuple[ObservationOutcome, ObservationOutcome],
    accepted: bool,
) -> None:
    observations = tuple(
        _calibration_observation(
            attempt,
            verdict,
            accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
            acceptance=ProviderAcceptanceState.ACCEPTED,
        )
        for attempt, verdict in enumerate(observed, start=1)
    )
    summary = calibration_summary_from_observations(
        base=_calibration_base(outcome=outcome),
        observations=observations,
        expected_policy_digest=_sampled_plan().safety_policy.digest,
        max_provider_calls_per_attempt=_sampled_plan().capability.provider_calls_per_attempt,
    )
    destination = tmp_path / outcome
    if accepted:
        _write_calibration(destination, summary)
        assert load_calibration_snapshot(destination).summary.outcome == outcome
    else:
        with pytest.raises(ResultSnapshotError, match="outcome"):
            _write_calibration(destination, summary)


def test_result_snapshot_round_trip_is_complete_and_immutable(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"

    assert _write(destination) == destination
    loaded = load_result_snapshot(destination)

    assert loaded.root == destination
    assert loaded.summary == _summary()
    assert loaded.summary.schema_version == 1
    assert loaded.summary.kind == "reduction"
    assert loaded.summary.status == "reduced"
    assert loaded.reduced_request is not None
    assert loaded.reduced_request["model"] == "fake"
    assert tuple(
        node.id for node in map(ContextNode.model_validate_json, loaded.retained_context_lines)
    ) == ("ctx-a", "ctx-c")
    assert loaded.experiment.digest == _experiment().digest
    assert loaded.execution_plan.digest == _plan().digest
    assert loaded.provenance.source == "synthetic.json"
    assert loaded.checkpoint == _checkpoint()
    assert {path.name for path in destination.iterdir()} == {
        "summary.json",
        "request.json",
        "retained-context.jsonl",
        "experiment.toml",
        "provenance.json",
        "execution-plan.json",
        "checkpoint.json",
    }
    with pytest.raises(ValidationError, match="frozen"):
        loaded.summary.status = "stopped"  # type: ignore[misc]


def test_source_backed_result_has_no_materialized_request(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "source-backed"
    experiment_path = tmp_path / "default.toml"
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")
    original_plan = _plan()
    source_capability = original_plan.capability.model_copy(
        update={
            "harness": "fixture-harness",
            "source_schema": "fixture-source-schema",
            "supported_capture_types": ("logical-session",),
            "supported_materializations": ("source-backed",),
            "reconstruction_method": "source-backed-test-v1",
        }
    )
    plan = original_plan.model_copy(
        update={
            "capture_type": "logical-session",
            "candidate_materialization": "source-backed",
            "capability": source_capability,
            "harness": source_capability.harness,
            "source_schema": source_capability.source_schema,
        }
    )
    checkpoint = _checkpoint(execution_plan_digest=plan.digest)
    retained_nodes = (_node("ctx-a", "/input/0"), _node("ctx-c", "/input/2"))
    candidate = Candidate(
        case_digest=plan.case_digest,
        execution_plan_digest=plan.digest,
        source_artifact_digests=("1" * 64,),
        retained_context_ids=("ctx-a", "ctx-c"),
        removed_context_ids=("ctx-b",),
        materialization="source-backed",
    )
    provenance = {
        "schema_version": 1,
        "source": "synthetic.json",
        "inference_call_id": "inference-001",
        "trace_schema": "synthetic-v1",
    }
    summary = _summary(
        execution_plan_digest=plan.digest,
        candidate_digest=candidate.digest,
        candidate_materialization="source-backed",
        source_artifact_digests=candidate.source_artifact_digests,
        reduced_request_digest=None,
        checkpoint_digest=content_digest(checkpoint.model_dump(mode="json")),
    )

    write_result_snapshot(
        destination=destination,
        summary=summary,
        request=None,
        retained_context_lines=tuple(node.model_dump_json() for node in retained_nodes),
        experiment_source=experiment_path,
        provenance=provenance,
        execution_plan=plan,
        checkpoint=checkpoint,
    )
    loaded = load_result_snapshot(destination)

    assert loaded.reduced_request is None
    assert not (destination / "request.json").exists()


def test_result_snapshot_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)

    with pytest.raises(FileExistsError, match="already exists"):
        _write(destination)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_result_snapshot_uses_and_requires_private_permissions(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)

    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())

    (destination / "summary.json").chmod(0o644)
    with pytest.raises(ResultSnapshotError, match=r"summary\.json must have mode 0600"):
        load_result_snapshot(destination)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"original_context_count": 2}, "original_context_count"),
        ({"retained_context_ids": ("ctx-a", "ctx-a")}, "duplicates"),
        ({"removed_context_ids": ("ctx-a", "ctx-b")}, "disjoint"),
        ({"candidate_runs_used": 11}, "must not exceed"),
        ({"resume_of": "run-001"}, "earlier run"),
        ({"calibration_observation_ids": ()}, "calibration outcome requires"),
        ({"confirmation_observation_ids": ()}, "confirmation outcome requires"),
        (
            {"calibration_observation_ids": ("obs-calibration-1", "obs-calibration-1")},
            "must not contain duplicates",
        ),
        ({"observations_recorded": 5}, "all referenced observations"),
        (
            {"status": "stopped", "search_result": "search-incomplete"},
            "require next_action",
        ),
        ({"confirmation_result": "not-run"}, "reduced results require"),
        (
            {"status": "confirmation-failed"},
            "confirmation-failed status requires a failed confirmation",
        ),
        (
            {"status": "not-observed"},
            "not-observed status requires a not-observed calibration",
        ),
        (
            {
                "status": "failed",
                "calibration_result": "failed",
                "confirmation_result": "not-run",
                "confirmation_observation_ids": (),
            },
            "search requires an observed calibration target",
        ),
        (
            {
                "status": "failed",
                "search_result": "not-run",
                "confirmation_result": "failed",
            },
            "confirmation requires a completed or incomplete search",
        ),
        (
            {
                "status": "failed",
                "search_result": "not-run",
                "confirmation_result": "not-run",
                "confirmation_observation_ids": (),
            },
            "candidate_runs_used must be zero when search was not run",
        ),
        (
            {
                "status": "calibrated",
                "search_result": "search-incomplete",
                "confirmation_result": "not-run",
                "confirmation_observation_ids": (),
            },
            "calibrated status requires an observed baseline and no later phases",
        ),
        (
            {"status": "failed"},
            "confirmed-preserving confirmation requires reduced status",
        ),
        (
            {
                "status": "interrupted",
                "confirmation_result": "failed",
                "next_action": "Review the interrupted result.",
            },
            "failed confirmation requires a failed result status",
        ),
        (
            {
                "status": "failed",
                "search_result": "search-incomplete",
                "confirmation_result": "failed",
            },
            "confirmation requires a search-1-minimal result",
        ),
    ],
)
def test_result_summary_rejects_inconsistent_claims(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _summary(**overrides)


def test_interrupted_summary_records_exact_continuation_and_parent() -> None:
    summary = _summary(
        run_id="run-002",
        status="interrupted",
        search_result="search-incomplete",
        confirmation_result="not-run",
        confirmation_observation_ids=(),
        resume_of="run-001",
        next_action="Continue the saved search.",
        next_command="ithrl shrink /private/cases/case-001 --resume run-002",
        next_command_shell="posix",
    )

    assert summary.resume_of == "run-001"
    assert summary.next_command == "ithrl shrink /private/cases/case-001 --resume run-002"


def test_writer_rejects_identity_and_context_mismatches(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    experiment_path = tmp_path / "default.toml"
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")
    lines = tuple(
        node.model_dump_json() for node in (_node("ctx-a", "/input/0"), _node("ctx-c", "/input/2"))
    )
    provenance = {
        "source": "synthetic.json",
        "inference_call_id": None,
        "trace_schema": "synthetic-v1",
    }

    with pytest.raises(ResultSnapshotError, match="execution_plan_digest"):
        write_result_snapshot(
            destination=destination,
            summary=_summary(execution_plan_digest="f" * 64),
            request={},
            retained_context_lines=lines,
            experiment_source=experiment_path,
            provenance=provenance,
            execution_plan=_plan(),
            checkpoint=_checkpoint(),
        )

    with pytest.raises(ResultSnapshotError, match="retained_context_digest"):
        write_result_snapshot(
            destination=destination,
            summary=_summary(),
            request=_request(),
            retained_context_lines=(_node("ctx-c", "/input/2").model_dump_json(),),
            experiment_source=experiment_path,
            provenance=provenance,
            execution_plan=_plan(),
            checkpoint=_checkpoint(),
        )


@pytest.mark.parametrize(
    ("target_digest", "target_type"),
    [
        ("f" * 64, "shell-action"),
        (_plan().target_digest, "final-response"),
    ],
)
def test_writer_binds_execution_plan_target_to_experiment(
    tmp_path: Path,
    target_digest: str,
    target_type: str,
) -> None:
    destination = tmp_path / "results" / "result-test"
    experiment_path = tmp_path / "default.toml"
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")
    plan = _plan().model_copy(update={"target_digest": target_digest, "target_type": target_type})
    checkpoint = _checkpoint(target_digest=target_digest)
    summary = _summary(
        target_digest=target_digest,
        execution_plan_digest=plan.digest,
        checkpoint_digest=content_digest(checkpoint.model_dump(mode="json")),
    )

    with pytest.raises(ResultSnapshotError, match="target does not match experiment"):
        write_result_snapshot(
            destination=destination,
            summary=summary,
            request=_request(),
            retained_context_lines=tuple(
                node.model_dump_json()
                for node in (_node("ctx-a", "/input/0"), _node("ctx-c", "/input/2"))
            ),
            experiment_source=experiment_path,
            provenance={
                "source": "synthetic.json",
                "inference_call_id": "inference-001",
                "trace_schema": "synthetic-v1",
            },
            execution_plan=plan,
            checkpoint=checkpoint,
        )

    assert not destination.exists()


@pytest.mark.parametrize(
    ("request_value", "message"),
    [
        ({"model": "fake", "input": ("not", "json")}, "unsupported tuple"),
        ({"model": "fake", 1: "not-json"}, "non-string key"),
    ],
)
def test_writer_rejects_non_json_request_values_before_writing(
    tmp_path: Path,
    request_value: dict[object, object],
    message: str,
) -> None:
    destination = tmp_path / "results" / "result-test"
    experiment_path = tmp_path / "default.toml"
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")

    with pytest.raises(ResultSnapshotError, match=message):
        write_result_snapshot(
            destination=destination,
            summary=_summary(),
            request=request_value,  # type: ignore[arg-type]
            retained_context_lines=(),
            experiment_source=experiment_path,
            provenance={
                "source": "synthetic.json",
                "inference_call_id": "inference-001",
                "trace_schema": "synthetic-v1",
            },
            execution_plan=_plan(),
            checkpoint=_checkpoint(),
        )

    assert not destination.exists()


def test_writer_normalizes_invalid_retained_graph_errors(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    experiment_path = tmp_path / "default.toml"
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")
    nodes = (
        _node("ctx-a", "/input/0"),
        _node("ctx-c", "/input/2").model_copy(update={"depends_on": ("missing",)}),
    )
    context_data = [node.model_dump(mode="json") for node in nodes]

    with pytest.raises(ResultSnapshotError, match="valid context graph"):
        write_result_snapshot(
            destination=destination,
            summary=_summary(retained_context_digest=content_digest(context_data)),
            request=_request(),
            retained_context_lines=tuple(node.model_dump_json() for node in nodes),
            experiment_source=experiment_path,
            provenance={
                "source": "synthetic.json",
                "inference_call_id": "inference-001",
                "trace_schema": "synthetic-v1",
            },
            execution_plan=_plan(),
            checkpoint=_checkpoint(),
        )

    assert not destination.exists()


def test_writer_rejects_invalid_checkpoint_without_partial_snapshot(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    experiment_path = tmp_path / "default.toml"
    experiment_path.write_text(_EXPERIMENT, encoding="utf-8")

    checkpoint = _checkpoint().model_dump(mode="json")
    checkpoint["candidate_runs_used"] = 5
    with pytest.raises(ResultSnapshotError, match="candidate_runs_used"):
        write_result_snapshot(
            destination=destination,
            summary=_summary(),
            request={},
            retained_context_lines=tuple(
                node.model_dump_json()
                for node in (_node("ctx-a", "/input/0"), _node("ctx-c", "/input/2"))
            ),
            experiment_source=experiment_path,
            provenance={
                "source": "synthetic.json",
                "inference_call_id": None,
                "trace_schema": "synthetic-v1",
            },
            execution_plan=_plan(),
            checkpoint=checkpoint,
        )

    assert not destination.exists()


@pytest.mark.parametrize("missing", ["summary.json", "checkpoint.json", "execution-plan.json"])
def test_loader_rejects_missing_required_file(tmp_path: Path, missing: str) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    (destination / missing).unlink()

    with pytest.raises(ResultSnapshotError, match=f"missing: {missing}"):
        load_result_snapshot(destination)


def test_loader_rejects_malformed_and_duplicate_key_json(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    (destination / "checkpoint.json").write_text('{"phase": 1, "phase": 2}\n')
    if os.name == "posix":
        (destination / "checkpoint.json").chmod(0o600)

    with pytest.raises(ResultSnapshotError, match="duplicate key 'phase'"):
        load_result_snapshot(destination)


def test_loader_rejects_oversized_file_before_reading(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    summary_path = destination / "summary.json"
    with summary_path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(ResultSnapshotError, match=r"summary\.json exceeds the .*byte limit"):
        load_result_snapshot(destination)


@pytest.mark.skipif(os.name != "posix", reason="symbolic-link contract")
def test_loader_rejects_symlinked_snapshot_file(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    summary = destination / "summary.json"
    outside = tmp_path / "outside.json"
    outside.write_text(summary.read_text(), encoding="utf-8")
    summary.unlink()
    summary.symlink_to(outside)

    with pytest.raises(ResultSnapshotError, match="regular file, not a link"):
        load_result_snapshot(destination)


@pytest.mark.skipif(os.name != "posix", reason="symbolic-link contract")
def test_loader_rejects_symlinked_snapshot_directory(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    linked = tmp_path / "result-link"
    linked.symlink_to(destination, target_is_directory=True)

    with pytest.raises(ResultSnapshotError, match="path contains a symbolic link"):
        load_result_snapshot(linked)


def test_loader_rejects_tampered_cross_file_identity(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    summary_path = destination / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["runner_fingerprint"] = "different-runner"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    if os.name == "posix":
        summary_path.chmod(0o600)

    with pytest.raises(ResultSnapshotError, match="runner identities differ"):
        load_result_snapshot(destination)


def test_loader_rejects_tampered_reduced_request(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    request_path = destination / "request.json"
    request = json.loads(request_path.read_text())
    request["model"] = "tampered"
    _rewrite_json(request_path, request)

    with pytest.raises(ResultSnapshotError, match="reduced_request_digest"):
        load_result_snapshot(destination)


def test_loader_rejects_tampered_retained_context(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    context_path = destination / "retained-context.jsonl"
    lines = context_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["source"] = "other.json"
    lines[0] = json.dumps(first)
    context_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        context_path.chmod(0o600)

    with pytest.raises(ResultSnapshotError, match="retained_context_digest"):
        load_result_snapshot(destination)


def test_loader_rejects_tampered_provenance(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    provenance_path = destination / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["source"] = "other.json"
    _rewrite_json(provenance_path, provenance)

    with pytest.raises(ResultSnapshotError, match="provenance_digest"):
        load_result_snapshot(destination)


@pytest.mark.parametrize(
    ("checkpoint_update", "summary_update", "message"),
    [
        (
            {"runner_fingerprint": "another-runner"},
            {},
            "checkpoint identities",
        ),
        (
            {"removed_context_ids": []},
            {},
            "removed_context_ids",
        ),
        (
            {"final_sweep_complete": False},
            {},
            "complete final sweep",
        ),
        (
            {},
            {"candidate_runs_used": 3},
            "checkpoint candidate_runs_used",
        ),
    ],
)
def test_loader_rejects_cross_file_checkpoint_claims(
    tmp_path: Path,
    checkpoint_update: dict[str, object],
    summary_update: dict[str, object],
    message: str,
) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    checkpoint_path = destination / "checkpoint.json"
    summary_path = destination / "summary.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    summary = json.loads(summary_path.read_text())
    checkpoint.update(checkpoint_update)
    summary.update(summary_update)
    summary["checkpoint_digest"] = content_digest(checkpoint)
    _rewrite_json(checkpoint_path, checkpoint)
    _rewrite_json(summary_path, summary)

    with pytest.raises(ResultSnapshotError, match=message):
        load_result_snapshot(destination)


def test_loader_rejects_tampered_checkpoint_without_matching_digest(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    checkpoint_path = destination / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    empty_removals: list[str] = []
    checkpoint["removed_context_ids"] = empty_removals
    _rewrite_json(checkpoint_path, checkpoint)

    with pytest.raises(ResultSnapshotError, match="checkpoint_digest"):
        load_result_snapshot(destination)


def test_loader_rejects_duplicate_checkpoint_observation_ids(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    checkpoint_path = destination / "checkpoint.json"
    summary_path = destination / "summary.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    summary = json.loads(summary_path.read_text())
    checkpoint["trials"][1]["observation_id"] = checkpoint["trials"][0]["observation_id"]
    summary["checkpoint_digest"] = content_digest(checkpoint)
    _rewrite_json(checkpoint_path, checkpoint)
    _rewrite_json(summary_path, summary)

    with pytest.raises(ResultSnapshotError, match="observation IDs must not contain duplicates"):
        load_result_snapshot(destination)


def test_loader_rejects_fabricated_invalid_final_sweep_proof(tmp_path: Path) -> None:
    destination = tmp_path / "results" / "result-test"
    _write(destination)
    checkpoint_path = destination / "checkpoint.json"
    summary_path = destination / "summary.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    summary = json.loads(summary_path.read_text())
    trial = checkpoint["trials"][-1]
    trial.update(
        {
            "candidate_digest": None,
            "effective_removals": trial["base_removals"],
            "target_observed": None,
            "classification": "invalid",
            "runner_executed": False,
            "observation_id": None,
            "observation_attempt": None,
        }
    )
    checkpoint["candidate_runs_used"] -= 1
    summary["candidate_runs_used"] -= 1
    summary["checkpoint_digest"] = content_digest(checkpoint)
    _rewrite_json(checkpoint_path, checkpoint)
    _rewrite_json(summary_path, summary)

    with pytest.raises(ResultSnapshotError, match="does not prove a losing removal"):
        load_result_snapshot(destination)


def _rewrite_json(path: Path, value: object) -> None:
    path.write_text(f"{json.dumps(value)}\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
