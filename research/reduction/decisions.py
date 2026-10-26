"""Independent finite calculations for screening, confirmation and attempt ceilings."""

# Mathematical invariants and explicit research reports use assertions.
# ruff: noqa: S101

from __future__ import annotations

import json
import math
from fractions import Fraction
from functools import cache
from pathlib import Path

import numpy as np
import reference
import stationary
from scipy.stats import binom

ROOT = Path(__file__).resolve().parent


@cache
def decisions(original: int, candidate: int, confidence: float, margin: float) -> np.ndarray:
    """Enumerate each possible independent-arm count pair once."""
    table = np.empty((original + 1, candidate + 1), dtype=np.int8)
    for x in range(original + 1):
        for y in range(candidate + 1):
            verdict = reference.classify_fixed_sample(
                reference.BinomialSample(y, candidate),
                reference.BinomialSample(x, original),
                required_candidate=candidate,
                required_original=original,
                confidence=confidence,
                margin=margin,
            )
            table[x, y] = {"preserves": 1, "loses": -1, "inconclusive": 0}[verdict.outcome]
    table.flags.writeable = False
    return table


def probability(table: np.ndarray, original_p: float, candidate_p: float) -> float:
    original = binom.pmf(np.arange(table.shape[0]), table.shape[0] - 1, original_p)
    candidate = binom.pmf(np.arange(table.shape[1]), table.shape[1] - 1, candidate_p)
    result = float(original @ table @ candidate)
    assert -1e-12 <= result <= 1 + 1e-12
    return min(1.0, max(0.0, result))


def admission_table(
    original: int, candidate: int, method: str, confidence: float, margin: Fraction
) -> np.ndarray:
    if method == "point":
        x = np.arange(original + 1)[:, None]
        y = np.arange(candidate + 1)[None, :]
        return (
            y * original - x * candidate
        ) * margin.denominator >= -margin.numerator * original * candidate
    table = decisions(original, candidate, confidence, float(margin))
    if method == "preserves":
        return table == 1
    if method == "loss-veto":
        return table != -1
    raise ValueError("unsupported exploratory rule")


def rational_point_probability(
    original: int, candidate: int, original_p: Fraction, candidate_p: Fraction, margin: Fraction
) -> Fraction:
    total = Fraction()
    for x in range(original + 1):
        left = math.comb(original, x) * original_p**x * (1 - original_p) ** (original - x)
        for y in range(candidate + 1):
            if Fraction(y, candidate) - Fraction(x, original) >= -margin:
                right = (
                    math.comb(candidate, y) * candidate_p**y * (1 - candidate_p) ** (candidate - y)
                )
                total += left * right
    return total


def completion(required: int, maximum: int, failure: float) -> float:
    return float(binom.sf(required - 1, maximum, 1 - failure))


def attempt_ceiling(required: int, failure: float, target: float) -> int:
    if type(required) is not int or required <= 0 or not 0 <= failure < 1 or not 0 < target < 1:
        raise ValueError(
            "positive target count, failure below one and probability target inside (0,1) required"
        )
    limit = required
    while completion(required, limit, failure) < target:
        limit += 1
    return limit


def workflow(
    tree: dict,
    *,
    instruction_position: int,
    retained_admission: float,
    removed_admission: float,
    block_completion: float,
    calibration_ready: float,
    confirmation_complete: float,
    confirmation_preserves: float,
) -> dict:
    """Walk frozen decision edges independently of the stationary-study recursion."""
    stack = [("", 1.0)]
    useful = []
    completed = []
    ceiling = []
    incomplete = []
    group = f"g{instruction_position}"
    while stack:
        prefix, weight = stack.pop()
        if prefix in tree["terminals"]:
            terminal = tree["terminals"][prefix]
            if terminal["complete"]:
                completed.append(weight)
                if group in terminal["retained"] and len(terminal["removed"]) >= 2:
                    useful.append(weight)
            else:
                ceiling.append(weight)
            continue
        node = tree["nodes"][prefix]
        admit = removed_admission if group in node["effective"] else retained_admission
        incomplete.append(weight * (1 - block_completion))
        stack.append((prefix + "1", weight * block_completion * admit))
        stack.append((prefix + "0", weight * block_completion * (1 - admit)))
    assert math.isclose(math.fsum(completed + ceiling + incomplete), 1, rel_tol=0, abs_tol=3e-12)
    return {
        "position": instruction_position,
        "useful_probability": calibration_ready
        * math.fsum(useful)
        * confirmation_complete
        * confirmation_preserves,
        "search_complete_given_calibration": math.fsum(completed),
        "search_incomplete_sampling_given_calibration": math.fsum(incomplete),
        "search_incomplete_cap_given_calibration": math.fsum(ceiling),
    }


