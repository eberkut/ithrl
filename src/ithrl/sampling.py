"""Sequential collection of frozen fresh-original/candidate sampling blocks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ithrl.case import CaseEvidence
from ithrl.execution.budget import (
    SamplingAttemptCost,
    sampling_attempt_cost,
    sampling_budget_refusal,
)
from ithrl.experiment import SampledExperimentDefinition
from ithrl.measurement import RunnerExecutionError, observe_once
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationRole,
    ObservationStore,
    SamplingArm,
    SamplingBlockEvidence,
    SamplingBlockPlan,
)
from ithrl.run_state import (
    SampledRunState,
    SampledRunStateFile,
    SampledRunUsage,
    SamplingDispatchReservation,
)
from ithrl.runner import Candidate, PreparedAttempt, prepare_candidate

if TYPE_CHECKING:
    from ithrl.workflow import PreparedExperiment


@dataclass(frozen=True, slots=True)
class SamplingCollectionResult:
    """Collection evidence and resource accounting, without a statistical decision."""

    evidence: SamplingBlockEvidence = field(repr=False)
    usage: SampledRunUsage
    stop_reason: str | None

    @property
    def complete(self) -> bool:
        return self.stop_reason is None and self.evidence.complete and self.evidence.eligible


def _now() -> datetime:
    return datetime.now(UTC)


def sampling_pacing_delay(state: SampledRunState, store: ObservationStore, now: datetime) -> float:
    """Remaining pause since the last owned observation, including time spent offline."""

    delay = state.allowance.inter_attempt_delay_seconds
    if not delay:
        return 0.0
    reference = next(
        (
            block.observations[-1].observation_id
            for block in reversed(state.blocks)
            if block.observations
        ),
        state.calibration_observation_ids[-1] if state.calibration_observation_ids else None,
    )
    if reference is None:
        return 0.0
    observation = store.get(reference)
    if observation is None:
        raise ValueError("pacing requires the last owned observation")
    if now < observation.recorded_at:
        raise ValueError("sampling clock precedes recorded progress")
    return max(0.0, delay - (now - observation.recorded_at).total_seconds())


async def collect_sampling_block(
    *,
    evidence: CaseEvidence,
    original: PreparedExperiment,
    candidate: PreparedExperiment,
    removed_context_ids: tuple[str, ...],
    plan: SamplingBlockPlan,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    clock: Callable[[], datetime] = _now,
    verify_independence: Callable[[Observation], str | None] | None = None,
) -> SamplingCollectionResult:
    """Admit a complete bounded block, collect serially, or retain an auditable refusal.

    The trusted verifier must establish that a specific safe preaccept failure is independent
    of the potential behavioral outcome, then return its private evidence digest. Error labels
    and digests alone do not establish independence. Without a verifier, preaccept exclusions
    are refused. The frozen opt-in overload policy instead requires its validated safety proof
    and scopes inference to valid observations under independent, stationary attempt draws.
    Recorded attempts are reconciled before continuation; an unresolved dispatch is never
    replayed. Callers own selection and statistical classification in later workflow layers.

    Whole-block time admission covers remaining runner deadlines and cancellation grace.
    Local synchronous preparation/persistence has no hard completion bound; elapsed time is
    checked again before each dispatch. Finite token allowances require an estimator, which
    current shared execution contracts do not provide, and are explicitly refused here.
    """

    with state_file.acquire_lock(store):
        return await _collect_sampling_block_owned(
            evidence=evidence,
            original=original,
            candidate=candidate,
            removed_context_ids=removed_context_ids,
            plan=plan,
            state_file=state_file,
            store=store,
            clock=clock,
            verify_independence=verify_independence,
        )


async def _collect_sampling_block_owned(
    *,
    evidence: CaseEvidence,
    original: PreparedExperiment,
    candidate: PreparedExperiment,
    removed_context_ids: tuple[str, ...],
    plan: SamplingBlockPlan,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    clock: Callable[[], datetime] = _now,
    verify_independence: Callable[[Observation], str | None] | None = None,
) -> SamplingCollectionResult:
    """Collect while the caller owns run/store locks across the composed search."""

    state_file._require_lock(store)
    originals = prepare_candidate(evidence, original.execution_plan)
    selected = prepare_candidate(evidence, candidate.execution_plan, removed_context_ids)
    inputs = {"baseline": (original, originals), "candidate": (candidate, selected)}
    _validate_inputs(evidence, plan, inputs)
    cost = sampling_attempt_cost(original.runner.capability_profile, original.resource_limits)
    state = state_file.load(store)
    if (
        state.original_identity != plan.original_identity
        or state.original_candidate_digest != plan.original_candidate_digest
        or state.provider_calls_per_attempt != plan.provider_calls_per_attempt
    ):
        raise ValueError("sampling block does not match the original run identity")
    if state.pending is not None:
        pending_plan = state.blocks[-1].plan
        if pending_plan.digest != plan.digest:
            raise ValueError("continuation requires the exact frozen block")
        pending = state.pending
        prepared = inputs[pending.arm][0]
        digest = (
            plan.original_candidate_digest if pending.arm == "baseline" else plan.candidate_digest
        )
        observation = store.get_attempt(
            prepared.identity,
            candidate_digest=digest,
            role=ObservationRole(plan.role),
            arm=ObservationArm(pending.arm),
            attempt=pending.attempt,
        )
        if observation is None:
            return _result(state_file, store, plan, clock(), "unresolved-dispatch")
        state = state_file.reconcile(
            store, independence_evidence_digest=_independence(observation, verify_independence)
        )

    active = bool(state.blocks and not state.blocks[-1].archived)
    if active and state.blocks[-1].plan.digest != plan.digest:
        raise ValueError("continuation requires the exact frozen block")
    block = (
        state_file.evidence(store)[-1] if active else SamplingBlockEvidence(plan=plan, attempts=())
    )
    if not active and any(progress.plan.block_id == plan.block_id for progress in state.blocks):
        raise ValueError("archived sampling blocks cannot be collected again")
    if not active and any(progress.plan.role == "confirmation" for progress in state.blocks):
        return _result(state_file, store, plan, clock(), "confirmation-already-started", block)
    # An unsafe earlier block cannot be hidden by archiving it and drawing a new block.
    prior_blocks = state_file.evidence(store)
    if any(not earlier.eligible for earlier in prior_blocks):
        return _result(state_file, store, plan, clock(), "ineligible-prior-block", block)
    if (
        not active
        and plan.role == "search"
        and any(not earlier.complete for earlier in prior_blocks)
    ):
        return _result(state_file, store, plan, clock(), "unfinished-prior-block", block)

    while True:
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("sampling clock must include a UTC offset")
        usage = state_file.usage(store, now=now)
        reason = block.eligibility_refusal
        if reason is None and not plan.frozen_at <= now < plan.expires_at:
            reason = "freshness-expired"
        if reason is None and block.attempts and now < block.attempts[-1].observation.recorded_at:
            reason = "clock-precedes-progress"
        if reason is None and usage.wall_time_seconds > state.allowance.max_wall_time_seconds:
            reason = "wall-time-exhausted"
        if reason is None and state.allowance.max_tokens is not None:
            reason = "token-estimate-unavailable"
        if reason is not None:
            return SamplingCollectionResult(block, usage, reason)
        if block.complete:
            return SamplingCollectionResult(block, usage, None)

        position = block.next_schedule_position
        if position is None:
            return SamplingCollectionResult(block, usage, "attempt-limit")
        unfinished = {
            arm
            for arm in ("baseline", "candidate")
            if block.counts_for(arm).valid < plan.targets_for(arm).required_valid
        }
        remaining = sum(arm in unfinished for arm in plan.schedule[position:])
        reason = _budget_reason(state, plan, usage, remaining, cost, active)
        if reason is not None:
            return SamplingCollectionResult(block, usage, reason)
        arm = plan.schedule[position]
        prepared, partition = inputs[arm]
        previous = store.list_compatible(
            prepared.identity,
            candidate_digest=partition.digest,
            role=ObservationRole(plan.role),
            arm=ObservationArm(arm),
        )
        if any(
            not item.allows_fresh_attempt(plan.reduction.eligibility_policy) for item in previous
        ):
            return SamplingCollectionResult(block, usage, "unsafe-prior-attempt")
        delay = sampling_pacing_delay(state, store, clock())
        if delay:
            # Wait before publishing any reservation. Cancellation leaves no dispatch to replay.
            await asyncio.sleep(delay)
        attempt = _attempt(evidence, prepared, partition, plan, arm)
        # Preparation is outside the runner deadline; include its elapsed time before
        # publishing a dispatch. Synchronous publication itself has no hard time bound.
        reserved_at = clock()
        refreshed = state_file.usage(store, now=reserved_at)
        reason = _budget_reason(state, plan, refreshed, remaining, cost, active)
        if reason is not None:
            return SamplingCollectionResult(block, refreshed, reason)
        if not plan.frozen_at <= reserved_at < plan.expires_at:
            return SamplingCollectionResult(
                block, state_file.usage(store, now=reserved_at), "freshness-expired"
            )
        if not active:
            state = state_file.admit_block(store, plan)
            active = True
        state_file.reserve_dispatch(
            store,
            SamplingDispatchReservation(
                block_id=plan.block_id,
                block_digest=plan.digest,
                schedule_position=position,
                arm=arm,
                attempt=1 + max((item.attempt for item in previous), default=0),
                attempt_id=attempt.attempt_id,
                attempt_digest=attempt.digest,
                reserved_at=reserved_at,
                provider_calls=cost.provider_calls,
                runner_seconds=cost.runner_seconds,
            ),
        )
        interrupted = False
        try:
            observation = await observe_once(
                runner=prepared.runner,
                identity=prepared.identity,
                store=store,
                attempt=attempt,
                eligibility_policy=plan.reduction.eligibility_policy,
            )
        except RunnerExecutionError as error:
            if error.observation is None:
                return _result(state_file, store, plan, clock(), "unresolved-dispatch")
            observation = error.observation
        except asyncio.CancelledError:
            interrupted = True
            # observe_once records cancellation when possible. Exact reconciliation below
            # preserves its effects; missing evidence leaves the full dispatch charged.
            observation = store.get_attempt(
                prepared.identity,
                candidate_digest=partition.digest,
                role=ObservationRole(plan.role),
                arm=ObservationArm(arm),
                attempt=1 + max((item.attempt for item in previous), default=0),
            )
            if observation is None:
                return _result(state_file, store, plan, clock(), "unresolved-dispatch")
        state = state_file.reconcile(
            store, independence_evidence_digest=_independence(observation, verify_independence)
        )
        block = state_file.evidence(store)[-1]
        if interrupted:
            return _result(state_file, store, plan, clock(), "user-interrupt")


def _validate_inputs(
    evidence: CaseEvidence,
    plan: SamplingBlockPlan,
    inputs: dict[str, tuple[PreparedExperiment, Candidate]],
) -> None:
    for arm, (prepared, partition) in inputs.items():
        identity = plan.original_identity if arm == "baseline" else plan.candidate_identity
        digest = plan.original_candidate_digest if arm == "baseline" else plan.candidate_digest
        profile = prepared.runner.capability_profile
        if (
            not isinstance(prepared.experiment, SampledExperimentDefinition)
            or prepared.experiment.reduction != plan.reduction
            or prepared.identity != identity
            or partition.digest != digest
            or profile.deterministic
            or profile.provider_calls_per_attempt != plan.provider_calls_per_attempt
            or profile != prepared.execution_plan.capability
            or prepared.runner.fingerprint != identity.runner_fingerprint
            or identity.case_digest != evidence.digest
            or identity.experiment_digest != prepared.experiment.digest
            or identity.execution_plan_digest != prepared.execution_plan.digest
            or identity.target_digest != prepared.experiment.target.digest
        ):
            raise ValueError("sampling inputs do not match the frozen block")
        _attempt(
            evidence, prepared, partition, plan, "baseline" if arm == "baseline" else "candidate"
        )
    original, candidate = inputs["baseline"][0], inputs["candidate"][0]
    if original.resource_limits != candidate.resource_limits:
        raise ValueError("sampling arms require the same frozen resource limits")
    if original.runner.capability_profile != candidate.runner.capability_profile:
        raise ValueError("sampling arms require the same capability profile")


def _attempt(
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    partition: Candidate,
    plan: SamplingBlockPlan,
    arm: SamplingArm,
) -> PreparedAttempt:
    return PreparedAttempt(
        evidence=evidence,
        candidate=partition,
        target=prepared.experiment.target,
        execution_plan=prepared.execution_plan,
        safety_policy=prepared.safety_policy,
        resource_limits=prepared.resource_limits,
        phase=plan.role,
        arm=arm,
    )


def _budget_reason(
    state: SampledRunState,
    plan: SamplingBlockPlan,
    usage: SampledRunUsage,
    remaining: int,
    cost: SamplingAttemptCost,
    active: bool,
) -> str | None:
    elapsed = state.started_at + timedelta(seconds=usage.wall_time_seconds)
    future_seconds = remaining * (cost.runner_seconds + state.allowance.inter_attempt_delay_seconds)
    if elapsed + timedelta(seconds=future_seconds) >= plan.expires_at:
        return "freshness-budget"
    confirmation = plan.reduction.confirmation
    return sampling_budget_refusal(
        allowance=state.allowance,
        charged_provider_calls=usage.charged_provider_calls,
        elapsed_seconds=usage.wall_time_seconds,
        remaining_attempts=remaining,
        attempt_cost=cost,
        confirmation_attempts=(
            confirmation.original.max_attempts + confirmation.candidate.max_attempts
        ),
        search_blocks=sum(progress.plan.role == "search" for progress in state.blocks)
        + int(not active and plan.role == "search"),
        role=plan.role,
    )


def _independence(
    observation: Observation, verifier: Callable[[Observation], str | None] | None
) -> str | None:
    if observation.valid or not observation.attempt_evidence.retry_safe or verifier is None:
        return None
    digest = verifier(observation)
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("independence verifier returned an invalid evidence digest")
    return digest


def _result(
    state_file: SampledRunStateFile,
    store: ObservationStore,
    plan: SamplingBlockPlan,
    now: datetime,
    reason: str,
    block: SamplingBlockEvidence | None = None,
) -> SamplingCollectionResult:
    if block is None:
        block = state_file.evidence(store)[-1]
        if block.plan.digest != plan.digest:
            raise ValueError("sampling result does not match the frozen block")
    return SamplingCollectionResult(block, state_file.usage(store, now=now), reason)
