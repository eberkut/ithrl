"""Typed runner capability, policy, limit, and execution-plan contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.behavior import BehaviorType
from ithrl.context import content_digest

type CaptureType = Literal[
    "provider-request",
    "logical-session",
    "reconstructed",
    "transcript",
    "unknown",
]
type CandidateMaterialization = Literal["exact-request", "source-backed"]
type ContextOperation = Literal["dependency-closed-removal"]
type ProviderTransport = Literal["forbidden", "required"]
type ProviderTransportPolicy = Literal["forbidden", "allowed"]
type ToolNetworkAccess = Literal["forbidden", "possible"]
type ActionVisibility = Literal["not-applicable", "visible", "unknown"]
type ActionDenial = Literal["not-applicable", "enforced", "unverified"]
type ActionExecution = Literal["denied", "possible"]
type ProtectedStateEffect = Literal["unchanged", "may-change"]
type DisposableStateEffect = Literal["none", "writes"]
type InputTokenLimitEnforcement = Literal["unavailable", "pre-dispatch"]
type ProtectedStateEnforcement = Literal["not-applicable", "postcondition", "preventive"]
type DisposableStateLimitEnforcement = Literal["not-applicable", "observed", "enforced"]
type ProcessContainment = Literal["not-applicable", "process-group", "process-tree"]

MAX_PROVIDER_CALLS_PER_ATTEMPT = 16
MAX_WALL_TIME_SECONDS = 24 * 60 * 60
MAX_CANCELLATION_GRACE_SECONDS = 60
MAX_NATIVE_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_DISPOSABLE_STATE_BYTES = 4 * 1024 * 1024 * 1024
MAX_DISPOSABLE_STATE_ENTRIES = 1_000_000


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CapabilityProfile(_ImmutableContract):
    """Versioned execution facts advertised by one runner implementation."""

    schema_version: Literal[1] = 1
    runner_type: Annotated[str, Field(min_length=1, max_length=128)]
    runner_version: Annotated[str, Field(min_length=1, max_length=128)]
    harness: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    runtime_harness_version: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    protocol_version: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    source_schema: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    deterministic: bool
    supported_capture_types: Annotated[tuple[CaptureType, ...], Field(min_length=1)]
    supported_materializations: Annotated[tuple[CandidateMaterialization, ...], Field(min_length=1)]
    supported_context_operations: Annotated[tuple[ContextOperation, ...], Field(min_length=1)]
    reconstruction_method: Annotated[
        str,
        Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
    ]
    supported_targets: Annotated[tuple[BehaviorType, ...], Field(min_length=1)]
    provider_transport: ProviderTransport
    provider_calls_per_attempt: Annotated[int, Field(ge=0, le=MAX_PROVIDER_CALLS_PER_ATTEMPT)]
    tool_network_access: ToolNetworkAccess
    action_visibility: ActionVisibility
    action_denial: ActionDenial
    action_execution: ActionExecution
    source_store_effect: ProtectedStateEffect
    original_workspace_effect: ProtectedStateEffect
    protected_state_enforcement: ProtectedStateEnforcement = "not-applicable"
    disposable_state_effect: DisposableStateEffect
    disposable_state_limit_enforcement: DisposableStateLimitEnforcement = "not-applicable"
    process_containment: ProcessContainment = "not-applicable"
    supports_timeout: bool
    supports_cancellation: bool
    cancellation_grace_seconds: Annotated[int, Field(ge=0, le=MAX_CANCELLATION_GRACE_SECONDS)] = 0
    reports_usage: bool
    input_token_limit_enforcement: InputTokenLimitEnforcement = "unavailable"
    provider: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    endpoint: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    deviations: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = ()

    @field_validator(
        "supported_capture_types",
        "supported_materializations",
        "supported_context_operations",
        "supported_targets",
        "deviations",
        mode="before",
    )
    @classmethod
    def normalize_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_profile(self) -> CapabilityProfile:
        _validate_capability_sequences(self)
        _validate_capability_source(self)
        _validate_provider_transport(self.provider_transport, self.provider_calls_per_attempt)
        _validate_capability_provider(self)
        _validate_capability_actions(self)
        _validate_capability_state(self)
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of the complete capability profile."""

        return content_digest(self.model_dump(mode="json"))

    @classmethod
    def deterministic_fake(cls, *, runner_version: str = "1") -> CapabilityProfile:
        """Return the built-in offline fake-runner capability profile."""

        return cls(
            runner_type="fake",
            runner_version=runner_version,
            deterministic=True,
            supported_capture_types=("provider-request",),
            supported_materializations=("exact-request",),
            supported_context_operations=("dependency-closed-removal",),
            reconstruction_method="exact-request-json-pointer-v1",
            supported_targets=("final-response", "shell-action"),
            provider_transport="forbidden",
            provider_calls_per_attempt=0,
            tool_network_access="forbidden",
            action_visibility="visible",
            action_denial="enforced",
            action_execution="denied",
            source_store_effect="unchanged",
            original_workspace_effect="unchanged",
            protected_state_enforcement="not-applicable",
            disposable_state_effect="none",
            disposable_state_limit_enforcement="not-applicable",
            process_containment="not-applicable",
            supports_timeout=True,
            supports_cancellation=True,
            reports_usage=False,
            provider="fake",
        )


