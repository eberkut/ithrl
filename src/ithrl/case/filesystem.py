"""Safe filesystem primitives for private case workspaces."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ithrl.case.contracts import (
    CaseError,
    CaseExistsError,
    CaseFormatError,
    UnsafeCasePathError,
)
from ithrl.path_safety import UnsafePathError, validate_directory_chain

type FileSignature = tuple[int, int, int, int, int]
type FileIdentity = tuple[int, int]


@dataclass(frozen=True, slots=True)
class PublishedFile:
    """Identity of a case file created by the current operation."""

    path: Path
    signature: FileSignature


def validate_target_path(path: Path) -> Path:
    _validate_new_target(path)
    parent = path.parent if path.parent != Path() else Path()
    _validate_directory_control(parent)
    reject_symlink_components(parent)
    if not parent.is_dir():
        raise UnsafeCasePathError(f"case parent directory does not exist: {parent}")
    return path


def prepare_target_path(path: Path) -> Path:
    """Create missing private parents after validating a new case path."""

    _validate_new_target(path)
    parent = path.parent if path.parent != Path() else Path()
    _validate_directory_control(parent)
    reject_symlink_components(parent)
    _create_missing_private_directories(parent)
    return validate_target_path(path)


def prepare_private_directory(path: Path) -> Path:
    """Create a private directory and any missing private parents."""

    _validate_directory_control(path)
    reject_symlink_components(path)
    _create_missing_private_directories(path)
    if not path.is_dir():
        raise UnsafeCasePathError("private data path is not a directory")
    set_private_mode(path, 0o700)
    _validate_directory_control(path)
    return path


def _create_missing_private_directories(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        if os.path.lexists(current):
            raise UnsafeCasePathError(f"case parent is unavailable: {current}")
        missing.append(current)
        ancestor = current.parent
        if ancestor == current:
            raise UnsafeCasePathError(f"case parent directory does not exist: {path}")
        current = ancestor
    if not current.is_dir():
        raise UnsafeCasePathError(f"case parent directory does not exist: {current}")
    for directory in reversed(missing):
        create_private_directory(directory)


def _validate_new_target(path: Path) -> None:
    if ".." in path.parts:
        raise UnsafeCasePathError("case path cannot contain '..'")
    if os.path.lexists(path):
        raise CaseExistsError(f"case path already exists: {path}")


def validate_existing_root(path: Path) -> Path:
    if ".." in path.parts:
        raise UnsafeCasePathError("case path cannot contain '..'")
    _validate_directory_control(path)
    reject_symlink_components(path)
    try:
        metadata = path.stat()
    except OSError as error:
        raise UnsafeCasePathError(f"case directory is unavailable: {path}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeCasePathError(f"case path is not a directory: {path}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UnsafeCasePathError("case directory must have mode 0700")
    return path


def reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise UnsafeCasePathError(f"case path contains a symlink: {current}")


def _validate_directory_control(path: Path) -> None:
    try:
        validate_directory_chain(path)
    except UnsafePathError as error:
        raise UnsafeCasePathError(str(error)) from error


def create_private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise CaseExistsError("refusing to reuse a case directory") from error
    except OSError as error:
        raise CaseError("cannot create a case directory") from error
    set_private_mode(path, 0o700)
    return path


def require_private_directory(path: Path, *, label: str) -> None:
    reject_symlink_components(path.parent)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CaseFormatError(f"required case directory is unavailable: {label}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafeCasePathError(f"case entry must not be a symbolic link: {label}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeCasePathError(f"case entry must be a directory: {label}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise UnsafeCasePathError(f"case directory must have mode 0700: {label}")


def read_safe_text(path: Path, *, max_bytes: int, label: str | None = None) -> str:
    file_label = label or path.name
    content = read_safe_bytes(path, max_bytes=max_bytes, label=file_label)
    try:
        return content.decode("utf-8")
    except UnicodeError as error:
        raise CaseFormatError(f"cannot read case file: {file_label}") from error


def read_safe_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    initial = _validate_file_before_open(path, max_bytes=max_bytes, label=label)
    descriptor = _open_read_only(path, label=label)
    try:
        opened = os.fstat(descriptor)
        _validate_opened_file(initial, opened, label=label)
        read_descriptor = descriptor
        descriptor = -1
        content, finished = _read_descriptor(
            read_descriptor,
            expected_size=opened.st_size,
            max_bytes=max_bytes,
            label=label,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > max_bytes:
        raise CaseFormatError(f"{label} exceeds the {max_bytes}-byte limit")
    current = lstat_regular_file(path, label=label)
    if file_signature(opened) != file_signature(finished) or file_signature(
        opened
    ) != file_signature(current):
        raise UnsafeCasePathError(f"case file changed while reading: {label}")
    return content


def _validate_file_before_open(path: Path, *, max_bytes: int, label: str) -> os.stat_result:
    reject_symlink_components(path.parent)
    metadata = lstat_regular_file(path, label=label)
    if metadata.st_size > max_bytes:
        raise CaseFormatError(f"{label} exceeds the {max_bytes}-byte limit")
    return metadata


def _open_read_only(path: Path, *, label: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        return os.open(path, flags)
    except OSError as error:
        raise CaseFormatError(f"cannot read case file: {label}") from error


def _validate_opened_file(expected: os.stat_result, opened: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise UnsafeCasePathError(f"case file changed while opening: {label}")
    if file_signature(expected) != file_signature(opened):
        raise UnsafeCasePathError(f"case file changed while opening: {label}")
    if os.name == "posix" and stat.S_IMODE(opened.st_mode) != 0o600:
        raise UnsafeCasePathError(f"case file must have mode 0600: {label}")


def _read_descriptor(
    descriptor: int, *, expected_size: int, max_bytes: int, label: str
) -> tuple[bytes, os.stat_result]:
    try:
        handle = os.fdopen(descriptor, "rb")
    except OSError as error:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise CaseFormatError(f"cannot read case file: {label}") from error
    try:
        with handle:
            content = handle.read(min(expected_size, max_bytes) + 1)
            return content, os.fstat(handle.fileno())
    except OSError as error:
        raise CaseFormatError(f"cannot read case file: {label}") from error


def lstat_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CaseFormatError(f"required case file is unavailable: {label}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeCasePathError(f"case entry must be a regular file: {label}")
    return metadata


def file_signature(metadata: os.stat_result) -> FileSignature:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def atomic_write(path: Path, content: str) -> PublishedFile:
    return atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> PublishedFile:
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as error:
        raise CaseError("cannot create temporary case file") from error
    temporary = Path(temporary_name)
    temporary_identity = file_identity(os.fstat(descriptor))
    try:
        _write_descriptor(descriptor, content)
        set_private_mode(temporary, 0o600)
        _link_without_overwrite(temporary, path)
        _unlink_if_identity_matches(temporary, temporary_identity)
        signature = _published_signature(path, temporary_identity)
        return PublishedFile(path=path, signature=signature)
    except CaseError:
        raise
    except OSError as error:
        raise CaseError("cannot write case file") from error
    finally:
        _unlink_if_identity_matches(temporary, temporary_identity)


def _write_descriptor(descriptor: int, content: bytes) -> None:
    try:
        handle = os.fdopen(descriptor, "wb")
    except OSError:
        os.close(descriptor)
        raise
    with handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _link_without_overwrite(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise CaseExistsError(f"refusing to overwrite case entry: {destination.name}") from error
    except OSError as error:
        raise CaseError(f"cannot create case file: {destination.name}") from error


def remove_published_file(published: PublishedFile) -> None:
    """Remove an unchanged file published by the current operation."""

    try:
        metadata = published.path.lstat()
    except OSError:
        return
    if stat.S_ISREG(metadata.st_mode) and file_signature(metadata) == published.signature:
        published.path.unlink()


def _published_signature(path: Path, expected: FileIdentity) -> FileSignature:
    try:
        metadata = path.lstat()
    except OSError as error:
        _unlink_if_identity_matches(path, expected)
        raise CaseError("published case file is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or file_identity(metadata) != expected:
        raise CaseError("published case file changed unexpectedly")
    return file_signature(metadata)


def _unlink_if_identity_matches(path: Path, identity: FileIdentity) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    if file_identity(metadata) == identity:
        with contextlib.suppress(OSError):
            path.unlink()


def set_private_mode(path: Path, mode: int) -> None:
    if os.name == "posix":
        path.chmod(mode)


def file_identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino
