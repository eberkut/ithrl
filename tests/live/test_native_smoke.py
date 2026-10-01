"""Opt-in exact-profile smoke tests; collection never probes or calls providers."""

from __future__ import annotations

import stat
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ithrl.case.filesystem import atomic_write_bytes
from ithrl.cli import ExitCode, app
from ithrl.cli.output import SampledErrorOutput, SampledOutput
from ithrl.experiment import (
    NativeRunnerConfig,
    SampledCalibrationPhase,
    SampledExperimentDefinition,
    parse_experiment_toml,
)
from ithrl.observations import ObservationStore
from ithrl.results import load_calibration_snapshot
from native_support.smoke import Profile, SmokeOptions, protected_tree, synthetic_case


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param("codex-final", marks=pytest.mark.native_smoke("codex-final")),
        pytest.param("copilot-final", marks=pytest.mark.native_smoke("copilot-final")),
        pytest.param("copilot-shell", marks=pytest.mark.native_smoke("copilot-shell")),
    ],
)
def test_native_smoke(
    native_smoke_options: SmokeOptions,
    profile: Profile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = native_smoke_options
    assert profile == options.profile
    # Fresh owned state, outside the repository and any real user's session/workspace.
    root = Path(tempfile.mkdtemp(prefix=f"ithrl-{profile}-{options.mode}-", dir=options.root))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    source, case, original = synthetic_case(root, options)
    if options.mode == "run":
        assert options.capability_digest is not None
        persisted = parse_experiment_toml(case.load().default_experiment)
        pinned = SampledExperimentDefinition(
            schema_version=1,
            target=persisted.target,
            oracle=persisted.oracle,
            runner=NativeRunnerConfig(
                type="codex" if profile == "codex-final" else "copilot",
                required_capability_digest=options.capability_digest,
            ),
            calibration=SampledCalibrationPhase(
                required_valid=2, minimum_matches=2, max_attempts=2
            ),
        )
        # This is our newly generated private case, before any protected snapshot or probe.
        (case.root / "experiments/default.toml").write_text(pinned.to_toml(), encoding="utf-8")
    protected = (
        protected_tree(case.root, allow_case_results=True),
        protected_tree(original),
        source.read_bytes(),
    )
    database = root / "observations.sqlite"
    arguments = [
        "shrink",
        str(case.root),
        "--original-workspace",
        str(original),
        "--observation-store",
        str(database),
        "--required-valid",
        "2",
        "--minimum-matches",
        "2",
        "--max-attempts",
        "2",
        "--attempt-timeout",
        "120",
        "--max-provider-calls",
        str(options.provider_call_bound),
        "--format",
        "json",
    ]
    preflight = CliRunner().invoke(app, [*arguments, "--dry-run"])
    if preflight.exit_code != ExitCode.SUCCESS:
        # Refusals are evidence, not a skipped or falsely passing live profile.
        atomic_write_bytes(root / "preflight-refusal.json", preflight.stdout.encode())
        refusal = SampledErrorOutput.model_validate_json(preflight.stdout)
        pytest.fail(
            f"native preflight refused ({refusal.error_code}); "
            f"stage: {refusal.probe_stage}; exit code: {refusal.probe_exit_code}; "
            f"reason: {refusal.probe_reason}; private probe diagnostic: {refusal.diagnostic_path}; "
            f"private receipt: {root / 'preflight-refusal.json'}"
        )
    report = SampledOutput.model_validate_json(preflight.stdout)
    atomic_write_bytes(root / "preflight.json", report.model_dump_json(indent=2).encode())
    sys.stdout.write(f"Private native smoke evidence: {root}\n")
    assert report.provider == options.provider
    assert report.requested_model == options.model
    assert report.max_provider_calls == options.provider_call_bound
    assert report.provider_call_accounting == "not-started"
    assert report.provider_calls == 0
    assert report.actions_executed == 0
    assert report.action_denial == "enforced"
    assert report.protected_state_enforcement == "preventive"
    assert report.disposable_state_limit_enforcement == "enforced"
    assert report.process_containment == "process-group"
    if profile == "copilot-shell":
        assert report.action_visibility == "visible"
    assert not database.exists()
    assert (
        protected_tree(case.root, allow_case_results=True),
        protected_tree(original),
        source.read_bytes(),
    ) == protected
    if options.mode == "preflight":
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
        return
    assert report.capability_digest == options.capability_digest
    result = CliRunner().invoke(
        app, [*arguments, "--calibrate-only", "--acknowledge-model-execution"]
    )
    atomic_write_bytes(root / "calibration-report.json", result.stdout.encode())
    if result.exit_code != ExitCode.SUCCESS:
        pytest.fail(f"native calibration did not establish the profile; private evidence: {root}")
    completed = SampledOutput.model_validate_json(result.stdout)
    assert completed.result_path is not None
    snapshot = load_calibration_snapshot(Path(completed.result_path))
    summary = snapshot.summary
    assert summary.outcome == "measurable"
    assert summary.observations_recorded == summary.matches == 2
    assert summary.provider_calls_may_have_started <= options.provider_call_bound
    assert summary.actions_executed == summary.tool_network_requests == 0
    assert summary.source_store_effect == summary.original_workspace_effect == "unchanged"
    observations = ObservationStore(database).list_for_experiment(snapshot.experiment.digest)
    assert len(observations) == 2
    assert len({item.attempt_evidence.attempt_id for item in observations}) == 2
    assert (
        len(
            {
                item.attempt_evidence.native_output.artifact_id
                for item in observations
                if item.attempt_evidence.native_output is not None
            }
        )
        == 2
    )
    for observation in observations:
        accounting = observation.attempt_evidence.accounting
        assert accounting is not None
        assert accounting.cleanup_outcome == "verified"
        assert accounting.residual_disposable_state_bytes == 0
        assert accounting.residual_disposable_state_entries == 0
        assert accounting.actions_executed == accounting.tool_network_requests == 0
    if profile == "copilot-shell":
        assert summary.actions_requested == summary.actions_denied == 2
        assert completed.action_visibility == "visible"
    assert not tuple((root / "data/ithrl/native-attempts").iterdir())
    assert (
        protected_tree(case.root, allow_case_results=True),
        protected_tree(original),
        source.read_bytes(),
    ) == protected
    for path in (root / "data/ithrl/native-output").rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    # Unknown acceptance is not converted into an exact or zero call count.
    if (
        summary.provider_calls_possible
        or summary.provider_calls_started != summary.provider_calls_may_have_started
    ):
        assert completed.provider_call_accounting == "started-unknown"
        assert completed.provider_calls is None
    else:
        assert completed.provider_call_accounting == "accounted"
        assert completed.provider_calls == summary.provider_calls_started
