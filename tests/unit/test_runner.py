from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from pydantic import ValidationError

import ithrl.runner as runner_module
from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.behavior import FinalResponseTarget
from ithrl.case import CaseEvidence
from ithrl.execution import (
    CapabilityProfile,
    PreflightIssue,
    ResourceLimits,
    SafetyPolicy,
    resolve_execution_plan,
)
from ithrl.experiment import ExperimentDefinition, FakeRunnerConfig
from ithrl.extraction import extract_decision
from ithrl.runner import (
    AttemptEvidence,
    Candidate,
    FakeRunner,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    RunnerRegistry,
    RunnerResolution,
    RunResult,
    ToolCall,
    built_in_runner_registry,
    prepare_candidate,
    validate_run_result,
    validate_runner_error,
)
from ithrl.runner.contracts import AttemptArm, AttemptPhase
from ithrl.synthetic import extract_inference
from ithrl.workflow import prepare_experiment

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _synthetic_evidence(tmp_path: Path) -> CaseEvidence:
    return extract_inference(
        FIXTURES / "synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "synthetic-case",
    ).load()


def _native_evidence(tmp_path: Path) -> CaseEvidence:
    session = parse_session(FIXTURES / "codex-session" / "rollout.jsonl")
    decision = enumerate_decisions(session)[0]
    return extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "native-case",
    ).load()


def _attempt(evidence: CaseEvidence) -> tuple[FakeRunner, PreparedAttempt]:
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
    assert isinstance(prepared.runner, FakeRunner)
    return prepared.runner, attempt


def test_public_runner_imports_execute_a_prepared_candidate(tmp_path: Path) -> None:
    runner, attempt = _attempt(_synthetic_evidence(tmp_path))

    result = asyncio.run(runner.run(attempt))
    validate_run_result(attempt, result)

    assert attempt.candidate.materialization == "exact-request"
    assert result.attempt_id == attempt.attempt_id
    assert result.accounting.actions_executed == 0
    assert result.accounting.tool_network_requests == 0


@pytest.mark.parametrize(
    ("phase", "arm"),
    [
        ("search", "baseline"),
        ("search", "candidate"),
        ("confirmation", "baseline"),
        ("confirmation", "candidate"),
        ("confirmation", "selected"),
    ],
)
def test_prepared_attempt_supports_fresh_sampling_arms(
    tmp_path: Path, phase: AttemptPhase, arm: AttemptArm
) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    sampled = replace(attempt, phase=phase, arm=arm)
    assert sampled.phase == phase
    assert sampled.arm == arm
    assert sampled.digest != attempt.digest


@pytest.mark.parametrize(
    ("phase", "arm"),
    [("calibration", "candidate"), ("calibration", "selected"), ("search", "selected")],
)
def test_prepared_attempt_rejects_wrong_sampling_arm(
    tmp_path: Path, phase: AttemptPhase, arm: AttemptArm
) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    with pytest.raises(ValueError, match="arm is not allowed"):
        replace(attempt, phase=phase, arm=arm)


def test_runner_package_preserves_the_previous_public_imports() -> None:
    assert {"FakeRunner", "Runner", "RunResult", "ToolCall"} <= set(runner_module.__all__)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (RunnerErrorCode.PREPARATION, ProviderAcceptanceState.NOT_STARTED),
        (RunnerErrorCode.STARTUP, ProviderAcceptanceState.NOT_STARTED),
        (RunnerErrorCode.TRANSPORT, ProviderAcceptanceState.UNKNOWN),
        (RunnerErrorCode.PROTOCOL, ProviderAcceptanceState.UNKNOWN),
        (RunnerErrorCode.INTERNAL, ProviderAcceptanceState.UNKNOWN),
    ],
)
def test_runner_error_defaults_provider_dispatch_safely(
    code: RunnerErrorCode,
    expected: ProviderAcceptanceState,
) -> None:
    assert RunnerError(code, "test failure").provider_acceptance_state is expected


