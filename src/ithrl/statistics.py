"""Pure fixed-sample inference for independent valid Bernoulli observations.

The uncorrected Wilson hybrid-score interval is Newcombe method 10. ``confidence``
is the nominal coefficient of each one-sided approximate bound: its normal quantile
is ``Phi^-1(confidence)``, so the central interval has coefficient ``2*confidence-1``.
This module assesses neither observation eligibility nor campaign-wide error control.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import hypot, isfinite, sqrt
from statistics import NormalDist


@dataclass(frozen=True)
class BinomialSample:
    """Matches among valid observations; excluded attempts are absent from both counts."""

    matches: int
    valid: int

    def __post_init__(self) -> None:
        _validate_count(self.valid, "valid", positive=False)
        _validate_count(self.matches, "matches", positive=False)
        if self.matches > self.valid:
            raise ValueError("matches must not exceed valid observations")


@dataclass(frozen=True)
class DifferenceInterval:
    """Bounds for candidate match probability minus original match probability."""

    lower: float
    upper: float

    def __post_init__(self) -> None:
        lower = _finite_number(self.lower, "lower bound")
        upper = _finite_number(self.upper, "upper bound")
        if not -1 <= lower <= upper <= 1:
            raise ValueError("difference bounds must be ordered within [-1, 1]")


class FixedSampleOutcome(StrEnum):
    """The four statistical outcomes of one frozen comparison."""

    PRESERVES = "preserves"
    LOSES = "loses"
    INCONCLUSIVE = "inconclusive"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class FixedSampleClassification:
    """A complete comparison's interval, or missing fixed-sample evidence."""

    outcome: FixedSampleOutcome
    interval: DifferenceInterval | None


@dataclass(frozen=True)
class Attainability:
    """Optimistic attainable extrema, without assumptions about power or affordability."""

    preservation_possible: bool
    loss_possible: bool
    best_lower: float
    worst_upper: float


@dataclass(frozen=True)
class EqualRateAnchors:
    """Descriptive zero/all-success intervals; these do not estimate statistical power."""

    zero_success: DifferenceInterval
    all_success: DifferenceInterval


def newcombe_interval(
    candidate: BinomialSample,
    original: BinomialSample,
    *,
    confidence: float = 0.95,
) -> DifferenceInterval:
    """Return the independent-arm Wilson hybrid-score difference interval.

    Both arms must have positive valid counts. The construction uses no continuity
    correction, pooling, or pseudocounts. Exact zero/all-success identities are retained.
    """

    z = _confidence_quantile(confidence)
    _validate_sample(candidate, positive=True)
    _validate_sample(original, positive=True)
    return _newcombe_interval(candidate, original, z)


def classify_fixed_sample(
    candidate: BinomialSample,
    original: BinomialSample,
    *,
    required_candidate: int,
    required_original: int,
    confidence: float,
    margin: float,
) -> FixedSampleClassification:
    """Classify only when both frozen valid-sample targets are exactly complete.

    Callers must separately prove operational eligibility. Partial samples return
    ``incomplete`` without an interval, even when their apparent outcome is decisive.
    Samples exceeding their fixed targets are rejected rather than reclassified.
    """

    z, allowed_margin = _validate_configuration(
        required_candidate, required_original, confidence, margin
    )
    _validate_sample(candidate, positive=False)
    _validate_sample(original, positive=False)
    if candidate.valid > required_candidate or original.valid > required_original:
        raise ValueError("valid observations must not exceed the fixed sample targets")
    if candidate.valid < required_candidate or original.valid < required_original:
        return FixedSampleClassification(FixedSampleOutcome.INCOMPLETE, None)

    interval = _newcombe_interval(candidate, original, z)
    if interval.lower >= -allowed_margin:
        outcome = FixedSampleOutcome.PRESERVES
    elif interval.upper < -allowed_margin:
        outcome = FixedSampleOutcome.LOSES
    else:
        outcome = FixedSampleOutcome.INCONCLUSIVE
    return FixedSampleClassification(outcome, interval)


