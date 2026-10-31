"""Shrink command preparation, result persistence, and resume validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli.output import CommandOutput, ExitCode, shell_command
from ithrl.context import JsonObject, content_digest
from ithrl.execution import ResourceLimits, SafetyPolicy
from ithrl.experiment import (
    SampledExperimentDefinition,
    UnavailableRunnerConfig,
    parse_experiment_toml,
)
from ithrl.extraction import load_extracted_case
from ithrl.measurement import RunnerExecutionError
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
)
from ithrl.reduce import (
    DEFAULT_MAX_CANDIDATE_RUNS,
    MAX_CANDIDATE_RUNS,
    DeterministicReducer,
    ReductionCheckpoint,
    ResumeMismatchError,
    validate_checkpoint_identity,
)
from ithrl.results import (
    ResultSnapshot,
    ResultSnapshotError,
    ResultStatus,
    ResultSummary,
    SearchResult,
    load_result_snapshot,
    write_result_snapshot,
)
from ithrl.run_state import RunStateError, RunStateFile
from ithrl.runner import RunnerRegistry, prepare_candidate
from ithrl.workflow import (
    PhaseOutcome,
    PreparedExperiment,
    prepare_experiment,
)

if TYPE_CHECKING:
    from ithrl.cli.native import NativeSelection


@dataclass(frozen=True, slots=True)
class SavedOutcome:
    report: CommandOutput
    exit_code: ExitCode


@dataclass(frozen=True, slots=True)
class _OutcomeDisposition:
    status: ResultStatus
    search_result: SearchResult
    exit_code: ExitCode


@dataclass(frozen=True, slots=True)
class _NextStep:
    action: str | None = None
    command: str | None = None
    command_shell: Literal["posix", "powershell"] | None = None


@dataclass(frozen=True, slots=True)
class _AttemptTotals:
    network_calls: int = 0
    provider_calls: int = 0
    provider_calls_may_have_started: int = 0
    actions_executed: int = 0


def save_outcome(
    *,
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    reducer: DeterministicReducer,
    previous: ResultSnapshot | None,
    phases: PhaseOutcome,
    calibrate_only: bool,
    max_candidates: int,
    observation_store_path: Path | None,
    observations: tuple[Observation, ...],
    run_state: RunStateFile,
) -> SavedOutcome:
    checkpoint = phases.reduction.checkpoint if phases.reduction is not None else reducer.checkpoint
    removed = frozenset(checkpoint.removed_context_ids)
    candidate = prepare_candidate(evidence, prepared.execution_plan, removed)
    request = _materialized_request(evidence, removed)
    original_ids = tuple(node.id for node in evidence.context.nodes)
    retained_ids, removed_ids = _partition_ids(original_ids, removed)
    run_id = f"result-{candidate.digest[:12]}-{uuid4().hex[:8]}"
    totals = _attempt_totals(observations)
    destination = workspace.root / "results" / run_id
    disposition = _classify_outcome(phases, checkpoint, calibrate_only=calibrate_only)
    next_step = _plan_next_step(
        disposition.status,
        checkpoint=checkpoint,
        workspace=workspace,
        destination=destination,
        max_candidates=max_candidates,
        observation_store_path=observation_store_path,
    )

    retained_nodes = tuple(node for node in evidence.context.nodes if node.id in retained_ids)
    retained_lines = tuple(node.model_dump_json() for node in retained_nodes)
    provenance = evidence.manifest.provenance.model_dump(mode="json")
    summary = ResultSummary(
        run_id=run_id,
        status=disposition.status,
        case_digest=evidence.digest,
        experiment_digest=prepared.experiment.digest,
        target_digest=prepared.experiment.target.digest,
        execution_plan_digest=prepared.execution_plan.digest,
        candidate_digest=candidate.digest,
        candidate_materialization=candidate.materialization,
        source_artifact_digests=candidate.source_artifact_digests,
        reduced_request_digest=content_digest(request) if request is not None else None,
        retained_context_digest=content_digest(
            [node.model_dump(mode="json") for node in retained_nodes]
        ),
        provenance_digest=content_digest(provenance),
        checkpoint_digest=content_digest(checkpoint.model_dump(mode="json")),
        runner_fingerprint=prepared.runner.fingerprint,
        algorithm_digest=checkpoint.algorithm_digest,
        original_context_count=len(original_ids),
        retained_context_count=len(retained_ids),
        original_context_ids=original_ids,
        retained_context_ids=retained_ids,
        removed_context_ids=removed_ids,
        calibration_result=phases.calibration_result,
        calibration_observation_ids=_observation_ids(phases.calibration_observation),
        search_result=disposition.search_result,
        confirmation_result=phases.confirmation_result,
        confirmation_observation_ids=_observation_ids(phases.confirmation_observation),
        candidate_runs_used=checkpoint.candidate_runs_used,
        max_candidate_runs=max_candidates,
        observations_recorded=len(observations),
        network_calls=totals.network_calls,
        provider_calls=totals.provider_calls,
        provider_calls_may_have_started=totals.provider_calls_may_have_started,
        actions_executed=totals.actions_executed,
        source_case=str(workspace.root.absolute()),
        resume_of=previous.summary.run_id if previous is not None else None,
        next_action=next_step.action,
        next_command=next_step.command,
        next_command_shell=next_step.command_shell,
    )
    write_result_snapshot(
        destination=destination,
        summary=summary,
        request=request,
        retained_context_lines=retained_lines,
        experiment_source=workspace.root / "experiments" / "default.toml",
        provenance=provenance,
        execution_plan=prepared.execution_plan,
        checkpoint=checkpoint,
    )
    # Publish the immutable result before replacing the active run with its completed pointer.
    run_state.complete(destination)
    report = CommandOutput(
        command="shrink",
        status=disposition.status,
        case_path=str(workspace.root.absolute()),
        case_digest=evidence.digest,
        result_path=str(destination.absolute()),
        original_context_count=len(original_ids),
        retained_context_count=len(retained_ids),
        calibration_result=phases.calibration_result,
        search_result=disposition.search_result,
        confirmation_result=phases.confirmation_result,
        candidate_runs_used=checkpoint.candidate_runs_used,
        max_candidate_runs=max_candidates,
        observations_recorded=len(observations),
        execution_plan_digest=prepared.execution_plan.digest,
        runner_type=prepared.execution_plan.capability.runner_type,
        runner_version=prepared.execution_plan.capability.runner_version,
        target_type=prepared.execution_plan.target_type,
        protected_context_ids=prepared.experiment.oracle.protected_context_ids,
        run_state_path=str(run_state.path.parent.absolute()),
        next_action=next_step.action,
        next_command=next_step.command,
        next_command_shell=next_step.command_shell,
    )
    return SavedOutcome(report=report, exit_code=disposition.exit_code)


def _attempt_totals(observations: tuple[Observation, ...]) -> _AttemptTotals:
    network_calls = 0
    provider_calls = 0
    provider_calls_may_have_started = 0
    actions_executed = 0
    for observation in observations:
        evidence = observation.attempt_evidence
        accounting = evidence.accounting
        provider_calls_may_have_started += evidence.provider_calls_possible
        if accounting is None:
            provider_calls += int(evidence.provider_call_started)
            continue
        network_calls += accounting.provider_connections_started + accounting.tool_network_requests
        provider_calls += accounting.provider_calls_started
        actions_executed += accounting.actions_executed
    return _AttemptTotals(
        network_calls,
        provider_calls,
        provider_calls_may_have_started,
        actions_executed,
    )


def _materialized_request(evidence: CaseEvidence, removed: frozenset[str]) -> JsonObject | None:
    return evidence.materialize(removed) if evidence.has_request else None


def _partition_ids(
    original_ids: tuple[str, ...], removed: frozenset[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    retained_ids = tuple(node_id for node_id in original_ids if node_id not in removed)
    removed_ids = tuple(node_id for node_id in original_ids if node_id in removed)
    return retained_ids, removed_ids


def _observation_ids(observation: Observation | None) -> tuple[str, ...]:
    return () if observation is None else (observation.observation_id,)


def _classify_outcome(
    phases: PhaseOutcome,
    checkpoint: ReductionCheckpoint,
    *,
    calibrate_only: bool,
) -> _OutcomeDisposition:
    if checkpoint.final_sweep_complete:
        partial_search: SearchResult = "search-1-minimal"
    elif checkpoint.candidate_runs_used:
        partial_search = "search-incomplete"
    else:
        partial_search = "not-run"
    if phases.stop_reason == "user-interrupt":
        return _OutcomeDisposition("interrupted", partial_search, ExitCode.BUDGET)
    if phases.stop_reason == "operational-error":
        exit_code = (
            ExitCode.PROVIDER
            if isinstance(phases.operational_error, RunnerExecutionError)
            else ExitCode.INTERNAL
        )
        return _OutcomeDisposition("failed", partial_search, exit_code)
    if phases.calibration_result == "not-observed":
        return _OutcomeDisposition("not-observed", "not-run", ExitCode.INCONCLUSIVE)
    if calibrate_only:
        return _OutcomeDisposition("calibrated", "not-run", ExitCode.SUCCESS)
    if phases.reduction is not None and not phases.reduction.final_sweep_complete:
        return _OutcomeDisposition("stopped", "search-incomplete", ExitCode.BUDGET)
    if phases.confirmation_result == "failed":
        return _OutcomeDisposition("confirmation-failed", "search-1-minimal", ExitCode.INCONCLUSIVE)
    return _OutcomeDisposition("reduced", "search-1-minimal", ExitCode.SUCCESS)


def _plan_next_step(
    status: ResultStatus,
    *,
    checkpoint: ReductionCheckpoint,
    workspace: CaseWorkspace,
    destination: Path,
    max_candidates: int,
    observation_store_path: Path | None,
) -> _NextStep:
    if status == "calibrated":
        arguments = ["ithrl", "shrink", str(workspace.root.absolute())]
        if observation_store_path is not None:
            arguments.extend(("--observation-store", str(observation_store_path.absolute())))
        command, command_shell = shell_command(tuple(arguments))
        return _NextStep("Run the full search when ready.", command, command_shell)
    if status == "not-observed":
        return _NextStep("Check the target and original case before running the experiment again.")
    if status == "confirmation-failed":
        return _NextStep(
            "Review the saved evidence before changing the experiment or trying again."
        )

    next_budget: int | None = None
    if status in {"stopped", "interrupted"}:
        if max_candidates >= MAX_CANDIDATE_RUNS:
            return _NextStep(
                f"Search reached the supported limit of {MAX_CANDIDATE_RUNS} candidate "
                "attempts. Review the partial result before changing the case or experiment."
            )
        next_budget = min(MAX_CANDIDATE_RUNS, max(max_candidates * 2, 2))
        action = "Continue the saved search with a larger total candidate limit."
    elif status == "failed":
        if checkpoint.final_sweep_complete or checkpoint.candidate_runs_used < max_candidates:
            next_budget = max_candidates
        elif max_candidates < MAX_CANDIDATE_RUNS:
            next_budget = min(MAX_CANDIDATE_RUNS, max_candidates * 2)
        if next_budget is None:
            return _NextStep(
                "The failed attempt reached the supported candidate limit. "
                "Review the saved evidence before changing the case or experiment."
            )
        action = "Resolve the reported error, then continue the saved experiment."
    else:
        return _NextStep()

    arguments = [
        "ithrl",
        "shrink",
        str(destination.absolute()),
        "--resume",
        "--max-candidates",
        str(next_budget),
    ]
    if observation_store_path is not None:
        arguments.extend(("--observation-store", str(observation_store_path.absolute())))
    command, command_shell = shell_command(tuple(arguments))
    return _NextStep(action, command, command_shell)


def prepare_shrink(
    *,
    source: Path,
    resume: bool,
    max_candidates: int | None,
    registry: RunnerRegistry | None = None,
    safety_policy: SafetyPolicy | None = None,
    resource_limits: ResourceLimits | None = None,
    native_selection_factory: Callable[[CaseWorkspace, CaseEvidence], NativeSelection]
    | None = None,
) -> tuple[
    CaseWorkspace,
    CaseEvidence,
    PreparedExperiment,
    ResultSnapshot | None,
    ReductionCheckpoint | None,
    RunStateFile | None,
    int,
]:
    workspace, previous, run_state = _resolve_source(source, resume=resume)
    evidence = load_extracted_case(workspace)
    if previous is not None:
        _validate_result_case(previous, evidence)
    experiment = parse_experiment_toml(evidence.default_experiment)
    if (
        native_selection_factory is not None
        and registry is None
        and (
            isinstance(experiment, SampledExperimentDefinition)
            or isinstance(experiment.runner, UnavailableRunnerConfig)
        )
    ):
        selection = native_selection_factory(workspace, evidence)
        registry = selection.registry
        safety_policy = selection.safety_policy
        resource_limits = selection.resource_limits
        experiment = selection.experiment
    prepared = prepare_experiment(
        evidence,
        registry=registry,
        safety_policy=safety_policy,
        resource_limits=resource_limits,
        experiment=experiment,
    )
    checkpoint = (
        run_state.load().checkpoint
        if run_state is not None
        else previous.checkpoint
        if previous is not None
        else None
    )
    if checkpoint is not None:
        validate_checkpoint_identity(
            checkpoint,
            case_digest=evidence.digest,
            identity=prepared.identity,
        )
    if max_candidates is not None and max_candidates > MAX_CANDIDATE_RUNS:
        raise ResumeMismatchError(f"--max-candidates cannot exceed {MAX_CANDIDATE_RUNS}")
    if (
        checkpoint is not None
        and max_candidates is not None
        and max_candidates < checkpoint.candidate_runs_used
    ):
        raise ResumeMismatchError(
            "--max-candidates must be at least the candidate count in the checkpoint"
        )
    candidate_limit = max_candidates or DEFAULT_MAX_CANDIDATE_RUNS
    if checkpoint is not None and max_candidates is None:
        candidate_limit = min(
            MAX_CANDIDATE_RUNS,
            max(DEFAULT_MAX_CANDIDATE_RUNS, checkpoint.candidate_runs_used * 2),
        )
    return workspace, evidence, prepared, previous, checkpoint, run_state, candidate_limit


def validate_phase_observations(
    snapshot: ResultSnapshot,
    prepared: PreparedExperiment,
    store: ObservationStore,
) -> None:
    observations = {
        observation.observation_id: observation
        for observation in store.list_for_experiment(prepared.experiment.digest)
    }
    _validate_referenced_observations(
        snapshot.summary.calibration_observation_ids,
        observations=observations,
        prepared=prepared,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
        result=snapshot.summary.calibration_result,
        status=snapshot.summary.status,
    )
    _validate_referenced_observations(
        snapshot.summary.confirmation_observation_ids,
        observations=observations,
        prepared=prepared,
        role=ObservationRole.CONFIRMATION,
        arm=ObservationArm.SELECTED,
        result=snapshot.summary.confirmation_result,
        status=snapshot.summary.status,
    )


def _validate_referenced_observations(
    observation_ids: tuple[str, ...],
    *,
    observations: dict[str, Observation],
    prepared: PreparedExperiment,
    role: ObservationRole,
    arm: ObservationArm,
    result: str,
    status: ResultStatus,
) -> None:
    for observation_id in observation_ids:
        observation = observations.get(observation_id)
        if observation is None or observation.identity != prepared.identity:
            raise ResumeMismatchError(
                f"result observation {observation_id!r} is unavailable or incompatible"
            )
        if observation.role is not role or observation.arm is not arm:
            raise ResumeMismatchError(
                f"result observation {observation_id!r} has the wrong experimental role"
            )
        _validate_observation_claim(observation_id, observation, result=result, status=status)


def _validate_observation_claim(
    observation_id: str,
    observation: Observation,
    *,
    result: str,
    status: ResultStatus,
) -> None:
    if result in {"observed", "confirmed-preserving"}:
        supported = observation.target_observed is True
        message = "does not support its claim"
    elif result == "not-observed":
        supported = observation.target_observed is False
        message = "does not support its claim"
    elif result == "failed" and status == "failed":
        supported = observation.outcome is ObservationOutcome.RUNNER_ERROR
        message = "does not support its failure"
    elif result == "failed" and status == "confirmation-failed":
        supported = observation.target_observed is False
        message = "does not support its claim"
    else:
        return
    if not supported:
        raise ResumeMismatchError(f"result observation {observation_id!r} {message}")


def _resolve_source(
    source: Path, *, resume: bool
) -> tuple[CaseWorkspace, ResultSnapshot | None, RunStateFile | None]:
    if not resume:
        return CaseWorkspace.open(source), None, None
    state_file = RunStateFile.open(source)
    if state_file.path.is_file():
        state = state_file.load()
        if state.status == "completed":
            raise RunStateError(f"run already completed: {state.result_path}")
        return CaseWorkspace.open(Path(state.source_case)), None, state_file
    snapshot = load_result_snapshot(source)
    return CaseWorkspace.open(snapshot.summary.source_case), snapshot, None


def _validate_result_case(snapshot: ResultSnapshot, evidence: CaseEvidence) -> None:
    summary = snapshot.summary
    original_ids = tuple(node.id for node in evidence.context.nodes)
    if evidence.digest != summary.case_digest or original_ids != summary.original_context_ids:
        raise ResultSnapshotError("result snapshot does not match its source case")
    try:
        removed = evidence.context.dependency_closure(summary.removed_context_ids)
        expected_candidate = prepare_candidate(evidence, snapshot.execution_plan, removed)
    except ValueError as error:
        raise ResultSnapshotError("result removals are invalid for the source case") from error
    if tuple(sorted(removed)) != tuple(sorted(summary.removed_context_ids)):
        raise ResultSnapshotError("result removals are not dependency-closed")
    if expected_candidate.digest != summary.candidate_digest:
        raise ResultSnapshotError("result candidate does not match source-case materialization")
    if expected_candidate.materialization == "exact-request":
        if evidence.materialize(removed) != snapshot.reduced_request:
            raise ResultSnapshotError("result request does not match source-case materialization")
    elif snapshot.reduced_request is not None:
        raise ResultSnapshotError("source-backed result contains a materialized request")
    retained_lines = tuple(
        node.model_dump_json()
        for node in evidence.context.nodes
        if node.id in summary.retained_context_ids
    )
    if retained_lines != snapshot.retained_context_lines:
        raise ResultSnapshotError("retained context does not match the source case")
