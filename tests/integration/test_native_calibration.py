"""Compose native CLI, mock processes, real calibration, and private persistence."""

from __future__ import annotations

import asyncio
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import ithrl.cli.native as native
import ithrl.runner.runtime.process as process_runtime
from ithrl.case import CaseWorkspace
from ithrl.cli import ExitCode, app
from ithrl.execution import CapabilityProfile
from ithrl.observations import ObservationStore
from ithrl.results import CalibrationSnapshot, load_calibration_snapshot
from ithrl.runner.codex import CodexProviderConfiguration, CodexRuntime
from ithrl.runner.copilot import CopilotRuntime, DetectedCopilotCapability
from ithrl.runner.copilot.capability import CopilotCapabilityError, CopilotCapabilityErrorCode
from ithrl.runner.runtime import ProcessSpec
from live.test_native_smoke import test_native_smoke as run_smoke
from native_support import codex, copilot
from native_support.smoke import Profile, SmokeOptions, protected_tree

type Harness = Literal["codex", "copilot"]
type Target = Literal["final-response", "shell-action"]


@dataclass(frozen=True)
class Scenario:
    case: CaseWorkspace
    source: Path
    original: Path
    data: Path
    marker: Path
    database: Path
    calls_per_attempt: int

    def arguments(self, *, acknowledge: bool = True) -> list[str]:
        return [
            "shrink",
            str(self.case.root),
            "--calibrate-only",
            "--original-workspace",
            str(self.original),
            "--observation-store",
            str(self.database),
            "--required-valid",
            "2",
            "--minimum-matches",
            "2",
            "--max-attempts",
            "2",
            "--attempt-timeout",
            "5",
            "--max-provider-calls",
            str(2 * self.calls_per_attempt),
            "--format",
            "json",
            *(["--acknowledge-model-execution"] if acknowledge else []),
        ]


def _scenario(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    target: Target,
    *,
    mode: str = "complete",
    response: str = "Fixture complete.",
) -> Scenario:
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("CODEX_HOME", str(root / "unused-codex-home"))
    monkeypatch.setenv("COPILOT_HOME", str(root / "unused-copilot-home"))
    monkeypatch.setattr(native, "sys", SimpleNamespace(executable="/usr/bin/python3"))
    marker = root / "forbidden-action"
    executable = root / "mock-harness"
    if harness == "codex":
        case, _ = codex._case(root)
        source = codex.FIXTURES / "codex-session/rollout.jsonl"
        script = codex._FAKE_APP_SERVER.replace("__FIXTURE_MODE__", mode)
        executable.write_text(script.replace("Fixture complete.", response))
        monkeypatch.setattr(
            native, "resolve_codex_configuration", Mock(return_value=codex._configuration())
        )

        def detect(
            runtime: CodexRuntime, configuration: CodexProviderConfiguration
        ) -> CapabilityProfile:
            return codex._capability(configuration, runtime)

        monkeypatch.setattr(native, "detect_codex_capability", detect)
    else:
        source, case, _ = copilot._case(root, target)
        executable.write_text(
            copilot._FAKE_COPILOT.replace("__MODE__", mode)
            .replace("__MARKER__", json.dumps(str(marker)))
            .replace("Fixture complete.", response)
        )
        configuration = copilot._configuration()
        executable.chmod(0o700)
        monkeypatch.setattr(
            native, "resolve_copilot_configuration", Mock(return_value=configuration)
        )
        detected = DetectedCopilotCapability(
            profile=copilot._capability(target),
            runtime=CopilotRuntime(copilot=executable, bubblewrap=Path("/usr/bin/bwrap")),
            configuration=configuration,
        )
        monkeypatch.setattr(native, "detect_copilot_capability", Mock(return_value=detected))
    executable.chmod(0o700)

    def select_executable(name: str) -> Path:
        return Path("/usr/bin/bwrap") if name == "bwrap" else executable

    monkeypatch.setattr(native, "_executable", select_executable)
    original = root / "original"
    original.mkdir()
    (original / "protected.txt").write_text("unchanged\n")
    return Scenario(
        case,
        source,
        original,
        root / "data",
        marker,
        root / "observations.sqlite",
        2 if target == "shell-action" else 1,
    )


