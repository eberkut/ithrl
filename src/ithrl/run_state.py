"""Atomic mutable state for an in-progress reduction run."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ithrl.case.contracts import CaseError
from ithrl.case.filesystem import read_safe_bytes
from ithrl.context import content_digest
from ithrl.fuzz.progress import FuzzCampaignProgress
from ithrl.observations import (
    Observation,
    ObservationArm,
    ObservationIdentity,
    ObservationRole,
    ObservationStore,
    ObservationStoreError,
    SamplingArm,
    SamplingAttempt,
    SamplingBlockEvidence,
    SamplingBlockPlan,
    SamplingBudgetAllowance,
)
from ithrl.path_safety import UnsafePathError, private_file_lock
from ithrl.reduce.contracts import ReductionCheckpoint, SampledReductionCheckpoint
from ithrl.runner import ProviderAcceptanceState

_MAX_RUN_STATE_BYTES = 64 * 1024 * 1024


class RunStateError(ValueError):
    """An in-progress run state is unavailable, unsafe, or inconsistent."""


class RunStateBusyError(RunStateError):
    """Another process holds the exclusive lock for an in-progress run."""


class RunState(BaseModel):
    """The complete state needed to continue an interrupted process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    source_case: Annotated[str, Field(min_length=1, max_length=4096)]
    observation_store: Annotated[str, Field(min_length=1, max_length=4096)]
    checkpoint: ReductionCheckpoint
    status: Literal["active", "completed"] = "active"
    result_path: Annotated[str | None, Field(min_length=1)] = None

    @model_validator(mode="after")
    def validate_completion(self) -> RunState:
        if (self.status == "completed") != (self.result_path is not None):
            raise ValueError("only completed run state may reference a result")
        return self