@pytest.mark.parametrize(
    "acceptance",
    [ProviderAcceptanceState.ACCEPTED, ProviderAcceptanceState.UNKNOWN],
)
def test_zero_call_plan_classifies_possible_provider_acceptance_as_safety(
    tmp_path: Path,
    acceptance: ProviderAcceptanceState,
) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    error = RunnerError(
        RunnerErrorCode.STARTUP,
        "contradictory acceptance evidence",
        provider_acceptance_state=acceptance,
    )

    classified = validate_runner_error(attempt, error)
    evidence = AttemptEvidence.from_error(attempt, classified)

    assert classified.code is RunnerErrorCode.SAFETY
    assert evidence.provider_acceptance_state is acceptance
    assert not evidence.automatic_retry_safe


def test_disposable_state_without_cleanup_proof_blocks_retry(tmp_path: Path) -> None:
    evidence = _synthetic_evidence(tmp_path)
    prepared = prepare_experiment(
        evidence,
        resource_limits=ResourceLimits(max_disposable_state_bytes=1),
    )
    profile = prepared.execution_plan.capability.model_copy(
        update={
            "disposable_state_effect": "writes",
            "disposable_state_limit_enforcement": "observed",
        }
    )
    plan = prepared.execution_plan.model_copy(update={"capability": profile})
    candidate = prepare_candidate(evidence, plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )
    error = RunnerError(
        RunnerErrorCode.PREPARATION,
        "disposable state remains",
        accounting=RunAccounting(
            disposable_state_bytes=1,
            residual_disposable_state_bytes=1,
            cleanup_outcome="failed",
        ),
    )

    attempt_evidence = AttemptEvidence.from_error(attempt, error)

    assert not attempt_evidence.automatic_retry_safe


@pytest.mark.parametrize(
    ("limit_enforcement", "retry_safe"),
    [("enforced", True), ("observed", False)],
)
def test_disposable_state_retry_requires_an_enforced_limit(
    tmp_path: Path,
    limit_enforcement: str,
    retry_safe: bool,
) -> None:
    evidence = _synthetic_evidence(tmp_path)
    prepared = prepare_experiment(
        evidence,
        resource_limits=ResourceLimits(max_disposable_state_bytes=64),
    )
    profile = prepared.execution_plan.capability.model_copy(
        update={
            "disposable_state_effect": "writes",
            "disposable_state_limit_enforcement": limit_enforcement,
        }
    )
    plan = prepared.execution_plan.model_copy(update={"capability": profile})
    candidate = prepare_candidate(evidence, plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=prepared.experiment.target,
        execution_plan=plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase="calibration",
        arm="baseline",
    )
    error = RunnerError(
        RunnerErrorCode.STARTUP,
        "runner did not start",
        accounting=RunAccounting(
            disposable_state_bytes=64,
            cleanup_outcome="verified",
        ),
    )

    attempt_evidence = AttemptEvidence.from_error(attempt, error)

    assert attempt_evidence.automatic_retry_safe is retry_safe


def test_cleanup_or_process_uncertainty_blocks_safe_retry(tmp_path: Path) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))

    for accounting in (
        RunAccounting(process_outcome="unknown"),
        RunAccounting(cleanup_outcome="failed"),
    ):
        error = RunnerError(
            RunnerErrorCode.STARTUP,
            "runner did not start cleanly",
            accounting=accounting,
        )
        assert not AttemptEvidence.from_error(attempt, error).automatic_retry_safe


def test_accounting_preserves_uncertain_termination_and_failed_cleanup(tmp_path: Path) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    accounting = RunAccounting(
        disposable_state_bytes=8,
        residual_disposable_state_bytes=8,
        disposable_state_entries=1,
        residual_disposable_state_entries=1,
        process_outcome="unknown",
        forced_termination=True,
        cleanup_outcome="failed",
    )
    error = RunnerError(
        RunnerErrorCode.INTERNAL,
        "runtime safety evidence is incomplete",
        accounting=accounting,
    )

    classified = validate_runner_error(attempt, error)

    assert classified.code is RunnerErrorCode.SAFETY
    assert classified.accounting == accounting
    assert not AttemptEvidence.from_error(attempt, classified).automatic_retry_safe