def _snapshot(report: dict[str, Any]) -> CalibrationSnapshot:
    return load_calibration_snapshot(Path(report["result_path"]))


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_dry_run_uses_harness_specific_state_budget_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: Harness
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, harness, "final-response")
    start = Mock(side_effect=AssertionError("dry-run dispatch"))
    monkeypatch.setattr(process_runtime, "_start_process", start)
    arguments = scenario.arguments(acknowledge=False)
    arguments.remove("--calibrate-only")

    result = CliRunner().invoke(app, [*arguments, "--dry-run"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert (
        report["max_disposable_state_bytes"] == (256 if harness == "copilot" else 64) * 1024 * 1024
    )
    assert report["provider_calls"] == report["actions_executed"] == 0
    start.assert_not_called()
    assert not scenario.database.exists()


@pytest.mark.parametrize("output_format", ["json", "human"])
@pytest.mark.parametrize("storage_available", [True, False])
def test_preflight_refusal_reports_probe_metadata_without_native_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    storage_available: bool,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, "copilot", "final-response")
    protected = (protected_tree(scenario.case.root), protected_tree(scenario.original))
    error = CopilotCapabilityError(
        CopilotCapabilityErrorCode.PROBE_FAILED, "Copilot capability probe failed"
    )
    error.probe_stage = "version"
    error.probe_exit_code = 93
    error.stdout = b"private-native-stdout"
    error.stderr = b"private-native-stderr"
    error.state_limit_details = {
        "limit_bytes": 10,
        "accounted_bytes": 12,
        "file_path": "private-owned-file",
        "file_bytes": 12,
    }
    detector = Mock(side_effect=error)
    monkeypatch.setattr(native, "detect_copilot_capability", detector)
    if not storage_available:
        monkeypatch.setattr(
            native, "atomic_write_bytes", Mock(side_effect=OSError("private-error"))
        )
    arguments = scenario.arguments(acknowledge=False)
    arguments.remove("--calibrate-only")
    arguments[arguments.index("--format") + 1] = output_format
    result = CliRunner().invoke(app, [*arguments, "--dry-run"])

    assert result.exit_code == ExitCode.UNSUPPORTED, result.output
    assert "private-native" not in result.output
    assert "private-owned-file" not in result.output
    assert "private-error" not in result.output
    assert "version" in result.output
    assert "93" in result.output
    if output_format == "json":
        report = json.loads(result.stdout)
        assert report["error_code"] == "copilot-probe-failed"
        assert report["probe_stage"] == "version"
        assert report["probe_exit_code"] == 93
        assert report["probe_reason"] == "Copilot capability probe failed"
        assert report["provider_call_accounting"] == "not-started"
        assert report["provider_calls"] == 0
        assert bool(report["diagnostic_path"]) is storage_available
    artifacts = tuple(
        (scenario.data / "ithrl/native-output/probe-diagnostics").glob("*/probe.json")
    )
    assert len(artifacts) == int(storage_available)
    if artifacts:
        artifact = artifacts[0]
        diagnostic = json.loads(artifact.read_bytes())
        assert diagnostic["stdout"] == "private-native-stdout"
        assert diagnostic["stderr"] == "private-native-stderr"
        assert diagnostic["state_limit"] == error.state_limit_details
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700
        assert str(artifact) in result.output
    detector.assert_called_once()
    assert not scenario.database.exists()
    assert not (scenario.case.root / "results").exists()
    assert not tuple((scenario.data / "ithrl/native-attempts").glob("*"))
    assert (protected_tree(scenario.case.root), protected_tree(scenario.original)) == protected


def _assert_cleanup(scenario: Scenario, snapshot: CalibrationSnapshot) -> None:
    observations = ObservationStore(scenario.database).list_for_experiment(
        snapshot.experiment.digest
    )
    assert len(observations) == snapshot.summary.observations_recorded
    assert len({item.attempt_evidence.attempt_id for item in observations}) == len(observations)
    references = []
    for item in observations:
        evidence = item.attempt_evidence
        assert evidence.accounting is not None
        assert evidence.accounting.cleanup_outcome == "verified"
        assert evidence.accounting.residual_disposable_state_bytes == 0
        assert evidence.accounting.residual_disposable_state_entries == 0
        assert evidence.accounting.source_store_effect == "unchanged"
        assert evidence.accounting.original_workspace_effect == "unchanged"
        assert evidence.accounting.actions_executed == 0
        assert evidence.accounting.tool_network_requests == 0
        assert evidence.native_output is not None
        references.append(evidence.native_output)
    assert len({ref.artifact_id for ref in references}) == len(references)
    assert not scenario.marker.exists()
    attempts = scenario.data / "ithrl/native-attempts"
    assert not tuple(attempts.iterdir())
    for path in (scenario.data / "ithrl/native-output").rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize(
    ("harness", "target"),
    [("codex", "final-response"), ("copilot", "final-response"), ("copilot", "shell-action")],
)
def test_native_cli_calibrates_and_reuses_independent_clean_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    target: Target,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, harness, target)
    before_case, before_workspace, before_source = (
        protected_tree(scenario.case.root, allow_case_results=True),
        protected_tree(scenario.original),
        scenario.source.read_bytes(),
    )
    result = CliRunner().invoke(app, scenario.arguments())
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    snapshot = _snapshot(report)
    assert report["status"] == "measurable"
    assert report["attempts_used"] == report["matches"] == 2
    assert report["actions_executed"] == 0
    assert report["provider_calls"] == (None if target == "shell-action" else 2)
    assert report["provider_call_accounting"] == (
        "started-unknown" if target == "shell-action" else "accounted"
    )
    assert snapshot.summary.provider_calls_completed == 2
    assert snapshot.summary.actions_requested == (2 if target == "shell-action" else 0)
    assert snapshot.summary.actions_denied == snapshot.summary.actions_requested
    assert (snapshot.summary.usage is not None) == (harness == "codex")
    _assert_cleanup(scenario, snapshot)
    output_files = tuple((scenario.data / "ithrl/native-output").rglob("*"))
    second = CliRunner().invoke(app, scenario.arguments())
    assert second.exit_code == ExitCode.SUCCESS, second.output
    assert json.loads(second.stdout)["attempts_used"] == 2
    assert tuple((scenario.data / "ithrl/native-output").rglob("*")) == output_files
    assert protected_tree(scenario.case.root, allow_case_results=True) == before_case
    assert protected_tree(scenario.original) == before_workspace
    assert scenario.source.read_bytes() == before_source
    for secret in ("fixture-auth-value", "private-fixture-token"):
        assert secret not in result.output
        assert secret not in snapshot.model_dump_json()


