"""Map shared exact-binomial change inference to fuzzing outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from ithrl.fuzz.contracts import FuzzOutcome
from ithrl.statistics import (
    BinomialSample,
    confirm_exact_change,
    require_exact_numerics,
    validate_exact_change_parameters,
)


def require_fuzz_numerics() -> None:
    """Refuse a missing optional dependency before any sampling resources are used."""

    try:
        require_exact_numerics()
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
    validate_exact_change_parameters(alpha=alpha, minimum_effect=minimum_decrease)


def confirm_change(
    original: BinomialSample,
    mutant: BinomialSample,
    *,
    required_original: int,
    required_mutant: int,
    alpha: float,
    minimum_decrease: float,
) -> ChangeConfirmation:
    """Confirm a decrease from the original to the mutant's match probability."""

    validate_change_parameters(alpha=alpha, minimum_decrease=minimum_decrease)
    require_fuzz_numerics()
    result = confirm_exact_change(
        original,
        mutant,
        required_positive=required_original,
        required_negative=required_mutant,
        alpha=alpha,
        minimum_effect=minimum_decrease,
    )
    if result.confirmed is None:
        outcome = FuzzOutcome.INCOMPLETE
    elif result.confirmed:
        outcome = FuzzOutcome.CONFIRMED_CHANGE
    else:
        outcome = FuzzOutcome.INCONCLUSIVE
    return ChangeConfirmation(
        outcome,
        result.difference,
        result.lower_bound,
        result.positive_lower_bound,
        result.negative_upper_bound,
    )
