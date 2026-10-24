"""Replay a small, deterministic subset of the audited synthetic study."""

# Assertions are intentional scientific checks; output is aggregate-only.
# ruff: noqa: S101, T201

from __future__ import annotations

import argparse
import csv
import json
import math

import numpy as np
from study import (
    ROOT,
    WORKFLOWS,
    method_checks,
    output_directory,
    simulate,
    verify_freeze,
    write_result,
)


def selected_summary(rows: list[dict]) -> list[dict]:
    """Summarize equally weighted cells, without inventing pooled cost quantiles."""
    summaries = []
    for invalid in (0.0, 0.05, 0.1, 0.2):
        cells = [row for row in rows if row["invalid"] == invalid]
        summaries.append(
            {
                "invalid": invalid,
                "success_min": min(row["workflow_success"] for row in cells),
                "success_max": max(row["workflow_success"] for row in cells),
                "mean_calls": float(np.mean([row["calls_mean"] for row in cells])),
                "largest_p95": max(row["calls_p95"] for row in cells),
                "calls_max": max(row["calls_max"] for row in cells),
            }
        )
    return summaries


def validate_selected_cells(expected: dict) -> None:
    retained = expected["selected_cells"]
    with (ROOT / retained["file"]).open(newline="", encoding="utf-8") as stream:
        rows = [
            retained["common"] | {key: json.loads(value) for key, value in row.items()}
            for row in csv.DictReader(stream)
        ]
    assert len(rows) == 64
    assert {(row["invalid"], row["location"], row["essential_mask"]) for row in rows} == {
        (invalid, location, 1 << atom)
        for invalid in (0.0, 0.05, 0.1, 0.2)
        for location in range(4)
        for atom in range(4)
    }
    for row in rows:
        count = row["repetitions"]
        assert sum(row["terminal_counts"].values()) == count
        assert all(
            isinstance(value, int) and value >= 0 for value in row["terminal_counts"].values()
        )
        assert row["invalid_original"] == row["invalid"]
        assert 0 <= row["calls_mean"] <= row["calls_max"] <= row["budget"]
        assert 0 <= row["calls_p50"] <= row["calls_p95"] <= row["calls_max"]
        assert 0 <= row["simultaneous_lower"] <= row["workflow_success"] <= row["confirmed"] <= 1
        assert row["workflow_success"] == row["simplified_confirmed"]
        assert 0 <= row["simplification_complete"] <= 1
        assert row["confirmed_without_effect"] == 0
        assert math.isclose(
            row["confirmed"] * count,
            row["terminal_counts"].get("confirmed-change", 0),
            abs_tol=1e-10,
            rel_tol=0,
        )
    assert selected_summary(rows) == retained["by_invalid"]
    selected = expected["analysis"]["selected"]
    aggregates = {
        "minimum_single_success": min(row["workflow_success"] for row in rows),
        "minimum_single_lower": min(row["simultaneous_lower"] for row in rows),
        "mean_calls": float(np.mean([row["calls_mean"] for row in rows])),
        "largest_p95": max(row["calls_p95"] for row in rows),
    }
    assert all(
        math.isclose(value, selected[key], abs_tol=1e-12, rel_tol=0)
        for key, value in aggregates.items()
    )
    for replay in expected["replay_cells"]:
        if replay["workflow"] == "adaptive-shrink":
            assert replay in rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=output_directory)
    options = parser.parse_args()
    verify_freeze(options.output)
    expected = json.loads((ROOT / "results.json").read_text())
    validate_selected_cells(expected)
    method_checks(options.output)
    method = json.loads((options.output / "method-results.json").read_text())
    for name in ("reference_count", "n10_zero_upper", "n10_all_lower"):
        assert math.isclose(method[name], expected["method"][name], abs_tol=1e-12, rel_tol=0)
    assert method["maximum_api_error"] < 1e-12
    assert len(method["exact_grid"]) == expected["method"]["exact_grid_count"]
    for actual, saved in zip(
        method["dense_boundary"], expected["method"]["dense_boundary"], strict=True
    ):
        assert actual.keys() == saved.keys()
        assert all(math.isclose(actual[k], saved[k], abs_tol=1e-12, rel_tol=0) for k in saved)

    for saved in expected["replay_cells"]:
        location, essential = saved["location"], saved["essential_mask"]
        geometry_index = location * 4 + essential.bit_length() - 1
        seed = np.random.SeedSequence(
            [
                2026102301,
                saved["budget"],
                saved["n_screen"],
                saved["n_confirm"],
                (0.0, 0.05, 0.1, 0.2).index(saved["invalid"]),
                geometry_index,
                WORKFLOWS.index(saved["workflow"]),
            ]
        )
        actual = simulate(
            **{
                k: saved[k]
                for k in ("budget", "n_screen", "n_confirm", "location", "invalid", "workflow")
            },
            essential=essential,
            seed=seed,
        )
        assert actual == {key: value for key, value in saved.items() if key != "geometry"}

    selected = expected["analysis"]["selected"]
    outcome = simulate(
        **{k: selected[k] for k in ("budget", "n_screen", "n_confirm")},
        location=0,
        essential=1,
        invalid=0.25,
        invalid_original=0.25,
        workflow="adaptive-shrink",
        p_original=2 / 3,
        p_essential=1 / 3,
        p_null_mutant=1 / 3,
        seed=np.random.SeedSequence([2026102302, 1, 1]),
    )
    saved_outcome = expected["sensitivity"]["rows"][-1]
    assert saved_outcome["scenario"] == "outcome-dependent-validity"
    assert outcome == {k: v for k, v in saved_outcome.items() if k != "scenario"}
    result = {
        "method_reference_count": method["reference_count"],
        "same_seed_replay_cells": len(expected["replay_cells"]),
        "replay_campaigns": (len(expected["replay_cells"]) + 1)
        * expected["analysis"]["repetitions"],
        "outcome_dependent_declarations": outcome["terminal_counts"]["confirmed-change"],
        "historical_aggregates_match": True,
        "retained_selected_cells": 64,
    }
    write_result(options.output, "validation-results.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
