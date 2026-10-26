"""Bounded operational baseline-sharing follow-up. No harness/provider integration."""

import time
from fractions import Fraction
from math import fsum, log, sqrt
from pathlib import Path

import driver
import numpy as np
import stationary
import stress

HERE = Path(__file__).resolve().parent
BINARY = (
    "calibration_incomplete",
    "calibration_failed",
    "search_incomplete",
    "confirmation_incomplete",
    "confirmation_forbidden",
    "confirmation_eligible",
    "preserves",
    "loses",
    "inconclusive",
    "useful_success",
    "selected_false_preservation_strict",
    "selected_false_preservation_boundary",
    "selected_null_strict",
    "selected_null_boundary",
    "expiry",
    "resume_refused",
    "same_plan_resume",
    "any_false_accept",
)
COSTS = ("provider_calls", "dispatched_attempts")


def emitted_tails(config):
    cells = (
        len(config["scenarios"])
        * len(config["instruction_positions"])
        * len(config["workloads"])
        * len(config["policies"])
    )
    # Binary two-sided endpoints; two bounded means each two tails; two conditional
    # null-error upper tails; adverse-upper/favorable-lower for each paired endpoint.
    return cells * (2 * len(BINARY) + 2 * len(COSTS) + 2) + cells // 2 * 4


def summarize(totals, n, budget, alpha):
    reference = stationary.reference()
    probabilities = {
        k: dict(
            zip(("lower", "upper"), reference.cp(totals[k], n, alpha), strict=True),
            point=totals[k] / n,
        )
        for k in BINARY
    }
    width = budget * sqrt(log(1 / alpha) / (2 * n))
    costs = {
        k: {
            "point": totals[k] / n,
            "lower": max(0.0, totals[k] / n - width),
            "upper": min(budget, totals[k] / n + width),
        }
        for k in COSTS
    }
    conditional = {}
    for endpoint in ("strict", "boundary"):
        selected = totals["selected_null_" + endpoint]
        false = totals["selected_false_preservation_" + endpoint]
        conditional[endpoint] = {
            "selected_complete_n": selected,
            "false_preserves_n": false,
            "upper": reference.cp(false, selected, alpha)[1] if selected else None,
        }
    return {
        "n": n,
        "counts": totals,
        "probabilities": probabilities,
        "mean_costs": costs,
        "conditional_error": conditional,
        "maximum_attempt_reservation": budget,
    }


def aggregate_primary(rows):
    output = {}
    for size in (1, 2):
        selected = [r for r in rows if r["policy"] == size and r["scenario"] == "healthy"]
        if len(selected) != 8 or len({(r["position"], r["workload"]) for r in selected}) != 8:
            raise ValueError("primary requires all eight weighted healthy cells")
        costs = {
            k: fsum(r["mean_costs"]["dispatched_attempts"][k] for r in selected) / 8
            for k in ("point", "lower", "upper")
        }
        useful = {
            k: fsum(r["probabilities"]["useful_success"][k] for r in selected) / 8
            for k in ("point", "lower", "upper")
        }
        output[str(size)] = {
            "charged_attempts": costs,
            "useful_probability": useful,
            "calls_per_useful": {
                "point": costs["point"] / useful["point"] if useful["point"] else None,
                "lower": costs["lower"] / useful["upper"] if useful["upper"] else None,
                "upper": costs["upper"] / useful["lower"] if useful["lower"] else None,
            },
        }
    return output


def component_errors(scenario, plan):
    """Known stationary final-rate components, independent of adaptive selection.

    The finite programme has one rate per strict-null/boundary category. A mixed
    category needs a different stratified estimator and is refused here.
    """
    shift = (
        Fraction(str(scenario.get("drift_size", 0.0)))
        if scenario.get("mode") == "abrupt"
        else Fraction()
    )

    def clip(value):
        return max(Fraction(), min(Fraction(1), Fraction(str(value)) + shift))

    original = clip(scenario["p0"])
    margin = Fraction(1, 5)
    categories = {"strict": set(), "boundary": set()}
    for field in ("ph", "pr"):
        candidate = clip(scenario[field])
        delta = candidate - original
        if delta < -margin:
            categories["strict"].add(candidate)
        elif delta == -margin:
            categories["boundary"].add(candidate)
    output = {}
    for name, rates in categories.items():
        if len(rates) > 1:
            raise ValueError(
                "mixed null components need a separately frozen stratified calculation"
            )
        error = (
            stationary.fresh_confirmation(
                plan,
                p0=float(original),
                p_candidate=float(next(iter(rates))),
                confidence=0.95,
                margin=0.2,
            )["preserves"]
            if rates
            else None
        )
        output[name] = (
            None
            if error is None
            else {
                "point": error,
                "lower": max(0.0, error - 1e-10),
                "upper": min(1.0, error + 1e-10),
            }
        )
    return output


def factorized_excess(reference, shared, components):
    """P(publication)=P(selected complete null)*P(fresh false preserve|null).

    Existing simultaneous selection bounds are reused. No extra MC tail is
    emitted; component tolerances are the parent diagnostic allowances.
    """
    output = {}
    for endpoint, component in components.items():
        if component is None:
            output[endpoint] = {"upper": 0.0, "inapplicable": True}
            continue
        key = "selected_null_" + endpoint
        difference = (
            shared["probabilities"][key]["upper"] - reference["probabilities"][key]["lower"]
        )
        output[endpoint] = {
            "upper": difference * component["upper" if difference >= 0 else "lower"],
            "inapplicable": False,
            "component": component,
        }
    return output


