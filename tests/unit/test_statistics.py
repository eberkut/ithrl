"""Pure fixed-sample inference contracts, independent of runners and provider state."""

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError
from pathlib import Path
from statistics import NormalDist
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

import ithrl.statistics as statistics_module
from ithrl.statistics import (
    BinomialSample,
    DifferenceInterval,
    FixedSampleOutcome,
    classify_fixed_sample,
    equal_rate_anchors,
    newcombe_interval,
    optimistic_attainability,
)

REFERENCE: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "fixtures/statistics_reference.json").read_text(encoding="utf-8")
)


def test_reference_vectors_record_published_independent_method_provenance() -> None:
    provenance = REFERENCE["provenance"]
    assert provenance["package"] == "statsmodels"
    assert provenance["version"] == "0.15.0"
    assert provenance["function"] == "statsmodels.stats.proportion.confint_proportions_2indep"
    assert provenance["method"] == "newcomb"
    assert provenance["compare"] == "diff"
    assert provenance["alpha"] == "2 * (1 - confidence)"
    assert provenance["arm_order"] == "candidate first, immutable original second"
    assert provenance["source_url"].startswith(
        "https://github.com/statsmodels/statsmodels/blob/v0.15.0/"
    )
    assert len(provenance["source_file_sha256"]) == 64
    assert len(REFERENCE["vectors"]) == 40


@pytest.mark.parametrize(
    "vector", REFERENCE["vectors"], ids=lambda vector: f"{vector['case']}-C{vector['confidence']}"
)
def test_intervals_agree_with_frozen_statsmodels_reference(vector: dict[str, Any]) -> None:
    candidate = BinomialSample(vector["candidate_matches"], vector["candidate_valid"])
    original = BinomialSample(vector["original_matches"], vector["original_valid"])
    interval = newcombe_interval(candidate, original, confidence=vector["confidence"])
    assert interval.lower == pytest.approx(vector["lower"], rel=0, abs=1e-12)
    assert interval.upper == pytest.approx(vector["upper"], rel=0, abs=1e-12)
    for margin in (0.0, 0.1, 0.3):
        if vector["lower"] >= -margin:
            expected = FixedSampleOutcome.PRESERVES
        elif vector["upper"] < -margin:
            expected = FixedSampleOutcome.LOSES
        else:
            expected = FixedSampleOutcome.INCONCLUSIVE
        result = classify_fixed_sample(
            candidate,
            original,
            required_candidate=candidate.valid,
            required_original=original.valid,
            confidence=vector["confidence"],
            margin=margin,
        )
        assert result.outcome is expected
        assert result.interval == interval


@pytest.mark.parametrize(
    ("matches", "valid"),
    [
        (True, 1),
        (0, True),
        (False, 0),
        (0, False),
        (1.0, 2),
        (1, 2.0),
        ("1", 2),
        (1, "2"),
        (None, 2),
        (0, None),
        (-1, 2),
        (0, -1),
        (2, 1),
        (1, 0),
        (0, 10**500),
    ],
)
def test_binomial_sample_rejects_malformed_counts(matches: Any, valid: Any) -> None:
    with pytest.raises(ValueError, match=r"matches|valid"):
        BinomialSample(matches, valid)


def test_samples_are_frozen_and_allow_empty_progress() -> None:
    sample = BinomialSample(0, 0)
    assert (sample.matches, sample.valid) == (0, 0)
    with pytest.raises(FrozenInstanceError):
        cast(Any, sample).matches = 1


@pytest.mark.parametrize(
    ("lower", "upper"),
    [
        (True, 1.0),
        (-1.0, False),
        (math.nan, 1.0),
        (-1.0, math.inf),
        (-math.inf, 1.0),
        (0.2, 0.1),
        (-1.01, 0.0),
        (0.0, 1.01),
        ("0", 1.0),
    ],
)
def test_difference_intervals_reject_nonfinite_unordered_or_out_of_range_bounds(
    lower: Any, upper: Any
) -> None:
    with pytest.raises(ValueError, match="bound"):
        DifferenceInterval(lower, upper)


