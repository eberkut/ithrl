"""Directed comparisons use two frozen contexts and independent confirmation evidence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast, override

import pytest

from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence
from ithrl.cli.comparison_output import result_report
from ithrl.comparison import engine as comparison_engine
from ithrl.comparison.contracts import (
    ComparisonCalibration,
    ComparisonConfiguration,
    ComparisonConfirmation,
    ComparisonLimits,
    ComparisonOutcome,
    ComparisonProgress,
)
from ithrl.comparison.engine import (
    PreparedComparison,
    comparison_budget_bounds,
    prepare_comparison,
    run_comparison,
)
from ithrl.comparison.results import (
    ComparisonEvidence,
    load_comparison_result,
    write_comparison_result,
)
from ithrl.context import content_digest
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.experiment import (
    CodexReplayConfig,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    SamplingArmTarget,
    StructuralOracleConfig,
)
from ithrl.mutation import ContextSelection, MutationError, TextAppend, TextMutation
from ithrl.observations import ObservationStore, SamplingBlockEvidence, SamplingBudgetAllowance
from ithrl.run_state import RunStateError, SampledRunStateFile, SamplingBudgetApproval
from ithrl.runner import (
    Candidate,
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerRegistry,
    RunResult,
)
from ithrl.runner.contracts import RunnerError, RunnerErrorCode
from ithrl.runner.fake import FakeRunner
from ithrl.synthetic import extract_inference
from ithrl.workflow import PreparedExperiment, prepare_experiment

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


class CountingRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__(required_markers=frozenset({"A", "C"}), final_text="BLUE")
        self.attempts: list[PreparedAttempt] = []
        self.fail = False
        self.power_loss = False
        self.lose_effect = False
        self.cancel = False
        self.model_drift = False
        self.admissions: list[Candidate] = []

    def validate_candidate(self, attempt: PreparedAttempt) -> None:
        self.admissions.append(attempt.candidate)

    @override
    async def run(
        self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        self.attempts.append(attempt)
        if self.cancel:
            raise asyncio.CancelledError
        if self.power_loss:
            raise SystemExit("simulated process loss")
        if self.fail:
            raise RunnerError(RunnerErrorCode.CONTRACT, "sanitized test refusal")
        result = await super().run(attempt, runtime_deadline=runtime_deadline)
        if self.model_drift:
            return result.model_copy(update={"model_returned": "different-model"})
        return (
            result.model_copy(update={"final_text": "BLUE"})
            if self.lose_effect and attempt.phase == "confirmation"
            else result
        )


class OverloadRunner(CountingRunner):
    """Simulate accounted provider observations without native or network execution."""

    def __init__(self, phase: str, harness: Literal["codex", "copilot"]) -> None:
        super().__init__()
        self.harness = harness
        self.overload_phase = phase
        self.overloaded = False

    @property
    @override
    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile.model_validate(
            {
                **super().capability_profile.model_dump(),
                "runner_type": self.harness,
                "deterministic": False,
                "provider_transport": "required",
                "provider_calls_per_attempt": 1,
                "provider": "fixture-provider",
                "endpoint": "https://example.invalid/responses",
                "protected_state_enforcement": "preventive",
            }
        )

    @override
    async def run(
        self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        if attempt.phase == self.overload_phase and not self.overloaded:
            self.overloaded = True
            self.attempts.append(attempt)
            raise RunnerError(
                RunnerErrorCode.PROVIDER_OVERLOADED,
                "Fixture overload.",
                provider_acceptance_state=ProviderAcceptanceState.UNKNOWN,
                model_requested=attempt.execution_plan.model_requested,
                provider=self.capability_profile.provider,
                endpoint=self.capability_profile.endpoint,
                effective_policy_digest=attempt.safety_policy.digest,
                behavior_output_observed=False,
                native_output=NativeOutputReference(
                    artifact_id="fixture-overload",
                    sha256="a" * 64,
                    media_type="application/json",
                    size_bytes=100,
                ),
                accounting=RunAccounting(
                    provider_calls_started=1,
                    native_output_bytes=100,
                    process_outcome="exited",
                    cleanup_outcome="verified",
                ),
            )
        result = await super().run(attempt, runtime_deadline=runtime_deadline)
        return result.model_copy(
            update={
                "provider": self.capability_profile.provider,
                "endpoint": self.capability_profile.endpoint,
                "accounting": RunAccounting(
                    provider_calls_started=1,
                    provider_calls_completed=1,
                ),
            }
        )


@dataclass
class ComparisonFixture:
    evidence: CaseEvidence
    prepared: PreparedExperiment
    configuration: ComparisonConfiguration
    state: SampledRunStateFile
    store: ObservationStore
    runner: CountingRunner
    admission: PreparedComparison

    def run(
        self,
        *,
        on_progress: Callable[[SamplingBlockEvidence], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        calibration_only: bool = False,
    ) -> ComparisonEvidence:
        return asyncio.run(
            run_comparison(
                evidence=self.evidence,
                prepared=self.prepared,
                configuration=self.configuration,
                state_file=self.state,
                store=self.store,
                admission=self.admission,
                on_progress=on_progress,
                clock=clock,
                calibration_only=calibration_only,
            )
        )


def _fixture(
    tmp_path: Path,
    *,
    objective: Literal["repair", "degradation"] = "repair",
    both_nonfull: bool = False,
    indistinguishable: bool = False,
    append: bool = False,
    wall_seconds: int = 1000,
    freshness_seconds: int = 600,
    pacing_seconds: int = 0,
    runner: CountingRunner | None = None,
) -> ComparisonFixture:
    evidence = extract_inference(
        FIXTURE, inference_call_id="synthetic-ac-001", case_path=tmp_path / "case"
    ).load()
    runner = runner or CountingRunner()
    profile = runner.capability_profile
    provider_calls = profile.provider_calls_per_attempt
    experiment = SampledExperimentDefinition(
        target=FinalResponseTarget(text_equals="BLUE"),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type=cast(Literal["codex", "copilot"], profile.runner_type),
            required_capability_digest=profile.digest,
        )
        if provider_calls
        else FakeRunnerConfig(required_markers=("A", "C"), final_text="BLUE"),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
    )
    prepared = prepare_experiment(
        evidence,
        experiment=experiment,
        registry=RunnerRegistry({profile.runner_type: lambda _config, _evidence: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider=profile.provider,
            allowed_endpoint=profile.endpoint,
        )
        if provider_calls
        else None,
        resource_limits=ResourceLimits(max_provider_calls=provider_calls, max_wall_time_seconds=1),
    )
    nodes = {node.path: node.id for node in evidence.context.nodes}
    absent = (nodes["/input/0"],) + ((nodes["/input/1"],) if both_nonfull else ())
    present = (nodes["/input/1"],) if both_nonfull or indistinguishable else ()
    if indistinguishable:
        absent = ()
    reference, candidate = (absent, present) if objective == "repair" else (present, absent)
    reference_selection = ContextSelection(removed_context_ids=reference)
    candidate_selection = ContextSelection(removed_context_ids=candidate)
    if append:
        reference_selection = ContextSelection()
        candidate_selection = ContextSelection(
            mutation=TextMutation(
                edits=(
                    TextAppend(
                        context_id=nodes["/input/0"],
                        content_index=0,
                        original_text_digest=content_digest("A"),
                        text="\nAdditional context.",
                    ),
                )
            )
        )
    one = SamplingArmTarget(required_valid=1, max_attempts=1)
    seven = SamplingArmTarget(required_valid=7, max_attempts=7)
    configuration = ComparisonConfiguration(
        objective=objective,
        reference=reference_selection,
        candidate=candidate_selection,
        calibration=ComparisonCalibration(
            reference=one, candidate=one, positive_minimum_matches=1, negative_maximum_matches=0
        ),
        confirmation=ComparisonConfirmation(reference=seven, candidate=seven, alpha=0.05),
        minimum_effect=0.1,
        seed=7,
        eligibility_policy="safe-provider-overload"
        if provider_calls
        else "independent-safe-preaccept-only",
        freshness_seconds=freshness_seconds,
        limits=ComparisonLimits(
            max_provider_calls=100 * provider_calls,
            max_wall_time_seconds=wall_seconds,
            attempt_timeout_seconds=1,
            inter_attempt_delay_seconds=pacing_seconds,
        ),
    )
    admission = prepare_comparison(evidence, prepared, configuration)
    prepared, binding = admission.prepared, admission.binding
    bounds = comparison_budget_bounds(
        configuration, sampling_attempt_cost(runner.capability_profile, prepared.resource_limits)
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    store.initialize()
    started = datetime.now(UTC)
    state = SampledRunStateFile.create(
        case_root=tmp_path / "case",
        run_id="direct-comparison",
        store=store,
        started_at=started,
        original_identity=prepared.identity,
        original_candidate_digest=binding.reference.digest,
        provider_calls_per_attempt=provider_calls,
        comparison_progress=ComparisonProgress(binding=binding),
        approval=SamplingBudgetApproval(
            approval_id="provider-free-test",
            approved_at=started,
            allowance=SamplingBudgetAllowance(
                run_id="direct-comparison",
                max_provider_calls=100 * provider_calls,
                max_wall_time_seconds=wall_seconds,
                max_candidate_comparisons=1,
                confirmation_provider_calls=bounds.confirmation_provider_calls,
                confirmation_runner_seconds=bounds.confirmation_runner_seconds,
                inter_attempt_delay_seconds=pacing_seconds,
            ),
        ),
    )
    return ComparisonFixture(evidence, prepared, configuration, state, store, runner, admission)


@pytest.mark.parametrize("objective", ["repair", "degradation"])
@pytest.mark.parametrize("both_nonfull", [False, True])
def test_directed_effect_and_completed_resume(
    tmp_path: Path, objective: Literal["repair", "degradation"], both_nonfull: bool
) -> None:
    fixture = _fixture(tmp_path, objective=objective, both_nonfull=both_nonfull)
    result = fixture.run()
    assert result.outcome == (
        ComparisonOutcome.CONFIRMED_IMPROVEMENT
        if objective == "repair"
        else ComparisonOutcome.CONFIRMED_DEGRADATION
    )
    assert result.difference == 1
    assert result.lower_bound is not None
    assert result.lower_bound > 0.1
    assert result.calibration is not None
    assert len(result.calibration.attempts) == 2
    assert result.confirmation is not None
    assert len(result.confirmation.attempts) == 14
    assert result.selected_at is not None
    assert all(
        attempt.observation.recorded_at >= result.selected_at
        for attempt in result.confirmation.attempts
    )
    assert len(fixture.runner.attempts) == 16
    result.validate_for_case(fixture.evidence)
    repeated = fixture.run()
    assert repeated.blocks == result.blocks
    assert len(fixture.runner.attempts) == 16


def test_failed_calibration_never_selects_or_confirms(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, indistinguishable=True)
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.CALIBRATION_FAILED
    assert result.selected_at is None
    assert result.confirmation is None
    assert result.difference is result.lower_bound is None
    assert len(fixture.runner.attempts) == 2
    assert fixture.run().outcome is ComparisonOutcome.CALIBRATION_FAILED
    assert len(fixture.runner.attempts) == 2


def test_calibration_only_stops_before_selection_and_can_resume_confirmation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    pilot = fixture.run(calibration_only=True)
    assert pilot.outcome is ComparisonOutcome.INCOMPLETE
    assert pilot.stop_reason == "calibration-only"
    assert pilot.calibration is not None
    assert pilot.calibration.complete
    assert pilot.state.selection is None
    assert pilot.confirmation is None
    assert pilot.state.comparison_progress is not None
    assert pilot.state.comparison_progress.terminal is None
    assert len(fixture.runner.attempts) == 2
    repeated = fixture.run(calibration_only=True)
    assert repeated.blocks == pilot.blocks
    assert repeated.state.approvals == pilot.state.approvals
    assert len(fixture.runner.attempts) == 2
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.CONFIRMED_IMPROVEMENT
    assert result.state.approvals == pilot.state.approvals
    assert len(fixture.runner.attempts) == 16


def test_calibration_only_refuses_selected_confirmation_without_dispatch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.run()
    with pytest.raises(RunStateError, match="cannot continue selected confirmation"):
        fixture.run(calibration_only=True)
    assert len(fixture.runner.attempts) == 16


def test_confirmation_after_pilot_keeps_the_original_wall_time_allowance(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    pilot = fixture.run(calibration_only=True)
    late = datetime.now(UTC) + timedelta(hours=1)

    def clock() -> datetime:
        nonlocal late
        late += timedelta(microseconds=1)
        return late

    result = fixture.run(clock=clock)
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "wall-time-exhausted"
    assert result.state.approvals == pilot.state.approvals
    assert len(fixture.runner.attempts) == 2
    assert result.state.comparison_progress is not None
    assert result.state.comparison_progress.terminal is ComparisonOutcome.INCOMPLETE
    assert result.state.comparison_progress.stopped_at is not None
    assert fixture.run(clock=clock).blocks == result.blocks
    assert len(fixture.runner.attempts) == 2


@pytest.mark.parametrize("dispatched", [False, True])
def test_selected_confirmation_expiry_is_terminal_without_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatched: bool
) -> None:
    fixture = _fixture(tmp_path)
    collect = comparison_engine._collect_sampling_block_owned

    async def interrupt_before_confirmation(**kwargs: Any):
        if kwargs["plan"].role == "confirmation":
            raise KeyboardInterrupt
        return await collect(**kwargs)

    def interrupt_partial(block: SamplingBlockEvidence) -> None:
        if block.plan.role == "confirmation" and len(block.attempts) == 1:
            raise KeyboardInterrupt

    if dispatched:
        with pytest.raises(KeyboardInterrupt):
            fixture.run(on_progress=interrupt_partial)
    else:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                comparison_engine, "_collect_sampling_block_owned", interrupt_before_confirmation
            )
            with pytest.raises(KeyboardInterrupt):
                fixture.run()
    with fixture.state.acquire_lock(fixture.store):
        state = fixture.state.load(fixture.store)
    assert state.selection is not None
    assert state.pending is None
    count = len(fixture.runner.attempts)
    expired = state.selection.plan.expires_at + timedelta(seconds=1)
    result = fixture.run(clock=lambda: expired)
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "freshness-expired"
    assert result.state.selection == state.selection
    assert result.state.comparison_progress is not None
    assert result.state.comparison_progress.terminal is ComparisonOutcome.INCOMPLETE
    assert result.state.comparison_progress.stopped_at == expired
    assert len(fixture.runner.attempts) == count
    assert fixture.run(clock=lambda: expired).blocks == result.blocks
    assert len(fixture.runner.attempts) == count
    destination = tmp_path / "evidence" / "expired.json"
    write_comparison_result(destination, result)
    assert load_comparison_result(destination, fixture.evidence) == result
    payload = result.model_dump(mode="json")
    payload["state"]["comparison_progress"]["stopped_at"] = (
        state.selection.plan.expires_at - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="terminal incompleteness"):
        ComparisonEvidence.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("declared", [True, False])
def test_declared_replay_mismatch_refuses_before_candidate_admission(
    tmp_path: Path, declared: bool
) -> None:
    from dataclasses import replace

    fixture = _fixture(tmp_path)
    replay = CodexReplayConfig(provider="openai", model="declared-model")
    configuration = fixture.configuration.model_copy(
        update={"codex_replay": replay if declared else None}
    )
    prepared = fixture.prepared
    if not declared:
        prepared = replace(
            prepared,
            execution_plan=prepared.execution_plan.model_copy(update={"codex_replay": replay}),
        )
    admitted = len(fixture.runner.admissions)
    with pytest.raises(ValueError, match="replay differs"):
        prepare_comparison(fixture.evidence, prepared, configuration)
    assert len(fixture.runner.admissions) == admitted
    assert fixture.runner.attempts == []


def test_calibration_does_not_supply_confirmation_credit(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.runner.lose_effect = True
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCONCLUSIVE
    assert result.difference == 0
    assert result.lower_bound is not None
    assert result.lower_bound < 0
    assert result.confirmation is not None
    assert result.confirmation.original_counts.matches == 7
    assert result.confirmation.candidate_counts.matches == 7


def test_explicit_text_intervention_is_bound_and_reconstructed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, objective="degradation", append=True)
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.CONFIRMED_DEGRADATION
    assert result.binding.reference.mutation.edits == ()
    assert len(result.binding.candidate.mutation.edits) == 1
    candidate_attempts = [a for a in fixture.runner.attempts if a.arm == "candidate"]
    assert len(candidate_attempts) == 8
    assert all(a.candidate == result.binding.candidate for a in candidate_attempts)
    result.validate_for_case(fixture.evidence)


def test_recorded_runner_error_is_not_a_behavioral_loss(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.runner.fail = True
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.confirmation is None
    assert result.calibration is not None
    assert (
        sum(
            count.matches + count.no_matches
            for count in (result.calibration.original_counts, result.calibration.candidate_counts)
        )
        == 0
    )
    assert len(result.calibration.attempts) == len(fixture.runner.attempts) == 1
    assert result.usage.actual_provider_calls == 0
    assert result.usage.possible_provider_calls == result.usage.unknown_provider_attempts == 1


def test_comparison_model_drift_stops_without_behavioral_sample_credit(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.runner.model_drift = True
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.confirmation is None
    assert result.calibration is not None
    assert len(result.calibration.attempts) == 1
    observation = result.calibration.attempts[0].observation
    assert not observation.valid
    assert observation.attempt_evidence.reason_code == "model-mismatch"
    assert (
        observation.attempt_evidence.model_requested
        == fixture.prepared.execution_plan.model_requested
    )
    assert observation.attempt_evidence.model_returned == "different-model"
    assert observation.attempt_evidence.accounting is not None
    assert (
        result.calibration.original_counts.valid == result.calibration.candidate_counts.valid == 0
    )


def test_large_comparison_bindings_refuse_before_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(comparison_engine, "MAX_COMPARISON_RESULT_BYTES", 1024)
    with pytest.raises(comparison_engine.ComparisonCapacityError, match="storage capacity"):
        prepare_comparison(fixture.evidence, fixture.prepared, fixture.configuration)
    assert fixture.runner.attempts == []


def test_partial_confirmation_resumes_without_replaying_samples(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def interrupt(block: SamplingBlockEvidence) -> None:
        if block.plan.role == "confirmation" and len(block.attempts) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        fixture.run(on_progress=interrupt)
    assert len(fixture.runner.attempts) == 5
    identities = {attempt.attempt_id for attempt in fixture.runner.attempts}
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.CONFIRMED_IMPROVEMENT
    assert len(fixture.runner.attempts) == 16
    assert len({attempt.attempt_id for attempt in fixture.runner.attempts}) == 16
    assert identities <= {attempt.attempt_id for attempt in fixture.runner.attempts}


def test_completed_confirmation_classifies_after_interrupted_final_reconcile(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def interrupt(block: SamplingBlockEvidence) -> None:
        if block.plan.role == "confirmation" and block.complete:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        fixture.run(on_progress=interrupt)
    assert len(fixture.runner.attempts) == 16
    late = datetime.now(UTC) + timedelta(days=1)
    result = fixture.run(clock=lambda: late)
    assert result.outcome is ComparisonOutcome.CONFIRMED_IMPROVEMENT
    assert len(fixture.runner.attempts) == 16
    assert result.recorded_at == late


def test_cancelled_runner_persists_eligibility_refusal_and_interrupts_invocation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.runner.cancel = True
    with pytest.raises(asyncio.CancelledError):
        fixture.run()
    fixture.runner.cancel = False
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "ineligible-exclusion"
    assert result.calibration is not None
    assert len(result.calibration.attempts) == 1
    counts = result.calibration.original_counts, result.calibration.candidate_counts
    assert sum(count.valid for count in counts) == 0
    assert result.usage.charged_provider_calls == 0
    assert result.usage.unknown_runner_attempts == 1
    assert len(fixture.runner.attempts) == 1


@pytest.mark.parametrize("tamper", ["outcome", "policy", "selection"])
def test_private_offline_evidence_recomputes_and_rejects_tampering(
    tmp_path: Path, tamper: str
) -> None:
    fixture = _fixture(tmp_path, both_nonfull=True)
    result = fixture.run()
    destination = tmp_path / "evidence" / "comparison.json"
    write_comparison_result(destination, result)
    assert destination.stat().st_mode & 0o777 == 0o600
    loaded = load_comparison_result(destination, case_evidence=fixture.evidence)
    assert loaded == result
    assert loaded.outcome is ComparisonOutcome.CONFIRMED_IMPROVEMENT
    assert loaded.usage == result.usage
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_comparison_result(destination, result)
    payload = json.loads(destination.read_text())
    if tamper == "outcome":
        payload["state"]["comparison_progress"]["terminal"] = "confirmed-degradation"
    elif tamper == "policy":
        payload["state"]["comparison_progress"]["binding"]["procedure"]["minimum_effect"] = 0.0
    else:
        payload["state"]["selection"]["checkpoint_digest"] = "0" * 64
    destination.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="cannot load private comparison evidence"):
        load_comparison_result(destination)


def test_fixed_trigger_and_foreign_nodes_refuse_before_dispatch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    last = next(node.id for node in fixture.evidence.context.nodes if node.path == "/input/6")
    for removed in ((last,), ("foreign-context-node",)):
        configuration = fixture.configuration.model_copy(
            update={"candidate": ContextSelection(removed_context_ids=removed)}
        )
        with pytest.raises((MutationError, ValueError)):
            prepare_comparison(fixture.evidence, fixture.prepared, configuration)
    assert fixture.runner.attempts == []


def test_dependency_closed_no_op_refuses_before_dispatch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    pair = tuple(node.id for node in fixture.evidence.context.nodes[:2])
    configuration = fixture.configuration.model_copy(
        update={
            "reference": ContextSelection(removed_context_ids=pair),
            "candidate": ContextSelection(removed_context_ids=tuple(reversed(pair))),
        }
    )
    with pytest.raises(ValueError, match="same candidate"):
        prepare_comparison(fixture.evidence, fixture.prepared, configuration)
    assert fixture.runner.attempts == []


def test_calibration_admission_preserves_the_whole_confirmation_reserve(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, wall_seconds=15)
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.calibration is None
    assert result.confirmation is None
    assert result.stop_reason == "wall-time-budget"
    assert fixture.runner.attempts == []


@pytest.mark.parametrize(("freshness", "pacing"), [(14, 0), (40, 3)])
def test_unfinishable_confirmation_refuses_before_calibration(
    tmp_path: Path, freshness: int, pacing: int
) -> None:
    fixture = _fixture(tmp_path, freshness_seconds=freshness, pacing_seconds=pacing)
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "sampling-freshness-too-small"
    assert result.blocks == ()
    assert fixture.runner.attempts == []


def test_prepared_pair_reuses_admission_and_direct_execution_still_admits(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    assert len(fixture.runner.admissions) == 2
    fixture.run()
    fixture.run()
    assert len(fixture.runner.admissions) == 2
    asyncio.run(
        run_comparison(
            evidence=fixture.evidence,
            prepared=fixture.prepared,
            configuration=fixture.configuration,
            state_file=fixture.state,
            store=fixture.store,
        )
    )
    assert len(fixture.runner.admissions) == 3
    assert len(fixture.runner.attempts) == 16


def test_prepared_pair_refuses_a_changed_runtime_before_dispatch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.runner.final_text = "CHANGED"
    with pytest.raises(ValueError, match="admission differs"):
        fixture.run()
    assert fixture.runner.attempts == []


def test_unresolved_dispatch_is_charged_and_never_replayed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.runner.power_loss = True
    with pytest.raises(SystemExit, match="simulated process loss"):
        fixture.run()
    fixture.runner.power_loss = False
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "unresolved-dispatch"
    assert result.state.pending is not None
    assert result.state.comparison_progress is not None
    assert result.state.comparison_progress.terminal is None
    assert result.usage.pending_runner_seconds == 1
    assert len(fixture.runner.attempts) == 1
    assert fixture.run().state.pending == result.state.pending
    assert len(fixture.runner.attempts) == 1


@pytest.fixture(scope="module")
def completed_evidence(tmp_path_factory: pytest.TempPathFactory) -> ComparisonEvidence:
    return _fixture(tmp_path_factory.mktemp("comparison-evidence")).run()


@pytest.mark.parametrize(
    "defect",
    [
        "boolean-schema",
        "naive-time",
        "early-time",
        "metadata",
        "missing-block",
        "reference",
        "effective-policy",
        "refusal",
    ],
)
def test_offline_result_rejects_inconsistent_persisted_evidence(
    tmp_path: Path, completed_evidence: ComparisonEvidence, defect: str
) -> None:
    payload = completed_evidence.model_dump(mode="json")
    if defect == "boolean-schema":
        payload["schema_version"] = True
    elif defect == "naive-time":
        payload["recorded_at"] = "2026-01-01T00:00:00"
    elif defect == "early-time":
        payload["recorded_at"] = "2000-01-01T00:00:00Z"
    elif defect == "metadata":
        payload["execution_metadata"]["plan_content_digest"] = "0" * 64
    elif defect == "missing-block":
        payload["blocks"] = payload["blocks"][:-1]
    elif defect == "reference":
        payload["state"]["blocks"][0]["observations"][0]["observation_id"] = "different-observation"
    elif defect == "effective-policy":
        payload["blocks"][0]["attempts"][0]["observation"]["attempt_evidence"][
            "effective_policy_digest"
        ] = "0" * 64
    else:
        payload["refusal"] = "unresolved-dispatch"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot load private comparison evidence"):
        load_comparison_result(path)


def test_historical_inspection_survives_implementation_change_but_resume_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    result = fixture.run()
    path = tmp_path / "evidence" / "comparison.json"
    write_comparison_result(path, result)
    monkeypatch.setattr(comparison_engine, "comparison_implementation_digest", lambda: "f" * 64)
    assert load_comparison_result(path, fixture.evidence) == result
    with pytest.raises(RunStateError, match="frozen source and purpose"):
        fixture.run()
    assert len(fixture.runner.attempts) == 16


@pytest.mark.parametrize("phase", ["calibration", "confirmation"])
@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_exhausted_eligible_comparison_is_terminal_and_never_redispatched(
    tmp_path: Path, phase: str, harness: Literal["codex", "copilot"]
) -> None:
    runner = OverloadRunner(phase, harness)
    fixture = _fixture(tmp_path, runner=runner)
    result = fixture.run()
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "attempt-limit"
    progress = result.state.comparison_progress
    assert progress is not None
    assert progress.terminal is ComparisonOutcome.INCOMPLETE
    block = result.blocks[-1]
    assert block.plan.role == phase
    assert block.eligible
    assert not block.complete
    assert block.next_schedule_position is None
    assert sum(a.observation.safe_provider_overload for a in block.attempts) == 1
    assert result.usage.actual_provider_calls == len(runner.attempts)
    assert result.usage.pending_provider_calls == 0
    assert result.usage.unknown_token_attempts >= 1
    report = result_report(result)
    assert report.next_command is None
    assert report.next_action is not None
    assert "new" in report.next_action.lower()
    count = len(runner.attempts)
    resumed = fixture.run()
    assert resumed.outcome is ComparisonOutcome.INCOMPLETE
    assert resumed.stop_reason == "attempt-limit"
    assert resumed.blocks == result.blocks
    assert len(runner.attempts) == count
    path = tmp_path / "evidence" / "comparison.json"
    write_comparison_result(path, result)
    assert load_comparison_result(path, fixture.evidence) == result
    # Terminal exhaustion is invalid if a frozen slot remains available.
    payload = result.model_dump(mode="json")
    payload["blocks"][-1]["attempts"].pop()
    payload["state"]["blocks"][-1]["observations"].pop()
    with pytest.raises(ValueError, match="terminal incompleteness"):
        ComparisonEvidence.model_validate_json(json.dumps(payload))


def test_exhaustion_wins_when_freshness_expires_after_the_final_attempt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, runner=OverloadRunner("calibration", "copilot"))
    expired = False

    def progress(block: SamplingBlockEvidence) -> None:
        nonlocal expired
        if block.attempts and not block.complete and block.next_schedule_position is None:
            expired = True

    def clock() -> datetime:
        now = datetime.now(UTC)
        return now + timedelta(hours=1) if expired else now

    result = fixture.run(clock=clock, on_progress=progress)
    assert result.outcome is ComparisonOutcome.INCOMPLETE
    assert result.stop_reason == "attempt-limit"
    assert result.state.comparison_progress is not None
    assert result.state.comparison_progress.stop_reason == "attempt-limit"
    count = len(fixture.runner.attempts)
    assert fixture.run().blocks == result.blocks
    assert len(fixture.runner.attempts) == count
