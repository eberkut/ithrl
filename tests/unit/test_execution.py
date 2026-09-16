from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import ithrl.execution as execution_module
from ithrl.behavior import ShellActionTarget
from ithrl.execution import (
    CapabilityProfile,
    PreflightIssue,
    PreflightReport,
    ResourceLimits,
    SafetyPolicy,
    resolve_execution_plan,
)
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    FakeRunnerConfig,
    StructuralOracleConfig,
)
from ithrl.synthetic import extract_inference

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


def test_execution_package_preserves_public_contract() -> None:
    assert {
        "CapabilityProfile",
        "PreflightIssue",
        "ResolvedExecutionPlan",
        "ResourceLimits",
        "SafetyPolicy",
        "resolve_execution_plan",
    } <= set(execution_module.__all__)


def _shell_experiment() -> ExperimentDefinition:
    return ExperimentDefinition(
        target=ShellActionTarget(tool_name_equals="bash", command_equals="pwd"),
        oracle=StructuralOracleConfig(protected_context_ids=("ctx-tool-bash",)),
        runner=FakeRunnerConfig(
            required_markers=("A", "C"),
            tool_name="bash",
            command="pwd",
        ),
        calibration=DeterministicPhase(role="calibration"),
        search=DeterministicPhase(role="search"),
        confirmation=DeterministicPhase(role="confirmation"),
    )


def _evidence(tmp_path: Path):
    return extract_inference(
        FIXTURE,
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()


def test_fake_capability_resolves_bounded_credential_free_plan(tmp_path: Path) -> None:
    experiment = _shell_experiment()
    capability = CapabilityProfile.deterministic_fake()
    policy = SafetyPolicy.credential_free()
    limits = ResourceLimits.credential_free()

    report = resolve_execution_plan(
        capability,
        experiment,
        evidence=_evidence(tmp_path),
        runner_fingerprint="fake-v1:A,C:bash:pwd",
        safety_policy=policy,
        resource_limits=limits,
    )

    assert report.ready
    assert report.issues == ()
    assert report.network_calls_started == 0
    assert report.provider_calls_started == 0
    assert report.actions_executed == 0
    assert report.plan is not None
    assert report.plan.candidate_materialization == "exact-request"
    assert report.plan.capability.supported_context_operations == ("dependency-closed-removal",)
    assert report.plan.capability.reconstruction_method == "exact-request-json-pointer-v1"
    assert report.plan.capability.provider_transport == "forbidden"
    assert report.plan.capability.provider_calls_per_attempt == 0
    assert report.plan.capability.tool_network_access == "forbidden"
    assert report.plan.capability.action_execution == "denied"
    assert report.plan.capability.source_store_effect == "unchanged"
    assert report.plan.capability.original_workspace_effect == "unchanged"
    assert report.plan.target_digest == experiment.target.digest
    assert report.plan.experiment_digest == experiment.digest
    assert report.plan.capability == capability
    assert report.plan.safety_policy == policy
    assert report.plan.resource_limits == limits
    assert len(report.plan.digest) == 64


def test_preflight_collects_every_incompatible_execution_fact(tmp_path: Path) -> None:
    capability = CapabilityProfile(
        runner_type="other",
        runner_version="1",
        harness="other-harness",
        source_schema="other-source-schema",
        deterministic=False,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="incompatible-test-v1",
        supported_targets=("final-response",),
        provider_transport="required",
        provider_calls_per_attempt=1,
        tool_network_access="possible",
        action_visibility="unknown",
        action_denial="unverified",
        action_execution="possible",
        source_store_effect="may-change",
        original_workspace_effect="may-change",
        disposable_state_effect="writes",
        supports_timeout=False,
        supports_cancellation=False,
        reports_usage=False,
        provider="other-provider",
        endpoint="https://provider.invalid/inference",
    )

    report = resolve_execution_plan(
        capability,
        _shell_experiment(),
        evidence=_evidence(tmp_path),
        runner_fingerprint="other-v1",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=ResourceLimits.credential_free(),
    )

    assert not report.ready
    assert report.plan is None
    assert {issue.code for issue in report.issues} == {
        "runner-type-mismatch",
        "runner-evidence-mismatch",
        "unsupported-capture",
        "unsupported-materialization",
        "unsupported-target",
        "sampled-execution-unsupported",
        "provider-transport-forbidden",
        "provider-call-limit-too-small",
        "provider-destination-mismatch",
        "tool-network-access-possible",
        "action-not-visible",
        "action-denial-unverified",
        "action-execution-possible",
        "source-store-may-change",
        "workspace-may-change",
        "timeout-unsupported",
        "cancellation-unsupported",
    }
    assert report.network_calls_started == 0
    assert report.provider_calls_started == 0
    assert report.actions_executed == 0


def test_preflight_accounts_for_allowed_provider_transport_separately(
    tmp_path: Path,
) -> None:
    capability = CapabilityProfile(
        runner_type="fake",
        runner_version="provider-test",
        deterministic=True,
        supported_capture_types=("provider-request",),
        supported_materializations=("exact-request",),
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="provider-test-v1",
        supported_targets=("shell-action",),
        provider_transport="required",
        provider_calls_per_attempt=1,
        tool_network_access="forbidden",
        action_visibility="visible",
        action_denial="enforced",
        action_execution="denied",
        source_store_effect="unchanged",
        original_workspace_effect="unchanged",
        disposable_state_effect="writes",
        supports_timeout=True,
        supports_cancellation=True,
        reports_usage=True,
        provider="test-provider",
        endpoint="https://provider.invalid/inference",
        deviations=("controlled test surface",),
    )

    report = resolve_execution_plan(
        capability,
        _shell_experiment(),
        evidence=_evidence(tmp_path),
        runner_fingerprint="provider-test",
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider="test-provider",
            allowed_endpoint="https://provider.invalid/inference",
        ),
        resource_limits=ResourceLimits(max_provider_calls=1),
    )

    assert report.ready
    assert report.plan is not None
    assert report.plan.capability.provider_transport == "required"
    assert report.plan.capability.provider_calls_per_attempt == 1
    assert report.plan.capability.tool_network_access == "forbidden"
    assert report.plan.capability.action_execution == "denied"
    assert report.plan.capability.disposable_state_effect == "writes"
    assert report.plan.capability.deviations == ("controlled test surface",)


