"""Validate normalized runner results against prepared attempts."""

from __future__ import annotations

from pydantic import ValidationError

from ithrl.runner.contracts import (
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    ToolCall,
)


def validate_run_result(attempt: PreparedAttempt, result: RunResult) -> None:
    """Reject normalized output that violates the prepared attempt."""

    _validate_result_identity(attempt, result)
    _validate_result_provenance(attempt, result)
    _validate_result_accounting(attempt, result)
    _validate_result_limits(attempt, result)
    _validate_native_output(result)
    _validate_tool_calls(result)


def validate_runner_error(attempt: PreparedAttempt, error: RunnerError) -> RunnerError:
    """Classify partial error evidence against the prepared safety boundary."""

    violation = _error_evidence_violation(attempt, error)
    if violation is None:
        return error
    code, message = violation
    return _copy_runner_error(error, code=code, message=message)


def _validate_result_identity(attempt: PreparedAttempt, result: RunResult) -> None:
    accounting = result.accounting
    if result.attempt_id != attempt.attempt_id:
        raise RunnerError(RunnerErrorCode.CONTRACT, "runner returned another attempt identity")
    if result.effective_policy_digest != attempt.safety_policy.digest:
        raise RunnerError(
            RunnerErrorCode.SAFETY, "runner did not enforce the resolved safety policy"
        )
    if result.termination_reason != "completed":
        code = {
            "timeout": RunnerErrorCode.TIMEOUT,
            "cancelled": RunnerErrorCode.CANCELLED,
            "failed": RunnerErrorCode.INTERNAL,
        }[result.termination_reason]
        raise RunnerError(code, f"runner terminated with {result.termination_reason}")
    planned = attempt.execution_plan.capability.provider_calls_per_attempt
    if accounting.provider_calls_started > planned:
        raise RunnerError(RunnerErrorCode.SAFETY, "runner exceeded its planned provider-call count")
    if planned > 0 and accounting.provider_calls_started == 0:
        raise RunnerError(
            RunnerErrorCode.CONTRACT, "completed provider-backed result has no provider call"
        )
    if accounting.provider_calls_completed != accounting.provider_calls_started:
        raise RunnerError(
            RunnerErrorCode.CONTRACT,
            "a completed result must account for every started provider call",
        )


def _validate_result_accounting(attempt: PreparedAttempt, result: RunResult) -> None:
    accounting = result.accounting
    _validate_result_state_contract(attempt, accounting)
    _validate_result_actions(result)
    _validate_result_protected_state(accounting)
    _validate_runtime_cleanup(accounting)
    _validate_result_transport(attempt, accounting)


def _validate_result_state_contract(
    attempt: PreparedAttempt,
    accounting: RunAccounting,
) -> None:
    if attempt.execution_plan.capability.disposable_state_effect == "none" and (
        accounting.disposable_state_bytes or accounting.disposable_state_entries
    ):
        raise RunnerError(
            RunnerErrorCode.CONTRACT,
            "runner reported disposable state that its capability excludes",
        )


def _validate_result_actions(result: RunResult) -> None:
    accounting = result.accounting
    if accounting.actions_requested != len(result.tool_calls):
        raise RunnerError(
            RunnerErrorCode.CONTRACT, "runner action count differs from normalized output"
        )
    if accounting.actions_executed != 0 or accounting.tool_network_requests != 0:
        raise RunnerError(
            RunnerErrorCode.SAFETY, "runner reported a forbidden action or tool network request"
        )
    if accounting.actions_denied != accounting.actions_requested:
        raise RunnerError(RunnerErrorCode.SAFETY, "runner did not deny every returned action")


def _validate_result_protected_state(accounting: RunAccounting) -> None:
    if accounting.source_store_effect != "unchanged":
        raise RunnerError(RunnerErrorCode.SAFETY, "runner changed the preserved source session")
    if accounting.original_workspace_effect != "unchanged":
        raise RunnerError(RunnerErrorCode.SAFETY, "runner changed the original workspace")


def _validate_result_transport(attempt: PreparedAttempt, accounting: RunAccounting) -> None:
    if (
        attempt.execution_plan.capability.provider_transport == "forbidden"
        and accounting.provider_connections_started != 0
    ):
        raise RunnerError(
            RunnerErrorCode.SAFETY,
            "runner used provider transport that the execution plan forbids",
        )


