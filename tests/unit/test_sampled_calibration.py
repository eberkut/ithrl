"""Credential-free sampled calibration contract tests."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import ithrl.calibration as calibration_module
from ithrl.calibration import (
    CalibrationBusyError,
    CalibrationInFlightError,
    SampledCalibrationResult,
    run_sampled_calibration,
    save_sampled_calibration_result,
)
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli.shrink import prepare_shrink
from ithrl.execution import CapabilityProfile
from ithrl.experiment import (
    ExperimentDefinition,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
)
from ithrl.observations import ObservationArm, ObservationOutcome, ObservationRole, ObservationStore
from ithrl.runner import (
    AttemptEvidence,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    RunnerRegistry,
    RunResult,
    ToolCall,
    prepare_candidate,
)
from ithrl.synthetic import extract_inference
from ithrl.workflow import PreflightRefusedError, WorkflowError, prepare_experiment, run_phases

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


class SequenceRunner:
    def __init__(
        self, outcomes: tuple[bool | RunnerErrorCode, ...], *, tool_name: str, command: str | None
    ) -> None:
        self.outcomes = outcomes
        self.tool_name = tool_name
        self.command = command
        self.calls = 0
        self._profile = CapabilityProfile.deterministic_fake().model_copy(
            update={"runner_type": "codex", "deterministic": False}
        )

    @property
    def capability_profile(self) -> CapabilityProfile:
        return self._profile

    @property
    def fingerprint(self) -> str:
        return "credential-free-sampled-v1"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, RunnerErrorCode):
            error = RunnerError(
                outcome,
                "sanitized simulated failure",
                provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                accounting=RunAccounting(),
                effective_policy_digest=attempt.safety_policy.digest,
            )
            if outcome is RunnerErrorCode.CANCELLED:
                raise RunnerCancelled(error)
            raise error
        tool_calls = (
            (ToolCall(name=self.tool_name, arguments={"command": self.command or ""}),)
            if outcome
            else ()
        )
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            tool_calls=tool_calls,
            final_text="sampled response",
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            provider="fake",
            accounting=RunAccounting(
                actions_requested=len(tool_calls), actions_denied=len(tool_calls)
            ),
        )


def _prepared(
    tmp_path: Path,
    *,
    outcomes: tuple[bool | RunnerErrorCode, ...],
    required_valid: int,
    minimum_matches: int,
    max_attempts: int,
):
    evidence = extract_inference(
        FIXTURE,
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    deterministic = ExperimentDefinition.from_toml(evidence.default_experiment)
    assert isinstance(deterministic.runner, FakeRunnerConfig)
    assert deterministic.runner.tool_name is not None
    runner = SequenceRunner(
        outcomes,
        tool_name=deterministic.runner.tool_name,
        command=deterministic.runner.command,
    )
    sampled = SampledExperimentDefinition(
        schema_version=1,
        target=deterministic.target,
        oracle=deterministic.oracle,
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=SampledCalibrationPhase(
            required_valid=required_valid,
            minimum_matches=minimum_matches,
            max_attempts=max_attempts,
        ),
    )
    case = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=evidence.context,
        default_experiment=sampled.to_toml(),
        decision=evidence.decision,
    )
    prepared = prepare_experiment(
        case, registry=RunnerRegistry({"codex": lambda _config, _evidence: runner})
    )
    return case, prepared, runner


def test_sampled_calibration_collects_exact_valid_block_and_reuses_it(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(True, False, True),
        required_valid=3,
        minimum_matches=2,
        max_attempts=4,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")

    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    again = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    assert result.outcome == "measurable"
    assert result.stop_reason == "valid-sample-limit"
    assert result.matches == 2
    assert result.no_matches == 1
    assert [item.attempt for item in result.observations] == [1, 2, 3]
    assert again == result
    assert runner.calls == 3


def test_shrink_preparation_accepts_an_injected_sampled_runner_registry(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(),
        required_valid=1,
        minimum_matches=1,
        max_attempts=2,
    )
    workspace = CaseWorkspace.create(
        tmp_path / "sampled-case",
        manifest=case.manifest,
        request=case.request,
        nodes=case.context.nodes,
        default_experiment=case.default_experiment,
        decision=case.decision,
    )

    loaded_workspace, loaded_case, selected, previous, checkpoint, run_state, _limit = (
        prepare_shrink(
            source=workspace.root,
            resume=False,
            max_candidates=None,
            registry=RunnerRegistry({"codex": lambda _config, _evidence: runner}),
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
        )
    )

    assert loaded_workspace.root == workspace.root
    assert loaded_case.digest == case.digest
    assert selected.identity == prepared.identity
    assert previous is checkpoint is run_state is None
    assert runner.calls == 0


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [((False, False, False), "not-measurable"), ((True, False, False), "inconclusive")],
)
def test_sampled_calibration_distinguishes_nonmatching_and_inconclusive_blocks(
    tmp_path: Path, outcomes: tuple[bool, ...], expected: str
) -> None:
    case, prepared, _runner = _prepared(
        tmp_path,
        outcomes=outcomes,
        required_valid=3,
        minimum_matches=2,
        max_attempts=3,
    )

    result = run_sampled_calibration(
        evidence=case, prepared=prepared, store=ObservationStore(tmp_path / "observations.sqlite")
    )

    assert result.outcome == expected


def test_retry_safe_operational_error_is_persisted_then_replaced(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.PREPARATION, True),
        required_valid=1,
        minimum_matches=1,
        max_attempts=2,
    )

    result = run_sampled_calibration(
        evidence=case, prepared=prepared, store=ObservationStore(tmp_path / "observations.sqlite")
    )

    assert result.outcome == "measurable"
    assert [item.outcome for item in result.observations] == [
        ObservationOutcome.RUNNER_ERROR,
        ObservationOutcome.MATCH,
    ]
    assert result.observations[0].attempt_evidence.automatic_retry_safe
    assert runner.calls == 2


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_safety_failure_refuses_without_a_second_attempt(tmp_path: Path, max_attempts: int) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.SAFETY,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=max_attempts,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")

    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    again = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    assert result.outcome == "refused"
    assert result.stop_reason == "safety-refusal"
    assert again == result
    assert runner.calls == 1


def test_safety_classified_invalid_observation_can_be_saved(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    candidate = prepare_candidate(case, prepared.execution_plan)
    store.record_attempt(
        identity=prepared.identity,
        candidate_digest=candidate.digest,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
        outcome=ObservationOutcome.INVALID_OBSERVATION,
        error_category="safety",
        attempt_evidence=AttemptEvidence(
            attempt_id="attempt-" + "0" * 32,
            attempt_digest="0" * 64,
            termination_reason="failed",
            provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
            effective_policy_digest=prepared.safety_policy.digest,
            accounting=RunAccounting(),
        ),
    )

    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    snapshot = save_sampled_calibration_result(
        workspace=CaseWorkspace(tmp_path / "case"),
        evidence=case,
        prepared=prepared,
        store=store,
        result=result,
    )

    assert runner.calls == 0
    assert result.outcome == "refused"
    assert snapshot.summary.stop_reason == "safety-refusal"
    assert snapshot.summary.terminal_error_category == "safety"


@pytest.mark.parametrize(
    ("acceptance", "possible", "started"),
    [
        (ProviderAcceptanceState.UNKNOWN, 1, 0),
        (ProviderAcceptanceState.ACCEPTED, 0, 1),
    ],
)
def test_over_plan_provider_evidence_is_saved_as_safety_refusal(
    tmp_path: Path,
    acceptance: ProviderAcceptanceState,
    possible: int,
    started: int,
) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    assert prepared.execution_plan.capability.provider_calls_per_attempt == 0
    store = ObservationStore(tmp_path / "observations.sqlite")
    candidate = prepare_candidate(case, prepared.execution_plan)
    store.record_attempt(
        identity=prepared.identity,
        candidate_digest=candidate.digest,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
        outcome=ObservationOutcome.RUNNER_ERROR,
        error_category="safety",
        attempt_evidence=AttemptEvidence(
            attempt_id="attempt-" + "0" * 32,
            attempt_digest="0" * 64,
            termination_reason="failed",
            provider_acceptance_state=acceptance,
            provider_calls_possible=possible,
            effective_policy_digest=prepared.safety_policy.digest,
            accounting=RunAccounting(provider_calls_started=started),
        ),
    )

    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    snapshot = save_sampled_calibration_result(
        workspace=CaseWorkspace(tmp_path / "case"),
        evidence=case,
        prepared=prepared,
        store=store,
        result=result,
    )

    assert runner.calls == 0
    assert result.outcome == "refused"
    assert snapshot.summary.provider_calls_may_have_started == 1
    assert snapshot.summary.terminal_error_category == "safety"


def test_invalid_observation_is_recorded_but_does_not_count_as_behavior_loss(
    tmp_path: Path,
) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=2,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    candidate = prepare_candidate(case, prepared.execution_plan)
    store.record_attempt(
        identity=prepared.identity,
        candidate_digest=candidate.digest,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
        outcome=ObservationOutcome.INVALID_OBSERVATION,
        error_category="oracle-incomplete",
        attempt_evidence=AttemptEvidence(
            attempt_id="attempt-" + "0" * 32,
            attempt_digest="0" * 64,
            termination_reason="completed",
            provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
            effective_policy_digest=prepared.safety_policy.digest,
            accounting=RunAccounting(),
        ),
    )

    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    assert result.outcome == "measurable"
    assert [item.outcome for item in result.observations] == [
        ObservationOutcome.INVALID_OBSERVATION,
        ObservationOutcome.MATCH,
    ]
    assert runner.calls == 1


def test_sampled_experiment_cannot_enter_deterministic_search(tmp_path: Path) -> None:
    case, prepared, _runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    with pytest.raises(WorkflowError, match="cannot enter deterministic search"):
        run_phases(
            evidence=case,
            prepared=prepared,
            store=ObservationStore(tmp_path / "observations.sqlite"),
            reducer=None,  # type: ignore[arg-type] -- the guard runs before dereferencing it.
            calibrate_only=False,
        )


def test_sampled_preflight_refuses_a_changed_capability_profile(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    runner._profile = runner.capability_profile.model_copy(update={"runner_version": "changed"})

    with pytest.raises(PreflightRefusedError) as refused:
        prepare_experiment(
            case, registry=RunnerRegistry({"codex": lambda _config, _evidence: runner})
        )

    assert {issue.code for issue in refused.value.report.issues} == {"capability-profile-mismatch"}
    assert prepared.execution_plan.capability.digest != runner.capability_profile.digest


def test_cancelled_attempt_is_saved_and_calibration_remains_incomplete(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(RunnerErrorCode.CANCELLED,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=2,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")

    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    assert result.outcome == "incomplete"
    assert result.stop_reason == "user-interrupt"
    assert [item.error_category for item in result.observations] == ["cancelled"]
    assert runner.calls == 1


def test_interruption_after_final_observation_keeps_completed_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, prepared, _runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    original = calibration_module.observe_once

    async def interrupted_after_record(**kwargs: Any) -> None:
        await original(**kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(calibration_module, "observe_once", interrupted_after_record)
    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    assert result.outcome == "measurable"
    assert result.stop_reason == "valid-sample-limit"
    assert len(result.observations) == 1
    snapshot = save_sampled_calibration_result(
        workspace=CaseWorkspace(tmp_path / "case"),
        evidence=case,
        prepared=prepared,
        store=store,
        result=result,
    )
    assert snapshot.summary.outcome == "measurable"
    assert snapshot.summary.stop_reason == "valid-sample-limit"


def test_concurrent_calibration_cannot_start_an_extra_attempt(tmp_path: Path) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    entered = Event()
    release = Event()
    original = runner.run

    async def blocking_run(attempt: PreparedAttempt) -> RunResult:
        entered.set()
        if not await asyncio.to_thread(release.wait, 10):
            raise RuntimeError("test runner was not released")
        return await original(attempt)

    runner.run = blocking_run
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run_sampled_calibration, evidence=case, prepared=prepared, store=store)
        assert entered.wait(5)
        try:
            with pytest.raises(CalibrationBusyError, match="another calibration"):
                run_sampled_calibration(evidence=case, prepared=prepared, store=store)
        finally:
            release.set()
        result = first.result(timeout=10)

    assert result.outcome == "measurable"
    assert runner.calls == 1
    assert len(result.observations) == 1


def test_unrecorded_attempt_blocks_automatic_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(True, True),
        required_valid=1,
        minimum_matches=1,
        max_attempts=2,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")

    def fail_append(**_kwargs: Any) -> None:
        raise OSError("simulated observation write failure")

    monkeypatch.setattr(store, "record_attempt", fail_append)
    with pytest.raises(CalibrationInFlightError, match="without a saved observation"):
        run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    assert runner.calls == 1
    assert calibration_module._inflight_path(store).is_file()

    with pytest.raises(CalibrationInFlightError, match="without a saved observation"):
        run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    assert runner.calls == 1


def test_committed_attempt_with_stale_marker_is_reconciled_without_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, prepared, runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    original = calibration_module._reconcile_inflight

    def interrupted_before_marker_clear(_store: ObservationStore) -> None:
        if calibration_module._inflight_path(_store).exists():
            raise RuntimeError("simulated process exit after observation commit")

    monkeypatch.setattr(calibration_module, "_reconcile_inflight", interrupted_before_marker_clear)
    with pytest.raises(RuntimeError, match="after observation commit"):
        run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    assert calibration_module._inflight_path(store).is_file()

    monkeypatch.setattr(calibration_module, "_reconcile_inflight", original)
    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    assert result.outcome == "measurable"
    assert runner.calls == 1
    assert not calibration_module._inflight_path(store).exists()


def test_calibration_snapshot_rejects_claimed_outcome_not_supported_by_rows(tmp_path: Path) -> None:
    case, prepared, _runner = _prepared(
        tmp_path,
        outcomes=(True,),
        required_valid=1,
        minimum_matches=1,
        max_attempts=1,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)
    forged = SampledCalibrationResult("refused", "safety-refusal", result.observations)

    with pytest.raises(ValueError, match="outcome does not match"):
        save_sampled_calibration_result(
            workspace=CaseWorkspace(tmp_path / "case"),
            evidence=case,
            prepared=prepared,
            store=store,
            result=forged,
        )


def test_sampled_calibration_persists_a_private_versioned_snapshot(tmp_path: Path) -> None:
    case, prepared, _runner = _prepared(
        tmp_path,
        outcomes=(True, False, True),
        required_valid=3,
        minimum_matches=2,
        max_attempts=3,
    )
    store = ObservationStore(tmp_path / "observations.sqlite")
    result = run_sampled_calibration(evidence=case, prepared=prepared, store=store)

    snapshot = save_sampled_calibration_result(
        workspace=CaseWorkspace(tmp_path / "case"),
        evidence=case,
        prepared=prepared,
        store=store,
        result=result,
    )

    assert snapshot.summary.schema_version == 1
    assert snapshot.summary.kind == "calibration"
    assert snapshot.summary.outcome == "measurable"
    assert snapshot.summary.matches == 2
    assert snapshot.summary.no_matches == 1
    assert snapshot.summary.calibration_observation_ids == tuple(
        item.observation_id for item in result.observations
    )
    assert snapshot.experiment.digest == prepared.experiment.digest
    assert snapshot.execution_plan.digest == prepared.execution_plan.digest
    assert (snapshot.root / "summary.json").stat().st_mode & 0o777 == 0o600
