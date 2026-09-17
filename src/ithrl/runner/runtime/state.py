"""Owned disposable state for one runner attempt."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath

from ithrl.case.contracts import CaseError
from ithrl.case.filesystem import (
    create_private_directory,
    file_identity,
    prepare_private_directory,
)
from ithrl.execution import MAX_DISPOSABLE_STATE_BYTES, MAX_DISPOSABLE_STATE_ENTRIES

type RootIdentity = tuple[int, int]

_MAX_DIRECTORY_DEPTH = 64


class AttemptStateError(RuntimeError):
    """Disposable attempt state cannot be created, inspected, or removed safely."""


class AttemptStateLimitError(AttemptStateError):
    """Disposable attempt state exceeded a configured inspection limit."""

    def __init__(self, measurement: StateMeasurement) -> None:
        super().__init__("disposable attempt state exceeds its resource limit")
        self.measurement = measurement


@dataclass(frozen=True, slots=True)
class StateMeasurement:
    """Observed disposable-state entry and byte counts."""

    entries: int = 0
    bytes: int = 0


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Verified cleanup evidence for one owned attempt root."""

    state_bytes: int
    state_entries: int
    residual_bytes: int = 0
    residual_entries: int = 0


def require_safe_attempt_state_support() -> None:
    """Refuse before execution when state inspection or cleanup is unsafe."""

    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not shutil.rmtree.avoids_symlink_attacks
    ):
        raise AttemptStateError("safe disposable-state handling is unavailable")


@dataclass(frozen=True, slots=True)
class OwnedAttemptState:
    """A fresh private directory owned by one runner attempt."""

    root: Path
    identity: RootIdentity

    @classmethod
    def create(cls, parent: Path, attempt_id: str) -> OwnedAttemptState:
        """Create a fresh attempt root below a private application directory."""

        _validate_attempt_id(attempt_id)
        absolute_parent = parent.absolute()
        try:
            prepare_private_directory(absolute_parent)
            root = create_private_directory(absolute_parent / attempt_id)
            metadata = root.lstat()
        except (CaseError, OSError, ValueError) as error:
            raise AttemptStateError("cannot create disposable attempt state") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise AttemptStateError("disposable attempt state is not a directory")
        return cls(root=root, identity=file_identity(metadata))

    def assert_unchanged(self) -> None:
        """Require the owned root to retain its original filesystem identity."""

        try:
            metadata = self.root.lstat()
        except OSError as error:
            raise AttemptStateError("disposable attempt state is unavailable") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or file_identity(metadata) != self.identity
        ):
            raise AttemptStateError("disposable attempt state was replaced")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
            raise AttemptStateError("disposable attempt state must have mode 0700")

    def member(self, name: str) -> Path:
        """Return a direct child path after validating its safe relative name."""

        self.assert_unchanged()
        path = PurePath(name)
        if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
            raise AttemptStateError("attempt state member must be a single relative name")
        return self.root / path.name

    def measure_bytes(self) -> int:
        """Return the total size of non-directory entries without following links."""

        return self.measure().bytes

    def measure(
        self,
        *,
        max_entries: int = MAX_DISPOSABLE_STATE_ENTRIES,
        max_bytes: int = MAX_DISPOSABLE_STATE_BYTES,
    ) -> StateMeasurement:
        """Measure state within explicit entry and byte limits without following links."""

        self.assert_unchanged()
        return _measure_directory(
            self.root,
            identity=self.identity,
            max_entries=max_entries,
            max_bytes=max_bytes,
        )

    def cleanup(self, measurement: StateMeasurement | None = None) -> CleanupResult:
        """Remove only this exact owned root and verify that it no longer exists."""

        self.assert_unchanged()
        observed = measurement or self.measure()
        require_safe_attempt_state_support()
        try:
            shutil.rmtree(self.root)
        except (OSError, RecursionError) as error:
            raise AttemptStateError("cannot remove disposable attempt state") from error
        if os.path.lexists(self.root):
            raise AttemptStateError("disposable attempt state remains after cleanup")
        return CleanupResult(state_bytes=observed.bytes, state_entries=observed.entries)


def _validate_attempt_id(attempt_id: str) -> None:
    prefix, separator, value = attempt_id.partition("-")
    if (
        prefix != "attempt"
        or separator != "-"
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AttemptStateError("attempt state requires a generated attempt identifier")


def _measure_directory(
    path: Path,
    *,
    identity: RootIdentity,
    max_entries: int,
    max_bytes: int,
) -> StateMeasurement:
    require_safe_attempt_state_support()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        root_descriptor = os.open(path, flags)
    except OSError as error:
        raise AttemptStateError("cannot inspect disposable attempt state") from error
    try:
        if file_identity(os.fstat(root_descriptor)) != identity:
            raise AttemptStateError("disposable attempt state was replaced")
        measurement = _measure_open_directory(
            root_descriptor,
            measurement=StateMeasurement(),
            flags=flags,
            max_entries=max_entries,
            max_bytes=max_bytes,
            depth=0,
        )
    finally:
        os.close(root_descriptor)
    return measurement


def _measure_open_directory(
    descriptor: int,
    *,
    measurement: StateMeasurement,
    flags: int,
    max_entries: int,
    max_bytes: int,
    depth: int,
) -> StateMeasurement:
    try:
        iterator = os.scandir(descriptor)
    except OSError as error:
        raise AttemptStateError("cannot inspect disposable attempt state") from error
    with iterator:
        for entry in iterator:
            measurement = StateMeasurement(
                entries=measurement.entries + 1,
                bytes=measurement.bytes,
            )
            _require_within_limits(
                measurement,
                max_entries=max_entries,
                max_bytes=max_bytes,
            )
            measurement = _measure_entry(
                descriptor,
                entry,
                measurement=measurement,
                flags=flags,
                max_entries=max_entries,
                max_bytes=max_bytes,
                depth=depth,
            )
    return measurement


def _measure_entry(
    parent_descriptor: int,
    entry: os.DirEntry[str],
    *,
    measurement: StateMeasurement,
    flags: int,
    max_entries: int,
    max_bytes: int,
    depth: int,
) -> StateMeasurement:
    try:
        metadata = entry.stat(follow_symlinks=False)
    except OSError as error:
        raise AttemptStateError("cannot inspect disposable attempt state") from error
    if stat.S_ISDIR(metadata.st_mode):
        if depth >= _MAX_DIRECTORY_DEPTH:
            raise AttemptStateError("disposable attempt state exceeds the nesting limit")
        try:
            child = os.open(entry.name, flags, dir_fd=parent_descriptor)
        except OSError as error:
            raise AttemptStateError("cannot inspect disposable attempt state") from error
        try:
            opened = os.fstat(child)
            if file_identity(opened) != file_identity(metadata) or not stat.S_ISDIR(opened.st_mode):
                raise AttemptStateError("disposable attempt state changed during inspection")
            return _measure_open_directory(
                child,
                measurement=measurement,
                flags=flags,
                max_entries=max_entries,
                max_bytes=max_bytes,
                depth=depth + 1,
            )
        finally:
            os.close(child)
    measured = StateMeasurement(
        entries=measurement.entries,
        bytes=measurement.bytes + metadata.st_size,
    )
    _require_within_limits(measured, max_entries=max_entries, max_bytes=max_bytes)
    return measured


def _require_within_limits(
    measurement: StateMeasurement,
    *,
    max_entries: int,
    max_bytes: int,
) -> None:
    if measurement.entries > max_entries or measurement.bytes > max_bytes:
        raise AttemptStateLimitError(measurement)
