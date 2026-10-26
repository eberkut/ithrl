"""Frozen finite native-layout study; credential-free, no runner/provider execution."""

# Assertions check mathematical and source-identity invariants.
# ruff: noqa: S101

from __future__ import annotations

import hashlib
import math
import resource
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from reference import (
    BinomialSample,
    FixedSampleOutcome,
    _sampled_screen_action,
    classify_fixed_sample,
    run_search,
    trial_key,
)

MAX_COMPARISONS = 10
TRIAL_CAP = 10_000
MARGIN = 0.20
P_ORIGINAL = 0.90
P_REMOVED = 0.10
GROUPS = ("history0", "history1", "history2", "history3", "trigger")


def hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), Path(__file__).with_name("reference.py"))
    }


FROZEN_HASHES = hashes()


def pmf(n: int, p: float) -> list[float]:
    return [math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(n + 1)]


def completion(required: int, maximum: int, failure: float) -> float:
    return math.fsum(pmf(maximum, 1 - failure)[required:])


def admission_probability(candidate_p: float) -> float:
    weights = []
    for original_matches, original_weight in enumerate(pmf(4, P_ORIGINAL)):
        for candidate_matches, candidate_weight in enumerate(pmf(5, candidate_p)):
            evidence = SimpleNamespace(
                eligible=True,
                complete=True,
                original_counts=SimpleNamespace(matches=original_matches, valid=4),
                candidate_counts=SimpleNamespace(matches=candidate_matches, valid=5),
                plan=SimpleNamespace(reduction=SimpleNamespace(margin=MARGIN)),
            )
            if _sampled_screen_action(evidence) == "admit":
                weights.append(original_weight * candidate_weight)
    return math.fsum(weights)


def holdout_probabilities(candidate_p: float) -> dict[str, float]:
    outcomes = {str(outcome): [] for outcome in FixedSampleOutcome}
    for original_matches, original_weight in enumerate(pmf(100, P_ORIGINAL)):
        for candidate_matches, candidate_weight in enumerate(pmf(100, candidate_p)):
            verdict = classify_fixed_sample(
                BinomialSample(candidate_matches, 100),
                BinomialSample(original_matches, 100),
                required_candidate=100,
                required_original=100,
                confidence=0.95,
                margin=MARGIN,
            )
            outcomes[str(verdict.outcome)].append(original_weight * candidate_weight)
    return {key: math.fsum(values) for key, values in outcomes.items()}


@dataclass(frozen=True)
class Trial:
    trial_key: str
    effective_removals: tuple[str, ...]
    accepts_removal: bool
    allows_search_continuation: bool = True


class BranchNeeded(Exception):
    def __init__(self, candidate_p: float) -> None:
        self.candidate_p = candidate_p


async def replay(prefix: tuple[bool, ...], instruction: str):
    sampled = 0

    async def classify(**arguments):
        nonlocal sampled
        base = arguments["base_removals"]
        requested = arguments["requested_removals"]
        phase = arguments["phase"]
        completed = arguments["completed_trials"]
        if len(completed) >= TRIAL_CAP:
            return None
        key = trial_key(phase, base, requested)
        effective = base | frozenset(requested)
        if "trigger" in effective:
            return Trial(key, tuple(sorted(base)), False)
        if sampled >= MAX_COMPARISONS:
            return None
        if sampled >= len(prefix):
            raise BranchNeeded(P_REMOVED if instruction in effective else P_ORIGINAL)
        accepts = prefix[sampled]
        sampled += 1
        return Trial(key, tuple(sorted(effective)), accepts)

    outcome = await run_search(
        None,
        removable=list(GROUPS),
        classify=classify,
        prior_trials=deque(),
        publish=False,
        publish_checkpoint=lambda *_: None,
    )
    assert sampled == len(prefix), "forced decisions must all be consumed exactly once"
    return outcome


