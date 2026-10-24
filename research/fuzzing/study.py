"""Reproducible fuzzing feasibility programme, using synthetic Bernoulli outcomes only.

This is an algorithm reference for Step 1, not a native runner or production campaign.
Every sampling block pays fresh original and mutant observations. No confirmation data
can affect discovery or intervention simplification. No network or subprocess is used.
"""

# Assertions are intentional scientific checks; output is aggregate-only.
# ruff: noqa: S101, T201, PT018

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import platform
import time
from pathlib import Path

import numpy as np
import scipy
from method import (
    attempts_limit,
    lower_bounds,
    rejection_cells,
    rejection_probability,
    upper_bounds,
)
from scipy.special import betaincinv
from scipy.stats import binomtest

ROOT = Path(__file__).resolve().parent
PROGRAMME = json.loads((ROOT / "programme.json").read_text())
REPETITIONS = PROGRAMME["qualification"]["repetitions_per_cell"]
SIZES = np.asarray(PROGRAMME["mutation"]["paragraph_sizes"])
FAMILY_SIZE = 27 * 4 * (4 * 4 + 6)
POWER_TAIL = 0.01 / FAMILY_SIZE
WORKFLOWS = PROGRAMME["workflows"]


OUTPUT_ROOT = ROOT / "outputs"
FROZEN_FILES = (
    "programme.json",
    "method.py",
    "study.py",
    "validate_results.py",
    "results.json",
    "selected-cells.csv",
)


def output_directory(value: str) -> Path:
    """Keep generated results in the explicitly ignored research output tree."""
    path = Path(value).resolve()
    if not path.is_relative_to(OUTPUT_ROOT.resolve()) or path == OUTPUT_ROOT.resolve():
        raise ValueError("--output must name a run directory inside research/fuzzing/outputs")
    return path


def write_result(output: Path, name: str, value: object) -> None:
    # Exclusive creation also refuses an existing symlink or a concurrent writer.
    with (output / name).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")


def freeze(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    data = {
        "sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FROZEN_FILES
        },
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }
    write_result(output, "freeze.json", data)
    print("Public reference sources frozen for this reproduction.")


def verify_freeze(output: Path) -> dict:
    data = json.loads((output / "freeze.json").read_text())
    if set(data["sha256"]) != set(FROZEN_FILES):
        raise ValueError("freeze does not cover the public reference sources")
    for name, expected in data["sha256"].items():
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"frozen source changed: {name}")
    if data["numpy"] != np.__version__ or data["scipy"] != scipy.__version__:
        raise ValueError("numerical dependency versions differ from this run's freeze")
    return data