def _validate_result_provenance(attempt: PreparedAttempt, result: RunResult) -> None:
    plan = attempt.execution_plan
    if result.provider != plan.capability.provider or result.endpoint != plan.capability.endpoint:
        raise RunnerError(
            RunnerErrorCode.SAFETY,
            "runner destination differs from the resolved execution plan",
        )
    if result.model_requested != plan.model_requested:
        raise RunnerError(
            RunnerErrorCode.CONTRACT,
            "runner requested model differs from the resolved execution plan",
        )


def _validate_result_limits(attempt: PreparedAttempt, result: RunResult) -> None:
    _validate_accounting_limits(attempt, result.accounting)
    _validate_usage_limits(attempt, result)


def _validate_accounting_limits(attempt: PreparedAttempt, accounting: RunAccounting) -> None:
    limits = attempt.resource_limits
    if accounting.provider_calls_started > limits.max_provider_calls:
        raise RunnerError(RunnerErrorCode.SAFETY, "runner exceeded the provider-call limit")
    if accounting.elapsed_milliseconds > limits.max_wall_time_seconds * 1000:
        raise RunnerError(RunnerErrorCode.TIMEOUT, "runner exceeded the wall-time limit")
    if accounting.native_output_bytes > limits.max_native_output_bytes:
        raise RunnerError(RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the native-output limit")
    if accounting.disposable_state_bytes > limits.max_disposable_state_bytes:
        raise RunnerError(
            RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the disposable-state limit"
        )
    if accounting.disposable_state_entries > limits.max_disposable_state_entries:
        raise RunnerError(
            RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the disposable-state entry limit"
        )


def _validate_usage_limits(attempt: PreparedAttempt, result: RunResult) -> None:
    limits = attempt.resource_limits
    if (
        result.usage is not None
        and limits.max_input_tokens is not None
        and result.usage.input_tokens > limits.max_input_tokens
    ):
        raise RunnerError(RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the input-token limit")
    if attempt.execution_plan.capability.reports_usage and result.usage is None:
        raise RunnerError(RunnerErrorCode.CONTRACT, "runner omitted required token usage")


def _validate_native_output(result: RunResult) -> None:
    accounting = result.accounting
    if (result.native_output is None) != (accounting.native_output_bytes == 0):
        raise RunnerError(
            RunnerErrorCode.CONTRACT, "native output reference and byte count disagree"
        )
    if (
        result.native_output is not None
        and result.native_output.size_bytes != accounting.native_output_bytes
    ):
        raise RunnerError(RunnerErrorCode.CONTRACT, "native output reference has the wrong size")
    if result.native_output is not None and not result.native_output.complete:
        raise RunnerError(RunnerErrorCode.CONTRACT, "completed runner output must be complete")


def _validate_tool_calls(result: RunResult) -> None:
    try:
        for tool_call in result.tool_calls:
            ToolCall.model_validate(tool_call.model_dump(mode="python"))
    except (ValidationError, ValueError) as error:
        raise RunnerError(
            RunnerErrorCode.OUTPUT_LIMIT,
            "runner returned tool arguments outside the shared bounds",
        ) from error


def _error_evidence_violation(
    attempt: PreparedAttempt,
    error: RunnerError,
) -> tuple[RunnerErrorCode, str] | None:
    plan = attempt.execution_plan
    accounting = error.accounting
    if (
        plan.capability.provider_calls_per_attempt == 0
        and error.provider_acceptance_state is not ProviderAcceptanceState.NOT_STARTED
    ):
        return (
            RunnerErrorCode.SAFETY,
            "runner reported possible provider acceptance under a zero-call plan",
        )
    if error.effective_policy_digest not in {None, attempt.safety_policy.digest}:
        return RunnerErrorCode.SAFETY, "runner did not enforce the resolved safety policy"
    if error.provider not in {None, plan.capability.provider} or error.endpoint not in {
        None,
        plan.capability.endpoint,
    }:
        return RunnerErrorCode.SAFETY, "runner destination differs from the execution plan"
    if error.model_requested not in {None, plan.model_requested}:
        return RunnerErrorCode.CONTRACT, "runner requested model differs from the execution plan"
    if accounting is None:
        return None
    return (
        _error_accounting_violation(attempt, accounting)
        or _error_limit_violation(attempt, error, accounting)
        or _native_output_violation(error.native_output, accounting.native_output_bytes)
    )


def _error_accounting_violation(
    attempt: PreparedAttempt,
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    checks = (
        _error_state_contract_violation(attempt, accounting),
        _error_action_violation(accounting),
        _error_protected_state_violation(accounting),
        _runtime_cleanup_violation(accounting),
        _error_transport_violation(attempt, accounting),
    )
    return next((violation for violation in checks if violation is not None), None)


def _error_state_contract_violation(
    attempt: PreparedAttempt,
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    if attempt.execution_plan.capability.disposable_state_effect == "none" and (
        accounting.disposable_state_bytes or accounting.disposable_state_entries
    ):
        return (
            RunnerErrorCode.CONTRACT,
            "runner reported disposable state that its capability excludes",
        )
    return None


def _error_action_violation(
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    if accounting.actions_executed or accounting.tool_network_requests:
        return RunnerErrorCode.SAFETY, "runner reported a forbidden action or tool network request"
    if accounting.actions_denied != accounting.actions_requested:
        return RunnerErrorCode.SAFETY, "runner did not account for every requested action as denied"
    return None


def _error_protected_state_violation(
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    if accounting.source_store_effect != "unchanged":
        return RunnerErrorCode.SAFETY, "runner changed the preserved source session"
    if accounting.original_workspace_effect != "unchanged":
        return RunnerErrorCode.SAFETY, "runner changed the original workspace"
    return None


def _error_transport_violation(
    attempt: PreparedAttempt,
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    if (
        attempt.execution_plan.capability.provider_transport == "forbidden"
        and accounting.provider_connections_started
    ):
        return RunnerErrorCode.SAFETY, "runner used forbidden provider transport"
    if (
        accounting.provider_calls_started
        > attempt.execution_plan.capability.provider_calls_per_attempt
    ):
        return RunnerErrorCode.SAFETY, "runner exceeded its planned provider-call count"
    if accounting.provider_calls_started > attempt.resource_limits.max_provider_calls:
        return RunnerErrorCode.SAFETY, "runner exceeded the provider-call limit"
    return None


def _validate_runtime_cleanup(accounting: RunAccounting) -> None:
    violation = _runtime_cleanup_violation(accounting)
    if violation is not None:
        code, message = violation
        raise RunnerError(code, message)


def _runtime_cleanup_violation(
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    if accounting.process_outcome == "unknown":
        return RunnerErrorCode.SAFETY, "runner could not verify process termination"
    if accounting.cleanup_outcome == "failed":
        return RunnerErrorCode.SAFETY, "runner could not verify disposable-state cleanup"
    if (
        accounting.process_outcome in {"exited", "terminated"}
        and accounting.cleanup_outcome != "verified"
    ):
        return RunnerErrorCode.SAFETY, "runner omitted cleanup evidence for a started process"
    if accounting.residual_disposable_state_bytes:
        return RunnerErrorCode.SAFETY, "runner left disposable state behind"
    if accounting.residual_disposable_state_entries:
        return RunnerErrorCode.SAFETY, "runner left disposable-state entries behind"
    return None


def _error_limit_violation(
    attempt: PreparedAttempt,
    error: RunnerError,
    accounting: RunAccounting,
) -> tuple[RunnerErrorCode, str] | None:
    limits = attempt.resource_limits
    if accounting.elapsed_milliseconds > limits.max_wall_time_seconds * 1000:
        return RunnerErrorCode.TIMEOUT, "runner exceeded the wall-time limit"
    if accounting.native_output_bytes > limits.max_native_output_bytes:
        return RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the native-output limit"
    if accounting.disposable_state_bytes > limits.max_disposable_state_bytes:
        return RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the disposable-state limit"
    if accounting.disposable_state_entries > limits.max_disposable_state_entries:
        return RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the disposable-state entry limit"
    if (
        error.usage is not None
        and limits.max_input_tokens is not None
        and error.usage.input_tokens > limits.max_input_tokens
    ):
        return RunnerErrorCode.OUTPUT_LIMIT, "runner exceeded the input-token limit"
    return None


def _native_output_violation(
    native_output: NativeOutputReference | None,
    native_output_bytes: int,
) -> tuple[RunnerErrorCode, str] | None:
    if (native_output is None) != (native_output_bytes == 0):
        return RunnerErrorCode.CONTRACT, "native output reference and byte count disagree"
    if native_output is not None and native_output.size_bytes != native_output_bytes:
        return RunnerErrorCode.CONTRACT, "native output reference has the wrong size"
    return None


def _copy_runner_error(
    error: RunnerError,
    *,
    code: RunnerErrorCode,
    message: str,
) -> RunnerError:
    return RunnerError(
        code,
        message,
        reason_code=error.reason_code,
        provider_acceptance_state=error.provider_acceptance_state,
        model_requested=error.model_requested,
        model_returned=error.model_returned,
        provider=error.provider,
        endpoint=error.endpoint,
        service_tier=error.service_tier,
        usage=error.usage,
        accounting=error.accounting,
        native_output=error.native_output,
        effective_policy_digest=error.effective_policy_digest,
    )
