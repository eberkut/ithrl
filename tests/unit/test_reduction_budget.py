from __future__ import annotations

import pytest

from ithrl.execution.budget import SamplingAttemptCost, reduction_budget_bounds
from ithrl.experiment import (
    SampledCalibrationPhase,
    SampledConfirmationPhase,
    SampledReductionPlan,
    SampledSearchPhase,
    SamplingArmTarget,
    TextSectionSearchPlan,
)
from ithrl.text_sections import TextSectionRemoval

CALIBRATION = SampledCalibrationPhase(required_valid=2, minimum_matches=2, max_attempts=3)
REDUCTION = SampledReductionPlan(
    search=SampledSearchPhase(
        original=SamplingArmTarget(required_valid=1, max_attempts=2),
        candidate=SamplingArmTarget(required_valid=2, max_attempts=3),
    ),
    confirmation=SampledConfirmationPhase(
        original=SamplingArmTarget(required_valid=11, max_attempts=12),
        candidate=SamplingArmTarget(required_valid=11, max_attempts=13),
        one_sided_confidence=0.95,
    ),
    margin=0.2,
    freshness_seconds=3600,
)
COST = SamplingAttemptCost(provider_calls=4, runner_seconds=13)


def test_reduction_bounds_include_asymmetric_attempt_caps_and_pacing() -> None:
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION,
        attempt_cost=COST,
        max_candidate_comparisons=4,
        inter_attempt_delay_seconds=2,
    )
    assert bounds.refusal_code is None
    assert bounds.max_attempts == 48  # 3 calibration + 4 * (2 + 3) search + (12 + 13) confirmation.
    assert bounds.max_provider_calls == 192
    assert bounds.max_runner_seconds == 624
    assert bounds.max_wall_time_seconds == 720
    assert bounds.minimum_provider_calls == 132
    assert bounds.minimum_wall_time_seconds == 495
    assert bounds.confirmation_provider_calls == 100
    assert bounds.confirmation_runner_seconds == 325
    assert bounds.candidate_capacity == 4


@pytest.mark.parametrize(
    ("calls", "seconds", "capacity", "refusal"),
    [(152, 1000, 2, None), (500, 570, 2, None), (131, 1000, 0, "sampling-budget-too-small")],
)
def test_administrative_ceilings_reserve_calibration_and_confirmation(
    calls: int, seconds: int, capacity: int, refusal: str | None
) -> None:
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION,
        attempt_cost=COST,
        max_candidate_comparisons=4,
        max_provider_calls=calls,
        max_wall_time_seconds=seconds,
        inter_attempt_delay_seconds=2,
    )
    assert bounds.max_provider_calls == calls
    assert bounds.max_wall_time_seconds == seconds
    assert bounds.candidate_capacity == capacity
    assert bounds.refusal_code == refusal


def test_section_stage_caps_intersect_the_administrative_comparison_limit() -> None:
    sections = TextSectionSearchPlan(
        fields=(
            TextSectionRemoval(
                context_id="context-1",
                content_index=0,
                original_text_digest="a" * 64,
                policy_digest="b" * 64,
                section_ids=("section-" + "a" * 64,),
            ),
        ),
        max_coarse_comparisons=1,
        max_fine_comparisons=2,
    )
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION.model_copy(update={"section_search": sections}),
        attempt_cost=COST,
        max_candidate_comparisons=10,
    )
    assert bounds.refusal_code is None
    assert bounds.max_attempts == 43
    assert bounds.candidate_capacity == 3


@pytest.mark.parametrize("remaining", [0, 4, 25, 30])
def test_resume_minimum_is_informational_and_does_not_refuse_new_run_minimum(
    remaining: int,
) -> None:
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION,
        attempt_cost=COST,
        max_candidate_comparisons=4,
        max_provider_calls=1,
        max_wall_time_seconds=1,
        remaining_minimum_attempts=remaining,
    )
    assert bounds.refusal_code is None
    assert bounds.minimum_provider_calls == remaining * 4
    assert bounds.minimum_wall_time_seconds == remaining * 13
    assert bounds.candidate_capacity == 0


def test_budget_refusal_precedes_freshness_and_attainability() -> None:
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION.model_copy(update={"freshness_seconds": 1}),
        attempt_cost=COST,
        max_candidate_comparisons=4,
        max_provider_calls=1,
    )
    assert bounds.refusal_code == "sampling-budget-too-small"


def test_freshness_requires_a_complete_block_including_pauses_and_cleanup() -> None:
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION.model_copy(update={"freshness_seconds": 375}),
        attempt_cost=COST,
        max_candidate_comparisons=4,
        inter_attempt_delay_seconds=2,
    )
    assert bounds.refusal_code == "sampling-freshness-too-small"


def test_unattainable_confirmation_refuses_without_inventing_sample_counts() -> None:
    arm = SamplingArmTarget(required_valid=1, max_attempts=1)
    confirmation = SampledConfirmationPhase(original=arm, candidate=arm, one_sided_confidence=0.95)
    bounds = reduction_budget_bounds(
        calibration=CALIBRATION,
        reduction=REDUCTION.model_copy(update={"confirmation": confirmation, "margin": 0.0}),
        attempt_cost=COST,
        max_candidate_comparisons=4,
    )
    assert bounds.refusal_code == "confirmation-unattainable"


@pytest.mark.parametrize(
    "changes",
    [
        {"max_candidate_comparisons": True},
        {"max_candidate_comparisons": 0},
        {"max_provider_calls": 0},
        {"max_wall_time_seconds": -1},
        {"inter_attempt_delay_seconds": True},
        {"inter_attempt_delay_seconds": 86_401},
        {"remaining_minimum_attempts": -1},
        {"attempt_cost": SamplingAttemptCost(provider_calls=0, runner_seconds=1)},
    ],
)
def test_invalid_administrative_inputs_fail_closed(changes: dict[str, object]) -> None:
    arguments = {
        "calibration": CALIBRATION,
        "reduction": REDUCTION,
        "attempt_cost": COST,
        "max_candidate_comparisons": 4,
        **changes,
    }
    with pytest.raises(ValueError, match=r"budget|delay|attempts|provider cost"):
        reduction_budget_bounds(**arguments)  # type: ignore[arg-type]
