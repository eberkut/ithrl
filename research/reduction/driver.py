"""Bounded stationary baseline-sharing and allocation comparison."""

from __future__ import annotations

import hashlib
import json
import time
from fractions import Fraction
from math import fsum, isfinite
from pathlib import Path

import stationary

HERE = Path(__file__).resolve().parent


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def fixture_bytes(content_lengths: list[int]) -> dict:
    """Encode the four independent synthetic histories and all sixteen removals."""
    if len(content_lengths) != 4 or any(type(n) is not int or n <= 0 for n in content_lengths):
        raise ValueError("four positive synthetic content lengths required")
    items = [
        {"role": "user", "content": str(i) * length} for i, length in enumerate(content_lengths)
    ]
    masks = {}
    for mask in range(16):
        removed = [g for i, g in enumerate(stationary.reference().GROUPS) if mask & (1 << i)]
        request = {
            "model": "fake",
            "input": [item for i, item in enumerate(items) if not mask & (1 << i)],
        }
        encoded = canonical(request)
        masks[str(mask)] = {
            "removed": removed,
            "request_bytes": len(encoded),
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
        }
    full = masks["0"]["request_bytes"]
    editable = full - masks["15"]["request_bytes"]
    for record in masks.values():
        record["removed_editable_bytes"] = full - record["request_bytes"]
        record["at_least_half_editable_removed"] = 2 * record["removed_editable_bytes"] >= editable
    return {
        "content_lengths": content_lengths,
        "full_request_bytes": full,
        "all_removed_request_bytes": masks["15"]["request_bytes"],
        "editable_request_bytes": editable,
        "masks": masks,
        "encoding": "UTF-8; sorted keys; compact JSON; ensure_ascii=False",
    }


def removal_mask(removed) -> int:
    return sum(1 << stationary.reference().GROUPS.index(g) for g in removed)


def apparent_useful(terminal: dict, fixture: dict, position: int) -> bool:
    return (
        terminal["complete"]
        and stationary.reference().GROUPS[position] in terminal["retained"]
        and fixture["masks"][str(removal_mask(terminal["removed"]))][
            "at_least_half_editable_removed"
        ]
    )


def decimal_effect(removed, scenario: dict, position: int) -> Fraction:
    if not removed:
        return Fraction()
    field = "pr" if stationary.reference().GROUPS[position] in removed else "ph"
    return Fraction(str(scenario[field])) - Fraction(str(scenario["p0"]))


def ratio_envelope(
    calls: float, probability: float, calls_tolerance: float, probability_tolerance: float
) -> dict:
    """Diagnostic allowance envelope; no certified accumulated-error claim."""
    point = calls / probability if probability > 0 else None
    if probability <= probability_tolerance:
        return {"point": point, "lower": None, "upper": None, "resolved": False}
    return {
        "point": point,
        "lower": max(0.0, calls - calls_tolerance) / min(1.0, probability + probability_tolerance),
        "upper": (calls + calls_tolerance) / (probability - probability_tolerance),
        "resolved": True,
    }


def conditional_envelope(numerator: float, denominator: float, tolerance: float) -> dict:
    point = numerator / denominator if denominator > 0 else None
    if denominator <= tolerance:
        return {"point": point, "lower": None, "upper": None, "resolved": False}
    return {
        "point": point,
        "lower": max(0.0, numerator - tolerance) / min(1.0, denominator + tolerance),
        "upper": min(1.0, (numerator + tolerance) / (denominator - tolerance)),
        "resolved": True,
    }


def tolerances(programme: dict) -> tuple[float, float]:
    numeric = programme["numerical_validation"]
    probability = numeric["probability_tolerance"]
    calls = numeric["calls_tolerance"]
    if any(not isfinite(x) or x <= 0 for x in (probability, calls)):
        raise ValueError("positive finite diagnostic numerical allowances required")
    return probability, calls