def score_fixture(raw, fixture, position):
    """Rescore the same potential outcomes; byte shapes never change the sampler."""
    useful = np.fromiter(
        (
            mask >= 0
            and not mask & (1 << position)
            and fixture["masks"][str(mask)]["at_least_half_editable_removed"]
            for mask in raw["selected_mask"]
        ),
        dtype=bool,
        count=len(raw["selected_mask"]),
    )
    apparent = useful & raw["preserves"].astype(bool)
    true_useful = apparent & ~(
        raw["selected_null_strict"].astype(bool) | raw["selected_null_boundary"].astype(bool)
    )
    return dict(
        raw,
        useful_success=true_useful.astype(np.int32),
        apparent_useful_success=apparent.astype(np.int32),
    )


def evaluate(config, parent, *, pilot=False):
    started = time.monotonic()
    if emitted_tails(config) > config["maximum_reported_confidence_tails"]:
        raise ValueError("emitted confidence tails exceed frozen family allowance")
    alpha = config["family_alpha"] / config["maximum_reported_confidence_tails"]
    plan = driver.study_plans(parent["programme"])[config["plan"]]
    tree = stationary.frozen_tree()
    table = stationary.reference().confirmation_table(plan.confirmation_n, 0.2)
    n = 64 if pilot else config["replicas_per_position_scenario_policy"]
    rows = []
    for si, scenario in enumerate(config["scenarios"]):
        for position in config["instruction_positions"]:
            batch_cache = {}
            for workload in config["workloads"]:
                if pilot and (scenario["name"], position, workload) != (
                    "search-change",
                    0,
                    "equal",
                ):
                    continue
                fixture = parent["snapshot"]["fixture_byte_maps"][workload]
                totals = {size: dict.fromkeys(BINARY + COSTS, 0) for size in config["policies"]}
                excess = {k: [0, 0] for k in ("strict", "boundary")}
                for offset in range(0, n, config["batch_size"]):
                    count = min(config["batch_size"], n - offset)
                    seed = config["seed"] + si * 1000000 + position * 100000 + offset
                    settings = dict(scenario, instruction_position=position, margin=0.2)
                    if offset not in batch_cache:
                        batch_cache[offset] = {
                            size: stress.simulate_batch(
                                tree, plan, size, settings, count, seed, table, fixture
                            )
                            for size in config["policies"]
                        }
                    results = {
                        size: score_fixture(raw, fixture, position)
                        for size, raw in batch_cache[offset].items()
                    }
                    for size, result in results.items():
                        budget = max(
                            stationary.lazy_path_cost(tree, terminal, plan, size)[
                                "required_workflow_attempt_budget"
                            ]
                            for terminal in tree["terminals"]
                        )
                        if np.any(
                            result["provider_calls"] > result["dispatched_attempts"]
                        ) or np.any(result["dispatched_attempts"] > budget):
                            raise AssertionError(
                                "observed work exceeds conservative accounting or path reservation"
                            )
                        for key in BINARY:
                            if np.any((result[key] != 0) & (result[key] != 1)):
                                raise AssertionError(
                                    "confidence endpoint is not a binary workflow event"
                                )
                        for key in totals[size]:
                            totals[size][key] += int(result[key].sum())
                    for endpoint in excess:
                        key = "selected_false_preservation_" + endpoint
                        original, shared = (
                            results[1][key].astype(bool),
                            results[2][key].astype(bool),
                        )
                        excess[endpoint][0] += int((shared & ~original).sum())
                        excess[endpoint][1] += int((original & ~shared).sum())
                    if time.monotonic() - started > config["runtime_ceiling_seconds"]:
                        raise TimeoutError("frozen operational runtime ceiling reached")
                pair = {}
                for size in config["policies"]:
                    budget = max(
                        stationary.lazy_path_cost(tree, terminal, plan, size)[
                            "required_workflow_attempt_budget"
                        ]
                        for terminal in tree["terminals"]
                    )
                    row = summarize(totals[size], n, budget, alpha)
                    row.update(
                        policy=size, scenario=scenario["name"], position=position, workload=workload
                    )
                    row["publication_excess_vs_strict"] = (
                        {}
                        if size == 1
                        else {
                            k: {
                                "adverse": v[0],
                                "favorable": v[1],
                                "upper": stationary.reference().cp(v[0], n, alpha)[1]
                                - stationary.reference().cp(v[1], n, alpha)[0],
                            }
                            for k, v in excess.items()
                        }
                    )
                    rows.append(row)
                    pair[size] = row
                pair[2]["factorized_publication_excess"] = factorized_excess(
                    pair[1], pair[2], component_errors(scenario, plan)
                )
    result = {
        "pilot": pilot,
        "rows": rows,
        "primary": None if pilot else aggregate_primary(rows),
        "emitted_tails": emitted_tails(config),
        "tail_alpha": alpha,
        "elapsed_seconds": time.monotonic() - started,
        "interpretation": "Bounded synthetic operational model. Safe preacceptance failures assumed independent. All dispatched attempts conservatively charged. No native or paid cost guarantee.",
    }
    if result["elapsed_seconds"] > config["runtime_ceiling_seconds"]:
        raise TimeoutError("frozen operational runtime ceiling reached")
    return result
