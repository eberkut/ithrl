"""Run one candidate and record its structural observation."""

from __future__ import annotations

import asyncio

from ithrl.behavior import BehaviorObservation, ShellAction, evaluate_target
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
)
from ithrl.runner import (
    AttemptEvidence,
    PreparedAttempt,
    ProviderAcceptanceState,
    Runner,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    validate_run_result,
    validate_runner_error,
)


class RunnerExecutionError(RuntimeError):
    """A runner failed before producing a structural observation."""

    def __init__(
        self,
        message: str,
        observation: Observation | None = None,
        *,
        attempt_started: bool = True,
    ) -> None:
        super().__init__(message)
        self.observation = observation
        self.attempt_started = attempt_started


class _RunnerTimedOut(TimeoutError):
    def __init__(self, error: RunnerError | None) -> None:
        super().__init__("runner exceeded the attempt wall-time limit")
        self.error = error


async def observe_once(
    *,
    runner: Runner,
    identity: ObservationIdentity,
    store: ObservationStore,
    attempt: PreparedAttempt,
) -> Observation:
    """Run once, evaluate the result, and save the observation."""

    role, arm, candidate_digest = _prepare_observation(runner, identity, store, attempt)
    result: RunResult | None = None
    try:
        result = await _run_with_deadlines(runner, attempt)
        validate_run_result(attempt, result)
        observed = evaluate_target(attempt.target, normalize_run_result(result))
    except asyncio.CancelledError as cancelled:
        error = (
            cancelled.error
            if isinstance(cancelled, RunnerCancelled)
            else RunnerError(
                RunnerErrorCode.CANCELLED,
                "runner was cancelled",
                provider_acceptance_state=_interrupted_acceptance_state(attempt),
                model_requested=attempt.execution_plan.model_requested,
                provider=attempt.execution_plan.capability.provider,
                endpoint=attempt.execution_plan.capability.endpoint,
            )
        )
        _record_runner_error(
            store,
            identity,
            attempt,
            role,
            arm,
            candidate_digest,
            error,
        )
        raise
    except _RunnerTimedOut as timed_out:
        error = _timeout_error(attempt, timed_out.error)
        failed_observation = _record_runner_error(
            store, identity, attempt, role, arm, candidate_digest, error
        )
        raise RunnerExecutionError(
            f"runner failed with {failed_observation.error_category}", failed_observation
        ) from error
    except Exception as error:
        runner_error = _normalize_runner_error(error, attempt, result)
        failed_observation = _record_runner_error(
            store, identity, attempt, role, arm, candidate_digest, runner_error
        )
        raise RunnerExecutionError(
            f"runner failed with {failed_observation.error_category}", failed_observation
        ) from error

    return store.record_attempt(
        identity=identity,
        candidate_digest=candidate_digest,
        role=role,
        arm=arm,
        outcome=(ObservationOutcome.MATCH if observed else ObservationOutcome.NO_MATCH),
        attempt_evidence=AttemptEvidence.from_result(attempt, result),
    )


def _prepare_observation(
    runner: Runner,
    identity: ObservationIdentity,
    store: ObservationStore,
    attempt: PreparedAttempt,
) -> tuple[ObservationRole, ObservationArm, str]:
    expected_identity = (
        attempt.candidate.case_digest,
        attempt.execution_plan.experiment_digest,
        runner.fingerprint,
        attempt.execution_plan.digest,
        attempt.target.digest,
    )
    actual_identity = (
        identity.case_digest,
        identity.experiment_digest,
        identity.runner_fingerprint,
        identity.execution_plan_digest,
        identity.target_digest,
    )
    if actual_identity != expected_identity:
        raise ValueError("observation identity does not match the prepared attempt")
    if runner.capability_profile.digest != attempt.execution_plan.capability.digest:
        raise ValueError("runner capability profile changed after preflight")
    candidate_digest = attempt.candidate.digest
    role = ObservationRole(attempt.phase)
    arm = ObservationArm(attempt.arm)
    previous = store.list_compatible(
        identity,
        candidate_digest=candidate_digest,
        role=role,
        arm=arm,
    )
    unsafe_to_retry = next(
        (item for item in reversed(previous) if not item.attempt_evidence.automatic_retry_safe),
        None,
    )
    if unsafe_to_retry is not None:
        raise RunnerExecutionError(
            "automatic retry is disabled because a previous incomplete attempt is not safe to repeat",
            unsafe_to_retry,
            attempt_started=False,
        )
    return role, arm, candidate_digest


