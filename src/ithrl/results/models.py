"""Persisted result snapshot models."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.case import ProvenanceFacts
from ithrl.context import JsonObject
from ithrl.execution import CandidateMaterialization, ResolvedExecutionPlan
from ithrl.experiment import ExperimentDefinition, SampledExperimentDefinition
from ithrl.observations import SamplingBlockEvidence
from ithrl.reduce import (
    MAX_CANDIDATE_RUNS,
    ReductionCheckpoint,
    SampledConfirmation,
    SampledReductionCheckpoint,
)
from ithrl.runner.contracts import TokenUsage

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

type ResultStatus = Literal[
    "calibrated",
    "reduced",
    "stopped",
    "interrupted",
    "confirmation-failed",
    "not-observed",
    "failed",
]
type CalibrationResult = Literal["not-run", "observed", "not-observed", "failed"]
type SearchResult = Literal["not-run", "search-1-minimal", "search-incomplete"]
type ConfirmationResult = Literal["not-run", "confirmed-preserving", "failed"]


class ResultSnapshotError(ValueError):
    """A result snapshot is unsafe, incomplete, or malformed."""


class _StrictImmutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResultSummary(_StrictImmutableModel):
    """Versioned evidence summary that is useful without the observation store."""

    schema_version: Literal[1] = 1
    kind: Literal["reduction"] = "reduction"
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    status: ResultStatus
    case_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    experiment_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    target_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    execution_plan_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    candidate_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    candidate_materialization: CandidateMaterialization
    source_artifact_digests: tuple[Annotated[str, Field(pattern=_DIGEST_PATTERN)], ...] = ()
    reduced_request_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = None
    retained_context_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    provenance_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    checkpoint_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    runner_fingerprint: Annotated[str, Field(min_length=1, max_length=512)]
    algorithm_digest: Annotated[str, Field(min_length=1, max_length=512)]
    original_context_count: Annotated[int, Field(ge=0)]
    retained_context_count: Annotated[int, Field(ge=0)]
    original_context_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    retained_context_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    removed_context_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    calibration_result: CalibrationResult
    calibration_observation_ids: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    search_result: SearchResult
    confirmation_result: ConfirmationResult
    confirmation_observation_ids: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    candidate_runs_used: Annotated[int, Field(ge=0, le=MAX_CANDIDATE_RUNS)]
    max_candidate_runs: Annotated[int | None, Field(ge=1, le=MAX_CANDIDATE_RUNS)] = None
    observations_recorded: Annotated[int, Field(ge=0)]
    network_calls: Annotated[int, Field(ge=0)] = 0
    provider_calls: Annotated[int, Field(ge=0)] = 0
    provider_calls_may_have_started: Annotated[int, Field(ge=0)] = 0
    actions_executed: Annotated[int, Field(ge=0)] = 0
    source_case: Annotated[str, Field(min_length=1)]
    resume_of: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    next_action: Annotated[str | None, Field(min_length=1)] = None
    next_command: Annotated[str | None, Field(min_length=1)] = None
    next_command_shell: Literal["posix", "powershell"] | None = None

    @model_validator(mode="after")
    def validate_summary(self) -> ResultSummary:
        """Reject internally inconsistent claims and context partitions."""

        from ithrl.results.summary import validate_summary

        validate_summary(self)
        return self


class ResultSnapshot(_StrictImmutableModel):
    """Validated contents of one result directory."""

    root: Path
    summary: ResultSummary
    reduced_request: JsonObject | None
    retained_context_lines: tuple[str, ...]
    experiment_source: str
    experiment: ExperimentDefinition
    provenance: ProvenanceFacts
    execution_plan: ResolvedExecutionPlan
    checkpoint: ReductionCheckpoint


type CalibrationOutcome = Literal[
    "measurable", "not-measurable", "inconclusive", "incomplete", "refused"
]
type CalibrationStopReason = Literal[
    "valid-sample-limit",
    "attempt-limit",
    "unsafe-retry",
    "safety-refusal",
    "user-interrupt",
    "wall-time-budget",
]
type CalibrationProtectedEffect = Literal["unchanged", "may-change", "unknown"]


class CalibrationSummary(_StrictImmutableModel):
    """Versioned summary for bounded full-context calibration only."""

    schema_version: Literal[1] = 1
    kind: Literal["calibration"] = "calibration"
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    outcome: CalibrationOutcome
    stop_reason: CalibrationStopReason
    case_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    experiment_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    target_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    execution_plan_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    candidate_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    candidate_materialization: CandidateMaterialization
    source_artifact_digests: tuple[Annotated[str, Field(pattern=_DIGEST_PATTERN)], ...] = ()
    request_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = None
    retained_context_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    provenance_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    runner_fingerprint: Annotated[str, Field(min_length=1, max_length=512)]
    algorithm_digest: Annotated[str, Field(min_length=1, max_length=512)]
    original_context_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    retained_context_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    calibration_observation_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    observations_recorded: Annotated[int, Field(ge=0)]
    matches: Annotated[int, Field(ge=0)]
    no_matches: Annotated[int, Field(ge=0)]
    invalid_observations: Annotated[int, Field(ge=0)]
    runner_errors: Annotated[int, Field(ge=0)]
    terminal_error_category: Annotated[
        str | None, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ] = None
    provider_calls_started: Annotated[int | None, Field(ge=0)]
    provider_calls_completed: Annotated[int | None, Field(ge=0)]
    provider_calls_possible: Annotated[int, Field(ge=0)]
    provider_calls_may_have_started: Annotated[int, Field(ge=0)]
    actions_requested: Annotated[int | None, Field(ge=0)]
    actions_denied: Annotated[int | None, Field(ge=0)]
    actions_executed: Annotated[int | None, Field(ge=0)]
    tool_network_requests: Annotated[int | None, Field(ge=0)]
    usage: TokenUsage | None
    elapsed_milliseconds: Annotated[int | None, Field(ge=0)]
    effective_policy_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)]
    source_store_effect: CalibrationProtectedEffect
    original_workspace_effect: CalibrationProtectedEffect
    source_case: Annotated[str, Field(min_length=1)]
    resume_of: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    next_action: Annotated[str | None, Field(min_length=1)] = None
    next_command: Annotated[str | None, Field(min_length=1)] = None
    next_command_shell: Literal["posix", "powershell"] | None = None

    @model_validator(mode="after")
    def validate_summary(self) -> CalibrationSummary:
        from ithrl.results.summary import validate_calibration_summary

        validate_calibration_summary(self)
        return self


class CalibrationSnapshot(_StrictImmutableModel):
    """Validated contents of one calibration result directory."""

    root: Path
    summary: CalibrationSummary
    request: JsonObject | None
    retained_context_lines: tuple[str, ...]
    experiment_source: str
    experiment: SampledExperimentDefinition
    provenance: ProvenanceFacts
    execution_plan: ResolvedExecutionPlan


class SampledReductionEvidence(_StrictImmutableModel):
    """Content-safe sampled claims, separate from integrated result-directory snapshots.

    The checkpoint validates the declared terminal-neighbor witness. Establishing that the
    generator enumerated every dependency-valid neighbor remains an integration requirement.
    """

    schema_version: Literal[1] = 1
    kind: Literal["sampled-reduction-evidence"] = "sampled-reduction-evidence"
    checkpoint: SampledReductionCheckpoint
    search_result: Literal["search-reduced", "search-incomplete"]
    selected_candidate_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    selected_at: datetime
    calibration_observation_ids: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    calibration_attempt_ids: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    confirmation: SampledConfirmation | None = None

    @property
    def confirmation_result(
        self,
    ) -> Literal[
        "not-run", "confirmed-preserving", "loses", "inconclusive", "incomplete", "refused"
    ]:
        if self.confirmation is None:
            return "not-run"
        from ithrl.statistics import FixedSampleOutcome

        match self.confirmation.outcome:
            case FixedSampleOutcome.PRESERVES:
                return "confirmed-preserving"
            case FixedSampleOutcome.LOSES:
                return "loses"
            case FixedSampleOutcome.INCONCLUSIVE:
                return "inconclusive"
            case FixedSampleOutcome.INCOMPLETE:
                return "incomplete"
            case None:
                return "refused"

    @field_validator("selected_at")
    @classmethod
    def normalize_selected_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selected_at must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_sampled_claims(self) -> SampledReductionEvidence:
        checkpoint = self.checkpoint
        expected = "search-reduced" if checkpoint.final_sweep_complete else "search-incomplete"
        if self.search_result != expected:
            raise ValueError("sampled search result must match the final sweep witness")
        if self.selected_candidate_digest != checkpoint.best_candidate_digest:
            raise ValueError("selected candidate must match the checkpoint best candidate")
        search_blocks = checkpoint.blocks
        if any(
            self.selected_at < block.plan.frozen_at
            or any(self.selected_at < item.observation.recorded_at for item in block.attempts)
            for block in search_blocks
        ):
            raise ValueError("candidate selection must follow all search evidence")
        blocks = search_blocks
        if self.confirmation is not None:
            if any(not block.eligible for block in search_blocks):
                raise ValueError("ineligible search evidence forbids final confirmation")
            confirmation = self.confirmation.evidence
            plan = confirmation.plan
            if (
                plan.procedure != checkpoint.reduction
                or plan.original_identity != checkpoint.original_identity
                or plan.original_candidate_digest != checkpoint.original_candidate_digest
                or plan.candidate_digest != self.selected_candidate_digest
                or plan.candidate_identity != checkpoint.best_candidate_identity
            ):
                raise ValueError(
                    "confirmation must bind the frozen original and selected candidate"
                )
            if plan.frozen_at <= self.selected_at:
                raise ValueError("confirmation must be frozen after candidate selection")
            blocks = (*blocks, confirmation)
        self._validate_evidence_reuse(blocks)
        return self

    def _validate_evidence_reuse(self, blocks: tuple[SamplingBlockEvidence, ...]) -> None:
        for references, label in (
            (tuple(block.plan.block_id for block in blocks), "block"),
            (
                (
                    *self.calibration_observation_ids,
                    *(i for b in blocks for i in b.observation_ids),
                ),
                "observation",
            ),
            (
                (*self.calibration_attempt_ids, *(i for b in blocks for i in b.attempt_ids)),
                "attempt",
            ),
        ):
            if len(set(references)) != len(references):
                raise ValueError(f"calibration, search and confirmation cannot reuse {label} IDs")