def rate_pairs(grid: dict) -> list[tuple[Fraction, Fraction]]:
    margin = Fraction(grid["margin"])
    pairs = set()
    for original in map(Fraction, grid["original_rates"]):
        candidates = [original - margin + Fraction(offset) for offset in grid["boundary_offsets"]]
        if grid["include_equal_rates"]:
            candidates.append(original)
        for candidate in candidates:
            if not 0 <= candidate <= 1:
                continue
            pairs.add((original, candidate))
            if grid["include_reflections"]:
                pairs.add((1 - candidate, 1 - original))
    return sorted(pairs)


def confirmation_grid(grid: dict) -> dict:
    pairs = rate_pairs(grid)
    margin = Fraction(grid["margin"])
    rows = []
    for count in grid["samples_per_arm"]:
        table = decisions(count, count, grid["confidence"], float(margin))
        cells = []
        for original, candidate in pairs:
            preserves = probability(table == 1, float(original), float(candidate))
            loses = probability(table == -1, float(original), float(candidate))
            cells.append(
                {
                    "original": str(original),
                    "candidate": str(candidate),
                    "preserves": preserves,
                    "loses": loses,
                }
            )
        maxima = {}
        for name, predicate, outcome in (
            ("false_preservation", lambda d: d < -margin, "preserves"),
            ("boundary_preservation", lambda d: d == -margin, "preserves"),
            ("false_loss", lambda d: d >= -margin, "loses"),
        ):
            eligible = [c for c, (o, p) in zip(cells, pairs, strict=True) if predicate(p - o)]
            worst = max(eligible, key=lambda c: c[outcome])
            maxima[name] = {
                "probability": worst[outcome],
                "original": worst["original"],
                "candidate": worst["candidate"],
            }
        rows.append(
            {
                "samples_per_arm": count,
                "maxima": maxima,
                "equal_90_power": probability(table == 1, 0.9, 0.9),
                "cells": cells,
            }
        )
    return {"pair_count": len(pairs), "rows": rows}


def evaluate(programme: dict | None = None) -> dict:
    if programme is None:
        programme = json.loads((ROOT / "data/decisions-programme.json").read_text())
    settings = programme["workload"]
    margin = Fraction(settings["margin"])
    tree = stationary.frozen_tree()
    if (settings["groups"], settings["comparison_cap"], settings["minimum_removed_groups"]) != (
        4,
        tree["cap"],
        2,
    ):
        raise ValueError(
            "this evaluation requires the declared four-group, six-comparison, half-removal workload"
        )
    conditional = []
    checked_rational = 0
    for screen in programme["conditional_screens"]:
        matrix = admission_table(
            screen["original"],
            screen["candidate"],
            screen["method"],
            screen.get("confidence", 0.95),
            margin,
        )
        for scenario in programme["scenarios"]:
            p0, ph, pr = (Fraction(scenario[k]) for k in ("original", "retained", "removed"))
            retained = probability(matrix, float(p0), float(ph))
            removed = probability(matrix, float(p0), float(pr))
            if screen["method"] == "point":
                for rate, got in ((ph, retained), (pr, removed)):
                    exact = rational_point_probability(
                        screen["original"], screen["candidate"], p0, rate, margin
                    )
                    assert math.isclose(got, float(exact), rel_tol=0, abs_tol=1e-12)
                    checked_rational += 1
            holdout = decisions(
                programme["confirmation_per_arm"],
                programme["confirmation_per_arm"],
                settings["confirmation_confidence"],
                float(margin),
            )
            preserving = probability(holdout == 1, float(p0), float(ph))
            rows = [
                workflow(
                    tree,
                    instruction_position=i,
                    retained_admission=retained,
                    removed_admission=removed,
                    block_completion=1,
                    calibration_ready=1,
                    confirmation_complete=1,
                    confirmation_preserves=preserving,
                )
                for i in settings["instruction_positions"]
            ]
            conditional.append(
                {
                    "screen": screen["name"],
                    "scenario": scenario["name"],
                    "rows": rows,
                    "minimum_useful_probability": min(r["useful_probability"] for r in rows),
                    "maximum_useful_probability": max(r["useful_probability"] for r in rows),
                    "maximum_calls_including_calibration": settings["conditional_calibration_calls"]
                    + settings["comparison_cap"] * (screen["original"] + screen["candidate"])
                    + 2 * programme["confirmation_per_arm"],
                }
            )
    failures = []
    for plan in programme["failure_plans"]:
        calibration, minimum, calibration_max = plan["calibration"]
        no, ao = plan["original"]
        nc, ac = plan["candidate"]
        nh, ah = plan["confirmation"]
        matrix = admission_table(no, nc, "point", 0.95, margin)
        holdout = decisions(nh, nh, settings["confirmation_confidence"], float(margin))
        for scenario in programme["scenarios"]:
            p0, ph, pr = (float(scenario[k]) for k in ("original", "retained", "removed"))
            retained, removed = probability(matrix, p0, ph), probability(matrix, p0, pr)
            preserving = probability(holdout == 1, p0, ph)
            for failure in programme["failure_rates"]:
                ready = completion(calibration, calibration_max, failure) * float(
                    binom.sf(minimum - 1, calibration, p0)
                )
                block = completion(no, ao, failure) * completion(nc, ac, failure)
                confirm = completion(nh, ah, failure) ** 2
                rows = [
                    workflow(
                        tree,
                        instruction_position=i,
                        retained_admission=retained,
                        removed_admission=removed,
                        block_completion=block,
                        calibration_ready=ready,
                        confirmation_complete=confirm,
                        confirmation_preserves=preserving,
                    )
                    for i in settings["instruction_positions"]
                ]
                failures.append(
                    {
                        "plan": plan["name"],
                        "scenario": scenario["name"],
                        "failure_probability": failure,
                        "calibration_ready": ready,
                        "comparison_complete": block,
                        "confirmation_complete": confirm,
                        "minimum_useful_probability": min(r["useful_probability"] for r in rows),
                        "rows": rows,
                    }
                )
    design = programme["ceiling_design"]
    ceilings = [
        {
            "required_valid": n,
            "max_attempts": attempt_ceiling(
                n, design["failure_probability"], design["completion_probability"]
            ),
        }
        for n in (3, 4, 5, 10, 80, 100, 200)
    ]
    for row in ceilings:
        n, a = row["required_valid"], row["max_attempts"]
        assert completion(n, a, design["failure_probability"]) >= design["completion_probability"]
        assert (
            completion(n, a - 1, design["failure_probability"]) < design["completion_probability"]
        )
    return {
        "evaluation": "independent decision validation",
        "plan_budgets": [
            {
                "plan": p["name"],
                "max_attempts": p["calibration"][2]
                + settings["comparison_cap"] * (p["original"][1] + p["candidate"][1])
                + 2 * p["confirmation"][1],
                "max_valid_provider_calls": p["calibration"][0]
                + settings["comparison_cap"] * (p["original"][0] + p["candidate"][0])
                + 2 * p["confirmation"][0],
            }
            for p in programme["failure_plans"]
        ],
        "conditional_workflows": conditional,
        "failure_workflows": failures,
        "ceilings": ceilings,
        "confirmation_grid": confirmation_grid(programme["confirmation_grid"]),
        "rational_admission_checks": checked_rational,
    }


