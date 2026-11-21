"""Direct two-context calibration and fresh confirmation over the shared collector."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from ithrl.case import CaseEvidence
from ithrl.comparison.contracts import (
    MAX_COMPARISON_RESULT_BYTES,
    BoundComparison,
    ComparisonConfiguration,
    ComparisonOutcome,
    ComparisonProgress,
    comparison_implementation_digest,
)
from ithrl.context import content_digest
from ithrl.execution import CapabilityProfile, ExecutionPlanMetadata
from ithrl.execution.budget import (
    SamplingAttemptCost,
    sampling_attempt_cost,
    sampling_budget_refusal,
    sampling_freshness_refusal,
)
from ithrl.experiment import SampledComparisonPlan, SampledExperimentDefinition
from ithrl.mutation import ContextSelection
from ithrl.observations import (
    Observation,
    ObservationIdentity,
    ObservationStore,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
    sampling_schedule,
)
from ithrl.run_state import (
    RunStateError,
    SampledFinalSelection,
    SampledRunState,
    SampledRunStateFile,
)
from ithrl.runner import (
    Candidate,
    PreparedAttempt,
    ProviderAcceptanceState,
    Runner,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    prepare_candidate,
)
from ithrl.runner.contracts import CandidateAdmission
from ithrl.sampling import _collect_sampling_block_owned
from ithrl.statistics import (
    BinomialSample,
    confirm_exact_change,
    require_exact_numerics,
    validate_exact_change_parameters,
)
from ithrl.workflow import PreparedExperiment

if TYPE_CHECKING:
    from ithrl.comparison.results import ComparisonEvidence


def _now() -> datetime:
    return datetime.now(UTC)


def _require_comparison_numerics() -> None:
    try:
        require_exact_numerics()
    except ImportError:
        raise ImportError("sampled comparison requires the 'ithrl[compare]' extra") from None


class ComparisonCapacityError(ValueError):
    """The frozen comparison cannot fit its bounded offline evidence format."""


def comparison_evidence_refusal(
    binding: BoundComparison, metadata: ExecutionPlanMetadata
) -> str | None:
    """Reserve space for generated observation metadata before dispatching a pair."""

    # Indented state/result snapshots repeat plans and partitions. Reserve eight
    # binding copies, 1 MiB of fixed overhead, and 64 KiB per generated attempt
    # for observation identities, accounting, grading and state references.
    # Native response bodies are external artifacts and do not enter this format.
    attempts = sum(
        arm.max_attempts
        for arm in (
            binding.procedure.calibration.original,
            binding.procedure.calibration.candidate,
            binding.procedure.confirmation.original,
            binding.procedure.confirmation.candidate,
        )
    )
    bound = (
        8 * len(binding.model_dump_json(indent=2).encode("utf-8"))
        + len(metadata.model_dump_json(indent=2).encode("utf-8"))
        + 1024 * 1024
        + attempts * 64 * 1024
    )
    return "comparison-evidence-capacity" if bound > MAX_COMPARISON_RESULT_BYTES else None


@dataclass(frozen=True, slots=True)
class _ComparisonRunner:
    """Require the same reported model before a comparison receives sample credit."""

    runner: Runner

    @property
    def fingerprint(self) -> str:
        return self.runner.fingerprint

    @property
    def capability_profile(self) -> CapabilityProfile:
        return self.runner.capability_profile

    async def run(
        self, attempt: PreparedAttempt, *, runtime_deadline: float | None = None
    ) -> RunResult:
        result = await self.runner.run(attempt, runtime_deadline=runtime_deadline)
        requested = attempt.execution_plan.model_requested
        if (
            requested is not None
            and result.model_returned is not None
            and result.model_returned != requested
        ):
            raise RunnerError(
                RunnerErrorCode.CONTRACT,
                "comparison reported model differs from its requested model",
                reason_code="model-mismatch",
                provider_acceptance_state=(
                    ProviderAcceptanceState.ACCEPTED
                    if result.accounting.provider_calls_started
                    else ProviderAcceptanceState.NOT_STARTED
                ),
                model_requested=requested,
                model_returned=result.model_returned,
                provider=result.provider,
                endpoint=result.endpoint,
                service_tier=result.service_tier,
                usage=result.usage,
                accounting=result.accounting,
                native_output=result.native_output,
                effective_policy_digest=result.effective_policy_digest,
            )
        return result


@dataclass(frozen=True, slots=True)
class PreparedComparison:
    """Pure admission for one immutable source, execution plan and selected pair."""

    evidence_digest: str
    prepared: PreparedExperiment = field(repr=False)
    configuration: ComparisonConfiguration = field(repr=False)
    binding: BoundComparison = field(repr=False)


@dataclass(frozen=True, slots=True)
class ComparisonBudgetBounds:
    planned_attempts: int
    planned_provider_calls: int
    planned_runner_seconds: int
    planned_wall_time_seconds: int
    calibration_attempts: int
    confirmation_attempts: int
    confirmation_provider_calls: int
    confirmation_runner_seconds: int


def comparison_budget_bounds(
    config: ComparisonConfiguration,
    cost: SamplingAttemptCost,
    *,
    inter_attempt_delay_seconds: int = 0,
) -> ComparisonBudgetBounds:
    """Reserve every frozen attempt, including eligible replacement slots."""

    if (
        type(inter_attempt_delay_seconds) is not int
        or not 0 <= inter_attempt_delay_seconds <= 86400
    ):
        raise ValueError("comparison pacing must be a bounded nonnegative integer")
    calibration = (
        config.calibration.reference.max_attempts + config.calibration.candidate.max_attempts
    )

    confirmation = (
        config.confirmation.reference.max_attempts + config.confirmation.candidate.max_attempts
    )
    attempts = calibration + confirmation
    return ComparisonBudgetBounds(
        planned_attempts=attempts,
        planned_provider_calls=attempts * cost.provider_calls,
        planned_runner_seconds=attempts * cost.runner_seconds,
        planned_wall_time_seconds=attempts * (cost.runner_seconds + inter_attempt_delay_seconds),
        calibration_attempts=calibration,
        confirmation_attempts=confirmation,
        confirmation_provider_calls=confirmation * cost.provider_calls,
        confirmation_runner_seconds=confirmation * cost.runner_seconds,
    )


def comparison_budget_refusal(
    config: ComparisonConfiguration, cost: SamplingAttemptCost
) -> str | None:
    """Precheck both full frozen blocks before collecting any calibration evidence."""

    bounds = comparison_budget_bounds(
        config, cost, inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds
    )
    if bounds.confirmation_provider_calls > config.limits.max_provider_calls:
        return "provider-call-budget"
    if bounds.confirmation_runner_seconds > config.limits.max_wall_time_seconds:
        return "wall-time-budget"
    if sampling_freshness_refusal(
        remaining_attempts=max(bounds.calibration_attempts, bounds.confirmation_attempts),
        attempt_cost=cost,
        remaining_seconds=config.freshness_seconds,
        inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds,
    ):
        return "sampling-freshness-too-small"
    allowance = SamplingBudgetAllowance(
        run_id="comparison-preflight",
        max_provider_calls=config.limits.max_provider_calls,
        max_wall_time_seconds=config.limits.max_wall_time_seconds,
        max_candidate_comparisons=1,
        confirmation_provider_calls=bounds.confirmation_provider_calls,
        confirmation_runner_seconds=bounds.confirmation_runner_seconds,
        inter_attempt_delay_seconds=config.limits.inter_attempt_delay_seconds,
    )
    return sampling_budget_refusal(
        allowance=allowance,
        charged_provider_calls=0,
        elapsed_seconds=0,
        remaining_attempts=bounds.calibration_attempts,
        attempt_cost=cost,
        confirmation_attempts=bounds.confirmation_attempts,
        search_blocks=1,
        role="calibration",
    )


def prepare_comparison(
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    configuration: ComparisonConfiguration,
) -> PreparedComparison:
    """Bind and pure-admit both exact contexts without allocating observations."""

    _require_comparison_numerics()
    validate_exact_change_parameters(
        alpha=configuration.confirmation.alpha,
        minimum_effect=configuration.minimum_effect,
    )
    if not isinstance(prepared.experiment, SampledExperimentDefinition):
        raise ValueError("comparison requires a sampled experiment")
    if (
        prepared.resource_limits.max_wall_time_seconds
        != configuration.limits.attempt_timeout_seconds
    ):
        raise ValueError("comparison timeout differs from the prepared execution limits")
    if (
        prepared.runner.fingerprint != prepared.execution_plan.runner_fingerprint
        or prepared.runner.capability_profile != prepared.execution_plan.capability
    ):
        raise ValueError("comparison runner differs from its prepared execution plan")
    if configuration.codex_replay != prepared.execution_plan.codex_replay:
        raise ValueError("comparison replay differs from its prepared execution plan")
    partitions: list[Candidate] = []
    selections: tuple[tuple[ContextSelection, Literal["baseline", "candidate"]], ...] = (
        (configuration.reference, "baseline"),
        (configuration.candidate, "candidate"),
    )
    for selection, arm in selections:
        candidate = prepare_candidate(
            evidence,
            prepared.execution_plan,
            selection.removed_context_ids,
            mutation=selection.mutation,
        )
        if evidence.has_request:
            # Coarse graph flags alone do not define the fixed triggering input.
            evidence._protect_section_fixed_input(frozenset(candidate.removed_context_ids))
        attempt = PreparedAttempt(
            evidence=evidence,
            candidate=candidate,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase="calibration",
            arm=arm,
        )
        # prepare_experiment already pure-admitted the immutable full source.
        if isinstance(prepared.runner, CandidateAdmission) and (
            candidate.removed_context_ids or candidate.mutation.edits
        ):
            prepared.runner.validate_candidate(attempt)
        partitions.append(candidate)
    binding = BoundComparison(
        procedure=configuration.procedure, reference=partitions[0], candidate=partitions[1]
    )
    if comparison_evidence_refusal(binding, prepared.execution_plan.inspection_metadata):
        raise ComparisonCapacityError("comparison evidence exceeds its supported storage capacity")
    return PreparedComparison(
        evidence_digest=evidence.digest,
        prepared=replace(
            prepared,
            identity=prepared.identity.model_copy(
                update={"algorithm_digest": configuration.policy_digest}
            ),
        ),
        configuration=configuration,
        binding=binding,
    )


def _block_key(run_id: str, binding: BoundComparison, role: str) -> str:
    if role == "confirmation":
        return content_digest({"run_id": run_id, "role": "confirmation"})
    return content_digest(
        {
            "run_id": run_id,
            "comparison": binding.digest,
            "role": role,
            "seed": binding.procedure.seed,
        }
    )


def comparison_block_plan(
    *,
    run_id: str,
    binding: BoundComparison,
    identity: ObservationIdentity,
    role: Literal["calibration", "confirmation"],
    frozen_at: datetime,
    provider_calls_per_attempt: int,
    selected_at: datetime | None = None,
) -> SamplingBlockPlan:
    """Derive one deterministic block identity from the frozen comparison purpose."""

    key = _block_key(run_id, binding, role)
    phase = (
        binding.procedure.confirmation if role == "confirmation" else binding.procedure.calibration
    )
    seed = int(key[:16], 16)
    return SamplingBlockPlan(
        block_id=f"confirmation-{key}"
        if role == "confirmation"
        else f"comparison-calibration-{key}",
        role=role,
        procedure=binding.procedure,
        original_identity=identity,
        candidate_identity=identity,
        original_candidate_digest=binding.reference.digest,
        candidate_digest=binding.candidate.digest,
        frozen_at=frozen_at,
        selected_at=selected_at,
        schedule_seed=seed,
        schedule=sampling_schedule(phase.original.max_attempts, phase.candidate.max_attempts, seed),
        provider_calls_per_attempt=provider_calls_per_attempt,
    )


def calibration_passes(block: SamplingBlockEvidence) -> bool | None:
    """A complete two-arm descriptive gate; no calibration evidence confirms an effect."""

    procedure = block.plan.procedure
    if not isinstance(procedure, SampledComparisonPlan) or block.plan.role != "calibration":
        raise ValueError("comparison calibration requires its exact calibration block")
    if not block.complete or not block.eligible:
        return None
    positive, negative = (
        (block.candidate_counts, block.original_counts)
        if procedure.objective == "repair"
        else (block.original_counts, block.candidate_counts)
    )
    return (
        positive.matches >= procedure.positive_minimum_matches
        and negative.matches <= procedure.negative_maximum_matches
    )


def _incomplete_reason(block: SamplingBlockEvidence) -> str | None:
    """Identify a terminal refusal that the frozen observation block proves."""

    if block.eligibility_refusal is not None:
        return block.eligibility_refusal
    if not block.complete and block.next_schedule_position is None:
        return "attempt-limit"
    return None


def _timing_refusal(
    state: SampledRunState,
    blocks: tuple[SamplingBlockEvidence, ...],
    stopped_at: datetime,
) -> str | None:
    """Prove expiry from the frozen plan or allowance without collecting again."""

    plan = (
        state.selection.plan
        if state.selection is not None
        else (blocks[-1].plan if blocks else None)
    )
    if plan is not None:
        if blocks and blocks[-1].plan == plan and blocks[-1].complete and blocks[-1].eligible:
            return None
        if stopped_at >= plan.expires_at:
            return "freshness-expired"
    if (stopped_at - state.started_at).total_seconds() > state.allowance.max_wall_time_seconds:
        return "wall-time-exhausted"
    return None


def confirm_comparison(
    block: SamplingBlockEvidence,
) -> tuple[ComparisonOutcome, float | None, float | None]:
    """Compute only the declared direction from complete, fresh eligible fixed samples."""

    procedure = block.plan.procedure
    if not isinstance(procedure, SampledComparisonPlan) or block.plan.role != "confirmation":
        raise ValueError("comparison confirmation requires its selected confirmation block")
    if not block.complete or not block.eligible:
        return ComparisonOutcome.INCOMPLETE, None, None
    _require_comparison_numerics()
    reference, candidate = block.original_counts, block.candidate_counts
    phase = procedure.confirmation
    if procedure.objective == "repair":
        positive, negative = candidate, reference
        positive_target, negative_target = phase.candidate, phase.original
        confirmed = ComparisonOutcome.CONFIRMED_IMPROVEMENT
    else:
        positive, negative = reference, candidate
        positive_target, negative_target = phase.original, phase.candidate
        confirmed = ComparisonOutcome.CONFIRMED_DEGRADATION
    result = confirm_exact_change(
        BinomialSample(positive.matches, positive.valid),
        BinomialSample(negative.matches, negative.valid),
        required_positive=positive_target.required_valid,
        required_negative=negative_target.required_valid,
        alpha=phase.alpha,
        minimum_effect=procedure.minimum_effect,
    )
    outcome = confirmed if result.confirmed else ComparisonOutcome.INCONCLUSIVE
    return outcome, result.difference, result.lower_bound


def validate_comparison_progress(
    state: SampledRunState,
    calibration: tuple[Observation, ...],
    blocks: tuple[SamplingBlockEvidence, ...],
    *,
    require_current_implementation: bool = True,
) -> None:
    """Recompute comparison decisions from immutable observations before any dispatch."""

    progress = state.comparison_progress
    if progress is None:
        raise RunStateError("comparison progress is missing")
    binding, identity = progress.binding, state.original_identity
    if (
        calibration
        or state.calibration_observation_ids
        or state.checkpoint is not None
        or state.fuzz_progress is not None
        or identity.algorithm_digest != binding.procedure.comparison_digest
        or identity.case_digest != binding.reference.case_digest
        or identity.execution_plan_digest != binding.reference.execution_plan_digest
        or state.original_candidate_digest != binding.reference.digest
        or (
            require_current_implementation
            and progress.implementation_digest != comparison_implementation_digest()
        )
    ):
        raise RunStateError("comparison state differs from its frozen source and purpose")
    roles = tuple(block.plan.role for block in blocks)
    if roles not in {(), ("calibration",), ("calibration", "confirmation")}:
        raise RunStateError("comparison permits one calibration and one confirmation block")
    for block in blocks:
        plan = block.plan
        expected = comparison_block_plan(
            run_id=state.run_id,
            binding=binding,
            identity=identity,
            role="confirmation" if plan.role == "confirmation" else "calibration",
            frozen_at=plan.frozen_at,
            selected_at=progress.selected_at if plan.role == "confirmation" else None,
            provider_calls_per_attempt=state.provider_calls_per_attempt,
        )
        if plan != expected:
            raise RunStateError("comparison block differs from its frozen contexts and policy")
    gate = calibration_passes(blocks[0]) if blocks else None
    if progress.selected_at is None:
        if state.selection is not None or "confirmation" in roles:
            raise RunStateError("comparison confirmation requires durable pair selection")
    else:
        if gate is not True or state.selection is None:
            raise RunStateError("comparison selection requires complete successful calibration")
        selection = state.selection
        if (
            selection.selected_at != progress.selected_at
            or selection.checkpoint_digest != progress.selection_digest
            or selection.plan.selected_at != progress.selected_at
        ):
            raise RunStateError("comparison selection identity or time changed")
        if any(
            progress.selected_at < attempt.observation.recorded_at for attempt in blocks[0].attempts
        ):
            raise RunStateError("comparison selection must follow all calibration observations")
        expected = comparison_block_plan(
            run_id=state.run_id,
            binding=binding,
            identity=identity,
            role="confirmation",
            frozen_at=selection.plan.frozen_at,
            selected_at=progress.selected_at,
            provider_calls_per_attempt=state.provider_calls_per_attempt,
        )
        if selection.plan != expected:
            raise RunStateError("comparison holdout differs from its exact selected pair")
    if progress.terminal is None:
        if progress.stop_reason is not None:
            raise RunStateError("nonterminal comparison progress cannot carry a terminal refusal")
        return
    if state.pending is not None:
        raise RunStateError("a terminal comparison cannot have an unresolved dispatch")
    if progress.terminal is ComparisonOutcome.CALIBRATION_FAILED:
        if (
            gate is not False
            or progress.selected_at is not None
            or progress.stop_reason != "calibration-failed"
        ):
            raise RunStateError("comparison calibration outcome differs from observations")
    elif progress.terminal is ComparisonOutcome.INCOMPLETE:
        reason = _incomplete_reason(blocks[-1]) if blocks else None
        if progress.stopped_at is not None:
            if progress.stopped_at < max(
                (
                    state.started_at,
                    progress.selected_at or state.started_at,
                    *(
                        attempt.observation.recorded_at
                        for block in blocks
                        for attempt in block.attempts
                    ),
                )
            ):
                raise RunStateError("comparison stop precedes its observations")
            reason = reason or _timing_refusal(state, blocks, progress.stopped_at)
        if reason is None or progress.stop_reason != reason:
            raise RunStateError("terminal incompleteness requires its observed block refusal")
    else:
        if roles != ("calibration", "confirmation") or state.selection is None:
            raise RunStateError("comparison effect requires its fresh selected confirmation")
        if (
            confirm_comparison(blocks[-1])[0] is not progress.terminal
            or progress.stop_reason != "confirmation-finished"
        ):
            raise RunStateError("comparison terminal outcome differs from its confirmation")


async def run_comparison(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    configuration: ComparisonConfiguration,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    admission: PreparedComparison | None = None,
    calibration_only: bool = False,
    clock: Callable[[], datetime] = _now,
    on_progress: Callable[[SamplingBlockEvidence], None] | None = None,
) -> ComparisonEvidence:
    """Finish or continue one selected pair; never replay completed confirmation."""

    if admission is None:
        admission = prepare_comparison(evidence, prepared, configuration)
    elif (
        admission.evidence_digest != evidence.digest
        or admission.prepared is not prepared
        or admission.configuration != configuration
        or prepared.runner.fingerprint != prepared.execution_plan.runner_fingerprint
        or prepared.runner.capability_profile != prepared.execution_plan.capability
    ):
        raise ValueError("comparison admission differs from its source, runtime or configuration")
    prepared, binding = admission.prepared, admission.binding
    cost = sampling_attempt_cost(prepared.runner.capability_profile, prepared.resource_limits)
    bounds = comparison_budget_bounds(configuration, cost)
    collection_prepared = replace(prepared, runner=_ComparisonRunner(prepared.runner))
    with state_file.acquire_lock(store):
        while True:
            state, _, blocks = state_file._read_validated(store)
            if (
                state.original_identity != prepared.identity
                or state.original_candidate_digest != binding.reference.digest
                or state.allowance.confirmation_provider_calls != bounds.confirmation_provider_calls
                or state.allowance.confirmation_runner_seconds != bounds.confirmation_runner_seconds
                or state.allowance.inter_attempt_delay_seconds
                != configuration.limits.inter_attempt_delay_seconds
            ):
                raise RunStateError("comparison runtime identity or confirmation reserve changed")
            progress = state.comparison_progress
            if progress is None:
                raise RunStateError("sampled run was not initialized for a comparison")
            if progress.binding != binding:
                raise RunStateError("comparison contexts or sampling policy changed")
            if calibration_only and progress.selected_at is not None:
                raise RunStateError(
                    "calibration-only invocation cannot continue selected confirmation"
                )
            if progress.terminal is not None:
                return _result(state_file, store, prepared, clock())
            if not blocks:
                refusal = comparison_budget_refusal(configuration, cost)
                if refusal is not None:
                    return _result(state_file, store, prepared, clock(), refusal)
            if (
                progress.selected_at is not None
                and blocks
                and blocks[-1].plan.role == "confirmation"
                and blocks[-1].complete
                and blocks[-1].eligible
            ):
                _finish(
                    state_file,
                    store,
                    progress,
                    confirm_comparison(blocks[-1])[0],
                    "confirmation-finished",
                )
                continue
            if progress.selected_at is None:
                gate = calibration_passes(blocks[0]) if blocks else None
                if gate is False:
                    _finish(
                        state_file,
                        store,
                        progress,
                        ComparisonOutcome.CALIBRATION_FAILED,
                        "calibration-failed",
                    )
                    continue
                if gate is True:
                    if calibration_only:
                        return _result(state_file, store, prepared, clock(), "calibration-only")
                    if not state.blocks[-1].archived:
                        state_file.archive_block(store)
                    selected_at = max(
                        clock(), *(a.observation.recorded_at for a in blocks[0].attempts)
                    )
                    selected = progress.model_copy(update={"selected_at": selected_at})
                    plan = comparison_block_plan(
                        run_id=state.run_id,
                        binding=binding,
                        identity=prepared.identity,
                        role="confirmation",
                        frozen_at=max(clock(), selected_at + timedelta(microseconds=1)),
                        selected_at=selected_at,
                        provider_calls_per_attempt=state.provider_calls_per_attempt,
                    )
                    state_file.select_comparison_candidate(
                        store,
                        selected,
                        SampledFinalSelection(
                            checkpoint_digest=selected.selection_digest,
                            selected_at=selected_at,
                            plan=plan,
                        ),
                    )
                    continue
                plan = (
                    blocks[0].plan
                    if blocks
                    else comparison_block_plan(
                        run_id=state.run_id,
                        binding=binding,
                        identity=prepared.identity,
                        role="calibration",
                        frozen_at=clock(),
                        provider_calls_per_attempt=state.provider_calls_per_attempt,
                    )
                )
            else:
                if state.selection is None:
                    raise RunStateError("comparison selection is unavailable")
                plan = state.selection.plan
            collected = await _collect_sampling_block_owned(
                evidence=evidence,
                original=collection_prepared,
                candidate=collection_prepared,
                removed_context_ids=(),
                plan=plan,
                state_file=state_file,
                store=store,
                clock=clock,
                on_progress=on_progress,
            )
            if collected.evidence.complete and collected.evidence.eligible:
                if plan.role == "confirmation":
                    outcome = confirm_comparison(collected.evidence)[0]
                    _finish(state_file, store, progress, outcome, "confirmation-finished")
                if collected.stop_reason == "user-interrupt":
                    raise asyncio.CancelledError
                continue
            current, _, current_blocks = state_file._read_validated(store)
            stopped_at = clock()
            reason = None
            if current.pending is None:
                reason = _incomplete_reason(collected.evidence) or _timing_refusal(
                    current, current_blocks, stopped_at
                )
            if reason is not None:
                _finish(
                    state_file,
                    store,
                    progress,
                    ComparisonOutcome.INCOMPLETE,
                    reason,
                    stopped_at=stopped_at
                    if reason in {"freshness-expired", "wall-time-exhausted"}
                    else None,
                )
            if collected.stop_reason == "user-interrupt":
                raise asyncio.CancelledError
            return _result(state_file, store, prepared, clock(), reason or collected.stop_reason)


def _finish(
    state_file: SampledRunStateFile,
    store: ObservationStore,
    progress: ComparisonProgress,
    outcome: ComparisonOutcome,
    reason: str,
    *,
    stopped_at: datetime | None = None,
) -> None:
    state_file.save_comparison_progress(
        store,
        progress.model_copy(
            update={"terminal": outcome, "stop_reason": reason, "stopped_at": stopped_at}
        ),
    )


def _result(
    state_file: SampledRunStateFile,
    store: ObservationStore,
    prepared: PreparedExperiment,
    now: datetime,
    refusal: str | None = None,
) -> ComparisonEvidence:
    from ithrl.comparison.results import ComparisonEvidence

    snapshot = state_file.snapshot(store, now=now)
    return ComparisonEvidence(
        state=snapshot.state,
        blocks=snapshot.blocks,
        execution_metadata=prepared.execution_plan.inspection_metadata,
        recorded_at=now,
        refusal=refusal,
    )
