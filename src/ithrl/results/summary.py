"""Result summary consistency validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ithrl.execution import MAX_PROVIDER_CALLS_PER_ATTEMPT
from ithrl.observations import Observation, ObservationArm, ObservationOutcome, ObservationRole
from ithrl.results.models import CalibrationSummary, ResultSummary
from ithrl.runner.contracts import RunAccounting, TokenUsage


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
    _validate_candidate_materialization(summary)
    _validate_candidate_budget(summary)


def _validate_candidate_materialization(summary: ResultSummary) -> None:
    has_request = summary.reduced_request_digest is not None
    if has_request != (summary.candidate_materialization == "exact-request"):
        raise ValueError("only exact-request results can have a reduced request digest")


def _validate_candidate_budget(summary: ResultSummary) -> None:
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


def validate_calibration_summary(summary: CalibrationSummary) -> None:
    """Reject internally inconsistent full-context calibration claims."""

    completed = {"measurable", "not-measurable", "inconclusive"}
    if summary.outcome in completed and summary.stop_reason != "valid-sample-limit":
        raise ValueError("completed calibration requires the valid-sample stop reason")
    if summary.outcome == "refused" and summary.stop_reason != "safety-refusal":
        raise ValueError("refused calibration requires a safety-refusal stop reason")
    if summary.outcome == "incomplete" and summary.stop_reason not in {
        "attempt-limit",
        "unsafe-retry",
        "user-interrupt",
        "wall-time-budget",
    }:
        raise ValueError("incomplete calibration requires an incomplete stop reason")
    original = summary.original_context_ids
    retained = summary.retained_context_ids
    if len(set(original)) != len(original) or len(set(retained)) != len(retained):
        raise ValueError("calibration context IDs must not contain duplicates")
    if original != retained:
        raise ValueError("calibration must retain the complete original context")
    if (summary.request_digest is None) == (summary.candidate_materialization == "exact-request"):
        raise ValueError("only exact-request calibration can have a request digest")
    ids = summary.calibration_observation_ids
    if len(set(ids)) != len(ids):
        raise ValueError("calibration observation IDs must not contain duplicates")
    if summary.observations_recorded != len(ids):
        raise ValueError("observations_recorded must equal calibration observation IDs")
    if (
        sum(
            (
                summary.matches,
                summary.no_matches,
                summary.invalid_observations,
                summary.runner_errors,
            )
        )
        != summary.observations_recorded
    ):
        raise ValueError("calibration outcome counts must equal observations_recorded")
    if summary.terminal_error_category is not None and not (
        summary.invalid_observations or summary.runner_errors
    ):
        raise ValueError("terminal error category requires a failed observation")
    if (
        summary.provider_calls_completed is not None
        and summary.provider_calls_started is not None
        and summary.provider_calls_completed > summary.provider_calls_started
    ):
        raise ValueError("completed provider calls cannot exceed started calls")
    if (summary.provider_calls_started is None) != (summary.provider_calls_completed is None):
        raise ValueError("provider started and completed totals must have equal availability")
    action_counts = (
        summary.actions_requested,
        summary.actions_denied,
        summary.actions_executed,
        summary.tool_network_requests,
    )
    if any(value is None for value in action_counts) and not all(
        value is None for value in action_counts
    ):
        raise ValueError("action and tool-network totals must have equal availability")
    if (
        summary.actions_requested is not None
        and summary.actions_denied is not None
        and summary.actions_executed is not None
        and summary.actions_denied + summary.actions_executed > summary.actions_requested
    ):
        raise ValueError("denied and executed actions cannot exceed requested actions")
    if max(summary.provider_calls_started or 0, summary.provider_calls_possible) > (
        summary.provider_calls_may_have_started
    ):
        raise ValueError("provider calls cannot exceed the conservative bound")
    if summary.resume_of == summary.run_id:
        raise ValueError("resume_of must identify an earlier run")
    if (summary.next_command is None) != (summary.next_command_shell is None):
        raise ValueError("next_command and next_command_shell must be set together")


_CALIBRATION_DERIVED_FIELDS = frozenset(
    {
        "calibration_observation_ids",
        "observations_recorded",
        "matches",
        "no_matches",
        "invalid_observations",
        "runner_errors",
        "terminal_error_category",
        "provider_calls_started",
        "provider_calls_completed",
        "provider_calls_possible",
        "provider_calls_may_have_started",
        "actions_requested",
        "actions_denied",
        "actions_executed",
        "tool_network_requests",
        "usage",
        "elapsed_milliseconds",
        "effective_policy_digest",
        "source_store_effect",
        "original_workspace_effect",
    }
)
_EMPTY_CALIBRATION_AGGREGATES: dict[str, object] = {
    "calibration_observation_ids": (),
    "observations_recorded": 0,
    "matches": 0,
    "no_matches": 0,
    "invalid_observations": 0,
    "runner_errors": 0,
    "terminal_error_category": None,
    "provider_calls_started": 0,
    "provider_calls_completed": 0,
    "provider_calls_possible": 0,
    "provider_calls_may_have_started": 0,
    "actions_requested": 0,
    "actions_denied": 0,
    "actions_executed": 0,
    "tool_network_requests": 0,
    "usage": None,
    "elapsed_milliseconds": 0,
    "effective_policy_digest": None,
    "source_store_effect": "unchanged",
    "original_workspace_effect": "unchanged",
}


def calibration_summary_from_observations(
    *,
    base: Mapping[str, object],
    observations: Sequence[Observation],
    expected_policy_digest: str,
    max_provider_calls_per_attempt: int,
) -> CalibrationSummary:
    """Build calibration aggregates from exact, ordered calibration observations."""

    if _CALIBRATION_DERIVED_FIELDS & base.keys():
        raise ValueError("calibration aggregate fields must come from observations")
    if not 0 <= max_provider_calls_per_attempt <= MAX_PROVIDER_CALLS_PER_ATTEMPT:
        raise ValueError("max_provider_calls_per_attempt is out of range")
    summary = CalibrationSummary.model_validate({**base, **_EMPTY_CALIBRATION_AGGREGATES})
    seen_ids: set[str] = set()
    for index, observation in enumerate(observations, start=1):
        if observation.observation_id in seen_ids or observation.attempt != index:
            raise ValueError("calibration observations must be unique and ordered by attempt")
        seen_ids.add(observation.observation_id)
        if (
            observation.role is not ObservationRole.CALIBRATION
            or observation.arm is not ObservationArm.BASELINE
            or observation.candidate_digest != summary.candidate_digest
        ):
            raise ValueError("calibration observation role, arm, or candidate differs")
        if (
            observation.case_digest != summary.case_digest
            or observation.experiment_digest != summary.experiment_digest
            or observation.target_digest != summary.target_digest
            or observation.execution_plan_digest != summary.execution_plan_digest
            or observation.runner_fingerprint != summary.runner_fingerprint
            or observation.algorithm_digest != summary.algorithm_digest
        ):
            raise ValueError("calibration observation identity differs")
        policy = observation.attempt_evidence.effective_policy_digest
        if policy is not None and policy != expected_policy_digest:
            raise ValueError("calibration observation effective policy differs")
        evidence = observation.attempt_evidence
        started = (
            evidence.accounting.provider_calls_started
            if evidence.accounting is not None
            else int(evidence.provider_call_started)
        )
        if max(started, evidence.provider_calls_possible) > max_provider_calls_per_attempt and not (
            summary.outcome == "refused"
            and index == len(observations)
            and observation.error_category in {"safety", "contract"}
        ):
            raise ValueError("calibration possible provider calls exceed the plan")

    accounting = tuple(item.attempt_evidence.accounting for item in observations)
    complete_accounting = all(value is not None for value in accounting)
    known = tuple(value for value in accounting if value is not None)
    started_known = sum(value.provider_calls_started for value in known)
    possible = sum(item.attempt_evidence.provider_calls_possible for item in observations)
    may_have_started = sum(
        item.attempt_evidence.provider_call_charge(max_provider_calls_per_attempt)
        for item in observations
    )
    usage = _aggregate_usage(observations)
    all_policy_known = bool(observations) and all(
        item.attempt_evidence.effective_policy_digest is not None for item in observations
    )
    values = {
        **base,
        "calibration_observation_ids": tuple(item.observation_id for item in observations),
        "observations_recorded": len(observations),
        "matches": sum(item.outcome is ObservationOutcome.MATCH for item in observations),
        "no_matches": sum(item.outcome is ObservationOutcome.NO_MATCH for item in observations),
        "invalid_observations": sum(
            item.outcome is ObservationOutcome.INVALID_OBSERVATION for item in observations
        ),
        "runner_errors": sum(
            item.outcome is ObservationOutcome.RUNNER_ERROR for item in observations
        ),
        "terminal_error_category": observations[-1].error_category if observations else None,
        "provider_calls_started": started_known if complete_accounting else None,
        "provider_calls_completed": (
            sum(value.provider_calls_completed for value in known) if complete_accounting else None
        ),
        "provider_calls_possible": possible,
        "provider_calls_may_have_started": may_have_started,
        "actions_requested": (
            sum(value.actions_requested for value in known) if complete_accounting else None
        ),
        "actions_denied": (
            sum(value.actions_denied for value in known) if complete_accounting else None
        ),
        "actions_executed": (
            sum(value.actions_executed for value in known) if complete_accounting else None
        ),
        "tool_network_requests": (
            sum(value.tool_network_requests for value in known) if complete_accounting else None
        ),
        "usage": usage,
        "elapsed_milliseconds": (
            sum(value.elapsed_milliseconds for value in known) if complete_accounting else None
        ),
        "effective_policy_digest": (expected_policy_digest if all_policy_known else None),
        "source_store_effect": _aggregate_effect(known, len(observations), "source_store_effect"),
        "original_workspace_effect": _aggregate_effect(
            known, len(observations), "original_workspace_effect"
        ),
    }
    return CalibrationSummary.model_validate(values)


def _aggregate_usage(observations: Sequence[Observation]) -> TokenUsage | None:
    if not observations or any(item.attempt_evidence.usage is None for item in observations):
        return None
    usage = [item.attempt_evidence.usage for item in observations]
    reported = [value for value in usage if value is not None]
    return TokenUsage(
        input_tokens=sum(value.input_tokens for value in reported),
        output_tokens=sum(value.output_tokens for value in reported),
        cached_input_tokens=(
            sum(value.cached_input_tokens or 0 for value in reported)
            if all(value.cached_input_tokens is not None for value in reported)
            else None
        ),
    )


def _aggregate_effect(accounting: tuple[RunAccounting, ...], count: int, field: str) -> str:
    effects = {getattr(value, field) for value in accounting}
    if "may-change" in effects:
        return "may-change"
    if len(accounting) != count:
        return "unknown"
    return "unchanged"
