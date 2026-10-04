from __future__ import annotations

from typing import Literal, cast

import pytest

from ithrl.execution.budget import (
    SamplingAttemptCost,
    sampling_attempt_cost,
    sampling_budget_refusal,
)
from ithrl.execution.contracts import CapabilityProfile, ResourceLimits
from ithrl.observations import SamplingBudgetAllowance


def _allowance(
    *,
    calls: int = 20,
    seconds: int = 100,
    comparisons: int = 3,
    confirmation_calls: int = 4,
    confirmation_seconds: int = 20,
) -> SamplingBudgetAllowance:
    return SamplingBudgetAllowance(
        run_id="run-1",
        max_provider_calls=calls,
        max_wall_time_seconds=seconds,
        max_candidate_comparisons=comparisons,
        confirmation_provider_calls=confirmation_calls,
        confirmation_runner_seconds=confirmation_seconds,
    )


def test_sampling_attempt_cost_includes_cancellation_grace() -> None:
    capability = CapabilityProfile.model_validate(
        {
            **CapabilityProfile.deterministic_fake().model_dump(mode="python"),
            "deterministic": False,
            "provider_transport": "required",
            "provider_calls_per_attempt": 2,
            "provider": "fixture-provider",
            "endpoint": "https://provider.invalid",
            "cancellation_grace_seconds": 3,
        }
    )

    assert sampling_attempt_cost(
        capability, ResourceLimits(max_provider_calls=2, max_wall_time_seconds=7)
    ) == SamplingAttemptCost(provider_calls=2, runner_seconds=10)


def test_search_budget_fits_exact_remaining_cost_and_protected_confirmation() -> None:
    cost = SamplingAttemptCost(provider_calls=2, runner_seconds=10)

    assert (
        sampling_budget_refusal(
            allowance=_allowance(
                calls=14, seconds=70, confirmation_calls=4, confirmation_seconds=20
            ),
            charged_provider_calls=4,
            elapsed_seconds=20,
            remaining_attempts=3,  # Includes any retry or replacement slots.
            attempt_cost=cost,
            confirmation_attempts=2,
            search_blocks=3,
            role="search",
        )
        is None
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"calls": 13}, "provider-call-budget"),
        ({"seconds": 69}, "wall-time-budget"),
        ({"confirmation_calls": 3}, "confirmation-call-reserve"),
        ({"confirmation_seconds": 19}, "confirmation-time-reserve"),
    ],
)
def test_search_budget_refuses_insufficient_call_or_time_capacity(
    changes: dict[str, int], expected: str
) -> None:
    assert expected in (
        sampling_budget_refusal(
            allowance=_allowance(**changes),
            charged_provider_calls=4,
            elapsed_seconds=20,
            remaining_attempts=3,
            attempt_cost=SamplingAttemptCost(provider_calls=2, runner_seconds=10),
            confirmation_attempts=2,
            search_blocks=1,
            role="search",
        )
        or ""
    )


def test_confirmation_consumes_reserve_without_double_charging() -> None:
    assert (
        sampling_budget_refusal(
            allowance=_allowance(
                calls=8, seconds=40, confirmation_calls=4, confirmation_seconds=20
            ),
            charged_provider_calls=4,
            elapsed_seconds=20,
            remaining_attempts=2,
            attempt_cost=SamplingAttemptCost(provider_calls=2, runner_seconds=10),
            confirmation_attempts=2,
            search_blocks=1,
            role="confirmation",
        )
        is None
    )


def test_confirmation_fits_remaining_whole_block_after_search_usage() -> None:
    assert (
        sampling_budget_refusal(
            allowance=_allowance(
                calls=7, seconds=50, confirmation_calls=4, confirmation_seconds=20
            ),
            charged_provider_calls=4,
            elapsed_seconds=20,
            remaining_attempts=2,
            attempt_cost=SamplingAttemptCost(provider_calls=2, runner_seconds=10),
            confirmation_attempts=2,
            search_blocks=1,
            role="confirmation",
        )
        == "provider-call-budget"
    )