@pytest.mark.parametrize("empty_candidate", [False, True])
def test_interval_requires_both_nonempty_arms(empty_candidate: bool) -> None:
    empty, nonempty = BinomialSample(0, 0), BinomialSample(1, 1)
    arms = (empty, nonempty) if empty_candidate else (nonempty, empty)
    with pytest.raises(ValueError, match="valid"):
        newcombe_interval(*arms, confidence=0.95)


@pytest.mark.parametrize("confidence", [0.8, 0.95, 0.99])
@pytest.mark.parametrize(("candidate_n", "original_n"), [(1, 1), (5, 20), (50, 10)])
def test_zero_and_all_success_analytic_endpoints(
    confidence: float, candidate_n: int, original_n: int
) -> None:
    z_squared = NormalDist().inv_cdf(confidence) ** 2
    candidate_a = z_squared / (candidate_n + z_squared)
    original_a = z_squared / (original_n + z_squared)

    zero = newcombe_interval(
        BinomialSample(0, candidate_n), BinomialSample(0, original_n), confidence=confidence
    )
    all_success = newcombe_interval(
        BinomialSample(candidate_n, candidate_n),
        BinomialSample(original_n, original_n),
        confidence=confidence,
    )
    assert zero.lower == -original_a
    assert zero.upper == candidate_a
    assert all_success.lower == -candidate_a
    assert all_success.upper == original_a


def test_confidence_is_one_sided_rather_than_central() -> None:
    interval = newcombe_interval(BinomialSample(0, 10), BinomialSample(0, 10), confidence=0.95)
    one_sided_z_squared = NormalDist().inv_cdf(0.95) ** 2
    central_z_squared = NormalDist().inv_cdf(0.975) ** 2
    assert interval.upper == one_sided_z_squared / (10 + one_sided_z_squared)
    assert interval.upper < central_z_squared / (10 + central_z_squared)


@st.composite
def _samples(draw: st.DrawFn) -> BinomialSample:
    valid = draw(st.integers(min_value=1, max_value=500))
    matches = draw(st.integers(min_value=0, max_value=valid))
    return BinomialSample(matches, valid)


@given(candidate=_samples(), original=_samples(), confidence=st.sampled_from([0.8, 0.95, 0.99]))
def test_interval_arm_swap_complement_and_range_identities(
    candidate: BinomialSample, original: BinomialSample, confidence: float
) -> None:
    interval = newcombe_interval(candidate, original, confidence=confidence)
    swapped = newcombe_interval(original, candidate, confidence=confidence)
    complemented = newcombe_interval(
        BinomialSample(candidate.valid - candidate.matches, candidate.valid),
        BinomialSample(original.valid - original.matches, original.valid),
        confidence=confidence,
    )
    assert -1 <= interval.lower <= interval.upper <= 1
    assert swapped.lower == -interval.upper
    assert swapped.upper == -interval.lower
    assert complemented.lower == pytest.approx(-interval.upper, rel=0, abs=1e-12)
    assert complemented.upper == pytest.approx(-interval.lower, rel=0, abs=1e-12)


@pytest.mark.parametrize(
    ("candidate", "original"),
    [
        (BinomialSample(0, 0), BinomialSample(0, 0)),
        (BinomialSample(4, 4), BinomialSample(0, 0)),
        (BinomialSample(0, 0), BinomialSample(4, 4)),
        (BinomialSample(0, 3), BinomialSample(4, 4)),
        (BinomialSample(4, 4), BinomialSample(0, 3)),
    ],
)
def test_incomplete_arms_never_construct_interval_or_claim_loss(
    monkeypatch: pytest.MonkeyPatch, candidate: BinomialSample, original: BinomialSample
) -> None:
    def forbidden_interval(*args: Any, **kwargs: Any) -> None:
        pytest.fail("incomplete arms must not construct a fixed-sample interval")

    monkeypatch.setattr(statistics_module, "_newcombe_interval", forbidden_interval)
    result = classify_fixed_sample(
        candidate,
        original,
        required_candidate=4,
        required_original=4,
        confidence=0.95,
        margin=0.1,
    )
    assert result.outcome is FixedSampleOutcome.INCOMPLETE
    assert result.interval is None


