from __future__ import annotations

import pytest
from pydantic import ValidationError

from ithrl.behavior import ShellActionTarget
from ithrl.execution import (
    CapabilityProfile,
    PreflightIssue,
    PreflightReport,
    resolve_execution_plan,
)
from ithrl.experiment import (
    DeterministicPhase,
    ExperimentDefinition,
    FakeRunnerConfig,
    StructuralOracleConfig,
)


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


def test_fake_capability_resolves_offline_denied_execution_plan() -> None:
    experiment = _shell_experiment()
    capability = CapabilityProfile.deterministic_fake()

    report = resolve_execution_plan(
        capability,
        experiment,
        runner_fingerprint="fake-v1:A,C:bash:pwd",
    )

    assert report.ready
    assert report.issues == ()
    assert report.network_calls_started == 0
    assert report.provider_calls_started == 0
    assert report.actions_executed == 0
    assert report.plan is not None
    assert report.plan.network_calls_planned == 0
    assert report.plan.provider_calls_planned == 0
    assert report.plan.action_execution == "denied"
    assert report.plan.source_store_effect == "unchanged"
    assert report.plan.target_digest == experiment.target.digest
    assert report.plan.experiment_digest == experiment.digest
    assert report.plan.capability_digest == capability.digest
    assert len(report.plan.digest) == 64


def test_preflight_collects_every_missing_credential_free_guarantee() -> None:
    capability = CapabilityProfile(
        runner_type="other",
        runner_version="1",
        deterministic=False,
        supported_targets=("final-response",),
        network_access="required",
        provider_calls_required=1,
        action_execution="possible",
        source_store_effect="may-write",
    )

    report = resolve_execution_plan(
        capability,
        _shell_experiment(),
        runner_fingerprint="other-v1",
    )

    assert not report.ready
    assert report.plan is None
    assert {issue.code for issue in report.issues} == {
        "runner-type-mismatch",
        "unsupported-target",
        "non-deterministic-runner",
        "network-not-forbidden",
        "provider-calls-required",
        "action-execution-not-denied",
        "source-store-may-change",
    }
    assert report.network_calls_started == 0
    assert report.provider_calls_started == 0
    assert report.actions_executed == 0


def test_preflight_report_rejects_inconsistent_outcomes() -> None:
    issue = PreflightIssue(
        code="unsupported-target",
        message="unsupported",
    )

    with pytest.raises(ValidationError, match="ready preflight requires"):
        PreflightReport(ready=True, plan=None, issues=(issue,))
    with pytest.raises(ValidationError, match="failed preflight requires"):
        PreflightReport(ready=False, plan=None)


def test_capability_contract_is_strict_and_immutable() -> None:
    capability = CapabilityProfile.deterministic_fake()
    field = "deterministic"

    with pytest.raises(ValidationError, match="valid integer"):
        CapabilityProfile.model_validate(
            {**capability.model_dump(), "provider_calls_required": "0"}
        )
    with pytest.raises(ValidationError, match="frozen"):
        setattr(capability, field, False)