def test_expired_wall_time_and_comparison_limit_are_refused() -> None:
    cost = SamplingAttemptCost(provider_calls=1, runner_seconds=5)
    assert (
        sampling_budget_refusal(
            allowance=_allowance(seconds=24, confirmation_seconds=5),
            charged_provider_calls=0,
            elapsed_seconds=20,
            remaining_attempts=0,
            attempt_cost=cost,
            confirmation_attempts=1,
            search_blocks=1,
            role="search",
        )
        == "wall-time-budget"
    )
    assert (
        sampling_budget_refusal(
            allowance=_allowance(comparisons=1, confirmation_seconds=5),
            charged_provider_calls=0,
            elapsed_seconds=0,
            remaining_attempts=0,
            attempt_cost=cost,
            confirmation_attempts=1,
            search_blocks=2,
            role="search",
        )
        == "candidate-limit"
    )


def test_invalid_budget_arguments_fail_closed() -> None:
    allowance = _allowance(confirmation_seconds=5)
    cost = SamplingAttemptCost(provider_calls=1, runner_seconds=5)
    with pytest.raises(ValueError, match="charged provider calls"):
        sampling_budget_refusal(
            allowance=allowance,
            charged_provider_calls=True,
            elapsed_seconds=0,
            remaining_attempts=0,
            attempt_cost=cost,
            confirmation_attempts=1,
            search_blocks=1,
            role="search",
        )
    with pytest.raises(ValueError, match="elapsed seconds"):
        sampling_budget_refusal(
            allowance=allowance,
            charged_provider_calls=0,
            elapsed_seconds=float("inf"),
            remaining_attempts=0,
            attempt_cost=cost,
            confirmation_attempts=1,
            search_blocks=1,
            role="search",
        )
    with pytest.raises(ValueError, match="remaining attempts"):
        sampling_budget_refusal(
            allowance=allowance,
            charged_provider_calls=0,
            elapsed_seconds=0,
            remaining_attempts=-1,
            attempt_cost=cost,
            confirmation_attempts=1,
            search_blocks=1,
            role="search",
        )
    with pytest.raises(ValueError, match="confirmation attempts"):
        sampling_budget_refusal(
            allowance=allowance,
            charged_provider_calls=0,
            elapsed_seconds=0,
            remaining_attempts=0,
            attempt_cost=cost,
            confirmation_attempts=0,
            search_blocks=1,
            role="search",
        )
    with pytest.raises(ValueError, match="role"):
        sampling_budget_refusal(
            allowance=allowance,
            charged_provider_calls=0,
            elapsed_seconds=0,
            remaining_attempts=0,
            attempt_cost=cost,
            confirmation_attempts=1,
            search_blocks=1,
            role=cast(Literal["search", "confirmation"], "calibration"),
        )


def test_confirmation_can_run_without_any_search_blocks() -> None:
    assert (
        sampling_budget_refusal(
            allowance=_allowance(
                calls=4,
                seconds=20,
                comparisons=1,
                confirmation_calls=2,
                confirmation_seconds=10,
            ),
            charged_provider_calls=0,
            elapsed_seconds=0,
            remaining_attempts=2,
            attempt_cost=SamplingAttemptCost(provider_calls=1, runner_seconds=5),
            confirmation_attempts=2,
            search_blocks=0,
            role="confirmation",
        )
        is None
    )


def test_attempt_cost_requires_enforceable_per_attempt_call_limit() -> None:
    capability = CapabilityProfile.model_validate(
        {
            **CapabilityProfile.deterministic_fake().model_dump(mode="python"),
            "deterministic": False,
            "provider_transport": "required",
            "provider_calls_per_attempt": 2,
            "provider": "fixture-provider",
            "endpoint": "https://provider.invalid",
        }
    )
    with pytest.raises(ValueError, match="provider-call limit"):
        sampling_attempt_cost(capability, ResourceLimits(max_provider_calls=1))
    with pytest.raises(ValueError, match="input-token limit"):
        sampling_attempt_cost(
            capability,
            ResourceLimits(max_provider_calls=2, max_input_tokens=100),
        )


@pytest.mark.parametrize(
    ("provider_calls", "runner_seconds"),
    [(0, 1), (1, 0), (True, 1), (1, True)],
)
def test_attempt_cost_requires_positive_integer_fields(
    provider_calls: int, runner_seconds: int
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SamplingAttemptCost(provider_calls=provider_calls, runner_seconds=runner_seconds)
