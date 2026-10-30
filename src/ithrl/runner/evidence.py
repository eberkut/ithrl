"""Persisted, content-safe evidence for one runner attempt."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.execution import MAX_PROVIDER_CALLS_PER_ATTEMPT
from ithrl.runner.contracts import (
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    TerminationReason,
    TokenUsage,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class AttemptEvidence(BaseModel):
    """Content-safe execution facts persisted with one observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    attempt_id: Annotated[str, Field(pattern=r"^attempt-[0-9a-f]{32}$")]
    attempt_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    termination_reason: TerminationReason
    provider_acceptance_state: ProviderAcceptanceState
    reason_code: Annotated[
        str | None, Field(pattern=r"^[A-Za-z0-9._-]{1,80}$", exclude_if=lambda value: value is None)
    ] = None
    provider_calls_possible: Annotated[int, Field(ge=0, le=MAX_PROVIDER_CALLS_PER_ATTEMPT)] = 0
    effective_policy_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = None
    model_requested: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    model_returned: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    provider: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    endpoint: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    service_tier: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    usage: TokenUsage | None = None
    accounting: RunAccounting | None = None
    native_output: NativeOutputReference | None = None
    retry_safe: bool = False
    overload_replacement_safe: bool = False

    @field_validator("reason_code", mode="before")
    @classmethod
    def validate_reason_code(cls, value: object) -> object:
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value, flags=re.ASCII) is None
        ):
            raise ValueError("runner error reason_code must be a bounded machine code")
        return value

    @model_validator(mode="after")
    def validate_completion(self) -> AttemptEvidence:
        self._validate_completed_attempt()
        self._validate_provider_acceptance()
        if self.retry_safe:
            self._validate_safe_retry()
        if self.overload_replacement_safe:
            accounting = self.accounting
            if (
                self.termination_reason != "failed"
                or self.retry_safe
                or self.effective_policy_digest is None
                or accounting is None
                or not _safe_overload_accounting(accounting)
                or self.provider_calls_possible != 1
                or (self.usage is not None and self.usage.output_tokens != 0)
                or self.provider_acceptance_state is not ProviderAcceptanceState.UNKNOWN
                or self.native_output is None
                or not self.native_output.complete
                or self.native_output.size_bytes != accounting.native_output_bytes
            ):
                raise ValueError("overload replacement requires complete bounded safety evidence")
        return self

    def _validate_completed_attempt(self) -> None:
        if self.termination_reason != "completed":
            return
        if self.reason_code is not None:
            raise ValueError("completed attempt evidence cannot have an error reason code")
        if self.accounting is None:
            raise ValueError("completed attempt evidence requires accounting")
        if self.effective_policy_digest is None:
            raise ValueError("completed attempt evidence requires an effective policy")
        if self.provider_acceptance_state is ProviderAcceptanceState.UNKNOWN:
            raise ValueError("completed attempt evidence cannot have unknown acceptance")

    def _validate_provider_acceptance(self) -> None:
        unknown = self.provider_acceptance_state is ProviderAcceptanceState.UNKNOWN
        if unknown != (self.provider_calls_possible > 0):
            raise ValueError("unknown acceptance requires a positive possible provider-call count")
        if self.accounting is None:
            return
        started = self.accounting.provider_calls_started
        if self.provider_acceptance_state is ProviderAcceptanceState.NOT_STARTED and started:
            raise ValueError("not-started provider acceptance conflicts with call accounting")
        if self.provider_acceptance_state is ProviderAcceptanceState.ACCEPTED and not started:
            raise ValueError("accepted provider state requires a started provider call")

    def _validate_safe_retry(self) -> None:
        accounting = self.accounting
        if self.termination_reason == "completed":
            raise ValueError("completed attempts do not need a retry-safe marker")
        if self.provider_acceptance_state is not ProviderAcceptanceState.NOT_STARTED:
            raise ValueError("retry-safe evidence requires provider acceptance not to have started")
        if accounting is None:
            raise ValueError("retry-safe evidence requires complete effect accounting")
        if not _accounting_has_no_external_effects(accounting):
            raise ValueError("retry-safe evidence cannot contain external effects")

    @property
    def acceptance_unknown(self) -> bool:
        """Return whether provider acceptance could not be determined."""

        return self.provider_acceptance_state is ProviderAcceptanceState.UNKNOWN

    @property
    def provider_call_started(self) -> bool:
        """Return whether at least one provider call definitely started."""

        return self.provider_acceptance_state is ProviderAcceptanceState.ACCEPTED

    def provider_call_charge(self, per_attempt_limit: int) -> int:
        """Retain the conservative attempt charge whenever provider work started."""

        known = (
            self.accounting.provider_calls_started
            if self.accounting is not None
            else int(self.provider_call_started)
        )
        return max(
            known,
            self.provider_calls_possible,
            per_attempt_limit if self.provider_call_started else 0,
        )

    @property
    def automatic_retry_safe(self) -> bool:
        """Return whether this evidence permits an automatic repeat attempt."""

        return self.termination_reason == "completed" or self.retry_safe

    @classmethod
    def from_result(cls, attempt: PreparedAttempt, result: RunResult) -> AttemptEvidence:
        """Create persisted evidence from a validated runner result."""

        return cls(
            attempt_id=attempt.attempt_id,
            attempt_digest=attempt.digest,
            termination_reason=result.termination_reason,
            provider_acceptance_state=(
                ProviderAcceptanceState.ACCEPTED
                if result.accounting.provider_calls_started > 0
                else ProviderAcceptanceState.NOT_STARTED
            ),
            effective_policy_digest=result.effective_policy_digest,
            model_requested=result.model_requested,
            model_returned=result.model_returned,
            provider=result.provider,
            endpoint=result.endpoint,
            service_tier=result.service_tier,
            usage=result.usage,
            accounting=result.accounting,
            native_output=result.native_output,
        )

    @classmethod
    def from_error(cls, attempt: PreparedAttempt, error: RunnerError) -> AttemptEvidence:
        """Create persisted evidence from a bounded operational error."""

        acceptance = _error_acceptance_state(attempt, error)
        return cls(
            attempt_id=attempt.attempt_id,
            attempt_digest=attempt.digest,
            termination_reason=_termination_reason(error.code),
            provider_acceptance_state=acceptance,
            **({"reason_code": error.reason_code} if error.reason_code is not None else {}),
            provider_calls_possible=(
                _possible_provider_calls(attempt, error)
                if acceptance is ProviderAcceptanceState.UNKNOWN
                else 0
            ),
            effective_policy_digest=error.effective_policy_digest,
            model_requested=error.model_requested,
            model_returned=error.model_returned,
            provider=error.provider,
            endpoint=error.endpoint,
            service_tier=error.service_tier,
            usage=error.usage,
            accounting=error.accounting,
            native_output=error.native_output,
            retry_safe=_error_retry_safe(attempt, error, acceptance),
            overload_replacement_safe=_error_overload_replacement_safe(attempt, error),
        )


