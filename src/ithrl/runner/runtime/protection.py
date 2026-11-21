"""Bounded snapshots for filesystem state that a runner must not change."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, overload

_CHUNK_SIZE = 64 * 1024
_MAX_LABEL_LENGTH = 128

# Native runners share a finite, complete-tree read budget.
DEFAULT_MAX_PROTECTED_ENTRIES = 250_000
DEFAULT_MAX_PROTECTED_BYTES = 512 * 1024 * 1024

type _ReadSignature = tuple[int, int, int, int, int, int]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


class ProtectionErrorCode(StrEnum):
    """Stable categories for protected-path failures."""

    INVALID_SPECIFICATION = "invalid-specification"
    UNSAFE_PATH = "unsafe-path"
    UNAVAILABLE = "unavailable"
    LIMIT_EXCEEDED = "limit-exceeded"
    CHANGED_DURING_READ = "changed-during-read"


class ProtectedPathError(RuntimeError):
    """A protected path could not be fingerprinted safely."""

    def __init__(self, code: ProtectionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProtectedPath:
    """A caller-defined label and the file or directory it identifies."""

    label: str
    path: Path = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CapturedPath:
    label: str
    path: Path = field(repr=False)
    fingerprint: bytes = field(repr=False)


@dataclass(slots=True)
class _Budget:
    max_entries: int
    max_bytes: int
    entries: int = 0
    bytes: int = 0

    def add_entry(self) -> None:
        self.entries += 1
        if self.entries > self.max_entries:
            raise ProtectedPathError(
                ProtectionErrorCode.LIMIT_EXCEEDED,
                "protected paths exceed the entry limit",
            )

    def add_bytes(self, size: int) -> None:
        self.bytes += size
        if self.bytes > self.max_bytes:
            self.byte_limit_exceeded()

    def require_available_bytes(self, size: int) -> None:
        if size > self.max_bytes - self.bytes:
            self.byte_limit_exceeded()

    def require_available_entries(self, size: int) -> None:
        if size > self.max_entries - self.entries:
            self.entry_limit_exceeded()

    @staticmethod
    def entry_limit_exceeded() -> None:
        raise ProtectedPathError(
            ProtectionErrorCode.LIMIT_EXCEEDED,
            "protected paths exceed the entry limit",
        )

    @staticmethod
    def byte_limit_exceeded() -> None:
        raise ProtectedPathError(
            ProtectionErrorCode.LIMIT_EXCEEDED,
            "protected paths exceed the byte limit",
        )


@dataclass(frozen=True, slots=True)
class ProtectedPathSnapshot:
    """Fingerprints of labeled paths captured within explicit read limits."""

    _paths: tuple[_CapturedPath, ...] = field(repr=False)
    max_entries: int
    max_bytes: int
    allow_internal_symlinks: bool = False

    @classmethod
    def capture(
        cls,
        paths: Iterable[ProtectedPath],
        *,
        max_entries: int,
        max_bytes: int,
        allow_internal_symlinks: bool = False,
    ) -> ProtectedPathSnapshot:
        """Capture labeled paths without following links or reading special files."""

        entries = tuple(paths)
        if type(allow_internal_symlinks) is not bool:
            _invalid_specification()
        _validate_specification(entries, max_entries=max_entries, max_bytes=max_bytes)
        budget = _Budget(max_entries=max_entries, max_bytes=max_bytes)
        captured: list[_CapturedPath] = []
        for entry in entries:
            absolute_path = entry.path.absolute()
            captured.append(
                _CapturedPath(
                    label=entry.label,
                    path=absolute_path,
                    fingerprint=_fingerprint(
                        absolute_path,
                        budget=budget,
                        allow_internal_symlinks=allow_internal_symlinks,
                    ),
                )
            )
        return cls(
            _paths=tuple(captured),
            max_entries=max_entries,
            max_bytes=max_bytes,
            allow_internal_symlinks=allow_internal_symlinks,
        )

    def verify(self) -> tuple[str, ...]:
        """Return labels whose names, types, modes, or contents changed."""

        budget = _Budget(max_entries=self.max_entries, max_bytes=self.max_bytes)
        changed: list[str] = []
        for captured in self._paths:
            current = _fingerprint(
                captured.path,
                budget=budget,
                allow_missing=True,
                allow_internal_symlinks=self.allow_internal_symlinks,
            )
            if current is None or current != captured.fingerprint:
                changed.append(captured.label)
        return tuple(changed)


def _validate_specification(
    paths: tuple[ProtectedPath, ...],
    *,
    max_entries: int,
    max_bytes: int,
) -> None:
    _validate_limits(max_entries=max_entries, max_bytes=max_bytes)
    if not paths:
        _invalid_specification()
    labels: set[str] = set()
    normalized_paths: set[str] = set()
    for entry in paths:
        label, normalized = _validate_entry(entry)
        if label in labels or normalized in normalized_paths:
            _invalid_specification()
        labels.add(label)
        normalized_paths.add(normalized)


def _validate_limits(*, max_entries: int, max_bytes: int) -> None:
    if type(max_entries) is not int or max_entries < 1:
        _invalid_specification()
    if type(max_bytes) is not int or max_bytes < 0:
        _invalid_specification()


def _validate_entry(entry: ProtectedPath) -> tuple[str, str]:
    if not isinstance(entry, ProtectedPath):
        _invalid_specification()
    if not entry.label or len(entry.label) > _MAX_LABEL_LENGTH or not entry.label.isprintable():
        _invalid_specification()
    if not isinstance(entry.path, Path) or ".." in entry.path.parts:
        _invalid_specification()
    return entry.label, os.path.normcase(os.fspath(entry.path.absolute()))


def _invalid_specification() -> None:
    raise ProtectedPathError(
        ProtectionErrorCode.INVALID_SPECIFICATION,
        "protected-path specification is invalid",
    )


@overload
def _fingerprint(
    path: Path,
    *,
    budget: _Budget,
    allow_missing: Literal[False] = False,
    allow_internal_symlinks: bool = False,
) -> bytes: ...


@overload
def _fingerprint(
    path: Path,
    *,
    budget: _Budget,
    allow_missing: Literal[True],
    allow_internal_symlinks: bool = False,
) -> bytes | None: ...


def _fingerprint(
    path: Path,
    *,
    budget: _Budget,
    allow_missing: bool = False,
    allow_internal_symlinks: bool = False,
) -> bytes | None:
    _reject_symlink_components(path, allow_missing_leaf=allow_missing)
    digest = hashlib.sha256()
    try:
        _fingerprint_entry(
            path,
            relative_parts=(),
            digest=digest,
            budget=budget,
            allow_internal_symlinks=allow_internal_symlinks,
        )
    except FileNotFoundError:
        if allow_missing and not os.path.lexists(path):
            return None
        raise ProtectedPathError(
            ProtectionErrorCode.CHANGED_DURING_READ,
            "a protected path changed while it was read",
        ) from None
    except ProtectedPathError:
        raise
    except RecursionError:
        raise ProtectedPathError(
            ProtectionErrorCode.LIMIT_EXCEEDED,
            "protected paths exceed the supported nesting depth",
        ) from None
    except OSError:
        raise ProtectedPathError(
            ProtectionErrorCode.UNAVAILABLE,
            "a protected path cannot be read",
        ) from None
    return digest.digest()


def _reject_symlink_components(path: Path, *, allow_missing_leaf: bool) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    for part in parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and not os.path.lexists(path):
                return
            raise ProtectedPathError(
                ProtectionErrorCode.UNAVAILABLE,
                "a protected path cannot be read",
            ) from None
        except OSError:
            raise ProtectedPathError(
                ProtectionErrorCode.UNAVAILABLE,
                "a protected path cannot be read",
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ProtectedPathError(
                ProtectionErrorCode.UNSAFE_PATH,
                "a protected path contains a symbolic link",
            )


def _fingerprint_entry(
    path: Path,
    *,
    relative_parts: tuple[str, ...],
    digest: _Digest,
    budget: _Budget,
    allow_internal_symlinks: bool = False,
) -> None:
    metadata = path.lstat()
    budget.add_entry()
    if stat.S_ISREG(metadata.st_mode):
        _fingerprint_file(
            path,
            relative_parts=relative_parts,
            initial=metadata,
            digest=digest,
            budget=budget,
        )
        return
    if stat.S_ISDIR(metadata.st_mode):
        _fingerprint_directory(
            path,
            relative_parts=relative_parts,
            initial=metadata,
            digest=digest,
            budget=budget,
            allow_internal_symlinks=allow_internal_symlinks,
        )
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise ProtectedPathError(
            ProtectionErrorCode.UNSAFE_PATH,
            "a protected path contains a symbolic link",
        )
    raise ProtectedPathError(
        ProtectionErrorCode.UNSAFE_PATH,
        "a protected path contains a special file",
    )


def _fingerprint_directory(
    path: Path,
    *,
    relative_parts: tuple[str, ...],
    initial: os.stat_result,
    digest: _Digest,
    budget: _Budget,
    allow_internal_symlinks: bool,
) -> None:
    _update_header(digest, relative_parts=relative_parts, kind=b"D", mode=initial.st_mode)
    descriptor = _open_directory(path, initial=initial)
    try:
        _fingerprint_open_directory(
            descriptor,
            relative_parts=relative_parts,
            digest=digest,
            budget=budget,
            directory_descriptors=(descriptor,) if allow_internal_symlinks else None,
        )
        finished = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _read_signature(initial) != _read_signature(finished) or _read_signature(
        initial
    ) != _read_signature(current):
        _changed_during_read()
    digest.update(b"E")


def _fingerprint_open_directory(
    descriptor: int,
    *,
    relative_parts: tuple[str, ...],
    digest: _Digest,
    budget: _Budget,
    directory_descriptors: tuple[int, ...] | None,
) -> None:
    names: list[str] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            budget.require_available_entries(len(names) + 1)
            names.append(entry.name)
    for name in sorted(names, key=os.fsencode):
        _fingerprint_entry_at(
            descriptor,
            name,
            relative_parts=(*relative_parts, name),
            digest=digest,
            budget=budget,
            directory_descriptors=directory_descriptors,
        )


def _fingerprint_entry_at(
    parent_descriptor: int,
    name: str,
    *,
    relative_parts: tuple[str, ...],
    digest: _Digest,
    budget: _Budget,
    directory_descriptors: tuple[int, ...] | None,
) -> None:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    budget.add_entry()
    if stat.S_ISREG(metadata.st_mode):
        _fingerprint_file_at(
            parent_descriptor,
            name,
            relative_parts=relative_parts,
            initial=metadata,
            digest=digest,
            budget=budget,
        )
        return
    if stat.S_ISDIR(metadata.st_mode):
        _fingerprint_directory_at(
            parent_descriptor,
            name,
            relative_parts=relative_parts,
            initial=metadata,
            digest=digest,
            budget=budget,
            directory_descriptors=directory_descriptors,
        )
        return
    if stat.S_ISLNK(metadata.st_mode):
        if directory_descriptors is not None:
            _fingerprint_symlink_at(
                parent_descriptor,
                name,
                relative_parts=relative_parts,
                initial=metadata,
                digest=digest,
                budget=budget,
                directory_descriptors=directory_descriptors,
            )
            return
        raise ProtectedPathError(
            ProtectionErrorCode.UNSAFE_PATH,
            "a protected path contains a symbolic link",
        )
    raise ProtectedPathError(
        ProtectionErrorCode.UNSAFE_PATH,
        "a protected path contains a special file",
    )


def _fingerprint_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    relative_parts: tuple[str, ...],
    initial: os.stat_result,
    digest: _Digest,
    budget: _Budget,
    directory_descriptors: tuple[int, ...] | None,
) -> None:
    _update_header(digest, relative_parts=relative_parts, kind=b"D", mode=initial.st_mode)
    descriptor = _open_directory(name, initial=initial, dir_fd=parent_descriptor)
    try:
        _fingerprint_open_directory(
            descriptor,
            relative_parts=relative_parts,
            digest=digest,
            budget=budget,
            directory_descriptors=(*directory_descriptors, descriptor)
            if directory_descriptors is not None
            else None,
        )
        finished = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _read_signature(initial) != _read_signature(finished) or _read_signature(
        initial
    ) != _read_signature(current):
        _changed_during_read()
    digest.update(b"E")


def _fingerprint_symlink_at(
    parent_descriptor: int,
    name: str,
    *,
    relative_parts: tuple[str, ...],
    initial: os.stat_result,
    digest: _Digest,
    budget: _Budget,
    directory_descriptors: tuple[int, ...],
) -> None:
    target = os.readlink(name, dir_fd=parent_descriptor)
    encoded = os.fsencode(target)
    budget.add_bytes(len(encoded))
    _validate_internal_link_target(target, directory_descriptors)
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _read_signature(initial) != _read_signature(current):
        _changed_during_read()
    _update_header(digest, relative_parts=relative_parts, kind=b"L", mode=initial.st_mode)
    digest.update(len(encoded).to_bytes(8, "big", signed=False))
    digest.update(encoded)


def _validate_internal_link_target(target: str, directory_descriptors: tuple[int, ...]) -> None:
    # Resolve only leading parent steps through descriptors already opened within this root.
    # Reject later traversal rather than normalizing away a possible symlink or root escape.
    parts = target.split("/")
    parents = 0
    while parts and parts[0] in {".", ".."}:
        if parts.pop(0) == "..":
            parents += 1
    if (
        not target
        or target.startswith("/")
        or parents >= len(directory_descriptors)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        _unsafe_link_target()
    descriptor = directory_descriptors[-1 - parents]
    opened: list[int] = []
    try:
        for index, part in enumerate(parts):
            metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if index == len(parts) - 1:
                if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    _unsafe_link_target()
            else:
                if not stat.S_ISDIR(metadata.st_mode):
                    _unsafe_link_target()
                descriptor = _open_directory(part, initial=metadata, dir_fd=descriptor)
                opened.append(descriptor)
    except FileNotFoundError:
        _unsafe_link_target()
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _unsafe_link_target() -> None:
    raise ProtectedPathError(
        ProtectionErrorCode.UNSAFE_PATH,
        "a protected symbolic link does not target a real entry within its root",
    )


def _open_directory(
    path: Path | str,
    *,
    initial: os.stat_result,
    dir_fd: int | None = None,
) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ProtectedPathError(
            ProtectionErrorCode.UNSAFE_PATH,
            "safe protected-path inspection is unavailable",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _read_signature(initial) != _read_signature(opened):
        os.close(descriptor)
        _changed_during_read()
    return descriptor


def _fingerprint_file(
    path: Path,
    *,
    relative_parts: tuple[str, ...],
    initial: os.stat_result,
    digest: _Digest,
    budget: _Budget,
) -> None:
    descriptor = os.open(path, _file_open_flags())
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _read_signature(initial) != _read_signature(opened):
            _changed_during_read()
        budget.require_available_bytes(opened.st_size)
        _update_header(digest, relative_parts=relative_parts, kind=b"F", mode=opened.st_mode)
        digest.update(opened.st_size.to_bytes(8, "big", signed=False))
        _read_file_content(
            descriptor,
            expected_size=opened.st_size,
            digest=digest,
            budget=budget,
        )
        finished = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if _read_signature(opened) != _read_signature(finished) or _read_signature(
        opened
    ) != _read_signature(current):
        _changed_during_read()


def _fingerprint_file_at(
    parent_descriptor: int,
    name: str,
    *,
    relative_parts: tuple[str, ...],
    initial: os.stat_result,
    digest: _Digest,
    budget: _Budget,
) -> None:
    descriptor = os.open(name, _file_open_flags(), dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _read_signature(initial) != _read_signature(opened):
            _changed_during_read()
        budget.require_available_bytes(opened.st_size)
        _update_header(digest, relative_parts=relative_parts, kind=b"F", mode=opened.st_mode)
        digest.update(opened.st_size.to_bytes(8, "big", signed=False))
        _read_file_content(
            descriptor,
            expected_size=opened.st_size,
            digest=digest,
            budget=budget,
        )
        finished = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _read_signature(opened) != _read_signature(finished) or _read_signature(
        opened
    ) != _read_signature(current):
        _changed_during_read()


def _read_file_content(
    descriptor: int,
    *,
    expected_size: int,
    digest: _Digest,
    budget: _Budget,
) -> None:
    remaining = expected_size
    while remaining and (chunk := os.read(descriptor, min(_CHUNK_SIZE, remaining))):
        budget.add_bytes(len(chunk))
        digest.update(chunk)
        remaining -= len(chunk)


def _file_open_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ProtectedPathError(
            ProtectionErrorCode.UNSAFE_PATH,
            "safe protected-file inspection is unavailable",
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    for option in ("O_CLOEXEC", "O_BINARY", "O_NONBLOCK"):
        flags |= getattr(os, option, 0)
    return flags


def _changed_during_read() -> None:
    raise ProtectedPathError(
        ProtectionErrorCode.CHANGED_DURING_READ,
        "a protected path changed while it was read",
    )


def _read_signature(metadata: os.stat_result) -> _ReadSignature:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _update_header(
    digest: _Digest,
    *,
    relative_parts: tuple[str, ...],
    kind: bytes,
    mode: int,
) -> None:
    digest.update(kind)
    digest.update(stat.S_IMODE(mode).to_bytes(4, "big", signed=False))
    digest.update(len(relative_parts).to_bytes(4, "big", signed=False))
    for part in relative_parts:
        encoded = os.fsencode(part)
        digest.update(len(encoded).to_bytes(8, "big", signed=False))
        digest.update(encoded)