async def enumerate_position(position: int, failure: float, holdout_pass: float):
    instruction = GROUPS[position]
    block_complete = completion(4, 8, failure) * completion(5, 9, failure)
    stack = [((), 1.0)]
    useful = []
    completed = []
    retained = []
    search_incomplete = []
    ceiling_incomplete = []
    terminal_mass = []
    max_comparisons = 0
    max_trials = 0
    branches = 0
    while stack:
        prefix, probability = stack.pop()
        try:
            outcome = await replay(prefix, instruction)
        except BranchNeeded as branch:
            branches += 1
            admission = ADMISSION[branch.candidate_p]
            search_incomplete.append(probability * (1 - block_complete))
            stack.append(((*prefix, True), probability * block_complete * admission))
            stack.append(((*prefix, False), probability * block_complete * (1 - admission)))
            continue
        terminal_mass.append(probability)
        max_comparisons = max(max_comparisons, len(prefix))
        max_trials = max(max_trials, len(outcome.trials))
        if not outcome.complete:
            ceiling_incomplete.append(probability)
            continue
        completed.append(probability)
        instruction_retained = instruction not in outcome.removed
        if instruction_retained:
            retained.append(probability)
        removed_histories = sum(group in outcome.removed for group in GROUPS[:4])
        if instruction_retained and removed_histories >= 2:
            useful.append(probability)
    mass = math.fsum(terminal_mass) + math.fsum(search_incomplete)
    assert math.isclose(mass, 1.0, rel_tol=0, abs_tol=1e-12), mass
    calibration = completion(3, 7, failure) * math.fsum(pmf(3, P_ORIGINAL)[2:])
    holdout_complete = completion(100, 124, failure) ** 2
    search_useful = math.fsum(useful)
    return {
        "instruction_position": position,
        "independent_preaccept_failure_probability": failure,
        "calibration_ready_probability": calibration,
        "search_block_completion_probability": block_complete,
        "search_final_sweep_complete_probability_given_calibration": math.fsum(completed),
        "instruction_retained_and_sweep_complete_probability_given_calibration": math.fsum(
            retained
        ),
        "useful_search_probability_given_calibration": search_useful,
        "search_sampling_incomplete_probability_given_calibration": math.fsum(search_incomplete),
        "search_ceiling_incomplete_probability_given_calibration": math.fsum(ceiling_incomplete),
        "confirmation_complete_probability": holdout_complete,
        "confirmation_preserves_probability_given_complete_and_retained": holdout_pass,
        "unconditional_useful_probability": calibration
        * search_useful
        * holdout_complete
        * holdout_pass,
        "maximum_sampled_comparisons_in_enumerated_paths": max_comparisons,
        "maximum_trials_in_enumerated_paths": max_trials,
        "enumerated_branch_points": branches,
    }


ADMISSION = {p: admission_probability(p) for p in (P_ORIGINAL, P_REMOVED)}


async def evaluate() -> dict:
    started = time.perf_counter()
    holdout = {str(p): holdout_probabilities(p) for p in (P_ORIGINAL, P_REMOVED)}
    rows = []
    for failure in (0.0, 0.05, 0.10):
        for position in range(4):
            rows.append(await enumerate_position(position, failure, holdout["0.9"]["preserves"]))
    minimums = {
        str(failure): min(
            row["unconditional_useful_probability"]
            for row in rows
            if row["independent_preaccept_failure_probability"] == failure
        )
        for failure in (0.0, 0.05, 0.10)
    }
    assert hashes() == FROZEN_HASHES, "frozen source changed during evaluation"
    maximum_attempts = 7 + MAX_COMPARISONS * (8 + 9) + 2 * 124
    return {
        "kind": "exact-finite-binomial-native-layout-enumeration",
        "frozen_source_sha256": FROZEN_HASHES,
        "frozen_configuration": {
            "groups": GROUPS,
            "one_equal_byte_instruction_history_position": [0, 1, 2, 3],
            "mandatory_trigger_removal": "zero-provider-call invalid candidate",
            "original_and_retained_behavior_probability": P_ORIGINAL,
            "instruction_removed_behavior_probability": P_REMOVED,
            "failures": [0.0, 0.05, 0.10],
            "max_comparisons": MAX_COMPARISONS,
            "max_trials": TRIAL_CAP,
            "calibration": [3, 2, 7],
            "search_original_valid_attempts": [4, 8],
            "search_candidate_valid_attempts": [5, 9],
            "confirmation_each_valid_attempts": [100, 124],
            "confidence": 0.95,
            "margin": MARGIN,
            "screen": "current production _sampled_screen_action using Fraction ratios and Fraction(float margin)",
        },
        "search_admission_probabilities_given_complete": {str(p): v for p, v in ADMISSION.items()},
        "holdout_outcome_probabilities_given_complete": holdout,
        "rows": rows,
        "minimum_unconditional_useful_probability_by_failure": minimums,
        "every_position_above_0_80_by_failure": {
            key: value >= 0.80 for key, value in minimums.items()
        },
        "worst_case_attempts": maximum_attempts,
        "worst_case_final_response_provider_calls": maximum_attempts,
        "worst_case_shell_possible_provider_calls": 2 * maximum_attempts,
        "wall_time_upper_bound_at_120_second_deadline_plus_3_second_grace": maximum_attempts * 123,
        "evaluation_wall_seconds": time.perf_counter() - started,
        "evaluation_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "limitations": [
            "Idealized stationary independent Bernoulli behavioral rates, not native profile evidence.",
            "Completion exclusions model only independent safe preaccept failures; model-side invalidity has no proven outcome independence.",
            "Four equal-byte independent histories, no dependency closures, native reconstruction surprises, token growth, resource drift, or outcome drift.",
            "Search generator and classifier are real; bounded sampling completion is analytically modeled rather than executing sampling or runners.",
            "No unresolved dispatches, process failures after provider acceptance, token/cost measurements, or operational freshness expiration are simulated.",
            "Incomplete search cannot pass final-sweep usefulness; no repeated selection, confirmation, or favorable resampling is permitted.",
        ],
    }