def summarize(raw: dict, fixture: dict, scenario: dict, position: int, programme: dict) -> dict:
    probability_tolerance, calls_tolerance = tolerances(programme)
    useful = apparent = null = false_null = boundary = false_boundary = 0.0
    margin = Fraction(str(programme["margin"]))
    for terminal in raw["terminals"].values():
        confirmation = terminal["fresh_confirmation"]
        if confirmation is None:
            continue
        mass = terminal["workflow_probability"]
        preserving = mass * confirmation["preserves"]
        effect = decimal_effect(terminal["removed"], scenario, position)
        if apparent_useful(terminal, fixture, position):
            apparent += preserving
            # The boundary is part of the noninferiority null for this programme.
            if effect > -margin:
                useful += preserving
        if effect < -margin:
            null += mass
            false_null += preserving
        elif effect == -margin:
            boundary += mass
            false_boundary += preserving
    return {
        "useful_probability": useful,
        "apparent_useful_probability": apparent,
        "selected_null_probability": null,
        "selected_null_false_preservation_probability": false_null,
        "conditional_null_error": conditional_envelope(false_null, null, probability_tolerance),
        "selected_boundary_probability": boundary,
        "selected_boundary_false_preservation_probability": false_boundary,
        "conditional_boundary_error": conditional_envelope(
            false_boundary, boundary, probability_tolerance
        ),
        "expected": raw["expected"],
        "statuses": raw["statuses"],
        "path_maxima": raw["path_maxima"],
        "calls_per_useful_result": ratio_envelope(
            raw["expected"]["total_calls"], useful, calls_tolerance, probability_tolerance
        ),
        "terminals": raw["terminals"],
    }


def validate_weights(programme: dict) -> None:
    families = (
        programme["instruction_position_weights"],
        [w["primary_weight"] for w in programme["workloads"]],
        list(programme["scenario_weights_for_primary_benefit"].values()),
    )
    for weights in families:
        if any(not isfinite(w) or w < 0 for w in weights) or abs(fsum(weights) - 1) > 1e-12:
            raise ValueError("each primary weight family must be nonnegative and sum to one")
    if len(programme["instruction_position_weights"]) != 4:
        raise ValueError("four instruction-position weights required")
    declared = {s["name"] for s in programme["scenarios"]}
    if not set(programme["scenario_weights_for_primary_benefit"]) <= declared:
        raise ValueError("weighted scenario absent from programme")


def policies_for_plan(programme: dict, name: str) -> list[int]:
    if name not in programme["plans"]:
        raise ValueError("policy requested for an undeclared plan")
    overrides = programme.get("policy_overrides", {})
    if not set(overrides) <= set(programme["plans"]):
        raise ValueError("policy override references an undeclared plan")
    policies = overrides.get(name, programme["policies"])
    if (
        not isinstance(policies, list)
        or not policies
        or len(policies) != len(set(policies))
        or any(type(p) is not int or p not in (1, 2, 4) for p in policies)
    ):
        raise ValueError("declared policies must be unique supported cohorts 1/2/4")
    return policies


def study_plans(programme: dict) -> dict:
    """Resolve the frozen reference plans and the declared allocation ablation."""
    reference = stationary.reference()
    plans = {p.name: p for p in reference.PLANS}
    additional_names = set()
    for spec in programme.get("additional_plans", []):
        try:
            plan = reference.Plan(**spec)
        except TypeError as error:
            raise ValueError("invalid additional plan specification") from error
        if not isinstance(plan.name, str) or not plan.name or plan.name in plans:
            raise ValueError("duplicate or invalid plan name")
        for target, limit in (
            (plan.calibration_n, plan.calibration_a),
            (plan.original_n, plan.original_a),
            (plan.candidate_n, plan.candidate_a),
            (plan.confirmation_n, plan.confirmation_a),
        ):
            if type(target) is not int or type(limit) is not int or target <= 0 or limit < target:
                raise ValueError(
                    "plan targets must be positive integers with sufficient attempt limits"
                )
        if (
            type(plan.calibration_min) is not int
            or not 0 <= plan.calibration_min <= plan.calibration_n
        ):
            raise ValueError("calibration minimum outside its valid target")
        plans[plan.name] = plan
        additional_names.add(plan.name)
    declared = programme["plans"]
    if len(declared) != len(set(declared)) or any(name not in plans for name in declared):
        raise ValueError("declared plans must be unique supported names")
    for name in declared:
        policies = policies_for_plan(programme, name)
        if name in additional_names and policies != [1]:
            raise ValueError("additional allocation ablation supports only strict cohort 1")
    return plans