async def _run_with_deadlines(runner: Runner, attempt: PreparedAttempt) -> RunResult:
    wall_time = attempt.resource_limits.max_wall_time_seconds
    cancellation_deadline = wall_time + runner.capability_profile.cancellation_grace_seconds
    cancellation_error: RunnerError | None = None
    execution_timer: asyncio.Timeout | None = None
    cleanup_timer: asyncio.Timeout | None = None
    try:
        async with asyncio.timeout(cancellation_deadline) as cleanup_timer:
            async with asyncio.timeout(wall_time) as execution_timer:
                try:
                    result = await runner.run(attempt)
                except RunnerCancelled as cancelled:
                    cancellation_error = cancelled.error
                    if _deadline_expired(execution_timer, cleanup_timer):
                        raise asyncio.CancelledError from None
                    raise
    except TimeoutError as error:
        if _deadline_expired(execution_timer, cleanup_timer):
            raise _RunnerTimedOut(cancellation_error) from error
        raise
    if _deadline_expired(execution_timer, cleanup_timer):
        raise _RunnerTimedOut(cancellation_error)
    return result


def _deadline_expired(*timers: asyncio.Timeout | None) -> bool:
    return any(timer is not None and timer.expired() for timer in timers)


def _timeout_error(attempt: PreparedAttempt, evidence: RunnerError | None) -> RunnerError:
    if evidence is None:
        plan = attempt.execution_plan
        return RunnerError(
            RunnerErrorCode.TIMEOUT,
            "runner exceeded the attempt wall-time limit",
            provider_acceptance_state=_interrupted_acceptance_state(attempt),
            model_requested=plan.model_requested,
            provider=plan.capability.provider,
            endpoint=plan.capability.endpoint,
        )
    return RunnerError(
        RunnerErrorCode.TIMEOUT,
        "runner exceeded the attempt wall-time limit",
        provider_acceptance_state=evidence.provider_acceptance_state,
        model_requested=evidence.model_requested,
        model_returned=evidence.model_returned,
        provider=evidence.provider,
        endpoint=evidence.endpoint,
        service_tier=evidence.service_tier,
        usage=evidence.usage,
        accounting=evidence.accounting,
        native_output=evidence.native_output,
        effective_policy_digest=evidence.effective_policy_digest,
    )


def _normalize_runner_error(
    error: Exception,
    attempt: PreparedAttempt,
    result: RunResult | None,
) -> RunnerError:
    if isinstance(error, RunnerError) and result is None:
        return error
    plan = attempt.execution_plan
    if result is None:
        return RunnerError(
            RunnerErrorCode.INTERNAL,
            "runner failed internally",
            provider_acceptance_state=_interrupted_acceptance_state(attempt),
            model_requested=plan.model_requested,
            provider=plan.capability.provider,
            endpoint=plan.capability.endpoint,
        )
    code = error.code if isinstance(error, RunnerError) else RunnerErrorCode.INTERNAL
    return RunnerError(
        code,
        "runner result failed validation",
        provider_acceptance_state=_result_acceptance_state(attempt, result),
        model_requested=result.model_requested,
        model_returned=result.model_returned,
        provider=result.provider,
        endpoint=result.endpoint,
        service_tier=result.service_tier,
        usage=result.usage,
        accounting=result.accounting,
        native_output=result.native_output,
        effective_policy_digest=result.effective_policy_digest,
    )


def _interrupted_acceptance_state(attempt: PreparedAttempt) -> ProviderAcceptanceState:
    return (
        ProviderAcceptanceState.UNKNOWN
        if attempt.execution_plan.capability.provider_calls_per_attempt > 0
        else ProviderAcceptanceState.NOT_STARTED
    )


def _result_acceptance_state(
    attempt: PreparedAttempt,
    result: RunResult,
) -> ProviderAcceptanceState:
    accounting = result.accounting
    planned = attempt.execution_plan.capability.provider_calls_per_attempt
    if accounting.provider_calls_started != accounting.provider_calls_completed:
        return ProviderAcceptanceState.UNKNOWN
    if accounting.provider_calls_started != planned:
        return ProviderAcceptanceState.UNKNOWN
    return (
        ProviderAcceptanceState.ACCEPTED
        if accounting.provider_calls_started > 0
        else ProviderAcceptanceState.NOT_STARTED
    )


def _record_runner_error(
    store: ObservationStore,
    identity: ObservationIdentity,
    attempt: PreparedAttempt,
    role: ObservationRole,
    arm: ObservationArm,
    candidate_digest: str,
    error: RunnerError,
) -> Observation:
    error = validate_runner_error(attempt, error)
    return store.record_attempt(
        identity=identity,
        candidate_digest=candidate_digest,
        role=role,
        arm=arm,
        outcome=ObservationOutcome.RUNNER_ERROR,
        error_category=error.code.value,
        attempt_evidence=AttemptEvidence.from_error(attempt, error),
    )


def normalize_run_result(result: RunResult) -> BehaviorObservation:
    """Convert runner output to the standard behavior format."""

    actions: list[ShellAction] = []
    for call in result.tool_calls:
        command = call.arguments.get("command", call.arguments.get("cmd", ""))
        actions.append(
            ShellAction(
                tool_name=call.name,
                command=command if isinstance(command, str) else "",
            )
        )
    return BehaviorObservation(final_response=result.final_text, shell_actions=tuple(actions))
