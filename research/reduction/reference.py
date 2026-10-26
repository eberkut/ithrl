"""Frozen numerical and search reference for the reduction studies.

This module has no harness, filesystem or provider execution interface.
Algorithm origins and publication adjustments are recorded in provenance.json.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from math import hypot, isfinite, sqrt
from statistics import NormalDist
from typing import Any, Literal, Protocol

type CaseEvidence = Any

type SearchPhase = Literal["chunk", "final-sweep"]

type SamplingBlockEvidence = Any

type SampledScreenAction = Literal["admit", "retain", "incomplete"]


def content_digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def trial_key(
    phase: SearchPhase,
    base_removals: tuple[str, ...] | frozenset[str],
    requested_removals: tuple[str, ...],
) -> str:
    """Return the stable identity for a deterministic candidate trial."""

    return content_digest(
        {
            "phase": phase,
            "base_removals": sorted(base_removals),
            "requested_removals": list(requested_removals),
        }
    )


def _sampled_screen_action(evidence: SamplingBlockEvidence) -> SampledScreenAction | None:
    if not evidence.eligible:
        return None
    if not evidence.complete:
        return "incomplete"
    original = evidence.original_counts
    candidate = evidence.candidate_counts
    # Exact ratios preserve the admission boundary without a floating-point tolerance.
    difference = Fraction(candidate.matches, candidate.valid) - Fraction(
        original.matches, original.valid
    )
    return "admit" if difference >= -Fraction(evidence.plan.reduction.margin) else "retain"


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


class _Trial(Protocol):
    @property
    def trial_key(self) -> str: ...

    @property
    def effective_removals(self) -> tuple[str, ...]: ...

    @property
    def accepts_removal(self) -> bool: ...

    @property
    def allows_search_continuation(self) -> bool: ...


class _Classifier[Trial: _Trial](Protocol):
    async def __call__(
        self,
        *,
        evidence: CaseEvidence,
        base_removals: frozenset[str],
        requested_removals: Sequence[str],
        phase: SearchPhase,
        prior_trials: deque[Trial],
        completed_trials: Sequence[Trial],
        publish: bool,
    ) -> Trial | None: ...


type _CheckpointPublisher[Trial: _Trial] = Callable[[Iterable[str], Iterable[Trial], bool], object]


@dataclass(frozen=True, slots=True)
class _SearchOutcome[Trial: _Trial]:
    removed: frozenset[str]
    trials: list[Trial]
    complete: bool


@dataclass(slots=True)
class _Search[Trial: _Trial]:
    evidence: CaseEvidence
    removable: list[str]
    classify: _Classifier[Trial]
    prior_trials: deque[Trial]
    publish: bool
    publish_checkpoint: _CheckpointPublisher[Trial]
    trials: list[Trial]

    async def run(self) -> _SearchOutcome[Trial]:
        removed, chunk_complete = await self._run_chunks()
        if not chunk_complete:
            return _SearchOutcome(removed, self.trials, False)
        removed, final_complete = await self._run_final_sweep(removed)
        return _SearchOutcome(removed, self.trials, final_complete)

    async def _run_chunks(self) -> tuple[frozenset[str], bool]:
        removed: frozenset[str] = frozenset()
        granularity = min(2, len(self.removable))
        while self.removable and granularity:
            removed, reduced, complete = await self._try_chunks(removed, granularity)
            if not complete:
                return removed, False
            if reduced:
                granularity = min(max(granularity - 1, 2), len(self.removable))
                continue
            if granularity >= len(self.removable):
                break
            granularity = min(len(self.removable), granularity * 2)
        return removed, True

    async def _try_chunks(
        self, removed: frozenset[str], granularity: int
    ) -> tuple[frozenset[str], bool, bool]:
        for chunk in _partition(self.removable, granularity):
            trial = await self._run_trial(removed, chunk, "chunk")
            if trial is None:
                return removed, False, False
            if trial.accepts_removal:
                removed = frozenset(trial.effective_removals)
                self._remove_effective(removed)
                return removed, True, True
        return removed, False, True

    async def _run_final_sweep(self, removed: frozenset[str]) -> tuple[frozenset[str], bool]:
        restart = True
        while restart:
            restart = False
            for node_id in tuple(self.removable):
                trial = await self._run_trial(removed, (node_id,), "final-sweep")
                if trial is None:
                    return removed, False
                if trial.accepts_removal:
                    removed = frozenset(trial.effective_removals)
                    self._remove_effective(removed)
                    restart = True
                    break
        return removed, True

    async def _run_trial(
        self,
        removed: frozenset[str],
        requested: Sequence[str],
        phase: SearchPhase,
    ) -> Trial | None:
        key = trial_key(phase, removed, tuple(requested))
        # Finer partitions can repeat an unchanged chunk. Keep its frozen decision
        # rather than duplicate the trial or redraw a favorable sampled block.
        previous = next((trial for trial in self.trials if trial.trial_key == key), None)
        if previous is not None:
            return previous if previous.allows_search_continuation else None
        replaying = bool(self.prior_trials)
        trial = await self.classify(
            evidence=self.evidence,
            base_removals=removed,
            requested_removals=requested,
            phase=phase,
            prior_trials=self.prior_trials,
            completed_trials=self.trials,
            publish=self.publish,
        )
        if trial is None:
            return None
        self.trials.append(trial)
        if self.publish and not replaying:
            checkpoint_removed = trial.effective_removals if trial.accepts_removal else removed
            self.publish_checkpoint(checkpoint_removed, self.trials, False)
        return trial if trial.allows_search_continuation else None

    def _remove_effective(self, removed: frozenset[str]) -> None:
        self.removable = [item for item in self.removable if item not in removed]


async def run_search[Trial: _Trial](
    evidence: CaseEvidence,
    *,
    removable: list[str],
    classify: _Classifier[Trial],
    prior_trials: deque[Trial],
    publish: bool,
    publish_checkpoint: _CheckpointPublisher[Trial],
) -> _SearchOutcome[Trial]:
    """Run the same chunk order and restarting final sweep for either decision policy."""

    search = _Search(
        evidence=evidence,
        removable=removable,
        classify=classify,
        prior_trials=prior_trials,
        publish=publish,
        publish_checkpoint=publish_checkpoint,
        trials=[],
    )
    return await search.run()


def _partition(items: Sequence[str], count: int) -> Iterable[tuple[str, ...]]:
    if count < 1:
        raise ValueError("partition count must be at least one")
    size, remainder = divmod(len(items), count)
    start = 0
    for index in range(count):
        width = size + (1 if index < remainder else 0)
        if width:
            yield tuple(items[start : start + width])
        start += width
