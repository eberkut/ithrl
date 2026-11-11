"""Conservative cost bounds for sampled calibration and sampling blocks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ithrl.execution.contracts import CapabilityProfile, ResourceLimits
from ithrl.experiment import SampledCalibrationPhase, SampledReductionPlan
from ithrl.statistics import optimistic_attainability

if TYPE_CHECKING:
    from ithrl.observations import SamplingBudgetAllowance


@dataclass(frozen=True, slots=True)
class CalibrationBudgetBounds:
    """Maximum runner work, excluding unbounded local preparation and persistence."""

    attempts: int
    provider_calls: int
    input_tokens: int | None
    runner_seconds: int


@dataclass(frozen=True, slots=True)
class SamplingAttemptCost:
    """Conservative provider and runner cost for one sampled attempt."""

    provider_calls: int
    runner_seconds: int

    def __post_init__(self) -> None:
        if type(self.provider_calls) is not int or self.provider_calls < 0:
            raise ValueError("provider calls per attempt must be a nonnegative integer")
        if type(self.runner_seconds) is not int or self.runner_seconds < 1:
            raise ValueError("runner seconds per attempt must be a positive integer")


@dataclass(frozen=True, slots=True)
class ReductionBudgetBounds:
    """Frozen-plan bounds and the first applicable preflight refusal."""

    max_attempts: int
    max_provider_calls: int
    max_runner_seconds: int
    max_wall_time_seconds: int
    candidate_capacity: int
    minimum_provider_calls: int
    minimum_wall_time_seconds: int
    confirmation_provider_calls: int
    confirmation_runner_seconds: int
    refusal_code: (
        Literal[
            "sampling-budget-too-small", "sampling-freshness-too-small", "confirmation-unattainable"
        ]
        | None
    )


def reduction_budget_bounds(
    *,
    calibration: SampledCalibrationPhase,
    reduction: SampledReductionPlan,
    attempt_cost: SamplingAttemptCost,
    max_candidate_comparisons: int,
    max_provider_calls: int | None = None,
    max_wall_time_seconds: int | None = None,
    inter_attempt_delay_seconds: int = 0,
    remaining_minimum_attempts: int | None = None,
) -> ReductionBudgetBounds:
    """Calculate reduction admission without loading state or invoking a runner.

    A supplied remaining minimum comes from a resumed run. Its minimum bounds are
    informational: only a new run refuses administrative ceilings below the minimum.
    """

    if type(max_candidate_comparisons) is not int or max_candidate_comparisons < 1:
        raise ValueError("candidate budget unavailable")
    if (
        type(inter_attempt_delay_seconds) is not int
        or not 0 <= inter_attempt_delay_seconds <= 86_400
    ):
        raise ValueError("inter-attempt delay must be bounded nonnegative seconds")
    for name, value in (
        ("provider-call budget", max_provider_calls),
        ("wall-time budget", max_wall_time_seconds),
    ):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"{name} must be a positive integer")
    if remaining_minimum_attempts is not None and (
        type(remaining_minimum_attempts) is not int or remaining_minimum_attempts < 0
    ):
        raise ValueError("remaining minimum attempts must be a nonnegative integer")
    if attempt_cost.provider_calls < 1:
        raise ValueError("sampled reduction requires a positive provider cost")

    # Conservatively admit a full pause for every future attempt; the first may need none.
    seconds_per_attempt = attempt_cost.runner_seconds + inter_attempt_delay_seconds
    calibration_attempts = calibration.max_attempts
    search = reduction.search.original.max_attempts + reduction.search.candidate.max_attempts
    confirmation = (
        reduction.confirmation.original.max_attempts + reduction.confirmation.candidate.max_attempts
    )
    minimum_attempts = (
        calibration_attempts + search + confirmation
        if remaining_minimum_attempts is None
        else remaining_minimum_attempts
    )
    minimum_calls = minimum_attempts * attempt_cost.provider_calls
    minimum_seconds = minimum_attempts * seconds_per_attempt
    refusal: (
        Literal[
            "sampling-budget-too-small", "sampling-freshness-too-small", "confirmation-unattainable"
        ]
        | None
    ) = None
    if remaining_minimum_attempts is None and (
        (max_provider_calls is not None and max_provider_calls < minimum_calls)
        or (max_wall_time_seconds is not None and max_wall_time_seconds < minimum_seconds)
    ):
        refusal = "sampling-budget-too-small"
    elif max(search, confirmation) * seconds_per_attempt >= reduction.freshness_seconds:
        refusal = "sampling-freshness-too-small"
    elif not optimistic_attainability(
        required_candidate=reduction.confirmation.candidate.required_valid,
        required_original=reduction.confirmation.original.required_valid,
        confidence=reduction.confirmation.one_sided_confidence,
        margin=reduction.margin,
    ).preservation_possible:
        refusal = "confirmation-unattainable"

    comparisons = max_candidate_comparisons
    if reduction.section_search is not None:
        sections = reduction.section_search
        comparisons = min(
            comparisons, sections.max_coarse_comparisons + sections.max_fine_comparisons
        )
    attempts = calibration_attempts + comparisons * search + confirmation
    calls = (
        max_provider_calls
        if max_provider_calls is not None
        else attempts * attempt_cost.provider_calls
    )
    seconds = (
        max_wall_time_seconds
        if max_wall_time_seconds is not None
        else attempts * seconds_per_attempt
    )
    capacity = max(
        0,
        min(
            comparisons,
            (calls - (calibration_attempts + confirmation) * attempt_cost.provider_calls)
            // (search * attempt_cost.provider_calls),
            (seconds - (calibration_attempts + confirmation) * seconds_per_attempt)
            // (search * seconds_per_attempt),
        ),
    )
    return ReductionBudgetBounds(
        max_attempts=attempts,
        max_provider_calls=calls,
        max_runner_seconds=attempts * attempt_cost.runner_seconds,
        max_wall_time_seconds=seconds,
        candidate_capacity=capacity,
        minimum_provider_calls=minimum_calls,
        minimum_wall_time_seconds=minimum_seconds,
        confirmation_provider_calls=confirmation * attempt_cost.provider_calls,
        confirmation_runner_seconds=confirmation * attempt_cost.runner_seconds,
        refusal_code=refusal,
    )


def sampling_attempt_cost(
    capability: CapabilityProfile, limits: ResourceLimits
) -> SamplingAttemptCost:
    """Resolve one attempt's maximum calls and timeout including cancellation grace."""

    if capability.deterministic and not (
        capability.runner_type == "fake"
        and capability.provider_transport == "forbidden"
        and capability.provider_calls_per_attempt == 0
    ):
        raise ValueError("deterministic collection requires the credential-free fake runner")
    if not capability.deterministic and capability.provider_calls_per_attempt < 1:
        raise ValueError("sampling requires at least one provider call per attempt")
    if capability.provider_calls_per_attempt > limits.max_provider_calls:
        raise ValueError("provider-call limit is below the runner profile maximum")
    if limits.max_input_tokens is not None and (
        capability.input_token_limit_enforcement != "pre-dispatch"
    ):
        raise ValueError("input-token limit is not enforceable before dispatch")
    return SamplingAttemptCost(
        provider_calls=capability.provider_calls_per_attempt,
        runner_seconds=(limits.max_wall_time_seconds + capability.cancellation_grace_seconds),
    )


