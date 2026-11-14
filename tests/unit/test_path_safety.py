"""Shared lock-file policy is applied before the lock library can modify a leaf."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from filelock import Timeout

import ithrl.path_safety as path_safety
from ithrl.path_safety import UnsafePathError, private_file_lock, validate_directory_chain


def test_directory_chain_rejects_parent_traversal() -> None:
    with pytest.raises(UnsafePathError, match="cannot contain"):
        validate_directory_chain(Path("private/../evidence"))


@pytest.mark.parametrize("inside_profile", [False, True])
def test_non_posix_directory_control_requires_the_active_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside_profile: bool
) -> None:
    target = tmp_path / "data"
    monkeypatch.setattr(path_safety, "os", SimpleNamespace(name="nt"))
    check = Mock(return_value=inside_profile)
    monkeypatch.setattr(path_safety, "_is_below_user_profile", check)
    if inside_profile:
        validate_directory_chain(target)
    else:
        with pytest.raises(UnsafePathError, match="active user's profile"):
            validate_directory_chain(target)
    check.assert_called_once_with(target.absolute())


def test_profile_containment_handles_incompatible_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            path_safety.os.path, "commonpath", Mock(side_effect=ValueError("different drives"))
        )
        assert not path_safety._is_below_user_profile(Path.home())
    assert path_safety._is_below_user_profile(Path.home() / "data")


@pytest.mark.parametrize("root_fault", ["unavailable", "not-directory"])
def test_directory_chain_refuses_an_unusable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_fault: str
) -> None:
    original = Path.lstat
    root = Path(tmp_path.anchor)

    def root_metadata(path: Path) -> os.stat_result:
        if path == root:
            if root_fault == "unavailable":
                raise PermissionError("private-root-detail")
            metadata = original(path)
            return os.stat_result((stat.S_IFREG | 0o600, *metadata[1:]))
        return original(path)

    monkeypatch.setattr(Path, "lstat", root_metadata)
    with pytest.raises(UnsafePathError, match="path root is") as refused:
        validate_directory_chain(tmp_path)
    assert "private-root-detail" not in str(refused.value)


@pytest.mark.parametrize("component", ["file", "symlink", "dangling-symlink"])
def test_directory_chain_rejects_nondirectory_or_symlink_components(
    tmp_path: Path, component: str
) -> None:
    parent = tmp_path / "parent"
    target = tmp_path / "target"
    if component == "file":
        parent.write_text("existing evidence")
    else:
        if component == "symlink":
            target.mkdir()
        parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(UnsafePathError, match=r"not a directory|symlink"):
        validate_directory_chain(parent / "new-evidence")

    assert not (target / "new-evidence").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership contract")
@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_directory_chain_rejects_writable_parent_despite_private_child(
    tmp_path: Path, mode: int
) -> None:
    parent = tmp_path / "shared"
    child = parent / "private"
    child.mkdir(parents=True, mode=0o700)
    parent.chmod(mode)

    with pytest.raises(UnsafePathError, match="writable by other users"):
        validate_directory_chain(child / "evidence")


@pytest.mark.skipif(os.name != "posix", reason="POSIX sticky-directory contract")
@pytest.mark.parametrize("private_child", [False, True])
def test_sticky_shared_directory_requires_an_existing_private_descendant(
    tmp_path: Path, private_child: bool
) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    parent.chmod(0o1777)
    child = parent / "private"
    if private_child:
        child.mkdir(mode=0o700)
        validate_directory_chain(child / "evidence")
    else:
        with pytest.raises(UnsafePathError, match="writable by other users"):
            validate_directory_chain(child / "evidence")
    assert not (child / "evidence").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership contract")
def test_directory_chain_rejects_a_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    foreign_uid = max(os.geteuid(), Path(parent.anchor).stat().st_uid) + 1
    lstat = Path.lstat

    def foreign_owner(path: Path) -> os.stat_result:
        metadata = lstat(path)
        if path == parent:
            return os.stat_result((*metadata[:4], foreign_uid, *metadata[5:]))
        return metadata

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    with pytest.raises(UnsafePathError, match="controlled by another user"):
        validate_directory_chain(parent / "evidence")


def test_directory_chain_hides_unavailable_component_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    lstat = Path.lstat

    def unavailable(path: Path) -> os.stat_result:
        if path == parent:
            raise PermissionError("private diagnostic")
        return lstat(path)

    monkeypatch.setattr(Path, "lstat", unavailable)
    with pytest.raises(UnsafePathError) as caught:
        validate_directory_chain(parent / "evidence")
    assert str(caught.value) == "path directory is unavailable"


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