class SafetyPolicy(_ImmutableContract):
    """Maximum effects allowed for one runner attempt."""

    schema_version: Literal[1] = 1
    provider_transport: ProviderTransportPolicy = "forbidden"
    allowed_provider: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    allowed_endpoint: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    tool_network_access: Literal["forbidden"] = "forbidden"
    action_execution: Literal["denied"] = "denied"
    source_store_effect: Literal["unchanged"] = "unchanged"
    original_workspace_effect: Literal["unchanged"] = "unchanged"

    @model_validator(mode="after")
    def validate_destination(self) -> SafetyPolicy:
        destination = (self.allowed_provider, self.allowed_endpoint)
        if self.provider_transport == "allowed" and None in destination:
            raise ValueError("allowed provider transport requires a provider and endpoint")
        if self.provider_transport == "forbidden" and destination != (None, None):
            raise ValueError("a provider destination requires allowed provider transport")
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of the safety policy."""

        return content_digest(self.model_dump(mode="json"))

    @classmethod
    def credential_free(cls) -> SafetyPolicy:
        """Return the policy used by the deterministic local workflow."""

        return cls()


class ResourceLimits(_ImmutableContract):
    """Per-attempt bounds known before runner execution."""

    schema_version: Literal[1] = 1
    max_provider_calls: Annotated[int, Field(ge=0, le=MAX_PROVIDER_CALLS_PER_ATTEMPT)] = 0
    max_input_tokens: Annotated[int | None, Field(ge=1)] = None
    max_wall_time_seconds: Annotated[int, Field(ge=1, le=MAX_WALL_TIME_SECONDS)] = 30
    max_native_output_bytes: Annotated[int, Field(ge=1, le=MAX_NATIVE_OUTPUT_BYTES)] = 1024 * 1024
    max_disposable_state_bytes: Annotated[int, Field(ge=0, le=MAX_DISPOSABLE_STATE_BYTES)] = 0
    max_disposable_state_entries: Annotated[int, Field(ge=0, le=MAX_DISPOSABLE_STATE_ENTRIES)] = (
        10_000
    )

    @property
    def digest(self) -> str:
        """Return the stable identity of the resource limits."""

        return content_digest(self.model_dump(mode="json"))

    @classmethod
    def credential_free(cls) -> ResourceLimits:
        """Return bounded limits for the deterministic local workflow."""

        return cls()


class ResolvedExecutionPlan(_ImmutableContract):
    """One immutable runner plan resolved without starting external work."""

    schema_version: Literal[1] = 1
    case_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    capture_type: CaptureType
    candidate_materialization: CandidateMaterialization
    capability: CapabilityProfile
    harness: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    source_harness_version: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    source_schema: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    model_requested: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    decision_digest: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    runner_fingerprint: Annotated[str, Field(min_length=1, max_length=512)]
    safety_policy: SafetyPolicy
    resource_limits: ResourceLimits
    experiment_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    target_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    target_type: BehaviorType

    @model_validator(mode="after")
    def validate_plan(self) -> ResolvedExecutionPlan:
        """Reject a plan whose embedded execution facts contradict each other."""

        _validate_materialization(self)
        _validate_resolved_transport(self)
        _validate_resolved_effects(self)
        _validate_resolved_limits(self)
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of the fully resolved execution plan."""

        return content_digest(self.model_dump(mode="json"))


