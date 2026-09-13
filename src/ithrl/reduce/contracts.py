"""Persisted contracts and identities for deterministic reduction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ithrl.context import JsonObject, content_digest
from ithrl.observations import ObservationIdentity

ALGORITHM_NAME = "hierarchical-ddmin"
ALGORITHM_VERSION = "2"
ALGORITHM_DIGEST = content_digest({"name": ALGORITHM_NAME, "version": ALGORITHM_VERSION})
DEFAULT_MAX_CANDIDATE_RUNS = 100
MAX_CANDIDATE_RUNS = 10_000

type SearchClassification = Literal["preserves", "loses", "invalid"]
type SearchPhase = Literal["chunk", "final-sweep"]
type SearchStatus = Literal["search-1-minimal", "search-incomplete"]


class ReductionError(RuntimeError):
    """Base error for deterministic reduction."""


class ResumeMismatchError(ReductionError):
    """A checkpoint does not belong to the requested reduction."""


class CandidateTrial(BaseModel):
    """One candidate classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_key: str = Field(min_length=1)
    candidate_digest: str | None
    base_removals: tuple[str, ...]
    requested_removals: tuple[str, ...]
    effective_removals: tuple[str, ...]
    target_observed: bool | None
    classification: SearchClassification
    phase: SearchPhase
    runner_executed: bool
    observation_id: str | None = None
    observation_attempt: int | None = Field(default=None, ge=1)
    reused: bool = False

    @model_validator(mode="after")
    def validate_classification(self) -> CandidateTrial:
        self._validate_removals()
        if self.classification == "invalid":
            self._validate_invalid()
        else:
            self._validate_observed()
        return self

    def _validate_removals(self) -> None:
        if self.base_removals != tuple(sorted(set(self.base_removals))):
            raise ValueError("base_removals must contain sorted unique node IDs")
        if len(set(self.requested_removals)) != len(self.requested_removals):
            raise ValueError("requested_removals must not contain duplicates")
        if self.effective_removals != tuple(sorted(set(self.effective_removals))):
            raise ValueError("effective_removals must contain sorted unique node IDs")
        if not set(self.base_removals) <= set(self.effective_removals):
            raise ValueError("effective_removals must include base_removals")
        if self.trial_key != trial_key(self.phase, self.base_removals, self.requested_removals):
            raise ValueError("trial_key does not match the trial inputs")

    def _validate_observed(self) -> None:
        if any(
            value is None
            for value in (
                self.candidate_digest,
                self.target_observed,
                self.observation_id,
                self.observation_attempt,
            )
        ):
            raise ValueError("valid trials require a candidate observation")
        if (self.classification == "preserves") != self.target_observed:
            raise ValueError("classification must match target_observed")
        if not self.runner_executed:
            raise ValueError("observed trials must record runner execution")

    def _validate_invalid(self) -> None:
        if any(
            value is not None
            for value in (
                self.candidate_digest,
                self.target_observed,
                self.observation_id,
                self.observation_attempt,
            )
        ):
            raise ValueError("invalid trials cannot have an observation")
        if self.runner_executed:
            raise ValueError("invalid trials cannot execute the runner")
        if self.effective_removals != self.base_removals:
            raise ValueError("invalid trials cannot change effective removals")


class ReductionCheckpoint(BaseModel):
    """Complete deterministic search state needed for exact continuation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    case_digest: str = Field(min_length=1)
    experiment_digest: str = Field(min_length=1)
    runner_fingerprint: str = Field(min_length=1)
    target_digest: str = Field(min_length=1)
    algorithm_digest: str = Field(pattern=r"^[0-9a-f]{64}$", default=ALGORITHM_DIGEST)
    removed_context_ids: tuple[str, ...]
    trials: tuple[CandidateTrial, ...] = Field(max_length=MAX_CANDIDATE_RUNS)
    failed_candidate_observation_ids: tuple[str, ...] = Field(
        default=(), max_length=MAX_CANDIDATE_RUNS
    )
    candidate_runs_used: int = Field(ge=0, le=MAX_CANDIDATE_RUNS)
    final_sweep_complete: bool = False

    @model_validator(mode="after")
    def validate_run_count(self) -> ReductionCheckpoint:
        if self.removed_context_ids != tuple(sorted(set(self.removed_context_ids))):
            raise ValueError("removed_context_ids must contain sorted unique node IDs")
        trial_keys = tuple(trial.trial_key for trial in self.trials)
        if len(set(trial_keys)) != len(trial_keys):
            raise ValueError("checkpoint trial keys must not contain duplicates")
        if len(set(self.failed_candidate_observation_ids)) != len(
            self.failed_candidate_observation_ids
        ):
            raise ValueError("failed candidate observation IDs must not contain duplicates")
        executed = sum(trial.runner_executed for trial in self.trials) + len(
            self.failed_candidate_observation_ids
        )
        if self.candidate_runs_used != executed:
            raise ValueError("candidate_runs_used must match executed candidate trials")
        return self


class ReductionResult(BaseModel):
    """Best candidate found by the search that keeps dependencies valid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SearchStatus
    case_digest: str
    experiment_digest: str
    original_request: JsonObject
    reduced_request: JsonObject
    removed_context_ids: tuple[str, ...]
    retained_context_ids: tuple[str, ...]
    trials: tuple[CandidateTrial, ...]
    candidate_runs_used: int
    final_sweep_complete: bool
    checkpoint: ReductionCheckpoint


def validate_checkpoint_identity(
    checkpoint: ReductionCheckpoint,
    *,
    case_digest: str,
    identity: ObservationIdentity,
) -> None:
    """Reject a checkpoint that belongs to another reduction."""

    expected = (
        case_digest,
        identity.experiment_digest,
        identity.runner_fingerprint,
        identity.target_digest,
        ALGORITHM_DIGEST,
    )
    actual = (
        checkpoint.case_digest,
        checkpoint.experiment_digest,
        checkpoint.runner_fingerprint,
        checkpoint.target_digest,
        checkpoint.algorithm_digest,
    )
    if actual != expected:
        raise ResumeMismatchError(
            "checkpoint identities do not match the case, experiment, runner, target, and algorithm"
        )


def trial_key(
    phase: SearchPhase,
    base_removals: tuple[str, ...] | frozenset[str],
    requested_removals: tuple[str, ...],
) -> str:
    """Return the stable identity for a deterministic candidate trial."""

    return content_digest(
        {
            "phase": phase,
            "base_removals": sorted(base_removals),
            "requested_removals": list(requested_removals),
        }
    )
