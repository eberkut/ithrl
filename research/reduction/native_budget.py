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
    (Path(__file__).parent / "data/native-budget-programme.json").read_text()
)


def hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            Path(__file__),
            Path(__file__).with_name("reference.py"),
            Path(__file__).with_name("native_layout.py"),
        )
    }


FROZEN_HASHES = hashes()


async def evaluate() -> dict:
    started = time.perf_counter()
    holdout = {
        str(p): BASELINE.holdout_probabilities(p) for p in (BASELINE.P_ORIGINAL, BASELINE.P_REMOVED)
    }
    family = []
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
        minimums = {
            str(failure): min(
                row["unconditional_useful_probability"]
                for row in rows
                if row["independent_preaccept_failure_probability"] == failure
            )
            for failure in CONFIGURATION["independent_preaccept_failure_probabilities"]
        }
        maximum_attempts = 7 + cap * (8 + 9) + 2 * 124
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
                "deadline_plus_grace_upper_bound_seconds": maximum_attempts * 123,
                "fits_frozen_86400_second_command_limit_at_deadlines": maximum_attempts * 123
                <= 86400,
            }
        )
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
        "live_instruction_position_unchanged": 0,
        "evaluation_wall_seconds": time.perf_counter() - started,
        "evaluation_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "baseline_cap_10_failure_preserved": True,
        "limitations": json.loads(
            (Path(__file__).parent / "data/native-layout-results.json").read_text()
        )["limitations"],
    }