def test_runner_cancellation_requires_cancelled_evidence() -> None:
    cancelled = RunnerError(RunnerErrorCode.CANCELLED, "runner was cancelled")

    assert RunnerCancelled(cancelled).error is cancelled
    with pytest.raises(ValueError, match="requires the cancelled error category"):
        RunnerCancelled(RunnerError(RunnerErrorCode.TIMEOUT, "runner timed out"))


def test_source_backed_candidate_retains_native_evidence_without_materializing_it(
    tmp_path: Path,
) -> None:
    evidence = _native_evidence(tmp_path)
    assert evidence.decision is not None
    target = evidence.decision.selected_target
    assert isinstance(target, FinalResponseTarget)
    assert target.text_equals is not None
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment).model_copy(
        update={
            "runner": FakeRunnerConfig(
                required_markers=("unused",),
                final_text=target.text_equals,
            )
        }
    )
    profile = CapabilityProfile(
        runner_type="fake",
        runner_version="source-candidate-test",
        harness=evidence.manifest.harness,
        runtime_harness_version="test-runtime",
        source_schema=evidence.manifest.source_schema,
        deterministic=True,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="source-candidate-test-v1",
        supported_targets=("final-response",),
        provider_transport="forbidden",
        provider_calls_per_attempt=0,
        tool_network_access="forbidden",
        action_visibility="not-applicable",
        action_denial="enforced",
        action_execution="denied",
        source_store_effect="unchanged",
        original_workspace_effect="unchanged",
        disposable_state_effect="none",
        supports_timeout=True,
        supports_cancellation=True,
        reports_usage=False,
    )
    policy = SafetyPolicy.credential_free()
    limits = ResourceLimits.credential_free()
    report = resolve_execution_plan(
        profile,
        experiment,
        evidence=evidence,
        runner_fingerprint="source-candidate-test",
        safety_policy=policy,
        resource_limits=limits,
    )
    assert report.plan is not None
    before = tuple(artifact.content for artifact in evidence.source_artifacts)

    candidate = prepare_candidate(evidence, report.plan)
    attempt = PreparedAttempt(
        evidence=evidence,
        candidate=candidate,
        target=target,
        execution_plan=report.plan,
        safety_policy=policy,
        resource_limits=limits,
        phase="calibration",
        arm="baseline",
    )

    assert candidate.materialization == "source-backed"
    assert candidate.source_artifact_digests == tuple(
        artifact.sha256 for artifact in evidence.source_artifacts
    )
    assert tuple(artifact.content for artifact in evidence.source_artifacts) == before
    assert attempt.evidence is evidence

    class SourceBackedRunner:
        fingerprint = "source-candidate-test"
        capability_profile = profile

        async def run(self, prepared_attempt: PreparedAttempt) -> RunResult:
            assert prepared_attempt.candidate.materialization == "source-backed"
            assert prepared_attempt.evidence.source_artifacts
            return RunResult(
                attempt_id=prepared_attempt.attempt_id,
                effective_policy_digest=prepared_attempt.safety_policy.digest,
                final_text=target.text_equals,
                model_requested=prepared_attempt.execution_plan.model_requested,
                provider=prepared_attempt.execution_plan.capability.provider,
                endpoint=prepared_attempt.execution_plan.capability.endpoint,
                accounting=RunAccounting(),
            )

    result = asyncio.run(SourceBackedRunner().run(attempt))
    validate_run_result(attempt, result)
    assert result.final_text == target.text_equals
    assert tuple(artifact.content for artifact in evidence.source_artifacts) == before


def test_prepared_attempt_rejects_tampered_candidate_identity(tmp_path: Path) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    candidate = attempt.candidate.model_copy(update={"removed_context_ids": ("unknown",)})

    with pytest.raises(ValueError, match="unknown context node IDs"):
        PreparedAttempt(
            evidence=attempt.evidence,
            candidate=candidate,
            target=attempt.target,
            execution_plan=attempt.execution_plan,
            safety_policy=attempt.safety_policy,
            resource_limits=attempt.resource_limits,
            phase=attempt.phase,
            arm=attempt.arm,
        )
    with pytest.raises(FrozenInstanceError):
        attempt.phase = "search"  # type: ignore[misc]


