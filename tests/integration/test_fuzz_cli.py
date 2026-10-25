"""CLI walkthroughs run real collection and persistence without native/provider access."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ithrl.cli import app
from ithrl.cli.fuzz_session import FuzzSessionError, load_fuzz_session
from ithrl.observations import ObservationStore
from ithrl.run_state import SampledRunStateFile
from ithrl.runner import PreparedAttempt, RunResult
from ithrl.synthetic import extract_inference
from native_support.smoke import protected_tree

CONFIGURATION = """seed = 1
mutators = ["append-stale-text"]
max_candidates = 4
max_simplification_trials = 4
minimum_decrease = 0.1
eligibility_policy = "independent-safe-preaccept-only"
freshness_seconds = 86400
stale_text = "PRIVATE-STRESS-TEXT"
[calibration]
required_valid = 2
minimum_matches = 2
max_attempts = 2
[screening]
minimum_decrease = 0.4
[screening.original]
required_valid = 2
max_attempts = 2
[screening.mutant]
required_valid = 2
max_attempts = 2
[confirmation]
alpha = 0.05
[confirmation.original]
required_valid = 20
max_attempts = 20
[confirmation.mutant]
required_valid = 20
max_attempts = 20
[limits]
max_provider_calls = 0
max_wall_time_seconds = 10000
attempt_timeout_seconds = 1
"""


def inputs(tmp_path: Path) -> list[str]:
    extract_inference(
        Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    )
    config = tmp_path / "fuzz.toml"
    config.write_text(CONFIGURATION)
    config.chmod(0o600)
    return [
        "fuzz",
        str(tmp_path / "case"),
        "--config",
        str(config),
        "--observation-store",
        str(tmp_path / "private" / "observations.sqlite"),
        "--format",
        "json",
    ]


def _protected_inputs(root: Path):
    case = protected_tree(root / "case", allow_case_results=True)
    # Canonical run state and result receipts are the command's owned outputs.
    case = {path: value for path, value in case.items() if path.parts[:1] != (".runs",)}
    return case, protected_tree(root / "original")


def test_fake_dry_run_has_no_execution_or_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dry-run attempted execution or native detection")

    monkeypatch.setattr("ithrl.cli.fuzz.native_selection", forbidden)
    monkeypatch.setattr("ithrl.runner.FakeRunner.run", forbidden)
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "ready"
    assert report["harness_started"] is False
    assert report["planned_provider_calls"] == 0
    assert report["stress_text_bytes"] == len("PRIVATE-STRESS-TEXT")
    assert "PRIVATE-STRESS-TEXT" not in result.output
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "case/.runs").exists()


def test_fake_campaign_saved_rates_and_terminal_resume(tmp_path: Path) -> None:
    args = inputs(tmp_path)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "confirmed-change", report
    assert report["confirmation_original"]["matches"] == 20
    assert report["confirmation_mutant"]["matches"] == 0
    assert report["difference"] == 1.0
    assert report["lower_bound"] > 0.1
    assert report["provider_calls"] == 0
    assert report["usage"]["charged_provider_calls"] == 0
    assert report["actions_executed"] == 0
    assert "PRIVATE-STRESS-TEXT" not in result.output
    receipt = Path(report["result_path"])
    saved = json.loads((receipt / "summary.json").read_text())
    assert saved["confirmation_original"]["match_rate"] == 1.0
    assert saved["confirmation_mutant"]["match_rate"] == 0.0
    session = load_fuzz_session(receipt)
    store = ObservationStore(session.observation_store)
    state = SampledRunStateFile.open(session.run_directory)
    with state.acquire_lock(store):
        before = state.snapshot(store, now=datetime.now(UTC))
    resumed = CliRunner().invoke(app, ["fuzz", str(receipt), "--resume", "--format", "json"])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["status"] == "confirmed-change"
    with state.acquire_lock(store):
        assert state.snapshot(store, now=datetime.now(UTC)).state == before.state


def mock_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fuzz_support import FuzzRunner
    from ithrl.behavior import FinalResponseTarget
    from ithrl.cli.native import NativeSelection
    from ithrl.execution import ResourceLimits, SafetyPolicy
    from ithrl.experiment import (
        NativeRunnerConfig,
        SampledExperimentDefinition,
        StructuralOracleConfig,
    )
    from ithrl.fuzz.configuration import FuzzConfiguration
    from ithrl.runner import RunnerRegistry

    args = inputs(tmp_path)
    runner = FuzzRunner()
    config_path = tmp_path / "fuzz.toml"
    config_path.write_text(
        CONFIGURATION.replace("max_provider_calls = 0", "max_provider_calls = 500")
    )
    config = FuzzConfiguration.from_toml(config_path.read_text())
    experiment = SampledExperimentDefinition(
        schema_version=1,
        target=FinalResponseTarget(text_equals="BLUE"),
        oracle=StructuralOracleConfig(),
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=config.calibration,
    )
    (tmp_path / "case/experiments/default.toml").write_text(experiment.to_toml())
    selection = NativeSelection(
        registry=RunnerRegistry({"codex": lambda _c, _e: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed",
            allowed_provider="synthetic-provider",
            allowed_endpoint="https://provider.example.invalid",
        ),
        resource_limits=ResourceLimits(max_provider_calls=1, max_wall_time_seconds=1),
        experiment=experiment,
    )
    detections = []

    def detect(*_args: object, **_kwargs: object):
        from dataclasses import replace

        detections.append(True)
        requested = _kwargs.get("experiment")
        assert isinstance(requested, SampledExperimentDefinition)
        return replace(
            selection,
            experiment=requested.model_copy(
                update={
                    "runner": NativeRunnerConfig(
                        type="codex", required_capability_digest=runner.capability_profile.digest
                    )
                }
            ),
        )

    monkeypatch.setattr("ithrl.cli.fuzz.native_selection", detect)
    original = tmp_path / "original"
    original.mkdir(mode=0o700)
    (original / "protected.txt").write_text("unchanged\n")
    return [*args, "--original-workspace", str(original)], runner, detections


def test_native_detection_consent_and_saved_profile_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, runner, detections = mock_native(tmp_path, monkeypatch)
    protected = _protected_inputs(tmp_path)
    preflight = CliRunner().invoke(app, [*args, "--dry-run"])
    assert preflight.exit_code == 0, preflight.output
    assert json.loads(preflight.stdout)["status"] == "detection-required"
    assert not detections
    assert not runner.calls
    profile = tmp_path / "profile.json"
    detected = CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)])
    assert detected.exit_code == 0, detected.output
    assert len(detections) == 1
    assert not runner.calls
    report = json.loads(detected.stdout)
    assert report["provider"] == "synthetic-provider"
    assert profile.stat().st_mode & 0o777 == 0o600
    args += ["--profile", str(profile)]
    dry = CliRunner().invoke(app, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["capability_source"] == "saved-detection"
    assert len(detections) == 1
    assert not runner.calls
    refused = CliRunner().invoke(app, args)
    assert json.loads(refused.stdout)["error_code"] == "model-execution-not-acknowledged"
    assert len(detections) == 1
    assert not runner.calls
    result = CliRunner().invoke(app, [*args, "--acknowledge-model-execution"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["status"] == "confirmed-change", output
    assert output["usage"]["charged_provider_calls"] == len(runner.calls)
    assert output["provider_calls"] == len(runner.calls)
    assert len(detections) == 2
    assert "PRIVATE-STRESS-TEXT" not in result.output
    before = len(runner.calls)
    saved = CliRunner().invoke(
        app, ["fuzz", output["result_path"], "--resume", "--dry-run", "--format", "json"]
    )
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["capability_source"] == "saved-detection"
    assert json.loads(saved.stdout)["harness_started"] is False
    assert len(detections) == 2
    assert len(runner.calls) == before
    assert protected == _protected_inputs(tmp_path)


@pytest.mark.parametrize(
    ("outcome", "failure_phase"),
    [
        ("inconclusive", None),
        ("incomplete", "discovery"),
        ("incomplete", "confirmation"),
        ("no-candidate-found", None),
    ],
)
def test_mock_native_terminal_outcomes_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, failure_phase: str | None
) -> None:
    args, runner, _ = mock_native(tmp_path, monkeypatch)
    if outcome == "no-candidate-found":
        runner.mutant_matches = True
    elif outcome == "inconclusive":
        # Exploration still sees an effect. Only the fresh holdout loses it.
        original_run = runner.run

        async def changed(attempt: PreparedAttempt) -> RunResult:
            if attempt.phase == "confirmation":
                runner.mutant_matches = True
            return await original_run(attempt)

        monkeypatch.setattr(runner, "run", changed)
    else:
        original_run = runner.run

        async def failed(attempt: PreparedAttempt) -> RunResult:
            if attempt.phase == failure_phase:
                runner.fail = True
            return await original_run(attempt)

        monkeypatch.setattr(runner, "run", failed)
    profile = tmp_path / "profile.json"
    detected = CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)])
    assert detected.exit_code == 0, detected.output
    result = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    output = json.loads(result.stdout)
    assert output["status"] == outcome, output
    if outcome == "incomplete":
        assert output["execution_reliability"]["runner_errors"] == 1
        assert output["confirmation_original"]["no_matches"] == 0
        assert output["confirmation_mutant"]["no_matches"] == 0
        assert output["confirmation_result"] != "confirmed-change"
        if failure_phase == "confirmation":
            assert output["selected_mutation_digest"] is not None
    before = len(runner.calls)
    resumed = CliRunner().invoke(
        app,
        [
            "fuzz",
            output["result_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    assert json.loads(resumed.stdout)["status"] == outcome, resumed.output
    assert len(runner.calls) == before


@pytest.mark.parametrize("boundary", ["calibration", "trial", "confirmation"])
def test_interruption_resumes_exact_campaign_without_repeating_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from ithrl.run_state import SampledRunState

    args, runner, _ = mock_native(tmp_path, monkeypatch)
    protected = _protected_inputs(tmp_path)
    profile = tmp_path / "profile.json"
    assert CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)]).exit_code == 0
    write = SampledRunStateFile._write
    interrupted = False

    def interrupt(self: SampledRunStateFile, state: SampledRunState) -> None:
        nonlocal interrupted
        write(self, state)
        block = state.blocks[-1] if state.blocks else None
        hit = (
            (boundary == "calibration" and len(state.calibration_observation_ids) == 1)
            or (
                boundary == "trial"
                and state.fuzz_progress is not None
                and bool(state.fuzz_progress.trials)
            )
            or (
                boundary == "confirmation"
                and block is not None
                and block.plan.role == "confirmation"
                and len(block.observations) == 2
                and state.pending is None
            )
        )
        if hit and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(SampledRunStateFile, "_write", interrupt)
    first = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    report = json.loads(first.stdout)
    assert interrupted
    assert report["status"] == "interrupted", report
    ids = [a.attempt_id for a in runner.calls]
    assert report["usage"]["charged_provider_calls"] == len(ids)
    resumed = CliRunner().invoke(
        app,
        [
            "fuzz",
            report["result_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    final = json.loads(resumed.stdout)
    assert final["status"] == "confirmed-change", final
    assert final["usage"]["charged_provider_calls"] == len(runner.calls)
    assert len({a.attempt_id for a in runner.calls}) == len(runner.calls)
    assert [a.attempt_id for a in runner.calls[: len(ids)]] == ids
    assert protected == _protected_inputs(tmp_path)


def test_unresolved_calibration_dispatch_stays_charged_without_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.calibration as calibration

    args, runner, _ = mock_native(tmp_path, monkeypatch)
    profile = tmp_path / "profile.json"
    assert CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)]).exit_code == 0
    reserve = calibration._reserve_attempt

    def interrupt(store: ObservationStore, reservation: calibration._InFlightAttempt) -> None:
        reserve(store, reservation)
        raise KeyboardInterrupt()

    monkeypatch.setattr(calibration, "_reserve_attempt", interrupt)
    first = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    report = json.loads(first.stdout)
    assert report["status"] == "interrupted", report
    assert report["usage"]["actual_provider_calls"] == 0
    assert report["usage"]["pending_provider_calls"] == 1
    assert report["usage"]["charged_provider_calls"] == 1
    assert report["actions_executed"] is None
    assert report["provider_calls"] is None
    assert report["next_command"] is None
    assert not runner.calls
    resumed = CliRunner().invoke(
        app,
        [
            "fuzz",
            report["result_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    output = json.loads(resumed.stdout)
    assert output["stop_reason"] == "unresolved-calibration-dispatch", output
    assert output["usage"]["charged_provider_calls"] == 1
    assert not runner.calls


def test_profile_change_and_unsupported_mutation_refuse_before_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, runner, _ = mock_native(tmp_path, monkeypatch)
    profile = tmp_path / "profile.json"
    assert CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)]).exit_code == 0
    runner.capability_profile = runner.capability_profile.model_copy(
        update={"supported_context_operations": ("dependency-closed-removal",)}
    )
    changed = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    assert changed.exit_code != 0
    assert not runner.calls
    current = tmp_path / "current.json"
    detected = CliRunner().invoke(app, [*args, "--detect", "--profile", str(current)])
    assert detected.exit_code == 0, detected.output
    refused = CliRunner().invoke(app, [*args, "--profile", str(current), "--dry-run"])
    assert json.loads(refused.stdout)["error_code"] == "unsupported-context-operation", (
        refused.output
    )
    assert not runner.calls


def test_insufficient_budget_is_visible_before_storage_or_execution(tmp_path: Path) -> None:
    args = inputs(tmp_path)
    config_path = tmp_path / "fuzz.toml"
    config_path.write_text(
        CONFIGURATION.replace("max_wall_time_seconds = 10000", "max_wall_time_seconds = 1")
    )
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    report = json.loads(result.stdout)
    assert result.exit_code == 6, report
    assert report["stop_reason"] == "wall-time-budget"
    assert report["minimum_wall_time_seconds"] > 1
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("policy", ["safe-provider-overload", "independent-safe-preaccept-only"])
def test_calibration_uses_frozen_policy_for_a_verified_overload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    from ithrl.runner import (
        NativeOutputReference,
        ProviderAcceptanceState,
        RunAccounting,
        RunnerError,
        RunnerErrorCode,
    )

    args, runner, _ = mock_native(tmp_path, monkeypatch)
    config_path = tmp_path / "fuzz.toml"
    config_path.write_text(
        config_path.read_text()
        .replace(
            'eligibility_policy = "independent-safe-preaccept-only"',
            f'eligibility_policy = "{policy}"',
        )
        .replace("max_attempts = 2", "max_attempts = 3", 1)
    )
    original_run = runner.run
    failed = False

    async def overload_once(attempt: PreparedAttempt) -> RunResult:
        nonlocal failed
        if not failed:
            failed = True
            runner.calls.append(attempt)
            raise RunnerError(
                RunnerErrorCode.PROVIDER_OVERLOADED,
                "Synthetic overload.",
                provider_acceptance_state=ProviderAcceptanceState.UNKNOWN,
                effective_policy_digest=attempt.safety_policy.digest,
                model_requested=attempt.execution_plan.model_requested,
                provider="synthetic-provider",
                endpoint="https://provider.example.invalid",
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
        return await original_run(attempt)

    monkeypatch.setattr(runner, "run", overload_once)
    profile = tmp_path / "profile.json"
    detected = CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)])
    assert detected.exit_code == 0, detected.output
    result = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    report = json.loads(result.stdout)
    if policy == "safe-provider-overload":
        assert report["status"] == "confirmed-change", report
        assert report["calibration"]["valid"] == 2
        assert report["calibration"]["runner_errors"] == 1
    else:
        assert report["status"] == "incomplete", report
        assert report["stop_reason"] == "unsafe-retry"
        assert report["calibration"]["valid"] == 0
        assert len(runner.calls) == 1
    assert report["unknown_acceptance_attempts"] == 1
    assert report["usage"]["charged_provider_calls"] == len(runner.calls)
    assert report["execution_reliability"]["runner_errors"] == 1


@pytest.mark.parametrize(
    ("matches", "reason"),
    [(0, "calibration-not-measurable"), (1, "calibration-inconclusive")],
)
def test_completed_unmeasurable_calibration_reports_inconclusive_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matches: int, reason: str
) -> None:
    from ithrl.cli.output import ExitCode

    args, runner, _ = mock_native(tmp_path, monkeypatch)
    runner.original_matching = matches
    profile = tmp_path / "profile.json"
    detected = CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)])
    assert detected.exit_code == ExitCode.SUCCESS, detected.output
    result = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    report = json.loads(result.stdout)
    assert result.exit_code == ExitCode.INCONCLUSIVE, report
    assert report["status"] == "inconclusive"
    assert report["stop_reason"] == reason
    assert report["calibration"]["matches"] == matches
    assert report["calibration"]["valid"] == 2
    assert report["calibration"]["runner_errors"] == 0
    assert report["confirmation_result"] == "not-run"
    assert report["next_command"] is None
    assert len(runner.calls) == 2
    assert all(attempt.phase == "calibration" for attempt in runner.calls)
    saved = json.loads((Path(report["result_path"]) / "summary.json").read_text())
    assert saved["status"] == "inconclusive"
    assert saved["stop_reason"] == reason
    resumed = CliRunner().invoke(
        app,
        [
            "fuzz",
            report["result_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    assert resumed.exit_code == ExitCode.INCONCLUSIVE, resumed.output
    assert json.loads(resumed.stdout)["stop_reason"] == reason
    assert json.loads(resumed.stdout)["next_command"] is None
    assert len(runner.calls) == 2


def _detected_mock_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    args, runner, _ = mock_native(tmp_path, monkeypatch)
    profile = tmp_path / "profile.json"
    detected = CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)])
    assert detected.exit_code == 0, detected.output
    return [*args, "--profile", str(profile), "--acknowledge-model-execution"], runner


@pytest.mark.parametrize("target", ["session", "state", "snapshot", "summary"])
def test_tampered_fuzz_resume_refuses_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    args, runner = _detected_mock_command(tmp_path, monkeypatch)
    completed = CliRunner().invoke(app, args)
    assert completed.exit_code == 0, completed.output
    report = json.loads(completed.stdout)
    receipt = Path(report["result_path"])
    live = Path(report["run_state_path"])
    paths = {
        "session": live / "fuzz-session.json",
        "state": live / "sampled-state.json",
        "snapshot": receipt / "sampling/sampling-state.json",
        "summary": receipt / "summary.json",
    }
    path = paths[target]
    assert path.is_file()
    original = path.read_bytes()
    path.write_text('{"PRIVATE-CORRUPTION":')
    before = len(runner.calls)
    refused = CliRunner().invoke(
        app, ["fuzz", str(receipt), "--resume", "--acknowledge-model-execution", "--format", "json"]
    )
    assert refused.exit_code != 0
    assert json.loads(refused.stdout)["error_code"] == "invalid-saved-fuzz-metadata"
    assert "PRIVATE-CORRUPTION" not in refused.output
    assert len(runner.calls) == before
    path.write_bytes(original)
    conflict = CliRunner().invoke(
        app,
        [
            "fuzz",
            str(receipt),
            "--resume",
            "--config",
            str(tmp_path / "fuzz.toml"),
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    assert json.loads(conflict.stdout)["error_code"] == "resume-input-conflict"
    assert len(runner.calls) == before


def test_budget_exhaustion_protects_confirmation_and_is_terminal_through_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, runner, _ = mock_native(tmp_path, monkeypatch)
    config = tmp_path / "fuzz.toml"
    config.write_text(
        config.read_text()
        .replace("max_provider_calls = 500", "max_provider_calls = 46")
        .replace(
            'stale_text = "PRIVATE-STRESS-TEXT"',
            'stale_text = "PRIVATE-STRESS-TEXT\\n\\nsecond paragraph"',
        )
    )
    profile = tmp_path / "profile.json"
    assert CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)]).exit_code == 0
    result = CliRunner().invoke(
        app, [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    )
    report = json.loads(result.stdout)
    assert result.exit_code == 6, report
    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "provider-call-budget"
    assert len(runner.calls) == 6
    assert report["usage"]["charged_provider_calls"] == 6
    assert report["confirmation_result"] == "not-run"
    resumed = CliRunner().invoke(
        app,
        [
            "fuzz",
            report["result_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    assert json.loads(resumed.stdout)["status"] == "incomplete", resumed.output
    assert len(runner.calls) == 6


def test_interrupted_campaign_refuses_busy_resume_and_expired_block_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ithrl.run_state import SampledRunState

    args, runner, _ = mock_native(tmp_path, monkeypatch)
    config = tmp_path / "fuzz.toml"
    config.write_text(
        config.read_text().replace("freshness_seconds = 86400", "freshness_seconds = 600")
    )
    profile = tmp_path / "profile.json"
    assert CliRunner().invoke(app, [*args, "--detect", "--profile", str(profile)]).exit_code == 0
    args = [*args, "--profile", str(profile), "--acknowledge-model-execution"]
    write = SampledRunStateFile._write
    interrupted = False

    def interrupt(self: SampledRunStateFile, state: SampledRunState) -> None:
        nonlocal interrupted
        write(self, state)
        if (
            state.blocks
            and state.blocks[-1].observations
            and state.pending is None
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(SampledRunStateFile, "_write", interrupt)
    first = CliRunner().invoke(app, args)
    report = json.loads(first.stdout)
    assert report["status"] == "interrupted", report
    assert interrupted
    before = len(runner.calls)
    session = load_fuzz_session(Path(report["result_path"]))
    state_file = SampledRunStateFile.open(session.run_directory)
    store = ObservationStore(session.observation_store)
    resume = [
        "fuzz",
        report["result_path"],
        "--resume",
        "--acknowledge-model-execution",
        "--format",
        "json",
    ]
    with state_file.acquire_lock(store):
        busy = CliRunner().invoke(app, resume)
    assert busy.exit_code != 0
    assert len(runner.calls) == before

    # Resume the recorded block after its frozen freshness allowance, without sleeping.
    future = datetime.now(UTC) + timedelta(seconds=1200)

    def now(_tz: object = None) -> datetime:
        return future

    monkeypatch.setattr("ithrl.fuzz.campaign.datetime", SimpleNamespace(now=now))
    monkeypatch.setattr("ithrl.cli.fuzz_run.datetime", SimpleNamespace(now=now))
    expired = CliRunner().invoke(app, resume)
    output = json.loads(expired.stdout)
    assert output["status"] == "incomplete", output
    assert output["stop_reason"] == "freshness-expired"
    assert output["usage"]["charged_provider_calls"] == before
    assert output["confirmation_result"] == "not-run"
    assert len(runner.calls) == before


@pytest.mark.parametrize("failure", [OSError, FuzzSessionError])
def test_failed_result_publication_retains_accounting_and_resumes_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    from pydantic import BaseModel

    from ithrl.cli.fuzz_session import _save_private
    from ithrl.cli.output import ExitCode

    args, runner = _detected_mock_command(tmp_path, monkeypatch)

    def fail(path: Path, model: BaseModel) -> None:
        if path.name == "summary.json":
            raise failure("PRIVATE-PUBLICATION-FAILURE")
        _save_private(path, model)

    monkeypatch.setattr("ithrl.cli.fuzz_session._save_private", fail)
    result = CliRunner().invoke(app, args)
    report = json.loads(result.stdout)
    assert result.exit_code == ExitCode.INTERNAL
    assert report["status"] == "incomplete", report
    assert report["error_code"] == "result-publication-failed"
    assert report["usage"]["charged_provider_calls"] == len(runner.calls)
    assert report["provider_calls"] == len(runner.calls)
    assert report["confirmation_result"] == "confirmed-change"
    assert report["result_path"] is None
    assert report["run_state_path"] is not None
    assert "PRIVATE-PUBLICATION-FAILURE" not in result.output
    before = len(runner.calls)
    assert not tuple((tmp_path / "case/results").glob(".fuzz-*"))
    monkeypatch.setattr("ithrl.cli.fuzz_session._save_private", _save_private)
    resumed = CliRunner().invoke(
        app,
        [
            "fuzz",
            report["run_state_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ],
    )
    final = json.loads(resumed.stdout)
    assert resumed.exit_code == 0, final
    assert final["status"] == "confirmed-change"
    assert Path(final["result_path"]).is_dir()
    assert len(runner.calls) == before