def method_checks(output: Path) -> None:
    alpha = PROGRAMME["confirmation"]["alpha"]
    gamma = alpha / 2
    errors = []
    checks = 0
    for n in (*range(1, 21), 40, 80):
        lo, hi = lower_bounds(n, gamma), upper_bounds(n, gamma)
        assert lo[0] == 0 and hi[n] == 1
        assert np.allclose(lo, 1 - hi[::-1], atol=2e-14, rtol=0)
        for k in range(n + 1):
            ref_lo = float(
                binomtest(k, n, alternative="greater")
                .proportion_ci(confidence_level=1 - gamma, method="exact")
                .low
            )
            ref_hi = float(
                binomtest(k, n, alternative="less")
                .proportion_ci(confidence_level=1 - gamma, method="exact")
                .high
            )
            errors.extend((abs(lo[k] - ref_lo), abs(hi[k] - ref_hi)))
            assert abs(lo[k] - ref_lo) < 1e-12 and abs(hi[k] - ref_hi) < 1e-12
            # Independent standard-library binomial tail equations, not beta inversion.
            if k > 0:
                tail = math.fsum(
                    math.comb(n, j) * lo[k] ** j * (1 - lo[k]) ** (n - j) for j in range(k, n + 1)
                )
                assert abs(tail - gamma) < 2e-12
            if k < n:
                tail = math.fsum(
                    math.comb(n, j) * hi[k] ** j * (1 - hi[k]) ** (n - j) for j in range(k + 1)
                )
                assert abs(tail - gamma) < 2e-12
            checks += 1
        assert abs(lo[n] - gamma ** (1 / n)) < 2e-14
        assert abs(hi[0] - (1 - gamma ** (1 / n))) < 2e-14
    pairs = PROGRAMME["operating_characteristics"]["sample_pairs"]
    grid = PROGRAMME["operating_characteristics"]
    rows = []
    for (no, nm), po, delta, offset in itertools.product(
        pairs, grid["original_rates"], grid["deltas"], grid["decrease_offsets"]
    ):
        decrease = max(0, delta + offset)
        pm = po - decrease
        if not 0 <= pm <= 1:
            continue
        prob = rejection_probability(no, nm, po, pm, alpha, delta)
        rows.append(
            {
                "n_original": no,
                "n_mutant": nm,
                "p_original": po,
                "p_mutant": pm,
                "delta": delta,
                "rejection_probability": prob,
                "null": decrease <= delta,
            }
        )
        if decrease <= delta:
            assert prob <= alpha + 1e-12
    # Dense finite boundary checks complement, but do not replace, the uniform union-bound proof.
    boundary = []
    for (no, nm), delta in itertools.product(pairs, grid["deltas"]):
        probabilities = [
            rejection_probability(no, nm, float(po), float(po - delta), alpha, delta)
            for po in np.linspace(delta, 1, 1001)
        ]
        maximum = max(probabilities)
        assert maximum <= alpha + 1e-12
        boundary.append(
            {"n_original": no, "n_mutant": nm, "delta": delta, "max_boundary_error": maximum}
        )
    # Equality must not pass: a zero lower difference cannot exceed delta=0.
    assert not rejection_cells(20, 20, alpha, 0)[0, 20]
    result = {
        "reference_count": checks,
        "maximum_api_error": max(errors),
        "exact_grid": rows,
        "dense_boundary": boundary,
        "n10_zero_upper": float(upper_bounds(10, gamma)[0]),
        "n10_all_lower": float(lower_bounds(10, gamma)[10]),
    }
    write_result(output, "method-results.json", result)
    print(
        json.dumps({k: v for k, v in result.items() if k not in ("exact_grid", "dense_boundary")})
    )


def geometry() -> list[tuple[int, int, str]]:
    single = [(location, 1 << atom, "single") for location in range(4) for atom in range(4)]
    interactions = [
        (0, (1 << a) | (1 << b), "interaction")
        for a, b in PROGRAMME["mutation"]["interaction_pairs"]
    ]
    return single + interactions


def proposal(mask: int, cursor: int) -> tuple[int, int] | None:
    """The actual proposed paragraph simplifier: ordered removal, restart on acceptance."""
    for atom in range(cursor, 4):
        if mask & (1 << atom):
            return mask & ~(1 << atom), atom
    return None


def simplify_truth(essential: int) -> int:
    mask, cursor = 15, 0
    seen: dict[int, bool] = {}
    for _ in range(10):
        next_item = proposal(mask, cursor)
        if next_item is None:
            return mask
        candidate, atom = next_item
        accepted = candidate != 0 and seen.setdefault(candidate, candidate & essential == essential)
        if accepted:
            mask, cursor = candidate, 0
        else:
            cursor = atom + 1
    assert proposal(mask, cursor) is None
    return mask


