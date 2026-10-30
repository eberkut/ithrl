"""Durable bounded fuzz orchestration over the existing observation and dispatch store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal

from ithrl.case import CaseEvidence
from ithrl.context import content_digest
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.fuzz.contracts import FuzzCampaignConfig, FuzzOutcome
from ithrl.fuzz.generation import generate_mutations
from ithrl.fuzz.progress import (
    FuzzCampaignProgress,
    FuzzTrial,
    campaign_implementation_digest,
    campaign_steps,
)
from ithrl.fuzz.sampling import (
    confirm_fuzz_evidence,
    fuzz_block_plan,
    fuzz_budget_bounds,
    fuzz_procedure,
    prepare_fuzz_sampling,
    screen_fuzz_block,
    screen_fuzz_evidence,
)
from ithrl.fuzz.simplify import MutationDecision
from ithrl.fuzz.statistics import ChangeConfirmation
from ithrl.mutation import TextMutation
from ithrl.observations import (
    Observation,
    ObservationOutcome,
    ObservationStore,
    SamplingBlockEvidence,
)
from ithrl.run_state import (
    RunStateError,
    SampledFinalSelection,
    SampledRunState,
    SampledRunStateFile,
    SampledRunUsage,
)
from ithrl.runner import prepare_candidate
from ithrl.runner.contracts import CandidateInvalidError
from ithrl.sampling import _collect_sampling_block_owned
from ithrl.workflow import PreparedExperiment


@dataclass(frozen=True, slots=True)
class FuzzCampaignResult:
    outcome: FuzzOutcome
    selected_mutation: TextMutation | None = field(repr=False)
    confirmation: ChangeConfirmation | None
    usage: SampledRunUsage
    stop_reason: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _generation_input(evidence: CaseEvidence, stale_text: str) -> str:
    return content_digest(
        {
            "sites": [
                [s.context_id, s.content_index, content_digest(s.original_text)]
                for s in evidence.editable_text()
            ],
            "stale_text": stale_text,
        }
    )


def initialize_fuzz_campaign(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    campaign: FuzzCampaignConfig,
    stale_text: str,
    state_file: SampledRunStateFile,
    store: ObservationStore,
) -> None:
    """Freeze generation after separately completed calibration, before any trial dispatch.

    The composing workflow owns calibration and execution acknowledgement. This internal
    service accepts only its exact, measurable, charged calibration evidence.
    """

    with state_file.acquire_lock(store):
        _initialize_fuzz_campaign_owned(
            evidence=evidence,
            prepared=prepared,
            campaign=campaign,
            stale_text=stale_text,
            state_file=state_file,
            store=store,
        )


def _initialize_fuzz_campaign_owned(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    campaign: FuzzCampaignConfig,
    stale_text: str,
    state_file: SampledRunStateFile,
    store: ObservationStore,
) -> None:
    """Caller owns the shared run/store locks throughout composition."""

    bound = prepare_fuzz_sampling(evidence, prepared, campaign)
    state = state_file.load(store)
    if state.blocks or state.selection or state.checkpoint or state.fuzz_progress:
        raise RunStateError("campaign initialization requires unused sampled progress")
    if state.original_identity != bound.identity:
        raise RunStateError("campaign requires its prepared observation identity")
    bounds = fuzz_budget_bounds(
        campaign, sampling_attempt_cost(bound.runner.capability_profile, bound.resource_limits)
    )
    if (
        state.allowance.confirmation_provider_calls != bounds.confirmation_provider_calls
        or state.allowance.confirmation_runner_seconds != bounds.confirmation_runner_seconds
    ):
        raise RunStateError("campaign requires its complete frozen confirmation reserve")
    batch = generate_mutations(evidence.editable_text(), campaign, stale_text=stale_text)
    progress = FuzzCampaignProgress(
        config=campaign,
        implementation_digest=campaign_implementation_digest(),
        generation_input_digest=_generation_input(evidence, stale_text),
        stale_text=stale_text,
        generated=batch.mutations,
        proposals_examined=batch.proposals_examined,
    )
    state_file.save_fuzz_progress(store, progress)


def _replay(
    state: SampledRunState, blocks: tuple[SamplingBlockEvidence, ...]
) -> tuple[Literal["discovery", "simplification"], TextMutation] | TextMutation | None:
    """Reconstruct the next action solely from frozen traversal and immutable observations."""

    progress = state.fuzz_progress
    if progress is None:
        raise RunStateError("fuzz campaign has not been initialized")
    steps = campaign_steps(progress)
    try:
        expected = next(steps)
    except StopIteration as finished:
        if progress.trials:
            raise RunStateError("campaign contains trials outside its bounded traversal") from None
        return finished.value
    by_id = {b.plan.block_id: b for b in blocks}
    for index, trial in enumerate(progress.trials):
        if (trial.role, trial.mutation) != expected:
            raise RunStateError("campaign trial order differs from its recorded decisions")
        if trial.plan is None:
            decision = MutationDecision.INVALID
        else:
            block = by_id.get(trial.plan.block_id)
            decision = None if block is None else screen_fuzz_evidence(block)
            if decision is None:
                if index != len(progress.trials) - 1:
                    raise RunStateError("campaign progressed past an unavailable screen")
                return expected
        try:
            expected = steps.send(decision)
        except StopIteration as finished:
            if index != len(progress.trials) - 1:
                raise RunStateError("campaign contains trials after final choice") from None
            return finished.value
    return expected


def validate_campaign_progress(
    state: SampledRunState,
    calibration: tuple[Observation, ...],
    blocks: tuple[SamplingBlockEvidence, ...],
) -> None:
    """Reject forged decisions and selection before the collector can dispatch anything."""

    progress = state.fuzz_progress
    if progress is None:
        raise RunStateError("fuzz campaign has not been initialized")
    config = progress.config
    if (
        progress.implementation_digest != campaign_implementation_digest()
        or config.digest != state.original_identity.algorithm_digest
        or config.original.digest != state.original_candidate_digest
    ):
        raise RunStateError("fuzz campaign implementation or original identity changed")
    valid = sum(o.valid for o in calibration)
    if (
        valid != config.calibration.required_valid
        or sum(o.outcome is ObservationOutcome.MATCH for o in calibration)
        < config.calibration.minimum_matches
        or len(calibration) > config.calibration.max_attempts
    ):
        raise RunStateError("fuzz campaign requires complete measurable calibration")
    if any(o.attempt != i for i, o in enumerate(calibration, 1)) or any(
        not o.allows_fresh_attempt(config.eligibility_policy) for o in calibration[:-1]
    ):
        raise RunStateError("calibration must preserve its exact safe attempt sequence")
    plans = tuple(t.plan for t in progress.trials if t.plan is not None)
    actual = tuple(b.plan for b in blocks if b.plan.role != "confirmation")
    if actual != plans[: len(actual)] or len(plans) - len(actual) not in (0, 1):
        raise RunStateError("campaign trials differ from authoritative sampling blocks")
    for index, trial in enumerate(progress.trials):
        if trial.plan is None:
            continue
        key = content_digest(
            {"run_id": state.run_id, "trial": index, "mutation": trial.mutation.digest}
        )
        plan = trial.plan
        if (
            plan.block_id != f"fuzz-{key}"
            or plan.schedule_seed != int(key[:16], 16)
            or plan.role != trial.role
            or plan.procedure != fuzz_procedure(config)
            or plan.original_identity != state.original_identity
            or plan.candidate_identity != state.original_identity
            or plan.original_candidate_digest != state.original_candidate_digest
        ):
            raise RunStateError("campaign trial has incompatible sampling policy or ownership")
    position = _replay(state, blocks)
    if state.selection is None:
        if progress.selected_mutation is not None or any(
            b.plan.role == "confirmation" for b in blocks
        ):
            raise RunStateError("fuzz confirmation requires durable final selection")
    else:
        if state.selection.plan.selected_at != state.selection.selected_at:
            raise RunStateError("confirmation must preserve the exact selection time")
        if not isinstance(position, TextMutation) or position != progress.selected_mutation:
            raise RunStateError("fuzz selection differs from authoritative exploratory decisions")
        if any(state.selection.selected_at < o.recorded_at for o in calibration) or any(
            state.selection.selected_at < a.observation.recorded_at
            for b in blocks
            if b.plan.role != "confirmation"
            for a in b.attempts
        ):
            raise RunStateError("fuzz selection must follow all exploratory evidence")
    if progress.terminal is FuzzOutcome.NO_CANDIDATE_FOUND and (
        position is not None
        or state.selection is not None
        or progress.stop_reason != "no-candidate"
    ):
        raise RunStateError("no-candidate outcome disagrees with bounded exploration")
    if progress.terminal in (FuzzOutcome.CONFIRMED_CHANGE, FuzzOutcome.INCONCLUSIVE):
        if state.selection is None or not blocks or blocks[-1].plan != state.selection.plan:
            raise RunStateError("terminal finding requires its exact fresh confirmation")
        result = confirm_fuzz_evidence(blocks[-1])
        if (
            result.outcome is not progress.terminal
            or progress.stop_reason != "confirmation-finished"
        ):
            raise RunStateError("terminal finding disagrees with authoritative observations")
    if progress.terminal is FuzzOutcome.INCOMPLETE and progress.collection_refusal is None:
        raise RunStateError("an incomplete campaign requires its collection refusal")


def _validate_materializations(
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    state: SampledRunState,
) -> None:
    progress = state.fuzz_progress
    if progress is None:
        raise RunStateError("fuzz campaign has not been initialized")
    bound = prepare_fuzz_sampling(evidence, prepared, progress.config)
    if bound.identity != state.original_identity:
        raise RunStateError("campaign execution identity changed")
    if bound.identity != prepared.identity:
        raise RunStateError("prepare campaign-bound sampling before continuation")
    batch = generate_mutations(
        evidence.editable_text(), progress.config, stale_text=progress.stale_text
    )
    if (
        batch.mutations != progress.generated
        or batch.proposals_examined != progress.proposals_examined
        or _generation_input(evidence, progress.stale_text) != progress.generation_input_digest
    ):
        raise RunStateError("campaign generation differs from frozen source, seed or payload")
    # Validate the immutable original before accepting any candidate-local refusal.
    from ithrl.runner import PreparedAttempt
    from ithrl.runner.contracts import CandidateAdmission

    if isinstance(prepared.runner, CandidateAdmission):
        prepared.runner.validate_candidate(
            PreparedAttempt(
                evidence=evidence,
                candidate=progress.config.original,
                target=prepared.experiment.target,
                execution_plan=prepared.execution_plan,
                safety_policy=prepared.safety_policy,
                resource_limits=prepared.resource_limits,
                phase="discovery",
                arm="baseline",
            )
        )
    for trial in progress.trials:
        try:
            partition = prepare_candidate(
                evidence, prepared.execution_plan, mutation=trial.mutation
            )
            if isinstance(prepared.runner, CandidateAdmission):
                prepared.runner.validate_candidate(
                    PreparedAttempt(
                        evidence=evidence,
                        candidate=partition,
                        target=prepared.experiment.target,
                        execution_plan=prepared.execution_plan,
                        safety_policy=prepared.safety_policy,
                        resource_limits=prepared.resource_limits,
                        phase=trial.role,
                        arm="candidate",
                    )
                )
        except CandidateInvalidError:
            if trial.plan is not None:
                raise RunStateError(
                    "previously admitted mutation is no longer reconstructable"
                ) from None
        else:
            if trial.plan is None or trial.plan.candidate_digest != partition.digest:
                raise RunStateError("campaign mutation does not match its recorded admission")


async def run_fuzz_campaign(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    clock: Callable[[], datetime] = _now,
    verify_independence: Callable[[Observation], str | None] | None = None,
    on_progress: Callable[[int, SamplingBlockEvidence], None] | None = None,
) -> FuzzCampaignResult:
    """Finish or recover the same campaign; complete terminal evidence is never redrawn."""

    with state_file.acquire_lock(store):
        return await _run_fuzz_campaign_owned(
            evidence=evidence,
            prepared=prepared,
            state_file=state_file,
            store=store,
            clock=clock,
            verify_independence=verify_independence,
            on_progress=on_progress,
        )


async def _run_fuzz_campaign_owned(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    clock: Callable[[], datetime] = _now,
    verify_independence: Callable[[Observation], str | None] | None = None,
    on_progress: Callable[[int, SamplingBlockEvidence], None] | None = None,
) -> FuzzCampaignResult:
    """Caller owns the shared run/store locks throughout composition."""

    state = state_file.load(store)
    _validate_materializations(evidence, prepared, state)
    while True:
        state = state_file.load(store)
        progress = state.fuzz_progress
        if progress is None:
            raise RunStateError("fuzz campaign has not been initialized")
        if progress.terminal is not None:
            return _result(state_file, store, clock(), None)
        blocks = state_file.evidence(store)
        if (
            state.blocks
            and not state.blocks[-1].archived
            and state.blocks[-1].plan.role != "confirmation"
            and blocks[-1].complete
            and blocks[-1].eligible
        ):
            state_file.archive_block(store)
            continue
        if state.selection is not None:
            mutation, plan = progress.selected_mutation, state.selection.plan
            if mutation is None:
                raise RunStateError("fuzz selection has no frozen mutation")
            if blocks and blocks[-1].plan == plan and blocks[-1].complete and blocks[-1].eligible:
                confirmation = confirm_fuzz_evidence(blocks[-1])
                _finish(state_file, store, progress, confirmation.outcome, "confirmation-finished")
                continue
        else:
            position = _replay(state, blocks)
            if isinstance(position, TextMutation):
                selected_at = clock()
                holdout = content_digest({"run_id": state.run_id, "role": "confirmation"})
                plan = fuzz_block_plan(
                    evidence=evidence,
                    prepared=prepared,
                    campaign=progress.config,
                    mutation=position,
                    role="confirmation",
                    block_id=f"confirmation-{holdout}",
                    frozen_at=max(clock(), selected_at + timedelta(microseconds=1)),
                    selected_at=selected_at,
                    schedule_seed=int(holdout[:16], 16),
                )
                selected = progress.model_copy(update={"selected_mutation": position})
                state_file.select_fuzz_candidate(
                    store,
                    selected,
                    SampledFinalSelection(
                        checkpoint_digest=selected.selection_digest,
                        selected_at=selected_at,
                        plan=plan,
                    ),
                )
                continue
            if position is None:
                _finish(state_file, store, progress, FuzzOutcome.NO_CANDIDATE_FOUND, "no-candidate")
                continue
            role, mutation = position
            active = next((t for t in progress.trials if t.mutation == mutation), None)
            if active is None:
                index = len(progress.trials)
                key = content_digest(
                    {"run_id": state.run_id, "trial": index, "mutation": mutation.digest}
                )
                try:
                    plan = fuzz_block_plan(
                        evidence=evidence,
                        prepared=prepared,
                        campaign=progress.config,
                        mutation=mutation,
                        role=role,
                        block_id=f"fuzz-{key}",
                        frozen_at=clock(),
                        schedule_seed=int(key[:16], 16),
                    )
                except CandidateInvalidError:
                    plan = None
                active = FuzzTrial(role=role, mutation=mutation, plan=plan)
                progress = progress.model_copy(update={"trials": (*progress.trials, active)})
                state_file.save_fuzz_progress(store, progress)
            if active.plan is None:
                continue
            plan = active.plan
        collected = await _collect_sampling_block_owned(
            evidence=evidence,
            original=prepared,
            candidate=prepared,
            removed_context_ids=(),
            plan=plan,
            state_file=state_file,
            store=store,
            clock=clock,
            verify_independence=verify_independence,
            mutation=mutation,
            on_progress=partial(on_progress, len(progress.trials)) if on_progress else None,
        )
        if collected.stop_reason == "unresolved-dispatch":
            return _result(state_file, store, clock(), "unresolved-dispatch")
        if plan.role == "confirmation":
            # Reconciliation may have recovered the last owned observation after
            # the execution deadline. Complete eligible evidence still has its
            # original timestamps; no further dispatch or admission is needed.
            confirmation = confirm_fuzz_evidence(collected.evidence)
            _finish(
                state_file,
                store,
                progress,
                confirmation.outcome,
                "confirmation-finished",
                collected.stop_reason if confirmation.outcome is FuzzOutcome.INCOMPLETE else None,
            )
        elif screen_fuzz_block(collected) is None:
            _finish(
                state_file,
                store,
                progress,
                FuzzOutcome.INCOMPLETE,
                "screen-incomplete",
                collected.stop_reason,
            )
        else:
            state_file.archive_block(store)


def _finish(
    state_file: SampledRunStateFile,
    store: ObservationStore,
    progress: FuzzCampaignProgress,
    outcome: FuzzOutcome,
    reason: str,
    refusal: str | None = None,
) -> None:
    state_file.save_fuzz_progress(
        store,
        progress.model_copy(
            update={"terminal": outcome, "stop_reason": reason, "collection_refusal": refusal}
        ),
    )


def _result(
    state_file: SampledRunStateFile,
    store: ObservationStore,
    now: datetime,
    refusal: str | None,
) -> FuzzCampaignResult:
    state = state_file.load(store)
    progress = state.fuzz_progress
    if progress is None:
        raise RunStateError("fuzz campaign has not been initialized")
    confirmation = None
    if (
        state.selection is not None
        and state.blocks
        and state.blocks[-1].plan.role == "confirmation"
    ):
        confirmation = confirm_fuzz_evidence(state_file.evidence(store)[-1])
        if progress.terminal is FuzzOutcome.INCOMPLETE:
            confirmation = ChangeConfirmation(FuzzOutcome.INCOMPLETE, None, None, None, None)
    return FuzzCampaignResult(
        progress.terminal or FuzzOutcome.INCOMPLETE,
        progress.selected_mutation,
        confirmation,
        state_file.usage(store, now=now),
        refusal or progress.collection_refusal or progress.stop_reason,
    )
