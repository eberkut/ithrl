"""Eager baseline-sharing simulation over the fixed reference search order."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from pathlib import Path

import numpy as np
from reference import BinomialSample, classify_fixed_sample, run_search, trial_key
from scipy.stats import beta, binom

HERE = Path(__file__).resolve().parent
CAP = 6
GROUPS = tuple(f"g{i}" for i in range(4))
MAX_REPORT = 64 * 1024 * 1024


@dataclass(frozen=True)
class Plan:
    name: str
    calibration_n: int
    calibration_min: int
    calibration_a: int
    original_n: int
    original_a: int
    candidate_n: int
    candidate_a: int
    confirmation_n: int
    confirmation_a: int


PLANS = (
    Plan("small", 3, 2, 7, 4, 8, 5, 9, 100, 124),
    Plan("large", 10, 2, 16, 80, 100, 80, 100, 200, 239),
)


@dataclass(frozen=True)
class Trial:
    trial_key: str
    effective_removals: tuple[str, ...]
    accepts_removal: bool
    allows_search_continuation: bool = True


def build_tree() -> dict:
    """Exactly 64 six-bit strings drive actual callbacks; cached keys never redraw."""
    case = None
    nodes: dict[str, dict] = {}
    terminals: dict[str, dict] = {}
    paths = []
    for bits in itertools.product((0, 1), repeat=CAP):
        prefix = ""
        visited = []

        async def classify(
            *, base_removals, requested_removals, phase, visited=visited, bits=bits, **_
        ):
            nonlocal prefix
            if len(prefix) == CAP:
                return None
            base = tuple(sorted(base_removals))
            request = tuple(requested_removals)
            effective = tuple(sorted(set(base) | set(request)))
            facts = {
                "base": base,
                "request": request,
                "effective": effective,
                "phase": phase,
                "key": trial_key(phase, base, request),
            }
            if prefix in nodes and nodes[prefix] != facts:
                raise AssertionError("actual search is not prefix deterministic")
            nodes[prefix] = facts
            visited.append(prefix)
            accept = bool(bits[len(prefix)])
            prefix += str(int(accept))
            return Trial(facts["key"], effective, accept)

        outcome = asyncio.run(
            run_search(
                case,
                removable=list(GROUPS),
                classify=classify,
                prior_trials=deque(),
                publish=False,
                publish_checkpoint=lambda *_: None,
            )
        )
        terminal = {
            "removed": tuple(sorted(outcome.removed)),
            "retained": tuple(g for g in GROUPS if g not in outcome.removed),
            "complete": outcome.complete,
            "applied": len(outcome.trials),
        }
        if prefix in terminals and terminals[prefix] != terminal:
            raise AssertionError("terminal mismatch")
        terminals[prefix] = terminal
        paths.append({"bits": "".join(map(str, bits)), "prefix": prefix, "visited": visited})
    return {
        "nodes": nodes,
        "terminals": terminals,
        "paths": paths,
        "synthetic_group_bytes": [128] * 4,
        "cap": CAP,
    }


def cohort(tree: dict, prefix: str, size: int) -> list[str]:
    """Freeze consecutive retain neighbors; acceptance discards the unused suffix."""
    first = tree["nodes"][prefix]
    members = [prefix]
    cursor = prefix + "0"
    while len(members) < size and cursor in tree["nodes"]:
        other = tree["nodes"][cursor]
        if (other["base"], other["phase"], len(other["request"])) != (
            first["base"],
            first["phase"],
            len(first["request"]),
        ):
            break
        members.append(cursor)
        cursor += "0"
    return members


def path_cost(tree: dict, terminal: str, plan: Plan, size: int) -> dict:
    prefix = ""
    originals = candidates = discarded = 0
    while prefix in tree["nodes"]:
        members = cohort(tree, prefix, size)
        originals += 1
        candidates += len(members)
        for index, member in enumerate(members):
            bit = terminal[len(member)]
            prefix = member + bit
            if bit == "1":
                discarded += len(members) - index - 1
                break
    search = originals * plan.original_a + candidates * plan.candidate_a
    return {
        "search_attempt_budget": search,
        "original_arms": originals,
        "candidate_arms": candidates,
        "discarded_candidates": discarded,
        "workflow_attempt_budget": plan.calibration_a + search + 2 * plan.confirmation_a,
    }


def budgets(tree: dict, plan: Plan, size: int) -> dict:
    fixed_strict = (
        plan.calibration_a + CAP * (plan.original_a + plan.candidate_a) + 2 * plan.confirmation_a
    )
    fixed_epoch = (
        plan.calibration_a
        + ceil(CAP / size) * plan.original_a
        + CAP * plan.candidate_a
        + 2 * plan.confirmation_a
    )
    paths = {p: path_cost(tree, p, plan, size) for p in tree["terminals"]}
    worst = max(v["workflow_attempt_budget"] for v in paths.values())
    strict_worst = max(
        path_cost(tree, p, plan, 1)["workflow_attempt_budget"] for p in tree["terminals"]
    )
    return {
        "fixed_six_strict": fixed_strict,
        "fixed_six_policy": fixed_epoch,
        "equivalent_work_saving": 1 - fixed_epoch / fixed_strict,
        "actual_worst_workflow": worst,
        "strict_worst_workflow": strict_worst,
        "worst_workflow_saving": 1 - worst / strict_worst,
        "budget_pass": fixed_epoch <= 0.8 * fixed_strict,
        "flat_budget_covers_actual_worst": worst <= fixed_epoch,
        "terminal_costs": paths,
    }


def cp(count: int, n: int, alpha: float) -> tuple[float, float]:
    """Exact one-sided Clopper-Pearson tails, each using supplied tail alpha."""
    if not 0 <= count <= n or n <= 0 or not 0 < alpha < 1:
        raise ValueError("invalid exact interval inputs")
    lower = 0.0 if count == 0 else float(beta.ppf(alpha, count, n - count + 1))
    upper = 1.0 if count == n else float(beta.ppf(1 - alpha, count + 1, n - count))
    return lower, upper


def assured_n(*, m: int, n: int = 200_000) -> dict:
    """Check a declared N at frozen design quantiles; never claim a minimal N."""
    alpha = 0.01 / m
    quantile_alpha = 0.05 / m
    errors = int(binom.ppf(1 - quantile_alpha, n, 0.05))
    adverse = int(binom.ppf(1 - quantile_alpha, n, 0.001))
    plus = int(binom.ppf(1 - quantile_alpha, n, 0.01))
    minus = int(binom.ppf(quantile_alpha, n, 0.01))
    error_upper = cp(errors, n, alpha)[1]
    adverse_upper = cp(adverse, n, alpha)[1]
    symmetric_upper = cp(plus, n, alpha)[1] - cp(minus, n, alpha)[0]
    useful = int(binom.ppf(quantile_alpha, n, 0.9))
    useful_lower = cp(useful, n, alpha)[0]
    return {
        "n": n,
        "bonferroni_tails": m,
        "alpha_per_tail": alpha,
        "design_assurance": 0.95,
        "design_quantile_tail": quantile_alpha,
        "assumed_reference_error": 0.05,
        "assumed_adverse_discordance": 0.001,
        "ceiling": 0.055,
        "excess_ceiling": 0.005,
        "error_quantile_upper": error_upper,
        "adverse_quantile_upper": adverse_upper,
        "symmetric_discordance_design": 0.01,
        "symmetric_quantile_excess_upper": symmetric_upper,
        "useful_design_probability": 0.9,
        "useful_quantile_lower": useful_lower,
        "design_pass": error_upper <= 0.055
        and adverse_upper <= 0.005
        and symmetric_upper <= 0.005
        and useful_lower >= 0.8,
    }


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def family_tails(config: dict) -> int:
    """Exact maximum number of confidence tails emitted by this frozen report."""
    cells = (
        len(config.get("plans", ["small", "large"]))
        * len(config["scenarios"])
        * len(config["instruction_positions"])
    )
    # Per policy/cell: two unconditional endpoints each two tails, two conditional
    # endpoint upper tails, one conditional false-loss upper tail, seven
    # descriptive endpoints each two tails, two costs
    # each two tails. Each of two nonreference policies has two paired endpoints,
    # using one upper adverse and one lower favorable discordance tail.
    return cells * (3 * (4 + 2 + 1 + 14 + 4) + 2 * 2 * 2)


def useful_removal(removal_mask: int, instruction_position: int) -> bool:
    """Retain the instruction and remove at least half of the four equal groups."""
    return not removal_mask & (1 << instruction_position) and removal_mask.bit_count() >= 2


def validate_refusal_declarations(config: dict, tree: dict) -> list[dict]:
    """Prove declared no-holdout profiles from the first complete frozen cohort.

    Even zero failures cannot supply all valid targets before the declared expiry.
    Calibration failures also forbid confirmation, so they cannot defeat this proof.
    Only this structural declaration makes missing conditional-null data inapplicable.
    """
    proofs = []
    for scenario in config["scenarios"]:
        for size in scenario.get("expected_search_refusal_cohorts", []):
            if size not in (2, 4):
                raise ValueError("expected refusal declaration must identify cohort 2 or 4")
            members = cohort(tree, "", size)[: scenario.get("cohort_expiry_candidates", size)]
            for plan in PLANS:
                if plan.name not in config.get("plans", ["small", "large"]):
                    continue
                expiry = scenario.get("cohort_expiry_slots", {})
                limit = expiry.get(plan.name, 10**9) if isinstance(expiry, dict) else expiry
                minimum = plan.original_n + len(members) * plan.candidate_n
                if minimum <= limit:
                    raise ValueError("expected refusal lacks a first-cohort lifetime proof")
                proofs.append(
                    {
                        "scenario": scenario["name"],
                        "plan": plan.name,
                        "cohort": size,
                        "frozen_candidates": len(members),
                        "minimum_valid_dispatches": minimum,
                        "exclusive_lifetime": limit,
                        "confirmation_structurally_impossible": True,
                    }
                )
    return proofs


def rates(scenario: dict, times: np.ndarray, removal_mask: int, shock: np.ndarray) -> np.ndarray:
    instruction = scenario.get("instruction_position", 0)
    base = (
        scenario["p0"]
        if not removal_mask
        else (scenario["pr"] if removal_mask & (1 << instruction) else scenario["ph"])
    )
    mode = scenario.get("mode", "stationary")
    change = np.zeros_like(times, dtype=float)
    if mode == "smooth":
        change = scenario["drift_size"] * np.minimum(times / scenario["drift_at"], 1)
    elif mode == "abrupt":
        change = scenario["drift_size"] * (times >= scenario["drift_at"])
    elif mode not in ("stationary", "shock"):
        raise ValueError("undeclared time model")
    # Shared movement changes both latent arm probabilities at the same instant.
    return np.clip(base + change + shock, 0, 1)


def truth(
    scenario: dict, times: np.ndarray, removal_mask: int, shock: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Exact decimal-rate null labels except clipping changes, handled algebraically.

    Comparing the generator's decimal base difference to its decimal margin avoids
    an arbitrary tolerance around a rational boundary. Clipped effects are evaluated
    by separate lower/upper saturation branches; equality is exact in those branches.
    """
    instruction = scenario.get("instruction_position", 0)
    base = (
        scenario["p0"]
        if not removal_mask
        else (scenario["pr"] if removal_mask & (1 << instruction) else scenario["ph"])
    )
    original = rates(scenario, times, 0, shock)
    candidate = rates(scenario, times, removal_mask, shock)
    interior = (original > 0) & (original < 1) & (candidate > 0) & (candidate < 1)
    decimal_delta = Fraction(str(base)) - Fraction(str(scenario["p0"]))
    margin = Fraction(str(scenario["margin"]))
    difference = candidate - original
    bad = difference < -float(margin)
    boundary = difference == -float(margin)
    bad[interior] = decimal_delta < -margin
    boundary[interior] = decimal_delta == -margin
    return bad, boundary, ~bad