def simulate(
    *,
    n_screen: int,
    n_confirm: int,
    budget: int,
    location: int,
    essential: int,
    invalid: float,
    workflow: str,
    seed: int,
    p_original: float = 0.9,
    p_essential: float = 0.1,
    invalid_original: float | None = None,
    p_null_mutant: float | None = None,
) -> dict:
    """Vectorized independent campaigns. Only the synthetic sampling draw is batched."""
    n = REPETITIONS
    rng = np.random.default_rng(seed)
    calls = np.zeros(n, dtype=np.int64)
    alive = np.ones(n, dtype=bool)
    selected = np.full(n, -1, dtype=np.int8)
    masks = np.full(n, 15, dtype=np.int8)
    finished = np.ones(n, dtype=bool) if workflow != "adaptive-shrink" else np.zeros(n, dtype=bool)
    reason = np.full(n, "", dtype="U24")
    max_seen = 0
    original_invalid = invalid if invalid_original is None else invalid_original
    unchanged_mutant_rate = p_original if p_null_mutant is None else p_null_mutant
    reserve = 2 * attempts_limit(n_confirm)
    cache = np.full((4, 16, n), -1, dtype=np.int8)

    def pay_arm(
        indices: np.ndarray, required: int, ceiling: int, q: float
    ) -> tuple[np.ndarray, np.ndarray]:
        needed = rng.negative_binomial(required, 1 - q, size=len(indices)) + required
        calls[indices] += np.minimum(needed, ceiling)
        return needed <= ceiling, needed

    # Calibration is a fixed valid block, separate from every later comparison.
    if budget < 7 + reserve:
        alive[:] = False
        reason[:] = "initial-budget-refusal"
    else:
        ids = np.arange(n)
        complete, _ = pay_arm(ids, 3, 7, original_invalid)
        baseline = rng.binomial(3, p_original, size=n)
        alive &= complete & (baseline >= 2)
        reason[~complete] = "calibration-incomplete"
        reason[complete & (baseline < 2)] = "calibration-not-measurable"

    def pair(indices: np.ndarray, required: int, rates: np.ndarray, *, final: bool = False):
        nonlocal max_seen
        ceiling = attempts_limit(required)
        future = 0 if final else reserve
        admission = calls[indices] + 2 * ceiling + future <= budget
        refused = indices[~admission]
        alive[refused] = False
        reason[refused] = "block-budget-refusal"
        indices, rates = indices[admission], rates[admission]
        # The frozen block executes all original attempts then all mutant attempts in this
        # IID cost model. Interleaving has the same joint law; drift is evaluated separately.
        ok_o, _ = pay_arm(indices, required, ceiling, original_invalid)
        ok_m, _ = pay_arm(indices, required, ceiling, invalid)
        complete_pair = ok_o & ok_m
        failed = indices[~complete_pair]
        alive[failed] = False
        reason[failed] = "sampling-incomplete"
        indices, rates = indices[complete_pair], rates[complete_pair]
        xo = rng.binomial(required, p_original, size=len(indices))
        xm = rng.binomial(required, rates, size=len(indices))
        max_seen = max(max_seen, int(calls.max()))
        return indices, xo, xm

    if workflow == "direct-unshrunk":
        selected[alive] = rng.integers(0, 4, size=int(alive.sum()))
    else:
        for candidate_location in range(4):
            ids = np.flatnonzero(alive & (selected < 0))
            rates = np.full(
                len(ids), p_essential if candidate_location == location else unchanged_mutant_rate
            )
            ids, xo, xm = pair(ids, n_screen, rates)
            accepted = (xo - xm) >= 0.4 * n_screen
            cache[candidate_location, 15, ids] = accepted
            selected[ids[accepted]] = candidate_location
        no_hit = alive & (selected < 0)
        reason[no_hit] = "no-candidate-found"
        alive[no_hit] = False

    if workflow == "adaptive-shrink":
        cursor = np.zeros(n, dtype=np.int8)
        for _ in range(10):
            ids = np.flatnonzero(alive & (selected >= 0) & ~finished)
            if len(ids) == 0:
                break
            proposed = np.full(n, -1, dtype=np.int8)
            atoms = np.full(n, -1, dtype=np.int8)
            for mask, start in itertools.product(range(1, 16), range(5)):
                group = ids[(masks[ids] == mask) & (cursor[ids] == start)]
                if len(group) == 0:
                    continue
                next_item = proposal(mask, start)
                if next_item is None:
                    finished[group] = True
                else:
                    proposed[group], atoms[group] = next_item
            ids = ids[proposed[ids] >= 0]
            accept = np.zeros(n, dtype=bool)
            # Empty mutation has no intervention, so cannot satisfy this objective.
            sampled = ids[proposed[ids] != 0]
            cached = cache[selected[sampled], proposed[sampled], sampled]
            reused = sampled[cached >= 0]
            accept[reused] = cached[cached >= 0].astype(bool)
            fresh = sampled[cached < 0]
            has_effect = (selected[fresh] == location) & (
                (proposed[fresh] & essential) == essential
            )
            rates = np.where(has_effect, p_essential, unchanged_mutant_rate)
            fresh, xo, xm = pair(fresh, n_screen, rates)
            decision = (xo - xm) >= 0.4 * n_screen
            accept[fresh] = decision
            cache[selected[fresh], proposed[fresh], fresh] = decision
            admitted = ids[alive[ids]]
            accepted = admitted[accept[admitted]]
            rejected = admitted[~accept[admitted]]
            masks[accepted] = proposed[accepted]
            cursor[accepted] = 0
            cursor[rejected] = atoms[rejected] + 1
        for mask, start in itertools.product(range(1, 16), range(5)):
            if proposal(mask, start) is None:
                finished |= (masks == mask) & (cursor == start)
        unfinished = alive & (selected >= 0) & ~finished
        reason[unfinished] = "simplification-work-cap"

    # Freeze selection here. There is exactly one fresh holdout; no retry/reselection.
    ids = np.flatnonzero(alive & (selected >= 0))
    has_effect = (selected[ids] == location) & ((masks[ids] & essential) == essential)
    rates = np.where(has_effect, p_essential, unchanged_mutant_rate)
    ids, xo, xm = pair(ids, n_confirm, rates, final=True)
    confirmed = np.zeros(n, dtype=bool)
    cells = rejection_cells(n_confirm, n_confirm, 0.05, 0.10)
    confirmed[ids] = cells[xo, xm]
    reason[ids] = np.where(confirmed[ids], "confirmed-change", "inconclusive")
    retains_essential = (selected == location) & ((masks & essential) == essential)
    noise = [i for i in range(4) if not (essential & (1 << i))]
    total_noise = int(SIZES[noise].sum())
    removed_noise = np.zeros(n, dtype=np.int64)
    for i in noise:
        removed_noise += ((masks & (1 << i)) == 0) * SIZES[i]
    simplified = (removed_noise >= total_noise / 2) & finished & retains_essential
    success = confirmed & simplified if workflow == "adaptive-shrink" else confirmed
    successes = int(success.sum())
    lower = float(betaincinv(successes, n - successes + 1, POWER_TAIL)) if successes else 0.0
    conditional_rates = np.where(retains_essential, p_essential, unchanged_mutant_rate)
    conditional_effect = p_original - conditional_rates > 0.10
    geometry_applies = p_null_mutant is None
    assert int(calls.max()) <= budget
    assert not np.any(confirmed & ~alive)
    return {
        "workflow": workflow,
        "n_screen": n_screen,
        "n_confirm": n_confirm,
        "budget": budget,
        "location": location,
        "essential_mask": essential,
        "invalid": invalid,
        "p_original": p_original,
        "p_essential": p_essential,
        "invalid_original": original_invalid,
        "repetitions": n,
        "workflow_success": successes / n if geometry_applies else None,
        "simultaneous_lower": lower if geometry_applies else None,
        "confirmed": float(confirmed.mean()),
        "simplified_confirmed": float((confirmed & simplified).mean())
        if geometry_applies
        else None,
        "simplification_complete": float(finished.mean()),
        "calls_mean": float(calls.mean()),
        "calls_p50": float(np.quantile(calls, 0.5)),
        "calls_p95": float(np.quantile(calls, 0.95)),
        "calls_max": int(calls.max()),
        "confirmed_without_effect": int((confirmed & ~conditional_effect).sum()),
        "terminal_counts": {
            str(key): int(count)
            for key, count in zip(*np.unique(reason, return_counts=True), strict=True)
        },
    }


