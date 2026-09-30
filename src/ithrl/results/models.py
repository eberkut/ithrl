"""Persisted result snapshot models."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.case import ProvenanceFacts
from ithrl.context import JsonObject
from ithrl.execution import CandidateMaterialization, ResolvedExecutionPlan
from ithrl.experiment import ExperimentDefinition, SampledExperimentDefinition
from ithrl.reduce import MAX_CANDIDATE_RUNS, ReductionCheckpoint
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
    "valid-sample-limit", "attempt-limit", "unsafe-retry", "safety-refusal", "user-interrupt"
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