type PreflightIssueCode = Literal[
    "runner-type-mismatch",
    "runner-evidence-mismatch",
    "unsupported-capture",
    "unsupported-materialization",
    "unsupported-target",
    "sampled-execution-unsupported",
    "provider-transport-forbidden",
    "provider-call-limit-too-small",
    "input-token-limit-unenforceable",
    "provider-destination-mismatch",
    "tool-network-access-possible",
    "action-not-visible",
    "action-denial-unverified",
    "action-execution-possible",
    "source-store-may-change",
    "workspace-may-change",
    "protected-state-enforcement-insufficient",
    "timeout-unsupported",
    "cancellation-unsupported",
    "runner-unavailable",
]


class PreflightIssue(_ImmutableContract):
    """One stable reason an execution plan is refused."""

    code: PreflightIssueCode
    message: Annotated[str, Field(min_length=1)]


class PreflightReport(_ImmutableContract):
    """Preflight result; creating it starts no calls or actions."""

    schema_version: Literal[1] = 1
    ready: bool
    plan: ResolvedExecutionPlan | None
    issues: tuple[PreflightIssue, ...] = ()
    network_calls_started: Literal[0] = 0
    provider_calls_started: Literal[0] = 0
    actions_executed: Literal[0] = 0

    @model_validator(mode="after")
    def validate_outcome(self) -> PreflightReport:
        if self.ready and (self.plan is None or self.issues):
            raise ValueError("ready preflight requires a plan and no issues")
        if not self.ready and (self.plan is not None or not self.issues):
            raise ValueError("failed preflight requires issues and no plan")
        return self


def _validate_materialization(plan: ResolvedExecutionPlan) -> None:
    exact_request = plan.candidate_materialization == "exact-request"
    if exact_request != (plan.capture_type == "provider-request"):
        raise ValueError("candidate materialization contradicts the captured request type")
    capability = plan.capability
    if plan.capture_type not in capability.supported_capture_types:
        raise ValueError("execution plan capture type is not supported by its capability")
    if plan.candidate_materialization not in capability.supported_materializations:
        raise ValueError("candidate materialization is not supported by its capability")
    if plan.target_type not in capability.supported_targets:
        raise ValueError("execution plan target is not supported by its capability")
    if capability.harness is not None and capability.harness != plan.harness:
        raise ValueError("execution plan harness differs from its capability")
    if capability.source_schema is not None and capability.source_schema != plan.source_schema:
        raise ValueError("execution plan source schema differs from its capability")
    operations = capability.supported_context_operations
    _require_unique("supported_context_operations", operations)
    if "dependency-closed-removal" not in operations:
        raise ValueError("execution plan does not support dependency-closed context removal")


def _validate_resolved_transport(plan: ResolvedExecutionPlan) -> None:
    policy = plan.safety_policy
    capability = plan.capability
    if capability.provider_transport == "required":
        if capability.provider_calls_per_attempt == 0:
            raise ValueError("required provider transport needs at least one planned call")
        if policy.provider_transport != "allowed":
            raise ValueError("required provider transport is forbidden by the safety policy")
        if (
            capability.provider != policy.allowed_provider
            or capability.endpoint != policy.allowed_endpoint
        ):
            raise ValueError("runner destination differs from the safety policy")
    elif capability.provider_calls_per_attempt != 0:
        raise ValueError("forbidden provider transport cannot plan provider calls")