def run_budget(budget: int, output: Path) -> None:
    rows = []
    start = time.monotonic()
    for _config_index, (ns, nc) in enumerate(itertools.product((3, 5, 8), (20, 40, 80))):
        for q_index, invalid in enumerate((0.0, 0.05, 0.1, 0.2)):
            for g_index, (location, essential, kind) in enumerate(geometry()):
                for w_index, workflow in enumerate(WORKFLOWS):
                    # A stable tuple seed identifies each cell and avoids correlated holdouts.
                    seed = np.random.SeedSequence(
                        [2026102301, budget, ns, nc, q_index, g_index, w_index]
                    )
                    result = simulate(
                        n_screen=ns,
                        n_confirm=nc,
                        budget=budget,
                        location=location,
                        essential=essential,
                        invalid=invalid,
                        workflow=workflow,
                        seed=seed,
                    )
                    result["geometry"] = kind
                    rows.append(result)
        adaptive = [
            row
            for row in rows
            if row["n_screen"] == ns
            and row["n_confirm"] == nc
            and row["workflow"] == "adaptive-shrink"
            and row["geometry"] == "single"
        ]
        print(
            json.dumps(
                {
                    "budget": budget,
                    "n_screen": ns,
                    "n_confirm": nc,
                    "minimum_single_success": min(r["workflow_success"] for r in adaptive),
                    "minimum_single_lower": min(r["simultaneous_lower"] for r in adaptive),
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                }
            ),
            flush=True,
        )
    write_result(output, f"workflow-{budget}.json", rows)


