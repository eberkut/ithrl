"""Independent binomial-tail checks for fixed one-finding exact confirmation."""

from __future__ import annotations

import builtins
import math
from typing import Any

import pytest

from ithrl.fuzz.contracts import FuzzOutcome
from ithrl.fuzz.statistics import confirm_change, require_fuzz_numerics
from ithrl.statistics import BinomialSample

pytest.importorskip("scipy")


@pytest.mark.parametrize("n", [1, 2, 5, 10, 20, 37])
def test_exact_bounds_satisfy_independent_standard_library_tail_equations(n: int) -> None:
    for x in range(n + 1):
        result = confirm_change(
            BinomialSample(x, n),
            BinomialSample(x, n),
            required_original=n,
            required_mutant=n,
            alpha=0.05,
            minimum_decrease=0.1,
        )
        lower, upper = result.original_lower_bound, result.mutant_upper_bound
        assert lower is not None
        assert upper is not None
        if x == 0:
            assert lower == 0
        else:
            tail = math.fsum(
                math.comb(n, k) * lower**k * (1 - lower) ** (n - k) for k in range(x, n + 1)
            )
            assert tail == pytest.approx(0.025, abs=2e-12)
        if x == n:
            assert upper == 1
        else:
            tail = math.fsum(
                math.comb(n, k) * upper**k * (1 - upper) ** (n - k) for k in range(x + 1)
            )
            assert tail == pytest.approx(0.025, abs=2e-12)
        assert result.outcome is FuzzOutcome.INCONCLUSIVE


def test_reference_endpoints_unequal_arms_and_strict_effect_boundary() -> None:
    result = confirm_change(
        BinomialSample(10, 10),
        BinomialSample(0, 20),
        required_original=10,
        required_mutant=20,
        alpha=0.05,
        minimum_decrease=0.1,
    )
    assert result.original_lower_bound == pytest.approx(0.6915028921812392)
    assert result.mutant_upper_bound == pytest.approx(1 - 0.025 ** (1 / 20))
    assert result.outcome is FuzzOutcome.CONFIRMED_CHANGE
    assert result.lower_bound is not None
    boundary = confirm_change(
        BinomialSample(10, 10),
        BinomialSample(0, 20),
        required_original=10,
        required_mutant=20,
        alpha=0.05,
        minimum_decrease=result.lower_bound,
    )
    assert boundary.outcome is FuzzOutcome.INCONCLUSIVE
    assert (
        confirm_change(
            BinomialSample(10, 10),
            BinomialSample(0, 19),
            required_original=10,
            required_mutant=20,
            alpha=0.05,
            minimum_decrease=0.1,
        ).outcome
        is FuzzOutcome.INCOMPLETE
    )
    with pytest.raises(ValueError, match="exceed"):
        confirm_change(
            BinomialSample(10, 11),
            BinomialSample(0, 20),
            required_original=10,
            required_mutant=20,
            alpha=0.05,
            minimum_decrease=0.1,
        )


def test_enumerated_null_false_declarations_stay_below_campaign_alpha() -> None:
    n, m = 8, 12
    rejected = [
        (x, y)
        for x in range(n + 1)
        for y in range(m + 1)
        if confirm_change(
            BinomialSample(x, n),
            BinomialSample(y, m),
            required_original=n,
            required_mutant=m,
            alpha=0.05,
            minimum_decrease=0.1,
        ).outcome
        is FuzzOutcome.CONFIRMED_CHANGE
    ]
    for p, q in ((0.0, 0.0), (0.1, 0.0), (0.5, 0.4), (0.9, 0.8), (1.0, 1.0)):
        probability = math.fsum(
            math.comb(n, x)
            * p**x
            * (1 - p) ** (n - x)
            * math.comb(m, y)
            * q**y
            * (1 - q) ** (m - y)
            for x, y in rejected
        )
        assert probability <= 0.05


def test_missing_extra_and_unrepresentable_tail_refuse_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def missing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("scipy"):
            raise ModuleNotFoundError("synthetic missing optional dependency")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", missing)
        with pytest.raises(ImportError, match=r"ithrl\[fuzz\]"):
            require_fuzz_numerics()
    with pytest.raises(ValueError, match="representable"):
        confirm_change(
            BinomialSample(1, 1),
            BinomialSample(0, 1),
            required_original=1,
            required_mutant=1,
            alpha=1e-300,
            minimum_decrease=0.1,
        )