def optimistic_attainability(
    *,
    required_candidate: int,
    required_original: int,
    confidence: float,
    margin: float,
) -> Attainability:
    """Test whether any complete outcomes can support preservation or loss.

    The maximum lower bound occurs at all candidate matches and zero original
    matches; the minimum upper bound occurs at the opposite outcomes. This is an
    optimistic mathematical screen, not a power calculation or a budget assessment.
    """

    z, allowed_margin = _validate_configuration(
        required_candidate, required_original, confidence, margin
    )
    best = _newcombe_interval(
        BinomialSample(required_candidate, required_candidate),
        BinomialSample(0, required_original),
        z,
    )
    worst = _newcombe_interval(
        BinomialSample(0, required_candidate),
        BinomialSample(required_original, required_original),
        z,
    )
    return Attainability(
        preservation_possible=best.lower >= -allowed_margin,
        loss_possible=worst.upper < -allowed_margin,
        best_lower=best.lower,
        worst_upper=worst.upper,
    )


def equal_rate_anchors(
    *,
    required_candidate: int,
    required_original: int,
    confidence: float,
) -> EqualRateAnchors:
    """Return exact analytic zero/all-success anchors for the specified arm sizes."""

    _validate_count(required_candidate, "required_candidate", positive=True)
    _validate_count(required_original, "required_original", positive=True)
    z = _confidence_quantile(confidence)
    candidate_width = _endpoint_width(required_candidate, z)
    original_width = _endpoint_width(required_original, z)
    return EqualRateAnchors(
        zero_success=DifferenceInterval(-original_width, candidate_width),
        all_success=DifferenceInterval(-candidate_width, original_width),
    )


def _newcombe_interval(
    candidate: BinomialSample, original: BinomialSample, z: float
) -> DifferenceInterval:
    candidate_lower, candidate_upper = _wilson_distances(candidate, z)
    original_lower, original_upper = _wilson_distances(original, z)
    difference = candidate.matches / candidate.valid - original.matches / original.valid
    lower = difference - hypot(candidate_lower, original_upper)
    upper = difference + hypot(candidate_upper, original_lower)
    return DifferenceInterval(max(-1.0, lower), min(1.0, upper))


def _wilson_distances(sample: BinomialSample, z: float) -> tuple[float, float]:
    """Return q-l and u-q, avoiding endpoint cancellation and squared-count overflow."""

    width = _endpoint_width(sample.valid, z)
    if sample.matches == 0:
        return 0.0, width
    if sample.matches == sample.valid:
        return width, 0.0

    proportion = sample.matches / sample.valid
    complement = (sample.valid - sample.matches) / sample.valid
    inverse_n = 1.0 / sample.valid
    scale = z * z * inverse_n
    denominator = 1 + scale
    shift = scale * (proportion - 0.5) / denominator
    half = (
        z
        * hypot(sqrt(proportion * complement) * sqrt(inverse_n), 0.5 * z * inverse_n)
        / denominator
    )
    return (
        min(proportion, max(0.0, half + shift)),
        min(complement, max(0.0, half - shift)),
    )


def _endpoint_width(valid: int, z: float) -> float:
    squared_z = z * z
    return squared_z / (valid + squared_z)


def _validate_sample(sample: BinomialSample, *, positive: bool) -> None:
    if not isinstance(sample, BinomialSample):
        raise ValueError("samples must be BinomialSample values")
    _validate_count(sample.valid, "valid", positive=positive)
    _validate_count(sample.matches, "matches", positive=False)
    if sample.matches > sample.valid:
        raise ValueError("matches must not exceed valid observations")


def _validate_configuration(
    required_candidate: int, required_original: int, confidence: float, margin: float
) -> tuple[float, float]:
    _validate_count(required_candidate, "required_candidate", positive=True)
    _validate_count(required_original, "required_original", positive=True)
    z = _confidence_quantile(confidence)
    allowed_margin = _finite_number(margin, "margin")
    if not 0 <= allowed_margin < 1:
        raise ValueError("margin must satisfy 0 <= margin < 1")
    return z, allowed_margin


def _confidence_quantile(confidence: float) -> float:
    coefficient = _finite_number(confidence, "confidence")
    if not 0.5 < coefficient < 1:
        raise ValueError("confidence must satisfy 0.5 < confidence < 1")
    return NormalDist().inv_cdf(coefficient)


def _validate_count(value: int, name: str, *, positive: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be {qualifier}")
    _finite_number(value, name)


def _finite_number(value: float | int, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} cannot be represented as a finite number") from None
    if not isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number