def test_transport_permission_does_not_require_transport_use(tmp_path: Path) -> None:
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider="unused-provider",
        allowed_endpoint="https://provider.invalid/unused",
    )

    report = resolve_execution_plan(
        CapabilityProfile.deterministic_fake(),
        _shell_experiment(),
        evidence=_evidence(tmp_path),
        runner_fingerprint="fake-v1:A,C:bash:pwd",
        safety_policy=policy,
        resource_limits=ResourceLimits.credential_free(),
    )

    assert report.ready
    assert report.plan is not None
    assert report.plan.capability.provider_transport == "forbidden"
    assert report.plan.safety_policy == policy


def test_source_backed_capability_requires_native_format_identity() -> None:
    profile = CapabilityProfile.deterministic_fake().model_dump(mode="python")
    profile.update(
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        reconstruction_method="source-backed-test-v1",
    )

    with pytest.raises(
        ValidationError,
        match="source-backed materialization requires a harness and source schema",
    ):
        CapabilityProfile.model_validate(profile)


def test_preflight_refuses_sampled_runner_before_sampled_calibration_exists(
    tmp_path: Path,
) -> None:
    capability = CapabilityProfile.deterministic_fake().model_copy(update={"deterministic": False})

    report = resolve_execution_plan(
        capability,
        _shell_experiment(),
        evidence=_evidence(tmp_path),
        runner_fingerprint="sampled-test",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=ResourceLimits.credential_free(),
    )

    assert not report.ready
    assert {issue.code for issue in report.issues} == {"sampled-execution-unsupported"}


def test_preflight_refuses_an_unenforceable_input_token_limit(tmp_path: Path) -> None:
    capability = CapabilityProfile.deterministic_fake()
    evidence = _evidence(tmp_path)
    limits = ResourceLimits(max_input_tokens=1000)
    refused = resolve_execution_plan(
        capability,
        _shell_experiment(),
        evidence=evidence,
        runner_fingerprint="fake-v1:A,C:bash:pwd",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=limits,
    )
    accepted = resolve_execution_plan(
        capability.model_copy(update={"input_token_limit_enforcement": "pre-dispatch"}),
        _shell_experiment(),
        evidence=evidence,
        runner_fingerprint="fake-with-token-limit",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=limits,
    )

    assert not refused.ready
    assert {issue.code for issue in refused.issues} == {"input-token-limit-unenforceable"}
    assert accepted.ready
    assert accepted.plan is not None
    assert accepted.plan.capability.input_token_limit_enforcement == "pre-dispatch"


def test_preflight_requires_action_denial_for_final_response(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    capability = CapabilityProfile.deterministic_fake().model_copy(
        update={"action_denial": "unverified"}
    )

    report = resolve_execution_plan(
        capability,
        experiment,
        evidence=evidence,
        runner_fingerprint="unverified-denial",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=ResourceLimits.credential_free(),
    )

    assert not report.ready
    assert {issue.code for issue in report.issues} == {"action-denial-unverified"}


def test_preflight_report_rejects_inconsistent_outcomes() -> None:
    issue = PreflightIssue(code="unsupported-target", message="unsupported")

    with pytest.raises(ValidationError, match="ready preflight requires"):
        PreflightReport(ready=True, plan=None, issues=(issue,))
    with pytest.raises(ValidationError, match="failed preflight requires"):
        PreflightReport(ready=False, plan=None)


def test_capability_contract_is_strict_and_immutable() -> None:
    capability = CapabilityProfile.deterministic_fake()
    field = "deterministic"

    with pytest.raises(ValidationError, match="valid integer"):
        CapabilityProfile.model_validate(
            {**capability.model_dump(), "provider_calls_per_attempt": "0"}
        )
    with pytest.raises(ValidationError, match="frozen"):
        setattr(capability, field, False)


@pytest.mark.parametrize(
    ("plan_changes", "capability_changes", "message"),
    [
        ({"candidate_materialization": "source-backed"}, {}, "materialization"),
        ({}, {"provider_calls_per_attempt": 1}, "provider calls"),
        ({}, {"tool_network_access": "possible"}, "tool network"),
        ({}, {"action_denial": "unverified"}, "action denial"),
        ({}, {"source_store_effect": "may-change"}, "source session"),
        ({}, {"supports_timeout": False}, "timeout and cancellation"),
    ],
)
def test_resolved_plan_rejects_contradictory_execution_facts(
    tmp_path: Path,
    plan_changes: dict[str, object],
    capability_changes: dict[str, object],
    message: str,
) -> None:
    report = resolve_execution_plan(
        CapabilityProfile.deterministic_fake(),
        _shell_experiment(),
        evidence=_evidence(tmp_path),
        runner_fingerprint="fake-v1:A,C:bash:pwd",
        safety_policy=SafetyPolicy.credential_free(),
        resource_limits=ResourceLimits.credential_free(),
    )
    assert report.plan is not None
    payload = {**report.plan.model_dump(mode="python"), **plan_changes}
    payload["capability"] = {
        **report.plan.capability.model_dump(mode="python"),
        **capability_changes,
    }

    with pytest.raises(ValidationError, match=message):
        type(report.plan).model_validate(payload)
