"""Result summary consistency validation."""

from __future__ import annotations

from ithrl.results.models import ResultSummary


def validate_summary(summary: ResultSummary) -> None:
    """Reject internally inconsistent summary claims in a stable order."""

    _validate_context(summary)
    _validate_observation_ids(summary)
    _validate_continuation(summary)
    _validate_status_phase_outcomes(summary)
    _validate_phase_sequence(summary)
    _validate_phase_observations(summary)
    _validate_observation_counts(summary)
    _validate_terminal_status(summary)


def _validate_context(summary: ResultSummary) -> None:
    original = summary.original_context_ids
    retained = summary.retained_context_ids
    removed = summary.removed_context_ids
    if len(set(original)) != len(original):
        raise ValueError("original_context_ids must not contain duplicates")
    if len(set(retained)) != len(retained):
        raise ValueError("retained_context_ids must not contain duplicates")
    if len(set(removed)) != len(removed):
        raise ValueError("removed_context_ids must not contain duplicates")
    if summary.original_context_count != len(original):
        raise ValueError("original_context_count must match original_context_ids")
    if summary.retained_context_count != len(retained):
        raise ValueError("retained_context_count must match retained_context_ids")
    if set(retained) & set(removed):
        raise ValueError("retained and removed context IDs must be disjoint")
    if set(original) != set(retained) | set(removed):
        raise ValueError("retained and removed context IDs must partition the original IDs")
    if (
        summary.max_candidate_runs is not None
        and summary.candidate_runs_used > summary.max_candidate_runs
    ):
        raise ValueError("candidate_runs_used must not exceed max_candidate_runs")


def _validate_observation_ids(summary: ResultSummary) -> None:
    calibration_ids = summary.calibration_observation_ids
    confirmation_ids = summary.confirmation_observation_ids
    if len(set(calibration_ids)) != len(calibration_ids):
        raise ValueError("calibration_observation_ids must not contain duplicates")
    if len(set(confirmation_ids)) != len(confirmation_ids):
        raise ValueError("confirmation_observation_ids must not contain duplicates")
    if set(calibration_ids) & set(confirmation_ids):
        raise ValueError("calibration and confirmation observation IDs must be disjoint")


def _validate_continuation(summary: ResultSummary) -> None:
    if summary.resume_of == summary.run_id:
        raise ValueError("resume_of must identify an earlier run")
    if summary.status in {"stopped", "interrupted"} and summary.next_action is None:
        raise ValueError("stopped and interrupted results require next_action")
    if (summary.next_command is None) != (summary.next_command_shell is None):
        raise ValueError("next_command and next_command_shell must be set together")


def _validate_status_phase_outcomes(summary: ResultSummary) -> None:
    if summary.status == "reduced":
        outcomes = (
            summary.calibration_result,
            summary.search_result,
            summary.confirmation_result,
        )
        if outcomes != ("observed", "search-1-minimal", "confirmed-preserving"):
            raise ValueError(
                "reduced results require observed calibration, search-1-minimal, "
                "and confirmed-preserving outcomes"
            )
    elif summary.status == "confirmation-failed":
        if summary.confirmation_result != "failed":
            raise ValueError("confirmation-failed status requires a failed confirmation")
    elif summary.status == "not-observed":
        if summary.calibration_result != "not-observed":
            raise ValueError("not-observed status requires a not-observed calibration")


def _validate_phase_sequence(summary: ResultSummary) -> None:
    if summary.search_result != "not-run" and summary.calibration_result != "observed":
        raise ValueError("search requires an observed calibration target")
    if summary.confirmation_result != "not-run" and summary.search_result == "not-run":
        raise ValueError("confirmation requires a completed or incomplete search")
    if summary.search_result == "not-run" and summary.candidate_runs_used != 0:
        raise ValueError("candidate_runs_used must be zero when search was not run")


def _validate_phase_observations(summary: ResultSummary) -> None:
    if summary.calibration_result == "not-run" and summary.calibration_observation_ids:
        raise ValueError("calibration observation IDs require a calibration outcome")
    if summary.calibration_result != "not-run" and not summary.calibration_observation_ids:
        raise ValueError("a calibration outcome requires calibration observation IDs")
    if summary.confirmation_result == "not-run" and summary.confirmation_observation_ids:
        raise ValueError("confirmation observation IDs require a confirmation outcome")
    if summary.confirmation_result != "not-run" and not summary.confirmation_observation_ids:
        raise ValueError("a confirmation outcome requires confirmation observation IDs")


def _validate_observation_counts(summary: ResultSummary) -> None:
    if summary.observations_recorded < summary.candidate_runs_used:
        raise ValueError("observations_recorded must include every candidate run")
    phase_observations = (
        summary.candidate_runs_used
        + len(summary.calibration_observation_ids)
        + len(summary.confirmation_observation_ids)
    )
    if summary.observations_recorded < phase_observations:
        raise ValueError("observations_recorded must include all referenced observations")


def _validate_terminal_status(summary: ResultSummary) -> None:
    if summary.status == "calibrated":
        outcomes = (
            summary.calibration_result,
            summary.search_result,
            summary.confirmation_result,
        )
        if outcomes != ("observed", "not-run", "not-run"):
            raise ValueError("calibrated status requires an observed baseline and no later phases")

    if summary.confirmation_result == "confirmed-preserving":
        if summary.status != "reduced":
            raise ValueError("confirmed-preserving confirmation requires reduced status")
    elif summary.confirmation_result == "failed" and summary.status not in {
        "confirmation-failed",
        "failed",
    }:
        raise ValueError("failed confirmation requires a failed result status")

    if summary.confirmation_result != "not-run" and summary.search_result != "search-1-minimal":
        raise ValueError("confirmation requires a search-1-minimal result")