def frozen_schedule(
    arms: list[tuple[int, int, int]],
    *,
    seed: int,
    identity: str,
    total_rows: int,
    row_indices: np.ndarray | None = None,
) -> np.ndarray:
    """Independent canonical replica schedules, unaffected by other row prefixes.

    The batch seed includes the frozen global replica offset. Each row has an
    independent permutation at its canonical index, salted by the stable decision
    prefix (or calibration identity). Generate one full bounded matrix and then
    select the current prefix subgroup; subgroup size/order never selects a seed.
    """
    slots = np.concatenate(
        [np.full(limit, j, dtype=np.uint8) for j, (_, limit, _) in enumerate(arms)]
    )
    salt = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "little")
    schedule_rng = np.random.default_rng(np.random.SeedSequence([seed, salt]))
    matrix = schedule_rng.permuted(np.broadcast_to(slots, (total_rows, len(slots))).copy(), axis=1)
    if row_indices is None:
        return matrix
    if np.any(row_indices < 0) or np.any(row_indices >= total_rows):
        raise ValueError("schedule replica indices outside frozen batch")
    return matrix[row_indices]


def sample_ordered_q0(
    rng,
    scenario: dict,
    times: np.ndarray,
    arms: list[tuple[int, int, int]],
    schedule: np.ndarray,
    shock: np.ndarray,
):
    """Vectorize zero-failure dispatches, preserving the loop's exact RNG order.

    Arm occurrence ranks identify completed-arm skips without an adaptive loop.
    Retained dispatch ranks determine physical times and deadline eligibility.
    Events are ordered by column, arm and canonical subgroup row, matching the
    reference loop. Every group's unused eligibility draws are still consumed,
    and its success draws occupy the second half of that group's random vector.
    """
    rows, columns = schedule.shape
    k = len(arms)
    kept = np.zeros_like(schedule, dtype=bool)
    for j, (target, limit, _) in enumerate(arms):
        occurrences = schedule == j
        ranks = np.cumsum(occurrences, axis=1, dtype=np.int32)
        kept |= occurrences & (ranks <= min(target, limit))
    dispatch_rank = np.cumsum(kept, axis=1, dtype=np.int32)
    resume = scenario.get("resume", "none")
    pause = scenario.get("pause_slots", 1) if resume != "none" else 0
    elapsed_before = dispatch_rank - 1 + pause * (dispatch_rank > 1)
    kept &= elapsed_before < scenario.get("deadline_slots", 10**9)
    if resume == "stale":
        kept[:, 1:] = False
    row, column = np.nonzero(kept)
    arm = schedule[row, column]
    groups = column * k + arm
    order = np.lexsort((row, groups))
    row, column, arm, groups = row[order], column[order], arm[order], groups[order]
    counts = np.bincount(groups, minlength=columns * k)
    starts = np.cumsum(counts) - counts
    # Event i belongs after starts[group] earlier events. The group's eligibility
    # vector precedes its success vector; this is exactly the old two RNG calls.
    success_draw_index = np.arange(len(row)) + starts[groups] + counts[groups]
    draws = rng.random(2 * len(row))
    start = times.copy()
    event_rank = dispatch_rank[row, column]
    event_times = start[row] + event_rank - 1 + pause * (event_rank > 1)
    probability = np.empty(len(row), dtype=float)
    for j, (_, _, removed) in enumerate(arms):
        subset = arm == j
        probability[subset] = rates(scenario, event_times[subset], removed, shock[row[subset]])
    success = draws[success_draw_index] < probability
    attempts = np.zeros((k, rows), dtype=np.int16)
    matches = np.zeros_like(attempts)
    for j in range(k):
        subset = arm == j
        attempts[j] = np.bincount(row[subset], minlength=rows)
        matches[j] = np.bincount(row[subset], weights=success[subset], minlength=rows)
    dispatched = attempts.sum(axis=0)
    times[:] = start + dispatched + pause * (dispatched > 0)
    return matches, attempts.copy(), attempts, times, shock


