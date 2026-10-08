"""Credential-free operator walkthrough of the composed sampled shrink command."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self, override

import pytest
from typer.testing import CliRunner, Result

import ithrl.calibration as calibration
import ithrl.cli as cli
import ithrl.cli.sampled_reduction as sampled_reduction
import ithrl.cli.sampled_session as sampled_session
import ithrl.observations as observations
import ithrl.sampling as sampling
import ithrl.workflow as workflow
from ithrl.adapters import enumerate_decisions, parse_session
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli import ExitCode, app
from ithrl.cli.native import NativeSelection
from ithrl.execution import CapabilityProfile, ResourceLimits, SafetyPolicy
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
from ithrl.observations import ObservationStore
from ithrl.results import SampledReductionEvidence, load_sampling_snapshot
from ithrl.run_state import SampledRunStateFile
from ithrl.runner import PreparedAttempt, RunAccounting, RunnerRegistry, RunResult, TokenUsage

_SOURCE = Path(__file__).parents[1] / "fixtures/codex-session/rollout.jsonl"
_PRIVATE_CONTEXT = "PRIVATE_FIXTURE_CONTEXT_DO_NOT_DISPLAY"
_PRIVATE_RESPONSE = "PRIVATE_FIXTURE_NATIVE_RESPONSE_DO_NOT_DISPLAY"
_PROVIDER = "fixture-provider"
_ENDPOINT = "https://example.invalid/responses"


class OperatorRunner:
    """Observe synthetic responses without starting a process or contacting a provider."""

    def __init__(self, planted_id: str) -> None:
        self.planted_id = planted_id
        self.calls: list[PreparedAttempt] = []
        self.calibration_matches = True
        self.confirmation_matches: int | None = None
        self.capability_profile = CapabilityProfile.model_validate(
            {
                **CapabilityProfile.deterministic_fake().model_dump(),
                "runner_type": "codex",
                "harness": "codex",
                "runtime_harness_version": "fixture-1",
                "source_schema": "codex-rollout-jsonl",
                "deterministic": False,
                "supported_capture_types": ("logical-session",),
                "supported_materializations": ("source-backed",),
                "reconstruction_method": "codex-app-server-inject-items-v1",
                "supported_targets": ("final-response",),
                "provider_transport": "required",
                "provider_calls_per_attempt": 1,
                "provider": _PROVIDER,
                "endpoint": _ENDPOINT,
                "reports_usage": True,
            }
        )

    @property
    def fingerprint(self) -> str:
        return "credential-free-operator-workflow-v1"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        self.calls.append(attempt)
        confirmation_index = (
            sum(call.phase == "confirmation" and call.arm == "candidate" for call in self.calls) - 1
        )
        return RunResult(
            attempt_id=attempt.attempt_id,
            effective_policy_digest=attempt.safety_policy.digest,
            model_requested=attempt.execution_plan.model_requested,
            model_returned=attempt.execution_plan.model_requested,
            provider=_PROVIDER,
            endpoint=_ENDPOINT,
            final_text=(
                _PRIVATE_RESPONSE
                if self.planted_id in attempt.candidate.removed_context_ids
                or (attempt.phase == "calibration" and not self.calibration_matches)
                or (
                    attempt.phase == "confirmation"
                    and attempt.arm == "candidate"
                    and self.confirmation_matches is not None
                    and confirmation_index >= self.confirmation_matches
                )
                else "Fixture complete."
            ),
            usage=TokenUsage(input_tokens=10, output_tokens=2),
            accounting=RunAccounting(
                provider_calls_started=1,
                provider_calls_completed=1,
                provider_connections_started=1,
                elapsed_milliseconds=1,
            ),
        )


@dataclass
class OperatorCase:
    workspace: CaseWorkspace
    evidence: CaseEvidence
    original: Path
    source: Path
    experiment_path: Path
    store_path: Path
    runner: OperatorRunner
    selection: NativeSelection
    frozen_files: dict[Path, bytes]

    def command(self, *extra: str, acknowledged: bool = True) -> list[str]:
        args = [
            "shrink",
            str(self.workspace.root),
            "--experiment",
            str(self.experiment_path),
            "--original-workspace",
            str(self.original),
            "--observation-store",
            str(self.store_path),
            "--max-provider-calls",
            "100",
            "--max-wall-time",
            "200",
            "--max-candidates",
            "20",
            "--attempt-timeout",
            "1",
            "--format",
            "json",
        ]
        if acknowledged:
            args.append("--acknowledge-model-execution")
        return [*args, *extra]

    def assert_sources_unchanged(self) -> None:
        assert {path: path.read_bytes() for path in self.frozen_files} == self.frozen_files
        assert self.workspace.load().digest == self.evidence.digest


@pytest.fixture
def operator_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OperatorCase:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Native source is synthetic and credential-free, but passes actual adapter/extraction checks.
    source = tmp_path / "rollout.jsonl"
    source.write_text(
        _SOURCE.read_text().replace("Inspect the fixture context.", _PRIVATE_CONTEXT),
        encoding="utf-8",
    )
    session = parse_session(source)
    decision = enumerate_decisions(session)[0]
    workspace = extract_decision(
        session,
        decision_id=decision.decision_id,
        target_id=decision.targets[0].target_id,
        case_path=tmp_path / "case",
    )
    evidence = workspace.load()
    planted = next(n.id for n in evidence.context.nodes if n.kind == "response_item.message")
    runner = OperatorRunner(planted)
    default = ExperimentDefinition.from_toml(evidence.default_experiment)
    search_arm = SamplingArmTarget(required_valid=1, max_attempts=1)
    confirmation_arm = SamplingArmTarget(required_valid=4, max_attempts=4)
    experiment = SampledExperimentDefinition(
        target=default.target,
        oracle=default.oracle,
        runner=NativeRunnerConfig(
            type="codex", required_capability_digest=runner.capability_profile.digest
        ),
        calibration=SampledCalibrationPhase(required_valid=1, minimum_matches=1, max_attempts=1),
        reduction=SampledReductionPlan(
            search=SampledSearchPhase(original=search_arm, candidate=search_arm),
            confirmation=SampledConfirmationPhase(
                original=confirmation_arm, candidate=confirmation_arm, one_sided_confidence=0.8
            ),
            margin=0.3,
            freshness_seconds=60,
        ),
    )
    experiment_path = tmp_path / "sampled.toml"
    experiment_path.write_text(experiment.to_toml(), encoding="utf-8")
    experiment_path.chmod(0o600)
    original = tmp_path / "original workspace"
    original.mkdir()
    (original / "application.txt").write_text("original workspace fixture", encoding="utf-8")
    selection = NativeSelection(
        registry=RunnerRegistry({"codex": lambda _config, _evidence: runner}),
        safety_policy=SafetyPolicy(
            provider_transport="allowed", allowed_provider=_PROVIDER, allowed_endpoint=_ENDPOINT
        ),
        resource_limits=ResourceLimits(max_provider_calls=1, max_wall_time_seconds=1),
        experiment=experiment,
    )

    def select(
        _workspace: CaseWorkspace, _evidence: CaseEvidence, **kwargs: Any
    ) -> NativeSelection:
        selected_experiment = kwargs.get("experiment") or experiment
        limits = selection.resource_limits
        if kwargs.get("attempt_timeout_seconds") is not None:
            limits = limits.model_copy(
                update={"max_wall_time_seconds": kwargs["attempt_timeout_seconds"]}
            )
        return NativeSelection(
            registry=selection.registry,
            safety_policy=selection.safety_policy,
            resource_limits=limits,
            experiment=selected_experiment,
        )

    monkeypatch.setattr(cli, "native_selection", select)
    frozen = {
        path: path.read_bytes()
        for root in (workspace.root, original)
        for path in root.rglob("*")
        if path.is_file()
    }
    frozen.update({source: source.read_bytes(), experiment_path: experiment_path.read_bytes()})
    return OperatorCase(
        workspace,
        evidence,
        original,
        source,
        experiment_path,
        tmp_path / "private" / "observations.sqlite",
        runner,
        selection,
        frozen,
    )


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _json(result: Result) -> dict[str, Any]:
    assert _PRIVATE_CONTEXT not in result.output
    assert _PRIVATE_RESPONSE not in result.output
    return json.loads(result.stdout)


def _assert_no_dispatch(case: OperatorCase) -> None:
    assert case.runner.calls == []
    assert not case.store_path.exists()
    assert not (case.workspace.root / ".runs").exists()
    assert not (case.workspace.root / "results").exists()
    case.assert_sources_unchanged()


def _install_wall_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """Advance command time and recorded observations together without sleeping."""
    offset = timedelta()

    class AdvancedDateTime(datetime):
        @classmethod
        @override
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(datetime.now(tz).timestamp() + offset.total_seconds(), tz)

    def advance(seconds: int) -> None:
        nonlocal offset
        offset += timedelta(seconds=seconds)

    for module in (sampled_reduction, workflow, observations):
        monkeypatch.setattr(module, "datetime", AdvancedDateTime)
    return advance


def _write_operator_experiment(case: OperatorCase, experiment: SampledExperimentDefinition) -> None:
    case.experiment_path.write_text(experiment.to_toml(), encoding="utf-8")
    case.frozen_files[case.experiment_path] = case.experiment_path.read_bytes()


def test_operator_dry_run_exposes_complete_plan_without_creating_run(
    operator_case: OperatorCase,
) -> None:
    result = _invoke(operator_case.command("--dry-run", acknowledged=False))
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = _json(result)
    assert report["status"] == "ready"
    assert report["baseline_policy"] == "strict-fresh-original"
    assert report["confirmation_capacity"] == "protected"
    assert report["confirmation_provider_calls"] == 8
    assert report["minimum_provider_calls"] == 11
    assert report["search_candidate_required_valid"] == 1
    assert report["confirmation_candidate_required_valid"] == 4
    assert report["margin"] == 0.3
    assert report["one_sided_confidence"] == 0.8
    assert report["estimand"] == "valid-only"
    assert report["provider_calls"] == report["actions_executed"] == 0
    assert "no-campaign-wide-error-guarantee" in report["assumptions"]
    command = shlex.split(report["next_command"])
    assert command[command.index("--experiment") + 1] == str(operator_case.experiment_path)
    assert "--acknowledge-model-execution" in command
    _assert_no_dispatch(operator_case)


def test_operator_acknowledgement_precedes_persistence(operator_case: OperatorCase) -> None:
    result = _invoke(operator_case.command(acknowledged=False))
    assert result.exit_code == ExitCode.USAGE, result.output
    assert _json(result)["error_code"] == "acknowledgement-required"
    _assert_no_dispatch(operator_case)


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-file mode contract")
def test_public_experiment_file_refuses_without_dispatch(operator_case: OperatorCase) -> None:
    operator_case.experiment_path.chmod(0o644)
    result = _invoke(operator_case.command())
    assert result.exit_code == ExitCode.USAGE, result.output
    assert _json(result)["error_code"] == "invalid-sampled-input"
    _assert_no_dispatch(operator_case)


def test_unmeasurable_calibration_does_not_start_search(operator_case: OperatorCase) -> None:
    operator_case.runner.calibration_matches = False
    result = _invoke(operator_case.command())
    assert result.exit_code == ExitCode.INCONCLUSIVE, result.output
    report = _json(result)
    assert report["calibration_result"] == "not-measurable"
    assert report["search_result"] == report["confirmation_result"] == "not-run"
    assert report["provider_calls"] == 1
    assert [a.phase for a in operator_case.runner.calls] == ["calibration"]
    operator_case.assert_sources_unchanged()


def test_full_plan_calibration_only_preserves_existing_workflow(
    operator_case: OperatorCase,
) -> None:
    args = operator_case.command("--calibrate-only")
    for flag in ("--max-wall-time", "--max-candidates"):
        index = args.index(flag)
        del args[index : index + 2]
    result = _invoke(args)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = _json(result)
    assert report["status"] == "measurable"
    assert report["provider_calls"] == 1
    assert [a.phase for a in operator_case.runner.calls] == ["calibration"]
    assert not (operator_case.workspace.root / ".runs").exists()
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize(
    ("flag", "value"), [("--max-provider-calls", "10"), ("--max-wall-time", "10")]
)
def test_full_command_budget_refusal_precedes_calibration(
    operator_case: OperatorCase, flag: str, value: str
) -> None:
    result = _invoke(operator_case.command(flag, value))
    assert result.exit_code == ExitCode.USAGE, result.output
    report = _json(result)
    assert report["error_code"] == "sampling-budget-too-small"
    assert report["provider_calls"] == 0
    assert report["minimum_provider_calls"] == 11
    _assert_no_dispatch(operator_case)


@pytest.mark.parametrize(
    ("error_code", "next_action"),
    [
        (
            "confirmation-unattainable",
            "Revise the confirmation sample plan: even all matching samples cannot establish "
            "preservation at the configured margin and confidence.",
        ),
        (
            "sampling-freshness-too-small",
            "Choose attempt deadlines and a declared freshness window that admit a complete "
            "comparison, including cancellation time, before expiry.",
        ),
    ],
)
def test_statistical_preflight_refusals_are_private_and_non_mutating(
    operator_case: OperatorCase, error_code: str, next_action: str
) -> None:
    experiment = operator_case.selection.experiment
    assert experiment.reduction is not None
    plan = experiment.reduction
    if error_code == "confirmation-unattainable":
        arm = SamplingArmTarget(required_valid=1, max_attempts=1)
        reduction = plan.model_copy(
            update={
                "margin": 0.0,
                "confirmation": SampledConfirmationPhase(
                    original=arm, candidate=arm, one_sided_confidence=0.95
                ),
            }
        )
    else:
        # Eight confirmation attempts each need one deadline second plus one grace second.
        operator_case.runner.capability_profile = (
            operator_case.runner.capability_profile.model_copy(
                update={"cancellation_grace_seconds": 1}
            )
        )
        experiment = experiment.model_copy(
            update={
                "runner": NativeRunnerConfig(
                    type="codex",
                    required_capability_digest=operator_case.runner.capability_profile.digest,
                )
            }
        )
        reduction = plan.model_copy(update={"freshness_seconds": 16})
    _write_operator_experiment(
        operator_case, experiment.model_copy(update={"reduction": reduction})
    )
    result = _invoke(operator_case.command("--dry-run"))
    assert result.exit_code == ExitCode.USAGE, result.output
    report = _json(result)
    assert report["error_code"] == error_code
    assert report["status"] == "refused"
    assert report["provider_call_accounting"] == "not-started"
    assert report["provider_calls"] == 0
    assert report["next_action"] == next_action
    assert result.stderr == ""
    _assert_no_dispatch(operator_case)
    human = _invoke(operator_case.command("--dry-run", "--format", "human"))
    assert human.exit_code == ExitCode.USAGE, human.output
    assert f"Sampled run refused before calibration. ({error_code})" in human.stderr
    assert "Provider calls: 0 (model execution did not start)." in human.stderr
    assert next_action in " ".join(human.stderr.split())
    assert _PRIVATE_CONTEXT not in human.output
    assert _PRIVATE_RESPONSE not in human.output
    assert human.stdout == ""
    _assert_no_dispatch(operator_case)


def test_operator_search_confirmation_result_and_exact_resume(operator_case: OperatorCase) -> None:
    result = _invoke(operator_case.command())
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = _json(result)
    preflight = json.loads(result.stderr.splitlines()[0])
    assert preflight["status"] == "ready"
    assert preflight["provider_calls"] == 0
    assert preflight["confirmation_capacity"] == "protected"
    assert report["status"] == "confirmed"
    assert report["calibration_result"] == "measurable"
    assert report["search_result"] == "search-reduced"
    assert report["confirmation_result"] == "confirmed-preserving"
    assert report["reliability_status"] == "complete"
    assert report["removed_context_count"] == 2
    assert report["retained_context_count"] == len(operator_case.evidence.context.nodes) - 2
    assert report["confirmation_capacity"] == "consumed"
    assert report["provider_calls"] == len(operator_case.runner.calls)
    assert report["known_tokens"] == 12 * len(operator_case.runner.calls)
    assert report["actions_executed"] == 0
    destination = Path(report["result_path"])
    saved_result = SampledReductionEvidence.model_validate_json(
        (destination / "result.json").read_text()
    )
    assert saved_result.confirmation is not None
    assert saved_result.confirmation.evidence.complete
    assert len(saved_result.confirmation.evidence.attempts) == 8
    store = ObservationStore(operator_case.store_path)
    snapshot = load_sampling_snapshot(destination / "sampling", store=store)
    assert snapshot.checkpoint == saved_result.checkpoint
    assert snapshot.selection is not None
    assert snapshot.selection.plan == saved_result.confirmation.evidence.plan
    private_before = {p: p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    calls_before = len(operator_case.runner.calls)
    for source in (Path(report["run_state_path"]), destination):
        resumed = _invoke(
            ["shrink", str(source), "--resume", "--acknowledge-model-execution", "--format", "json"]
        )
        assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
        resumed_report = _json(resumed)
        assert resumed_report["confirmation_result"] == "confirmed-preserving"
        assert resumed_report["max_provider_calls"] == 100
        assert resumed_report["max_wall_time_seconds"] == 200
        assert resumed_report["max_candidate_comparisons"] == 20
        assert len(operator_case.runner.calls) == calls_before
    assert {p: p.read_bytes() for p in private_before} == private_before
    if os.name == "posix":
        for path in destination.rglob("*"):
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    operator_case.assert_sources_unchanged()


def test_human_walkthrough_reports_the_same_composed_outcome(operator_case: OperatorCase) -> None:
    result = _invoke(operator_case.command("--format", "human", "--verbose"))
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "Sampled shrink: confirmed" in result.stdout
    assert "measurable" in result.stdout
    assert "search-reduced" in result.stdout
    assert "confirmed-preserving" in result.stdout
    assert "removed" in result.stdout
    assert "one-sided" in result.stdout
    assert "independent valid observations" in result.stdout
    assert "consumed" in result.stdout
    assert _PRIVATE_CONTEXT not in result.output
    assert _PRIVATE_RESPONSE not in result.output
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize(("matches", "expected"), [(0, "loses"), (2, "inconclusive")])
def test_terminal_confirmation_is_not_resampled(
    operator_case: OperatorCase, matches: int, expected: str
) -> None:
    operator_case.runner.confirmation_matches = matches
    result = _invoke(operator_case.command())
    assert result.exit_code == ExitCode.INCONCLUSIVE, result.output
    report = _json(result)
    assert report["confirmation_result"] == expected
    assert report["search_result"] == "search-reduced"
    assert report["next_command"] is None
    assert report["confirmation_capacity"] == "consumed"
    destination = Path(report["result_path"])
    private_before = {p: p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    calls = len(operator_case.runner.calls)
    resumed = _invoke(
        [
            "shrink",
            str(destination),
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ]
    )
    assert resumed.exit_code == ExitCode.INCONCLUSIVE, resumed.output
    assert _json(resumed)["confirmation_result"] == expected
    assert len(operator_case.runner.calls) == calls
    assert sum(a.phase == "confirmation" for a in operator_case.runner.calls) == 8
    assert {p: p.read_bytes() for p in private_before} == private_before
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize("phase", ["search", "confirmation"])
def test_operator_resumes_same_durable_block_after_interruption(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    observe = sampling.observe_once
    interrupted = False

    async def stop_after_observation(**kwargs: Any):
        nonlocal interrupted
        observation = await observe(**kwargs)
        if kwargs["attempt"].phase == phase and not interrupted:
            interrupted = True
            raise asyncio.CancelledError
        return observation

    with monkeypatch.context() as patch:
        patch.setattr(sampling, "observe_once", stop_after_observation)
        stopped = _invoke(operator_case.command())
    assert stopped.exit_code == ExitCode.CANCELLED, stopped.output
    report = _json(stopped)
    assert report["status"] == "interrupted"
    assert report["provider_call_accounting"] == "accounted"
    assert report["pending_provider_calls"] == 0
    command = shlex.split(report["next_command"])
    assert command[:2] == ["ithrl", "shrink"]
    run_file = SampledRunStateFile.open(Path(report["run_state_path"]))
    store = ObservationStore(operator_case.store_path)
    with run_file.acquire_lock(store):
        before = run_file.load(store)
        blocks = run_file.evidence(store)
    partial = next(block for block in blocks if block.plan.role == phase)
    assert len(partial.attempts) == 1
    assert before.pending is None
    calls_before = len(operator_case.runner.calls)
    resumed = _invoke([*command[1:], "--format", "json"])
    assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
    assert _json(resumed)["confirmation_result"] == "confirmed-preserving"
    with run_file.acquire_lock(store):
        after = run_file.load(store)
        resumed_block = next(
            block
            for block in run_file.evidence(store)
            if block.plan.block_id == partial.plan.block_id
        )
    assert resumed_block.plan == partial.plan
    assert resumed_block.attempts[:1] == partial.attempts
    assert after.allowance == before.allowance
    assert sum(a.phase == "calibration" for a in operator_case.runner.calls) == 1
    if phase == "confirmation":
        assert len(operator_case.runner.calls) == calls_before + 7
        assert after.selection == before.selection
    ids = [attempt.attempt_id for attempt in operator_case.runner.calls]
    assert len(ids) == len(set(ids))
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize("output_format", ["json", "human"])
def test_reserved_dispatch_without_observation_stays_charged_and_never_replays(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    reserved_attempts: list[PreparedAttempt] = []

    async def interrupt_before_observation(**kwargs: Any) -> None:
        # The collector has already persisted its reservation. No observation can prove
        # whether dispatch reached a provider, even though this synthetic runner is idle.
        reserved_attempts.append(kwargs["attempt"])
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(sampling, "observe_once", interrupt_before_observation)
        stopped = _invoke(operator_case.command("--format", output_format))
    assert stopped.exit_code == ExitCode.UNSUPPORTED, stopped.output
    assert _PRIVATE_CONTEXT not in stopped.output
    assert _PRIVATE_RESPONSE not in stopped.output
    assert len(reserved_attempts) == 1
    assert [a.phase for a in operator_case.runner.calls] == ["calibration"]
    if output_format == "json":
        report = _json(stopped)
        assert report["status"] == "refused"
        assert report["stop_reason"] == "unresolved-dispatch"
        assert report["provider_call_accounting"] == "started-unknown"
        assert report["provider_calls"] is None
        assert report["actual_provider_calls"] == 1
        assert report["pending_provider_calls"] == 1
        assert report["charged_provider_calls"] == 2
        assert report["reliability_status"] == "unknown"
        assert report["next_command"] is None
    else:
        assert "Sampled shrink: refused" in stopped.stdout
        assert "unknown actual; 2 charged / 100 maximum" in stopped.stdout
        assert "1 pending" in stopped.stdout
        assert "unresolved-dispatch" in stopped.stdout
    run_path = next((operator_case.workspace.root / ".runs").glob("*/sampled-state.json"))
    run_file = SampledRunStateFile.open(run_path.parent)
    store = ObservationStore(operator_case.store_path)
    with run_file.acquire_lock(store):
        before = run_file.load(store)
        blocks_before = run_file.evidence(store)
    assert before.pending is not None
    assert before.pending.attempt_id == reserved_attempts[0].attempt_id
    assert len(blocks_before) == 1
    assert blocks_before[0].attempts == ()
    assert before.selection is None
    resumed = _invoke(
        [
            "shrink",
            str(run_path.parent),
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ]
    )
    assert resumed.exit_code == ExitCode.UNSUPPORTED, resumed.output
    resumed_report = _json(resumed)
    assert resumed_report["stop_reason"] == "unresolved-dispatch"
    assert resumed_report["pending_provider_calls"] == 1
    assert resumed_report["charged_provider_calls"] == 2
    assert resumed_report["provider_calls"] is None
    assert resumed_report["next_command"] is None
    with run_file.acquire_lock(store):
        assert run_file.load(store) == before
        assert run_file.evidence(store) == blocks_before
    assert [a.phase for a in operator_case.runner.calls] == ["calibration"]
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize("metadata", ["missing", "malformed"])
def test_interrupted_session_publication_does_not_propose_unusable_resume(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    def interrupt_session_publication(_root: Path, _session: Any) -> None:
        if metadata == "malformed":
            path = _root / "cli-session.json"
            path.write_text(_PRIVATE_CONTEXT, encoding="utf-8")
            path.chmod(0o600)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(sampled_reduction, "save_cli_session", interrupt_session_publication)
        stopped = _invoke(operator_case.command())
    assert stopped.exit_code == ExitCode.CANCELLED, stopped.output
    report = _json(stopped)
    assert report["status"] == "interrupted"
    assert report["provider_call_accounting"] == "accounted"
    assert report["provider_calls"] == report["charged_provider_calls"] == 0
    assert report["attempts_used"] == 0
    assert operator_case.runner.calls == []
    run_root = Path(report["run_state_path"])
    assert (run_root / "sampled-state.json").is_file()
    assert (run_root / "cli-session.json").exists() == (metadata == "malformed")
    state_before = (run_root / "sampled-state.json").read_bytes()
    # The durable state cannot reconstruct CLI inputs without its private session pointer.
    # A proposed --resume command would deterministically refuse before dispatch.
    unusable_resume = _invoke(
        [
            "shrink",
            str(run_root),
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ]
    )
    assert unusable_resume.exit_code == ExitCode.USAGE, unusable_resume.output
    assert _json(unusable_resume)["error_code"] == (
        "invalid-input" if metadata == "missing" else "sampled-resume-mismatch"
    )
    assert operator_case.runner.calls == []
    assert (run_root / "sampled-state.json").read_bytes() == state_before
    assert report["next_command"] is None
    assert report["next_command_shell"] is None
    assert "new run" in report["next_action"]
    assert not (operator_case.workspace.root / "results").exists()
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize("metadata", ["missing", "malformed"])
def test_unavailable_resume_metadata_preserves_recorded_work_accounting(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    observe = sampling.observe_once

    async def stop_after_observation_without_metadata(**kwargs: Any):
        observation = await observe(**kwargs)
        if kwargs["attempt"].phase == "search":
            path = next((operator_case.workspace.root / ".runs").glob("*/cli-session.json"))
            if metadata == "missing":
                path.unlink()
            else:
                path.write_text(_PRIVATE_CONTEXT, encoding="utf-8")
            raise asyncio.CancelledError
        return observation

    with monkeypatch.context() as patch:
        patch.setattr(sampling, "observe_once", stop_after_observation_without_metadata)
        stopped = _invoke(operator_case.command())
    # The observation is durable; result publication subsequently refuses the missing metadata.
    assert stopped.exit_code == ExitCode.UNSUPPORTED, stopped.output
    report = _json(stopped)
    assert report["status"] == "refused"
    assert report["stop_reason"] == "execution-refused"
    assert report["provider_call_accounting"] == "accounted"
    assert report["provider_calls"] == report["charged_provider_calls"] == 2
    assert report["attempts_used"] == 2
    assert report["pending_provider_calls"] == 0
    assert report["next_command"] is report["next_command_shell"] is None
    assert "Inspect the private run evidence" in report["next_action"]
    assert "no attempt was dispatched" not in report["next_action"]
    assert [attempt.phase for attempt in operator_case.runner.calls] == ["calibration", "search"]
    run_file = SampledRunStateFile.open(Path(report["run_state_path"]))
    store = ObservationStore(operator_case.store_path)
    with run_file.acquire_lock(store):
        state = run_file.load(store)
        assert state.pending is None
        assert len(run_file.evidence(store)[0].attempts) == 1
    operator_case.assert_sources_unchanged()


def test_interruption_after_session_publication_can_resume_without_recreating_run(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_session = sampled_reduction.save_cli_session

    def stop_after_publication(root: Path, session: sampled_session.SampledCliSession) -> None:
        save_session(root, session)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(sampled_reduction, "save_cli_session", stop_after_publication)
        stopped = _invoke(operator_case.command())
    assert stopped.exit_code == ExitCode.CANCELLED, stopped.output
    before = _json(stopped)
    assert before["provider_calls"] == before["attempts_used"] == 0
    assert operator_case.runner.calls == []
    run_path = Path(before["run_state_path"])
    assert (run_path / "cli-session.json").is_file()
    resumed = _invoke([*shlex.split(before["next_command"])[1:], "--format", "json"])
    assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
    after = _json(resumed)
    assert after["run_state_path"] == before["run_state_path"]
    assert after["confirmation_result"] == "confirmed-preserving"
    assert len(list((operator_case.workspace.root / ".runs").iterdir())) == 1
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize("output_format", ["json", "human"])
def test_result_publication_failure_recovers_without_repeating_confirmation(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    sync_directory = sampled_session._sync_directory

    def fail_before_publication(directory: Path) -> None:
        if directory.name == "result" and directory.parent.name.startswith(".sampled-"):
            raise OSError(_PRIVATE_RESPONSE)
        sync_directory(directory)

    with monkeypatch.context() as patch:
        # Fail after private result files and snapshot are staged, before atomic rename.
        patch.setattr(sampled_session, "_sync_directory", fail_before_publication)
        stopped = _invoke(operator_case.command("--format", output_format))
    assert stopped.exit_code == ExitCode.UNSUPPORTED, stopped.output
    assert _PRIVATE_CONTEXT not in stopped.output
    assert _PRIVATE_RESPONSE not in stopped.output
    if output_format == "json":
        report = _json(stopped)
        assert report["status"] == "refused"
        assert report["stop_reason"] == "execution-refused"
        assert report["confirmation_result"] == "confirmed-preserving"
        assert report["provider_call_accounting"] == "accounted"
        assert report["result_path"] is None
    else:
        assert "Sampled shrink: refused" in stopped.stdout
        assert "confirmed-preserving" in stopped.stdout
        assert "execution-refused" in stopped.stdout
    results_root = operator_case.workspace.root / "results"
    assert results_root.is_dir()
    assert list(results_root.iterdir()) == []
    run_path = next((operator_case.workspace.root / ".runs").glob("*/sampled-state.json"))
    run_file = SampledRunStateFile.open(run_path.parent)
    store = ObservationStore(operator_case.store_path)
    with run_file.acquire_lock(store):
        before = run_file.load(store)
        blocks_before = run_file.evidence(store)
    confirmation = next(block for block in blocks_before if block.plan.role == "confirmation")
    assert before.pending is None
    assert before.selection is not None
    assert confirmation.complete
    assert len(confirmation.attempts) == 8
    calls_before = tuple(operator_case.runner.calls)
    resumed = _invoke(
        [
            "shrink",
            str(run_path.parent),
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ]
    )
    assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
    report = _json(resumed)
    assert report["confirmation_result"] == "confirmed-preserving"
    assert report["provider_calls"] == len(calls_before)
    assert report["confirmation_capacity"] == "consumed"
    destination = Path(report["result_path"])
    assert list(results_root.iterdir()) == [destination]
    saved = SampledReductionEvidence.model_validate_json((destination / "result.json").read_text())
    assert saved.confirmation is not None
    assert saved.confirmation.evidence == confirmation
    assert tuple(operator_case.runner.calls) == calls_before
    with run_file.acquire_lock(store):
        assert run_file.load(store) == before
        assert run_file.evidence(store) == blocks_before
    operator_case.assert_sources_unchanged()


def test_calibration_rechecks_overall_wall_budget_before_next_attempt(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = operator_case.selection.experiment.model_copy(
        update={
            "calibration": SampledCalibrationPhase(
                required_valid=2, minimum_matches=2, max_attempts=2
            )
        }
    )
    _write_operator_experiment(operator_case, experiment)
    advance = _install_wall_clock(monkeypatch)
    observe = calibration.observe_once

    async def advance_after_durable_observation(**kwargs: Any):
        observation = await observe(**kwargs)
        advance(300)
        return observation

    monkeypatch.setattr(calibration, "observe_once", advance_after_durable_observation)
    stopped = _invoke(operator_case.command())
    assert stopped.exit_code == ExitCode.BUDGET, stopped.output
    report = _json(stopped)
    assert report["stop_reason"] == "wall-time-budget"
    assert report["calibration_result"] == "incomplete"
    assert report["search_result"] == report["confirmation_result"] == "not-run"
    assert report["wall_time_seconds"] >= 300
    assert report["provider_calls"] == 1
    assert [attempt.phase for attempt in operator_case.runner.calls] == ["calibration"]
    run_file = SampledRunStateFile.open(Path(report["run_state_path"]))
    store = ObservationStore(operator_case.store_path)
    with run_file.acquire_lock(store):
        state = run_file.load(store)
    assert len(state.calibration_observation_ids) == 1
    assert state.checkpoint is None
    assert state.pending is None
    assert state.allowance.max_wall_time_seconds == 200
    recorded = store.get_many(state.calibration_observation_ids)
    assert recorded[0].attempt_evidence.attempt_id == operator_case.runner.calls[0].attempt_id
    proposal = shlex.split(report["next_command"])
    assert int(proposal[proposal.index("--max-wall-time") + 1]) > 300
    assert int(proposal[proposal.index("--max-provider-calls") + 1]) >= 100
    operator_case.assert_sources_unchanged()


def test_expired_wall_allowance_proposes_executable_extension_for_same_block(
    operator_case: OperatorCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = operator_case.selection.experiment
    assert experiment.reduction is not None
    _write_operator_experiment(
        operator_case,
        experiment.model_copy(
            update={"reduction": experiment.reduction.model_copy(update={"freshness_seconds": 600})}
        ),
    )
    advance = _install_wall_clock(monkeypatch)
    observe = sampling.observe_once

    async def pause_after_search_observation(**kwargs: Any):
        observation = await observe(**kwargs)
        if kwargs["attempt"].phase == "search":
            raise asyncio.CancelledError
        return observation

    with monkeypatch.context() as patch:
        patch.setattr(sampling, "observe_once", pause_after_search_observation)
        paused = _invoke(operator_case.command())
    assert paused.exit_code == ExitCode.CANCELLED, paused.output
    paused_report = _json(paused)
    run_file = SampledRunStateFile.open(Path(paused_report["run_state_path"]))
    store = ObservationStore(operator_case.store_path)
    with run_file.acquire_lock(store):
        before = run_file.load(store)
        partial = run_file.evidence(store)[0]
    assert len(partial.attempts) == 1
    assert before.selection is None
    calls = len(operator_case.runner.calls)
    advance(201)
    expired = _invoke(
        [
            "shrink",
            str(run_file.path.parent),
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
        ]
    )
    assert expired.exit_code == ExitCode.BUDGET, expired.output
    report = _json(expired)
    assert report["stop_reason"] == "wall-time-exhausted"
    assert report["wall_time_seconds"] >= 201
    assert report["confirmation_result"] == "not-run"
    assert len(operator_case.runner.calls) == calls
    proposal = shlex.split(report["next_command"])
    assert int(proposal[proposal.index("--max-wall-time") + 1]) > 201
    # Invoke the displayed continuation verbatim, including its default human presentation.
    resumed = _invoke(proposal[1:])
    assert resumed.exit_code == ExitCode.SUCCESS, resumed.output
    assert "confirmed-preserving" in resumed.stdout
    assert _PRIVATE_CONTEXT not in resumed.output
    assert _PRIVATE_RESPONSE not in resumed.output
    with run_file.acquire_lock(store):
        after = run_file.load(store)
        blocks = run_file.evidence(store)
    resumed_block = next(block for block in blocks if block.plan.block_id == partial.plan.block_id)
    assert resumed_block.plan == partial.plan
    assert resumed_block.attempts[:1] == partial.attempts
    assert after.original_identity == before.original_identity
    assert after.allowance.max_wall_time_seconds > before.allowance.max_wall_time_seconds
    assert after.approvals[: len(before.approvals)] == before.approvals
    assert after.calibration_observation_ids == before.calibration_observation_ids
    assert after.selection is not None
    confirmation = next(block for block in blocks if block.plan.role == "confirmation")
    assert confirmation.plan == after.selection.plan
    assert len(confirmation.attempts) == 8
    ids = [attempt.attempt_id for attempt in operator_case.runner.calls]
    assert len(ids) == len(set(ids))
    assert sum(attempt.phase == "calibration" for attempt in operator_case.runner.calls) == 1
    operator_case.assert_sources_unchanged()


@pytest.mark.parametrize("override", ["workspace", "attempt", "experiment", "budget", "candidates"])
def test_resume_mismatch_refuses_without_dispatch(
    operator_case: OperatorCase, override: str
) -> None:
    completed = _invoke(operator_case.command())
    assert completed.exit_code == ExitCode.SUCCESS, completed.output
    report = _json(completed)
    calls = len(operator_case.runner.calls)
    extras = {
        "workspace": ["--original-workspace", str(operator_case.workspace.root)],
        "attempt": ["--attempt-timeout", "2"],
        "experiment": ["--experiment", str(operator_case.experiment_path)],
        "budget": ["--max-provider-calls", "99"],
        "candidates": ["--max-candidates", "19"],
    }[override]
    resumed = _invoke(
        [
            "shrink",
            report["run_state_path"],
            "--resume",
            "--acknowledge-model-execution",
            "--format",
            "json",
            *extras,
        ]
    )
    assert resumed.exit_code == ExitCode.USAGE, resumed.output
    assert _json(resumed)["provider_calls"] == 0
    assert len(operator_case.runner.calls) == calls
    operator_case.assert_sources_unchanged()
