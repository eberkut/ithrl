"""Bounded lazy-two operational simulation; no provider integration.

Synthetic dispatch slots, frozen sampled counts and a compatible pause are modeled.
This is not a persistence, native clock, retry-safety or production resume proof.
Only the chosen 80/80 search and fresh 200/200 confirmation plan is supported.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import stationary


def confirmation_drift_proof(tree: dict, plan, size: int, scenario: dict) -> dict | None:
    if scenario.get("mode", "stationary") != "abrupt":
        return None
    complete = [
        plan.calibration_n + stationary.lazy_path_cost(tree, prefix, plan, size)["search_calls_q0"]
        for prefix, facts in tree["terminals"].items()
        if facts["complete"]
    ]
    if not complete or min(complete) <= scenario["drift_at"]:
        raise ValueError("unsupported drift within an eligible fresh confirmation")
    return {
        "minimum_complete_confirmation_start": min(complete),
        "drift_at": scenario["drift_at"],
        "every_eligible_confirmation_after_step": True,
    }


def validate_profile(tree: dict, plan, size: int, scenario: dict) -> None:
    if size not in (1, 2) or (plan.original_n, plan.candidate_n, plan.confirmation_n) != (
        80,
        80,
        200,
    ):
        raise ValueError("only chosen 80/80, fresh 200/200, strict/lazy-two profile is supported")
    q = scenario.get("q", 0.0)
    mode = scenario.get("mode", "stationary")
    lifetime = scenario.get("cohort_expiry_slots", 10**9)
    if isinstance(lifetime, dict) or lifetime <= 0:
        raise ValueError("one positive scalar sibling-group lifetime is required")
    if q not in (0.0, 0.01, 0.05) or mode not in ("stationary", "abrupt"):
        raise ValueError("unsupported failure or drift model")
    if q and (mode != "stationary" or lifetime < 10**9 or scenario.get("resume", "none") != "none"):
        raise ValueError("per-row varying failure/expiry/resume lifetime model is unsupported")
    if scenario.get("resume", "none") not in ("none", "same", "stale"):
        raise ValueError("unsupported synthetic resume model")
    if scenario.get("shock", 0.0) != 0:
        raise ValueError("shared latent shock outside this bounded study")
    confirmation_drift_proof(tree, plan, size, scenario)


def simulate_batch(tree, plan, size, scenario, n, seed, table, fixture):
    """Return per-replica arrays; sample no unvisited candidate suffix.

    Valid provider acceptances and all dispatched potential calls are separate.
    The q model assumes independent failures before acceptance; errors never enter
    behavioral counts. All first/second candidate arms complete before their own
    frozen screen; final confirmation requires an operationally complete search.
    """
    validate_profile(tree, plan, size, scenario)
    reference = stationary.reference()
    rng = np.random.default_rng(seed)
    times = np.zeros(n, dtype=np.int32)
    prefixes = np.full(n, "", dtype="<U7")
    active = np.ones(n, dtype=bool)
    names = (
        "provider_calls",
        "dispatched_attempts",
        "charged_potential_calls",
        "preacceptance_failures",
        "useful_success",
        "apparent_useful_success",
        "calibration_failed",
        "calibration_incomplete",
        "search_incomplete",
        "confirmation_incomplete",
        "confirmation_forbidden",
        "confirmation_eligible",
        "preserves",
        "loses",
        "inconclusive",
        "selected_false_preservation_strict",
        "selected_false_preservation_boundary",
        "selected_null_strict",
        "selected_null_boundary",
        "expiry",
        "resume_refused",
        "same_plan_resume",
        "any_false_accept",
        "comparisons",
        "original_arms",
        "candidate_arms",
        "uncharged_unsampled_calls",
    )
    metrics = {name: np.zeros(n, dtype=np.int32) for name in names}
    position = scenario.get("instruction_position", 0)
    lifetime = scenario.get("cohort_expiry_slots", 10**9)
    margin = Fraction(str(scenario.get("margin", 0.2)))

    def charge(index, block):
        matches, valid, attempts, end, shock = block
        metrics["provider_calls"][index] += valid.sum(axis=0)
        metrics["dispatched_attempts"][index] += attempts.sum(axis=0)
        metrics["charged_potential_calls"][index] += attempts.sum(axis=0)
        times[index] = end
        return matches, valid, attempts, shock

    calibration_scenario = dict(scenario, resume="none", deadline_slots=10**9)
    indices = np.arange(n)
    calibration = reference.sample_cohort(
        rng,
        calibration_scenario,
        times.copy(),
        [(plan.calibration_n, plan.calibration_a, 0)],
        seed=seed,
        schedule_identity="stress:calibration",
        total_rows=n,
    )
    matches, valid, _, _ = charge(indices, calibration)
    incomplete = valid[0] != plan.calibration_n
    failed = ~incomplete & (matches[0] < plan.calibration_min)
    metrics["calibration_incomplete"] += incomplete
    metrics["calibration_failed"] += failed
    active &= ~(incomplete | failed)

    def mask(member):
        return sum(1 << reference.GROUPS.index(g) for g in tree["nodes"][member]["effective"])

    while active.any():
        for prefix in np.unique(prefixes[active]):
            index = np.flatnonzero(active & (prefixes == prefix))
            if prefix in tree["terminals"]:
                active[index] = False
                continue
            members = reference.cohort(tree, str(prefix), size)
            start = times[index].copy()
            resume = scenario.get("resume", "none")
            if resume == "same" and metrics["same_plan_resume"][index].any():
                if not metrics["same_plan_resume"][index].all():
                    raise ValueError("unsupported mixed resume state within one prefix group")
                resume = "none"
            group_scenario = dict(scenario, resume=resume, deadline_slots=lifetime)
            first = reference.sample_cohort(
                rng,
                group_scenario,
                times[index].copy(),
                [
                    (plan.original_n, plan.original_a, 0),
                    (plan.candidate_n, plan.candidate_a, mask(members[0])),
                ],
                seed=seed,
                schedule_identity=f"stress:{prefix}:first",
                row_indices=index,
                total_rows=n,
            )
            first_matches, first_valid, _, first_shock = charge(index, first)
            metrics["original_arms"][index] += 1
            metrics["candidate_arms"][index] += 1
            if resume == "same":
                metrics["same_plan_resume"][index] += 1
            if resume == "stale":
                metrics["resume_refused"][index] += 1
            expired = times[index] - start >= lifetime
            incomplete = (
                first_valid != np.array([plan.original_n, plan.candidate_n])[:, None]
            ).any(axis=0)
            stopped = expired | incomplete | (resume == "stale")
            metrics["expiry"][index] += expired
            metrics["search_incomplete"][index[stopped]] = 1
            active[index[stopped]] = False
            pending = np.flatnonzero(~stopped)
            for j, member in enumerate(members):
                if not len(pending):
                    break
                ids = index[pending]
                if j == 0:
                    candidate_matches = first_matches[1, pending]
                    shock = first_shock[pending]
                else:
                    elapsed = times[ids] - start[pending]
                    if lifetime < 10**9 and not np.all(elapsed == elapsed[0]):
                        raise ValueError("per-row residual sibling lifetime is unsupported")
                    remaining = lifetime - int(elapsed[0]) if lifetime < 10**9 else lifetime
                    if remaining <= 0:
                        metrics["expiry"][ids] += 1
                        metrics["search_incomplete"][ids] = 1
                        active[ids] = False
                        break
                    next_scenario = dict(scenario, resume="none", deadline_slots=remaining)
                    block = reference.sample_cohort(
                        rng,
                        next_scenario,
                        times[ids].copy(),
                        [(plan.candidate_n, plan.candidate_a, mask(member))],
                        seed=seed,
                        schedule_identity=f"stress:{prefix}:candidate:{member}",
                        row_indices=ids,
                        total_rows=n,
                    )
                    candidate, candidate_valid, _, shock = charge(ids, block)
                    metrics["candidate_arms"][ids] += 1
                    expired = times[ids] - start[pending] >= lifetime
                    stopped = expired | (candidate_valid[0] != plan.candidate_n)
                    metrics["expiry"][ids] += expired
                    metrics["search_incomplete"][ids[stopped]] = 1
                    active[ids[stopped]] = False
                    pending = pending[~stopped]
                    ids = ids[~stopped]
                    candidate_matches = candidate[0, ~stopped]
                    shock = shock[~stopped]
                    if not len(ids):
                        break
                cross = (
                    candidate_matches.astype(np.int64) * plan.original_n
                    - first_matches[0, pending].astype(np.int64) * plan.candidate_n
                )
                admit = (
                    cross * margin.denominator
                    >= -margin.numerator * plan.original_n * plan.candidate_n
                )
                bad, _, _ = reference.truth(scenario, times[ids], mask(member), shock)
                metrics["any_false_accept"][ids] |= (admit & bad).astype(np.int32)
                metrics["comparisons"][ids] += 1
                prefixes[ids] = member + "0"
                prefixes[ids[admit]] = member + "1"
                pending = pending[~admit]

    eligible = np.array(
        [p in tree["terminals"] and tree["terminals"][p]["complete"] for p in prefixes]
    )
    eligible &= (
        metrics["calibration_failed"]
        + metrics["calibration_incomplete"]
        + metrics["search_incomplete"]
    ) == 0
    metrics["confirmation_eligible"] = eligible.astype(np.int32)
    metrics["confirmation_forbidden"] = (~eligible).astype(np.int32)
    selected_mask = np.full(n, -1, dtype=np.int16)
    for prefix, facts in tree["terminals"].items():
        selected_mask[eligible & (prefixes == prefix)] = sum(
            1 << reference.GROUPS.index(g) for g in facts["removed"]
        )
    proof = confirmation_drift_proof(tree, plan, size, scenario)
    final_scenario = dict(scenario, resume="none", deadline_slots=10**9, force_slots=False)
    if proof is not None:
        final_scenario.update(mode="stationary", drift_size=0.0)
        for field in ("p0", "ph", "pr"):
            final_scenario[field] = float(
                np.clip(scenario[field] + scenario["drift_size"], 0.0, 1.0)
            )
    for removed in range(16):
        ids = np.flatnonzero(eligible & (selected_mask == removed))
        if not len(ids):
            continue
        fresh_start = times[ids].copy()
        if proof is not None and np.any(fresh_start <= proof["drift_at"]):
            raise AssertionError("fresh confirmation began before the proved final drift state")
        # Canonical potential fresh outcomes for every replica stabilize coupling
        # across strict/lazy selection membership. The mask stream is independent
        # of search and subgroup size/order. Potential draws outside eligible ids
        # are simulation random numbers, never dispatched or charged calls.
        final_rng = np.random.default_rng(np.random.SeedSequence([seed, 0xC0FFEE, removed]))
        potential_start = np.full(
            n, proof["drift_at"] + 1 if proof is not None else 0, dtype=np.int32
        )
        potential = reference.sample_cohort(
            final_rng,
            final_scenario,
            potential_start.copy(),
            [
                (plan.confirmation_n, plan.confirmation_a, 0),
                (plan.confirmation_n, plan.confirmation_a, removed),
            ],
            seed=seed,
            schedule_identity=f"stress:confirmation:{removed}",
            total_rows=n,
        )
        block = (
            potential[0][:, ids],
            potential[1][:, ids],
            potential[2][:, ids],
            fresh_start + potential[3][ids] - potential_start[ids],
            potential[4][ids],
        )
        matches, valid, _, shock = charge(ids, block)
        full = (valid == plan.confirmation_n).all(axis=0)
        codes = table[matches[0], matches[1]]
        metrics["confirmation_incomplete"][ids] = (~full).astype(np.int32)
        for code, name in ((1, "preserves"), (2, "loses"), (3, "inconclusive")):
            metrics[name][ids] = ((codes == code) & full).astype(np.int32)
        bad, boundary, _ = reference.truth(scenario, fresh_start, removed, shock)
        preserving = (codes == 1) & full
        metrics["selected_null_strict"][ids] = (bad & full).astype(np.int32)
        metrics["selected_null_boundary"][ids] = (boundary & full).astype(np.int32)
        metrics["selected_false_preservation_strict"][ids] = (bad & preserving).astype(np.int32)
        metrics["selected_false_preservation_boundary"][ids] = (boundary & preserving).astype(
            np.int32
        )
        useful = (
            not removed & (1 << position)
            and fixture["masks"][str(removed)]["at_least_half_editable_removed"]
        )
        metrics["apparent_useful_success"][ids] = (preserving & useful).astype(np.int32)
        metrics["useful_success"][ids] = (preserving & useful & ~bad & ~boundary).astype(np.int32)
    metrics["preacceptance_failures"] = metrics["dispatched_attempts"] - metrics["provider_calls"]
    metrics["selected_mask"] = selected_mask
    metrics["elapsed_slots"] = times
    if np.any(metrics["provider_calls"] > metrics["dispatched_attempts"]):
        raise AssertionError("valid acceptances exceed dispatched potential calls")
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
        raise AssertionError("final operational/statistical outcomes do not partition replicas")
    return metrics
