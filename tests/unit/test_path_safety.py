"""Shared lock-file policy is applied before the lock library can modify a leaf."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from filelock import Timeout

import ithrl.path_safety as path_safety
from ithrl.path_safety import UnsafePathError, private_file_lock


@pytest.mark.parametrize("existing", [False, True])
def test_private_file_lock_accepts_missing_or_private_regular_leaf(
    tmp_path: Path, existing: bool
) -> None:
    path = tmp_path / "run.lock"
    if existing:
        path.write_bytes(b"prior lock contents")
        path.chmod(0o600)

    lock = private_file_lock(path)

    assert lock.timeout == 0
    with lock:
        assert path.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(Timeout), private_file_lock(path):
            pytest.fail("concurrent lock acquisition succeeded")


@pytest.mark.parametrize("unsafe", ["directory", "symlink", "dangling-symlink", "hard-link"])
def test_private_file_lock_refuses_unsafe_leaf_before_library_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    path = tmp_path / "run.lock"
    target = tmp_path / "private-evidence"
    original = b"private evidence remains intact"
    target.write_bytes(original)
    target.chmod(0o600)
    if unsafe == "directory":
        path.mkdir()
    elif unsafe == "symlink":
        path.symlink_to(target)
    elif unsafe == "dangling-symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        os.link(target, path)

    def forbidden_constructor(*args: object, **kwargs: object) -> None:
        pytest.fail("unsafe leaf reached the lock library")

    monkeypatch.setattr(path_safety, "FileLock", forbidden_constructor)
    with pytest.raises(UnsafePathError, match="single-link regular file"):
        private_file_lock(path)

    assert target.read_bytes() == original
    if unsafe == "hard-link":
        assert path.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock-file layout contract")
@pytest.mark.parametrize("mode", [0o644, 0o666, 0o400])
def test_private_file_lock_refuses_nonprivate_mode(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "run.lock"
    original = b"private evidence remains intact"
    path.write_bytes(original)
    path.chmod(mode)

    with pytest.raises(UnsafePathError, match="0600"):
        private_file_lock(path)

    assert path.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO contract")
def test_private_file_lock_refuses_fifo(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    os.mkfifo(path, 0o600)

    with pytest.raises(UnsafePathError, match="single-link regular file"):
        private_file_lock(path)


def test_private_file_lock_translates_metadata_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(self: Path) -> os.stat_result:
        raise PermissionError("private diagnostic")

    monkeypatch.setattr(Path, "lstat", unavailable)
    with pytest.raises(UnsafePathError, match="private lock file is unavailable"):
        private_file_lock(tmp_path / "run.lock")