def test_run_result_rejects_executed_action_as_a_safety_failure(tmp_path: Path) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    result = RunResult(
        attempt_id=attempt.attempt_id,
        effective_policy_digest=attempt.safety_policy.digest,
        tool_calls=(ToolCall(name="exec_command"),),
        accounting=RunAccounting(actions_requested=1, actions_executed=1),
    )

    with pytest.raises(RunnerError) as failure:
        validate_run_result(attempt, result)

    assert failure.value.code is RunnerErrorCode.SAFETY


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"provider_connections_started": 1}, RunnerErrorCode.SAFETY),
        ({"tool_network_requests": 1}, RunnerErrorCode.SAFETY),
        ({"actions_denied": 0}, RunnerErrorCode.SAFETY),
        ({"source_store_effect": "may-change"}, RunnerErrorCode.SAFETY),
        ({"original_workspace_effect": "may-change"}, RunnerErrorCode.SAFETY),
        ({"elapsed_milliseconds": 30_001}, RunnerErrorCode.TIMEOUT),
        ({"native_output_bytes": 1_048_577}, RunnerErrorCode.OUTPUT_LIMIT),
        ({"disposable_state_bytes": 1}, RunnerErrorCode.OUTPUT_LIMIT),
        ({"disposable_state_entries": 10_001}, RunnerErrorCode.OUTPUT_LIMIT),
    ],
)
def test_run_result_enforces_attempt_safety_and_resource_limits(
    tmp_path: Path,
    changes: dict[str, object],
    error_code: RunnerErrorCode,
) -> None:
    runner, attempt = _attempt(_synthetic_evidence(tmp_path))
    if {"disposable_state_bytes", "disposable_state_entries"} & changes.keys():
        capability = attempt.execution_plan.capability.model_copy(
            update={
                "disposable_state_effect": "writes",
                "disposable_state_limit_enforcement": "enforced",
            }
        )
        execution_plan = attempt.execution_plan.model_copy(update={"capability": capability})
        candidate = prepare_candidate(attempt.evidence, execution_plan)
        attempt = replace(attempt, candidate=candidate, execution_plan=execution_plan)
    result = asyncio.run(runner.run(attempt))
    changed = result.model_copy(update={"accounting": result.accounting.model_copy(update=changes)})

    with pytest.raises(RunnerError) as failure:
        validate_run_result(attempt, changed)

    assert failure.value.code is error_code


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "another-provider"},
        {"endpoint": "https://provider.invalid/other"},
        {"model_requested": "another-model"},
    ],
)
def test_run_result_must_match_resolved_execution_identity(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    runner, attempt = _attempt(_synthetic_evidence(tmp_path))
    result = asyncio.run(runner.run(attempt)).model_copy(update=changes)

    with pytest.raises(RunnerError, match="differs from the resolved execution plan"):
        validate_run_result(attempt, result)


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt_id": "another-attempt"},
        {"effective_policy_digest": "f" * 64},
        {"termination_reason": "failed"},
    ],
)
def test_run_result_enforces_attempt_identity_and_completion(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    runner, attempt = _attempt(_synthetic_evidence(tmp_path))
    result = asyncio.run(runner.run(attempt)).model_copy(update=changes)

    with pytest.raises(RunnerError):
        validate_run_result(attempt, result)


def test_registry_reports_native_runner_unavailability_without_constructing_one(
    tmp_path: Path,
) -> None:
    evidence = _native_evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)

    resolution = built_in_runner_registry().resolve(experiment.runner, evidence)

    assert resolution.runner is None
    assert resolution.issue is not None
    assert resolution.issue.code == "runner-unavailable"
    with pytest.raises(ValueError, match="exactly one outcome"):
        RunnerResolution(runner=None, issue=None)