def _validate_resolved_effects(plan: ResolvedExecutionPlan) -> None:
    capability = plan.capability
    if capability.tool_network_access != "forbidden":
        raise ValueError("execution plan permits tool network access")
    _validate_resolved_actions(plan)
    _validate_resolved_state(capability)
    if not capability.supports_timeout or not capability.supports_cancellation:
        raise ValueError("execution plan must support timeout and cancellation")


def _validate_resolved_actions(plan: ResolvedExecutionPlan) -> None:
    capability = plan.capability
    if capability.action_denial != "enforced" or capability.action_execution != "denied":
        raise ValueError("execution plan does not enforce action denial")
    if plan.target_type == "shell-action" and capability.action_visibility != "visible":
        raise ValueError("shell-action execution plan must keep the action visible")


def _validate_resolved_state(capability: CapabilityProfile) -> None:
    if capability.source_store_effect != "unchanged":
        raise ValueError("execution plan may change the preserved source session")
    if capability.original_workspace_effect != "unchanged":
        raise ValueError("execution plan may change the original workspace")
    if (
        capability.process_containment != "not-applicable"
        and capability.protected_state_enforcement != "preventive"
    ):
        raise ValueError("process execution requires preventive protected-state isolation")
    if capability.protected_state_enforcement == "postcondition":
        raise ValueError("execution plan detects protected-state changes but cannot prevent them")


def _validate_resolved_limits(plan: ResolvedExecutionPlan) -> None:
    limits = plan.resource_limits
    capability = plan.capability
    if capability.provider_calls_per_attempt > limits.max_provider_calls:
        raise ValueError("planned provider calls exceed the resource limit")
    if (
        limits.max_input_tokens is not None
        and capability.input_token_limit_enforcement != "pre-dispatch"
    ):
        raise ValueError("input-token limit cannot be enforced before provider dispatch")


def _require_unique(name: str, values: tuple[object, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


def _validate_provider_transport(transport: ProviderTransport, calls: int) -> None:
    if transport == "forbidden" and calls != 0:
        raise ValueError("a runner without provider transport cannot make provider calls")
    if transport == "required" and calls == 0:
        raise ValueError("required provider transport needs at least one provider call")


def _validate_capability_sequences(profile: CapabilityProfile) -> None:
    _require_unique("supported_capture_types", profile.supported_capture_types)
    _require_unique("supported_materializations", profile.supported_materializations)
    _require_unique("supported_context_operations", profile.supported_context_operations)
    _require_unique("supported_targets", profile.supported_targets)
    _require_unique("deviations", profile.deviations)


def _validate_capability_source(profile: CapabilityProfile) -> None:
    if profile.runtime_harness_version is not None and profile.harness is None:
        raise ValueError("runtime_harness_version requires harness")
    if "source-backed" in profile.supported_materializations and (
        profile.harness is None or profile.source_schema is None
    ):
        raise ValueError("source-backed materialization requires a harness and source schema")


def _validate_capability_provider(profile: CapabilityProfile) -> None:
    if profile.provider_transport == "required" and (
        profile.provider is None or profile.endpoint is None
    ):
        raise ValueError("provider transport requires a known provider and endpoint")


def _validate_capability_actions(profile: CapabilityProfile) -> None:
    if profile.action_execution == "possible" and profile.action_denial == "enforced":
        raise ValueError("enforced action denial cannot advertise possible action execution")


def _validate_capability_state(profile: CapabilityProfile) -> None:
    state_is_absent = profile.disposable_state_effect == "none"
    limit_is_not_applicable = profile.disposable_state_limit_enforcement == "not-applicable"
    if state_is_absent != limit_is_not_applicable:
        raise ValueError("disposable-state writes require a declared limit enforcement")
    if profile.process_containment != "not-applicable" and profile.cancellation_grace_seconds == 0:
        raise ValueError("process containment requires a cancellation grace period")
