"""Freeze, check and replay the synthetic reduction studies without a provider."""

# Assertions and stdout reports are intentional research validation interfaces.
# ruff: noqa: S101, T201

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
from pathlib import Path

import decisions
import driver
import native_budget
import native_layout
import numpy as np
import operational
import reference
import scipy
import study

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
METADATA = {
    "elapsed_seconds",
    "evaluation_wall_seconds",
    "evaluation_max_rss_kib",
    "source_manifest",
    "frozen_source_sha256",
    "frozen_source_and_configuration_sha256",
    "config_hash",
    "frozen_manifest_sha256",
    "itinerary_source_sha256",
}


def read(name: str):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


def write(output: Path, name: str, value) -> None:
    with (output / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def source_hashes() -> dict[str, str]:
    paths = [
        *ROOT.glob("*.py"),
        *ROOT.glob("*.json"),
        *ROOT.glob("*.md"),
        *ROOT.glob("data/*.json"),
    ]
    paths.append(ROOT.parents[1] / "tests/fixtures/statistics_reference.json")
    return {
        path.relative_to(ROOT).as_posix()
        if path.is_relative_to(ROOT)
        else "../../tests/fixtures/statistics_reference.json": hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(paths)
    }


def output_directory(value: str) -> Path:
    path = Path(value).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("--output must not traverse symlinks")
        if component == ROOT:
            break
    path = path.resolve()
    if path == OUTPUTS or not path.is_relative_to(OUTPUTS):
        raise ValueError("--output must name a run directory under research/reduction/outputs")
    return path


def freeze(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    write(
        output,
        "freeze.json",
        {
            "sha256": source_hashes(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    )
    print("Research sources and numerical dependencies frozen for this run.")


def verify_freeze(output: Path) -> None:
    frozen = json.loads((output / "freeze.json").read_text(encoding="utf-8"))
    if frozen["sha256"] != source_hashes():
        raise ValueError("research sources or reference data changed after freeze")
    if frozen["numpy"] != np.__version__ or frozen["scipy"] != scipy.__version__:
        raise ValueError("numerical dependency versions changed after freeze")


def compare(actual, expected, path: str = "result") -> None:
    """Require identical counts/structure and bounded floating-point differences."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        keys = set(expected) - METADATA
        assert keys == set(actual) - METADATA, (path, keys ^ (set(actual) - METADATA))
        for key in sorted(keys):
            compare(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, (list, tuple)), path
        assert len(actual) == len(expected), path
        for index, (got, wanted) in enumerate(zip(actual, expected, strict=True)):
            compare(got, wanted, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert type(actual) in (float, int), path
        assert math.isfinite(actual), path
        assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-10), (
            path,
            actual,
            expected,
        )
    elif type(expected) is int:
        assert type(actual) in (int, float), (path, actual, expected)
        assert actual == expected, (path, actual, expected)
    else:
        assert type(actual) is type(expected), (path, actual, expected)
        assert actual == expected, (path, actual, expected)


def check_saved_results() -> dict[str, int]:
    compare(study.build_tree(), read("search-tree.json"), "search-tree")
    sharing = read("sharing-results.json")
    programme = read("sharing-programme.json")
    fixtures = {
        w["name"]: driver.fixture_bytes(w["content_lengths"]) for w in programme["workloads"]
    }
    compare(fixtures, sharing["fixture_byte_maps"], "byte-fixtures")
    compare(
        driver.aggregate(sharing["cells"], programme),
        sharing["primary_aggregate"],
        "sharing-aggregate",
    )
    for cell in sharing["cells"]:
        total = cell["statuses"]
        mass = sum(
            total[key]
            for key in (
                "calibration_failed",
                "search_incomplete",
                "preserves",
                "loses",
                "inconclusive",
            )
        )
        assert math.isclose(mass, 1, rel_tol=0, abs_tol=3e-12)
    operational_results = read("operational-results.json")
    for row in operational_results["rows"]:
        counts = row["counts"]
        assert (
            sum(
                counts[key]
                for key in (
                    "preserves",
                    "loses",
                    "inconclusive",
                    "confirmation_incomplete",
                    "confirmation_forbidden",
                )
            )
            == row["n"]
        )
        assert (
            0
            <= counts["provider_calls"]
            <= counts["dispatched_attempts"]
            <= row["n"] * row["maximum_attempt_reservation"]
        )
        recalculated = operational.summarize(
            counts, row["n"], row["maximum_attempt_reservation"], operational_results["tail_alpha"]
        )
        compare(recalculated, {key: row[key] for key in recalculated}, "operational-summary")
    compare(
        operational.aggregate_primary(operational_results["rows"]),
        operational_results["primary"],
        "operational-aggregate",
    )
    vectors = json.loads(
        (ROOT.parents[1] / "tests/fixtures/statistics_reference.json").read_text()
    )["vectors"]
    for vector in vectors:
        interval = reference.newcombe_interval(
            reference.BinomialSample(vector["candidate_matches"], vector["candidate_valid"]),
            reference.BinomialSample(vector["original_matches"], vector["original_valid"]),
            confidence=vector["confidence"],
        )
        assert math.isclose(interval.lower, vector["lower"], rel_tol=0, abs_tol=1e-12)
        assert math.isclose(interval.upper, vector["upper"], rel_tol=0, abs_tol=1e-12)
    return {
        "search_paths": len(read("search-tree.json")["paths"]),
        "byte_fixtures": 16 * len(fixtures),
        "stationary_cells": len(sharing["cells"]),
        "operational_rows": len(operational_results["rows"]),
        "formula_vectors": len(vectors),
    }


def replay_eager() -> dict:
    configuration = read("eager-programme.json")
    frozen = {
        "tree": read("search-tree.json"),
        "config_hash": study.digest(configuration),
        "source_manifest": {},
    }
    assert (
        configuration["scenarios"][0]["name"]
        == configuration["stable_savings_scenario"]
        == "stationary"
    )
    result = study.evaluate(dict(configuration, scenarios=configuration["scenarios"][:1]), frozen)
    decisions = {}
    for name, value in result["decisions"].items():
        decisions[name] = {
            "fixed_equivalent_work_budget_pass": value["fixed_equivalent_work_budget_pass"],
            "meaningful_stable_expected_call_saving": value[
                "meaningful_stable_expected_call_saving"
            ],
            "stable_cost_comparisons": value["stable_cost_comparisons"],
            "cost_gate_pass": value["fixed_equivalent_work_budget_pass"]
            and value["meaningful_stable_expected_call_saving"],
            "enable": False,
            "reason": "Full risk family is required after all cost gates pass",
        }
    result.update(
        decisions=decisions,
        full_risk_family_completed=False,
        executed_scope="stationary, both plans, all three policies, all four instruction positions",
        parent_frozen_family_tails=configuration["bonferroni_tails"],
    )
    return result


def replay_sharing() -> dict:
    result = driver.evaluate(read("sharing-programme.json"))
    for cell in result["cells"]:
        del cell["terminals"]
    return result


def replay_operational() -> dict:
    programme = read("sharing-programme.json")
    parent = {
        "programme": programme,
        "snapshot": {
            "fixture_byte_maps": {
                w["name"]: driver.fixture_bytes(w["content_lengths"])
                for w in programme["workloads"]
            }
        },
    }
    return operational.evaluate(read("operational-programme.json"), parent)


def replay_native() -> dict:
    native_layout.MAX_COMPARISONS = 10
    baseline = asyncio.run(native_layout.evaluate())
    compare(baseline, read("native-layout-results.json"), "native-layout")
    family = asyncio.run(native_budget.evaluate())
    compare(family, read("native-budget-results.json"), "native-budget")
    return {"baseline": baseline, "budget_family": family}


def replay_decisions() -> dict:
    result = decisions.evaluate(read("decisions-programme.json"))
    decisions.validate(result, read("decisions-programme.json"))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "verify", "replay"))
    parser.add_argument("--output", required=True, type=output_directory)
    parser.add_argument(
        "--study", choices=("eager", "sharing", "operational", "native", "decisions")
    )
    args = parser.parse_args()
    if args.study is not None and args.mode != "replay":
        parser.error("--study is available only with replay")
    if args.mode == "freeze":
        freeze(args.output)
        return
    verify_freeze(args.output)
    if args.mode == "verify":
        if (args.output / "verification.json").exists():
            raise FileExistsError("verification already completed; choose a fresh output directory")
        checked = check_saved_results()
        compare(replay_sharing(), read("sharing-results.json"), "sharing")
        replay_native()
        decision_result = replay_decisions()
        compare(decision_result, read("decisions-results.json"), "decisions")
        checked.update(decisions.validate(decision_result))
        verify_freeze(args.output)
        write(
            args.output, "verification.json", {"checked": checked, "native_layout_replayed": True}
        )
        print(json.dumps(checked, sort_keys=True))
        return
    studies = {
        "eager": replay_eager,
        "sharing": replay_sharing,
        "operational": replay_operational,
        "native": replay_native,
        "decisions": replay_decisions,
    }
    for name in [args.study] if args.study is not None else studies:
        target = f"{name}-replay.json"
        if (args.output / target).exists():
            raise FileExistsError(
                f"{name} replay already completed; choose a fresh output directory"
            )
        print(f"Replaying {name}", flush=True)
        result = studies[name]()
        if name != "native":
            compare(result, read(f"{name}-results.json"), name)
        verify_freeze(args.output)
        write(args.output, target, result)
        print(f"{name}: matches the reference results", flush=True)


if __name__ == "__main__":
    main()
