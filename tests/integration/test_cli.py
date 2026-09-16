from __future__ import annotations

import json
import os
import socket
import subprocess
from importlib import import_module
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli import ExitCode, OutputFormat, app
from ithrl.cli.extract import select_inference
from ithrl.context import JsonObject, content_digest
from ithrl.experiment import ExperimentDefinition
from ithrl.measurement import RunnerExecutionError
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
)
from ithrl.reduce import ReductionCheckpoint
from ithrl.results import load_result_snapshot
from ithrl.run_state import RunStateFile
from ithrl.runner import (
    AttemptEvidence,
    FakeRunner,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunResult,
    prepare_candidate,
)
from ithrl.workflow import prepare_experiment

FIXTURE = Path(__file__).parents[1] / "fixtures/synthetic-trace.json"
INFERENCE_ID = "synthetic-ac-001"


def test_root_version_option_reports_the_installed_version() -> None:
    result = CliRunner().invoke(app, ["--version"], color=False)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert result.stdout == f"ithrl {distribution_version('ithrl')}\n"
    assert result.stderr == ""


def _extract(runner: CliRunner, case: Path, *, output_format: str = "human"):
    return runner.invoke(
        app,
        [
            "extract",
            str(FIXTURE),
            "--inference",
            INFERENCE_ID,
            "--output",
            str(case),
            "--format",
            output_format,
        ],
        color=False,
    )