def analyse(output: Path) -> None:
    rows = list(
        itertools.chain.from_iterable(
            (
                json.loads((output / f"workflow-{budget}.json").read_text())
                for budget in (256, 384, 512)
            ),
        )
    )
    configurations = []
    for budget, ns, nc in itertools.product((256, 384, 512), (3, 5, 8), (20, 40, 80)):
        subset = [
            r
            for r in rows
            if r["budget"] == budget
            and r["n_screen"] == ns
            and r["n_confirm"] == nc
            and r["workflow"] == "adaptive-shrink"
        ]
        single = [r for r in subset if r["geometry"] == "single"]
        interaction = [r for r in subset if r["geometry"] == "interaction"]
        configurations.append(
            {
                "budget": budget,
                "n_screen": ns,
                "n_confirm": nc,
                "single_pass": min(r["simultaneous_lower"] for r in single) >= 0.8,
                "interaction_pass": min(r["simultaneous_lower"] for r in interaction) >= 0.8,
                "minimum_single_success": min(r["workflow_success"] for r in single),
                "minimum_single_lower": min(r["simultaneous_lower"] for r in single),
                "minimum_interaction_success": min(r["workflow_success"] for r in interaction),
                "minimum_interaction_lower": min(r["simultaneous_lower"] for r in interaction),
                "mean_calls": float(np.mean([r["calls_mean"] for r in single])),
                "largest_p95": max(r["calls_p95"] for r in single),
            }
        )
    eligible = [r for r in configurations if r["single_pass"]]
    selected = (
        min(eligible, key=lambda r: (r["mean_calls"], r["budget"], r["n_confirm"]))
        if eligible
        else None
    )
    baselines = []
    if selected:
        for workflow in WORKFLOWS:
            subset = [
                r
                for r in rows
                if r["workflow"] == workflow
                and r["geometry"] == "single"
                and all(r[k] == selected[k] for k in ("budget", "n_screen", "n_confirm"))
            ]
            baselines.append(
                {
                    "workflow": workflow,
                    "minimum_confirmed": min(r["confirmed"] for r in subset),
                    "minimum_simplified_confirmed": min(r["simplified_confirmed"] for r in subset),
                    "mean_calls": float(np.mean([r["calls_mean"] for r in subset])),
                    "largest_p95": max(r["calls_p95"] for r in subset),
                }
            )
    result = {
        "selected": selected,
        "designs": configurations,
        "baselines": baselines,
        "repetitions": REPETITIONS,
        "simultaneous_family_size": FAMILY_SIZE,
    }
    write_result(output, "analysis.json", result)
    print(json.dumps({"selected": selected, "baselines": baselines}))


