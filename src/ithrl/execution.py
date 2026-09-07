"""Resolve runner capabilities and check execution requirements."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.behavior import BehaviorType
from ithrl.context import content_digest
from ithrl.experiment import ExperimentDefinition

type NetworkAccess = Literal["forbidden", "required"]
type ActionExecution = Literal["denied", "possible"]
type SourceStoreEffect = Literal["unchanged", "may-write"]


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CapabilityProfile(_ImmutableContract):
    """Versioned facts a runner must advertise before plan resolution."""

    schema_version: Literal[1] = 1
    runner_type: Annotated[str, Field(min_length=1)]
    runner_version: Annotated[str, Field(min_length=1)]
    deterministic: bool
    supported_targets: Annotated[tuple[BehaviorType, ...], Field(min_length=1)]
    network_access: NetworkAccess
    provider_calls_required: Annotated[int, Field(ge=0)]
    action_execution: ActionExecution
    source_store_effect: SourceStoreEffect

    @model_validator(mode="after")
    def validate_unique_targets(self) -> CapabilityProfile:
        if len(set(self.supported_targets)) != len(self.supported_targets):
            raise ValueError("supported_targets must not contain duplicates")
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of this capability profile."""

        return content_digest(self.model_dump(mode="json"))

    @classmethod
    def deterministic_fake(cls, *, runner_version: str = "1") -> CapabilityProfile:
        """Return the built-in offline fake-runner capability profile."""

        return cls(
            runner_type="fake",
            runner_version=runner_version,
            deterministic=True,
            supported_targets=("final-response", "shell-action"),
            network_access="forbidden",
            provider_calls_required=0,
            action_execution="denied",
            source_store_effect="unchanged",
        )


class ResolvedExecutionPlan(_ImmutableContract):
    """A deterministic plan that requires no credentials or network access."""

    schema_version: Literal[1] = 1
    runner_type: Annotated[str, Field(min_length=1)]
    runner_version: Annotated[str, Field(min_length=1)]
    runner_fingerprint: Annotated[str, Field(min_length=1)]
    capability_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    experiment_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    target_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    target_type: BehaviorType
    deterministic: Literal[True] = True
    network_calls_planned: Literal[0] = 0
    provider_calls_planned: Literal[0] = 0
    action_execution: Literal["denied"] = "denied"
    source_store_effect: Literal["unchanged"] = "unchanged"

    @property
    def digest(self) -> str:
        """Return the stable identity of the fully resolved execution plan."""

        return content_digest(self.model_dump(mode="json"))


type PreflightIssueCode = Literal[
    "runner-type-mismatch",
    "unsupported-target",
    "non-deterministic-runner",
    "network-not-forbidden",
    "provider-calls-required",
    "action-execution-not-denied",
    "source-store-may-change",
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


def resolve_execution_plan(
    capability: CapabilityProfile,
    experiment: ExperimentDefinition,
    *,
    runner_fingerprint: str,
) -> PreflightReport:
    """Resolve the offline fake-runner plan or return all safety refusals."""

    issues: list[PreflightIssue] = []
    target_type = experiment.target.type
    if capability.runner_type != experiment.runner.type:
        issues.append(
            PreflightIssue(
                code="runner-type-mismatch",
                message=(
                    f"experiment requires runner {experiment.runner.type!r}, "
                    f"profile provides {capability.runner_type!r}"
                ),
            )
        )
    if target_type not in capability.supported_targets:
        issues.append(
            PreflightIssue(
                code="unsupported-target",
                message=f"runner does not support target {target_type!r}",
            )
        )
    if not capability.deterministic:
        issues.append(
            PreflightIssue(
                code="non-deterministic-runner",
                message="the selected runner is not deterministic",
            )
        )
    if capability.network_access != "forbidden":
        issues.append(
            PreflightIssue(
                code="network-not-forbidden",
                message="runner does not prove that network access is forbidden",
            )
        )
    if capability.provider_calls_required != 0:
        issues.append(
            PreflightIssue(
                code="provider-calls-required",
                message="runner requires provider calls",
            )
        )
    if capability.action_execution != "denied":
        issues.append(
            PreflightIssue(
                code="action-execution-not-denied",
                message="runner does not prove that action execution is denied",
            )
        )
    if capability.source_store_effect != "unchanged":
        issues.append(
            PreflightIssue(
                code="source-store-may-change",
                message="runner may change the source session store",
            )
        )

    if issues:
        return PreflightReport(ready=False, plan=None, issues=tuple(issues))

    plan = ResolvedExecutionPlan(
        runner_type=capability.runner_type,
        runner_version=capability.runner_version,
        runner_fingerprint=runner_fingerprint,
        capability_digest=capability.digest,
        experiment_digest=experiment.digest,
        target_digest=experiment.target.digest,
        target_type=target_type,
    )
    return PreflightReport(ready=True, plan=plan)