def test_credential_free_cli_vertical_slice_and_json_contract(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "private" / "observations.sqlite"

    extracted = _extract(runner, case, output_format="json")
    assert extracted.exit_code == ExitCode.SUCCESS, extracted.output
    extract_report = json.loads(extracted.stdout)
    assert extract_report["status"] == "case-created"
    assert extract_report["provider_calls"] == 0
    assert extract_report["case_path"] == str(case)

    dry_run = runner.invoke(
        app,
        ["shrink", str(case), "--dry-run", "--format", "json"],
        color=False,
    )
    assert dry_run.exit_code == ExitCode.SUCCESS, dry_run.output
    dry_report = json.loads(dry_run.stdout)
    assert dry_report["status"] == "ready"
    assert dry_report["runner_type"] == "fake"
    assert dry_report["runner_version"] == "3"
    assert dry_report["target_type"] == "shell-action"
    assert len(dry_report["execution_plan_digest"]) == 64
    assert dry_report["protected_context_ids"]
    assert dry_report["max_candidate_runs"] == 100
    assert dry_report["next_action"] == "Run the experiment when ready."
    assert f"ithrl shrink {case}" in dry_report["next_command"]
    assert "--max-candidates 100" in dry_report["next_command"]
    assert not (case / "results").exists()

    reduced = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    assert reduced.exit_code == ExitCode.SUCCESS, reduced.output
    report = json.loads(reduced.stdout)
    assert report["status"] == "reduced"
    assert report["original_context_count"] == 10
    assert report["retained_context_count"] == 5
    assert report["search_result"] == "search-1-minimal"
    assert report["confirmation_result"] == "confirmed-preserving"
    assert report["max_candidate_runs"] == 100
    assert report["provider_calls"] == 0
    assert report["run_state_path"]
    for message in (
        "Testing original context",
        "Searching dependency-valid candidates",
        "Search complete:",
        "Confirming the reduced context with a new run",
    ):
        assert message in reduced.stderr

    snapshot = load_result_snapshot(Path(report["result_path"]))
    assert snapshot.summary.status == "reduced"
    assert snapshot.summary.observations_recorded == report["observations_recorded"]
    assert snapshot.reduced_request is not None
    input_items = cast(list[JsonObject], snapshot.reduced_request["input"])
    assert [item["content"] for item in input_items] == ["A", "C"]
    roles = [
        observation.role
        for observation in ObservationStore(observations).list_for_experiment(
            snapshot.summary.experiment_digest
        )
    ]
    assert roles[0] is ObservationRole.CALIBRATION
    assert roles[-1] is ObservationRole.CONFIRMATION


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_default_data_root_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    runner = CliRunner()

    extracted = runner.invoke(
        app,
        [
            "extract",
            str(FIXTURE),
            "--inference",
            INFERENCE_ID,
            "--format",
            "json",
        ],
        color=False,
    )
    assert extracted.exit_code == ExitCode.SUCCESS, extracted.output
    case = Path(json.loads(extracted.stdout)["case_path"])
    reduced = runner.invoke(app, ["shrink", str(case), "--format", "json"], color=False)

    assert reduced.exit_code == ExitCode.SUCCESS, reduced.output
    data_root = data_home / "ithrl"
    assert data_root.stat().st_mode & 0o777 == 0o700
    assert (data_root / "cases").stat().st_mode & 0o777 == 0o700
    assert (data_root / "observations.sqlite").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_default_data_root_creates_private_parents_with_a_permissive_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "first" / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    previous_umask = os.umask(0)
    try:
        result = CliRunner().invoke(
            app,
            [
                "extract",
                str(FIXTURE),
                "--inference",
                INFERENCE_ID,
                "--format",
                "json",
            ],
            color=False,
        )
    finally:
        os.umask(previous_umask)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    for path in (tmp_path / "first", data_home, data_home / "ithrl"):
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_default_data_root_rejects_a_parent_writable_by_other_users(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "shared"
    data_home.mkdir(mode=0o777)
    data_home.chmod(0o777)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(FIXTURE),
            "--inference",
            INFERENCE_ID,
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error_code"] == "unsafe-case-path"
    assert not (data_home / "ithrl").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_shrink_reports_an_unsafe_default_data_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = tmp_path / "case"
    assert _extract(CliRunner(), case).exit_code == ExitCode.SUCCESS
    data_home = tmp_path / "shared"
    data_home.mkdir(mode=0o777)
    data_home.chmod(0o777)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    result = CliRunner().invoke(
        app,
        ["shrink", str(case), "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error_code"] == "unsafe-data-path"
    assert not (data_home / "ithrl").exists()


def test_unusable_observation_store_fails_before_creating_run_state(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    private = tmp_path / "private"
    private.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)

    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(linked / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert json.loads(result.stdout)["error_code"] == "observation-store-error"
    assert not (case / ".runs").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["extract", "/definitely/missing", "--inference", "x", "--format", "json"],
        ["shrink", "/definitely/missing", "--max-candidates", "0", "--format=json"],
        ["unknown", "--format", "json"],
    ],
)
def test_command_line_parser_errors_honor_json_format(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments, color=False)

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report["status"] == "error"
    assert report["error_code"] == "invalid-command-line"
    assert report["exit_code"] == ExitCode.USAGE
    assert report["network_calls"] == 0
    assert report["provider_calls"] == 0
    assert report["actions_executed"] == 0
    assert "Usage:" not in result.output
    assert "Traceback" not in result.output


def test_human_refusal_reports_that_no_external_work_occurred(tmp_path: Path) -> None:
    trace = tmp_path / "invalid.json"
    trace.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["extract", str(trace), "--inference", "x"],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert "No network requests or provider calls. No actions executed." in result.stderr


def test_human_success_reports_paths_resources_and_next_step(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--calibrate-only",
            "--observation-store",
            str(observations),
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Original context tested" in result.stdout
    assert f"case            {case}" in result.stdout
    assert "result          " in result.stdout
    assert "run state       " in result.stdout
    assert "candidate limit 100" in result.stdout
    assert "No network requests or provider calls. No actions executed." in result.stdout
    assert "Run the full search when ready." in result.stdout
    assert "--observation-store" in result.stdout


def test_budget_stop_saves_and_resumes_without_repeating_search(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    assert stopped.exit_code == ExitCode.BUDGET, stopped.output
    stopped_report = json.loads(stopped.stdout)
    assert stopped_report["status"] == "stopped"
    assert stopped_report["search_result"] == "search-incomplete"
    assert "--observation-store" in stopped_report["next_command"]
    assert str(observations) in stopped_report["next_command"]
    first_snapshot = load_result_snapshot(Path(stopped_report["result_path"]))
    first_observations = len(
        ObservationStore(observations).list_for_experiment(first_snapshot.summary.experiment_digest)
    )

    resumed = runner.invoke(
        app,
        [
            "shrink",
            stopped_report["result_path"],
            "--resume",
            "--max-candidates",
            "30",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
    resumed_report = json.loads(resumed.stdout)
    resumed_snapshot = load_result_snapshot(Path(resumed_report["result_path"]))
    assert resumed_snapshot.summary.resume_of == first_snapshot.summary.run_id
    assert resumed_snapshot.summary.status == "reduced"
    total_observations = len(
        ObservationStore(observations).list_for_experiment(first_snapshot.summary.experiment_digest)
    )
    new_candidate_runs = (
        resumed_snapshot.summary.candidate_runs_used - first_snapshot.summary.candidate_runs_used
    )
    assert total_observations - first_observations == new_candidate_runs + 1


def test_hard_candidate_limit_does_not_print_an_invalid_resume_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    monkeypatch.setattr(import_module("ithrl.cli.shrink"), "MAX_CANDIDATE_RUNS", 1)

    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(tmp_path / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert stopped.exit_code == ExitCode.BUDGET, stopped.output
    report = json.loads(stopped.stdout)
    assert report["next_command"] is None
    assert "supported limit of 1 candidate attempts" in report["next_action"]


def test_active_run_state_can_resume_after_a_process_stops_before_a_result(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    snapshot = load_result_snapshot(Path(json.loads(stopped.stdout)["result_path"]))
    active = RunStateFile.create(
        case_root=case,
        run_id="simulated-process-stop",
        observation_store=observations,
        checkpoint=snapshot.checkpoint,
    )

    resumed = runner.invoke(
        app,
        [
            "shrink",
            str(active.path.parent),
            "--resume",
            "--max-candidates",
            "30",
            "--format",
            "json",
        ],
        color=False,
    )

    assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
    assert json.loads(resumed.stdout)["status"] == "reduced"
    assert active.load().status == "completed"


def test_active_resume_rejects_a_conflicting_observation_store(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    snapshot = load_result_snapshot(Path(json.loads(stopped.stdout)["result_path"]))
    active = RunStateFile.create(
        case_root=case,
        run_id="conflicting-store",
        observation_store=observations,
        checkpoint=snapshot.checkpoint,
    )

    resumed = runner.invoke(
        app,
        [
            "shrink",
            str(active.path.parent),
            "--resume",
            "--observation-store",
            str(tmp_path / "other.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert resumed.exit_code == ExitCode.USAGE, resumed.output
    assert "does not match the store saved" in json.loads(resumed.stdout)["message"]


def test_active_resume_refuses_a_concurrent_process(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    evidence = CaseWorkspace.open(case).load()
    prepared = prepare_experiment(evidence)
    active = RunStateFile.create(
        case_root=case,
        run_id="locked-run",
        observation_store=observations,
        checkpoint=ReductionCheckpoint(
            case_digest=evidence.digest,
            experiment_digest=prepared.identity.experiment_digest,
            runner_fingerprint=prepared.identity.runner_fingerprint,
            execution_plan_digest=prepared.identity.execution_plan_digest,
            target_digest=prepared.identity.target_digest,
            removed_context_ids=(),
            trials=(),
            candidate_runs_used=0,
        ),
    )

    with active.acquire_lock():
        resumed = runner.invoke(
            app,
            ["shrink", str(active.path.parent), "--resume", "--format", "json"],
            color=False,
        )

    assert resumed.exit_code == ExitCode.USAGE, resumed.output
    assert json.loads(resumed.stdout)["error_code"] == "run-in-use"


def test_active_resume_validates_trial_order_before_running_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "3",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    snapshot = load_result_snapshot(Path(json.loads(stopped.stdout)["result_path"]))
    assert len(snapshot.checkpoint.trials) > 1
    changed = snapshot.checkpoint.model_copy(
        update={"trials": tuple(reversed(snapshot.checkpoint.trials))}
    )
    active = RunStateFile.create(
        case_root=case,
        run_id="invalid-trial-order",
        observation_store=observations,
        checkpoint=changed,
    )
    runner_calls = 0

    async def reject_run(_self: FakeRunner, _attempt: PreparedAttempt) -> RunResult:
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("resume validation must not execute the runner")

    monkeypatch.setattr(FakeRunner, "run", reject_run)
    resumed = runner.invoke(
        app,
        [
            "shrink",
            str(active.path.parent),
            "--resume",
            "--max-candidates",
            "30",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )

    assert resumed.exit_code == ExitCode.USAGE, resumed.output
    assert "deterministic search order" in json.loads(resumed.stdout)["message"]
    assert runner_calls == 0


def test_active_run_state_identity_mismatch_fails_before_calibration(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    evidence = CaseWorkspace.open(case).load()
    prepared = prepare_experiment(evidence)
    checkpoint = ReductionCheckpoint(
        case_digest="another-case",
        experiment_digest=prepared.identity.experiment_digest,
        runner_fingerprint=prepared.identity.runner_fingerprint,
        execution_plan_digest=prepared.identity.execution_plan_digest,
        target_digest=prepared.identity.target_digest,
        removed_context_ids=(),
        trials=(),
        candidate_runs_used=0,
    )
    active = RunStateFile.create(
        case_root=case,
        run_id="mismatched-run",
        observation_store=observations,
        checkpoint=checkpoint,
    )

    result = runner.invoke(
        app,
        [
            "shrink",
            str(active.path.parent),
            "--resume",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert "checkpoint identities do not match" in json.loads(result.stdout)["message"]
    assert not observations.exists()


def test_active_resume_refuses_reordered_context_before_calibration(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    evidence = CaseWorkspace.open(case).load()
    prepared = prepare_experiment(evidence)
    checkpoint = ReductionCheckpoint(
        case_digest=evidence.digest,
        experiment_digest=prepared.identity.experiment_digest,
        runner_fingerprint=prepared.identity.runner_fingerprint,
        execution_plan_digest=prepared.identity.execution_plan_digest,
        target_digest=prepared.identity.target_digest,
        removed_context_ids=(),
        trials=(),
        candidate_runs_used=0,
    )
    active = RunStateFile.create(
        case_root=case,
        run_id="reordered-context",
        observation_store=observations,
        checkpoint=checkpoint,
    )
    context_path = case / "context.jsonl"
    records = context_path.read_text(encoding="utf-8").splitlines()
    context_path.write_text("\n".join(reversed(records)) + "\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "shrink",
            str(active.path.parent),
            "--resume",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert "checkpoint identities do not match" in json.loads(result.stdout)["message"]
    assert not observations.exists()


def test_calibrate_only_saves_a_result_without_starting_search(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--calibrate-only",
            "--observation-store",
            str(tmp_path / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "calibrated"
    assert report["search_result"] == "not-run"
    assert report["candidate_runs_used"] == 0
    assert load_result_snapshot(Path(report["result_path"])).summary.status == "calibrated"


def test_absent_baseline_target_saves_evidence_and_skips_search(tmp_path: Path) -> None:
    case = tmp_path / "case"
    runner = CliRunner()
    extracted = _extract(runner, case)
    experiment_path = case / "experiments" / "default.toml"
    experiment = ExperimentDefinition.from_toml(experiment_path.read_text(encoding="utf-8"))
    changed = experiment.model_copy(
        update={"runner": experiment.runner.model_copy(update={"required_markers": ("missing",)})}
    )
    experiment_path.write_text(changed.to_toml(), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(tmp_path / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert extracted.exit_code == ExitCode.SUCCESS, extracted.output
    assert result.exit_code == ExitCode.INCONCLUSIVE, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "not-observed"
    snapshot = load_result_snapshot(Path(report["result_path"]))
    assert snapshot.summary.calibration_result == "not-observed"
    assert len(snapshot.summary.calibration_observation_ids) == 1
    assert snapshot.summary.search_result == "not-run"


def test_keyboard_interrupt_saves_an_explicit_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    async def interrupt(_self: object, _evidence: CaseEvidence) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("ithrl.cli.DeterministicReducer.reduce", interrupt)
    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(tmp_path / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.BUDGET, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "interrupted"
    assert "--resume" in report["next_command"]
    snapshot = load_result_snapshot(Path(report["result_path"]))
    assert snapshot.summary.status == "interrupted"


def test_resume_fails_closed_when_checkpoint_identity_changed(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    result_path = Path(json.loads(stopped.stdout)["result_path"])
    checkpoint_path = result_path / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["runner_fingerprint"] = "different-runner"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    checkpoint_path.chmod(0o600)

    resumed = runner.invoke(
        app,
        [
            "shrink",
            str(result_path),
            "--resume",
            "--max-candidates",
            "30",
            "--observation-store",
            str(observations),
        ],
        color=False,
    )

    assert resumed.exit_code == ExitCode.USAGE
    assert "checkpoint_digest does not match" in resumed.output
    assert "Traceback" not in resumed.output


def test_resume_reconstructs_the_saved_request_from_the_source_case(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )
    result_path = Path(json.loads(stopped.stdout)["result_path"])
    request_path = result_path / "request.json"
    summary_path = result_path / "summary.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    request["model"] = "tampered"
    summary["reduced_request_digest"] = content_digest(request)
    for path, value in ((request_path, request), (summary_path, summary)):
        path.write_text(json.dumps(value), encoding="utf-8")
        if os.name == "posix":
            path.chmod(0o600)

    resumed = runner.invoke(
        app,
        [
            "shrink",
            str(result_path),
            "--resume",
            "--max-candidates",
            "30",
            "--observation-store",
            str(observations),
        ],
        color=False,
    )

    assert resumed.exit_code == ExitCode.USAGE
    assert "does not match source-case materialization" in resumed.output


def test_resume_requires_the_exact_referenced_phase_observations(tmp_path: Path) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    stopped = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "1",
            "--observation-store",
            str(tmp_path / "original.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    resumed = runner.invoke(
        app,
        [
            "shrink",
            json.loads(stopped.stdout)["result_path"],
            "--resume",
            "--max-candidates",
            "30",
            "--observation-store",
            str(tmp_path / "empty.sqlite"),
        ],
        color=False,
    )

    assert resumed.exit_code == ExitCode.USAGE
    assert "unavailable" in resumed.output
    assert "incompatible" in resumed.output


def test_runner_failure_saves_partial_result_and_uses_provider_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS

    async def fail(_self: object, _evidence: CaseEvidence) -> None:
        raise RunnerExecutionError("synthetic runner failure")

    monkeypatch.setattr("ithrl.cli.DeterministicReducer.reduce", fail)
    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(tmp_path / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.PROVIDER
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert load_result_snapshot(Path(report["result_path"])).summary.status == "failed"
    assert "synthetic runner failure" in result.stderr
    assert "Traceback" not in result.output


def test_candidate_runner_failure_is_checkpointed_and_can_be_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    observations = tmp_path / "observations.sqlite"
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    original_run = FakeRunner.run
    runner_calls = 0

    async def fail_first_candidate(
        runner_instance: FakeRunner, attempt: PreparedAttempt
    ) -> RunResult:
        nonlocal runner_calls
        runner_calls += 1
        if runner_calls == 2:
            raise OSError("candidate transport failed")
        return await original_run(runner_instance, attempt)

    monkeypatch.setattr(FakeRunner, "run", fail_first_candidate)
    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--max-candidates",
            "5",
            "--observation-store",
            str(observations),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.PROVIDER, result.output
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert report["candidate_runs_used"] == 1
    assert "--resume" in report["next_command"]
    snapshot = load_result_snapshot(Path(report["result_path"]))
    failed_ids = snapshot.checkpoint.failed_candidate_observation_ids
    assert len(failed_ids) == 1
    failed = next(
        observation
        for observation in ObservationStore(observations).list_for_experiment(
            snapshot.summary.experiment_digest
        )
        if observation.observation_id == failed_ids[0]
    )
    assert failed.outcome is ObservationOutcome.RUNNER_ERROR


def test_conflicting_local_observations_use_internal_failure_not_provider_failure(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    case = tmp_path / "case"
    store = ObservationStore(tmp_path / "observations.sqlite")
    assert _extract(runner, case).exit_code == ExitCode.SUCCESS
    loaded = CaseWorkspace.open(case).load()
    prepared = prepare_experiment(loaded)
    candidate_digest = prepare_candidate(loaded, prepared.execution_plan).digest
    store.append_many(
        tuple(
            Observation(
                **prepared.identity.model_dump(),
                candidate_digest=candidate_digest,
                role=ObservationRole.CALIBRATION,
                arm=ObservationArm.BASELINE,
                attempt=attempt,
                outcome=outcome,
                attempt_evidence=AttemptEvidence(
                    attempt_id="attempt-" + "0" * 32,
                    attempt_digest="0" * 64,
                    termination_reason="completed",
                    provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                    effective_policy_digest="0" * 64,
                    accounting=RunAccounting(),
                ),
            )
            for attempt, outcome in (
                (1, ObservationOutcome.MATCH),
                (2, ObservationOutcome.NO_MATCH),
            )
        )
    )

    result = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(store.path),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.INTERNAL, result.output
    assert json.loads(result.stdout)["status"] == "failed"
    assert "stored deterministic calibration observations disagree" in result.stderr


def test_normal_output_does_not_echo_captured_request_bodies(tmp_path: Path) -> None:
    trace_data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trace_data["inferences"][0]["request"]["input"][1]["content"] = "private-sentinel"
    trace = tmp_path / "private-trace.json"
    trace.write_text(json.dumps(trace_data), encoding="utf-8")
    case = tmp_path / "case"

    result = CliRunner().invoke(
        app,
        ["extract", str(trace), "--inference", INFERENCE_ID, "--output", str(case)],
        color=False,
    )

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "private-sentinel" not in result.output
    assert "Traceback" not in result.output


def test_extract_lists_available_inferences_in_non_interactive_mode(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["extract", str(FIXTURE), "--output", str(tmp_path / "case")],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert "--inference is required in non-interactive mode" in result.output
    assert INFERENCE_ID in result.output
    assert not (tmp_path / "case").exists()


def test_extract_guides_interactive_inference_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InteractiveInput:
        @staticmethod
        def isatty() -> bool:
            return True

    def choose_first(*_args: object, **_kwargs: object) -> int:
        return 1

    extract_module = import_module("ithrl.cli.extract")
    monkeypatch.setattr(extract_module.sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(extract_module.typer, "prompt", choose_first)

    selected = select_inference(FIXTURE, None, OutputFormat.HUMAN)

    assert selected == INFERENCE_ID


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_extract_does_not_change_existing_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "existing"
    parent.mkdir()
    parent.chmod(0o755)

    result = _extract(CliRunner(), parent / "case")

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert parent.stat().st_mode & 0o777 == 0o755


def test_human_output_treats_paths_as_plain_text(tmp_path: Path) -> None:
    case = tmp_path / "[red]case"

    result = _extract(CliRunner(), case)

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert str(case) in result.output


def test_errors_escape_terminal_control_characters(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(FIXTURE),
            "--inference",
            "unknown\x1b[31m",
            "--output",
            str(tmp_path / "case"),
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    assert "\\u001b" in result.output
    assert "\x1b" not in result.output


def test_json_failures_use_the_versioned_machine_envelope(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "extract",
            str(FIXTURE),
            "--inference",
            "unknown",
            "--output",
            str(tmp_path / "case"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert result.exit_code == ExitCode.USAGE
    report = json.loads(result.stdout)
    assert report == {
        "actions_executed": 0,
        "command": "extract",
        "error_code": "invalid-extraction",
        "exit_code": ExitCode.USAGE,
        "message": ("inference_call_id not found: unknown; available inferences: " + INFERENCE_ID),
        "network_calls": 0,
        "provider_calls": 0,
        "schema_version": 1,
        "status": "error",
    }
    assert result.stderr == ""


def test_unexpected_cli_failures_are_sanitized_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("private-sentinel")

    monkeypatch.setattr(import_module("ithrl.cli.extract"), "list_inferences", fail)
    result = CliRunner().invoke(
        app,
        ["extract", str(FIXTURE), "--output", str(tmp_path / "case"), "--format", "json"],
        color=False,
    )

    assert result.exit_code == ExitCode.INTERNAL
    report = json.loads(result.stdout)
    assert report["error_code"] == "internal-error"
    assert report["message"] == "unexpected internal error (RuntimeError)"
    assert "private-sentinel" not in result.output
    assert "Traceback" not in result.output


def test_cli_help_exposes_only_the_milestone_commands() -> None:
    result = CliRunner().invoke(app, ["--help"], color=False)

    assert result.exit_code == ExitCode.SUCCESS
    for command in ("extract", "shrink"):
        assert command in result.output
    for unavailable in ("record", "preflight", "reproduce", "fuzz", "compare", "export"):
        assert unavailable not in result.output


def test_shrink_help_describes_the_default_resume_budget() -> None:
    result = CliRunner().invoke(app, ["shrink", "--help"], color=False, terminal_width=80)

    assert result.exit_code == ExitCode.SUCCESS
    help_text = " ".join(result.output.replace("│", " ").split())
    assert "100 or twice the saved count, whichever is larger, capped at 10,000" in help_text


def test_vertical_slice_cannot_open_network_connections_or_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("credential-free workflow attempted an external operation")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    runner = CliRunner()
    case = tmp_path / "case"

    extracted = _extract(runner, case, output_format="json")
    reduced = runner.invoke(
        app,
        [
            "shrink",
            str(case),
            "--observation-store",
            str(tmp_path / "observations.sqlite"),
            "--format",
            "json",
        ],
        color=False,
    )

    assert extracted.exit_code == ExitCode.SUCCESS, extracted.output
    assert reduced.exit_code == ExitCode.SUCCESS, reduced.output
