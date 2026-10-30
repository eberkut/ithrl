"""Credential-free tests of native CLI selection, never installed harnesses."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import ithrl.cli.native as native
from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli import ExitCode, app
from ithrl.execution import CapabilityProfile, calibration_budget_bounds
from ithrl.experiment import (
    ExperimentDefinition,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledExperimentDefinition,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
)
from ithrl.extraction import extract_decision

FIXTURE = Path(__file__).parents[1] / "fixtures/codex-session/rollout.jsonl"
PHASE = SampledCalibrationPhase(required_valid=2, minimum_matches=1, max_attempts=3)
_EXECUTABLE = native._executable


@pytest.fixture
def native_case(tmp_path: Path) -> tuple[CaseWorkspace, CaseEvidence, Path]:
    session = parse_session(FIXTURE)
    decision = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    original = tmp_path / "original"
    original.mkdir()
    return workspace, workspace.load(), original


@pytest.fixture
def profile() -> CapabilityProfile:
    return CapabilityProfile(
        runner_type="codex",
        runner_version="1",
        harness="codex",
        runtime_harness_version="fixture",
        protocol_version="fixture-schema",
        source_schema="codex-rollout-jsonl",
        deterministic=False,
        supported_capture_types=("logical-session",),
        supported_materializations=("source-backed",),
        supported_context_operations=("dependency-closed-removal",),
        reconstruction_method="codex-app-server-inject-items-v1",
        supported_targets=("final-response",),
        provider_transport="required",
        provider_calls_per_attempt=2,
        tool_network_access="forbidden",
        action_visibility="not-applicable",
        action_denial="enforced",
        action_execution="denied",
        source_store_effect="unchanged",
        original_workspace_effect="unchanged",
        protected_state_enforcement="preventive",
        disposable_state_effect="writes",
        disposable_state_limit_enforcement="enforced",
        process_containment="process-group",
        supports_timeout=True,
        supports_cancellation=True,
        cancellation_grace_seconds=3,
        reports_usage=True,
        provider="fixture-provider",
        endpoint="https://example.invalid/responses",
    )


@pytest.fixture
def mocked_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: CapabilityProfile
) -> tuple[Mock, Mock]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    def executable(name: str) -> Path:
        return tmp_path / name

    monkeypatch.setattr(native, "_executable", executable)
    monkeypatch.setattr(native, "CodexRuntime", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(
        native,
        "resolve_codex_configuration",
        Mock(return_value=SimpleNamespace(authentication="openai-chatgpt-file")),
    )
    detector = Mock(return_value=profile)
    constructor = Mock(
        return_value=SimpleNamespace(capability_profile=profile, fingerprint="fixture-native-v1")
    )
    monkeypatch.setattr(native, "detect_codex_capability", detector)
    monkeypatch.setattr(native, "CodexRunner", constructor)
    return detector, constructor


def test_ordinary_case_resolves_explicit_samples_without_rewriting_evidence(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    profile: CapabilityProfile,
) -> None:
    workspace, evidence, original = native_case
    original_experiment = (workspace.root / "experiments/default.toml").read_bytes()
    selection = native.native_selection(
        workspace,
        evidence,
        original_workspace=original,
        calibration=PHASE,
        attempt_timeout_seconds=12,
        max_provider_calls=6,
    )

    assert selection.experiment.schema_version == 1
    assert selection.experiment.kind == "sampled"
    assert selection.experiment.calibration == PHASE
    assert selection.experiment.reduction is None
    assert isinstance(selection.experiment.runner, NativeRunnerConfig)
    assert selection.experiment.runner.required_capability_digest == profile.digest
    bounds = calibration_budget_bounds(PHASE, profile, selection.resource_limits)
    assert (bounds.attempts, bounds.provider_calls, bounds.runner_seconds) == (3, 6, 45)
    assert bounds.input_tokens is None
    assert selection.safety_policy.allowed_endpoint == profile.endpoint
    assert (workspace.root / "experiments/default.toml").read_bytes() == original_experiment
    assert workspace.load().digest == evidence.digest
    detector, constructor = mocked_runtime
    detector.assert_called_once()
    assert detector.call_args.kwargs["max_provider_calls_per_attempt"] == 16
    constructor.assert_called_once()
    assert constructor.call_args.kwargs["source_paths"][0].path == workspace.root
    assert constructor.call_args.kwargs["workspace_paths"][0].path == original
    assert selection.registry.resolve(selection.experiment.runner, evidence).issue is None
    wrong = selection.experiment.runner.model_copy(update={"required_capability_digest": "0" * 64})
    assert selection.registry.resolve(wrong, evidence).issue is not None


@pytest.mark.parametrize(
    ("missing_samples", "missing_workspace", "provider_cap", "input_cap", "expected"),
    [
        (True, False, None, None, "calibration-settings-required"),
        (False, True, None, None, "original-workspace-required"),
        (False, False, 5, None, "provider-call-limit-too-small"),
        (False, False, None, 100, "input-token-limit-unenforceable"),
    ],
)
def test_native_selection_refuses_missing_inputs_and_unenforceable_budgets(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    missing_samples: bool,
    missing_workspace: bool,
    provider_cap: int | None,
    input_cap: int | None,
    expected: str,
) -> None:
    workspace, evidence, original = native_case
    with pytest.raises(native.NativeSelectionError) as raised:
        native.native_selection(
            workspace,
            evidence,
            original_workspace=None if missing_workspace else original,
            calibration=None if missing_samples else PHASE,
            max_provider_calls=provider_cap,
            max_input_tokens=input_cap,
        )
    assert raised.value.code == expected
    if expected.endswith("required"):
        mocked_runtime[0].assert_not_called()
    else:
        assert raised.value.harness_started is True


def test_persisted_sampled_experiment_requires_exact_detected_profile(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path], mocked_runtime: tuple[Mock, Mock]
) -> None:
    workspace, evidence, original = native_case
    persisted = ExperimentDefinition.from_toml(evidence.default_experiment)
    sampled = SampledExperimentDefinition(
        schema_version=1,
        target=persisted.target,
        oracle=persisted.oracle,
        runner=NativeRunnerConfig(type="codex", required_capability_digest="0" * 64),
        calibration=PHASE,
    )
    case = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=evidence.context,
        default_experiment=sampled.to_toml(),
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    with pytest.raises(native.NativeSelectionError) as raised:
        native.native_selection(workspace, case, original_workspace=original)
    assert raised.value.code == "capability-profile-mismatch"
    assert raised.value.harness_started is True
    mocked_runtime[0].assert_called_once()
    mocked_runtime[1].assert_not_called()


def _reduction_experiment(
    evidence: CaseEvidence, profile: CapabilityProfile
) -> SampledExperimentDefinition:
    persisted = ExperimentDefinition.from_toml(evidence.default_experiment)
    return SampledExperimentDefinition(
        target=persisted.target,
        oracle=persisted.oracle,
        runner=NativeRunnerConfig(type="codex", required_capability_digest=profile.digest),
        calibration=PHASE,
        reduction=SampledReductionPlan(
            search=SampledSearchPhase(
                original=SamplingArmTarget(required_valid=4, max_attempts=8),
                candidate=SamplingArmTarget(required_valid=5, max_attempts=9),
            ),
            confirmation=SampledConfirmationPhase(
                original=SamplingArmTarget(required_valid=20, max_attempts=24),
                candidate=SamplingArmTarget(required_valid=21, max_attempts=25),
                one_sided_confidence=0.95,
            ),
            margin=0.2,
            freshness_seconds=600,
        ),
    )


def test_native_selection_preserves_complete_persisted_reduction(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    profile: CapabilityProfile,
) -> None:
    workspace, evidence, original = native_case
    sampled = _reduction_experiment(evidence, profile)
    case = CaseEvidence(
        manifest=evidence.manifest,
        request=None,
        context=evidence.context,
        default_experiment=sampled.to_toml(),
        source_artifacts=evidence.source_artifacts,
        decision=evidence.decision,
    )
    selection = native.native_selection(workspace, case, original_workspace=original)
    assert selection.experiment == sampled
    assert selection.experiment.digest == sampled.digest
    assert selection.experiment.reduction == sampled.reduction
    assert selection.registry.resolve(selection.experiment.runner, case).issue is None
    mocked_runtime[0].assert_called_once()


def test_explicit_experiment_preserves_reduction_with_calibration_override(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    profile: CapabilityProfile,
) -> None:
    workspace, evidence, original = native_case
    sampled = _reduction_experiment(evidence, profile)
    phase = SampledCalibrationPhase(required_valid=3, minimum_matches=2, max_attempts=5)
    original_toml = (workspace.root / "experiments/default.toml").read_bytes()
    selection = native.native_selection(
        workspace,
        evidence,
        original_workspace=original,
        experiment=sampled,
        calibration=phase,
        max_provider_calls=10,
    )
    expected = sampled.model_copy(update={"calibration": phase})
    assert selection.experiment == expected
    assert selection.experiment.digest == expected.digest
    assert selection.experiment.reduction == sampled.reduction
    assert sampled.calibration == PHASE
    assert selection.registry.resolve(selection.experiment.runner, evidence).issue is None
    assert calibration_budget_bounds(phase, profile, selection.resource_limits).provider_calls == 10
    assert (workspace.root / "experiments/default.toml").read_bytes() == original_toml
    mocked_runtime[0].assert_called_once()


def test_explicit_experiment_still_requires_its_capability_pin(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    profile: CapabilityProfile,
) -> None:
    workspace, evidence, original = native_case
    sampled = _reduction_experiment(evidence, profile).model_copy(
        update={"runner": NativeRunnerConfig(type="codex", required_capability_digest="0" * 64)}
    )
    with pytest.raises(native.NativeSelectionError) as raised:
        native.native_selection(
            workspace, evidence, original_workspace=original, experiment=sampled
        )
    assert raised.value.code == "capability-profile-mismatch"
    mocked_runtime[0].assert_called_once()
    mocked_runtime[1].assert_not_called()


def test_explicit_experiment_cannot_change_captured_harness(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    profile: CapabilityProfile,
) -> None:
    workspace, evidence, original = native_case
    sampled = _reduction_experiment(evidence, profile).model_copy(
        update={
            "runner": NativeRunnerConfig(type="copilot", required_capability_digest=profile.digest)
        }
    )
    with pytest.raises(native.NativeSelectionError) as raised:
        native.native_selection(
            workspace, evidence, original_workspace=original, experiment=sampled
        )
    assert raised.value.code == "runner-evidence-mismatch"
    mocked_runtime[0].assert_not_called()


@pytest.mark.parametrize(
    "options", [[], ["--max-candidates", "1"], ["--dry-run", "--calibrate-only"]]
)
def test_unsupported_native_modes_refuse_before_detection(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    options: list[str],
) -> None:
    workspace, _, _ = native_case
    result = CliRunner().invoke(app, ["shrink", str(workspace.root), *options, "--format", "json"])
    assert result.exit_code == ExitCode.UNSUPPORTED, result.output
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["kind"] == "sampled"
    assert report["provider_call_accounting"] == "not-started"
    assert report["harness_started"] is False
    mocked_runtime[0].assert_not_called()


def test_missing_runtime_is_content_safe_and_never_probes(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, evidence, original = native_case
    monkeypatch.setattr(native, "_executable", _EXECUTABLE)
    monkeypatch.setattr(native.shutil, "which", Mock(return_value=None))
    with pytest.raises(native.NativeSelectionError) as raised:
        native.native_selection(workspace, evidence, original_workspace=original, calibration=PHASE)
    assert raised.value.code == "runtime-unavailable"
    assert str(original) not in str(raised.value)
    mocked_runtime[0].assert_not_called()


def test_native_storage_overlap_refuses_before_probe(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, evidence, original = native_case
    monkeypatch.setattr(native, "user_data_path", Mock(return_value=original / "private"))
    with pytest.raises(native.NativeSelectionError) as raised:
        native.native_selection(workspace, evidence, original_workspace=original, calibration=PHASE)
    assert raised.value.code == "storage-overlap"
    mocked_runtime[0].assert_not_called()


def test_native_dry_run_resolves_bounds_without_dispatch_or_case_results(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path], mocked_runtime: tuple[Mock, Mock]
) -> None:
    workspace, _, original = native_case
    result = CliRunner().invoke(
        app,
        [
            "shrink",
            str(workspace.root),
            "--dry-run",
            "--original-workspace",
            str(original),
            "--required-valid",
            "2",
            "--minimum-matches",
            "1",
            "--max-attempts",
            "3",
            "--attempt-timeout",
            "12",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["kind"] == "sampled"
    assert report["status"] == "ready"
    assert report["max_provider_calls"] == 6
    assert report["max_provider_calls_per_attempt"] == 2
    assert "--max-provider-calls-per-attempt 2" in report["next_command"]
    assert report["max_runner_seconds"] == 45
    assert report["provider_calls"] == report["attempts_used"] == report["actions_executed"] == 0
    assert report["provider_call_accounting"] == "not-started"
    assert not (workspace.root / "results").exists()
    mocked_runtime[0].assert_called_once()


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ([], "calibration-settings-required"),
        (["--required-valid", "2"], "calibration-settings-required"),
        (
            ["--required-valid", "2", "--minimum-matches", "3", "--max-attempts", "3"],
            "calibration-settings-invalid",
        ),
    ],
)
def test_cli_does_not_invent_samples_or_probe_invalid_counts(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    counts: list[str],
    expected: str,
) -> None:
    workspace, _, original = native_case
    result = CliRunner().invoke(
        app,
        [
            "shrink",
            str(workspace.root),
            "--dry-run",
            "--original-workspace",
            str(original),
            *counts,
            "--format",
            "json",
        ],
    )
    report = json.loads(result.stdout)
    assert report["error_code"] == expected
    assert report["harness_started"] is False
    assert report["provider_call_accounting"] == "not-started"
    mocked_runtime[0].assert_not_called()


@pytest.mark.parametrize("target", ["final-response", "shell-action"])
def test_copilot_selector_passes_exact_target_and_detected_profile(
    tmp_path: Path,
    profile: CapabilityProfile,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    copilot_profile = profile.model_copy(update={"runner_type": "copilot", "harness": "copilot"})
    detected = SimpleNamespace(profile=copilot_profile)
    detector = Mock(return_value=detected)
    constructor = Mock(return_value=SimpleNamespace(capability_profile=copilot_profile))
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))

    def executable(name: str) -> Path:
        return tmp_path / name

    monkeypatch.setattr(native, "_executable", executable)
    monkeypatch.setattr(native, "CopilotRuntime", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(
        native,
        "resolve_copilot_configuration",
        Mock(return_value=SimpleNamespace(authentication="managed-config")),
    )
    monkeypatch.setattr(native, "detect_copilot_capability", detector)
    monkeypatch.setattr(native, "CopilotRunner", constructor)

    runner = native._copilot_runner(
        copilot_profile.digest,
        target,
        state_parent=tmp_path / "state",
        output_root=tmp_path / "output",
        sources=(),
        workspaces=(),
    )
    assert runner.capability_profile.digest == copilot_profile.digest
    assert detector.call_args.kwargs == {"target": target}
    assert constructor.call_args.kwargs["detected_capability"] is detected

    with pytest.raises(native.NativeSelectionError) as raised:
        native._copilot_runner(
            "0" * 64,
            target,
            state_parent=tmp_path / "state",
            output_root=tmp_path / "output",
            sources=(),
            workspaces=(),
        )
    assert raised.value.code == "capability-profile-mismatch"
    assert raised.value.harness_started is True
    constructor.assert_called_once()


@pytest.mark.parametrize("limit", [1, 7, 16])
def test_selected_per_attempt_limit_is_detected_and_used_before_total_budget_admission(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    profile: CapabilityProfile,
    limit: int,
) -> None:
    workspace, evidence, original = native_case
    detector, constructor = mocked_runtime
    selected = profile.model_copy(update={"provider_calls_per_attempt": limit})
    detector.return_value = selected
    constructor.return_value = SimpleNamespace(capability_profile=selected)
    selection = native.native_selection(
        workspace,
        evidence,
        original_workspace=original,
        calibration=PHASE,
        max_provider_calls_per_attempt=limit,
        max_provider_calls=3 * limit,
    )
    assert detector.call_args.kwargs["max_provider_calls_per_attempt"] == limit
    assert selection.resource_limits.max_provider_calls == limit
    assert isinstance(selection.experiment.runner, NativeRunnerConfig)
    assert selection.experiment.runner.required_capability_digest == selected.digest
    with pytest.raises(native.NativeSelectionError) as refused:
        native.native_selection(
            workspace,
            evidence,
            original_workspace=original,
            calibration=PHASE,
            max_provider_calls_per_attempt=limit,
            max_provider_calls=3 * limit - 1,
        )
    assert refused.value.code == "provider-call-limit-too-small"


@pytest.mark.parametrize("limit", [0, 17])
@pytest.mark.parametrize("command", ["shrink", "fuzz"])
def test_cli_per_attempt_limit_bounds_refuse_before_detection(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    mocked_runtime: tuple[Mock, Mock],
    limit: int,
    command: str,
) -> None:
    workspace, _, _ = native_case
    result = CliRunner().invoke(
        app, [command, str(workspace.root), "--max-provider-calls-per-attempt", str(limit)]
    )
    assert result.exit_code == ExitCode.USAGE
    mocked_runtime[0].assert_not_called()


@pytest.mark.parametrize("limit", [1, 2])
def test_detected_copilot_per_attempt_limit_is_preserved(
    native_case: tuple[CaseWorkspace, CaseEvidence, Path],
    profile: CapabilityProfile,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    workspace, evidence, original = native_case
    copilot_evidence = Mock(
        manifest=SimpleNamespace(harness="copilot"),
        source_artifacts=evidence.source_artifacts,
        default_experiment=evidence.default_experiment,
    )
    detected = profile.model_copy(
        update={"runner_type": "copilot", "harness": "copilot", "provider_calls_per_attempt": limit}
    )
    monkeypatch.setattr(
        native, "_copilot_runner", Mock(return_value=SimpleNamespace(capability_profile=detected))
    )
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=ExperimentDefinition.from_toml(evidence.default_experiment).target,
        oracle=ExperimentDefinition.from_toml(evidence.default_experiment).oracle,
        runner=NativeRunnerConfig(type="copilot", required_capability_digest=detected.digest),
        calibration=PHASE,
    )
    selection = native.native_selection(
        workspace,
        copilot_evidence,
        experiment=experiment,
        original_workspace=original,
        max_provider_calls_per_attempt=limit,
    )
    assert selection.resource_limits.max_provider_calls == limit
    with pytest.raises(native.NativeSelectionError) as refused:
        native.native_selection(
            workspace,
            copilot_evidence,
            experiment=experiment,
            original_workspace=original,
            max_provider_calls_per_attempt=3 - limit,
        )
    assert refused.value.code == "unsupported-provider-call-limit"
