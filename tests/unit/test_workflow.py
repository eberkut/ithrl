from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ithrl.case import CaseEvidence
from ithrl.execution import (
    CapabilityProfile,
    ResourceLimits,
    SafetyPolicy,
    resolve_execution_plan,
)
from ithrl.experiment import ExperimentDefinition
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationRole,
    ObservationStore,
)
from ithrl.runner import (
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    prepare_candidate,
)
from ithrl.synthetic import extract_inference
from ithrl.workflow import OraclePrerequisiteError, calibrate, confirm, prepare_experiment

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"
INFERENCE_ID = "synthetic-ac-001"


def _evidence(tmp_path: Path) -> CaseEvidence:
    return extract_inference(
        FIXTURE,
        inference_call_id=INFERENCE_ID,
        case_path=tmp_path / "case",
    ).load()


def test_prepare_calibrate_and_confirm_use_separate_observation_roles(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    store = ObservationStore(tmp_path / "observations.sqlite")

    assert asyncio.run(calibrate(evidence=evidence, prepared=prepared, store=store)).target_observed
    assert asyncio.run(calibrate(evidence=evidence, prepared=prepared, store=store)).target_observed
    assert asyncio.run(
        confirm(evidence=evidence, removed_context_ids=(), prepared=prepared, store=store)
    ).target_observed
    assert asyncio.run(
        confirm(evidence=evidence, removed_context_ids=(), prepared=prepared, store=store)
    ).target_observed

    observations = store.list_for_experiment(prepared.experiment.digest)
    assert [item.role for item in observations] == [
        ObservationRole.CALIBRATION,
        ObservationRole.CONFIRMATION,
        ObservationRole.CONFIRMATION,
    ]
    assert prepared.execution_plan.capability.provider_transport == "forbidden"
    assert prepared.execution_plan.capability.provider_calls_per_attempt == 0
    assert prepared.execution_plan.capability.action_execution == "denied"


def test_calibration_records_when_the_target_is_absent(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    changed = experiment.model_copy(
        update={
            "runner": experiment.runner.model_copy(update={"required_markers": ("missing-marker",)})
        }
    )
    changed_evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=evidence.context,
        default_experiment=changed.to_toml(),
    )
    prepared = prepare_experiment(changed_evidence)

    observation = asyncio.run(
        calibrate(
            evidence=changed_evidence,
            prepared=prepared,
            store=ObservationStore(tmp_path / "observations.sqlite"),
        )
    )

    assert observation.target_observed is False


def test_concurrent_measurements_keep_both_completed_runner_results(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)

    class ConcurrentRunner:
        def __init__(self) -> None:
            self.calls = 0
            self._both_started = asyncio.Event()

        @property
        def capability_profile(self) -> CapabilityProfile:
            return prepared.runner.capability_profile

        @property
        def fingerprint(self) -> str:
            return prepared.runner.fingerprint

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            self.calls += 1
            if self.calls == 2:
                self._both_started.set()
            await self._both_started.wait()
            return await prepared.runner.run(attempt)

    async def measure_twice() -> tuple[Observation, Observation]:
        runner = ConcurrentRunner()
        store = ObservationStore(tmp_path / "observations.sqlite")
        identity = prepared.identity
        candidate = prepare_candidate(evidence, prepared.execution_plan)

        def attempt() -> PreparedAttempt:
            return PreparedAttempt(
                evidence=evidence,
                candidate=candidate,
                target=prepared.experiment.target,
                execution_plan=prepared.execution_plan,
                safety_policy=prepared.safety_policy,
                resource_limits=prepared.resource_limits,
                phase="confirmation",
                arm="selected",
            )

        first, second = await asyncio.gather(
            observe_once(
                runner=runner,
                identity=identity,
                store=store,
                attempt=attempt(),
            ),
            observe_once(
                runner=runner,
                identity=identity,
                store=store,
                attempt=attempt(),
            ),
        )
        assert runner.calls == 2
        assert len(store.list_for_experiment(prepared.experiment.digest)) == 2
        return first, second

    first, second = asyncio.run(measure_twice())

    assert {first.attempt, second.attempt} == {1, 2}


def test_preflight_requires_oracle_dependencies_to_be_protected(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    mutable_id = next(node.id for node in evidence.context.nodes if node.mutable)
    changed = experiment.model_copy(
        update={
            "oracle": experiment.oracle.model_copy(update={"protected_context_ids": (mutable_id,)})
        }
    )
    changed_evidence = CaseEvidence(
        manifest=evidence.manifest,
        request=evidence.request,
        context=evidence.context,
        default_experiment=changed.to_toml(),
    )

    with pytest.raises(OraclePrerequisiteError, match="reducible"):
        prepare_experiment(changed_evidence)


def test_observation_enforces_the_shared_wall_time_limit(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)

    class HangingRunner:
        fingerprint = prepared.runner.fingerprint
        capability_profile = prepared.runner.capability_profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            await asyncio.sleep(2)
            return await prepared.runner.run(attempt)

    limits = prepared.resource_limits.model_copy(update={"max_wall_time_seconds": 1})
    plan = prepared.execution_plan.model_copy(update={"resource_limits": limits})
    candidate = prepare_candidate(evidence, plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=plan,
        safety_policy=prepared.safety_policy,
        resource_limits=limits,
        phase="calibration",
        arm="baseline",
    )
    identity = prepared.identity.model_copy(update={"execution_plan_digest": plan.digest})
    store = ObservationStore(tmp_path / "timeout-observations.sqlite")

    with pytest.raises(RunnerExecutionError, match="timeout"):
        asyncio.run(
            observe_once(runner=HangingRunner(), identity=identity, store=store, attempt=attempt)
        )

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert observation.attempt_evidence.termination_reason == "timeout"


def test_cancellation_grace_does_not_extend_the_execution_deadline(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    limits = prepared.resource_limits.model_copy(update={"max_wall_time_seconds": 1})
    profile = prepared.runner.capability_profile.model_copy(
        update={"cancellation_grace_seconds": 5}
    )
    fingerprint = "cancellation-grace-test"
    plan = prepared.execution_plan.model_copy(
        update={
            "capability": profile,
            "resource_limits": limits,
            "runner_fingerprint": fingerprint,
        }
    )
    candidate = prepare_candidate(evidence, plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=plan,
        safety_policy=prepared.safety_policy,
        resource_limits=limits,
        phase="calibration",
        arm="baseline",
    )
    identity = prepared.identity.model_copy(
        update={
            "execution_plan_digest": plan.digest,
            "runner_fingerprint": fingerprint,
        }
    )

    class CleanupRunner:
        capability_profile = profile

        @property
        def fingerprint(self) -> str:
            return fingerprint

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise RunnerCancelled(
                    RunnerError(
                        RunnerErrorCode.CANCELLED,
                        "runner cleanup completed after the deadline",
                        provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                        accounting=RunAccounting(
                            process_outcome="terminated",
                            cleanup_outcome="verified",
                        ),
                        effective_policy_digest=attempt.safety_policy.digest,
                    )
                ) from None
            raise AssertionError("deadline did not cancel runner")

    store = ObservationStore(tmp_path / "cleanup-grace.sqlite")
    with pytest.raises(RunnerExecutionError, match="timeout"):
        asyncio.run(
            observe_once(
                runner=CleanupRunner(),
                identity=identity,
                store=store,
                attempt=attempt,
            )
        )

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert observation.attempt_evidence.termination_reason == "timeout"
    accounting = observation.attempt_evidence.accounting
    assert accounting is not None
    assert accounting.process_outcome == "terminated"
    assert accounting.cleanup_outcome == "verified"


@pytest.mark.parametrize("failure_mode", ["generic", "transport", "accepted"])
def test_incomplete_provider_attempt_blocks_automatic_retry(
    tmp_path: Path,
    *,
    failure_mode: str,
) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    calls = 0
    profile = prepared.runner.capability_profile.model_copy(
        update={
            "runner_version": "ambiguous-test",
            "provider_transport": "required",
            "provider_calls_per_attempt": 1,
            "endpoint": "https://provider.invalid/inference",
        }
    )
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider="fake",
        allowed_endpoint="https://provider.invalid/inference",
    )
    limits = ResourceLimits(max_provider_calls=1)
    report = resolve_execution_plan(
        profile,
        prepared.experiment,
        evidence=evidence,
        runner_fingerprint="ambiguous-test",
        safety_policy=policy,
        resource_limits=limits,
    )
    assert report.plan is not None

    class AmbiguousRunner:
        fingerprint = "ambiguous-test"
        capability_profile = profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            del attempt
            nonlocal calls
            calls += 1
            if failure_mode == "transport":
                raise RunnerError(
                    RunnerErrorCode.TRANSPORT,
                    "connection ended after request write",
                )
            if failure_mode == "accepted":
                raise RunnerError(
                    RunnerErrorCode.PROTOCOL,
                    "response stream ended before a result was produced",
                    provider_acceptance_state=ProviderAcceptanceState.ACCEPTED,
                    accounting=RunAccounting(
                        provider_calls_started=1,
                        provider_calls_completed=1,
                    ),
                )
            raise OSError("connection ended after request write")

    candidate = prepare_candidate(evidence, report.plan)
    identity = prepared.identity.model_copy(
        update={
            "runner_fingerprint": "ambiguous-test",
            "execution_plan_digest": report.plan.digest,
        }
    )

    def attempt() -> PreparedAttempt:
        return PreparedAttempt(
            evidence=evidence,
            candidate=candidate,
            target=prepared.experiment.target,
            execution_plan=report.plan,
            safety_policy=policy,
            resource_limits=limits,
            phase="calibration",
            arm="baseline",
        )

    store = ObservationStore(tmp_path / "ambiguous-observations.sqlite")
    runner = AmbiguousRunner()
    with pytest.raises(RunnerExecutionError):
        asyncio.run(observe_once(runner=runner, identity=identity, store=store, attempt=attempt()))
    with pytest.raises(RunnerExecutionError, match="automatic retry is disabled"):
        asyncio.run(observe_once(runner=runner, identity=identity, store=store, attempt=attempt()))

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    accepted = failure_mode == "accepted"
    assert observation.attempt_evidence.provider_call_started is accepted
    assert observation.attempt_evidence.acceptance_unknown is not accepted
    assert observation.attempt_evidence.provider_calls_possible == (0 if accepted else 1)
    assert calls == 1


def test_inconsistent_provider_accounting_blocks_automatic_retry(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    calls = 0
    profile = prepared.runner.capability_profile.model_copy(
        update={
            "runner_version": "accounting-test",
            "provider_transport": "required",
            "provider_calls_per_attempt": 1,
            "endpoint": "https://provider.invalid/inference",
        }
    )
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider="fake",
        allowed_endpoint="https://provider.invalid/inference",
    )
    limits = ResourceLimits(max_provider_calls=1)
    report = resolve_execution_plan(
        profile,
        prepared.experiment,
        evidence=evidence,
        runner_fingerprint="accounting-test",
        safety_policy=policy,
        resource_limits=limits,
    )
    assert report.plan is not None

    class InconsistentRunner:
        fingerprint = "accounting-test"
        capability_profile = profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            nonlocal calls
            calls += 1
            return RunResult(
                attempt_id=attempt.attempt_id,
                effective_policy_digest=attempt.safety_policy.digest,
                final_text="synthetic target response",
                provider=attempt.execution_plan.capability.provider,
                endpoint=attempt.execution_plan.capability.endpoint,
                model_requested=attempt.execution_plan.model_requested,
                accounting=RunAccounting(),
            )

    candidate = prepare_candidate(evidence, report.plan)
    identity = prepared.identity.model_copy(
        update={
            "runner_fingerprint": "accounting-test",
            "execution_plan_digest": report.plan.digest,
        }
    )

    def attempt() -> PreparedAttempt:
        return PreparedAttempt(
            evidence=evidence,
            candidate=candidate,
            target=prepared.experiment.target,
            execution_plan=report.plan,
            safety_policy=policy,
            resource_limits=limits,
            phase="calibration",
            arm="baseline",
        )

    store = ObservationStore(tmp_path / "inconsistent-accounting.sqlite")
    runner = InconsistentRunner()
    with pytest.raises(RunnerExecutionError, match="contract"):
        asyncio.run(observe_once(runner=runner, identity=identity, store=store, attempt=attempt()))
    with pytest.raises(RunnerExecutionError, match="automatic retry is disabled"):
        asyncio.run(observe_once(runner=runner, identity=identity, store=store, attempt=attempt()))

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert observation.attempt_evidence.provider_acceptance_state == "unknown"
    assert observation.attempt_evidence.provider_calls_possible == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("accounting_changes", "error_provider", "expected_category"),
    [
        ({"actions_requested": 1, "actions_executed": 1}, None, "safety"),
        ({"tool_network_requests": 1}, None, "safety"),
        ({"source_store_effect": "may-change"}, None, "safety"),
        ({"original_workspace_effect": "may-change"}, None, "safety"),
        ({"native_output_bytes": 1_048_577}, None, "output-limit"),
        (
            {
                "disposable_state_bytes": 1,
                "residual_disposable_state_bytes": 1,
                "cleanup_outcome": "failed",
            },
            None,
            "contract",
        ),
        ({}, "another-provider", "safety"),
    ],
)
def test_partial_error_effects_block_retry_and_are_reclassified(
    tmp_path: Path,
    accounting_changes: dict[str, object],
    error_provider: str | None,
    expected_category: str,
) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    calls = 0

    class UnsafeErrorRunner:
        fingerprint = prepared.runner.fingerprint
        capability_profile = prepared.runner.capability_profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            del attempt
            nonlocal calls
            calls += 1
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                "partial failure",
                accounting=RunAccounting().model_copy(update=accounting_changes),
                provider=error_provider,
            )

    def attempt() -> PreparedAttempt:
        return PreparedAttempt(
            evidence=evidence,
            candidate=candidate,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="calibration",
            arm="baseline",
        )

    runner = UnsafeErrorRunner()
    store = ObservationStore(tmp_path / "unsafe-partial-evidence.sqlite")
    with pytest.raises(RunnerExecutionError, match=expected_category):
        asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt())
        )
    with pytest.raises(RunnerExecutionError, match="automatic retry is disabled"):
        asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt())
        )

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert observation.error_category == expected_category
    assert not observation.attempt_evidence.automatic_retry_safe
    assert calls == 1


