"""Conservative cost bounds for sampled calibration and sampling blocks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ithrl.execution.contracts import CapabilityProfile, ResourceLimits
from ithrl.experiment import SampledCalibrationPhase

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
        if type(self.provider_calls) is not int or self.provider_calls < 1:
            raise ValueError("provider calls per attempt must be a positive integer")
        if type(self.runner_seconds) is not int or self.runner_seconds < 1:
            raise ValueError("runner seconds per attempt must be a positive integer")


def sampling_attempt_cost(
    capability: CapabilityProfile, limits: ResourceLimits
) -> SamplingAttemptCost:
    """Resolve one attempt's maximum calls and timeout including cancellation grace."""

    if capability.deterministic:
        raise ValueError("sampling requires a nondeterministic runner profile")
    if capability.provider_calls_per_attempt < 1:
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
    role: Literal["search", "confirmation"],
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
    if role not in {"search", "confirmation"}:
        raise ValueError("role must be search or confirmation")

    confirmation_calls = confirmation_attempts * attempt_cost.provider_calls
    confirmation_seconds = confirmation_attempts * attempt_cost.runner_seconds
    if allowance.confirmation_provider_calls < confirmation_calls:
        return "confirmation-call-reserve"
    if allowance.confirmation_runner_seconds < confirmation_seconds:
        return "confirmation-time-reserve"
    if search_blocks > allowance.max_candidate_comparisons:
        return "candidate-limit"

    if role == "search":
        future_calls = remaining_attempts * attempt_cost.provider_calls
        future_calls += allowance.confirmation_provider_calls
        future_seconds = (
            remaining_attempts * attempt_cost.runner_seconds + allowance.confirmation_runner_seconds
        )
    else:
        # The confirmation reserve is already part of the overall allowance. During
        # confirmation, charge only the remaining attempts to avoid counting it twice.
        future_calls = remaining_attempts * attempt_cost.provider_calls
        future_seconds = remaining_attempts * attempt_cost.runner_seconds

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

    if capability.deterministic:
        raise ValueError("sampled calibration requires a nondeterministic runner profile")
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
