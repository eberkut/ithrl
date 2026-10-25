"""Private CLI inputs, saved structural detection and atomic fuzz result receipts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ithrl.case.filesystem import (
    atomic_write,
    lstat_regular_file,
    prepare_private_directory,
    read_safe_text,
    reject_symlink_components,
    validate_existing_root,
)
from ithrl.cli.fuzz_output import FuzzOutput
from ithrl.context import content_digest
from ithrl.execution import ResolvedExecutionPlan
from ithrl.experiment import SampledExperimentDefinition
from ithrl.fuzz.configuration import FuzzConfiguration
from ithrl.observations import ObservationIdentity, ObservationStore
from ithrl.results import load_sampling_snapshot, write_sampling_snapshot
from ithrl.results.validation import loads_json
from ithrl.run_state import SampledRunState, SampledRunStateFile

_MAX_BYTES = 64 * 1024 * 1024


class FuzzSessionError(ValueError):
    """Private fuzz metadata is unavailable, unsafe or inconsistent."""


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FuzzProfile(_PrivateModel):
    schema_version: Literal[1] = 1
    kind: Literal["fuzz-profile"] = "fuzz-profile"
    case_path: Path = Field(repr=False)
    original_workspace: Path | None = Field(default=None, repr=False)
    configuration: FuzzConfiguration = Field(repr=False)
    experiment: SampledExperimentDefinition = Field(repr=False)
    execution_plan: ResolvedExecutionPlan = Field(repr=False)
    identity: ObservationIdentity

    @field_validator("case_path", "original_workspace")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("fuzz profile paths must be absolute and contain no traversal")
        return value

    @model_validator(mode="after")
    def consistent_identity(self) -> FuzzProfile:
        plan = self.execution_plan
        if (
            self.identity.case_digest != plan.case_digest
            or self.identity.experiment_digest != self.experiment.digest
            or plan.experiment_digest != self.experiment.digest
            or self.identity.execution_plan_digest != plan.digest
            or self.identity.target_digest != self.experiment.target.digest
            or plan.target_digest != self.experiment.target.digest
            or self.experiment.calibration != self.configuration.calibration
            or plan.resource_limits.max_wall_time_seconds
            != self.configuration.limits.attempt_timeout_seconds
            or self.identity.runner_fingerprint != plan.runner_fingerprint
        ):
            raise ValueError("fuzz profile execution identities differ")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class FuzzSession(_PrivateModel):
    schema_version: Literal[1] = 1
    kind: Literal["fuzz-session"] = "fuzz-session"
    run_id: Annotated[str, Field(pattern=r"^fuzz-[0-9a-f]{32}$")]
    run_directory: Path = Field(repr=False)
    observation_store: Path = Field(repr=False)
    profile: FuzzProfile = Field(repr=False)
    started_at: datetime

    @model_validator(mode="after")
    def canonical_run(self) -> FuzzSession:
        if self.run_directory != self.profile.case_path / ".runs" / self.run_id:
            raise ValueError("fuzz run must belong to its canonical case")
        if not self.observation_store.is_absolute() or ".." in self.observation_store.parts:
            raise ValueError("observation store path must be absolute without traversal")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("fuzz session start needs a UTC offset")
        return self


def _read_private(path: Path) -> str:
    reject_symlink_components(path.absolute())
    metadata = lstat_regular_file(path, label="fuzz metadata")
    if metadata.st_nlink != 1 or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600):
        raise FuzzSessionError("fuzz metadata must be private and single-link")
    raw = read_safe_text(path, max_bytes=_MAX_BYTES, label="fuzz metadata")
    loads_json(raw, label="fuzz metadata")
    return raw


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _save_private(path: Path, model: BaseModel) -> None:
    rendered = (
        json.dumps(model.public(), indent=2)
        if isinstance(model, FuzzOutput)
        else model.model_dump_json(indent=2)
    ) + "\n"
    if len(rendered.encode("utf-8")) > _MAX_BYTES:
        raise FuzzSessionError("fuzz metadata exceeds its byte limit")
    atomic_write(path, rendered)
    _sync_directory(path.parent)


def save_fuzz_profile(path: Path, profile: FuzzProfile) -> None:
    prepare_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        if load_fuzz_profile(path) != profile:
            raise FuzzSessionError(
                "profile already exists with different inputs; choose a new path"
            )
        return
    _save_private(path, profile)


def load_fuzz_profile(path: Path) -> FuzzProfile:
    try:
        return FuzzProfile.model_validate_json(_read_private(path))
    except (OSError, ValueError) as error:
        raise FuzzSessionError(
            "cannot load private fuzz profile; run explicit detection"
        ) from error


def save_fuzz_session(
    session: FuzzSession, state_file: SampledRunStateFile, store: ObservationStore
) -> None:
    state = state_file.load(store)
    _validate_session_state(session, state)
    _save_private(session.run_directory / "fuzz-session.json", session)


def load_fuzz_session(source: Path) -> FuzzSession:
    try:
        root = validate_existing_root(source.absolute())
        session = FuzzSession.model_validate_json(_read_private(root / "fuzz-session.json"))
        live = validate_existing_root(session.run_directory)
        canonical = FuzzSession.model_validate_json(_read_private(live / "fuzz-session.json"))
        if canonical != session:
            raise FuzzSessionError("result session differs from its canonical run")
        state = SampledRunState.model_validate_json(_read_private(live / "sampled-state.json"))
        _validate_session_state(session, state)
        if root != live:
            if root.parent != session.profile.case_path / "results":
                raise FuzzSessionError("fuzz result belongs to another case")
            if {p.name for p in root.iterdir()} != {
                "fuzz-session.json",
                "summary.json",
                "sampling",
                ".sampling.sampling.lock",
            }:
                raise FuzzSessionError("fuzz result has unexpected entries")
            snapshot = load_sampling_snapshot(
                root / "sampling", ObservationStore(session.observation_store)
            )
            _validate_session_state(session, snapshot)
            _read_private(root / "summary.json")
        return session
    except (OSError, ValueError) as error:
        raise FuzzSessionError("cannot load private fuzz session or result") from error


def _validate_session_state(session: FuzzSession, state: SampledRunState) -> None:
    if (
        state.run_id != session.run_id
        or Path(state.source_case) != session.profile.case_path
        or Path(state.observation_store) != session.observation_store
        or state.original_identity != session.profile.identity
        or state.started_at != session.started_at.astimezone(UTC)
    ):
        raise FuzzSessionError("fuzz session differs from its run state")


def publish_fuzz_result(
    session: FuzzSession,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    summary: FuzzOutput,
) -> Path:
    """Caller owns run/store locks; publish one bounded immutable evidence receipt."""

    state = state_file.load(store)
    _validate_session_state(session, state)
    if load_fuzz_session(session.run_directory) != session:
        raise FuzzSessionError("canonical session changed before publication")
    parent = prepare_private_directory(session.profile.case_path / "results")
    destination = parent / f"{session.run_id}-{uuid4().hex}"
    staging = Path(tempfile.mkdtemp(prefix=".fuzz-", dir=parent))
    try:
        _save_private(staging / "fuzz-session.json", session)
        _save_private(
            staging / "summary.json", summary.model_copy(update={"result_path": str(destination)})
        )
        write_sampling_snapshot(destination=staging / "sampling", state=state, store=store)
        _sync_directory(staging)
        staging.rename(destination)
        _sync_directory(parent)
        return destination
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
