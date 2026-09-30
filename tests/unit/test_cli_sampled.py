"""Sampled CLI orchestration with credential-free, bounded service doubles."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import typer
from typer._click import termui

import ithrl.cli.sampled as sampled
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli.output import ExitCode, OutputFormat
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
from ithrl.experiment import (
    ExperimentDefinition,
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
)
from ithrl.synthetic import extract_inference

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"


@pytest.fixture
def prepared_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CaseWorkspace, CaseEvidence, Any, Path, Path]:
    workspace = extract_inference(
        FIXTURE, inference_call_id="synthetic-ac-001", case_path=tmp_path / "case"
    )
    evidence = workspace.load()
    deterministic = ExperimentDefinition.from_toml(evidence.default_experiment)
    capability = CapabilityProfile.model_validate(
        {
            **CapabilityProfile.deterministic_fake().model_dump(),
            "runner_type": "codex",
            "deterministic": False,
            "harness": "codex",
            "runtime_harness_version": "fixture-1",
            "provider": "fixture-provider",
            "endpoint": "https://example.invalid/responses",
            "provider_calls_per_attempt": 2,
            "provider_transport": "required",
            "cancellation_grace_seconds": 3,
        }
    )
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=deterministic.target,
        oracle=deterministic.oracle,
        runner=NativeRunnerConfig(type="codex", required_capability_digest=capability.digest),
        calibration=SampledCalibrationPhase(required_valid=2, minimum_matches=1, max_attempts=3),
    )
    policy = SafetyPolicy(
        provider_transport="allowed",
        allowed_provider=capability.provider,
        allowed_endpoint=capability.endpoint,
    )
    limits = ResourceLimits(max_provider_calls=2, max_wall_time_seconds=12)
    # Only orchestration uses this double; native selection/preflight have their
    # own contract tests, so no native runtime or model can be launched here.
    prepared = SimpleNamespace(
        experiment=experiment,
        safety_policy=policy,
        resource_limits=limits,
        execution_plan=SimpleNamespace(
            digest="a" * 64,
            capability=capability,
            target_type=experiment.target.type,
            decision_digest=None,
            model_requested="fixture-model",
        ),
    )
    original = tmp_path / "original workspace"
    original.mkdir()
    store = tmp_path / "private" / "observations.sqlite"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return workspace, evidence, prepared, original, store


def _invoke(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    *,
    dry_run: bool = False,
    calibrate_only: bool = True,
    acknowledge_model_execution: bool = False,
    observation_store: Path | None = None,
    output_format: OutputFormat = OutputFormat.JSON,
) -> None:
    workspace, evidence, prepared, original, store = prepared_case
    sampled.handle_sampled_shrink(
        workspace,
        evidence,
        prepared,
        dry_run=dry_run,
        calibrate_only=calibrate_only,
        acknowledge_model_execution=acknowledge_model_execution,
        original_workspace=original,
        observation_store=observation_store if observation_store is not None else store,
        output_format=output_format,
    )


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, Mock]:
    store = Mock(return_value=SimpleNamespace())
    run = Mock(side_effect=AssertionError("calibration must not dispatch"))
    save = Mock(side_effect=AssertionError("results must not be saved"))
    monkeypatch.setattr(sampled, "ObservationStore", store)
    monkeypatch.setattr(sampled, "run_sampled_calibration", run)
    monkeypatch.setattr(sampled, "save_sampled_calibration_result", save)
    return store, run, save


def test_dry_run_has_reproducible_bounds_without_store_or_calibration(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _invoke(prepared_case, dry_run=True, calibrate_only=False)
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == ""
    assert report["schema_version"] == 1
    assert report["kind"] == "sampled"
    assert report["status"] == "ready"
    assert report["max_attempts"] == 3
    assert report["required_valid"] == 2
    assert report["minimum_matches"] == 1
    assert report["max_provider_calls"] == 6
    assert report["max_runner_seconds"] == 45
    assert report["max_tokens"] is None
    assert report["harness_started"] is True
    assert report["provider_call_accounting"] == "not-started"
    assert report["provider_calls"] == report["actions_executed"] == 0
    command = shlex.split(report["next_command"])
    assert command[command.index("--original-workspace") + 1] == str(prepared_case[3])
    assert command[command.index("--observation-store") + 1] == str(prepared_case[4])
    for flag, value in (
        ("--required-valid", "2"),
        ("--minimum-matches", "1"),
        ("--max-attempts", "3"),
        ("--attempt-timeout", "12"),
        ("--max-provider-calls", "6"),
    ):
        assert command[command.index(flag) + 1] == value
    assert "--acknowledge-model-execution" in command
    assert not prepared_case[4].exists()
    assert not (prepared_case[0].root / "results").exists()
    for service in services:
        service.assert_not_called()


@pytest.mark.parametrize("interactive", [False, True])
def test_acknowledgement_required_or_declined_before_store_writes(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interactive: bool,
) -> None:
    monkeypatch.setattr(sampled.sys.stdin, "isatty", lambda: interactive)
    confirm = Mock(return_value=False)
    monkeypatch.setattr(sampled.typer, "confirm", confirm)
    with pytest.raises(typer.Exit) as raised:
        _invoke(prepared_case)
    assert raised.value.exit_code == ExitCode.USAGE
    output = capsys.readouterr()
    preflight = json.loads(output.err)
    report = json.loads(output.out)
    assert preflight["status"] == "ready"
    assert report["error_code"] == (
        "acknowledgement-declined" if interactive else "acknowledgement-required"
    )
    assert report["provider_call_accounting"] == "not-started"
    assert report["provider_calls"] == 0
    assert bool(confirm.call_count) is interactive
    for service in services:
        service.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "status", "exit_code"),
    [
        (RuntimeError("PRIVATE_EXCEPTION"), "incomplete", ExitCode.PROVIDER),
        (KeyboardInterrupt(), "interrupted", ExitCode.CANCELLED),
    ],
)
def test_post_dispatch_failures_never_claim_zero_or_expose_exception_text(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    status: str,
    exit_code: ExitCode,
) -> None:
    services[1].side_effect = failure
    with pytest.raises(typer.Exit) as raised:
        _invoke(prepared_case, acknowledge_model_execution=True)
    assert raised.value.exit_code == exit_code
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == status
    assert report["provider_call_accounting"] == "started-unknown"
    assert report["provider_calls"] is None
    assert "PRIVATE_EXCEPTION" not in output.out + output.err
    services[0].assert_called_once()
    services[1].assert_called_once()
    services[2].assert_not_called()


@pytest.mark.parametrize(
    ("outcome", "stop_reason", "retry_safe", "remaining", "expected_exit", "continuation"),
    [
        ("measurable", "valid-sample-limit", True, True, None, False),
        ("not-measurable", "valid-sample-limit", True, True, ExitCode.INCONCLUSIVE, False),
        ("inconclusive", "valid-sample-limit", True, True, ExitCode.INCONCLUSIVE, False),
        ("refused", "safety-refusal", False, True, ExitCode.UNSUPPORTED, False),
        ("incomplete", "unsafe-retry", False, True, ExitCode.PROVIDER, False),
        ("incomplete", "attempt-limit", True, False, ExitCode.BUDGET, False),
        ("incomplete", "user-interrupt", True, True, ExitCode.CANCELLED, True),
    ],
)
def test_outcomes_have_truthful_accounting_and_only_safe_continuations(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    stop_reason: str,
    retry_safe: bool,
    remaining: bool,
    expected_exit: ExitCode | None,
    continuation: bool,
) -> None:
    observations = (
        SimpleNamespace(valid=True, attempt_evidence=SimpleNamespace(automatic_retry_safe=False)),
        SimpleNamespace(
            valid=False, attempt_evidence=SimpleNamespace(automatic_retry_safe=retry_safe)
        ),
    )
    result = SimpleNamespace(observations=observations)
    summary = SimpleNamespace(
        provider_calls_started=2,
        provider_calls_may_have_started=2,
        stop_reason=stop_reason,
        outcome=outcome,
        observations_recorded=2 if remaining else 3,
        matches=1,
        no_matches=0,
        invalid_observations=0,
        runner_errors=1,
        actions_executed=0,
        elapsed_milliseconds=1200,
    )
    services[1].side_effect = None
    services[1].return_value = result
    services[2].side_effect = None
    services[2].return_value = SimpleNamespace(
        summary=summary, root=prepared_case[0].root / "results/fixture"
    )
    if expected_exit is None:
        _invoke(prepared_case, acknowledge_model_execution=True)
    else:
        with pytest.raises(typer.Exit) as raised:
            _invoke(prepared_case, acknowledge_model_execution=True)
        assert raised.value.exit_code == expected_exit
    output = capsys.readouterr()
    assert json.loads(output.err)["status"] == "ready"
    report = json.loads(output.out)
    assert report["status"] == ("interrupted" if stop_reason == "user-interrupt" else outcome)
    assert report["provider_call_accounting"] == "accounted"
    assert report["provider_calls"] == 2
    assert report["attempts_used"] == summary.observations_recorded
    assert bool(report["next_command"]) is continuation
    services[2].assert_called_once()


@pytest.mark.parametrize("relative", ["case", "original", "native-attempts", "native-output"])
def test_observation_storage_cannot_overlap_protected_or_disposable_state(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    tmp_path: Path,
    relative: str,
) -> None:
    workspace, _, _, original, _ = prepared_case
    roots = {
        "case": workspace.root,
        "original": original,
        "native-attempts": tmp_path / "data/ithrl/native-attempts",
        "native-output": tmp_path / "data/ithrl/native-output",
    }
    with pytest.raises(ValueError, match="overlaps protected state"):
        sampled._observation_store_path(workspace, original, roots[relative] / "store.sqlite")


@pytest.mark.parametrize(
    "suffix", ["", ".calibration.lock", ".calibration.inflight", "-wal", "-shm", "-journal"]
)
def test_store_lifecycle_hardlinks_refused_before_ack_or_write(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    store = prepared_case[4]
    store.parent.mkdir()
    source = tmp_path / "other-file"
    source.write_text("protected content")
    os.link(source, store.with_name(store.name + suffix))
    with pytest.raises(typer.Exit) as raised:
        _invoke(prepared_case, acknowledge_model_execution=True)
    assert raised.value.exit_code == ExitCode.UNSUPPORTED
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "sampled-preflight-invalid"
    assert report["provider_call_accounting"] == "not-started"
    assert source.read_text() == "protected content"
    for service in services:
        service.assert_not_called()


@pytest.mark.parametrize("stage", ["preflight", "render", "store"])
def test_ctrl_c_before_dispatch_is_sampled_and_explicitly_unstarted(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    if stage == "preflight":
        monkeypatch.setattr(sampled, "_preflight_report", Mock(side_effect=KeyboardInterrupt()))
    elif stage == "render":
        monkeypatch.setattr(sampled, "render_sampled", Mock(side_effect=KeyboardInterrupt()))
    else:
        services[0].side_effect = KeyboardInterrupt()
    with pytest.raises(typer.Exit) as raised:
        _invoke(prepared_case, acknowledge_model_execution=True)
    assert raised.value.exit_code == ExitCode.CANCELLED
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["kind"] == "sampled"
    assert report["status"] == "interrupted"
    assert report["provider_call_accounting"] == "not-started"
    assert report["provider_calls"] == 0
    services[1].assert_not_called()
    services[2].assert_not_called()


@pytest.mark.parametrize("unsafe", ["store-symlink", "parent-symlink", "directory", "dotdot"])
def test_unsafe_store_paths_refused_without_writes(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unsafe: str,
) -> None:
    store = prepared_case[4]
    target = tmp_path / "other"
    target.mkdir()
    if unsafe == "parent-symlink":
        store.parent.symlink_to(target, target_is_directory=True)
    elif unsafe == "store-symlink":
        store.parent.mkdir()
        store.symlink_to(target / "untouched.sqlite")
    elif unsafe == "directory":
        store.mkdir(parents=True)
    else:
        store = tmp_path / "private/../other/store.sqlite"
    with pytest.raises(typer.Exit):
        _invoke(prepared_case, observation_store=store, acknowledge_model_execution=True)
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "sampled-preflight-invalid"
    assert report["provider_call_accounting"] == "not-started"
    assert list(target.iterdir()) == []
    for service in services:
        service.assert_not_called()


def test_human_dry_run_distinguishes_probe_from_provider_calls(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _invoke(prepared_case, dry_run=True, calibrate_only=False, output_format=OutputFormat.HUMAN)
    output = capsys.readouterr().out
    assert "harness started True" in output
    assert "provider calls  0 (execution did not start)" in output
    assert "token bound     unknown" in output
    assert "requested model fixture-model" in output
    assert "actions-denied" in output
    for service in services:
        service.assert_not_called()


@pytest.mark.parametrize("accounted", [None, 7])
def test_ambiguous_or_overbound_summary_cannot_be_rendered_as_known_calls(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    capsys: pytest.CaptureFixture[str],
    accounted: int | None,
) -> None:
    services[1].side_effect = None
    services[1].return_value = SimpleNamespace(observations=())
    services[2].side_effect = None
    services[2].return_value = SimpleNamespace(
        root=prepared_case[0].root / "results/fixture",
        summary=SimpleNamespace(
            provider_calls_started=accounted,
            provider_calls_may_have_started=7,
            outcome="incomplete",
            stop_reason="unsafe-retry",
            observations_recorded=3,
            matches=0,
            no_matches=0,
            invalid_observations=0,
            runner_errors=3,
            actions_executed=None,
            elapsed_milliseconds=None,
        ),
    )
    with pytest.raises(typer.Exit):
        _invoke(prepared_case, acknowledge_model_execution=True)
    report = json.loads(capsys.readouterr().out)
    assert report["provider_call_accounting"] == "started-unknown"
    assert report["provider_calls"] is None
    assert report["next_command"] is None


def test_actual_typer_confirmation_ctrl_c_is_interrupted_not_declined(
    prepared_case: tuple[CaseWorkspace, CaseEvidence, Any, Path, Path],
    services: tuple[Mock, Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sampled.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(termui, "visible_prompt_func", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(typer.Exit) as raised:
        _invoke(prepared_case)
    assert raised.value.exit_code == ExitCode.CANCELLED
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "interrupted"
    assert report["kind"] == "sampled"
    assert report["provider_call_accounting"] == "not-started"
    assert report["provider_calls"] == 0
    for service in services:
        service.assert_not_called()