def _termination_reason(code: RunnerErrorCode) -> TerminationReason:
    if code is RunnerErrorCode.TIMEOUT:
        return "timeout"
    if code is RunnerErrorCode.CANCELLED:
        return "cancelled"
    return "failed"


def _error_acceptance_state(
    attempt: PreparedAttempt,
    error: RunnerError,
) -> ProviderAcceptanceState:
    accounting = error.accounting
    if accounting is not None:
        if accounting.provider_calls_completed > 0:
            return ProviderAcceptanceState.ACCEPTED
        if accounting.provider_calls_started > 0:
            return ProviderAcceptanceState.UNKNOWN
        if error.provider_acceptance_state is ProviderAcceptanceState.ACCEPTED:
            return ProviderAcceptanceState.UNKNOWN
    return error.provider_acceptance_state


def _possible_provider_calls(attempt: PreparedAttempt, error: RunnerError) -> int:
    accounted = error.accounting.provider_calls_started if error.accounting is not None else 0
    return max(1, attempt.execution_plan.capability.provider_calls_per_attempt, accounted)


_RETRYABLE_ERROR_CODES = {
    RunnerErrorCode.PREPARATION,
    RunnerErrorCode.STARTUP,
    RunnerErrorCode.TRANSPORT,
}


def _error_overload_replacement_safe(attempt: PreparedAttempt, error: RunnerError) -> bool:
    """Qualify a fresh sample without asserting unobserved provider work did not occur."""

    accounting = error.accounting
    capability = attempt.execution_plan.capability
    return (
        error.code is RunnerErrorCode.PROVIDER_OVERLOADED
        and error.behavior_output_observed is False
        and accounting is not None
        and _safe_overload_accounting(accounting)
        and (error.usage is None or error.usage.output_tokens == 0)
        and capability.provider_calls_per_attempt == 1
        and capability.action_denial == "enforced"
        and capability.protected_state_enforcement == "preventive"
        and error.effective_policy_digest == attempt.safety_policy.digest
        and error.provider == capability.provider
        and error.endpoint == capability.endpoint
        and error.model_requested == attempt.execution_plan.model_requested
        and _retry_limits_allow(attempt, error, accounting)
        and _retry_plan_matches(attempt, error)
        and error.native_output is not None
        and error.native_output.complete
        and _native_output_is_consistent(error.native_output, accounting.native_output_bytes)
    )


