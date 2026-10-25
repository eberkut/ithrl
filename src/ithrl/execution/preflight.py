"""Resolve runner capabilities into bounded execution plans."""

from __future__ import annotations

from ithrl.behavior import BehaviorType
from ithrl.case import CaseEvidence
from ithrl.execution.contracts import (
    CandidateMaterialization,
    CapabilityProfile,
    PreflightIssue,
    PreflightReport,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.experiment import (
    AnyExperimentDefinition,
    ExperimentDefinition,
    FakeRunnerConfig,
    NativeRunnerConfig,
    SampledExperimentDefinition,
    UnavailableRunnerConfig,
)


def resolve_execution_plan(
    capability: CapabilityProfile,
    experiment: AnyExperimentDefinition,
    *,
    evidence: CaseEvidence,
    runner_fingerprint: str,
    safety_policy: SafetyPolicy,
    resource_limits: ResourceLimits,
) -> PreflightReport:
    """Resolve one bounded plan or return every known refusal."""

    if isinstance(experiment.runner, UnavailableRunnerConfig):
        return PreflightReport(
            ready=False,
            plan=None,
            issues=(
                PreflightIssue(
                    code="runner-unavailable",
                    message="the experiment has no compatible implemented runner",
                ),
            ),
        )

    capture_type = evidence.manifest.capture.request
    materialization: CandidateMaterialization = (
        "exact-request" if evidence.has_request else "source-backed"
    )
    issues: list[PreflightIssue] = []
    _check_runner_identity(capability, experiment, evidence, issues)
    if capture_type not in capability.supported_capture_types:
        issues.append(
            PreflightIssue(
                code="unsupported-capture",
                message=f"runner does not support capture type {capture_type!r}",
            )
        )
    if materialization not in capability.supported_materializations:
        issues.append(
            PreflightIssue(
                code="unsupported-materialization",
                message=f"runner does not support {materialization!r} candidates",
            )
        )
    if experiment.target.type not in capability.supported_targets:
        issues.append(
            PreflightIssue(
                code="unsupported-target",
                message=f"runner does not support target {experiment.target.type!r}",
            )
        )
    if isinstance(experiment, ExperimentDefinition) and not capability.deterministic:
        issues.append(
            PreflightIssue(
                code="sampled-execution-unsupported",
                message="sampled runners require the later sampled calibration contract",
            )
        )
    if isinstance(experiment, SampledExperimentDefinition):
        fake = isinstance(experiment.runner, FakeRunnerConfig) and (
            capability.runner_type == "fake"
            and capability.deterministic
            and capability.provider_transport == "forbidden"
            and capability.provider_calls_per_attempt == 0
        )
        if capability.deterministic and not fake:
            issues.append(
                PreflightIssue(
                    code="sampled-runner-required",
                    message="sampled calibration requires a nondeterministic runner profile",
                )
            )
        native_runner = experiment.runner
        if not isinstance(native_runner, NativeRunnerConfig) and not fake:
            raise TypeError("sampled execution requires a native or credential-free fake runner")
        if (
            isinstance(native_runner, NativeRunnerConfig)
            and capability.digest != native_runner.required_capability_digest
        ):
            issues.append(
                PreflightIssue(
                    code="capability-profile-mismatch",
                    message="runner capability profile does not match the experiment",
                )
            )
    _check_transport(capability, safety_policy, resource_limits, issues)
    _check_effects(capability, safety_policy, experiment.target.type, issues)
    if issues:
        return PreflightReport(ready=False, plan=None, issues=tuple(issues))
    return _resolved_report(
        capability,
        experiment,
        evidence=evidence,
        runner_fingerprint=runner_fingerprint,
        safety_policy=safety_policy,
        resource_limits=resource_limits,
        materialization=materialization,
    )


def _resolved_report(
    capability: CapabilityProfile,
    experiment: AnyExperimentDefinition,
    *,
    evidence: CaseEvidence,
    runner_fingerprint: str,
    safety_policy: SafetyPolicy,
    resource_limits: ResourceLimits,
    materialization: CandidateMaterialization,
) -> PreflightReport:
    manifest = evidence.manifest
    decision = evidence.decision
    plan = ResolvedExecutionPlan(
        case_digest=evidence.digest,
        capture_type=manifest.capture.request,
        candidate_materialization=materialization,
        capability=capability,
        harness=manifest.harness,
        source_harness_version=manifest.harness_version,
        source_schema=manifest.source_schema,
        model_requested=manifest.model_requested,
        decision_digest=decision.selector.digest if decision is not None else None,
        runner_fingerprint=runner_fingerprint,
        safety_policy=safety_policy,
        resource_limits=resource_limits,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        target_type=experiment.target.type,
    )
    return PreflightReport(ready=True, plan=plan)


def _check_runner_identity(
    capability: CapabilityProfile,
    experiment: AnyExperimentDefinition,
    evidence: CaseEvidence,
    issues: list[PreflightIssue],
) -> None:
    if capability.runner_type != experiment.runner.type:
        issues.append(
            PreflightIssue(
                code="runner-type-mismatch",
                message=(
                    f"experiment requires runner {experiment.runner.type!r}, "
                    f"profile provides {capability.runner_type!r}"
                ),
            )
        )
    expected = (capability.harness, capability.source_schema)
    actual = (evidence.manifest.harness, evidence.manifest.source_schema)
    labels = ("harness", "source schema")
    for label, required, captured in zip(labels, expected, actual, strict=True):
        if required is not None and required != captured:
            issues.append(
                PreflightIssue(
                    code="runner-evidence-mismatch",
                    message=f"runner {label} {required!r} does not match captured {captured!r}",
                )
            )


def _check_transport(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    limits: ResourceLimits,
    issues: list[PreflightIssue],
) -> None:
    _check_provider_limits(capability, policy, limits, issues)
    _check_input_token_limit(capability, limits, issues)
    _check_provider_destination(capability, policy, issues)
    _check_execution_lifecycle(capability, issues)


def _check_provider_limits(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    limits: ResourceLimits,
    issues: list[PreflightIssue],
) -> None:
    if capability.provider_transport == "required" and policy.provider_transport == "forbidden":
        issues.append(
            PreflightIssue(
                code="provider-transport-forbidden",
                message="runner requires provider transport but the safety policy forbids it",
            )
        )
    if capability.provider_calls_per_attempt > limits.max_provider_calls:
        issues.append(
            PreflightIssue(
                code="provider-call-limit-too-small",
                message="provider-call limit is smaller than the runner requirement",
            )
        )


def _check_input_token_limit(
    capability: CapabilityProfile,
    limits: ResourceLimits,
    issues: list[PreflightIssue],
) -> None:
    if (
        limits.max_input_tokens is not None
        and capability.input_token_limit_enforcement != "pre-dispatch"
    ):
        issues.append(
            PreflightIssue(
                code="input-token-limit-unenforceable",
                message="runner cannot enforce the input-token limit before provider dispatch",
            )
        )


def _check_provider_destination(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    issues: list[PreflightIssue],
) -> None:
    if capability.provider_transport == "required" and (
        capability.provider != policy.allowed_provider
        or capability.endpoint != policy.allowed_endpoint
    ):
        issues.append(
            PreflightIssue(
                code="provider-destination-mismatch",
                message="runner destination is not allowed by the safety policy",
            )
        )


def _check_execution_lifecycle(
    capability: CapabilityProfile,
    issues: list[PreflightIssue],
) -> None:
    if not capability.supports_timeout:
        issues.append(
            PreflightIssue(
                code="timeout-unsupported",
                message="runner cannot enforce the attempt wall-time limit",
            )
        )
    if not capability.supports_cancellation:
        issues.append(
            PreflightIssue(
                code="cancellation-unsupported",
                message="runner cannot cancel and clean up a bounded attempt",
            )
        )


def _check_effects(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    target_type: BehaviorType,
    issues: list[PreflightIssue],
) -> None:
    _check_tool_network(capability, policy, issues)
    _check_actions(capability, policy, target_type, issues)
    _check_protected_state(capability, policy, issues)


def _check_tool_network(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    issues: list[PreflightIssue],
) -> None:
    if capability.tool_network_access != policy.tool_network_access:
        issues.append(
            PreflightIssue(
                code="tool-network-access-possible",
                message="runner cannot prove that tool-side network access is forbidden",
            )
        )


def _check_actions(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    target_type: BehaviorType,
    issues: list[PreflightIssue],
) -> None:
    if target_type == "shell-action" and capability.action_visibility != "visible":
        issues.append(
            PreflightIssue(
                code="action-not-visible",
                message="runner cannot keep the target action visible",
            )
        )
    if capability.action_denial != "enforced":
        issues.append(
            PreflightIssue(
                code="action-denial-unverified",
                message="runner cannot prove that returned actions are denied",
            )
        )
    if capability.action_execution != policy.action_execution:
        issues.append(
            PreflightIssue(
                code="action-execution-possible",
                message="runner cannot prove that action execution is denied",
            )
        )


def _check_protected_state(
    capability: CapabilityProfile,
    policy: SafetyPolicy,
    issues: list[PreflightIssue],
) -> None:
    if capability.source_store_effect != policy.source_store_effect:
        issues.append(
            PreflightIssue(
                code="source-store-may-change",
                message="runner may change the preserved source session",
            )
        )
    if capability.original_workspace_effect != policy.original_workspace_effect:
        issues.append(
            PreflightIssue(
                code="workspace-may-change",
                message="runner may change the original workspace",
            )
        )
    if capability.protected_state_enforcement == "postcondition" or (
        capability.process_containment != "not-applicable"
        and capability.protected_state_enforcement != "preventive"
    ):
        issues.append(
            PreflightIssue(
                code="protected-state-enforcement-insufficient",
                message="process runners require preventive protected-state isolation",
            )
        )
