"""Fuzz-specific policy and decisions around the shared frozen-block collector.

Campaign ordering, durable final selection and resume are owned by the campaign
layer. This module provides bounded comparisons without inventing a second
dispatch, accounting or eligibility engine.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from fractions import Fraction
from typing import Literal

from ithrl.case import CaseEvidence
from ithrl.execution.budget import SamplingAttemptCost, sampling_attempt_cost
from ithrl.experiment import (
    SampledChangeConfirmation,
    SampledChangePlan,
    SampledExperimentDefinition,
    SampledSearchPhase,
)
from ithrl.fuzz.contracts import FuzzCampaignConfig, FuzzOutcome
from ithrl.fuzz.simplify import MutationDecision
from ithrl.fuzz.statistics import (
    ChangeConfirmation,
    confirm_change,
    require_fuzz_numerics,
    validate_change_parameters,
)
from ithrl.mutation import TextMutation
from ithrl.observations import SamplingBlockPlan, sampling_schedule
from ithrl.runner import PreparedAttempt, prepare_candidate
from ithrl.runner.contracts import CandidateAdmission
from ithrl.sampling import SamplingCollectionResult
from ithrl.statistics import BinomialSample
from ithrl.workflow import PreparedExperiment

type FuzzSamplingRole = Literal["discovery", "simplification", "confirmation"]


def prepare_fuzz_sampling(
    evidence: CaseEvidence, prepared: PreparedExperiment, campaign: FuzzCampaignConfig
) -> PreparedExperiment:
    """Bind observations to the campaign policy without changing case evidence."""

    require_fuzz_numerics()
    validate_change_parameters(
        alpha=campaign.confirmation.alpha, minimum_decrease=campaign.minimum_decrease
    )
    if not isinstance(prepared.experiment, SampledExperimentDefinition):
        raise ValueError("sampled fuzzing requires a sampled experiment")
    if prepared.experiment.calibration != campaign.calibration:
        raise ValueError("campaign calibration differs from the prepared experiment")
    if prepare_candidate(evidence, prepared.execution_plan) != campaign.original:
        raise ValueError("campaign original differs from the immutable execution candidate")
    sampling_attempt_cost(prepared.runner.capability_profile, prepared.resource_limits)
    return replace(
        prepared,
        identity=prepared.identity.model_copy(update={"algorithm_digest": campaign.digest}),
    )


def fuzz_procedure(campaign: FuzzCampaignConfig) -> SampledChangePlan:
    return SampledChangePlan(
        campaign_digest=campaign.digest,
        search=SampledSearchPhase(
            original=campaign.screening.original, candidate=campaign.screening.mutant
        ),
        confirmation=SampledChangeConfirmation(
            original=campaign.confirmation.original,
            candidate=campaign.confirmation.mutant,
            alpha=campaign.confirmation.alpha,
        ),
        screening_decrease=campaign.screening.minimum_decrease,
        minimum_decrease=campaign.minimum_decrease,
        eligibility_policy=campaign.eligibility_policy,
        freshness_seconds=campaign.freshness_seconds,
    )


def fuzz_block_plan(
    *,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    campaign: FuzzCampaignConfig,
    mutation: TextMutation,
    role: FuzzSamplingRole,
    block_id: str,
    frozen_at: datetime,
    schedule_seed: int,
    selected_at: datetime | None = None,
) -> SamplingBlockPlan:
    """Pure admission and a frozen role/arm/payload identity before budget allocation."""

    bound = prepare_fuzz_sampling(evidence, prepared, campaign)
    if bound.identity != prepared.identity:
        raise ValueError("prepare campaign-bound sampling identities before creating blocks")
    if mutation.is_empty:
        raise ValueError("a fuzz comparison requires a nonempty intervention")
    original = prepare_candidate(evidence, prepared.execution_plan)
    candidate = prepare_candidate(evidence, prepared.execution_plan, mutation=mutation)
    for partition, arm in ((original, "baseline"), (candidate, "candidate")):
        attempt = PreparedAttempt(
            evidence=evidence,
            candidate=partition,
            target=prepared.experiment.target,
            execution_plan=prepared.execution_plan,
            safety_policy=prepared.safety_policy,
            resource_limits=prepared.resource_limits,
            phase=role,
            arm=arm,
        )
        if isinstance(prepared.runner, CandidateAdmission):
            prepared.runner.validate_candidate(attempt)
    procedure = fuzz_procedure(campaign)
    targets = procedure.confirmation if role == "confirmation" else procedure.search
    return SamplingBlockPlan(
        block_id=block_id,
        role=role,
        procedure=procedure,
        original_identity=prepared.identity,
        candidate_identity=prepared.identity,
        original_candidate_digest=original.digest,
        candidate_digest=candidate.digest,
        frozen_at=frozen_at,
        selected_at=selected_at,
        schedule_seed=schedule_seed,
        schedule=sampling_schedule(
            targets.original.max_attempts, targets.candidate.max_attempts, schedule_seed
        ),
        provider_calls_per_attempt=prepared.runner.capability_profile.provider_calls_per_attempt,
    )


@dataclass(frozen=True, slots=True)
class FuzzBudgetBounds:
    """Worst-case planned work; administrative call/time limits can stop exploration earlier."""

    exploratory_comparisons: int
    planned_attempts: int
    planned_provider_calls: int
    planned_runner_seconds: int
    planned_wall_time_seconds: int
    confirmation_attempts: int
    confirmation_provider_calls: int
    confirmation_runner_seconds: int
    input_tokens: None = None


def fuzz_budget_bounds(
    campaign: FuzzCampaignConfig,
    cost: SamplingAttemptCost,
    *,
    inter_attempt_delay_seconds: int = 0,
) -> FuzzBudgetBounds:
    if (
        type(inter_attempt_delay_seconds) is not int
        or not 0 <= inter_attempt_delay_seconds <= 86_400
    ):
        raise ValueError("pacing requires a bounded nonnegative number of seconds")
    comparisons = campaign.max_candidates + campaign.max_simplification_trials
    exploratory = comparisons * (
        campaign.screening.original.max_attempts + campaign.screening.mutant.max_attempts
    )
    final = campaign.confirmation.original.max_attempts + campaign.confirmation.mutant.max_attempts
    attempts = campaign.calibration.max_attempts + exploratory + final
    return FuzzBudgetBounds(
        exploratory_comparisons=comparisons,
        planned_attempts=attempts,
        planned_provider_calls=attempts * cost.provider_calls,
        planned_runner_seconds=attempts * cost.runner_seconds,
        planned_wall_time_seconds=attempts * (cost.runner_seconds + inter_attempt_delay_seconds),
        confirmation_attempts=final,
        confirmation_provider_calls=final * cost.provider_calls,
        confirmation_runner_seconds=final * cost.runner_seconds,
    )


def screen_fuzz_block(result: SamplingCollectionResult) -> MutationDecision | None:
    """An exploratory point screen; incomplete/ineligible evidence has no decision."""

    plan = result.evidence.plan
    if not isinstance(plan.procedure, SampledChangePlan) or plan.role not in {
        "discovery",
        "simplification",
    }:
        raise ValueError("fuzz screening requires exploratory change evidence")
    if not result.complete:
        return None
    original, mutant = result.evidence.original_counts, result.evidence.candidate_counts
    decrease = Fraction(original.matches, original.valid) - Fraction(mutant.matches, mutant.valid)
    return (
        MutationDecision.CHANGED
        if decrease >= Fraction(str(plan.procedure.screening_decrease))
        else MutationDecision.UNCHANGED
    )


def confirm_fuzz_block(result: SamplingCollectionResult) -> ChangeConfirmation:
    plan = result.evidence.plan
    if not isinstance(plan.procedure, SampledChangePlan) or plan.role != "confirmation":
        raise ValueError("change confirmation requires its fresh selected comparison")
    if not result.complete:
        return ChangeConfirmation(FuzzOutcome.INCOMPLETE, None, None, None, None)
    return confirm_change(
        BinomialSample(
            result.evidence.original_counts.matches, result.evidence.original_counts.valid
        ),
        BinomialSample(
            result.evidence.candidate_counts.matches, result.evidence.candidate_counts.valid
        ),
        required_original=plan.procedure.confirmation.original.required_valid,
        required_mutant=plan.procedure.confirmation.candidate.required_valid,
        alpha=plan.procedure.confirmation.alpha,
        minimum_decrease=plan.procedure.minimum_decrease,
    )
