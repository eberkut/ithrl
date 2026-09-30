"""Conservative pre-dispatch bounds for one sampled calibration block."""

from __future__ import annotations

from dataclasses import dataclass

from ithrl.execution.contracts import CapabilityProfile, ResourceLimits
from ithrl.experiment import SampledCalibrationPhase


@dataclass(frozen=True, slots=True)
class CalibrationBudgetBounds:
    """Maximum runner work, excluding unbounded local preparation and persistence."""

    attempts: int
    provider_calls: int
    input_tokens: int | None
    runner_seconds: int


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
