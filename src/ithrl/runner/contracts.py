"""Shared candidate, attempt, and normalized runner contracts."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.behavior import (
    MAX_BEHAVIOR_TEXT_LENGTH,
    MAX_SHELL_ACTIONS,
    MAX_TOOL_NAME_LENGTH,
    BehaviorTarget,
)
from ithrl.case import CaseEvidence
from ithrl.context import MAX_CONTEXT_NODES, JsonObject, content_digest, validate_json_limits
from ithrl.execution import (
    MAX_NATIVE_OUTPUT_BYTES,
    MAX_PROVIDER_CALLS_PER_ATTEMPT,
    CapabilityProfile,
    ProtectedStateEffect,
    ResolvedExecutionPlan,
    ResourceLimits,
    SafetyPolicy,
)
from ithrl.mutation import MutationError, TextMutation

type AttemptPhase = Literal["calibration", "search", "discovery", "simplification", "confirmation"]
type AttemptArm = Literal["baseline", "candidate", "selected"]
type TerminationReason = Literal["completed", "timeout", "cancelled", "failed"]
type ProcessOutcome = Literal["not-started", "exited", "terminated", "unknown"]
type CleanupOutcome = Literal["not-required", "verified", "failed"]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_ARGUMENT_DEPTH = 16
MAX_TOOL_ARGUMENT_ITEMS = 4096
MAX_MODEL_NAME_LENGTH = 256
MAX_SERVICE_TIER_LENGTH = 128


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Candidate(_ImmutableContract):
    """A context partition and optional text intervention bound to an execution plan."""

    model_config = ConfigDict(hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    case_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    execution_plan_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    source_artifact_digests: Annotated[
        tuple[Annotated[str, Field(pattern=_DIGEST_PATTERN)], ...], Field(max_length=64)
    ] = ()
    retained_context_ids: Annotated[tuple[str, ...], Field(max_length=MAX_CONTEXT_NODES)]
    removed_context_ids: Annotated[tuple[str, ...], Field(max_length=MAX_CONTEXT_NODES)]
    materialization: Literal["exact-request", "source-backed"]
    mutation: TextMutation = Field(default_factory=TextMutation)

    @model_validator(mode="after")
    def validate_partition(self) -> Candidate:
        if len(set(self.retained_context_ids)) != len(self.retained_context_ids):
            raise ValueError("retained_context_ids must not contain duplicates")
        if len(set(self.removed_context_ids)) != len(self.removed_context_ids):
            raise ValueError("removed_context_ids must not contain duplicates")
        if set(self.retained_context_ids) & set(self.removed_context_ids):
            raise ValueError("retained and removed context IDs must be disjoint")
        if not self.mutation.is_empty and self.removed_context_ids:
            raise ValueError("text mutation requires the unchanged original context partition")
        if any(edit.context_id not in self.retained_context_ids for edit in self.mutation.edits):
            raise ValueError("mutation must address a retained context node")
        return self

    @property
    def digest(self) -> str:
        """Return the identity of the candidate and its reconstruction plan."""

        return content_digest(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class PreparedAttempt:
    """Immutable inputs supplied to one runner invocation."""

    evidence: CaseEvidence = field(repr=False, compare=False)
    candidate: Candidate
    target: BehaviorTarget
    execution_plan: ResolvedExecutionPlan
    safety_policy: SafetyPolicy
    resource_limits: ResourceLimits
    phase: AttemptPhase
    arm: AttemptArm
    attempt_id: str = field(default_factory=lambda: f"attempt-{uuid4().hex}")

    def __post_init__(self) -> None:
        allowed_phase_arms = {
            "calibration": ("baseline",),
            "search": ("baseline", "candidate"),
            "discovery": ("baseline", "candidate"),
            "simplification": ("baseline", "candidate"),
            "confirmation": ("baseline", "candidate", "selected"),
        }
        if self.phase not in allowed_phase_arms or self.arm not in allowed_phase_arms[self.phase]:
            raise ValueError("attempt arm is not allowed for its phase")
        prefix, separator, value = self.attempt_id.partition("-")
        if prefix != "attempt" or separator != "-" or len(value) != 32 or not value.isascii():
            raise ValueError("attempt_id must use the generated attempt identifier format")
        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("attempt_id must use the generated attempt identifier format")
        _validate_attempt_identities(self)

    @property
    def digest(self) -> str:
        """Return an identity for this individual attempt."""

        return content_digest(
            {
                "attempt_id": self.attempt_id,
                "candidate_digest": self.candidate.digest,
                "target_digest": self.target.digest,
                "execution_plan_digest": self.execution_plan.digest,
                "safety_policy_digest": self.safety_policy.digest,
                "resource_limits_digest": self.resource_limits.digest,
                "phase": self.phase,
                "arm": self.arm,
            }
        )


class ToolCall(_ImmutableContract):
    """A normalized tool call emitted by a runner."""

    name: Annotated[str, Field(min_length=1, max_length=MAX_TOOL_NAME_LENGTH)]
    arguments: JsonObject = Field(default_factory=dict, repr=False)

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_arguments(cls, value: object) -> object:
        validate_json_limits(
            value,
            max_depth=MAX_TOOL_ARGUMENT_DEPTH,
            max_items=MAX_TOOL_ARGUMENT_ITEMS,
        )
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError(f"tool arguments exceed the {MAX_TOOL_ARGUMENT_BYTES}-byte limit")
        return value


class TokenUsage(_ImmutableContract):
    """Provider token counts when the runner can report them."""

    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int | None, Field(ge=0)] = None


class NativeOutputReference(_ImmutableContract):
    """Content-safe reference to protected native runner output."""

    artifact_id: Annotated[str, Field(min_length=1, max_length=128)]
    sha256: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    media_type: Annotated[str, Field(min_length=3, max_length=255)]
    size_bytes: Annotated[int, Field(ge=1, le=MAX_NATIVE_OUTPUT_BYTES)]
    complete: bool = True


class RunAccounting(_ImmutableContract):
    """Bounded effects and resource use observed during one attempt."""

    provider_calls_started: Annotated[int, Field(ge=0, le=MAX_PROVIDER_CALLS_PER_ATTEMPT)] = 0
    provider_calls_completed: Annotated[int, Field(ge=0, le=MAX_PROVIDER_CALLS_PER_ATTEMPT)] = 0
    provider_connections_started: Annotated[int, Field(ge=0, le=MAX_PROVIDER_CALLS_PER_ATTEMPT)] = 0
    tool_network_requests: Annotated[int, Field(ge=0, le=MAX_SHELL_ACTIONS)] = 0
    actions_requested: Annotated[int, Field(ge=0, le=MAX_SHELL_ACTIONS)] = 0
    actions_denied: Annotated[int, Field(ge=0, le=MAX_SHELL_ACTIONS)] = 0
    actions_executed: Annotated[int, Field(ge=0, le=MAX_SHELL_ACTIONS)] = 0
    elapsed_milliseconds: Annotated[int, Field(ge=0)] = 0
    native_output_bytes: Annotated[int, Field(ge=0)] = 0
    disposable_state_bytes: Annotated[int, Field(ge=0)] = 0
    residual_disposable_state_bytes: Annotated[int, Field(ge=0)] = 0
    disposable_state_entries: Annotated[int, Field(ge=0)] = 0
    residual_disposable_state_entries: Annotated[int, Field(ge=0)] = 0
    process_outcome: ProcessOutcome = "not-started"
    forced_termination: bool = False
    cleanup_outcome: CleanupOutcome = "not-required"
    source_store_effect: ProtectedStateEffect = "unchanged"
    original_workspace_effect: ProtectedStateEffect = "unchanged"

    @model_validator(mode="after")
    def validate_counts(self) -> RunAccounting:
        _validate_provider_counts(self)
        _validate_action_counts(self)
        _validate_state_counts(self)
        _validate_cleanup_counts(self)
        return self


def _validate_provider_counts(accounting: RunAccounting) -> None:
    if accounting.provider_calls_completed > accounting.provider_calls_started:
        raise ValueError("completed provider calls cannot exceed started calls")


def _validate_action_counts(accounting: RunAccounting) -> None:
    if accounting.actions_denied + accounting.actions_executed > accounting.actions_requested:
        raise ValueError("denied and executed actions cannot exceed requested actions")


def _validate_state_counts(accounting: RunAccounting) -> None:
    if accounting.residual_disposable_state_bytes > accounting.disposable_state_bytes:
        raise ValueError("residual disposable state cannot exceed its observed peak")
    if accounting.residual_disposable_state_entries > accounting.disposable_state_entries:
        raise ValueError("residual disposable entries cannot exceed their observed peak")


def _validate_cleanup_counts(accounting: RunAccounting) -> None:
    has_state = any(
        (
            accounting.disposable_state_bytes,
            accounting.residual_disposable_state_bytes,
            accounting.disposable_state_entries,
            accounting.residual_disposable_state_entries,
        )
    )
    if accounting.cleanup_outcome == "not-required" and has_state:
        raise ValueError("disposable state requires a cleanup outcome")
    if accounting.cleanup_outcome == "verified" and (
        accounting.residual_disposable_state_bytes or accounting.residual_disposable_state_entries
    ):
        raise ValueError("verified cleanup cannot leave disposable state")


class RunResult(_ImmutableContract):
    """Bounded normalized facts returned by a runner."""

    attempt_id: Annotated[str, Field(min_length=1, max_length=128)]
    effective_policy_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    termination_reason: TerminationReason = "completed"
    tool_calls: Annotated[
        tuple[ToolCall, ...], Field(max_length=MAX_SHELL_ACTIONS, repr=False)
    ] = ()
    final_text: Annotated[str | None, Field(max_length=MAX_BEHAVIOR_TEXT_LENGTH, repr=False)] = None
    model_requested: Annotated[
        str | None, Field(min_length=1, max_length=MAX_MODEL_NAME_LENGTH)
    ] = None
    model_returned: Annotated[str | None, Field(min_length=1, max_length=MAX_MODEL_NAME_LENGTH)] = (
        None
    )
    provider: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    endpoint: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    service_tier: Annotated[str | None, Field(min_length=1, max_length=MAX_SERVICE_TIER_LENGTH)] = (
        None
    )
    usage: TokenUsage | None = None
    accounting: RunAccounting
    native_output: NativeOutputReference | None = None


class ProviderAcceptanceState(StrEnum):
    """What an attempt proves about provider acceptance."""

    NOT_STARTED = "not-started"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


class RunnerErrorCode(StrEnum):
    """Stable operational runner failure categories."""

    PREPARATION = "preparation"
    STARTUP = "startup"
    TRANSPORT = "transport"
    AMBIGUOUS_ACCEPTANCE = "ambiguous-acceptance"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROTOCOL = "protocol"
    PROVIDER_OVERLOADED = "provider-overloaded"
    PROVIDER_CALL_LIMIT = "provider-call-limit"
    OUTPUT_LIMIT = "output-limit"
    SAFETY = "safety"
    CONTRACT = "contract"
    INTERNAL = "internal"


class RunnerError(RuntimeError):
    """Operational runner failure with bounded execution facts."""

    def __init__(
        self,
        code: RunnerErrorCode,
        message: str,
        *,
        reason_code: str | None = None,
        provider_acceptance_state: ProviderAcceptanceState | None = None,
        model_requested: str | None = None,
        model_returned: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        service_tier: str | None = None,
        usage: TokenUsage | None = None,
        accounting: RunAccounting | None = None,
        native_output: NativeOutputReference | None = None,
        effective_policy_digest: str | None = None,
        behavior_output_observed: bool = True,
    ) -> None:
        if reason_code is not None and (
            not isinstance(reason_code, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,80}", reason_code, flags=re.ASCII) is None
        ):
            raise ValueError("runner error reason_code must be a bounded machine code")
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code
        self.provider_acceptance_state = provider_acceptance_state or (
            ProviderAcceptanceState.NOT_STARTED
            if code in {RunnerErrorCode.PREPARATION, RunnerErrorCode.STARTUP}
            else ProviderAcceptanceState.UNKNOWN
        )
        self.model_requested = model_requested
        self.model_returned = model_returned
        self.provider = provider
        self.endpoint = endpoint
        self.service_tier = service_tier
        self.usage = usage
        self.accounting = accounting
        self.native_output = native_output
        self.effective_policy_digest = effective_policy_digest
        self.behavior_output_observed = behavior_output_observed


class RunnerCancelled(asyncio.CancelledError):
    """Cancellation raised after a runner has collected bounded cleanup evidence."""

    def __init__(self, error: RunnerError) -> None:
        super().__init__(str(error))
        if error.code is not RunnerErrorCode.CANCELLED:
            raise ValueError("runner cancellation evidence requires the cancelled error category")
        self.error = error


class Runner(Protocol):
    """Execute prepared candidates through one controlled boundary."""

    @property
    def fingerprint(self) -> str: ...

    @property
    def capability_profile(self) -> CapabilityProfile: ...

    async def run(self, attempt: PreparedAttempt) -> RunResult: ...


class CandidateInvalidError(ValueError):
    """A removal cannot be reconstructed from an otherwise supported original."""


@runtime_checkable
class CandidateAdmission(Protocol):
    """Optional pure reconstruction check before committing sampling resources.

    Implementations must not start a harness, probe runtime configuration or dispatch
    a provider request. Only candidate-local refusals use ``CandidateInvalidError``;
    original evidence, identity and unsupported-original failures remain terminal.
    """

    def validate_candidate(self, attempt: PreparedAttempt) -> None: ...


def prepare_candidate(
    evidence: CaseEvidence,
    execution_plan: ResolvedExecutionPlan,
    remove_ids: tuple[str, ...] | frozenset[str] = (),
    *,
    mutation: TextMutation | None = None,
) -> Candidate:
    """Build a dependency-valid candidate without interpreting native evidence."""

    if execution_plan.case_digest != evidence.digest:
        raise ValueError("execution plan does not belong to the case")
    mutation = (
        TextMutation() if mutation is None else TextMutation.model_validate(mutation.model_dump())
    )
    if not mutation.is_empty:
        if "append-text" not in execution_plan.capability.supported_context_operations:
            raise ValueError("execution plan does not support text mutation")
        for edit in mutation.edits:
            node = evidence.context.by_id.get(edit.context_id)
            if node is None or not node.mutable or node.protected:
                raise MutationError("mutation requires an editable context node")
        if evidence.has_request:
            evidence.materialize(mutation=mutation)
    removed = evidence.context.dependency_closure(remove_ids)
    retained_ids = tuple(node.id for node in evidence.context.nodes if node.id not in removed)
    removed_ids = tuple(node.id for node in evidence.context.nodes if node.id in removed)
    materialization = "exact-request" if evidence.has_request else "source-backed"
    if materialization != execution_plan.candidate_materialization:
        raise ValueError("candidate materialization does not match the execution plan")
    return Candidate(
        case_digest=evidence.digest,
        execution_plan_digest=execution_plan.digest,
        source_artifact_digests=tuple(artifact.sha256 for artifact in evidence.source_artifacts),
        retained_context_ids=retained_ids,
        removed_context_ids=removed_ids,
        materialization=materialization,
        mutation=mutation,
    )


def _validate_attempt_identities(attempt: PreparedAttempt) -> None:
    _validate_attempt_contracts(attempt)
    _validate_candidate_evidence(attempt)


def _validate_attempt_contracts(attempt: PreparedAttempt) -> None:
    evidence = attempt.evidence
    candidate = attempt.candidate
    plan = attempt.execution_plan
    ResolvedExecutionPlan.model_validate(plan.model_dump(mode="python"))
    if candidate.case_digest != evidence.digest or plan.case_digest != evidence.digest:
        raise ValueError("attempt case identities do not match")
    if candidate.execution_plan_digest != plan.digest:
        raise ValueError("candidate does not belong to the execution plan")
    if attempt.target.digest != plan.target_digest:
        raise ValueError("attempt target does not match the execution plan")
    if attempt.safety_policy != plan.safety_policy:
        raise ValueError("attempt safety policy does not match the execution plan")
    if attempt.resource_limits != plan.resource_limits:
        raise ValueError("attempt resource limits do not match the execution plan")


def _validate_candidate_evidence(attempt: PreparedAttempt) -> None:
    evidence = attempt.evidence
    candidate = attempt.candidate
    expected = prepare_candidate(
        evidence,
        attempt.execution_plan,
        frozenset(candidate.removed_context_ids),
        mutation=candidate.mutation,
    )
    if candidate != expected:
        raise ValueError("candidate is not the canonical dependency-valid case partition")