@pytest.mark.parametrize("candidate_oversampled", [False, True])
def test_fixed_targets_refuse_oversampling(candidate_oversampled: bool) -> None:
    arms = (BinomialSample(3, 5), BinomialSample(3, 4))
    if not candidate_oversampled:
        arms = arms[::-1]
    with pytest.raises(ValueError, match="fixed sample targets"):
        classify_fixed_sample(
            *arms, required_candidate=4, required_original=4, confidence=0.95, margin=0.1
        )


@pytest.mark.parametrize("target", [True, False, 0, -1, 1.0, "4", None, 10**500])
@pytest.mark.parametrize("malformed_candidate", [False, True])
def test_fixed_targets_require_positive_integers(target: Any, malformed_candidate: bool) -> None:
    candidate_target, original_target = (target, 4) if malformed_candidate else (4, target)
    with pytest.raises(ValueError, match=r"required_candidate|required_original"):
        classify_fixed_sample(
            BinomialSample(0, 0),
            BinomialSample(0, 0),
            required_candidate=candidate_target,
            required_original=original_target,
            confidence=0.95,
            margin=0.1,
        )
    with pytest.raises(ValueError, match=r"required_candidate|required_original"):
        optimistic_attainability(
            required_candidate=candidate_target,
            required_original=original_target,
            confidence=0.95,
            margin=0.1,
        )
    with pytest.raises(ValueError, match=r"required_candidate|required_original"):
        equal_rate_anchors(
            required_candidate=candidate_target,
            required_original=original_target,
            confidence=0.95,
        )


@pytest.mark.parametrize(
    "confidence", [True, False, 0.5, 1.0, -1.0, math.nan, math.inf, -math.inf, "0.95", None]
)
def test_confidence_validation_applies_even_to_incomplete_samples(confidence: Any) -> None:
    with pytest.raises(ValueError, match="confidence"):
        newcombe_interval(BinomialSample(2, 4), BinomialSample(2, 4), confidence=confidence)
    with pytest.raises(ValueError, match="confidence"):
        classify_fixed_sample(
            BinomialSample(0, 0),
            BinomialSample(0, 0),
            required_candidate=4,
            required_original=4,
            confidence=confidence,
            margin=0.1,
        )
    with pytest.raises(ValueError, match="confidence"):
        optimistic_attainability(
            required_candidate=4, required_original=4, confidence=confidence, margin=0.1
        )
    with pytest.raises(ValueError, match="confidence"):
        equal_rate_anchors(required_candidate=4, required_original=4, confidence=confidence)


@pytest.mark.parametrize(
    "margin", [True, False, -0.01, 1.0, math.nan, math.inf, -math.inf, "0.1", None]
)
def test_margin_validation_applies_even_to_incomplete_samples(margin: Any) -> None:
    with pytest.raises(ValueError, match="margin"):
        classify_fixed_sample(
            BinomialSample(0, 0),
            BinomialSample(0, 0),
            required_candidate=4,
            required_original=4,
            confidence=0.95,
            margin=margin,
        )
    with pytest.raises(ValueError, match="margin"):
        optimistic_attainability(
            required_candidate=4, required_original=4, confidence=0.95, margin=margin
        )