def sampling_budget_refusal(
    *,
    allowance: SamplingBudgetAllowance,
    charged_provider_calls: int,
    elapsed_seconds: float,
    remaining_attempts: int,
    attempt_cost: SamplingAttemptCost,
    confirmation_attempts: int,
    search_blocks: int,
    role: Literal["search", "discovery", "simplification", "confirmation"],
) -> str | None:
    """Return why remaining sampling and protected confirmation capacity do not fit."""

    if type(attempt_cost) is not SamplingAttemptCost:
        raise TypeError("attempt_cost must be SamplingAttemptCost")
    if type(charged_provider_calls) is not int or charged_provider_calls < 0:
        raise ValueError("charged provider calls must be a nonnegative integer")
    if (
        isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, (int, float))
        or not math.isfinite(elapsed_seconds)
        or elapsed_seconds < 0
    ):
        raise ValueError("elapsed seconds must be a finite nonnegative number")
    for name, value, minimum in (
        ("remaining attempts", remaining_attempts, 0),
        ("confirmation attempts", confirmation_attempts, 1),
        ("search blocks", search_blocks, 0),
    ):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer of at least {minimum}")
    if role not in {"search", "discovery", "simplification", "confirmation"}:
        raise ValueError("role must be exploratory or confirmation")

    confirmation_calls = confirmation_attempts * attempt_cost.provider_calls
    confirmation_seconds = confirmation_attempts * attempt_cost.runner_seconds
    if allowance.confirmation_provider_calls < confirmation_calls:
        return "confirmation-call-reserve"
    if allowance.confirmation_runner_seconds < confirmation_seconds:
        return "confirmation-time-reserve"
    if search_blocks > allowance.max_candidate_comparisons:
        return "candidate-limit"

    # During confirmation, its reserve is already included in the remaining attempts.
    future_calls = remaining_attempts * attempt_cost.provider_calls
    future_seconds = remaining_attempts * (
        attempt_cost.runner_seconds + allowance.inter_attempt_delay_seconds
    )
    if role != "confirmation":
        future_calls += allowance.confirmation_provider_calls
        future_seconds += allowance.confirmation_runner_seconds
        future_seconds += confirmation_attempts * allowance.inter_attempt_delay_seconds

    if charged_provider_calls + future_calls > allowance.max_provider_calls:
        return "provider-call-budget"
    if elapsed_seconds + future_seconds > allowance.max_wall_time_seconds:
        return "wall-time-budget"
    return None


def calibration_budget_bounds(
    phase: SampledCalibrationPhase,
    capability: CapabilityProfile,
    limits: ResourceLimits,
) -> CalibrationBudgetBounds:
    """Calculate only limits the resolved runner can enforce before dispatch."""

    if capability.deterministic and not (
        capability.runner_type == "fake"
        and capability.provider_transport == "forbidden"
        and capability.provider_calls_per_attempt == 0
    ):
        raise ValueError("deterministic calibration requires the credential-free fake runner")
    if capability.provider_calls_per_attempt > limits.max_provider_calls:
        raise ValueError("provider-call limit is below the runner profile maximum")
    if limits.max_input_tokens is not None and (
        capability.input_token_limit_enforcement != "pre-dispatch"
    ):
        raise ValueError("input-token limit is not enforceable before dispatch")
    return CalibrationBudgetBounds(
        attempts=phase.max_attempts,
        provider_calls=phase.max_attempts * capability.provider_calls_per_attempt,
        input_tokens=(
            phase.max_attempts * limits.max_input_tokens
            if limits.max_input_tokens is not None
            else None
        ),
        runner_seconds=phase.max_attempts
        * (limits.max_wall_time_seconds + capability.cancellation_grace_seconds),
    )
