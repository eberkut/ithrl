"""Shared contracts for selecting and preserving observed model decisions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.behavior import BehaviorObservation, BehaviorTarget, evaluate_target
from ithrl.context import (
    ContextGraph,
    SourceRecordSelector,
    content_digest,
    validate_source_reference,
)
from ithrl.timestamps import validate_rfc3339_timestamp

type EvidenceSupport = Literal["supported", "unsupported"]
type ExperimentalSupport = Literal["supported", "unvalidated", "unsupported"]
type DecisionLimitation = Literal[
    "experimental-support-unvalidated",
    "opaque-context-frozen",
]

MAX_DECISIONS = 4096
MAX_TARGETS_PER_DECISION = 32


class _ImmutableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DecisionSelector(_ImmutableContract):
    """Stable harness-native identity of one observed decision record."""

    schema_version: Literal[1] = 1
    harness: Annotated[str, Field(min_length=1, max_length=64)]
    session_id: Annotated[str, Field(min_length=1, max_length=1024)]
    turn_id: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Annotated[str, Field(min_length=1, max_length=128)]
    native_id: Annotated[str, Field(min_length=1, max_length=1024)]
    source: Annotated[str, Field(min_length=1, max_length=1024)]
    context_before_line: Annotated[int, Field(ge=1, le=250_000)]
    record: SourceRecordSelector
    record_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return validate_source_reference(value)

    @model_validator(mode="after")
    def validate_boundary(self) -> DecisionSelector:
        if self.context_before_line > self.record.line:
            raise ValueError("decision context boundary must not follow the decision record")
        return self

    @property
    def digest(self) -> str:
        """Return the stable identity of this selector."""

        return content_digest(self.model_dump(mode="json", exclude={"record_digest"}))

    @property
    def decision_id(self) -> str:
        """Return an opaque identifier suitable for deterministic selection."""

        return f"decision-{self.digest[:16]}"


class DecisionCaseFacts(_ImmutableContract):
    """Adapter facts required to compile a decision without interpreting it again."""

    schema_version: Literal[1] = 1
    adapter: Annotated[str, Field(min_length=1, max_length=128)]
    adapter_version: Annotated[str, Field(min_length=1, max_length=64)]
    provider: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    endpoint: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    model_requested: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    model_returned: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    capture_type: Literal["logical-session"] = "logical-session"
    completeness: Literal["complete"] = "complete"
    replay_state: Literal["harness-local"] = "harness-local"
    environment: Literal["same-harness-account"] = "same-harness-account"
    source_media_type: Literal["application/x-ndjson"] = "application/x-ndjson"
    source_schema: Annotated[str, Field(min_length=1, max_length=128)]


class TargetCandidate(_ImmutableContract):
    """One mechanically observable behavior and its current support status."""

    schema_version: Literal[1] = 1
    ordinal: Annotated[int, Field(ge=1, le=MAX_TARGETS_PER_DECISION)]
    target: BehaviorTarget
    evidence_support: EvidenceSupport
    experimental_support: ExperimentalSupport

    @property
    def target_id(self) -> str:
        return f"target-{self.target.type}-{self.ordinal}"


class TargetSummary(_ImmutableContract):
    """Content-free target metadata safe for decision listings."""

    schema_version: Literal[1] = 1
    target_id: Annotated[
        str, Field(pattern=r"^target-(?:final-response|shell-action)-(?:[1-9]|[12][0-9]|3[0-2])$")
    ]
    type: Literal["final-response", "shell-action"]
    evidence_support: EvidenceSupport
    experimental_support: ExperimentalSupport


class DecisionSummary(_ImmutableContract):
    """Content-free metadata for one selectable completed decision."""

    schema_version: Literal[1] = 1
    decision_id: Annotated[str, Field(pattern=r"^decision-[0-9a-f]{16}$")]
    harness: Annotated[str, Field(min_length=1, max_length=64)]
    kind: Annotated[str, Field(min_length=1, max_length=128)]
    source_line: Annotated[int, Field(ge=1, le=250_000)]
    observed_at: Annotated[str, Field(min_length=1, max_length=64)]
    completeness: Literal["complete"] = "complete"
    targets: Annotated[tuple[TargetSummary, ...], Field(min_length=1, max_length=32)]
    limitations: tuple[DecisionLimitation, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="observed_at")

    @model_validator(mode="after")
    def validate_targets(self) -> DecisionSummary:
        target_ids = tuple(target.target_id for target in self.targets)
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("decision summary contains duplicate targets")
        return self


class DecisionEvidence(_ImmutableContract):
    """Selected decision, observed behavior, and support facts persisted in a case."""

    schema_version: Literal[1] = 1
    selector: DecisionSelector
    observed_at: Annotated[str, Field(min_length=1, max_length=64)]
    completeness: Literal["complete"] = "complete"
    observation: BehaviorObservation
    targets: Annotated[
        tuple[TargetCandidate, ...], Field(min_length=1, max_length=MAX_TARGETS_PER_DECISION)
    ]
    selected_target_id: Annotated[
        str, Field(pattern=r"^target-(?:final-response|shell-action)-(?:[1-9]|[12][0-9]|3[0-2])$")
    ]
    limitations: tuple[DecisionLimitation, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="observed_at")

    @model_validator(mode="after")
    def validate_targets(self) -> DecisionEvidence:
        ordinals = tuple(candidate.ordinal for candidate in self.targets)
        target_ids = tuple(candidate.target_id for candidate in self.targets)
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("decision evidence contains duplicate target ordinals")
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("decision evidence contains duplicate targets")
        if self.selected_target_id not in target_ids:
            raise ValueError("selected_target_id is not available for this decision")
        if any(
            not evaluate_target(candidate.target, self.observation) for candidate in self.targets
        ):
            raise ValueError("decision targets must match the observed behavior")
        return self

    @property
    def decision_id(self) -> str:
        return self.selector.decision_id

    @property
    def selected_target(self) -> BehaviorTarget:
        return next(
            candidate.target
            for candidate in self.targets
            if candidate.target_id == self.selected_target_id
        )


class SelectedDecision(_ImmutableContract):
    """One adapter decision with a selected target, ready for case compilation."""

    schema_version: Literal[1] = 1
    case: DecisionCaseFacts
    evidence: DecisionEvidence
    context: ContextGraph


class DecisionDraft(_ImmutableContract):
    """Adapter output ready for deterministic target selection and case compilation."""

    schema_version: Literal[1] = 1
    case: DecisionCaseFacts
    selector: DecisionSelector
    observed_at: Annotated[str, Field(min_length=1, max_length=64)]
    observation: BehaviorObservation
    targets: Annotated[
        tuple[TargetCandidate, ...], Field(min_length=1, max_length=MAX_TARGETS_PER_DECISION)
    ]
    limitations: tuple[DecisionLimitation, ...] = ()

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        return validate_rfc3339_timestamp(value, field="observed_at")

    @model_validator(mode="after")
    def validate_targets(self) -> DecisionDraft:
        ordinals = tuple(candidate.ordinal for candidate in self.targets)
        candidate_ids = tuple(candidate.target_id for candidate in self.targets)
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("decision draft contains duplicate target ordinals")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("decision draft contains duplicate targets")
        if any(
            not evaluate_target(candidate.target, self.observation) for candidate in self.targets
        ):
            raise ValueError("decision targets must match the observed behavior")
        return self

    @property
    def decision_id(self) -> str:
        return self.selector.decision_id

    def summarize(self) -> DecisionSummary:
        """Return content-free metadata for selection and display."""

        return DecisionSummary(
            decision_id=self.decision_id,
            harness=self.selector.harness,
            kind=self.selector.kind,
            source_line=self.selector.record.line,
            observed_at=self.observed_at,
            targets=tuple(
                TargetSummary(
                    target_id=candidate.target_id,
                    type=candidate.target.type,
                    evidence_support=candidate.evidence_support,
                    experimental_support=candidate.experimental_support,
                )
                for candidate in self.targets
            ),
            limitations=self.limitations,
        )

    def select(self, target_id: str, *, context: ContextGraph) -> SelectedDecision:
        """Select one target without changing the adapter-produced decision."""

        return SelectedDecision(
            case=self.case,
            context=context,
            evidence=DecisionEvidence(
                selector=self.selector,
                observed_at=self.observed_at,
                observation=self.observation,
                targets=self.targets,
                selected_target_id=target_id,
                limitations=self.limitations,
            ),
        )