def test_runner_factory_can_refuse_incompatible_case_evidence(tmp_path: Path) -> None:
    evidence = _synthetic_evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    issue = PreflightIssue(
        code="runner-evidence-mismatch",
        message="runner cannot reconstruct this captured source",
    )
    registry = RunnerRegistry({"fake": lambda _config, _evidence: issue})

    resolution = registry.resolve(experiment.runner, evidence)

    assert resolution == RunnerResolution(runner=None, issue=issue)


def test_candidate_contract_rejects_overlapping_context_partitions() -> None:
    with pytest.raises(ValueError, match="disjoint"):
        Candidate(
            case_digest="a" * 64,
            execution_plan_digest="b" * 64,
            retained_context_ids=("ctx-a",),
            removed_context_ids=("ctx-a",),
            materialization="exact-request",
        )


def test_tool_call_rejects_oversized_or_deep_arguments() -> None:
    with pytest.raises(ValidationError, match="byte limit"):
        ToolCall(name="shell", arguments={"command": "x" * (64 * 1024)})

    nested: dict[str, object] = {}
    for _ in range(17):
        nested = {"child": nested}
    with pytest.raises(ValidationError, match="nesting"):
        ToolCall(name="shell", arguments=nested)

    with pytest.raises(ValidationError, match="item"):
        ToolCall(name="shell", arguments={"items": [None] * 4097})


def test_result_validation_rechecks_mutated_tool_arguments(tmp_path: Path) -> None:
    runner, attempt = _attempt(_synthetic_evidence(tmp_path))
    result = asyncio.run(runner.run(attempt))
    assert result.tool_calls
    result.tool_calls[0].arguments["oversized"] = "x" * (64 * 1024)

    with pytest.raises(RunnerError) as failure:
        validate_run_result(attempt, result)

    assert failure.value.code is RunnerErrorCode.OUTPUT_LIMIT


def test_attempt_evidence_rejects_provider_state_accounting_conflicts(
    tmp_path: Path,
) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    accepted_result = RunResult(
        attempt_id=attempt.attempt_id,
        effective_policy_digest=attempt.safety_policy.digest,
        accounting=RunAccounting(provider_calls_started=1, provider_calls_completed=1),
    )

    with pytest.raises(ValidationError, match="not-started"):
        AttemptEvidence(
            attempt_id=attempt.attempt_id,
            attempt_digest=attempt.digest,
            termination_reason="failed",
            provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
            accounting=accepted_result.accounting,
        )
    with pytest.raises(ValidationError, match="requires a started"):
        AttemptEvidence(
            attempt_id=attempt.attempt_id,
            attempt_digest=attempt.digest,
            termination_reason="failed",
            provider_acceptance_state=ProviderAcceptanceState.ACCEPTED,
            accounting=RunAccounting(),
        )

    reconciled = AttemptEvidence.from_error(
        attempt,
        RunnerError(
            RunnerErrorCode.PREPARATION,
            "contradictory partial evidence",
            provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
            accounting=accepted_result.accounting,
        ),
    )
    assert reconciled.provider_acceptance_state is ProviderAcceptanceState.ACCEPTED
    assert not reconciled.automatic_retry_safe


def test_fake_runner_preserves_unmatched_and_empty_argument_output(tmp_path: Path) -> None:
    _, attempt = _attempt(_synthetic_evidence(tmp_path))
    unmatched = FakeRunner(required_markers=frozenset({"missing"}), final_text="target")
    without_command = FakeRunner(
        required_markers=frozenset({"A", "C"}),
        tool_name="shell",
    )

    unmatched_result = asyncio.run(unmatched.run(attempt))
    tool_result = asyncio.run(without_command.run(attempt))

    assert unmatched_result.final_text == "Target behavior not emitted."
    assert tool_result.tool_calls[0].arguments == {}


@pytest.mark.parametrize(
    "arguments",
    [
        {"required_markers": frozenset(), "tool_name": "shell"},
        {"required_markers": frozenset({"A"}), "tool_name": None},
        {
            "required_markers": frozenset({"A"}),
            "tool_name": "shell",
            "final_text": "done",
        },
    ],
)
def test_fake_runner_rejects_invalid_direct_configuration(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"must not be empty|exactly one behavior family"):
        FakeRunner(**arguments)  # type: ignore[arg-type]