def validate(result: dict, programme: dict | None = None) -> dict[str, int]:
    """Check documented rounded figures and an independent workflow implementation."""
    if programme is None:
        programme = json.loads((ROOT / "data/decisions-programme.json").read_text())
    checks = json.loads((ROOT / "data/decisions-checks.json").read_text())["checks"]
    for check in checks:
        if check["kind"] == "conditional":
            rows = result["conditional_workflows"]
        elif check["kind"] == "failure":
            rows = result["failure_workflows"]
        else:
            rows = result["confirmation_grid"]["rows"]
        matching = [row for row in rows if all(row[k] == v for k, v in check["selector"].items())]
        assert len(matching) == 1, check["selector"]
        row = matching[0]
        if check["kind"] == "grid-maximum":
            got = max(v["probability"] for v in row["maxima"].values())
        elif "position" in check:
            got = row["rows"][check["position"]]["useful_probability"]
        else:
            got = row["minimum_useful_probability"]
        assert abs(got - check["expected"]) <= check["absolute_tolerance"], (check, got)
    screens = {s["name"]: s for s in programme["conditional_screens"]}
    scenarios = {s["name"]: s for s in programme["scenarios"]}
    independent = 0
    for cell in result["conditional_workflows"]:
        screen = screens[cell["screen"]]
        if screen["method"] != "point":
            continue
        scenario = scenarios[cell["scenario"]]
        plan = stationary.reference().Plan(
            "conditional",
            0,
            0,
            0,
            screen["original"],
            screen["original"],
            screen["candidate"],
            screen["candidate"],
            programme["confirmation_per_arm"],
            programme["confirmation_per_arm"],
        )
        for row in cell["rows"]:
            group = f"g{row['position']}"
            other = stationary.run_exact(
                plan=plan,
                cohort_size=1,
                p0=float(scenario["original"]),
                ph=float(scenario["retained"]),
                pr=float(scenario["removed"]),
                instruction_position=row["position"],
                margin=float(programme["workload"]["margin"]),
                confidence=programme["workload"]["confirmation_confidence"],
                terminal_predicate=lambda t, group=group: (
                    t["complete"] and group in t["retained"] and len(t["removed"]) >= 2
                ),
            )
            assert math.isclose(
                row["useful_probability"],
                other["useful_confirmed_probability"],
                rel_tol=0,
                abs_tol=1e-12,
            )
            independent += 1
    return {
        "documented_summary_checks": len(checks),
        "independent_workflow_checks": independent,
        "rational_admission_checks": result["rational_admission_checks"],
        "confirmation_rate_pairs": result["confirmation_grid"]["pair_count"],
    }