def test_clean_pre_dispatch_failure_can_be_retried(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    calls = 0

    class RetryableRunner:
        fingerprint = prepared.runner.fingerprint
        capability_profile = prepared.runner.capability_profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RunnerError(
                    RunnerErrorCode.STARTUP,
                    "failed before dispatch",
                    accounting=RunAccounting(),
                )
            return await prepared.runner.run(attempt)

    def attempt() -> PreparedAttempt:
        return PreparedAttempt(
            evidence=evidence,
            candidate=candidate,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="calibration",
            arm="baseline",
        )

    runner = RetryableRunner()
    store = ObservationStore(tmp_path / "retryable-pre-dispatch.sqlite")
    with pytest.raises(RunnerExecutionError, match="startup"):
        asyncio.run(
            observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt())
        )

    observation = asyncio.run(
        observe_once(runner=runner, identity=prepared.identity, store=store, attempt=attempt())
    )

    assert observation.valid
    assert calls == 2


def test_measurement_refuses_capability_change_after_preflight(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )

    class ChangedProfileRunner:
        fingerprint = prepared.runner.fingerprint
        capability_profile = prepared.runner.capability_profile.model_copy(
            update={"runner_version": "changed"}
        )

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            return await prepared.runner.run(attempt)

    store = ObservationStore(tmp_path / "changed-profile.sqlite")
    with pytest.raises(ValueError, match="capability profile changed"):
        asyncio.run(
            observe_once(
                runner=ChangedProfileRunner(),
                identity=prepared.identity,
                store=store,
                attempt=attempt,
            )
        )

    assert store.list_for_experiment(prepared.experiment.digest) == ()


