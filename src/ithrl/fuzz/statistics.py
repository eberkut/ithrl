"""Fixed independent-binomial confirmation of a declared target-rate decrease.

Each one-sided exact bound has tail error alpha/2. Their union controls a false
declaration for one independently selected finding under independent stationary
valid observations. Execution reliability is outside these Bernoulli counts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ithrl.fuzz.contracts import FuzzOutcome
from ithrl.statistics import BinomialSample


def require_fuzz_numerics() -> None:
    """Refuse a missing optional dependency before any sampling resources are used."""

    try:
        from scipy.special import betaincinv  # noqa: F401
    except ImportError as error:
        raise ImportError("sampled context fuzzing requires the 'ithrl[fuzz]' extra") from error


@dataclass(frozen=True, slots=True)
class ChangeConfirmation:
    outcome: FuzzOutcome
    difference: float | None
    lower_bound: float | None
    original_lower_bound: float | None
    mutant_upper_bound: float | None


def validate_change_parameters(*, alpha: float, minimum_decrease: float) -> None:
    for value, name in ((alpha, "alpha"), (minimum_decrease, "minimum decrease")):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} must be a finite number")
    if not 0 < alpha < 1 or alpha / 2 == 0 or 1 - alpha / 2 == 1:
        raise ValueError("alpha requires representable one-sided tail probabilities")
    if not 0 <= minimum_decrease < 1:
        raise ValueError("minimum decrease must be within [0, 1)")


def confirm_change(
    original: BinomialSample,
    mutant: BinomialSample,
    *,
    required_original: int,
    required_mutant: int,
    alpha: float,
    minimum_decrease: float,
) -> ChangeConfirmation:
    """Confirm only exact fixed samples; callers must establish eligibility/freshness."""

    validate_change_parameters(alpha=alpha, minimum_decrease=minimum_decrease)
    require_fuzz_numerics()
    for sample, required in ((original, required_original), (mutant, required_mutant)):
        if type(required) is not int or required < 1:
            raise ValueError("confirmation requires positive fixed valid-sample targets")
        if sample.valid > required:
            raise ValueError("confirmation samples exceed their frozen target")
    if original.valid != required_original or mutant.valid != required_mutant:
        return ChangeConfirmation(FuzzOutcome.INCOMPLETE, None, None, None, None)
    from scipy.special import betaincinv

    gamma = alpha / 2
    lower = (
        0.0
        if original.matches == 0
        else float(betaincinv(original.matches, original.valid - original.matches + 1, gamma))
    )
    upper = (
        1.0
        if mutant.matches == mutant.valid
        else float(betaincinv(mutant.matches + 1, mutant.valid - mutant.matches, 1 - gamma))
    )
    if not (math.isfinite(lower) and math.isfinite(upper) and 0 <= lower <= 1 and 0 <= upper <= 1):
        raise ValueError("exact-binomial computation returned invalid probability bounds")
    bound = lower - upper
    return ChangeConfirmation(
        FuzzOutcome.CONFIRMED_CHANGE if bound > minimum_decrease else FuzzOutcome.INCONCLUSIVE,
        original.matches / original.valid - mutant.matches / mutant.valid,
        bound,
        lower,
        upper,
    )
