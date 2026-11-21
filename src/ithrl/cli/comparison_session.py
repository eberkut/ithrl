"""Private structural profiles and canonical CLI comparison sessions."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.case.filesystem import (
    atomic_write,
    lstat_regular_file,
    prepare_target_path,
    read_safe_text,
    reject_symlink_components,
    validate_existing_root,
)
from ithrl.comparison.contracts import BoundComparison, ComparisonConfiguration
from ithrl.context import content_digest
from ithrl.execution import ResolvedExecutionPlan
from ithrl.experiment import NativeRunnerConfig, SampledExperimentDefinition
from ithrl.observations import ObservationIdentity, ObservationStore
from ithrl.results.validation import loads_json
from ithrl.run_state import SampledRunState, SampledRunStateFile

_MAX_BYTES = 64 * 1024 * 1024


class ComparisonSessionError(ValueError):
    """Private comparison metadata is unavailable, unsafe or inconsistent."""


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ComparisonProfile(_PrivateModel):
    schema_version: Literal[1] = 1
    kind: Literal["comparison-profile"] = "comparison-profile"
    case_path: Path = Field(repr=False)
    original_workspace: Path | None = Field(default=None, repr=False)
    configuration: ComparisonConfiguration = Field(repr=False)
    experiment: SampledExperimentDefinition = Field(repr=False)
    execution_plan: ResolvedExecutionPlan = Field(repr=False)
    identity: ObservationIdentity
    binding: BoundComparison = Field(repr=False)

    @field_validator("case_path", "original_workspace")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("comparison paths must be absolute without traversal")
        return value

    @model_validator(mode="after")
    def consistent_identity(self) -> ComparisonProfile:
        plan = self.execution_plan
        runner = self.experiment.runner
        if (
            isinstance(runner, NativeRunnerConfig)
            and runner.codex_replay != self.configuration.codex_replay
        ):
            raise ValueError("comparison replay configuration differs from its runner")
        if (
            self.identity.case_digest != plan.case_digest
            or self.identity.experiment_digest != self.experiment.digest
            or plan.experiment_digest != self.experiment.digest
            or self.identity.execution_plan_digest != plan.digest
            or self.identity.target_digest != self.experiment.target.digest
            or plan.target_digest != self.experiment.target.digest
            or plan.codex_replay != self.configuration.codex_replay
            or self.identity.runner_fingerprint != plan.runner_fingerprint
            or self.identity.algorithm_digest != self.configuration.policy_digest
            or self.binding.procedure != self.configuration.procedure
            or self.binding.reference.case_digest != plan.case_digest
            or self.binding.reference.execution_plan_digest != plan.digest
            or plan.resource_limits.max_wall_time_seconds
            != self.configuration.limits.attempt_timeout_seconds
        ):
            raise ValueError("comparison profile identities differ")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class ComparisonSession(_PrivateModel):
    schema_version: Literal[1] = 1
    kind: Literal["comparison-session"] = "comparison-session"
    run_id: Annotated[str, Field(pattern=r"^compare-[0-9a-f]{32}$")]
    run_directory: Path = Field(repr=False)
    observation_store: Path = Field(repr=False)
    profile: ComparisonProfile = Field(repr=False)
    started_at: datetime

    @model_validator(mode="after")
    def canonical_run(self) -> ComparisonSession:
        if self.run_directory != self.profile.case_path / ".runs" / self.run_id:
            raise ValueError("comparison run must belong to its canonical case")
        if not self.observation_store.is_absolute() or ".." in self.observation_store.parts:
            raise ValueError("observation store must be absolute without traversal")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("comparison session needs an aware start time")
        return self

    @property
    def result_path(self) -> Path:
        return self.profile.case_path / "results" / f"{self.run_id}.json"


def _read(path: Path) -> str:
    reject_symlink_components(path.absolute())
    metadata = lstat_regular_file(path, label="comparison metadata")
    if metadata.st_nlink != 1 or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ComparisonSessionError("comparison metadata must be private and single-link")
    raw = read_safe_text(path, max_bytes=_MAX_BYTES, label="comparison metadata")
    loads_json(raw, label="comparison metadata")
    return raw


def save_comparison_profile(path: Path, profile: ComparisonProfile) -> None:
    if not path.parent.exists():
        prepare_target_path(path)
    validate_existing_root(path.parent)
    if path.exists() or path.is_symlink():
        if load_comparison_profile(path) != profile:
            raise ComparisonSessionError("comparison profile already binds different inputs")
        return
    atomic_write(path, profile.model_dump_json(indent=2) + "\n")


def load_comparison_profile(path: Path) -> ComparisonProfile:
    try:
        return ComparisonProfile.model_validate_json(_read(path))
    except (OSError, ValueError) as error:
        raise ComparisonSessionError("cannot load private comparison profile") from error


def validate_session_state(session: ComparisonSession, state: SampledRunState) -> None:
    if (
        state.run_id != session.run_id
        or Path(state.source_case) != session.profile.case_path
        or Path(state.observation_store) != session.observation_store
        or state.original_identity != session.profile.identity
        or state.original_candidate_digest != session.profile.binding.reference.digest
        or state.started_at != session.started_at.astimezone(UTC)
        or state.comparison_progress is None
        or state.comparison_progress.binding != session.profile.binding
    ):
        raise ComparisonSessionError("comparison session differs from its run state")


def save_comparison_session(
    session: ComparisonSession, state_file: SampledRunStateFile, store: ObservationStore
) -> None:
    validate_session_state(session, state_file.load(store))
    atomic_write(
        session.run_directory / "comparison-session.json",
        session.model_dump_json(indent=2) + "\n",
    )


def load_comparison_session(source: Path) -> ComparisonSession:
    try:
        root = validate_existing_root(source.absolute())
        session = ComparisonSession.model_validate_json(_read(root / "comparison-session.json"))
        if root != session.run_directory:
            raise ComparisonSessionError("resume requires the canonical comparison run")
        state = SampledRunState.model_validate_json(_read(root / "sampled-state.json"))
        validate_session_state(session, state)
        return session
    except (OSError, ValueError) as error:
        raise ComparisonSessionError("cannot load canonical comparison session") from error
