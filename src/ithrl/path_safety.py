"""Validate filesystem paths against cross-user directory replacement."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafePathError(ValueError):
    """A path has an unsafe or untrusted controlling directory."""


def validate_directory_chain(path: Path) -> None:
    """Require existing path components to be directories controlled by the active user."""

    if ".." in path.parts:
        raise UnsafePathError("path cannot contain '..'")
    absolute = path.absolute()
    existing = _existing_directories(absolute)
    if os.name == "posix":
        _validate_posix_control(existing)
    elif not _is_below_user_profile(absolute):
        raise UnsafePathError("path must be inside the active user's profile on the host platform")


def _existing_directories(path: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    current = Path(path.anchor)
    try:
        root_metadata = current.lstat()
    except OSError as error:
        raise UnsafePathError("path root is unavailable") from error
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise UnsafePathError("path root is not a directory")
    existing: list[tuple[Path, os.stat_result]] = [(current, root_metadata)]
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise UnsafePathError("path directory is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafePathError("path contains a symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafePathError("path component is not a directory")
        existing.append((current, metadata))
    return tuple(existing)


def _validate_posix_control(existing: tuple[tuple[Path, os.stat_result], ...]) -> None:
    effective_user = os.geteuid()
    system_owner = existing[0][1].st_uid
    private_user_positions = {
        index
        for index, (_, metadata) in enumerate(existing)
        if metadata.st_uid == effective_user and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    }
    for index, (_, metadata) in enumerate(existing):
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022:
            shared_sticky_parent = bool(mode & stat.S_ISVTX) and any(
                private_index > index for private_index in private_user_positions
            )
            if not shared_sticky_parent:
                raise UnsafePathError("path directory is writable by other users")
        if metadata.st_uid not in {effective_user, system_owner}:
            raise UnsafePathError("path directory is controlled by another user")


def _is_below_user_profile(path: Path) -> bool:
    home = Path.home().absolute()
    try:
        return os.path.commonpath((path, home)) == str(home)
    except ValueError:
        return False
