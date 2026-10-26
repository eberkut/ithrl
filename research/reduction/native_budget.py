"""Prospective fixed-family followup reusing the preserved cap-10 enumeration."""

# Assertions check mathematical and source-identity invariants.
# ruff: noqa: S101

from __future__ import annotations

import hashlib
import json
import resource
import time
from pathlib import Path

import native_layout as BASELINE

CONFIGURATION = json.loads(
    (Path(__file__).parent / "data/native-budget-programme.json").read_text(encoding="utf-8")
)


def hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            Path(__file__),
            Path(__file__).with_name("reference.py"),
            Path(__file__).with_name("native_layout.py"),
            Path(__file__).parent / "data/native-budget-programme.json",
            Path(__file__).parent / "data/native-layout-results.json",
            Path(__file__).with_name("provenance.json"),
        )
    }


FROZEN_HASHES = hashes()


async def evaluate() -> dict:
    started = time.perf_counter()
    baseline_contract = {
        "calibration_required_valid_minimum_matches_max_attempts": [3, 2, 7],
        "search_original_valid_max_attempts": [4, 8],
        "search_candidate_valid_max_attempts": [5, 9],
        "confirmation_each_valid_max_attempts": [100, 124],
        "confirmation_method": "newcombe-method-10",
        "one_sided_confidence": 0.95,
        "margin": BASELINE.MARGIN,
        "original_and_retained_behavior_probability": BASELINE.P_ORIGINAL,
        "instruction_removed_behavior_probability": BASELINE.P_REMOVED,
        "groups": list(BASELINE.GROUPS),
        "history_byte_weights": [1, 1, 1, 1],
        "trial_cap": BASELINE.TRIAL_CAP,
    }
    if any(CONFIGURATION[key] != value for key, value in baseline_contract.items()):
        raise ValueError("budget programme does not match the frozen baseline sampling model")
    baseline_bytes = (Path(__file__).parent / "data/native-layout-results.json").read_bytes()
    provenance = json.loads(Path(__file__).with_name("provenance.json").read_text(encoding="utf-8"))
    if (
        hashlib.sha256(baseline_bytes).hexdigest()
        != provenance["published_reference_sha256"]["data/native-layout-results.json"]
    ):
        raise ValueError("native layout baseline does not match its recorded provenance")
    baseline_result = json.loads(baseline_bytes)
    deadline = (
        CONFIGURATION["attempt_deadline_seconds"] + CONFIGURATION["cancellation_grace_seconds"]
    )
    holdout = {
        str(p): BASELINE.holdout_probabilities(p) for p in (BASELINE.P_ORIGINAL, BASELINE.P_REMOVED)
    }
    family = []
    original_cap = BASELINE.MAX_COMPARISONS
    try:
        for cap in CONFIGURATION["comparison_caps"]:
            BASELINE.MAX_COMPARISONS = cap
            rows = []
            for failure in CONFIGURATION["independent_preaccept_failure_probabilities"]:
                for position in CONFIGURATION["instruction_positions"]:
                    rows.append(
                        await BASELINE.enumerate_position(
                            position, failure, holdout["0.9"]["preserves"]
                        )
                    )
                    if (
                        time.perf_counter() - started
                        > CONFIGURATION["maximum_family_evaluation_seconds"]
                    ):
                        raise TimeoutError("native budget family exceeded its evaluation limit")
            minimums = {
                str(failure): min(
                    row["unconditional_useful_probability"]
                    for row in rows
                    if row["independent_preaccept_failure_probability"] == failure
                )
                for failure in CONFIGURATION["independent_preaccept_failure_probabilities"]
            }
            maximum_attempts = (
                CONFIGURATION["calibration_required_valid_minimum_matches_max_attempts"][2]
                + cap
                * (
                    CONFIGURATION["search_original_valid_max_attempts"][1]
                    + CONFIGURATION["search_candidate_valid_max_attempts"][1]
                )
                + 2 * CONFIGURATION["confirmation_each_valid_max_attempts"][1]
            )
            family.append(
                {
                    "comparison_cap": cap,
                    "rows": rows,
                    "minimum_unconditional_useful_probability_by_failure": minimums,
                    "every_position_at_least_0_80_by_failure": {
                        key: value >= 0.80 for key, value in minimums.items()
                    },
                    "worst_case_attempts_and_final_response_calls": maximum_attempts,
                    "worst_case_shell_possible_provider_calls": 2 * maximum_attempts,
                    "deadline_plus_grace_upper_bound_seconds": maximum_attempts * deadline,
                    "fits_frozen_86400_second_command_limit_at_deadlines": maximum_attempts
                    * deadline
                    <= CONFIGURATION["command_wall_limit_seconds"],
                }
            )
    finally:
        BASELINE.MAX_COMPARISONS = original_cap
    passing = [
        item["comparison_cap"]
        for item in family
        if item["every_position_at_least_0_80_by_failure"]["0.0"]
    ]
    assert hashes() == FROZEN_HASHES, "frozen source or configuration changed during evaluation"
    return {
        "kind": "prospective-fixed-native-layout-budget-family-result",
        "frozen_source_and_configuration_sha256": FROZEN_HASHES,
        "frozen_configuration": CONFIGURATION,
        "search_admission_probabilities_given_complete": {
            str(p): v for p, v in BASELINE.ADMISSION.items()
        },
        "holdout_outcome_probabilities_given_complete": holdout,
        "family": family,
        "selected_lowest_cap_passing_all_positions_at_zero_failures": min(passing)
        if passing
        else None,
        "live_instruction_position_unchanged": CONFIGURATION["live_instruction_position"],
        "evaluation_wall_seconds": time.perf_counter() - started,
        "evaluation_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "baseline_cap_10_failure_preserved": not baseline_result[
            "every_position_above_0_80_by_failure"
        ]["0.0"],
        "limitations": baseline_result["limitations"],
    }
