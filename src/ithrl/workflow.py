"""Run deterministic local experiments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ithrl.case import CaseEvidence
from ithrl.context import JsonObject, content_digest
from ithrl.execution import (
    CapabilityProfile,
    PreflightIssue,
    PreflightReport,
    ResolvedExecutionPlan,
    resolve_execution_plan,
)
from ithrl.experiment import ExperimentDefinition, FakeRunnerConfig, UnavailableRunnerConfig
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationRole,
    ObservationStore,
)
from ithrl.reduce import (
    ALGORITHM_DIGEST,
    DeterministicReducer,
    ReductionResult,
)
from ithrl.runner import FakeRunner, Runner


class WorkflowError(RuntimeError):
    """Base error for a prepared deterministic workflow."""


class PreflightRefusedError(WorkflowError):
    """The local capability preflight refused execution."""

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        reasons = "; ".join(f"{issue.code}: {issue.message}" for issue in report.issues)
        super().__init__(reasons)


class OraclePrerequisiteError(WorkflowError):
    """The experiment does not protect its oracle prerequisites."""


class ConflictingObservationError(WorkflowError):
    """Stored deterministic observations disagree for the same identity."""


@dataclass(frozen=True, slots=True)
class PreparedExperiment:
    """Validated local services and identities for one case experiment."""

    experiment: ExperimentDefinition
    runner: Runner
    execution_plan: ResolvedExecutionPlan
    identity: ObservationIdentity


@dataclass(frozen=True, slots=True)
class PhaseOutcome:
    """Terminal state from calibration, search, and confirmation."""

    calibration_result: Literal["not-run", "observed", "not-observed", "failed"]
    calibration_observation: Observation | None
    reduction: ReductionResult | None
    confirmation_result: Literal["not-run", "confirmed-preserving", "failed"]
    confirmation_observation: Observation | None
    stop_reason: Literal["completed", "user-interrupt", "operational-error"]
    operational_error: WorkflowError | RunnerExecutionError | None


@dataclass(slots=True)
class _PhaseState:
    calibration_result: Literal["not-run", "observed", "not-observed", "failed"] = "not-run"
    calibration_observation: Observation | None = None
    reduction: ReductionResult | None = None
    confirmation_result: Literal["not-run", "confirmed-preserving", "failed"] = "not-run"
    confirmation_observation: Observation | None = None
    phase: Literal["calibration", "search", "confirmation"] = "calibration"


def prepare_experiment(evidence: CaseEvidence) -> PreparedExperiment:
    """Parse, protect, and resolve the built-in credential-free experiment."""

    experiment = ExperimentDefinition.from_toml(evidence.default_experiment)
    decision = evidence.decision
    if decision is not None and experiment.target != decision.selected_target:
        raise PreflightRefusedError(
            _runner_evidence_mismatch("experiment target does not match the selected decision")
        )
    _validate_oracle_prerequisites(evidence, experiment)
    config = experiment.runner
    if isinstance(config, FakeRunnerConfig) and not evidence.has_request:
        raise PreflightRefusedError(
            _runner_evidence_mismatch("the fake runner requires an exact provider request")
        )
    if isinstance(config, UnavailableRunnerConfig):
        if evidence.manifest.harness != config.harness:
            raise PreflightRefusedError(
                _runner_evidence_mismatch("experiment runner does not match the captured harness")
            )
        report = PreflightReport(
            ready=False,
            plan=None,
            issues=(
                PreflightIssue(
                    code="runner-unavailable",
                    message="the experiment has no compatible implemented runner",
                ),
            ),
        )
        raise PreflightRefusedError(report)
    runner = FakeRunner(
        required_markers=frozenset(config.required_markers),
        alternative_marker_sets=tuple(
            frozenset(marker_set) for marker_set in config.alternative_marker_sets
        ),
        tool_name=config.tool_name,
        command=config.command,
        final_text=config.final_text,
    )
    report = resolve_execution_plan(
        CapabilityProfile.deterministic_fake(runner_version="2"),
        experiment,
        runner_fingerprint=runner.fingerprint,
    )
    if not report.ready or report.plan is None:
        raise PreflightRefusedError(report)
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint=runner.fingerprint,
        target_digest=experiment.target.digest,
        algorithm_digest=ALGORITHM_DIGEST,
    )
    return PreparedExperiment(
        experiment=experiment,
        runner=runner,
        execution_plan=report.plan,
        identity=identity,
    )


def _runner_evidence_mismatch(message: str) -> PreflightReport:
    return PreflightReport(
        ready=False,
        plan=None,
        issues=(PreflightIssue(code="runner-evidence-mismatch", message=message),),
    )


def run_phases(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    reducer: DeterministicReducer,
    calibrate_only: bool,
    progress: Callable[[str], None] | None = None,
) -> PhaseOutcome:
    """Run all requested phases in one event loop."""

    state = _PhaseState()
    stop_reason: Literal["completed", "user-interrupt", "operational-error"] = "completed"
    operational_error: WorkflowError | RunnerExecutionError | None = None
    try:
        asyncio.run(
            _run_phases(
                state=state,
                evidence=evidence,
                prepared=prepared,
                store=store,
                reducer=reducer,
                calibrate_only=calibrate_only,
                progress=progress,
            )
        )
    except KeyboardInterrupt:
        stop_reason = "user-interrupt"
    except (WorkflowError, RunnerExecutionError) as error:
        stop_reason = "operational-error"
        operational_error = error
        if isinstance(error, RunnerExecutionError) and state.phase == "calibration":
            state.calibration_result = "failed"
            state.calibration_observation = error.observation
        elif isinstance(error, RunnerExecutionError) and state.phase == "confirmation":
            state.confirmation_result = "failed"
            state.confirmation_observation = error.observation
    return PhaseOutcome(
        calibration_result=state.calibration_result,
        calibration_observation=state.calibration_observation,
        reduction=state.reduction,
        confirmation_result=state.confirmation_result,
        confirmation_observation=state.confirmation_observation,
        stop_reason=stop_reason,
        operational_error=operational_error,
    )


async def _run_phases(
    *,
    state: _PhaseState,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    reducer: DeterministicReducer,
    calibrate_only: bool,
    progress: Callable[[str], None] | None,
) -> None:
    state.phase = "calibration"
    if progress is not None:
        progress("Testing original context")
    state.calibration_observation = await calibrate(
        evidence=evidence, prepared=prepared, store=store
    )
    if state.calibration_observation.target_observed is not True:
        state.calibration_result = "not-observed"
        if progress is not None:
            progress("Original context did not produce the target; search skipped")
        return
    state.calibration_result = "observed"
    if progress is not None:
        progress("Original context produced the target")
    if calibrate_only:
        return
    state.phase = "search"
    if progress is not None:
        progress("Searching dependency-valid candidates")
    state.reduction = await reducer.reduce(evidence)
    if not state.reduction.final_sweep_complete:
        if progress is not None:
            progress("Candidate budget reached; search state saved")
        return
    state.phase = "confirmation"
    if progress is not None:
        progress("Confirming the reduced context with a new run")
    state.confirmation_observation = await confirm(
        request=state.reduction.reduced_request,
        prepared=prepared,
        store=store,
    )
    state.confirmation_result = (
        "confirmed-preserving"
        if state.confirmation_observation.target_observed is True
        else "failed"
    )
    if progress is not None:
        progress(
            "Confirmation preserved the target"
            if state.confirmation_result == "confirmed-preserving"
            else "Confirmation did not preserve the target"
        )


async def calibrate(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
) -> Observation:
    """Measure the original case, reusing only matching deterministic observations."""

    candidate_digest = content_digest(evidence.request)
    compatible = store.list_compatible(
        prepared.identity,
        candidate_digest=candidate_digest,
        role=ObservationRole.CALIBRATION,
        arm=ObservationArm.BASELINE,
    )
    verdicts = {item.target_observed for item in compatible if item.valid}
    verdicts.discard(None)
    if len(verdicts) > 1:
        raise ConflictingObservationError("stored deterministic calibration observations disagree")
    if verdicts:
        observed = verdicts.pop()
        observation = next(item for item in compatible if item.target_observed is observed)
    else:
        observation = await observe_once(
            runner=prepared.runner,
            target=prepared.experiment.target,
            identity=prepared.identity,
            store=store,
            request=evidence.request,
            role=ObservationRole.CALIBRATION,
            arm=ObservationArm.BASELINE,
        )
    return observation


async def confirm(
    *,
    request: JsonObject,
    prepared: PreparedExperiment,
    store: ObservationStore,
) -> Observation:
    """Take a new final measurement of the candidate selected by the search."""

    return await observe_once(
        runner=prepared.runner,
        target=prepared.experiment.target,
        identity=prepared.identity,
        store=store,
        request=request,
        role=ObservationRole.CONFIRMATION,
        arm=ObservationArm.SELECTED,
    )


def _validate_oracle_prerequisites(
    evidence: CaseEvidence,
    experiment: ExperimentDefinition,
) -> None:
    by_id = evidence.context.by_id
    missing = [item for item in experiment.oracle.protected_context_ids if item not in by_id]
    unprotected = [
        item
        for item in experiment.oracle.protected_context_ids
        if item in by_id and by_id[item].mutable and not by_id[item].protected
    ]
    if missing or unprotected:
        details: list[str] = []
        if missing:
            details.append("unknown: " + ", ".join(missing))
        if unprotected:
            details.append("reducible: " + ", ".join(unprotected))
        raise OraclePrerequisiteError(
            "oracle prerequisites must exist and be frozen or protected ("
            + "; ".join(details)
            + ")"
        )