def sample_cohort(
    rng,
    scenario: dict,
    times: np.ndarray,
    arms: list[tuple[int, int, int]],
    *,
    seed: int,
    schedule_identity: str = "standalone",
    row_indices: np.ndarray | None = None,
    total_rows: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Complete every frozen arm before any comparison; q failures precede acceptance.

    Randomized planned arm slots are independent per canonical replica and prefix.
    Completed arms skip their
    remaining slots. Every actual dispatch advances the row's physical slot time.
    The stationary path is exact negative-binomial aggregation; changing-rate paths
    use per-dispatch Bernoulli sampling rather than aggregating across drift.
    """
    rows = len(times)
    k = len(arms)
    matches = np.zeros((k, rows), dtype=np.int16)
    valid = np.zeros_like(matches)
    attempts = np.zeros_like(matches)
    q = scenario.get("q", 0.0)
    shock_size = scenario.get("shock", 0.0)
    shock = rng.choice((-shock_size, shock_size), size=rows) if shock_size else np.zeros(rows)
    resume = scenario.get("resume", "none")
    start = times.copy()
    deadline = scenario.get("deadline_slots", 10**9)
    if (
        scenario.get("mode", "stationary") == "stationary"
        and not shock_size
        and resume == "none"
        and not scenario.get("force_slots", False)
        and sum(a[1] for a in arms) <= deadline
    ):
        # Aggregation is valid only when within-cohort probabilities are stationary.
        for j, (target, limit, removed) in enumerate(arms):
            failures = (
                rng.negative_binomial(target, 1 - q, size=rows) if q else np.zeros(rows, dtype=int)
            )
            needed = target + failures
            complete = needed <= limit
            attempts[j] = np.minimum(needed, limit)
            valid[j] = target
            # Conditional on failing to attain target, draw the truncated binomial
            # valid count exactly, rather than treating its failures as valid evidence.
            bad = ~complete
            if bad.any():
                weights = binom.cdf(target - 1, limit, 1 - q)
                u = rng.random(int(bad.sum())) * weights
                valid[j, bad] = binom.ppf(u, limit, 1 - q).astype(np.int16)
            probability = rates(scenario, times, removed, shock)
            matches[j] = rng.binomial(valid[j], probability)
        times = times + attempts.sum(axis=0)
        return matches, valid, attempts, times, shock
    schedule = frozen_schedule(
        arms,
        seed=seed,
        identity=schedule_identity,
        total_rows=rows if total_rows is None else total_rows,
        row_indices=row_indices,
    )
    if len(schedule) != rows:
        raise ValueError("schedule row count does not match the cohort subgroup")
    if q == 0:
        return sample_ordered_q0(rng, scenario, times, arms, schedule, shock)
    paused = np.zeros(rows, dtype=bool)
    for column in range(schedule.shape[1]):
        for j, (target, limit, removed) in enumerate(arms):
            active = (
                (schedule[:, column] == j)
                & (valid[j] < target)
                & (attempts[j] < limit)
                & (times - start < deadline)
            )
            if not active.any():
                continue
            index = np.flatnonzero(active)
            probability = rates(scenario, times[index], removed, shock[index])
            eligible = rng.random(len(index)) >= q
            success = rng.random(len(index)) < probability
            attempts[j, index] += 1
            valid[j, index] += eligible
            matches[j, index] += eligible & success
            times[index] += 1
            if resume != "none":
                newly_paused = index[~paused[index]]
                times[newly_paused] += scenario.get("pause_slots", 1)
                paused[newly_paused] = True
        # Every initially active replica's first frozen slot is dispatched before
        # stale-resume refusal. No later slot is dispatched on a stale resume.
        if column == 0 and resume == "stale":
            break
    return matches, valid, attempts, times, shock


def confirmation_table(n: int, margin: float) -> np.ndarray:
    result = np.empty((n + 1, n + 1), dtype=np.int8)
    codes = {"preserves": 1, "loses": 2, "inconclusive": 3}
    for original in range(n + 1):
        for candidate in range(n + 1):
            classification = classify_fixed_sample(
                BinomialSample(candidate, n),
                BinomialSample(original, n),
                required_candidate=n,
                required_original=n,
                confidence=0.95,
                margin=margin,
            )
            result[original, candidate] = codes[classification.outcome]
    return result


def simulate_batch(
    tree: dict, plan: Plan, size: int, scenario: dict, n: int, seed: int, table: np.ndarray
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    prefixes = np.full(n, "", dtype="<U7")
    active = np.ones(n, dtype=bool)
    times = np.zeros(n, dtype=np.int32)
    metrics = {
        name: np.zeros(n, dtype=np.int32)
        for name in (
            "provider_calls",
            "dispatched_attempts",
            "useless_prefetch_calls",
            "calibration_incomplete",
            "calibration_failed",
            "search_incomplete",
            "confirmation_incomplete",
            "confirmation_forbidden",
            "preserves",
            "loses",
            "inconclusive",
            "false_accepts",
            "false_rejects",
            "comparisons",
            "selected_false_preservation_strict",
            "selected_false_preservation_boundary",
            "missed_drift",
            "expiry",
            "resume_refused",
            "refused_invalidity",
            "confirmation_eligible",
            "preacceptance_failures",
            "useful_success",
            "selected_null_strict",
            "selected_null_boundary",
            "same_plan_resume",
            "any_false_accept",
            "any_false_reject",
            "any_missed_drift",
            "selected_true_good",
            "false_loss_good",
        )
    }
    if scenario.get("unsupported", False):
        metrics["refused_invalidity"][:] = 1
        metrics["confirmation_forbidden"][:] = 1
        return metrics

    def charge(index, valid, attempts):
        metrics["provider_calls"][index] += valid.sum(axis=0)
        metrics["dispatched_attempts"][index] += attempts.sum(axis=0)

    calibration_scenario = dict(scenario, resume="none")
    calibration = sample_cohort(
        rng,
        calibration_scenario,
        times,
        [(plan.calibration_n, plan.calibration_a, 0)],
        seed=seed,
        schedule_identity="calibration",
        total_rows=n,
    )
    matches, valid, attempts, times, _ = calibration
    charge(np.arange(n), valid, attempts)
    incomplete = valid[0] != plan.calibration_n
    failed = (matches[0] < plan.calibration_min) & ~incomplete
    metrics["calibration_incomplete"] += incomplete
    metrics["calibration_failed"] += failed
    active &= ~(incomplete | failed)
    while active.any():
        for prefix in np.unique(prefixes[active]):
            index = np.flatnonzero(active & (prefixes == prefix))
            if prefix in tree["terminals"]:
                active[index] = False
                continue
            members = cohort(tree, str(prefix), size)
            candidate_limit = scenario.get("cohort_expiry_candidates", size)
            members = members[:candidate_limit]
            if len(members) < len(cohort(tree, str(prefix), size)):
                metrics["expiry"][index] += 1

            def mask(member):
                return sum(1 << GROUPS.index(g) for g in tree["nodes"][member]["effective"])

            arms = [(plan.original_n, plan.original_a, 0)] + [
                (plan.candidate_n, plan.candidate_a, mask(member)) for member in members
            ]
            start = times[index].copy()
            expiry = scenario.get("cohort_expiry_slots", {})
            limit = expiry.get(plan.name, 10**9) if isinstance(expiry, dict) else expiry
            block_scenario = dict(scenario, deadline_slots=limit)
            if scenario.get("resume") == "same" and metrics["same_plan_resume"][index].any():
                block_scenario["resume"] = "none"
            block = sample_cohort(
                rng,
                block_scenario,
                times[index],
                arms,
                seed=seed,
                schedule_identity=f"search:{prefix}",
                row_indices=index,
                total_rows=n,
            )
            matches, valid, attempts, end, shock = block
            times[index] = end
            charge(index, valid, attempts)
            any_incomplete = (valid != np.asarray([a[0] for a in arms])[:, None]).any(axis=0)
            # The endpoint is exclusive: a completed cohort at its deadline is
            # expired and cannot classify or authorize fresh confirmation.
            expired = end - start >= limit
            metrics["expiry"][index] += expired
            stopped = expired | any_incomplete
            if scenario.get("resume") == "stale":
                metrics["resume_refused"][index] += 1
                stopped[:] = True
            elif block_scenario.get("resume") == "same":
                metrics["same_plan_resume"][index] += 1
            metrics["search_incomplete"][index[stopped]] = 1
            active[index[stopped]] = False
            pending = ~stopped
            for j, member in enumerate(members):
                rows = np.flatnonzero(pending)
                if not len(rows):
                    break
                ids = index[rows]
                # Production screen uses integer cross-products, avoiding float boundaries.
                margin = Fraction(str(scenario["margin"]))
                admit = matches[j + 1, rows] * plan.original_n - matches[
                    0, rows
                ] * plan.candidate_n >= -float(margin * plan.original_n * plan.candidate_n)
                removed = mask(member)
                true_bad, _, true_good = truth(scenario, end[rows], removed, shock[rows])
                metrics["comparisons"][ids] += 1
                metrics["false_accepts"][ids] += admit & true_bad
                metrics["false_rejects"][ids] += ~admit & true_good
                if scenario.get("mode") in ("smooth", "abrupt"):
                    changed = (
                        np.abs(
                            rates(scenario, end[rows], 0, shock[rows])
                            - rates(scenario, start[rows], 0, shock[rows])
                        )
                        > 0.05
                    )
                    metrics["missed_drift"][ids] += admit & changed
                prefixes[ids] = member + "0"
                accepted_ids = ids[admit]
                prefixes[accepted_ids] = member + "1"
                if j + 1 < len(members):
                    metrics["useless_prefetch_calls"][accepted_ids] += valid[
                        j + 2 :, rows[admit]
                    ].sum(axis=0)
                pending[rows[admit]] = False
    # No reselection, and no search/calibration observations enter confirmation.
    eligible = np.array(
        [p in tree["terminals"] and tree["terminals"][p]["complete"] for p in prefixes]
    )
    eligible &= (
        metrics["calibration_incomplete"]
        + metrics["calibration_failed"]
        + metrics["search_incomplete"]
    ) == 0
    metrics["confirmation_forbidden"] = (~eligible).astype(np.int32)
    index = np.flatnonzero(eligible)
    metrics["confirmation_eligible"][index] = 1
    final_rng = np.random.default_rng(np.random.SeedSequence([seed, 0xC0FFEE]))
    terminal_masks = {
        p: sum(1 << GROUPS.index(g) for g in t["removed"]) for p, t in tree["terminals"].items()
    }
    selected_masks = np.fromiter((terminal_masks[prefixes[i]] for i in index), dtype=np.int8)
    for removed in range(16):
        ids = index[selected_masks == removed]
        if not len(ids):
            continue
        # Fresh simultaneous latent state shared by both independent arm draws.
        shock_size = scenario.get("shock", 0.0)
        latent = (
            final_rng.choice((-shock_size, shock_size), size=len(ids))
            if shock_size
            else np.zeros(len(ids))
        )
        p0 = rates(scenario, times[ids], 0, latent)
        p1 = rates(scenario, times[ids], removed, latent)
        # Generate failures independently of potential behavior. Different per-row
        # latent rates require direct vectorized final draws, not a pooled binomial.
        failure0 = final_rng.negative_binomial(
            plan.confirmation_n, 1 - scenario.get("q", 0.0), size=len(ids)
        )
        failure1 = final_rng.negative_binomial(
            plan.confirmation_n, 1 - scenario.get("q", 0.0), size=len(ids)
        )
        attempted = np.minimum(
            plan.confirmation_n + np.stack((failure0, failure1)), plan.confirmation_a
        )
        full = ((plan.confirmation_n + np.stack((failure0, failure1))) <= plan.confirmation_a).all(
            axis=0
        )
        # Partial valid counts are sampled conditional on failure to reach target.
        v = np.full((2, len(ids)), plan.confirmation_n, dtype=int)
        for arm, failures in enumerate((failure0, failure1)):
            bad = plan.confirmation_n + failures > plan.confirmation_a
            if bad.any():
                u = final_rng.random(int(bad.sum())) * binom.cdf(
                    plan.confirmation_n - 1, plan.confirmation_a, 1 - scenario.get("q", 0.0)
                )
                v[arm, bad] = binom.ppf(u, plan.confirmation_a, 1 - scenario.get("q", 0.0)).astype(
                    int
                )
        charge(ids, v, attempted)
        original = final_rng.binomial(v[0], p0)
        candidate = final_rng.binomial(v[1], p1)
        codes = table[original, candidate]
        metrics["confirmation_incomplete"][ids] = ~full
        for code, name in ((1, "preserves"), (2, "loses"), (3, "inconclusive")):
            metrics[name][ids] = (codes == code) & full
        true_bad, boundary, true_good = truth(scenario, times[ids], removed, latent)
        preserving = (codes == 1) & full
        metrics["selected_false_preservation_strict"][ids] = preserving & true_bad
        metrics["selected_false_preservation_boundary"][ids] = preserving & boundary
        metrics["selected_null_strict"][ids] = true_bad & full
        metrics["selected_null_boundary"][ids] = boundary & full
        metrics["selected_true_good"][ids] = true_good & full
        metrics["false_loss_good"][ids] = (codes == 2) & true_good & full
        metrics["useful_success"][ids] = preserving & useful_removal(
            removed, scenario.get("instruction_position", 0)
        )
    metrics["preacceptance_failures"] = metrics["dispatched_attempts"] - metrics["provider_calls"]
    if np.any(metrics["provider_calls"] > metrics["dispatched_attempts"]):
        raise AssertionError("provider calls exceeded dispatches in preacceptance failure model")
    if np.any(
        sum(
            metrics[k]
            for k in (
                "preserves",
                "loses",
                "inconclusive",
                "confirmation_incomplete",
                "confirmation_forbidden",
            )
        )
        != 1
    ):
        raise AssertionError("normalized final outcomes do not partition the replicas")
    for source, target in (
        ("false_accepts", "any_false_accept"),
        ("false_rejects", "any_false_reject"),
        ("missed_drift", "any_missed_drift"),
    ):
        metrics[target] = (metrics[source] > 0).astype(np.int32)
    return metrics


def evaluate(config: dict, frozen: dict, *, pilot: bool = False) -> dict:
    tree = frozen["tree"]
    refusal_proofs = validate_refusal_declarations(config, tree)
    n = config["pilot_n"] if pilot else config["n"]
    alpha = config["tail_alpha"] / config["bonferroni_tails"]
    started = time.monotonic()
    rows = []
    tables = {}
    emitted_tails = family_tails(config)
    if emitted_tails > config["bonferroni_tails"]:
        raise ValueError("declared Bonferroni family is smaller than emitted tails")
    scenarios = [
        dict(s, instruction_position=position)
        for s in config["scenarios"]
        for position in config["instruction_positions"]
    ]
    for pi, plan in enumerate(PLANS):
        if plan.name not in config.get("plans", ["small", "large"]):
            continue
        for si, scenario in enumerate(scenarios):
            table_key = (plan.confirmation_n, scenario["margin"])
            if table_key not in tables:
                tables[table_key] = confirmation_table(*table_key)
            table = tables[table_key]
            totals = {size: {} for size in (1, 2, 4)}
            discordance = {size: {"strict": [0, 0], "boundary": [0, 0]} for size in (2, 4)}
            for offset in range(0, n, config["batch_size"]):
                batch = min(config["batch_size"], n - offset)
                # Common seeds give a declared coupling; marginal samplers remain exact.
                seed_base = config["pilot_seed"] if pilot else config["seed"]
                seed = seed_base + pi * 100_000_000 + si * 1_000_000 + offset
                results = {
                    size: simulate_batch(tree, plan, size, scenario, batch, seed, table)
                    for size in (1, 2, 4)
                }
                for size, result in results.items():
                    for key, values in result.items():
                        totals[size][key] = totals[size].get(key, 0) + int(values.sum())
                for size in (2, 4):
                    for endpoint in ("strict", "boundary"):
                        key = f"selected_false_preservation_{endpoint}"
                        reference = results[1][key].astype(bool)
                        policy = results[size][key].astype(bool)
                        discordance[size][endpoint][0] += int((policy & ~reference).sum())
                        discordance[size][endpoint][1] += int((reference & ~policy).sum())
                if time.monotonic() - started > config.get("runtime_limit_seconds", 600):
                    raise TimeoutError("frozen execution time bound reached")
            for size in (1, 2, 4):
                structurally_refused = size in scenario.get("expected_search_refusal_cohorts", [])
                if structurally_refused and (
                    totals[size]["confirmation_eligible"] != 0
                    or totals[size]["confirmation_forbidden"] != n
                    or any(totals[size][key] != 0 for key in ("preserves", "loses", "inconclusive"))
                ):
                    raise AssertionError("declared structural refusal admitted confirmation")
                summary = {
                    "plan": plan.name,
                    "scenario": scenario["name"],
                    "cohort": size,
                    "instruction_position": scenario["instruction_position"],
                    "assumption_violation": scenario.get("assumption_violation", False),
                    "unsupported": scenario.get("unsupported", False),
                    "conditional_null_structurally_inapplicable": structurally_refused,
                    "n": n,
                    "counts": totals[size],
                    "budgets": budgets(tree, plan, size),
                    "risk": {},
                    "descriptive_intervals": {},
                    "mean_cost": {},
                }
                for endpoint in ("strict", "boundary"):
                    key = f"selected_false_preservation_{endpoint}"
                    interval = cp(totals[size][key], n, alpha)
                    result = {
                        "rate": totals[size][key] / n,
                        "lower": interval[0],
                        "upper": interval[1],
                    }
                    conditional_n = totals[size][f"selected_null_{endpoint}"]
                    result["conditional_selected_null_n"] = conditional_n
                    result["conditional_confirmation_error_upper"] = (
                        cp(totals[size][key], conditional_n, alpha)[1] if conditional_n else None
                    )
                    if size != 1:
                        plus, minus = discordance[size][endpoint]
                        result.update(
                            adverse_discordance=plus,
                            favorable_discordance=minus,
                            paired_excess_upper=cp(plus, n, alpha)[1] - cp(minus, n, alpha)[0],
                        )
                    summary["risk"][endpoint] = result
                for key in (
                    "useful_success",
                    "calibration_incomplete",
                    "search_incomplete",
                    "confirmation_incomplete",
                ):
                    interval = cp(totals[size][key], n, alpha)
                    summary["descriptive_intervals"][key] = {
                        "rate": totals[size][key] / n,
                        "lower": interval[0],
                        "upper": interval[1],
                    }
                good_n = totals[size]["selected_true_good"]
                summary["conditional_false_loss"] = {
                    "selected_complete_true_good_n": good_n,
                    "false_loss_count": totals[size]["false_loss_good"],
                    "upper": cp(totals[size]["false_loss_good"], good_n, alpha)[1]
                    if good_n
                    else None,
                    "purpose": "descriptive; does not change the qualified confirmation classifier",
                }
                # Counts of screen errors/missed drift can exceed one per run. A
                # binomial confidence interval applies to the any-event indicator.
                for key in ("any_false_accept", "any_false_reject", "any_missed_drift"):
                    interval = cp(totals[size][key], n, alpha)
                    summary["descriptive_intervals"][key] = {
                        "rate": totals[size][key] / n,
                        "lower": interval[0],
                        "upper": interval[1],
                    }
                maximum = summary["budgets"]["actual_worst_workflow"]
                width = float(maximum * np.sqrt(np.log(1 / alpha) / (2 * n)))
                for key in ("provider_calls", "dispatched_attempts"):
                    mean = totals[size][key] / n
                    summary["mean_cost"][key] = {
                        "mean": mean,
                        "lower": max(0.0, mean - width),
                        "upper": min(maximum, mean + width),
                        "bound": "one-sided Hoeffding, frozen [0,worst] range",
                    }
                rows.append(summary)
    decisions = {}
    for plan in config.get("plans", ["small", "large"]):
        for size in (2, 4):
            relevant = [r for r in rows if r["cohort"] == size and r["plan"] == plan]
            arithmetic = all(r["budgets"]["budget_pass"] for r in relevant)
            risk_pass = True
            risk_unresolved = False
            for row in relevant:
                scenario = next(
                    s
                    for s in scenarios
                    if s["name"] == row["scenario"]
                    and s["instruction_position"] == row["instruction_position"]
                )
                if row["unsupported"] or scenario.get("resume") == "stale":
                    continue
                for endpoint in ("strict", "boundary"):
                    values = row["risk"][endpoint]
                    null_upper = values["conditional_confirmation_error_upper"]
                    # Prospective inapplicability follows decimal generator rates.
                    deltas = [
                        Fraction(str(scenario[x])) - Fraction(str(scenario["p0"]))
                        for x in ("ph", "pr")
                    ]
                    margin = Fraction(str(scenario["margin"]))
                    possible = (
                        any(d < -margin for d in deltas)
                        if endpoint == "strict"
                        else any(d == -margin for d in deltas) or margin == 0
                    )
                    if row["conditional_null_structurally_inapplicable"]:
                        possible = False
                    if possible and null_upper is None:
                        risk_unresolved = True
                    elif possible and null_upper > 0.055:
                        risk_pass = False
                    if values["paired_excess_upper"] > 0.005:
                        risk_pass = False
            meaningful_cost = True
            stable_comparisons = []
            for row in relevant:
                if row["scenario"] != config.get("stable_savings_scenario", "stationary"):
                    continue
                reference = next(
                    r
                    for r in rows
                    if r["cohort"] == 1
                    and r["plan"] == row["plan"]
                    and r["scenario"] == row["scenario"]
                    and r["instruction_position"] == row["instruction_position"]
                )
                bound = (
                    row["mean_cost"]["provider_calls"]["upper"]
                    - reference["mean_cost"]["provider_calls"]["lower"]
                )
                meaningful_cost &= bound < 0
                stable_comparisons.append(
                    {
                        "plan": row["plan"],
                        "instruction_position": row["instruction_position"],
                        "upper_policy_minus_lower_strict_mean_calls": bound,
                    }
                )
            meaningful_cost &= bool(stable_comparisons)
            decisions[f"{plan}/cohort{size}"] = {
                "fixed_equivalent_work_budget_pass": arithmetic,
                "risk_pass_all_declared_scenarios": risk_pass,
                "conditional_risk_unresolved": risk_unresolved,
                "meaningful_stable_expected_call_saving": meaningful_cost,
                "stable_cost_comparisons": stable_comparisons,
                "enable": arithmetic
                and risk_pass
                and not risk_unresolved
                and meaningful_cost
                and not pilot,
            }
    return {
        "config_hash": frozen["config_hash"],
        "source_manifest": frozen["source_manifest"],
        "pilot": pilot,
        "elapsed_seconds": time.monotonic() - started,
        "alpha_per_tail": alpha,
        "emitted_tails": emitted_tails,
        "expected_refusal_proofs": refusal_proofs,
        "decisions": decisions,
        "results": rows,
        "model_notes": [
            "Synthetic dispatch slots are not native runner clocks.",
            "Arm-slot schedules are independent per canonical batch replica and stable decision prefix; other rows cannot change their schedule identity.",
            "Confirmation freezes fresh arm rates at postsearch time with one new shared latent state.",
            "Time shocks violate unconditional independent-observation assumptions; strict and epoch absolute errors are both reported.",
            "Actual calls count valid provider acceptances; q failures consume dispatches but zero provider calls.",
            "No confirmation occurs after a six-comparison cap exhaustion or an operational refusal.",
        ],
    }
