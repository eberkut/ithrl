"""Stationary finite-sum follow-up with lazy sequential candidates.

This is numerical enumeration, not Monte Carlo or a production policy. Bernoulli
observations are stationary and independent within each arm; candidates are
independent conditional on their cohort's shared baseline count. New cohorts,
calibration and final confirmation are independent. q>0, drift, expiry and resume
are explicitly unsupported. No provider calls or files are written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from fractions import Fraction
from functools import cache
from math import fsum
from pathlib import Path

import numpy as np
from reference import BinomialSample, classify_fixed_sample
from scipy.stats import binom


def reference():
    import study

    return study


def frozen_tree() -> dict:
    return json.loads((Path(__file__).parent / "data/search-tree.json").read_text())


def candidate_rate(removed, *, p0: float, ph: float, pr: float, instruction_position: int) -> float:
    if not removed:
        return p0
    instruction = reference().GROUPS[instruction_position]
    return pr if instruction in removed else ph


def screen_acceptance(plan, p_candidate: float, *, margin: float) -> np.ndarray:
    """P(admit | fixed original count b), retaining the shared-baseline correlation."""
    candidate = np.arange(plan.candidate_n + 1, dtype=np.int64)
    original = np.arange(plan.original_n + 1, dtype=np.int64)
    allowed = Fraction(str(margin))
    cross = candidate[None, :] * plan.original_n - original[:, None] * plan.candidate_n
    admit = cross * allowed.denominator >= -allowed.numerator * plan.original_n * plan.candidate_n
    candidate_pmf = binom.pmf(candidate, plan.candidate_n, p_candidate)
    probability = admit @ candidate_pmf
    # Bound and remove only narrow float64 summation noise before forming the
    # retention probability. This is a defensive check, not a numerical certificate.
    tolerance = 1e-12
    if (
        not np.isfinite(probability).all()
        or np.any(probability < -tolerance)
        or np.any(probability > 1 + tolerance)
    ):
        raise ArithmeticError("finite admission probabilities outside [0,1] tolerance")
    return np.clip(probability, 0.0, 1.0)


def cohort_branches(
    tree: dict,
    prefix: str,
    plan,
    size: int,
    *,
    p0: float,
    ph: float,
    pr: float,
    instruction_position: int,
    margin: float,
) -> dict[str, float]:
    """Integrate first acceptance/all retention jointly over one baseline count.

    Later candidates are sampled only on preceding retention. Multiplying marginal
    screen probabilities would discard baseline correlation and is not used.
    """
    members = reference().cohort(tree, prefix, size)
    surviving = binom.pmf(np.arange(plan.original_n + 1), plan.original_n, p0)
    branches = {}
    for member in members:
        rate = candidate_rate(
            tree["nodes"][member]["effective"],
            p0=p0,
            ph=ph,
            pr=pr,
            instruction_position=instruction_position,
        )
        admit = screen_acceptance(plan, rate, margin=margin)
        branches[member + "1"] = float(np.dot(surviving, admit))
        surviving = surviving * (1 - admit)
    branches[members[-1] + "0"] = float(surviving.sum())
    if not np.isclose(fsum(branches.values()), 1.0, rtol=0.0, atol=2e-13):
        raise ArithmeticError("finite cohort branch probabilities do not conserve mass")
    return branches


def lazy_path_cost(tree: dict, terminal: str, plan, size: int) -> dict:
    """Same applied decisions as the reference, with no unused candidate sampling."""
    prefix = ""
    originals = candidates = 0
    while prefix in tree["nodes"]:
        originals += 1
        for member in reference().cohort(tree, prefix, size):
            candidates += 1
            bit = terminal[len(member)]
            prefix = member + bit
            if bit == "1":
                break
    search_calls = originals * plan.original_n + candidates * plan.candidate_n
    search_attempt_budget = originals * plan.original_a + candidates * plan.candidate_a
    complete = tree["terminals"][terminal]["complete"]
    return {
        "original_arms": originals,
        "candidate_arms": candidates,
        "comparisons": candidates,
        "search_calls_q0": search_calls,
        "search_attempts_q0": search_calls,
        "search_attempt_budget": search_attempt_budget,
        "workflow_calls_q0": plan.calibration_n
        + search_calls
        + (2 * plan.confirmation_n if complete else 0),
        "workflow_attempts_q0": plan.calibration_n
        + search_calls
        + (2 * plan.confirmation_n if complete else 0),
        "required_workflow_attempt_budget": plan.calibration_a
        + search_attempt_budget
        + 2 * plan.confirmation_a,
        "unused_candidate_calls": 0,
    }


@cache
def confirmation_table(n: int, confidence: float, margin: float) -> np.ndarray:
    codes = {"preserves": 0, "loses": 1, "inconclusive": 2}
    table = np.empty((n + 1, n + 1), dtype=np.uint8)
    for original in range(n + 1):
        for candidate in range(n + 1):
            result = classify_fixed_sample(
                BinomialSample(candidate, n),
                BinomialSample(original, n),
                required_candidate=n,
                required_original=n,
                confidence=confidence,
                margin=margin,
            )
            table[original, candidate] = codes[result.outcome]
    table.flags.writeable = False
    return table


def fresh_confirmation(
    plan, *, p0: float, p_candidate: float, confidence: float, margin: float
) -> dict[str, float]:
    table = confirmation_table(plan.confirmation_n, confidence, margin)
    counts = np.arange(plan.confirmation_n + 1)
    joint = np.outer(
        binom.pmf(counts, plan.confirmation_n, p0),
        binom.pmf(counts, plan.confirmation_n, p_candidate),
    )
    return {
        name: float(joint[table == code].sum())
        for code, name in enumerate(("preserves", "loses", "inconclusive"))
    }


def run_exact(
    *,
    plan,
    cohort_size: int,
    p0: float,
    ph: float,
    pr: float,
    instruction_position: int,
    tree: dict | None = None,
    margin: float = 0.2,
    confidence: float = 0.95,
    q: float = 0.0,
    group_bytes: Mapping[str, int] | None = None,
    terminal_predicate: Callable[[dict], bool] | None = None,
) -> dict:
    """Return exact finite workflow sums, explicit statuses and path accounting.

    The caller supplies any byte sizes and useful-result predicate. Calibration
    costs occur even when calibration fails. Confirmation occurs only on a complete
    search and uses two fresh arms; no neighbor reselection or observation reuse.
    All terminal paths remain in the report, including zero-probability paths.
    """
    if q != 0:
        raise ValueError("q>0 completion/attempt models are not implemented in this finite study")
    if cohort_size not in (1, 2, 4) or instruction_position not in range(4):
        raise ValueError("supported cohorts are 1/2/4 and instruction positions are 0..3")
    if any(not 0 <= p <= 1 for p in (p0, ph, pr)) or not 0 <= margin < 1:
        raise ValueError("rates/margin outside supported probability bounds")
    tree = frozen_tree() if tree is None else tree
    if group_bytes is not None and (
        set(group_bytes) != set(reference().GROUPS)
        or any(type(v) is not int or v < 0 for v in group_bytes.values())
    ):
        raise ValueError("group_bytes must provide nonnegative integer byte sizes for all groups")

    @cache
    def terminal_distribution(prefix: str) -> dict[str, float]:
        if prefix in tree["terminals"]:
            return {prefix: 1.0}
        result = {}
        branches = cohort_branches(
            tree,
            prefix,
            plan,
            cohort_size,
            p0=p0,
            ph=ph,
            pr=pr,
            instruction_position=instruction_position,
            margin=margin,
        )
        for child, probability in branches.items():
            for terminal, conditional in terminal_distribution(child).items():
                result[terminal] = result.get(terminal, 0.0) + probability * conditional
        return result

    conditional = terminal_distribution("")
    if not np.isclose(fsum(conditional.values()), 1.0, rtol=0.0, atol=2e-12):
        raise ArithmeticError("terminal finite sums do not conserve mass")
    calibration_pass = float(binom.sf(plan.calibration_min - 1, plan.calibration_n, p0))
    outcomes = {
        "calibration_failed": 1 - calibration_pass,
        "search_complete": 0.0,
        "search_incomplete": 0.0,
        "preserves": 0.0,
        "loses": 0.0,
        "inconclusive": 0.0,
    }
    expected = {
        "calibration_calls": float(plan.calibration_n),
        "calibration_attempts": float(plan.calibration_n),
        "search_calls": 0.0,
        "search_attempts": 0.0,
        "confirmation_calls": 0.0,
        "confirmation_attempts": 0.0,
        "original_arms": 0.0,
        "candidate_arms": 0.0,
        "comparisons": 0.0,
        "unused_candidate_calls": 0.0,
    }
    terminals = {}
    useful_confirmed = 0.0 if terminal_predicate is not None else None
    confirmations = {}
    for prefix, facts in tree["terminals"].items():
        probability = calibration_pass * conditional.get(prefix, 0.0)
        cost = lazy_path_cost(tree, prefix, plan, cohort_size)
        record = dict(
            facts,
            conditional_on_calibration_probability=conditional.get(prefix, 0.0),
            workflow_probability=probability,
            costs=cost,
        )
        if group_bytes is not None:
            record["removed_bytes"] = sum(group_bytes[g] for g in facts["removed"])
        for key in ("original_arms", "candidate_arms", "comparisons"):
            expected[key] += probability * cost[key]
        expected["search_calls"] += probability * cost["search_calls_q0"]
        expected["search_attempts"] += probability * cost["search_attempts_q0"]
        if not facts["complete"]:
            outcomes["search_incomplete"] += probability
            record["fresh_confirmation"] = None
        else:
            outcomes["search_complete"] += probability
            rate = candidate_rate(
                facts["removed"], p0=p0, ph=ph, pr=pr, instruction_position=instruction_position
            )
            if rate not in confirmations:
                confirmations[rate] = fresh_confirmation(
                    plan, p0=p0, p_candidate=rate, confidence=confidence, margin=margin
                )
            record["fresh_confirmation"] = confirmations[rate]
            for outcome, conditional_probability in confirmations[rate].items():
                outcomes[outcome] += probability * conditional_probability
            expected["confirmation_calls"] += probability * 2 * plan.confirmation_n
            expected["confirmation_attempts"] += probability * 2 * plan.confirmation_n
            if terminal_predicate is not None and terminal_predicate(record):
                useful_confirmed += probability * confirmations[rate]["preserves"]
        terminals[prefix] = record
    expected["total_calls"] = (
        expected["calibration_calls"] + expected["search_calls"] + expected["confirmation_calls"]
    )
    expected["total_attempts"] = (
        expected["calibration_attempts"]
        + expected["search_attempts"]
        + expected["confirmation_attempts"]
    )
    maxima = {
        key: max(t["costs"][key] for t in terminals.values())
        for key in (
            "search_calls_q0",
            "search_attempt_budget",
            "workflow_calls_q0",
            "required_workflow_attempt_budget",
            "original_arms",
            "candidate_arms",
            "comparisons",
        )
    }
    final_mass = (
        outcomes["calibration_failed"]
        + outcomes["search_incomplete"]
        + sum(outcomes[k] for k in ("preserves", "loses", "inconclusive"))
    )
    if not np.isclose(final_mass, 1.0, rtol=0.0, atol=3e-12):
        raise ArithmeticError("final finite-sum statuses do not conserve mass")
    return {
        "cohort_size": cohort_size,
        "plan": plan.name,
        "rates": {"p0": p0, "ph": ph, "pr": pr},
        "instruction_position": instruction_position,
        "calibration_pass_probability": calibration_pass,
        "statuses": outcomes,
        "expected": expected,
        "path_maxima": maxima,
        "terminals": terminals,
        "useful_confirmed_probability": useful_confirmed,
        "assumptions": [
            "Stationary independent Bernoulli observations within each arm.",
            "Candidate screens share exactly one fresh original count within each bounded sibling cohort.",
            "Candidate sampling stops at first acceptance; unused siblings have zero calls.",
            "Calibration, distinct cohorts and fresh final confirmation use independent observations.",
            "q=0 only: no failure, expiry, drift, interrupted work or provider continuation model.",
            "Finite sums use float64 arithmetic; no Monte Carlo or confidence extrapolation.",
        ],
    }
