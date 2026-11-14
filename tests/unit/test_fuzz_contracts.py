"""Finite single-finding campaign policy without execution or numerical dependencies."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ithrl.experiment import SampledCalibrationPhase, SamplingArmTarget
from ithrl.fuzz import FuzzCampaignConfig, FuzzConfirmation, FuzzScreening
from ithrl.fuzz.configuration import FuzzConfiguration
from ithrl.fuzz.progress import FuzzCampaignProgress, FuzzTrial
from ithrl.mutation import MAX_MUTATION_TEXT_BYTES, TextAppend, TextMutation
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


def test_configuration_bounds_stress_text_by_utf8_bytes() -> None:
    settings = _campaign().model_dump(exclude={"original"})
    settings["limits"] = {
        "max_provider_calls": 0,
        "max_wall_time_seconds": 60,
        "attempt_timeout_seconds": 1,
    }
    settings["stale_text"] = "é" * (MAX_MUTATION_TEXT_BYTES // 2)
    accepted = FuzzConfiguration.model_validate(settings)
    assert len(accepted.stale_text.encode()) == MAX_MUTATION_TEXT_BYTES
    settings["stale_text"] += "é"
    with pytest.raises(ValidationError, match="UTF-8 byte limit"):
        FuzzConfiguration.model_validate(settings)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("proposal-cap", "frozen proposal cap"),
        ("duplicate-generated", "nonempty and deduplicated"),
        ("empty-generated", "nonempty and deduplicated"),
        ("trial-cap", "frozen work limits"),
        ("repeated-trial", "cannot be sampled twice"),
        ("terminal", "terminal campaign requires its stop reason"),
    ],
)
def test_persisted_campaign_progress_enforces_frozen_work_and_terminal_state(
    fault: str, message: str
) -> None:
    mutation = TextMutation(
        edits=(
            TextAppend(
                context_id="ctx-a", content_index=0, original_text_digest="d" * 64, text="stress"
            ),
        )
    )
    trial = FuzzTrial(role="discovery", mutation=mutation, plan=None)
    progress = FuzzCampaignProgress(
        config=_campaign(max_simplification_trials=1),
        implementation_digest="a" * 64,
        generation_input_digest="b" * 64,
        stale_text="stress",
        generated=(mutation,),
        proposals_examined=1,
    )
    assert FuzzCampaignProgress.model_validate_json(progress.model_dump_json()) == progress
    data = progress.model_dump()
    if fault == "proposal-cap":
        data["proposals_examined"] = 0
    elif fault == "duplicate-generated":
        data.update(generated=(mutation, mutation), proposals_examined=2)
    elif fault == "empty-generated":
        data["generated"] = (TextMutation(),)
    elif fault == "trial-cap":
        data["trials"] = (trial,) * 3
    elif fault == "repeated-trial":
        data["trials"] = (trial, trial)
    else:
        data["stop_reason"] = "no-candidate"
    with pytest.raises(ValidationError, match=message):
        FuzzCampaignProgress.model_validate(data)


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
