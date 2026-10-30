"""Compose calibrated deterministic and fixed-plan sampled experiments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ithrl.case import CaseEvidence, CaseFormatError
from ithrl.context import content_digest
from ithrl.execution import (
    PreflightIssue,
    PreflightReport,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
    resolve_execution_plan,
)
from ithrl.experiment import (
    AnyExperimentDefinition,
    ExperimentDefinition,
    SampledExperimentDefinition,
    parse_experiment_toml,
)
from ithrl.extraction import validate_experiment_target
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationRole,
    ObservationStore,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    sampling_schedule,
)
from ithrl.reduce import (
    ALGORITHM_DIGEST,
    DeterministicReducer,
    ReductionResult,
    SampledConfirmation,
    SampledReducer,
)
from ithrl.runner import (
    PreparedAttempt,
    Runner,
    RunnerRegistry,
    built_in_runner_registry,
    prepare_candidate,
)
from ithrl.runner.contracts import CandidateAdmission

if TYPE_CHECKING:
    from ithrl.results import SampledReductionEvidence
    from ithrl.run_state import SampledRunState, SampledRunStateFile, SampledRunUsage


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


SAMPLED_CALIBRATION_ALGORITHM_DIGEST = content_digest(
    {"name": "sampled-full-context-calibration", "version": "1"}
)


@dataclass(frozen=True, slots=True)
class PreparedExperiment:
    """Validated local services and identities for one case experiment."""

    experiment: AnyExperimentDefinition
    runner: Runner
    execution_plan: ResolvedExecutionPlan
    safety_policy: SafetyPolicy
    resource_limits: ResourceLimits
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


def prepare_experiment(
    evidence: CaseEvidence,
    *,
    registry: RunnerRegistry | None = None,
    safety_policy: SafetyPolicy | None = None,
    resource_limits: ResourceLimits | None = None,
    experiment: AnyExperimentDefinition | None = None,
) -> PreparedExperiment:
    """Protect and resolve a persisted or explicitly supplied typed experiment."""

    if experiment is None:
        experiment = parse_experiment_toml(evidence.default_experiment)
    decision = evidence.decision
    if decision is not None:
        try:
            validate_experiment_target(decision, experiment.target)
        except CaseFormatError as error:
            raise PreflightRefusedError(
                PreflightReport(
                    ready=False,
                    plan=None,
                    issues=(
                        PreflightIssue(
                            code="runner-evidence-mismatch",
                            message=str(error),
                        ),
                    ),
                )
            ) from None
    _validate_oracle_prerequisites(evidence, experiment)
    registry = registry or built_in_runner_registry()
    resolution = registry.resolve(experiment.runner, evidence)
    if resolution.issue is not None:
        raise PreflightRefusedError(
            PreflightReport(ready=False, plan=None, issues=(resolution.issue,))
        )
    runner = resolution.runner
    if runner is None:
        raise RuntimeError("runner registry returned no resolution outcome")
    safety_policy = safety_policy or SafetyPolicy.credential_free()
    resource_limits = resource_limits or ResourceLimits.credential_free()
    report = resolve_execution_plan(
        runner.capability_profile,
        experiment,
        evidence=evidence,
        runner_fingerprint=runner.fingerprint,
        safety_policy=safety_policy,
        resource_limits=resource_limits,
    )
    if not report.ready or report.plan is None:
        raise PreflightRefusedError(report)
    if isinstance(runner, CandidateAdmission):
        attempt = PreparedAttempt(
            evidence=evidence,
            candidate=prepare_candidate(evidence, report.plan),
            target=experiment.target,
            execution_plan=report.plan,
            safety_policy=safety_policy,
            resource_limits=resource_limits,
            phase="calibration",
            arm="baseline",
        )
        try:
            runner.validate_candidate(attempt)
        except Exception:
            # Admission is pure and optional; original refusals remain terminal.
            # Native reconstruction diagnostics may contain private evidence.
            raise PreflightRefusedError(
                PreflightReport(
                    ready=False,
                    plan=None,
                    issues=(
                        PreflightIssue(
                            code="unsupported-materialization",
                            message="the runner cannot reconstruct the unchanged original case",
                        ),
                    ),
                )
            ) from None
    identity = ObservationIdentity(
        case_digest=evidence.digest,
        experiment_digest=experiment.digest,
        runner_fingerprint=runner.fingerprint,
        execution_plan_digest=report.plan.digest,
        target_digest=experiment.target.digest,
        algorithm_digest=(
            SAMPLED_CALIBRATION_ALGORITHM_DIGEST
            if isinstance(experiment, SampledExperimentDefinition)
            else ALGORITHM_DIGEST
        ),
    )
    return PreparedExperiment(
        experiment=experiment,
        runner=runner,
        execution_plan=report.plan,
        safety_policy=safety_policy,
        resource_limits=resource_limits,
        identity=identity,
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

    if not isinstance(prepared.experiment, ExperimentDefinition):
        raise WorkflowError("sampled calibration cannot enter deterministic search phases")

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
        evidence=evidence,
        removed_context_ids=state.reduction.removed_context_ids,
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

    if not isinstance(prepared.experiment, ExperimentDefinition):
        raise WorkflowError("sampled calibration requires its dedicated calibration service")

    candidate = prepare_candidate(evidence, prepared.execution_plan)
    compatible = store.list_compatible(
        prepared.identity,
        candidate_digest=candidate.digest,
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
            identity=prepared.identity,
            store=store,
            attempt=PreparedAttempt(
                evidence=evidence,
                candidate=candidate,
                target=prepared.experiment.target,
                execution_plan=prepared.execution_plan,
                safety_policy=prepared.safety_policy,
                resource_limits=prepared.resource_limits,
                phase="calibration",
                arm="baseline",
            ),
        )
    return observation


async def confirm(
    *,
    evidence: CaseEvidence,
    removed_context_ids: tuple[str, ...] | frozenset[str],
    prepared: PreparedExperiment,
    store: ObservationStore,
) -> Observation:
    """Take a new final measurement of the candidate selected by the search."""

    if not isinstance(prepared.experiment, ExperimentDefinition):
        raise WorkflowError("sampled calibration cannot enter deterministic confirmation")

    candidate = prepare_candidate(evidence, prepared.execution_plan, removed_context_ids)
    return await observe_once(
        runner=prepared.runner,
        identity=prepared.identity,
        store=store,
        attempt=PreparedAttempt(
            evidence=evidence,
            candidate=candidate,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="confirmation",
            arm="selected",
        ),
    )


def _validate_oracle_prerequisites(
    evidence: CaseEvidence,
    experiment: AnyExperimentDefinition,
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


# Only resource/candidate-capacity stops permit a new holdout. Operational stops and
# interrupted search first require reconciliation/continuation of their existing block.
_SAFE_SEARCH_STOPS = frozenset(
    {
        None,
        "candidate-limit",
        "trial-limit",
        "provider-call-budget",
        "wall-time-budget",
        "freshness-budget",
        "attempt-limit",
    }
)


@dataclass(frozen=True, slots=True)
class SampledPhaseOutcome:
    """Separate exploratory search, fresh confirmation, and operational accounting."""

    result: SampledReductionEvidence
    usage: SampledRunUsage
    stop_reason: str | None


async def run_sampled_phases(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    state_file: SampledRunStateFile,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    verify_independence: Callable[[Observation], str | None] | None = None,
    progress: Callable[[str], None] | None = None,
    on_sampling_progress: Callable[[SamplingBlockEvidence], None] | None = None,
) -> SampledPhaseOutcome:
    """Search after successful stored calibration, then confirm one durably selected artifact.

    Run/store ownership spans search, selection publication and holdout collection. Resume
    uses the same frozen holdout and never re-enters search after selecting the artifact.
    This shared service does not authorize provider execution or expose a new CLI path.
    """

    from ithrl.run_state import SampledFinalSelection
    from ithrl.sampling import _collect_sampling_block_owned

    reducer = SampledReducer(
        prepared=prepared,
        state_file=state_file,
        observation_store=store,
        clock=clock,
        verify_independence=verify_independence,
        on_progress=on_sampling_progress,
    )
    with state_file.acquire_lock(store):
        state = state_file.load(store)
        if state.selection is None:
            if progress is not None:
                progress("Searching dependency-valid candidates")
            search = await reducer._reduce_owned(evidence)
            state = state_file.load(store)
            now = clock()
            blocks = state_file.evidence(store)
            refusal = _confirmation_search_refusal(state, blocks, search.stop_reason, now)
            if refusal is not None:
                return SampledPhaseOutcome(
                    _sampled_result(state, store, now),
                    state_file.usage(store, now=now),
                    refusal,
                )
            phase = search.checkpoint.reduction.confirmation
            holdout_id = content_digest({"run_id": state.run_id, "role": "confirmation"})
            seed = int(holdout_id[:16], 16)
            plan = SamplingBlockPlan(
                block_id=f"confirmation-{holdout_id}",
                role="confirmation",
                procedure=search.checkpoint.reduction,
                original_identity=state.original_identity,
                candidate_identity=search.checkpoint.best_candidate_identity,
                original_candidate_digest=state.original_candidate_digest,
                candidate_digest=search.checkpoint.best_candidate_digest,
                frozen_at=clock(),
                schedule_seed=seed,
                schedule=sampling_schedule(
                    phase.original.max_attempts, phase.candidate.max_attempts, seed
                ),
                provider_calls_per_attempt=state.provider_calls_per_attempt,
            )
            state = state_file.select_candidate(
                store,
                SampledFinalSelection.model_validate(
                    {
                        "checkpoint_digest": content_digest(
                            search.checkpoint.model_dump(mode="json")
                        ),
                        "selected_at": now,
                        "plan": plan,
                        "search_stop_reason": search.stop_reason,
                    }
                ),
            )
        else:
            await reducer._load_and_validate(evidence, allow_selected=True)
        selection = state.selection
        if selection is None or state.checkpoint is None:
            raise WorkflowError("sampled confirmation requires a selected search checkpoint")
        block = next(
            (b for b in state_file.evidence(store) if b.plan.role == "confirmation"),
            SamplingBlockEvidence(plan=selection.plan, attempts=()),
        )
        stop_reason = block.eligibility_refusal
        if not block.complete and block.eligible:
            if progress is not None:
                progress(
                    "Confirming selected context with fresh original and candidate observations"
                )
            collection = await _collect_sampling_block_owned(
                evidence=evidence,
                original=prepared,
                candidate=prepared,
                removed_context_ids=state.checkpoint.removed_context_ids,
                plan=selection.plan,
                state_file=state_file,
                store=store,
                clock=clock,
                verify_independence=verify_independence,
                on_progress=on_sampling_progress,
            )
            block, stop_reason = collection.evidence, collection.stop_reason
        result = _sampled_result(
            state_file.load(store),
            store,
            selection.selected_at,
            confirmation=SampledConfirmation.from_evidence(block),
        )
        if progress is not None:
            progress(f"Confirmation: {result.confirmation_result}")
        return SampledPhaseOutcome(result, state_file.usage(store, now=clock()), stop_reason)


def _confirmation_search_refusal(
    state: SampledRunState,
    blocks: tuple[SamplingBlockEvidence, ...],
    reason: str | None,
    now: datetime,
) -> str | None:
    if state.pending is not None:
        return "unresolved-dispatch"
    if any(not b.eligible for b in blocks):
        return "ineligible-prior-block"
    if reason not in _SAFE_SEARCH_STOPS:
        return reason or "unsafe-search-stop"
    if any(not b.complete and not b.plan.frozen_at <= now < b.plan.expires_at for b in blocks):
        return "freshness-expired"
    if (now - state.started_at).total_seconds() >= state.allowance.max_wall_time_seconds:
        return "wall-time-exhausted"
    return None


def _sampled_result(
    state: SampledRunState,
    store: ObservationStore,
    selected_at: datetime,
    *,
    confirmation: SampledConfirmation | None = None,
) -> SampledReductionEvidence:
    from ithrl.results import SampledReductionEvidence

    checkpoint = state.checkpoint
    if checkpoint is None:
        raise WorkflowError("sampled result requires a durable search checkpoint")
    calibration = store.get_many(state.calibration_observation_ids)
    return SampledReductionEvidence(
        checkpoint=checkpoint,
        search_result="search-reduced" if checkpoint.final_sweep_complete else "search-incomplete",
        selected_candidate_digest=checkpoint.best_candidate_digest,
        selected_at=selected_at,
        calibration_observation_ids=state.calibration_observation_ids,
        calibration_attempt_ids=tuple(o.attempt_evidence.attempt_id for o in calibration),
        confirmation=confirmation,
    )
