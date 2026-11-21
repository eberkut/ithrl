"""Normal comparison CLI collection and resume use the provider-free fake runner."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ithrl.cli import app
from ithrl.comparison.results import ComparisonEvidence, load_comparison_result
from ithrl.observations import ObservationStore
from ithrl.synthetic import extract_inference


def inputs(
    tmp_path: Path,
    *,
    both_reduced: bool = False,
    objective: str = "repair",
    trace_path: Path | None = None,
) -> list[str]:
    evidence = extract_inference(
        trace_path or Path(__file__).parents[1] / "fixtures/synthetic-trace.json",
        inference_call_id="synthetic-ac-001",
        case_path=tmp_path / "case",
    ).load()
    nodes = {node.path: node.id for node in evidence.context.nodes}
    reference = [nodes["/input/0"], nodes["/input/2"]]
    candidate = []
    if both_reduced:
        reference.append(nodes["/input/1"])
        candidate.append(nodes["/input/1"])
    config = tmp_path / "comparison.toml"
    config.write_text(
        f'''schema_version = 1
kind = "comparison"
objective = "{objective}"
minimum_effect = 0.1
seed = 1
eligibility_policy = "independent-safe-preaccept-only"
freshness_seconds = 86400
[reference]
removed_context_ids = {json.dumps(reference)}
[candidate]
removed_context_ids = {json.dumps(candidate)}
[calibration]
positive_minimum_matches = 2
negative_maximum_matches = 0
[calibration.reference]
required_valid = 2
max_attempts = 2
[calibration.candidate]
required_valid = 2
max_attempts = 2
[confirmation]
alpha = 0.05
[confirmation.reference]
required_valid = 20
max_attempts = 20
[confirmation.candidate]
required_valid = 20
max_attempts = 20
[limits]
max_provider_calls = 0
max_wall_time_seconds = 10000
attempt_timeout_seconds = 1
'''
    )
    config.chmod(0o600)
    return [
        "compare",
        str(tmp_path / "case"),
        "--config",
        str(config),
        "--observation-store",
        str(tmp_path / "private/observations.sqlite"),
        "--format",
        "json",
    ]


def test_dry_run_admits_pair_without_execution_or_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dry run dispatched a runner or detected a native runtime")

    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    monkeypatch.setattr("ithrl.runner.FakeRunner.run", forbidden)
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "ready"
    assert report["planned_attempts"] == 44
    assert report["planned_provider_calls"] == 0
    assert report["reference_context"]["removed_context_ids"]
    assert not report["candidate_context"]["removed_context_ids"]
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "case/.runs").exists()


def test_codex_replay_refuses_another_harness_before_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)
    config = tmp_path / "comparison.toml"
    config.write_text(
        config.read_text()
        + '\n[codex_replay]\nprovider = "openai"\nmodel = "test-model"\nreasoning = "omit"\n'
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("incompatible replay started native detection")

    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error_code"] == "codex-replay-requires-codex"
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("both_reduced", [False, True])
def test_frozen_pair_confirmation_and_completed_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, both_reduced: bool
) -> None:
    args = inputs(tmp_path, both_reduced=both_reduced)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "confirmed-improvement", report
    assert report["confirmation_reference"]["matches"] == 0
    assert report["confirmation_candidate"]["matches"] == 20
    assert report["difference"] == 1.0
    assert report["lower_bound"] > 0.1
    assert report["usage"]["actual_provider_calls"] == 0
    saved = load_comparison_result(Path(report["result_path"]))
    assert len(saved.blocks) == 2
    assert len({a.observation.observation_id for b in saved.blocks for a in b.attempts}) == 44
    assert bool(saved.binding.candidate.removed_context_ids) is both_reduced

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("completed comparison was replayed")

    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    monkeypatch.setattr("ithrl.runner.FakeRunner.run", forbidden)
    resumed = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["result_path"] == report["result_path"]
    assert load_comparison_result(Path(report["result_path"])) == saved


def test_opposite_direction_fails_calibration_without_confirmation(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, inputs(tmp_path, objective="degradation"))
    assert result.exit_code != 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "calibration-failed"
    saved = load_comparison_result(Path(report["result_path"]))
    assert saved.confirmation is None
    assert len(saved.blocks) == 1


def test_invalid_or_private_inputs_fail_before_storage(tmp_path: Path) -> None:
    args = inputs(tmp_path)
    config = tmp_path / "comparison.toml"
    config.write_text(
        config.read_text().replace("max_wall_time_seconds = 10000", "max_wall_time_seconds = 1")
    )
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error_code"] == "wall-time-budget"
    assert not (tmp_path / "private").exists()

    config.chmod(0o644)
    result = CliRunner().invoke(app, args)
    assert result.exit_code != 0
    assert "required_markers" not in result.output
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_fixed_trigger_removal_is_refused_before_storage(tmp_path: Path, dry_run: bool) -> None:
    args = inputs(tmp_path)
    from ithrl.case import CaseWorkspace

    evidence = CaseWorkspace(tmp_path / "case").load()
    trigger = next(node.id for node in evidence.context.nodes if node.path == "/input/6")
    config = tmp_path / "comparison.toml"
    source = config.read_text()
    start = source.index("removed_context_ids = ")
    end = source.index("\n", start)
    config.write_text(source[:start] + f'removed_context_ids = ["{trigger}"]' + source[end:])
    result = CliRunner().invoke(app, [*args, *(["--dry-run"] if dry_run else [])])
    assert result.exit_code != 0
    assert not (tmp_path / "private").exists()


def test_result_inspection_needs_no_database_or_case(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, inputs(tmp_path))
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    store = ObservationStore(tmp_path / "private/observations.sqlite")
    assert store.path.exists()
    # The saved snapshot owns all observations needed to interpret the bound pair.
    store.path.unlink()
    inspected = CliRunner().invoke(
        app, ["inspect", "comparison", report["result_path"], "--format", "json"]
    )
    assert inspected.exit_code == 0, inspected.output
    offline = json.loads(inspected.stdout)
    assert offline["status"] == "confirmed-improvement"
    assert offline["model"] == "fake-deterministic-v1"
    assert offline["provider"] == "fake"
    assert offline["harness"] is None


def test_offline_comparison_hides_ambiguous_unknown_and_unsafe_recorded_identities(
    tmp_path: Path,
) -> None:
    from ithrl.cli.comparison_output import result_report
    from ithrl.comparison.results import ComparisonEvidence

    completed = CliRunner().invoke(app, inputs(tmp_path))
    assert completed.exit_code == 0, completed.output
    result = load_comparison_result(Path(json.loads(completed.stdout)["result_path"]))
    for values, expected in (
        ((None,), None),
        (("private identifier",), None),
        (("first-model", "second-model"), None),
        (("known-model", None), "known-model"),
    ):
        payload = result.model_dump(mode="json")
        for block in payload["blocks"]:
            for index, attempt in enumerate(block["attempts"]):
                value = values[index % len(values)]
                attempt["observation"]["attempt_evidence"]["model_requested"] = value
                attempt["observation"]["attempt_evidence"]["provider"] = value
        report = result_report(ComparisonEvidence.model_validate_json(json.dumps(payload)))
        assert report.model == expected
        assert report.provider == expected
        assert report.harness is None


def test_interruption_after_calibration_resumes_fresh_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)

    @contextmanager
    def interrupted_display():
        def interrupt(message: str) -> None:
            if message.count("2/2 valid") == 2:
                raise KeyboardInterrupt

        yield interrupt

    with monkeypatch.context() as scoped:
        scoped.setattr("ithrl.cli.progress.progress_display", interrupted_display)
        interrupted = CliRunner().invoke(app, args)
    report = json.loads(interrupted.stdout)
    assert report["status"] == "interrupted", report
    snapshot = load_comparison_result(Path(report["result_path"]))
    assert snapshot.calibration is not None
    assert snapshot.calibration.complete
    assert snapshot.confirmation is None
    original_ids = {a.observation.observation_id for a in snapshot.calibration.attempts}
    from ithrl.comparison.results import validate_comparison_source

    with monkeypatch.context() as scoped:
        validation = Mock(wraps=validate_comparison_source)
        scoped.setattr("ithrl.comparison.results.validate_comparison_source", validation)

        def forbidden(*_args: object, **_kwargs: object) -> None:
            pytest.fail("active resume dry-run dispatched an observation")

        scoped.setattr("ithrl.runner.FakeRunner.run", forbidden)
        inspected = CliRunner().invoke(
            app, ["compare", report["run_state_path"], "--resume", "--dry-run", "--format", "json"]
        )
        assert json.loads(inspected.stdout)["status"] == "calibrated"
        assert json.loads(inspected.stdout)["source_validation"] == "verified"
        validation.assert_called_once()
    resumed = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert resumed.exit_code == 0, resumed.output
    final = load_comparison_result(Path(json.loads(resumed.stdout)["result_path"]))
    assert final.calibration is not None
    assert final.confirmation is not None
    assert {a.observation.observation_id for a in final.calibration.attempts} == original_ids
    assert not original_ids & {a.observation.observation_id for a in final.confirmation.attempts}


def test_failed_result_publication_preserves_terminal_run_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)

    def failed_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("PRIVATE-PATH-DETAIL")

    with monkeypatch.context() as scoped:
        scoped.setattr("ithrl.comparison.results.write_comparison_result", failed_write)
        failed = CliRunner().invoke(app, args)
    assert failed.exit_code != 0
    report = json.loads(failed.stdout)
    assert report["status"] == "confirmed-improvement"
    assert report["error_code"] == "result-publication-failed"
    assert "PRIVATE-PATH-DETAIL" not in failed.output

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("publication retry repeated completed collection")

    monkeypatch.setattr("ithrl.runner.FakeRunner.run", forbidden)
    recovered = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert recovered.exit_code == 0, recovered.output
    assert json.loads(recovered.stdout)["status"] == "confirmed-improvement"


def test_missing_optional_numerics_names_comparison_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)

    monkeypatch.setitem(sys.modules, "scipy.special", None)
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code != 0
    report = json.loads(result.stdout)
    assert report["error_code"] == "comparison-extra-required"
    assert "ithrl[compare]" in report["next_action"]
    assert not (tmp_path / "private").exists()


def test_native_without_profile_requests_explicit_detection_without_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from native_support.codex import _case

    fake_root = tmp_path / "fake"
    fake_root.mkdir()
    args = inputs(fake_root)
    native_root = tmp_path / "native"
    native_root.mkdir()
    native_case, _ = _case(native_root)
    original = tmp_path / "original"
    original.mkdir()
    args[1] = str(native_case.root)
    args += ["--original-workspace", str(original), "--max-provider-calls-per-attempt", "2"]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unacknowledged invocation probed a native runtime")

    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "detection-required"
    assert "--max-provider-calls-per-attempt 2" in report["next_command"]
    assert report["harness_started"] is False
    refused = CliRunner().invoke(app, args)
    assert refused.exit_code != 0
    assert json.loads(refused.stdout)["error_code"] == "detection-required"
    assert not (fake_root / "private").exists()


def test_explicit_fake_experiment_overrides_native_default_without_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl.case import CaseWorkspace
    from ithrl.experiment import UnavailableRunnerConfig, parse_experiment_toml

    args = inputs(tmp_path)
    evidence = CaseWorkspace(tmp_path / "case").load()
    fake = parse_experiment_toml(evidence.default_experiment)
    native_default = fake.model_copy(update={"runner": UnavailableRunnerConfig(harness="codex")})
    case = CaseWorkspace.create(
        tmp_path / "native-default-case",
        manifest=evidence.manifest,
        request=evidence.request,
        nodes=evidence.context.nodes,
        default_experiment=native_default.to_toml(),
    )
    explicit = tmp_path / "fake-experiment.toml"
    explicit.write_text(fake.to_toml())
    explicit.chmod(0o600)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("explicit fake experiment attempted native detection")

    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    args[1] = str(case.root)
    result = CliRunner().invoke(app, [*args, "--experiment", str(explicit), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["harness"] == "fake"


def test_fake_profile_next_command_preserves_profile_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl.case import CaseWorkspace
    from ithrl.cli.compare import prepare_comparison_profile
    from ithrl.cli.comparison_session import save_comparison_profile
    from ithrl.comparison.contracts import load_comparison_configuration
    from ithrl.experiment import UnavailableRunnerConfig, parse_experiment_toml

    args = inputs(tmp_path)
    evidence = CaseWorkspace(tmp_path / "case").load()
    fake = parse_experiment_toml(evidence.default_experiment)
    native_default = fake.model_copy(update={"runner": UnavailableRunnerConfig(harness="codex")})
    case = CaseWorkspace.create(
        tmp_path / "native-default-case",
        manifest=evidence.manifest,
        request=evidence.request,
        nodes=evidence.context.nodes,
        default_experiment=native_default.to_toml(),
    )
    original_workspace = tmp_path / "original"
    original_workspace.mkdir()
    profile, _ = prepare_comparison_profile(
        case,
        load_comparison_configuration(tmp_path / "comparison.toml"),
        original_workspace,
        detect=False,
        experiment=fake,
    )
    profile_path = tmp_path / "fake-profile.json"
    save_comparison_profile(profile_path, profile)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("fake profile attempted native detection")

    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    args[1] = str(case.root)
    result = CliRunner().invoke(
        app,
        [
            *args,
            "--profile",
            str(profile_path),
            "--original-workspace",
            str(original_workspace),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    command = shlex.split(json.loads(result.stdout)["next_command"])
    assert command[command.index("--profile") + 1] == str(profile_path)
    assert command[command.index("--original-workspace") + 1] == str(original_workspace)
    assert "--acknowledge-model-execution" not in command
    repeated = CliRunner().invoke(app, [*command[1:], "--dry-run", "--format", "json"])
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.stdout)["harness"] == "fake"


def test_explicit_unavailable_experiment_controls_mode_and_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl.behavior import FinalResponseTarget
    from ithrl.case import CaseWorkspace
    from ithrl.cli.native import NativeSelectionError
    from ithrl.experiment import UnavailableRunnerConfig, parse_experiment_toml

    args = inputs(tmp_path)
    fake = parse_experiment_toml(CaseWorkspace(tmp_path / "case").load().default_experiment)
    selected = fake.model_copy(
        update={
            "runner": UnavailableRunnerConfig(harness="codex"),
            "target": FinalResponseTarget(text_contains="TARGET-SENTINEL"),
        }
    )
    explicit = tmp_path / "native-experiment.toml"
    explicit.write_text(selected.to_toml())
    explicit.chmod(0o600)
    original = tmp_path / "original"
    original.mkdir()
    args += ["--experiment", str(explicit), "--original-workspace", str(original)]
    calls = []

    def detect(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs["experiment"])
        raise NativeSelectionError("fixture-detection-stop", "controlled stop")

    monkeypatch.setattr("ithrl.cli.native.native_selection", detect)
    preview = CliRunner().invoke(app, [*args, "--dry-run"])
    assert json.loads(preview.stdout)["status"] == "detection-required"
    assert not calls
    detected = CliRunner().invoke(app, [*args, "--detect-capabilities"])
    assert json.loads(detected.stdout)["error_code"] == "fixture-detection-stop"
    assert calls == [selected]


@pytest.mark.parametrize("same_category", [True, False])
def test_native_comparison_binds_a_prospective_predicate_without_changing_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_category: bool
) -> None:
    from ithrl.behavior import FinalResponseTarget, ShellActionTarget
    from ithrl.cli.comparison_session import load_comparison_profile
    from ithrl.cli.native import NativeSelection
    from ithrl.execution import ResourceLimits, SafetyPolicy
    from ithrl.experiment import (
        AnyExperimentDefinition,
        NativeRunnerConfig,
        SampledCalibrationPhase,
        SampledExperimentDefinition,
        parse_experiment_toml,
    )
    from ithrl.runner import PreparedAttempt, RunnerRegistry, RunResult
    from ithrl.runner.copilot.materialize import materialize_copilot_attempt
    from ithrl.workflow import PreflightRefusedError, prepare_experiment
    from native_support.copilot import _capability, _case

    fake_root = tmp_path / "fake"
    fake_root.mkdir()
    args = inputs(fake_root)
    native_root = tmp_path / "native"
    native_root.mkdir()
    _, case, evidence = _case(native_root, "shell-action", context_sensitive=True)
    original_decision = evidence.decision
    target = (
        ShellActionTarget(tool_name_equals="bash", command_contains=" --isolated ")
        if same_category
        else FinalResponseTarget(text_contains="new prospective response")
    )
    experiment = parse_experiment_toml(evidence.default_experiment).model_copy(
        update={"target": target}
    )
    explicit = tmp_path / "prospective.toml"
    explicit.write_text(experiment.to_toml())
    explicit.chmod(0o600)
    config = Path(args[3])
    removals = iter([[], [evidence.context.nodes[1].id]])
    config.write_text(
        "\n".join(
            f"removed_context_ids = {json.dumps(next(removals))}"
            if line.startswith("removed_context_ids = ")
            else line.replace("max_provider_calls = 0", "max_provider_calls = 100")
            for line in config.read_text().splitlines()
        )
        + "\n"
    )
    capability = _capability("shell-action")

    class PureRunner:
        fingerprint = "prospective-target-fixture:1"
        capability_profile = capability

        def validate_candidate(self, attempt: PreparedAttempt) -> None:
            materialize_copilot_attempt(attempt)

        async def run(
            self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
        ) -> RunResult:
            pytest.fail("profile preparation executed a runner")

    def select(
        *_args: object,
        experiment: AnyExperimentDefinition,
        calibration: SampledCalibrationPhase,
        **_kwargs: object,
    ) -> NativeSelection:
        return NativeSelection(
            RunnerRegistry({"copilot": lambda _config, _evidence: PureRunner()}),
            SafetyPolicy(
                provider_transport="allowed",
                allowed_provider=capability.provider,
                allowed_endpoint=capability.endpoint,
            ),
            ResourceLimits(max_provider_calls=1, max_wall_time_seconds=1),
            SampledExperimentDefinition(
                target=experiment.target,
                oracle=experiment.oracle,
                runner=NativeRunnerConfig(
                    type="copilot", required_capability_digest=capability.digest
                ),
                calibration=calibration,
            ),
        )

    # The same explicit target remains invalid for workflows describing historical behavior.
    with pytest.raises(PreflightRefusedError) as refused:
        prepare_experiment(evidence, experiment=experiment)
    assert [issue.code for issue in refused.value.report.issues] == ["runner-evidence-mismatch"]

    monkeypatch.setattr("ithrl.cli.native.native_selection", select)
    args[1] = str(case.root)
    original = tmp_path / "original"
    original.mkdir()
    profile_path = tmp_path / "profile.json"
    result = CliRunner().invoke(
        app,
        [
            *args,
            "--experiment",
            str(explicit),
            "--original-workspace",
            str(original),
            "--detect-capabilities",
            "--profile",
            str(profile_path),
        ],
    )
    if same_category:
        assert result.exit_code == 0, result.output
        profile = load_comparison_profile(profile_path)
        assert profile.experiment.target == target
        assert profile.identity.target_digest == target.digest
        assert profile.execution_plan.target_digest == target.digest
        assert case.load().decision == original_decision
    else:
        assert result.exit_code != 0
        assert json.loads(result.stdout)["error_code"] == "comparison-input-or-execution-refused"
        assert not profile_path.exists()
    assert not (case.root / ".runs").exists()


@pytest.mark.parametrize("entry", ["database", "lock"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_completed_resume_refuses_hardlinked_storage_before_touching_it(
    tmp_path: Path, entry: str, dry_run: bool
) -> None:
    from ithrl.cli.paths import observation_storage_paths

    completed = CliRunner().invoke(app, inputs(tmp_path))
    assert completed.exit_code == 0, completed.output
    report = json.loads(completed.stdout)
    paths = observation_storage_paths(tmp_path / "private/observations.sqlite")
    target = (
        paths[0]
        if entry == "database"
        else next(path for path in paths if path.name.endswith(".lock"))
    )
    target.unlink(missing_ok=True)
    protected = tmp_path / "protected-sentinel"
    protected.write_bytes(b"DO-NOT-CHANGE")
    protected.chmod(0o644)
    os.link(protected, target)
    before = protected.stat()
    result = CliRunner().invoke(
        app,
        [
            "compare",
            report["run_state_path"],
            "--resume",
            "--format",
            "json",
            *(["--dry-run"] if dry_run else []),
        ],
    )
    assert result.exit_code != 0
    assert protected.read_bytes() == b"DO-NOT-CHANGE"
    assert protected.stat().st_mode == before.st_mode


def test_human_dry_run_displays_both_contexts_sampling_gates_and_time_limits(
    tmp_path: Path,
) -> None:
    args = inputs(tmp_path)
    result = CliRunner().invoke(app, [*args[:-2], "--dry-run"])
    assert result.exit_code == 0, result.output
    for expected in (
        "reference context:",
        "candidate context:",
        "ctx-4c4e9663bfd5daae",
        "ctx-8a6964db1606f4e7",
        "calibration samples:",
        "confirmation samples:",
        "candidate matches >= 2",
        "other matches <= 0",
        "attempt timeout",
        "wall-time ceiling",
    ):
        assert expected in result.output


def test_completed_resume_does_not_require_historical_original_workspace(tmp_path: Path) -> None:
    args = inputs(tmp_path)
    original = tmp_path / "original"
    original.mkdir()
    completed = CliRunner().invoke(app, [*args, "--original-workspace", str(original)])
    assert completed.exit_code == 0, completed.output
    report = json.loads(completed.stdout)
    original.rmdir()
    resumed = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["harness_started"] is False


def test_cancelled_invocation_reports_interruption_even_with_terminal_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)

    async def cancel(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("ithrl.runner.FakeRunner.run", cancel)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 130, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "interrupted"
    retained = load_comparison_result(Path(report["result_path"]))
    assert retained.state.comparison_progress is not None
    assert retained.state.comparison_progress.terminal is not None
    assert retained.usage.actual_provider_calls == 0


def test_resume_refuses_source_store_overlap_before_initializing_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = CliRunner().invoke(app, inputs(tmp_path))
    assert completed.exit_code == 0, completed.output
    report = json.loads(completed.stdout)
    run = Path(report["run_state_path"])
    source = tmp_path / "case/request.json"
    before = source.read_bytes()
    for name in ("comparison-session.json", "sampled-state.json"):
        path = run / name
        data = json.loads(path.read_text())
        data["observation_store"] = str(source)
        path.write_text(json.dumps(data))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("resume initialized SQLite before checking protected-path overlap")

    monkeypatch.setattr("ithrl.observations.ObservationStore.initialize", forbidden)
    resumed = CliRunner().invoke(app, ["compare", str(run), "--resume", "--format", "json"])
    assert resumed.exit_code != 0
    assert source.read_bytes() == before


def test_dry_run_refuses_impossible_complete_block_freshness_without_storage(
    tmp_path: Path,
) -> None:
    args = inputs(tmp_path)
    config = tmp_path / "comparison.toml"
    config.write_text(
        config.read_text().replace("freshness_seconds = 86400", "freshness_seconds = 1")
    )
    result = CliRunner().invoke(app, [*args, "--dry-run"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error_code"] == "sampling-freshness-too-small"
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "case/.runs").exists()


def test_calibration_only_repeat_then_fresh_confirmation_preserves_one_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = inputs(tmp_path)
    pilot = CliRunner().invoke(app, [*args, "--calibration-only"])
    assert pilot.exit_code == 0, pilot.output
    report = json.loads(pilot.stdout)
    assert report["status"] == "calibrated"
    snapshot = load_comparison_result(Path(report["result_path"]))
    assert snapshot.state.selection is None
    assert snapshot.confirmation is None
    assert snapshot.calibration is not None
    assert snapshot.calibration.complete
    assert len(snapshot.calibration.attempts) == 4
    assert snapshot.state.comparison_progress is not None
    assert snapshot.state.comparison_progress.terminal is None
    assert "--calibration-only" not in report["next_command"]
    original_ids = {a.observation.observation_id for a in snapshot.calibration.attempts}
    run = report["run_state_path"]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("repeating completed pilot dispatched another observation")

    with monkeypatch.context() as scoped:
        scoped.setattr("ithrl.runner.FakeRunner.run", forbidden)
        repeated = CliRunner().invoke(
            app, ["compare", run, "--resume", "--calibration-only", "--format", "json"]
        )
        assert repeated.exit_code == 0, repeated.output
        repeated_report = json.loads(repeated.stdout)
        assert repeated_report["status"] == "calibrated"
        repeated_snapshot = load_comparison_result(Path(repeated_report["result_path"]))
        assert repeated_snapshot.blocks == snapshot.blocks
        assert repeated_snapshot.state.started_at == snapshot.state.started_at
        assert repeated_snapshot.state.allowance == snapshot.state.allowance
        inspected = CliRunner().invoke(
            app, ["inspect", "comparison", report["result_path"], "--format", "json"]
        )
        assert inspected.exit_code == 0, inspected.output
        assert json.loads(inspected.stdout)["status"] == "calibrated"

    confirmed = CliRunner().invoke(app, ["compare", run, "--resume", "--format", "json"])
    assert confirmed.exit_code == 0, confirmed.output
    final = load_comparison_result(Path(json.loads(confirmed.stdout)["result_path"]))
    assert final.outcome.value == "confirmed-improvement"
    assert final.calibration is not None
    assert {a.observation.observation_id for a in final.calibration.attempts} == original_ids
    assert final.confirmation is not None
    assert len(final.confirmation.attempts) == 40
    assert not original_ids & {a.observation.observation_id for a in final.confirmation.attempts}
    assert final.state.allowance == snapshot.state.allowance
    assert final.state.started_at == snapshot.state.started_at

    with monkeypatch.context() as scoped:
        scoped.setattr("ithrl.runner.FakeRunner.run", forbidden)
        refused = CliRunner().invoke(
            app, ["compare", run, "--resume", "--calibration-only", "--format", "json"]
        )
    assert refused.exit_code != 0
    assert json.loads(refused.stdout)["error_code"] == "calibration-after-selection-refused"


def test_calibration_only_dry_run_keeps_full_reserve_and_pilot_command(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, [*inputs(tmp_path), "--dry-run", "--calibration-only"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["planned_attempts"] == 44
    assert "--calibration-only" in report["next_command"]
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "case/.runs").exists()


@pytest.mark.parametrize("failure", ["interruption", "publication"])
def test_pilot_recovery_command_cannot_start_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    args = inputs(tmp_path)

    @contextmanager
    def interrupted_display():
        def interrupt(message: str) -> None:
            if message.count("2/2 valid") == 2:
                raise KeyboardInterrupt

        yield interrupt

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("private output failure")

    with monkeypatch.context() as scoped:
        if failure == "interruption":
            scoped.setattr("ithrl.cli.progress.progress_display", interrupted_display)
        else:
            scoped.setattr("ithrl.comparison.results.write_comparison_result", fail_publication)
        failed = CliRunner().invoke(app, [*args, "--calibration-only"])
    assert failed.exit_code != 0
    report = json.loads(failed.stdout)
    assert "--calibration-only" in report["next_command"]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pilot recovery dispatched confirmation")

    monkeypatch.setattr("ithrl.runner.FakeRunner.run", forbidden)
    recovered = CliRunner().invoke(
        app, [*shlex.split(report["next_command"])[1:], "--format", "json"]
    )
    assert recovered.exit_code == 0, recovered.output
    recovery_report = json.loads(recovered.stdout)
    assert recovery_report["status"] == "calibrated"
    retained = load_comparison_result(Path(recovery_report["result_path"]))
    assert retained.confirmation is None
    assert retained.state.selection is None
    assert "authorized" in recovery_report["next_action"]
    assert "separate approval" not in recovery_report["next_action"]


def test_unsafe_display_model_does_not_refuse_or_leak_from_a_valid_plan(tmp_path: Path) -> None:
    payload = json.loads((Path(__file__).parents[1] / "fixtures/synthetic-trace.json").read_text())
    payload["inferences"][0]["request"]["model"] = "local model"
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(payload))
    args = inputs(tmp_path, trace_path=trace)
    preview = CliRunner().invoke(app, [*args, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["model"] is None
    completed = CliRunner().invoke(app, args)
    assert completed.exit_code == 0, completed.output
    report = json.loads(completed.stdout)
    assert report["model"] is None
    assert "local model" not in completed.stdout
    saved = load_comparison_result(Path(report["result_path"]))
    assert all(
        a.observation.attempt_evidence.model_requested == "local model"
        for block in saved.blocks
        for a in block.attempts
    )


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm", ".sampling.lock"])
def test_detection_refuses_profile_store_lifecycle_overlap(tmp_path: Path, suffix: str) -> None:
    args = inputs(tmp_path)
    store = tmp_path / "private/observations.sqlite"
    profile = store.with_name(store.name + suffix)
    result = CliRunner().invoke(app, [*args, "--detect-capabilities", "--profile", str(profile)])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error_code"] == "profile-store-overlap"
    assert not profile.exists()


def test_detection_preserves_a_refused_budget(tmp_path: Path) -> None:
    args = inputs(tmp_path)
    config = tmp_path / "comparison.toml"
    config.write_text(
        config.read_text().replace("max_wall_time_seconds = 10000", "max_wall_time_seconds = 1")
    )
    result = CliRunner().invoke(
        app, [*args, "--detect-capabilities", "--profile", str(tmp_path / "profiles/profile.json")]
    )
    report = json.loads(result.stdout)
    assert result.exit_code != 0
    assert report["status"] == "refused"
    assert report["error_code"] == "wall-time-budget"
    assert "detect again" in report["next_action"]


def test_concurrent_terminal_publication_reuses_the_canonical_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from ithrl.comparison import engine
    from ithrl.comparison.results import write_comparison_result

    original = engine.run_comparison
    published = []

    async def publish_first(**kwargs: Any) -> ComparisonEvidence:
        result = await original(**kwargs)
        existing = result.model_copy(
            update={"recorded_at": result.recorded_at + timedelta(microseconds=1)}
        )
        state_file, store = kwargs["state_file"], kwargs["store"]
        destination = Path(result.state.source_case) / "results" / f"{result.run_id}.json"
        with state_file.acquire_lock(store):
            write_comparison_result(destination, existing)
        published.append(existing)
        return result

    monkeypatch.setattr(engine, "run_comparison", publish_first)
    completed = CliRunner().invoke(app, inputs(tmp_path))
    assert completed.exit_code == 0, completed.output
    report = json.loads(completed.stdout)
    assert report["error_code"] is None
    assert load_comparison_result(Path(report["result_path"])) == published[0]


def test_conflicting_result_recovery_preserves_the_file_and_does_not_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl.case.filesystem import prepare_private_directory
    from ithrl.comparison import engine

    original = engine.run_comparison

    async def occupy_destination(**kwargs: Any) -> ComparisonEvidence:
        result = await original(**kwargs)
        destination = Path(result.state.source_case) / "results" / f"{result.run_id}.json"
        prepare_private_directory(destination.parent)
        destination.write_text("unrelated file")
        destination.chmod(0o600)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(engine, "run_comparison", occupy_destination)
        failed = CliRunner().invoke(app, inputs(tmp_path))
    report = json.loads(failed.stdout)
    assert failed.exit_code != 0
    assert report["error_code"] == "result-destination-conflict"
    assert "Preserve the conflicting" in report["next_action"]
    destination = Path(report["result_path"])
    assert destination.read_text() == "unrelated file"
    rejected = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert json.loads(rejected.stdout)["error_code"] == "result-destination-conflict"
    destination.rename(destination.with_suffix(".preserved"))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("result recovery redispatched completed work")

    monkeypatch.setattr("ithrl.runner.FakeRunner.run", forbidden)
    recovered = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert recovered.exit_code == 0, recovered.output
    assert json.loads(recovered.stdout)["status"] == "confirmed-improvement"


def test_missing_comparison_progress_refuses_before_store_or_native_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithrl.comparison import engine

    async def stop_before_collection(**_kwargs):
        raise RuntimeError("fixture stop")

    with monkeypatch.context() as scoped:
        scoped.setattr(engine, "run_comparison", stop_before_collection)
        initial = CliRunner().invoke(app, inputs(tmp_path))
    report = json.loads(initial.stdout)
    state_path = Path(report["run_state_path"]) / "sampled-state.json"
    state = json.loads(state_path.read_text())
    assert state["blocks"] == []
    state.pop("comparison_progress")
    state_path.write_text(json.dumps(state))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid comparison progress reached store or native preparation")

    monkeypatch.setattr("ithrl.observations.ObservationStore.initialize", forbidden)
    monkeypatch.setattr("ithrl.cli.native.native_selection", forbidden)
    resumed = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    assert json.loads(resumed.stdout)["error_code"] == "invalid-comparison-session"


def test_expired_pilot_offers_inspection_without_another_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from ithrl.comparison import engine

    pilot = CliRunner().invoke(app, [*inputs(tmp_path), "--calibration-only"])
    assert pilot.exit_code == 0, pilot.output
    original = engine.run_comparison

    async def expired(**kwargs: Any) -> ComparisonEvidence:
        kwargs["clock"] = lambda: datetime.now(UTC) + timedelta(days=1)
        return await original(**kwargs)

    monkeypatch.setattr(engine, "run_comparison", expired)
    report = json.loads(pilot.stdout)
    resumed = CliRunner().invoke(
        app, ["compare", report["run_state_path"], "--resume", "--format", "json"]
    )
    stopped = json.loads(resumed.stdout)
    assert stopped["stop_reason"] == "wall-time-exhausted"
    assert stopped["next_command"] is None
    assert "Inspect" in stopped["next_action"]


@pytest.mark.parametrize("suffix", ["-journal", ".sampling.lock"])
def test_saved_profile_cannot_collide_with_a_later_selected_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    args = inputs(tmp_path)
    store = tmp_path / "private/observations.sqlite"
    profile = store.with_name(store.name + suffix)
    detection_args = args.copy()
    detection_args[detection_args.index("--observation-store") + 1] = str(
        tmp_path / "private/detection-store.sqlite"
    )
    detected = CliRunner().invoke(
        app, [*detection_args, "--detect-capabilities", "--profile", str(profile)]
    )
    assert detected.exit_code == 0, detected.output
    before = profile.read_bytes()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("profile collision reached store initialization")

    monkeypatch.setattr("ithrl.observations.ObservationStore.initialize", forbidden)
    result = CliRunner().invoke(app, [*args, "--profile", str(profile)])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error_code"] == "profile-store-overlap"
    assert profile.read_bytes() == before
    assert not store.exists()
