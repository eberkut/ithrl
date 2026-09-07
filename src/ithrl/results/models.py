"""Persisted result snapshot models."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.case import ProvenanceFacts
from ithrl.context import JsonObject
from ithrl.execution import ResolvedExecutionPlan
from ithrl.experiment import ExperimentDefinition
from ithrl.reduce import MAX_CANDIDATE_RUNS, ReductionCheckpoint

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
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    status: ResultStatus
    case_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    experiment_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    target_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    execution_plan_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    reduced_request_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
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
    network_calls: Literal[0] = 0
    provider_calls: Literal[0] = 0
    actions_executed: Literal[0] = 0
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
    reduced_request: JsonObject
    retained_context_lines: tuple[str, ...]
    experiment_source: str
    experiment: ExperimentDefinition
    provenance: ProvenanceFacts
    execution_plan: ResolvedExecutionPlan
    checkpoint: ReductionCheckpoint