class RunStateFile:
    """A private, atomically replaced state file outside immutable results."""

    def __init__(self, path: Path) -> None:
        self.path = path if path.is_absolute() else Path.cwd() / path

    @classmethod
    def create(
        cls,
        *,
        case_root: Path,
        run_id: str,
        observation_store: Path,
        checkpoint: ReductionCheckpoint,
    ) -> RunStateFile:
        runs_root = case_root / ".runs"
        _reject_unsafe_path(runs_root)
        runs_root.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            runs_root.chmod(0o700)
        root = runs_root / run_id
        _reject_unsafe_path(root)
        root.mkdir(mode=0o700, exist_ok=False)
        if os.name == "posix":
            root.chmod(0o700)
        state_file = cls(root / "state.json")
        state_file._write(
            RunState(
                run_id=run_id,
                source_case=str(case_root.absolute()),
                observation_store=str(observation_store.absolute()),
                checkpoint=checkpoint,
            )
        )
        return state_file

    @classmethod
    def open(cls, source: Path) -> RunStateFile:
        path = source / "state.json" if source.is_dir() else source
        return cls(path)

    def load(self) -> RunState:
        """Load and validate one private state file without accepting a symbolic link."""

        _reject_unsafe_path(self.path)
        try:
            metadata = self.path.lstat()
        except OSError as error:
            raise RunStateError(f"run state is unavailable: {self.path}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RunStateError("run state must be a regular file, not a link")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RunStateError("run state must have mode 0600")
        if metadata.st_size > _MAX_RUN_STATE_BYTES:
            raise RunStateError(f"run state exceeds the {_MAX_RUN_STATE_BYTES}-byte limit")
        if os.name == "posix":
            parent_mode = stat.S_IMODE(self.path.parent.stat().st_mode)
            if parent_mode != 0o700:
                raise RunStateError("run-state directory must have mode 0700")
        try:
            source = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RunStateError(f"run state is unavailable: {self.path}") from error

        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise RunStateError(f"run state contains duplicate key {key!r}")
                result[key] = value
            return result

        try:
            payload = json.loads(source, object_pairs_hook=reject_duplicate_keys)
            return RunState.model_validate(payload)
        except (json.JSONDecodeError, RecursionError, ValidationError) as error:
            raise RunStateError(f"invalid run state: {error}") from error

    def save_checkpoint(self, checkpoint: ReductionCheckpoint) -> None:
        current = self.load()
        if current.status == "completed":
            raise RunStateError(f"run already completed: {current.result_path}")
        self._write(current.model_copy(update={"checkpoint": checkpoint}))

    @contextmanager
    def acquire_lock(self) -> Generator[None]:
        """Prevent concurrent execution against the active run."""

        lock_path = self.path.with_name("run.lock")
        _reject_unsafe_path(lock_path)
        try:
            lock = private_file_lock(lock_path)
        except UnsafePathError as error:
            raise RunStateError("run lock file is unsafe") from error
        try:
            with lock:
                yield
        except Timeout as error:
            raise RunStateBusyError("another process is already using the active run") from error

    def complete(self, result_path: Path) -> None:
        current = self.load()
        self._write(
            current.model_copy(
                update={"status": "completed", "result_path": str(result_path.absolute())}
            )
        )

    def _write(self, state: RunState) -> None:
        _reject_unsafe_path(self.path.parent)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        rendered = json.dumps(
            state.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if len(rendered.encode("utf-8")) + 1 > _MAX_RUN_STATE_BYTES:
            raise RunStateError(f"run state exceeds the {_MAX_RUN_STATE_BYTES}-byte limit")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{rendered}\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "posix":
                temporary.chmod(0o600)
            temporary.replace(self.path)
            _sync_directory(self.path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _reject_unsafe_path(path: Path) -> None:
    if ".." in path.parts:
        raise RunStateError("run-state path cannot contain '..'")
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise RunStateError(f"run-state path contains a symbolic link: {current}")


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


type _StateDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type _PrivateRunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class _SampledStateContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SamplingObservationReference(_SampledStateContract):
    """Membership and prepared-attempt binding; observation facts remain in SQLite."""

    observation_id: Annotated[str, Field(min_length=1, max_length=128)]
    attempt_id: Annotated[str, Field(pattern=r"^attempt-[0-9a-f]{32}$")]
    attempt_digest: _StateDigest
    schedule_position: Annotated[int, Field(ge=0)]
    independence_evidence_digest: _StateDigest | None = None


class SamplingBlockProgress(_SampledStateContract):
    """One immutable admitted plan and its append-only ordered observation references."""

    plan: SamplingBlockPlan
    observations: Annotated[tuple[SamplingObservationReference, ...], Field(max_length=20_000)] = ()
    archived: bool = False

    @model_validator(mode="after")
    def validate_positions(self) -> SamplingBlockProgress:
        positions = tuple(item.schedule_position for item in self.observations)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("sampling references must have unique increasing positions")
        if positions and positions[-1] >= len(self.plan.schedule):
            raise ValueError("sampling reference exceeds the frozen schedule")
        return self


class SamplingBudgetApproval(_SampledStateContract):
    """Recorded administrative allowance; this reference grants no execution authority."""

    approval_id: _PrivateRunId
    approved_at: datetime
    allowance: SamplingBudgetAllowance

    @field_validator("approved_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _sampled_time(value)


class SamplingDispatchReservation(_SampledStateContract):
    """Durable charge and exact reconciliation key, published before native dispatch."""

    block_id: Annotated[str, Field(min_length=1, max_length=128)]
    block_digest: _StateDigest
    schedule_position: Annotated[int, Field(ge=0)]
    arm: SamplingArm
    attempt: Annotated[int, Field(ge=1)]
    attempt_id: Annotated[str, Field(pattern=r"^attempt-[0-9a-f]{32}$")]
    attempt_digest: _StateDigest
    reserved_at: datetime
    provider_calls: Annotated[int, Field(ge=0, le=2)]
    runner_seconds: Annotated[int, Field(ge=1)]

    @field_validator("reserved_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _sampled_time(value)


class SampledRunUsage(_SampledStateContract):
    """Known call lower bounds, conservative upper bounds and separately charged dispatch.

    Missing provider accounting remains explicit, including accepted attempts whose evidence
    proves at least one call but cannot establish the complete count.
    """

    actual_provider_calls: int
    possible_provider_calls: int
    unknown_provider_attempts: int
    pending_provider_calls: int
    runner_milliseconds: int
    unknown_runner_attempts: int
    known_tokens: int
    unknown_token_attempts: int
    wall_time_seconds: float
    pending_runner_seconds: int

    @property
    def charged_provider_calls(self) -> int:
        return (
            self.actual_provider_calls + self.possible_provider_calls + self.pending_provider_calls
        )


class SampledFinalSelection(_SampledStateContract):
    """One selected artifact and holdout plan, frozen before any confirmation dispatch."""

    checkpoint_digest: _StateDigest
    selected_at: datetime
    plan: SamplingBlockPlan
    search_stop_reason: (
        Literal[
            "candidate-limit",
            "trial-limit",
            "provider-call-budget",
            "wall-time-budget",
            "freshness-budget",
            "attempt-limit",
        ]
        | None
    ) = None

    @field_validator("selected_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _sampled_time(value)

    @model_validator(mode="after")
    def validate_plan(self) -> SampledFinalSelection:
        if self.plan.role != "confirmation" or self.plan.frozen_at <= self.selected_at:
            raise ValueError("a fresh confirmation plan must follow selection")
        return self


class SampledRunState(_SampledStateContract):
    """Private sampled progress, with administrative history outside frozen execution identities."""

    run_id: _PrivateRunId
    source_case: Annotated[str, Field(min_length=1, max_length=4096)]
    observation_store: Annotated[str, Field(min_length=1, max_length=4096)]
    started_at: datetime
    original_identity: ObservationIdentity
    original_candidate_digest: _StateDigest
    provider_calls_per_attempt: Annotated[int, Field(ge=0, le=2)]
    approvals: Annotated[tuple[SamplingBudgetApproval, ...], Field(min_length=1, max_length=10_000)]
    calibration_observation_ids: Annotated[tuple[str, ...], Field(max_length=16)] = ()
    blocks: Annotated[tuple[SamplingBlockProgress, ...], Field(max_length=20_001)] = ()
    pending: SamplingDispatchReservation | None = None
    checkpoint: SampledReductionCheckpoint | None = None
    selection: SampledFinalSelection | None = None
    fuzz_progress: FuzzCampaignProgress | None = Field(default=None, repr=False)

    @field_validator("started_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _sampled_time(value)

    @model_validator(mode="after")
    def validate_history(self) -> SampledRunState:
        if self.fuzz_progress is not None and self.checkpoint is not None:
            raise ValueError("fuzz and reduction checkpoints cannot share a run")
        if any(item.allowance.run_id != self.run_id for item in self.approvals):
            raise ValueError("budget approvals must belong to this run")
        if len({item.approval_id for item in self.approvals}) != len(self.approvals):
            raise ValueError("budget approval IDs must be unique")
        if self.approvals[0].approved_at > self.started_at:
            raise ValueError("initial allowance must precede run start")
        for earlier, later in zip(self.approvals, self.approvals[1:], strict=False):
            _validate_budget_extension(earlier, later)
        if len({item.plan.block_id for item in self.blocks}) != len(self.blocks):
            raise ValueError("block IDs must be unique")
        previous_time = self.started_at
        confirmations = tuple(b for b in self.blocks if b.plan.role == "confirmation")
        if len(confirmations) > 1 or (confirmations and self.blocks[-1] != confirmations[0]):
            raise ValueError("only one final confirmation block is permitted")
        for index, progress in enumerate(self.blocks):
            plan = progress.plan
            if plan.original_identity != self.original_identity:
                raise ValueError("block original identity must match the run")
            if plan.original_candidate_digest != self.original_candidate_digest:
                raise ValueError("block original materialization must match the run")
            if plan.provider_calls_per_attempt != self.provider_calls_per_attempt:
                raise ValueError("blocks must preserve the exact run profile call bound")
            if plan.frozen_at < previous_time:
                raise ValueError("blocks must follow run chronology")
            previous_time = plan.frozen_at
            if index + 1 < len(self.blocks) and not progress.archived:
                raise ValueError("only the final block may remain active")
            if self.blocks and plan.procedure != self.blocks[0].plan.procedure:
                raise ValueError("blocks must preserve the frozen sampling procedure")
        references = tuple(item for block in self.blocks for item in block.observations)
        observation_ids = (
            *self.calibration_observation_ids,
            *(r.observation_id for r in references),
        )
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("observation references must be unique across the run")
        if len({item.attempt_id for item in references}) != len(references):
            raise ValueError("prepared attempts must be unique across blocks")
        if self.pending is not None:
            reservation = self.pending
            if not self.blocks or self.blocks[-1].archived:
                raise ValueError("dispatch requires an active block")
            plan = self.blocks[-1].plan
            if reservation.block_id != plan.block_id or reservation.block_digest != plan.digest:
                raise ValueError("dispatch must reference the exact frozen block")
            if reservation.schedule_position >= len(plan.schedule):
                raise ValueError("dispatch exceeds the frozen schedule")
            if reservation.arm != plan.schedule[reservation.schedule_position]:
                raise ValueError("dispatch arm must match the frozen schedule")
            if reservation.provider_calls != plan.provider_calls_per_attempt:
                raise ValueError("dispatch charge must match the exact profile bound")
            if not plan.frozen_at <= reservation.reserved_at < plan.expires_at:
                raise ValueError("dispatch must be reserved within frozen freshness")
            if reservation.attempt_id in {item.attempt_id for item in references}:
                raise ValueError("dispatch cannot repeat a published prepared attempt")
        if self.selection is not None:
            selection, checkpoint = self.selection, self.checkpoint
            plan = selection.plan
            holdout_id = content_digest({"run_id": self.run_id, "role": "confirmation"})
            if plan.block_id != f"confirmation-{holdout_id}" or plan.schedule_seed != int(
                holdout_id[:16], 16
            ):
                raise ValueError("confirmation must use the single frozen run holdout")
            if self.fuzz_progress is not None:
                fuzz = self.fuzz_progress
                selected = next(
                    (t for t in fuzz.trials if t.mutation == fuzz.selected_mutation), None
                )
                if (
                    selection.checkpoint_digest != fuzz.selection_digest
                    or selected is None
                    or selected.plan is None
                    or plan.candidate_digest != selected.plan.candidate_digest
                    or plan.candidate_identity != self.original_identity
                    or plan.original_identity != self.original_identity
                    or plan.original_candidate_digest != self.original_candidate_digest
                    or plan.procedure != selected.plan.procedure
                ):
                    raise ValueError("confirmation must bind the frozen fuzz selection")
            elif checkpoint is None or selection.checkpoint_digest != content_digest(
                checkpoint.model_dump(mode="json")
            ):
                raise ValueError("selected search checkpoint cannot change")
            if checkpoint is not None and (
                plan.procedure != checkpoint.reduction
                or plan.original_identity != checkpoint.original_identity
                or plan.original_candidate_digest != checkpoint.original_candidate_digest
                or plan.candidate_identity != checkpoint.best_candidate_identity
                or plan.candidate_digest != checkpoint.best_candidate_digest
            ):
                raise ValueError("confirmation must bind the selected candidate and original")
            confirmation = tuple(b for b in self.blocks if b.plan.role == "confirmation")
            if len(confirmation) > 1 or (
                confirmation
                and (self.blocks[-1] != confirmation[0] or confirmation[0].plan != plan)
            ):
                raise ValueError("selection permits only its single final confirmation block")
        return self

    @property
    def allowance(self) -> SamplingBudgetAllowance:
        return self.approvals[-1].allowance

    def validate_observations(self, store: ObservationStore) -> None:
        """Resolve immutable references and validate any derived checkpoint against SQLite."""

        if Path(self.observation_store) != store.path.absolute():
            raise RunStateError("sampled run observation-store identity does not match")
        SampledRunStateFile._validate_authority(self, store)


def _sampled_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sampled state times must include a UTC offset")
    return value.astimezone(UTC)


def _validate_budget_extension(
    earlier: SamplingBudgetApproval, later: SamplingBudgetApproval
) -> None:
    if later.approved_at < earlier.approved_at:
        raise ValueError("budget approvals must follow chronological order")
    original, extended = earlier.allowance, later.allowance
    limits = ("max_provider_calls", "max_wall_time_seconds", "max_candidate_comparisons")
    if any(getattr(extended, name) < getattr(original, name) for name in limits):
        raise ValueError("administrative budget limits cannot decrease")
    if original.max_tokens is None:
        if extended.max_tokens is not None:
            raise ValueError("an unbounded token allowance cannot become smaller")
    elif extended.max_tokens is not None and extended.max_tokens < original.max_tokens:
        raise ValueError("token allowance cannot decrease")
    fixed = (
        "run_id",
        "confirmation_provider_calls",
        "confirmation_runner_seconds",
        "inter_attempt_delay_seconds",
        "token_accounting",
    )
    if any(getattr(extended, name) != getattr(original, name) for name in fixed):
        raise ValueError("budget extensions must preserve confirmation reserves and accounting")


@dataclass(frozen=True, slots=True, repr=False)
class _SampledRunSnapshot:
    """Private in-memory evidence resolved by one fresh owned state read."""

    state: SampledRunState
    calibration: tuple[Observation, ...]
    blocks: tuple[SamplingBlockEvidence, ...]
    usage: SampledRunUsage


class SampledRunStateFile:
    """Owned, atomic sampled progress; authoritative observations are never replayed."""

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()
        self._owner: tuple[int, int] | None = None
        self._store_path: Path | None = None

    @classmethod
    def create(
        cls,
        *,
        case_root: Path,
        run_id: str,
        store: ObservationStore,
        started_at: datetime,
        original_identity: ObservationIdentity,
        original_candidate_digest: str,
        provider_calls_per_attempt: int,
        approval: SamplingBudgetApproval,
        calibration_observation_ids: tuple[str, ...] = (),
    ) -> SampledRunStateFile:
        try:
            state = SampledRunState(
                run_id=run_id,
                source_case=str(case_root.absolute()),
                observation_store=str(store.path.absolute()),
                started_at=started_at,
                original_identity=original_identity,
                original_candidate_digest=original_candidate_digest,
                provider_calls_per_attempt=provider_calls_per_attempt,
                approvals=(approval,),
                calibration_observation_ids=calibration_observation_ids,
            )
        except ValidationError as error:
            raise RunStateError("invalid sampled run initialization") from error
        runs_root = case_root / ".runs"
        _reject_sampled_path(runs_root)
        runs_root.mkdir(mode=0o700, exist_ok=True)
        _private_directory(runs_root)
        root = runs_root / state.run_id
        root.mkdir(mode=0o700, exist_ok=False)
        state_file = cls(root / "sampled-state.json")
        with state_file.acquire_lock(store):
            state_file._validate_authority(state, store)
            state_file._write(state)
        return state_file

    @classmethod
    def open(cls, source: Path) -> SampledRunStateFile:
        _reject_sampled_path(source)
        return cls(source / "sampled-state.json" if source.is_dir() else source)

    @contextmanager
    def acquire_lock(self, store: ObservationStore) -> Generator[None]:
        """Own this run and sampled store from reconciliation through durable publication."""

        if self._owner is not None:
            raise RunStateBusyError("sampled run ownership is already active")
        _reject_sampled_path(self.path.parent)
        _private_directory(self.path.parent)
        lock_path = self.path.with_name("run.lock")
        _reject_sampled_path(lock_path)
        try:
            lock = private_file_lock(lock_path)
        except UnsafePathError as error:
            raise RunStateError("sampled run lock file is unsafe") from error
        try:
            lock.acquire()
        except Timeout as error:
            raise RunStateBusyError("another process owns sampled run progress") from error
        try:
            with store.acquire_sampling_lock():
                self._owner = (os.getpid(), threading.get_ident())
                self._store_path = store.path.absolute()
                try:
                    yield
                finally:
                    self._owner = None
                    self._store_path = None
        finally:
            lock.release()

    def _require_lock(self, store: ObservationStore) -> None:
        if (
            self._owner != (os.getpid(), threading.get_ident())
            or self._store_path != store.path.absolute()
        ):
            raise RunStateError("sampled progress requires owned run and observation-store locks")

    def load(self, store: ObservationStore) -> SampledRunState:
        """Read bounded private state and cross-check every cached fact against SQLite."""

        state, _, _ = self._read_validated(store)
        return state

    def _read_validated(
        self, store: ObservationStore
    ) -> tuple[SampledRunState, tuple[Observation, ...], tuple[SamplingBlockEvidence, ...]]:
        self._require_lock(store)
        _reject_sampled_path(self.path)
        _private_directory(self.path.parent)
        _private_regular_file(self.path)
        try:
            raw = read_safe_bytes(
                self.path, max_bytes=_MAX_RUN_STATE_BYTES, label="sampled run state"
            )
            _private_regular_file(self.path)
            # Validate JSON directly to preserve strict tuple/datetime JSON decoding.
            json.loads(raw, object_pairs_hook=_sampled_unique_keys)
            state = SampledRunState.model_validate_json(raw)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValidationError,
            CaseError,
        ) as error:
            raise RunStateError("invalid or unavailable sampled run state") from error
        if Path(state.observation_store) != store.path.absolute():
            raise RunStateError("sampled run observation-store identity does not match")
        calibration, blocks = self._validate_authority(state, store)
        return state, calibration, blocks

    def evidence(self, store: ObservationStore) -> tuple[SamplingBlockEvidence, ...]:
        _, _, blocks = self._read_validated(store)
        return blocks

    def snapshot(self, store: ObservationStore, *, now: datetime) -> _SampledRunSnapshot:
        """Resolve fresh authority and its accounting once under the owned locks."""

        state, calibration, blocks = self._read_validated(store)
        return _SampledRunSnapshot(
            state=state,
            calibration=calibration,
            blocks=blocks,
            usage=self._usage(state, calibration, blocks, now=now),
        )

    @staticmethod
    def _hydrate(progress: SamplingBlockProgress, store: ObservationStore) -> SamplingBlockEvidence:
        attempts: list[SamplingAttempt] = []
        try:
            resolved = store.get_many(item.observation_id for item in progress.observations)
        except (ObservationStoreError, ValueError) as error:
            raise RunStateError(
                "sampled progress references unavailable observation evidence"
            ) from error
        for reference, observation in zip(progress.observations, resolved, strict=True):
            if (
                observation.attempt_evidence.attempt_id != reference.attempt_id
                or observation.attempt_evidence.attempt_digest != reference.attempt_digest
            ):
                raise RunStateError("sampled progress prepared-attempt identity does not match")
            attempts.append(
                SamplingAttempt(
                    schedule_position=reference.schedule_position,
                    observation=observation,
                    independence_evidence_digest=reference.independence_evidence_digest,
                )
            )
        try:
            return SamplingBlockEvidence(plan=progress.plan, attempts=tuple(attempts))
        except ValidationError as error:
            raise RunStateError("sampled progress has inconsistent block membership") from error

    @staticmethod
    def _validate_authority(
        state: SampledRunState, store: ObservationStore
    ) -> tuple[tuple[Observation, ...], tuple[SamplingBlockEvidence, ...]]:
        observations = SampledRunStateFile._calibration(state, store)
        blocks = tuple(SampledRunStateFile._hydrate(progress, store) for progress in state.blocks)
        if blocks and blocks[-1].plan.selected_at is not None:
            selected_at = blocks[-1].plan.selected_at
            if any(
                attempt.observation.recorded_at > selected_at
                for block in blocks[:-1]
                for attempt in block.attempts
            ) or any(observation.recorded_at > selected_at for observation in observations):
                raise RunStateError("final change selection must follow all exploratory evidence")
        attempt_ids = [item.attempt_evidence.attempt_id for item in observations]
        attempt_ids.extend(attempt_id for block in blocks for attempt_id in block.attempt_ids)
        if len(set(attempt_ids)) != len(attempt_ids):
            raise RunStateError("sampled run contains reused prepared attempts")
        if state.pending is not None and state.pending.attempt_id in attempt_ids:
            raise RunStateError("pending dispatch duplicates a published attempt")
        if state.pending is not None and (
            state.pending.schedule_position != SampledRunStateFile._next_position(blocks[-1])
        ):
            raise RunStateError("pending dispatch does not follow the unfinished frozen schedule")
        checkpoint = state.checkpoint
        if state.fuzz_progress is not None:
            from ithrl.fuzz.campaign import validate_campaign_progress

            validate_campaign_progress(state, observations, blocks)
        if checkpoint is not None:
            by_digest = {block.plan.digest: block for block in blocks}
            if (
                checkpoint.original_identity != state.original_identity
                or checkpoint.original_candidate_digest != state.original_candidate_digest
                or (state.blocks and checkpoint.reduction != state.blocks[0].plan.procedure)
                or any(by_digest.get(block.plan.digest) != block for block in checkpoint.blocks)
            ):
                raise RunStateError("sampled checkpoint differs from authoritative block progress")
        if state.selection is not None and state.fuzz_progress is None:
            selection = state.selection
            search = tuple(block for block in blocks if block.plan.role == "search")
            if checkpoint is None or checkpoint.blocks != search:
                raise RunStateError(
                    "selected checkpoint must include all authoritative search evidence"
                )
            if any(
                selection.selected_at < block.plan.frozen_at
                or any(selection.selected_at < a.observation.recorded_at for a in block.attempts)
                for block in search
            ) or any(selection.selected_at < o.recorded_at for o in observations):
                raise RunStateError("selection must follow calibration and search evidence")
        return observations, blocks

    @staticmethod
    def _calibration(state: SampledRunState, store: ObservationStore) -> tuple[Observation, ...]:
        observations: list[Observation] = []
        try:
            resolved = store.get_many(state.calibration_observation_ids)
        except (ObservationStoreError, ValueError) as error:
            raise RunStateError("calibration accounting references unavailable evidence") from error
        for observation in resolved:
            if (
                observation.role is not ObservationRole.CALIBRATION
                or observation.identity != state.original_identity
                or observation.candidate_digest != state.original_candidate_digest
                or observation.arm is not ObservationArm.BASELINE
            ):
                raise RunStateError("calibration accounting does not match original run identity")
            observations.append(observation)
        return tuple(observations)

    def usage(self, store: ObservationStore, *, now: datetime) -> SampledRunUsage:
        state, calibration, blocks = self._read_validated(store)
        return self._usage(state, calibration, blocks, now=now)

    @staticmethod
    def _usage(
        state: SampledRunState,
        calibration: tuple[Observation, ...],
        blocks: tuple[SamplingBlockEvidence, ...],
        *,
        now: datetime,
    ) -> SampledRunUsage:
        now = _sampled_time(now)
        if now < state.started_at:
            raise RunStateError("run clock precedes its recorded start")
        observations = list(calibration)
        observations.extend(attempt.observation for block in blocks for attempt in block.attempts)
        actual = possible = missing_provider = runner = missing_runner = tokens = missing_tokens = 0
        for observation in observations:
            evidence = observation.attempt_evidence
            accounting = evidence.accounting
            known = (
                accounting.provider_calls_started
                if accounting is not None
                else int(evidence.provider_call_started)
            )
            actual += known
            upper = max(
                known,
                evidence.provider_calls_possible,
                state.provider_calls_per_attempt if evidence.provider_call_started else 0,
            )
            possible += upper - known
            if (
                accounting is None
                and evidence.provider_acceptance_state is not ProviderAcceptanceState.NOT_STARTED
            ):
                missing_provider += 1
            if accounting is None:
                missing_runner += 1
            else:
                runner += accounting.elapsed_milliseconds
            if evidence.usage is not None:
                tokens += evidence.usage.input_tokens + evidence.usage.output_tokens
            elif evidence.provider_acceptance_state is not ProviderAcceptanceState.NOT_STARTED:
                missing_tokens += 1
        pending = state.pending
        return SampledRunUsage(
            actual_provider_calls=actual,
            possible_provider_calls=possible,
            unknown_provider_attempts=missing_provider,
            pending_provider_calls=0 if pending is None else pending.provider_calls,
            runner_milliseconds=runner,
            unknown_runner_attempts=missing_runner,
            known_tokens=tokens,
            unknown_token_attempts=missing_tokens,
            wall_time_seconds=(now - state.started_at).total_seconds(),
            pending_runner_seconds=0 if pending is None else pending.runner_seconds,
        )

    def admit_block(self, store: ObservationStore, plan: SamplingBlockPlan) -> SampledRunState:
        """Record an externally admitted plan; whole-block budget scheduling is separate."""

        current = self.load(store)
        if current.selection is not None and plan != current.selection.plan:
            raise RunStateError("only the frozen final comparison may follow selection")
        if current.pending is not None or (current.blocks and not current.blocks[-1].archived):
            raise RunStateError("previous sampled block must be reconciled and archived")
        return self._publish(
            current, store, blocks=(*current.blocks, SamplingBlockProgress(plan=plan))
        )

    def continue_block(
        self, store: ObservationStore, *, expected_plan_digest: str, now: datetime
    ) -> SamplingBlockEvidence:
        """Prove exact partial-block continuation; never clear or repeat an unresolved dispatch."""

        current = self.load(store)
        if current.pending is not None:
            raise RunStateError("pending dispatch must be reconciled before continuation")
        if not current.blocks or current.blocks[-1].archived:
            raise RunStateError("no active sampled block is available")
        block = self._hydrate(current.blocks[-1], store)
        if block.plan.digest != expected_plan_digest:
            raise RunStateError("continuation requires the exact frozen block identity")
        now = _sampled_time(now)
        if not block.plan.frozen_at <= now < block.plan.expires_at:
            raise RunStateError("sampled block freshness has expired")
        if block.attempts and now < block.attempts[-1].observation.recorded_at:
            raise RunStateError("continuation clock precedes recorded sampling progress")
        if block.complete or not block.eligible:
            raise RunStateError("sampled block does not permit incomplete continuation")
        usage = self.usage(store, now=now)
        if usage.wall_time_seconds >= current.allowance.max_wall_time_seconds:
            raise RunStateError("sampled run wall-time allowance is exhausted")
        return block

    def reserve_dispatch(
        self, store: ObservationStore, reservation: SamplingDispatchReservation
    ) -> SampledRunState:
        block = self.continue_block(
            store, expected_plan_digest=reservation.block_digest, now=reservation.reserved_at
        )
        position = self._next_position(block)
        if reservation.schedule_position != position:
            raise RunStateError("dispatch must follow the frozen unfinished schedule")
        plan = block.plan
        identity, digest = _sampling_arm_identity(plan, reservation.arm)
        previous = store.list_compatible(
            identity,
            candidate_digest=digest,
            role=ObservationRole(plan.role),
            arm=ObservationArm(reservation.arm),
        )
        expected_attempt = 1 + max((item.attempt for item in previous), default=0)
        if reservation.attempt != expected_attempt:
            raise RunStateError("dispatch must pin the next authoritative arm-local attempt")
        current = self.load(store)
        usage = self.usage(store, now=reservation.reserved_at)
        if (
            usage.charged_provider_calls + reservation.provider_calls
            > current.allowance.max_provider_calls
        ):
            raise RunStateError("dispatch exceeds the remaining provider-call allowance")
        return self._publish(current, store, pending=reservation)

    @staticmethod
    def _next_position(block: SamplingBlockEvidence) -> int:
        position = block.next_schedule_position
        if position is None:
            raise RunStateError("frozen sampling schedule is exhausted")
        return position

    def append_observation(
        self,
        store: ObservationStore,
        observation: Observation,
        *,
        independence_evidence_digest: str | None = None,
    ) -> SampledRunState:
        """Validate the pinned dispatch, append once, then atomically publish its reference."""

        current = self.load(store)
        reference = self._reserved_reference(current, observation, independence_evidence_digest)
        updated = self._with_reference(current, reference)
        # Membership must validate before appending immutable evidence. Missing new record is
        # supplied locally only for this check; cached checkpoint is invalidated by progress.
        progress = updated.blocks[-1]
        earlier = self._hydrate(current.blocks[-1], store)
        try:
            SamplingBlockEvidence(
                plan=progress.plan,
                attempts=(
                    *earlier.attempts,
                    SamplingAttempt(
                        schedule_position=reference.schedule_position,
                        observation=observation,
                        independence_evidence_digest=reference.independence_evidence_digest,
                    ),
                ),
            )
        except ValidationError as error:
            raise RunStateError("reserved observation is not valid frozen membership") from error
        store.append(observation)
        updated = self._refresh_checkpoint(updated, store)
        self._validate_authority(updated, store)
        self._write(updated)
        return updated

    def reconcile(
        self, store: ObservationStore, *, independence_evidence_digest: str | None = None
    ) -> SampledRunState:
        """Publish an already appended exact dispatch, without performing another attempt."""

        current = self.load(store)
        reservation = current.pending
        if reservation is None:
            return current
        plan = current.blocks[-1].plan
        identity, digest = _sampling_arm_identity(plan, reservation.arm)
        observation = store.get_attempt(
            identity,
            candidate_digest=digest,
            role=ObservationRole(plan.role),
            arm=ObservationArm(reservation.arm),
            attempt=reservation.attempt,
        )
        if observation is None:
            raise RunStateError(
                "pending dispatch remains unresolved; automatic replay is forbidden"
            )
        reference = self._reserved_reference(current, observation, independence_evidence_digest)
        updated = self._with_reference(current, reference)
        updated = self._refresh_checkpoint(updated, store)
        self._validate_authority(updated, store)
        self._write(updated)
        return updated

    @staticmethod
    def _reserved_reference(
        current: SampledRunState, observation: Observation, independence: str | None
    ) -> SamplingObservationReference:
        reservation = current.pending
        if reservation is None:
            raise RunStateError("observation publication requires a persisted dispatch reservation")
        plan = current.blocks[-1].plan
        identity, digest = _sampling_arm_identity(plan, reservation.arm)
        if (
            observation.identity != identity
            or observation.candidate_digest != digest
            or observation.role is not ObservationRole(plan.role)
            or observation.arm is not ObservationArm(reservation.arm)
            or observation.attempt != reservation.attempt
            or observation.attempt_evidence.attempt_id != reservation.attempt_id
            or observation.attempt_evidence.attempt_digest != reservation.attempt_digest
            or observation.recorded_at < reservation.reserved_at
        ):
            raise RunStateError("observation does not match the exact reserved prepared attempt")
        try:
            return SamplingObservationReference(
                observation_id=observation.observation_id,
                attempt_id=reservation.attempt_id,
                attempt_digest=reservation.attempt_digest,
                schedule_position=reservation.schedule_position,
                independence_evidence_digest=independence,
            )
        except ValidationError as error:
            raise RunStateError("invalid sampling observation reference") from error

    @staticmethod
    def _with_reference(
        current: SampledRunState, reference: SamplingObservationReference
    ) -> SampledRunState:
        progress = current.blocks[-1]
        updated = SamplingBlockProgress(
            plan=progress.plan, observations=(*progress.observations, reference)
        )
        return _sampled_replace(current, blocks=(*current.blocks[:-1], updated), pending=None)

    def _refresh_checkpoint(
        self, state: SampledRunState, store: ObservationStore
    ) -> SampledRunState:
        checkpoint = state.checkpoint
        if checkpoint is None or checkpoint.active_block is None:
            return state
        progress = state.blocks[-1]
        if checkpoint.active_block.plan.digest != progress.plan.digest:
            return state
        evidence = self._hydrate(progress, store)
        # Search classification belongs to the reducer. A completed block remains durable in
        # progress while the cached checkpoint retains its previously selected trials.
        checkpoint = SampledReductionCheckpoint.model_validate(
            {**checkpoint.model_dump(), "active_block": None if evidence.complete else evidence}
        )
        return _sampled_replace(state, checkpoint=checkpoint)

    def archive_block(self, store: ObservationStore) -> SampledRunState:
        current = self.load(store)
        if current.pending is not None or not current.blocks or current.blocks[-1].archived:
            raise RunStateError("only a reconciled active block can be archived")
        block = current.blocks[-1]
        archived = SamplingBlockProgress(
            plan=block.plan, observations=block.observations, archived=True
        )
        return self._publish(current, store, blocks=(*current.blocks[:-1], archived))

    def record_calibration(
        self, store: ObservationStore, observation_ids: tuple[str, ...]
    ) -> SampledRunState:
        """Attach the exact calibration prefix, charging compatible prior observations too.

        Calibration may precede the administrative run. It never supplies observations
        to a search or confirmation arm. Once search starts, its references are frozen.
        """

        current = self.load(store)
        previous = current.calibration_observation_ids
        if observation_ids[: len(previous)] != previous:
            raise RunStateError("calibration continuation must preserve its recorded prefix")
        if current.checkpoint is not None or current.blocks or current.selection is not None:
            if observation_ids != previous:
                raise RunStateError("calibration references cannot change after search starts")
            return current
        return self._publish(current, store, calibration_observation_ids=observation_ids)

    def extend_budget(
        self, store: ObservationStore, approval: SamplingBudgetApproval
    ) -> SampledRunState:
        current = self.load(store)
        return self._publish(current, store, approvals=(*current.approvals, approval))

    def save_checkpoint(
        self, store: ObservationStore, checkpoint: SampledReductionCheckpoint
    ) -> SampledRunState:
        current = self.load(store)
        if current.pending is not None:
            raise RunStateError("checkpoint publication requires dispatch reconciliation")
        return self._publish(current, store, checkpoint=checkpoint)

    def save_fuzz_progress(
        self, store: ObservationStore, progress: FuzzCampaignProgress
    ) -> SampledRunState:
        """Publish campaign metadata under the same run/store lock as dispatch evidence."""

        current = self.load(store)
        if current.pending is not None:
            raise RunStateError("campaign publication requires dispatch reconciliation")
        if current.fuzz_progress is not None and current.fuzz_progress.terminal is not None:
            raise RunStateError("a terminal fuzz campaign cannot change")
        return self._publish(current, store, fuzz_progress=progress)

    def select_fuzz_candidate(
        self,
        store: ObservationStore,
        progress: FuzzCampaignProgress,
        selection: SampledFinalSelection,
    ) -> SampledRunState:
        """Freeze exact mutation and final policy together, before any holdout dispatch."""

        current = self.load(store)
        if current.selection is not None or current.pending is not None:
            raise RunStateError("fuzz selection requires reconciled, unselected progress")
        if current.blocks and not current.blocks[-1].archived:
            raise RunStateError("fuzz selection requires classified exploratory blocks")
        return self._publish(current, store, fuzz_progress=progress, selection=selection)

    def select_candidate(
        self, store: ObservationStore, selection: SampledFinalSelection
    ) -> SampledRunState:
        """Freeze selection and close search atomically, including a safe partial last block."""

        current = self.load(store)
        if current.selection is not None or current.pending is not None:
            raise RunStateError("selection requires reconciled search and cannot be repeated")
        if any(b.plan.role != "search" for b in current.blocks):
            raise RunStateError("selection must precede every confirmation dispatch")
        blocks = current.blocks
        if blocks and not blocks[-1].archived:
            blocks = (*blocks[:-1], blocks[-1].model_copy(update={"archived": True}))
        return self._publish(current, store, blocks=blocks, selection=selection)

    def _publish(
        self, current: SampledRunState, store: ObservationStore, **changes: object
    ) -> SampledRunState:
        updated = _sampled_replace(current, **changes)
        self._validate_authority(updated, store)
        self._write(updated)
        return updated

    def _write(self, state: SampledRunState) -> None:
        if self._owner != (os.getpid(), threading.get_ident()):
            raise RunStateError("sampled state publication requires owned locks")
        _reject_sampled_path(self.path)
        _private_directory(self.path.parent)
        if os.path.lexists(self.path):
            _private_regular_file(self.path)
        try:
            state = SampledRunState.model_validate_json(state.model_dump_json())
        except ValidationError as error:
            raise RunStateError("invalid sampled state publication") from error
        rendered = state.model_dump_json(indent=2).encode("utf-8") + b"\n"
        if len(rendered) > _MAX_RUN_STATE_BYTES:
            raise RunStateError("sampled run state exceeds its byte limit")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sampled-state-", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            _sync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _sampled_replace(state: SampledRunState, **changes: object) -> SampledRunState:
    try:
        return SampledRunState.model_validate({**state.model_dump(), **changes})
    except ValidationError as error:
        raise RunStateError("invalid sampled progress transition") from error


def _sampling_arm_identity(
    plan: SamplingBlockPlan, arm: SamplingArm
) -> tuple[ObservationIdentity, str]:
    if arm == "baseline":
        return plan.original_identity, plan.original_candidate_digest
    return plan.candidate_identity, plan.candidate_digest


def _sampled_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RunStateError("sampled run state contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_sampled_path(path: Path) -> None:
    try:
        _reject_unsafe_path(path)
    except RunStateError as error:
        raise RunStateError("sampled run-state path is unsafe") from error


def _private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RunStateError("sampled run-state directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or (
        os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RunStateError("sampled run-state directory must be private and regular")


def _private_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RunStateError("sampled run-state file is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise RunStateError("sampled run-state file must be regular with mode 0600")
