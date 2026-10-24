"""Finite single-finding campaign policy without execution or numerical dependencies."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.experiment import SampledCalibrationPhase, SamplingArmTarget
from ithrl.fuzz import FuzzCampaignConfig, FuzzConfirmation, FuzzScreening
from ithrl.mutation import TextAppend, TextMutation
from ithrl.runner import Candidate


def _campaign(**changes: Any) -> FuzzCampaignConfig:
    return FuzzCampaignConfig.model_validate(
        {
            "original": Candidate(
                case_digest="a" * 64,
                execution_plan_digest="b" * 64,
                retained_context_ids=("ctx-a",),
                removed_context_ids=(),
                materialization="source-backed",
            ),
            "seed": 2026102301,
            "mutators": ("duplicate-text", "append-stale-text"),
            "max_candidates": 4,
            "max_simplification_trials": 10,
            "calibration": SampledCalibrationPhase(
                required_valid=3, minimum_matches=2, max_attempts=7
            ),
            "screening": FuzzScreening(
                original=SamplingArmTarget(required_valid=8, max_attempts=18),
                mutant=SamplingArmTarget(required_valid=8, max_attempts=18),
                minimum_decrease=0.4,
            ),
            "confirmation": FuzzConfirmation(
                original=SamplingArmTarget(required_valid=20, max_attempts=36),
                mutant=SamplingArmTarget(required_valid=20, max_attempts=36),
                alpha=0.05,
            ),
            "minimum_decrease": 0.1,
            "eligibility_policy": "safe-provider-overload",
            "freshness_seconds": 86400,
            **changes,
        }
    )


def test_frozen_study_policy_round_trips_and_changes_identity_with_decision_inputs() -> None:
    campaign = _campaign()
    assert FuzzCampaignConfig.model_validate_json(campaign.model_dump_json()) == campaign
    for changes in (
        {"seed": 2},
        {"mutators": ("append-stale-text", "duplicate-text")},
        {"max_candidates": 3},
        {"max_simplification_trials": 9},
        {"minimum_decrease": 0.2},
        {"freshness_seconds": 3600},
        {"eligibility_policy": "independent-safe-preaccept-only"},
        {
            "confirmation": FuzzConfirmation(
                original=SamplingArmTarget(required_valid=40, max_attempts=66),
                mutant=SamplingArmTarget(required_valid=40, max_attempts=66),
                alpha=0.01,
            )
        },
    ):
        assert _campaign(**changes).digest != campaign.digest


@pytest.mark.parametrize(
    "changes",
    [
        {"objective": "increase-target-rate"},
        {"minimum_decrease": -0.1},
        {"minimum_decrease": 1.0},
        {"minimum_decrease": float("nan")},
        {"minimum_decrease": float("inf")},
        {"max_candidates": 0},
        {"max_candidates": True},
        {"max_candidates": 10_001},
        {"max_simplification_trials": 0},
        {"max_findings": 2},
        {"freshness_seconds": 0},
        {"mutators": ()},
        {"mutators": ("duplicate-text", "duplicate-text")},
        {"mutators": ("arbitrary-json-patch",)},
        {"estimand": "unconditional"},
        {"seed": -1},
        {"unknown": "value"},
    ],
)
def test_unsupported_or_unbounded_campaign_policy_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _campaign(**changes)


def test_a_reduced_or_mutated_candidate_cannot_become_the_campaign_original() -> None:
    original = _campaign().original
    for changes in (
        {"removed_context_ids": ("ctx-other",)},
        {
            "mutation": TextMutation(
                edits=(
                    TextAppend(
                        context_id="ctx-a",
                        content_index=0,
                        original_text_digest="d" * 64,
                        text="append",
                    ),
                )
            )
        },
    ):
        with pytest.raises(ValidationError, match="unchanged original"):
            _campaign(original=Candidate.model_validate({**original.model_dump(), **changes}))


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, float("nan"), float("inf"), True])
def test_confirmation_error_probability_is_explicit_and_finite(alpha: Any) -> None:
    with pytest.raises(ValidationError):
        FuzzConfirmation(
            original=SamplingArmTarget(required_valid=20, max_attempts=36),
            mutant=SamplingArmTarget(required_valid=20, max_attempts=36),
            alpha=alpha,
        )


def test_campaign_cannot_silently_install_sampling_defaults() -> None:
    data = _campaign().model_dump()
    for field in ("screening", "confirmation", "minimum_decrease", "eligibility_policy", "seed"):
        with pytest.raises(ValidationError):
            FuzzCampaignConfig.model_validate(
                {key: value for key, value in data.items() if key != field}
            )