def test_cancelled_attempt_is_persisted_before_cancellation_propagates(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )

    class CancelledRunner:
        fingerprint = prepared.runner.fingerprint
        capability_profile = prepared.runner.capability_profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            del attempt
            raise asyncio.CancelledError

    store = ObservationStore(tmp_path / "cancelled.sqlite")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            observe_once(
                runner=CancelledRunner(),
                identity=prepared.identity,
                store=store,
                attempt=attempt,
            )
        )

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    assert observation.attempt_evidence.termination_reason == "cancelled"


def test_cancelled_attempt_preserves_runner_cleanup_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    prepared = prepare_experiment(evidence)
    candidate = prepare_candidate(evidence, prepared.execution_plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )

    class CancelledRunner:
        fingerprint = prepared.runner.fingerprint
        capability_profile = prepared.runner.capability_profile

        async def run(self, attempt: PreparedAttempt) -> RunResult:
            raise RunnerCancelled(
                RunnerError(
                    RunnerErrorCode.CANCELLED,
                    "runner was cancelled",
                    provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                    accounting=RunAccounting(
                        process_outcome="terminated",
                        cleanup_outcome="verified",
                    ),
                    effective_policy_digest=attempt.safety_policy.digest,
                )
            )

    store = ObservationStore(tmp_path / "cancelled-with-evidence.sqlite")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            observe_once(
                runner=CancelledRunner(),
                identity=prepared.identity,
                store=store,
                attempt=attempt,
            )
        )

    [observation] = store.list_for_experiment(prepared.experiment.digest)
    accounting = observation.attempt_evidence.accounting
    assert accounting is not None
    assert accounting.process_outcome == "terminated"
    assert accounting.cleanup_outcome == "verified"