def aggregate(cells: list[dict], programme: dict) -> dict:
    validate_weights(programme)
    study_plans(programme)
    probability_tolerance, calls_tolerance = tolerances(programme)
    weights = {w["name"]: w["primary_weight"] for w in programme["workloads"]}
    output = {}
    for plan in programme["plans"]:
        for policy in policies_for_plan(programme, plan):
            selected = [c for c in cells if c["plan"] == plan and c["policy"] == policy]
            expected_keys = {
                (position, workload["name"], scenario)
                for position in programme["instruction_positions"]
                for workload in programme["workloads"]
                if workload["primary_weight"] > 0
                for scenario, weight in programme["scenario_weights_for_primary_benefit"].items()
                if weight > 0
                if programme["instruction_position_weights"][position] > 0
            }
            actual_keys = [
                (c["instruction_position"], c["workload"], c["scenario"])
                for c in selected
                if weights[c["workload"]] > 0
                and programme["instruction_position_weights"][c["instruction_position"]] > 0
                and programme["scenario_weights_for_primary_benefit"].get(c["scenario"], 0.0) > 0
            ]
            if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
                raise ValueError(
                    "primary aggregation requires every frozen weighted cell exactly once"
                )
            weighted_calls = weighted_probability = weight_mass = 0.0
            for cell in selected:
                weight = (
                    weights[cell["workload"]]
                    * programme["instruction_position_weights"][cell["instruction_position"]]
                    * programme["scenario_weights_for_primary_benefit"].get(cell["scenario"], 0.0)
                )
                weight_mass += weight
                weighted_calls += weight * cell["expected"]["total_calls"]
                weighted_probability += weight * cell["useful_probability"]
            if abs(weight_mass - 1) > 1e-12:
                raise ValueError(
                    "primary aggregation requires every frozen weighted cell exactly once"
                )
            output[f"{plan}/cohort{policy}"] = {
                "weighted_expected_calls": weighted_calls,
                "weighted_useful_probability": weighted_probability,
                "calls_per_useful_result": ratio_envelope(
                    weighted_calls, weighted_probability, calls_tolerance, probability_tolerance
                ),
            }
    return output


def evaluate(programme: dict, *, pilot: bool = False) -> dict:
    started = time.monotonic()
    frozen = {
        "snapshot": {
            "fixture_byte_maps": {
                w["name"]: fixture_bytes(w["content_lengths"]) for w in programme["workloads"]
            }
        }
    }
    plans = study_plans(programme)
    tree = stationary.frozen_tree()
    cells = []
    cache = {}
    for plan_name in programme["plans"]:
        for policy in policies_for_plan(programme, plan_name):
            for position in programme["instruction_positions"]:
                for workload in programme["workloads"]:
                    for scenario in programme["scenarios"]:
                        if pilot and (
                            plan_name,
                            policy,
                            position,
                            workload["name"],
                            scenario["name"],
                        ) != ("large", 1, 0, "equal", "stationary"):
                            continue
                        key = (plan_name, policy, position, scenario["name"])
                        if key not in cache:
                            cache[key] = stationary.run_exact(
                                plan=plans[plan_name],
                                cohort_size=policy,
                                p0=scenario["p0"],
                                ph=scenario["ph"],
                                pr=scenario["pr"],
                                instruction_position=position,
                                tree=tree,
                                margin=programme["margin"],
                                confidence=programme["confirmation_confidence"],
                            )
                        cell = summarize(
                            cache[key],
                            frozen["snapshot"]["fixture_byte_maps"][workload["name"]],
                            scenario,
                            position,
                            programme,
                        )
                        cell.update(
                            plan=plan_name,
                            policy=policy,
                            instruction_position=position,
                            workload=workload["name"],
                            scenario=scenario["name"],
                        )
                        cells.append(cell)
                        if (
                            time.monotonic() - started
                            > programme["numerical_validation"]["runtime_ceiling_seconds"]
                        ):
                            raise TimeoutError("frozen stationary runtime ceiling reached")
    if pilot and len(cells) != 1:
        raise ValueError("frozen programme lacks the declared pilot cell")
    result = {
        "pilot": pilot,
        "cells": cells,
        "primary_aggregate": None if pilot else aggregate(cells, programme),
        "fixture_byte_maps": frozen["snapshot"]["fixture_byte_maps"],
        "elapsed_seconds": time.monotonic() - started,
        "numeric_interpretation": "finite float diagnostic allowances; no certified accumulated-error claim",
        "scope": "stationary q=0 synthetic call counts; no native/provider-token/monetary cost or production sharing claim",
    }
    result["elapsed_seconds"] = time.monotonic() - started
    if result["elapsed_seconds"] > programme["numerical_validation"]["runtime_ceiling_seconds"]:
        raise TimeoutError("frozen stationary runtime ceiling reached")
    return result