@pytest.mark.parametrize("harness", ["codex", "copilot"])
def test_unacknowledged_native_cli_never_dispatches_or_creates_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch, harness, "final-response")
    start = Mock(side_effect=AssertionError("unacknowledged dispatch"))
    monkeypatch.setattr(process_runtime, "_start_process", start)
    result = CliRunner().invoke(app, scenario.arguments(acknowledge=False))
    report = json.loads(result.stdout)
    assert report["error_code"] == "acknowledgement-required"
    assert report["provider_call_accounting"] == "not-started"
    assert report["provider_calls"] == 0
    start.assert_not_called()
    assert not scenario.database.exists()
    assert not (scenario.case.root / "results").exists()


@pytest.mark.parametrize("harness", ["codex", "copilot"])
@pytest.mark.parametrize("mode", ["malformed", "interrupt"])
def test_failed_or_interrupted_native_cli_saves_unknown_acceptance_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    mode: str,
) -> None:
    scenario = _scenario(
        tmp_path,
        monkeypatch,
        harness,
        "final-response",
        mode="sleep"
        if mode == "interrupt"
        else ("after-dispatch-malformed" if harness == "codex" else mode),
    )
    protected = (
        protected_tree(scenario.case.root, allow_case_results=True),
        protected_tree(scenario.original),
        scenario.source.read_bytes(),
    )
    children: list[subprocess.Popen[bytes]] = []
    original_start = process_runtime._start_process

    def start(spec: ProcessSpec) -> subprocess.Popen[bytes]:
        child = original_start(spec)
        children.append(child)
        if mode == "interrupt":
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
        return child

    monkeypatch.setattr(process_runtime, "_start_process", start)
    result = CliRunner().invoke(app, scenario.arguments())
    assert result.exit_code != ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    snapshot = _snapshot(report)
    assert report["attempts_used"] == 1
    assert report["provider_call_accounting"] == "started-unknown"
    assert report["provider_calls"] is None
    assert report["next_command"] is None
    assert snapshot.summary.provider_calls_may_have_started >= 1
    assert snapshot.summary.provider_calls_possible >= 1
    _assert_cleanup(scenario, snapshot)
    assert children
    assert all(child.poll() is not None for child in children)
    count = len(children)
    resumed = CliRunner().invoke(app, scenario.arguments())
    assert json.loads(resumed.stdout)["attempts_used"] == 1
    assert len(children) == count
    assert (
        protected_tree(scenario.case.root, allow_case_results=True),
        protected_tree(scenario.original),
        scenario.source.read_bytes(),
    ) == protected


@pytest.mark.parametrize("profile", ["codex-final", "copilot-final", "copilot-shell"])
@pytest.mark.parametrize("mode", ["preflight", "run"])
def test_opt_in_smoke_workflow_with_mock_processes_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: Profile,
    mode: str,
) -> None:
    harness: Harness = "codex" if profile == "codex-final" else "copilot"
    target: Target = "shell-action" if profile == "copilot-shell" else "final-response"
    scenario = _scenario(tmp_path, monkeypatch, harness, target, response="ITHRL_SMOKE_OK")
    from ithrl.experiment import SampledCalibrationPhase

    selection = native.native_selection(
        scenario.case,
        scenario.case.load(),
        original_workspace=scenario.original,
        calibration=SampledCalibrationPhase(required_valid=2, minimum_matches=2, max_attempts=2),
    )
    options = SmokeOptions(
        "run" if mode == "run" else "preflight",
        profile,
        "fixture-model",
        "fixture-provider" if harness == "codex" else "github-copilot",
        tmp_path,
        selection.experiment.runner.required_capability_digest,
    )
    options.validate(ci=False)
    run_smoke(options, profile, monkeypatch)
