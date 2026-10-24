"""Numerical study reference; independent of the released inference code."""

from __future__ import annotations

import numpy as np
from scipy.special import betaincinv
from scipy.stats import binom


def lower_bounds(n: int, tail: float) -> np.ndarray:
    k = np.arange(n + 1)
    result = np.zeros(n + 1)
    result[1:] = betaincinv(k[1:], n - k[1:] + 1, tail)
    return result


def upper_bounds(n: int, tail: float) -> np.ndarray:
    k = np.arange(n + 1)
    result = np.ones(n + 1)
    result[:-1] = betaincinv(k[:-1] + 1, n - k[:-1], 1 - tail)
    return result


def rejection_cells(n_original: int, n_mutant: int, alpha: float, delta: float) -> np.ndarray:
    return (
        lower_bounds(n_original, alpha / 2)[:, None] - upper_bounds(n_mutant, alpha / 2)[None, :]
    ) > delta


def rejection_probability(
    n_original: int, n_mutant: int, p_original: float, p_mutant: float, alpha: float, delta: float
) -> float:
    cells = rejection_cells(n_original, n_mutant, alpha, delta)
    original = binom.pmf(np.arange(n_original + 1), n_original, p_original)
    mutant = binom.pmf(np.arange(n_mutant + 1), n_mutant, p_mutant)
    return float(original @ cells @ mutant)


def attempts_limit(n: int) -> int:
    return n + (n + 1) // 2 + 6