def _safe_overload_accounting(accounting: RunAccounting) -> bool:
    return (
        _safe_cleanup_and_effects(accounting)
        and accounting.cleanup_outcome == "verified"
        and accounting.process_outcome in {"exited", "terminated"}
        and accounting.actions_requested == 0
        and accounting.provider_calls_started == 1
        and accounting.provider_calls_completed == 0
        and accounting.provider_connections_started <= 1
    )


def _error_retry_safe(
    attempt: PreparedAttempt,
    error: RunnerError,
    acceptance: ProviderAcceptanceState,
) -> bool:
    accounting = error.accounting
    if accounting is None or not _has_retryable_start(error, acceptance, accounting):
        return False
    if not _retry_limits_allow(attempt, error, accounting):
        return False
    if not _retry_plan_matches(attempt, error):
        return False
    return _native_output_is_consistent(error.native_output, accounting.native_output_bytes)


def _retry_limits_allow(
    attempt: PreparedAttempt,
    error: RunnerError,
    accounting: RunAccounting,
) -> bool:
    limits = attempt.resource_limits
    within_limits = (
        accounting.elapsed_milliseconds <= limits.max_wall_time_seconds * 1000,
        accounting.native_output_bytes <= limits.max_native_output_bytes,
        accounting.disposable_state_bytes <= limits.max_disposable_state_bytes,
        accounting.disposable_state_entries <= limits.max_disposable_state_entries,
        error.usage is None
        or limits.max_input_tokens is None
        or error.usage.input_tokens <= limits.max_input_tokens,
    )
    return all(within_limits)


def _retry_plan_matches(attempt: PreparedAttempt, error: RunnerError) -> bool:
    capability = attempt.execution_plan.capability
    if (
        capability.disposable_state_effect == "writes"
        and capability.disposable_state_limit_enforcement != "enforced"
    ):
        return False
    if error.effective_policy_digest not in {None, attempt.safety_policy.digest}:
        return False
    return _error_provenance_matches(attempt, error)


def _has_retryable_start(
    error: RunnerError,
    acceptance: ProviderAcceptanceState,
    accounting: RunAccounting,
) -> bool:
    return (
        error.code in _RETRYABLE_ERROR_CODES
        and acceptance is ProviderAcceptanceState.NOT_STARTED
        and _accounting_has_no_external_effects(accounting)
    )


def _error_provenance_matches(attempt: PreparedAttempt, error: RunnerError) -> bool:
    plan = attempt.execution_plan
    return (
        error.provider in {None, plan.capability.provider}
        and error.endpoint in {None, plan.capability.endpoint}
        and error.model_requested in {None, plan.model_requested}
    )


def _accounting_has_no_external_effects(accounting: RunAccounting) -> bool:
    return (
        accounting.provider_calls_started == 0
        and accounting.provider_calls_completed == 0
        and accounting.provider_connections_started == 0
        and _safe_cleanup_and_effects(accounting)
    )


def _safe_cleanup_and_effects(accounting: RunAccounting) -> bool:
    return all(
        (
            accounting.tool_network_requests == 0,
            accounting.actions_executed == 0,
            accounting.actions_denied == accounting.actions_requested,
            accounting.process_outcome != "unknown",
            accounting.cleanup_outcome in {"not-required", "verified"},
            accounting.residual_disposable_state_bytes == 0,
            accounting.residual_disposable_state_entries == 0,
            accounting.source_store_effect == "unchanged",
            accounting.original_workspace_effect == "unchanged",
        )
    )


def _native_output_is_consistent(
    native_output: NativeOutputReference | None,
    native_output_bytes: int,
) -> bool:
    if (native_output is None) != (native_output_bytes == 0):
        return False
    return native_output is None or native_output.size_bytes == native_output_bytes
