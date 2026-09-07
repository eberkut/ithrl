"""Atomic mutable state for an in-progress reduction run."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ithrl.reduce import ReductionCheckpoint

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
    def acquire_lock(self) -> Iterator[None]:
        """Prevent concurrent execution against this active run."""

        lock_path = self.path.with_name("run.lock")
        _reject_unsafe_path(lock_path)
        lock = FileLock(lock_path, timeout=0, mode=0o600)
        try:
            with lock:
                yield
        except Timeout as error:
            raise RunStateBusyError("another process is already using this active run") from error

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