def sensitivity(output: Path) -> None:
    selected = json.loads((output / "analysis.json").read_text())["selected"]
    if not selected:
        raise ValueError(
            "no qualifying design; sensitivity selection requires prospective revision"
        )
    rows = []
    config = {k: selected[k] for k in ("budget", "n_screen", "n_confirm")}
    for i, (location, essential, _kind) in enumerate(geometry()):
        result = simulate(
            **config,
            location=location,
            essential=essential,
            invalid=0.1,
            workflow="adaptive-shrink",
            p_original=0.9,
            p_essential=0.9,
            seed=np.random.SeedSequence([2026102302, 0, i]),
        )
        result["scenario"] = "all-null-including-duplication-control"
        rows.append(result)
    for i, rates in enumerate(((0.9, 0.1, 0.05, 0.2), (2 / 3, 1 / 3, 0.25, 0.25))):
        po, pm, qo, qm = rates
        result = simulate(
            **config,
            location=0,
            essential=1,
            invalid=qm,
            invalid_original=qo,
            workflow="adaptive-shrink",
            p_original=po,
            p_essential=pm,
            seed=np.random.SeedSequence([2026102302, 1, i]),
            p_null_mutant=pm if i == 1 else None,
        )
        result["scenario"] = (
            "candidate-dependent-validity" if i == 0 else "outcome-dependent-validity"
        )
        rows.append(result)
    rng = np.random.default_rng(2026102302)
    nc = selected["n_confirm"]
    probabilities = np.linspace(0.9, 0.5, 2 * nc)
    order = rng.random((REPETITIONS, 2 * nc)).argsort(axis=1)
    original_mask = order < nc  # Exactly n labels of each arm, uniformly interleaved.
    outcomes = rng.random((REPETITIONS, 2 * nc)) < probabilities
    xo = (outcomes & original_mask).sum(axis=1)
    xm = (outcomes & ~original_mask).sum(axis=1)
    declarations = rejection_cells(nc, nc, 0.05, 0.10)[xo, xm]
    result = {
        "rows": rows,
        "common_time_drift": {
            "repetitions": REPETITIONS,
            "declarations": int(declarations.sum()),
            "declaration_rate": float(declarations.mean()),
            "iid_assumption_satisfied": False,
        },
    }
    write_result(output, "sensitivity.json", result)
    print(
        json.dumps(
            {
                "null_max_confirmed": max(r["confirmed"] for r in rows[:22]),
                "validity_rows": rows[22:],
                "drift": result["common_time_drift"],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("freeze", "method", "workflow", "analyse", "sensitivity"))
    parser.add_argument("--budget", type=int, choices=(256, 384, 512))
    parser.add_argument("--output", required=True, type=output_directory)
    options = parser.parse_args()
    if options.mode == "freeze":
        freeze(options.output)
        return
    verify_freeze(options.output)
    if options.mode == "method":
        method_checks(options.output)
    elif options.mode == "workflow":
        if options.budget is None:
            parser.error("workflow needs --budget")
        for _, essential, _ in geometry():
            assert simplify_truth(essential) == essential
        run_budget(options.budget, options.output)
    elif options.mode == "analyse":
        analyse(options.output)
    else:
        sensitivity(options.output)


if __name__ == "__main__":
    main()