@pytest.mark.parametrize(
    ("candidate", "original", "margin", "outcome"),
    [
        (BinomialSample(50, 50), BinomialSample(0, 50), 0.0, FixedSampleOutcome.PRESERVES),
        (BinomialSample(0, 50), BinomialSample(50, 50), 0.1, FixedSampleOutcome.LOSES),
        (BinomialSample(25, 50), BinomialSample(25, 50), 0.0, FixedSampleOutcome.INCONCLUSIVE),
    ],
)
def test_complete_fixed_samples_have_expected_outcomes(
    candidate: BinomialSample, original: BinomialSample, margin: float, outcome: FixedSampleOutcome
) -> None:
    result = classify_fixed_sample(
        candidate,
        original,
        required_candidate=50,
        required_original=50,
        confidence=0.95,
        margin=margin,
    )
    assert result.outcome is outcome
    assert result.interval == newcombe_interval(candidate, original, confidence=0.95)


def test_lower_boundary_is_inclusive_without_tolerance() -> None:
    candidate, original = BinomialSample(20, 20), BinomialSample(20, 20)
    boundary_margin = -newcombe_interval(candidate, original, confidence=0.95).lower
    for margin, expected in [
        (math.nextafter(boundary_margin, 0), FixedSampleOutcome.INCONCLUSIVE),
        (boundary_margin, FixedSampleOutcome.PRESERVES),
        (math.nextafter(boundary_margin, 1), FixedSampleOutcome.PRESERVES),
    ]:
        result = classify_fixed_sample(
            candidate,
            original,
            required_candidate=20,
            required_original=20,
            confidence=0.95,
            margin=margin,
        )
        assert result.outcome is expected


def test_upper_boundary_is_strict_without_tolerance() -> None:
    candidate, original = BinomialSample(0, 20), BinomialSample(20, 20)
    boundary_margin = -newcombe_interval(candidate, original, confidence=0.95).upper
    for margin, expected in [
        (math.nextafter(boundary_margin, 0), FixedSampleOutcome.LOSES),
        (boundary_margin, FixedSampleOutcome.INCONCLUSIVE),
        (math.nextafter(boundary_margin, 1), FixedSampleOutcome.INCONCLUSIVE),
    ]:
        result = classify_fixed_sample(
            candidate,
            original,
            required_candidate=20,
            required_original=20,
            confidence=0.95,
            margin=margin,
        )
        assert result.outcome is expected


@pytest.mark.parametrize(("candidate_n", "original_n"), [(1, 1), (2, 3), (5, 10), (10, 5)])
@pytest.mark.parametrize("confidence", [0.8, 0.95, 0.99])
@pytest.mark.parametrize("margin", [0.0, 0.1, 0.9])
def test_optimistic_helpers_describe_attainability_over_every_count_outcome(
    candidate_n: int, original_n: int, confidence: float, margin: float
) -> None:
    attainable = optimistic_attainability(
        required_candidate=candidate_n,
        required_original=original_n,
        confidence=confidence,
        margin=margin,
    )
    intervals = [
        newcombe_interval(
            BinomialSample(candidate_x, candidate_n),
            BinomialSample(original_x, original_n),
            confidence=confidence,
        )
        for candidate_x in range(candidate_n + 1)
        for original_x in range(original_n + 1)
    ]
    assert attainable.best_lower == max(interval.lower for interval in intervals)
    assert attainable.worst_upper == min(interval.upper for interval in intervals)
    assert attainable.preservation_possible is any(
        interval.lower >= -margin for interval in intervals
    )
    assert attainable.loss_possible is any(interval.upper < -margin for interval in intervals)


def test_equal_rate_anchors_are_endpoint_diagnostics_and_not_power() -> None:
    anchors = equal_rate_anchors(required_candidate=5, required_original=20, confidence=0.95)
    assert anchors.zero_success == newcombe_interval(
        BinomialSample(0, 5), BinomialSample(0, 20), confidence=0.95
    )
    assert anchors.all_success == newcombe_interval(
        BinomialSample(5, 5), BinomialSample(20, 20), confidence=0.95
    )
