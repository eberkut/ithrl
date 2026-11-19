"""Post-run grading preserves native accounting and refuses unsafe resampling."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

import ithrl.execution.preflight as preflight
import ithrl.measurement as measurement
from ithrl.behavior import FinalResponseTarget, ShellActionTarget
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    StructuralOracleConfig,
)
from ithrl.grading.contracts import GradeEvidence, PatchGradingConfig, SuiteEvidence
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import ObservationOutcome, ObservationStore
from ithrl.runner import (
    AttemptEvidence,
    PreparedAttempt,
    RunAccounting,
    RunnerRegistry,
    RunResult,
    prepare_candidate,
)
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


def _suites() -> tuple[SuiteEvidence, ...]:
    roles: tuple[Literal["source", "independent"], ...] = ("source", "independent")
    return tuple(
        SuiteEvidence(role=role, expected=1, collected=1, passed=1, failed=0, errors=0)
        for role in roles
    )


def _evidence(
    config: PatchGradingConfig,
    response: str | None,
    *,
    candidate_digest: str,
    attempt_id: str,
    status: Literal["pass", "fail", "error"] = "pass",
    reason_code: str | None = None,
) -> GradeEvidence:
    return GradeEvidence(
        config_digest=config.digest,
        candidate_digest=candidate_digest,
        response_digest=hashlib.sha256((response or "").encode()).hexdigest(),
        attempt_id=attempt_id,
        status=status,
        reason_code=reason_code,
        elapsed_milliseconds=1,
        suites=_suites() if status == "pass" else (),
    )


def _config(tmp_path: Path) -> PatchGradingConfig:
    return PatchGradingConfig(
        bundle_path=str(tmp_path / "task"),
        bundle_digest="a" * 64,
        environment_path=str(tmp_path / "env"),
        environment_digest="b" * 64,
        timeout_seconds=1,
    )


class RecordedRunner:
    """Return synthetic accounting; this class never starts a provider or process."""

    fingerprint = "grading-unit-test"

    def __init__(self) -> None:
        self.calls = 0
        self.runtime_deadline: float | None = None
        self.capability_profile = CapabilityProfile.model_validate(
            {
                **CapabilityProfile.deterministic_fake().model_dump(),
                "runner_type": "codex",
                "deterministic": False,
                "provider_transport": "required",
                "provider_calls_per_attempt": 1,
                "provider": "fixture",
                "endpoint": "https://example.invalid/inference",
            }
        )

    async def run(
        self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        self.calls += 1
        self.runtime_deadline = runtime_deadline
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            final_text="private patch proposal",
            provider="fixture",
            endpoint="https://example.invalid/inference",
            model_requested=attempt.execution_plan.model_requested,
            accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
        )


def _attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def validate(_input: object, *, timeout_seconds: float | None = None) -> None:
        return None

    monkeypatch.setattr(preflight, "validate_grading", validate)
    monkeypatch.setattr(preflight, "validate_grading_runtime", validate)
    monkeypatch.setattr(measurement, "validate_grading", validate)
    monkeypatch.setattr(measurement, "validate_grading_runtime", validate)
    evidence = extract_inference(
        FIXTURE, inference_call_id="synthetic-ac-001", case_path=tmp_path / "case"
    ).load()
    runner = RecordedRunner()
    experiment = SampledExperimentDefinition(
        target=FinalResponseTarget(),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
        grading=_config(tmp_path),
    )
    prepared = prepare_experiment(
        evidence,
        experiment=experiment,
        registry=RunnerRegistry({"codex": lambda _config, _evidence: runner}),
        resource_limits=ResourceLimits(max_provider_calls=1),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider="fixture",
            allowed_endpoint="https://example.invalid/inference",
        ),
    )
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=prepare_candidate(evidence, prepared.execution_plan),
        target=experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )
    return runner, prepared, attempt


@pytest.mark.parametrize("status", ["pass", "fail", "error"])
def test_grades_keep_completed_native_evidence_and_control_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: Literal["pass", "fail", "error"]
) -> None:
    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)

    async def grade(
        config: PatchGradingConfig,
        response: str | None,
        *,
        candidate_digest: str,
        attempt_id: str,
        timeout_seconds: float | None = None,
    ) -> GradeEvidence:
        return _evidence(
            config,
            response,
            candidate_digest=candidate_digest,
            attempt_id=attempt_id,
            status=status,
            reason_code="verification-failed" if status == "error" else None,
        )

    monkeypatch.setattr(measurement, "grade_response", grade)
    store = ObservationStore(tmp_path / "observations.sqlite")

    def observe():
        return asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt)
        )

    if status == "error":
        with pytest.raises(RunnerExecutionError, match="task grading failed"):
            observe()
        with pytest.raises(RunnerExecutionError, match="automatic retry is disabled"):
            observe()
    else:
        observe()
    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert runner.calls == 1
    assert (
        observation.outcome
        is {
            "pass": ObservationOutcome.MATCH,
            "fail": ObservationOutcome.NO_MATCH,
            "error": ObservationOutcome.INVALID_OBSERVATION,
        }[status]
    )
    assert observation.attempt_evidence.provider_call_charge(1) == 1
    assert observation.attempt_evidence.termination_reason == "completed"
    assert observation.attempt_evidence.grading is not None
    assert observation.attempt_evidence.grading.status == status
    assert observation.valid is (status != "error")


@pytest.mark.parametrize("interruption", ["cancel", "timeout", "exception", "wrong-identity"])
def test_interrupted_grading_retains_native_result_without_automatic_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)

    async def grade(
        config: PatchGradingConfig,
        response: str | None,
        *,
        candidate_digest: str,
        attempt_id: str,
        timeout_seconds: float | None = None,
    ) -> GradeEvidence:
        if interruption == "cancel":
            raise asyncio.CancelledError
        if interruption == "timeout":
            raise TimeoutError
        if interruption == "exception":
            raise ValueError("private verifier content")
        return _evidence(
            config,
            response,
            candidate_digest=candidate_digest,
            attempt_id=attempt_id,
        ).model_copy(update={"config_digest": "f" * 64})

    monkeypatch.setattr(measurement, "grade_response", grade)
    store = ObservationStore(tmp_path / "observations.sqlite")
    error = asyncio.CancelledError if interruption == "cancel" else RunnerExecutionError
    with pytest.raises(error):
        asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt)
        )
    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert observation.outcome is ObservationOutcome.INVALID_OBSERVATION
    assert observation.attempt_evidence.provider_call_charge(1) == 1
    assert not observation.allows_fresh_attempt("independent-safe-preaccept-only")
    assert not observation.allows_fresh_attempt("safe-provider-overload")
    assert runner.calls == 1
    assert "private" not in observation.model_dump_json()


def test_grader_identity_changes_experiment_and_execution_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    experiment = prepared.experiment
    assert isinstance(experiment, SampledExperimentDefinition)
    assert SampledExperimentDefinition.from_toml(experiment.to_toml()) == experiment
    assert experiment.grading is not None
    changed = experiment.model_copy(
        update={"grading": experiment.grading.model_copy(update={"bundle_digest": "c" * 64})}
    )
    assert changed.digest != experiment.digest
    assert prepared.execution_plan.grading == experiment.grading
    report = preflight.resolve_execution_plan(
        runner.capability_profile,
        changed,
        evidence=attempt.evidence,
        runner_fingerprint=runner.fingerprint,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
    )
    assert report.ready
    assert report.plan is not None
    assert report.plan.digest != prepared.execution_plan.digest


@pytest.mark.parametrize("target", [ShellActionTarget(), FinalResponseTarget(text_contains="OK")])
def test_grading_cannot_silently_mix_task_correctness_with_a_text_predicate(
    tmp_path: Path, target: ShellActionTarget | FinalResponseTarget
) -> None:
    with pytest.raises(ValidationError, match="unconstrained final-response"):
        SampledExperimentDefinition(
            target=target,
            oracle=StructuralOracleConfig(),
            runner=NativeRunnerConfig(type="codex", required_capability_digest="c" * 64),
            calibration=SampledCalibrationPhase(
                required_valid=1, minimum_matches=1, max_attempts=1
            ),
            grading=_config(tmp_path),
        )


def test_changed_grader_refuses_before_native_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)

    def changed(_config):
        raise ValueError("private filename")

    monkeypatch.setattr(measurement, "validate_grading", changed)
    store = ObservationStore(tmp_path / "observations.sqlite")
    with pytest.raises(RunnerExecutionError, match="runner failed with contract") as failure:
        asyncio.run(
            observe_once(
                runner=runner,
                identity=prepared.identity,
                store=store,
                attempt=attempt,
            )
        )
    assert failure.value.observation is not None
    assert failure.value.observation.attempt_evidence.provider_call_charge(1) == 0
    assert failure.value.observation.attempt_evidence.provider_calls_possible == 0
    assert not failure.value.observation.allows_fresh_attempt("independent-safe-preaccept-only")
    assert runner.calls == 0


def test_predispatch_grading_refusal_reconciles_calibration_without_possible_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl.calibration import run_sampled_calibration

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)

    def changed(_config: PatchGradingConfig) -> None:
        raise ValueError("private grading input")

    monkeypatch.setattr(measurement, "validate_grading", changed)
    store = ObservationStore(tmp_path / "calibration.sqlite")
    outcome = run_sampled_calibration(evidence=attempt.evidence, prepared=prepared, store=store)
    assert outcome.outcome == "refused"
    assert not store.path.with_name(f"{store.path.name}.calibration.inflight").exists()
    assert sum(o.attempt_evidence.provider_call_charge(1) for o in outcome.observations) == 0
    resumed = run_sampled_calibration(evidence=attempt.evidence, prepared=prepared, store=store)
    assert resumed == outcome
    assert runner.calls == 0


@pytest.mark.parametrize("seconds", [1, 2, 3, 4])
def test_preflight_reserves_grading_and_cleanup_before_allowing_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: int
) -> None:
    from ithrl.execution import resolve_execution_plan

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    report = resolve_execution_plan(
        runner.capability_profile,
        prepared.experiment,
        evidence=attempt.evidence,
        runner_fingerprint=runner.fingerprint,
        safety_policy=prepared.safety_policy,
        resource_limits=ResourceLimits(max_provider_calls=1, max_wall_time_seconds=seconds),
    )
    assert report.ready is (seconds > 3)
    assert runner.calls == 0
    if not report.ready:
        assert [item.code for item in report.issues] == ["grading-unavailable"]
        raw = prepared.execution_plan.model_dump()
        raw["resource_limits"]["max_wall_time_seconds"] = seconds
        with pytest.raises(ValidationError, match="time within the attempt limit"):
            type(prepared.execution_plan).model_validate(raw)


def test_slow_predispatch_validation_cannot_start_inference_after_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(measurement, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow(_config: PatchGradingConfig) -> None:
        clock[0] += 30

    def probe(_inputs: object, *, timeout_seconds: float) -> None:
        pytest.fail("grader probe started after input validation exhausted the deadline")

    monkeypatch.setattr(measurement, "validate_grading", slow)
    monkeypatch.setattr(measurement, "validate_grading_runtime", probe)
    store = ObservationStore(tmp_path / "slow-preparation.sqlite")
    with pytest.raises(RunnerExecutionError) as stopped:
        asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt)
        )
    assert runner.calls == 0
    observation = stopped.value.observation
    assert observation is not None
    assert observation.error_category == "timeout"
    assert observation.attempt_evidence.reason_code == "grading-preparation-timeout"
    assert observation.attempt_evidence.provider_call_charge(1) == 0


def test_predispatch_probe_uses_remaining_preparation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(measurement, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow(_config: PatchGradingConfig) -> None:
        clock[0] += 26.75

    def probe(_inputs: object, *, timeout_seconds: float) -> None:
        assert timeout_seconds == 0.25
        clock[0] += timeout_seconds

    monkeypatch.setattr(measurement, "validate_grading", slow)
    monkeypatch.setattr(measurement, "validate_grading_runtime", probe)
    store = ObservationStore(tmp_path / "slow-probe.sqlite")
    with pytest.raises(RunnerExecutionError) as stopped:
        asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt)
        )
    assert clock[0] == 27.0
    assert runner.calls == 0
    observation = stopped.value.observation
    assert observation is not None
    assert observation.error_category == "timeout"
    assert observation.attempt_evidence.reason_code == "grading-preparation-timeout"
    assert observation.attempt_evidence.provider_call_charge(1) == 0


def test_native_deadline_uses_only_the_allowance_left_after_preparation_and_grading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(measurement, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def slow(_config: PatchGradingConfig) -> None:
        clock[0] += 5

    async def grade(
        config: PatchGradingConfig,
        response: str | None,
        *,
        candidate_digest: str,
        attempt_id: str,
        timeout_seconds: float | None = None,
    ) -> GradeEvidence:
        return _evidence(
            config,
            response,
            candidate_digest=candidate_digest,
            attempt_id=attempt_id,
        )

    monkeypatch.setattr(measurement, "validate_grading", slow)
    monkeypatch.setattr(measurement, "grade_response", grade)
    observed = asyncio.run(
        observe_once(
            runner=runner,
            identity=prepared.identity,
            store=ObservationStore(tmp_path / "bounded-native.sqlite"),
            attempt=attempt,
        )
    )
    assert observed.valid
    assert runner.runtime_deadline == 27.0  # 30 seconds minus 1 grading and 2 cleanup.
    assert clock[0] == 5.0  # Five seconds spent preparing leave 22 for the native runner.


def test_grading_error_elapsed_time_excludes_native_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(measurement, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = runner.run

    async def native(
        prepared_attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        result = await original(prepared_attempt, runtime_deadline=runtime_deadline)
        clock[0] += 5
        return result

    async def grade(*_args: object, **_kwargs: object) -> GradeEvidence:
        clock[0] += 0.25
        raise TimeoutError

    monkeypatch.setattr(runner, "run", native)
    monkeypatch.setattr(measurement, "grade_response", grade)
    with pytest.raises(RunnerExecutionError) as failure:
        asyncio.run(
            observe_once(
                runner=runner,
                identity=prepared.identity,
                store=ObservationStore(tmp_path / "timing.sqlite"),
                attempt=attempt,
            )
        )
    assert failure.value.observation is not None
    grading = failure.value.observation.attempt_evidence.grading
    assert grading is not None
    assert grading.elapsed_milliseconds == 250


@pytest.mark.parametrize("status", ["pass", "fail", "error"])
def test_late_grades_become_timeouts_without_overwriting_backend_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["pass", "fail", "error"],
) -> None:
    from types import SimpleNamespace

    runner, prepared, attempt = _attempt(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(measurement, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def backend(
        config: PatchGradingConfig,
        response: str | None,
        *,
        candidate_digest: str,
        attempt_id: str,
        **_kwargs: object,
    ) -> GradeEvidence:
        clock[0] = 30.0
        return _evidence(
            config,
            response,
            candidate_digest=candidate_digest,
            attempt_id=attempt_id,
            status=status,
            reason_code="grading-cleanup-failed" if status == "error" else None,
        ).model_copy(update={"report_digest": "e" * 64})

    monkeypatch.setattr(measurement, "grade_response", backend)
    with pytest.raises(RunnerExecutionError) as failed:
        asyncio.run(
            observe_once(
                runner=runner,
                identity=prepared.identity,
                store=ObservationStore(tmp_path / "late-error.sqlite"),
                attempt=attempt,
            )
        )
    assert failed.value.observation is not None
    grade = failed.value.observation.attempt_evidence.grading
    assert grade is not None
    assert grade.status == "error"
    assert grade.reason_code == ("grading-cleanup-failed" if status == "error" else "timeout")
    assert grade.report_digest == ("e" * 64 if status == "error" else None)
    assert runner.calls == 1


def test_grading_helpers_require_a_frozen_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, attempt = _attempt(tmp_path, monkeypatch)
    plan = attempt.execution_plan.model_copy(update={"grading": None})
    attempt = replace(
        attempt,
        execution_plan=plan,
        candidate=prepare_candidate(attempt.evidence, plan),
    )
    result = RunResult(
        attempt_id=attempt.attempt_id,
        effective_policy_digest=attempt.safety_policy.digest,
        accounting=RunAccounting(),
    )
    with pytest.raises(ValueError, match="requires a frozen configuration"):
        measurement._grading_error(attempt, result, "timeout", 0)
    with pytest.raises(ValueError, match="requires a frozen configuration"):
        asyncio.run(measurement._grade_with_remaining_time(attempt, result, 1, 0, "a" * 64))


def test_expired_grading_deadline_preserves_a_completed_attempt_without_running_the_grader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner, _, attempt = _attempt(tmp_path, monkeypatch)
    result = asyncio.run(runner.run(attempt))
    backend = AsyncMock()
    monkeypatch.setattr(measurement, "grade_response", backend)
    monkeypatch.setattr(measurement, "time", SimpleNamespace(monotonic=lambda: 5))
    grade = asyncio.run(
        measurement._grade_with_remaining_time(attempt, result, 5, 5, attempt.candidate.digest)
    )
    assert grade.status == "error"
    assert grade.reason_code == "timeout"
    assert grade.attempt_id == result.attempt_id
    assert grade.elapsed_milliseconds == 0
    backend.assert_not_awaited()


@pytest.mark.parametrize("mismatch", ["incomplete", "attempt-id"])
def test_saved_grades_require_the_matching_completed_native_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    runner, _, attempt = _attempt(tmp_path, monkeypatch)
    result = asyncio.run(runner.run(attempt))
    config = attempt.execution_plan.grading
    assert config is not None
    grade = _evidence(
        config,
        result.final_text,
        candidate_digest=attempt.candidate.digest,
        attempt_id=attempt.attempt_id,
    )
    raw = AttemptEvidence.from_result(attempt, result, grade).model_dump()
    raw.update(
        {"termination_reason": "failed"}
        if mismatch == "incomplete"
        else {"attempt_id": "attempt-" + "f" * 32}
    )
    with pytest.raises(ValidationError, match="matching completed native attempt"):
        AttemptEvidence.model_validate(raw)
