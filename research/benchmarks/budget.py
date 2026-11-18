"""Size the proposed public-task pilot without detecting or starting a harness."""

from __future__ import annotations

import json
import sys

from ithrl.execution.budget import SamplingAttemptCost
from ithrl.experiment import SampledCalibrationPhase


def pilot_budget() -> dict[str, object]:
    """Return proposed maxima; these are not an execution-plan admission result."""

    tasks = 2
    harnesses = ("codex", "copilot")
    cells = tasks * len(harnesses)
    calibration = SampledCalibrationPhase(required_valid=3, minimum_matches=3, max_attempts=3)
    timeout_seconds = 120
    cleanup_grace_seconds = 3
    cost = SamplingAttemptCost(
        provider_calls=1,
        runner_seconds=timeout_seconds + cleanup_grace_seconds,
    )
    per_cell = {
        "original_source_generation_turns": 2,
        "changed_defect_source_generation_turns": 2,
        "fresh_original_calibration_replays": calibration.max_attempts,
        "irrelevant_section_removal_screens": 1,
        "withheld_case_evidence_removal_screens": 1,
        "changed_defect_replays": 1,
    }
    stage_totals = {stage: attempts * cells for stage, attempts in per_cell.items()}
    generation_turns = sum(
        attempts for stage, attempts in stage_totals.items() if stage.endswith("turns")
    )
    attempts = sum(stage_totals.values())
    # Each two-turn source has one acknowledgement and one repair response.
    grade_runs = attempts - generation_turns + cells * 2
    grading_timeout_seconds = 10
    return {
        "status": "proposal-not-runtime-admitted",
        "tasks": tasks,
        "harnesses": list(harnesses),
        "task_harness_cells": cells,
        "calibration_per_cell": calibration.model_dump(mode="json"),
        "attempts_per_cell_by_stage": per_cell,
        "total_attempts_by_stage": stage_totals,
        "maximum_generation_turns": generation_turns,
        "maximum_replay_attempts": attempts - generation_turns,
        "maximum_native_attempts": attempts,
        "maximum_provider_inference_requests": attempts * cost.provider_calls,
        "per_attempt": {
            "maximum_provider_inference_requests": cost.provider_calls,
            "timeout_seconds": timeout_seconds,
            "cleanup_grace_seconds": cleanup_grace_seconds,
        },
        "maximum_runner_seconds": attempts * cost.runner_seconds,
        "maximum_task_grade_runs": grade_runs,
        "task_grade_timeout_seconds": grading_timeout_seconds,
        "maximum_task_grading_seconds": grade_runs * grading_timeout_seconds,
        "grading_within_attempt_timeout": True,
        "maximum_runner_and_grading_seconds": attempts * cost.runner_seconds,
        "provider_window_seconds": 6600,
        "input_token_cap": None,
        "monetary_cap": None,
        "search_comparisons": 0,
        "preservation_confirmation_observations": 0,
        "automatic_extensions": False,
        "replacement_attempts": False,
        "changed_defect_is_separately_generated_source": True,
        "excluded_from_runner_bound": [
            "task preparation",
            "native structural and input-visibility controls",
            "persistence and other orchestration overhead",
            "authentication, catalogue and metadata traffic",
        ],
        "execution_prerequisites": [
            "bind current native capabilities, policy and resource limits",
            "verify one physical inference request per native attempt",
            "verify the task grader integration, 10-second grading bound and context controls",
            "verify the 110-minute provider window across generation, replays and grading",
            "obtain separate native and provider execution approval",
        ],
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(pilot_budget(), indent=2) + "\n")
