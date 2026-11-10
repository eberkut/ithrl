"""Filesystem failures preserve evidence, close descriptors and respect ownership."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from ithrl.case import CaseError, CaseFormatError, UnsafeCasePathError, filesystem


@pytest.mark.parametrize("stage", ["temporary", "fdopen", "fsync", "link"])
def test_failed_atomic_write_closes_descriptors_and_removes_only_owned_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    destination = tmp_path / "evidence.bin"
    descriptors: list[int] = []
    mkstemp = filesystem.tempfile.mkstemp

    def allocate(**kwargs: Any) -> tuple[int, str]:
        descriptor, name = mkstemp(**kwargs)
        descriptors.append(descriptor)
        return descriptor, name

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("private OS diagnostic")

    with monkeypatch.context() as patch:
        patch.setattr(filesystem.tempfile, "mkstemp", fail if stage == "temporary" else allocate)
        if stage != "temporary":
            patch.setattr(os, stage, fail)
        with pytest.raises(CaseError) as caught:
            filesystem.atomic_write_bytes(destination, b"private evidence")

    assert "private" not in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    for descriptor in descriptors:
        with pytest.raises(OSError, match=os.strerror(errno.EBADF)):
            os.fstat(descriptor)


@pytest.mark.parametrize("change", ["unchanged", "edited", "replaced", "symlink", "missing"])
def test_publication_rollback_removes_only_the_unchanged_owned_file(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "evidence.bin"
    publication = filesystem.atomic_write_bytes(path, b"original")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement must survive")
    if change == "edited":
        path.write_bytes(b"edited content with a different size")
    elif change == "replaced":
        replacement.replace(path)
    elif change == "symlink":
        path.unlink()
        path.symlink_to(replacement)
    elif change == "missing":
        path.unlink()
    before = path.read_bytes() if path.exists() else None

    filesystem.remove_published_file(publication)

    if change in {"unchanged", "missing"}:
        assert not path.exists()
    else:
        assert path.read_bytes() == before
        assert path.is_symlink() == (change == "symlink")
    if replacement.exists():
        assert replacement.read_bytes() == b"replacement must survive"


@pytest.mark.parametrize("replace", [False, True])
def test_atomic_publication_refuses_a_destination_changed_after_linking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    destination = tmp_path / "evidence.bin"
    link = os.link

    def race(source: Path, target: Path, *, follow_symlinks: bool) -> None:
        link(source, target, follow_symlinks=follow_symlinks)
        if replace:
            replacement = tmp_path / "replacement.bin"
            replacement.write_bytes(b"concurrent evidence")
            replacement.replace(target)
        else:
            Path(target).unlink()

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(CaseError, match="changed unexpectedly" if replace else "unavailable"):
        filesystem.atomic_write_bytes(destination, b"original")

    assert tuple(tmp_path.glob(".*.tmp")) == ()
    if replace:
        assert destination.read_bytes() == b"concurrent evidence"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("stage", ["open", "fdopen", "fstat", "final-fstat"])
def test_failed_safe_read_closes_any_opened_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path = tmp_path / "evidence.bin"
    path.write_bytes(b"captured evidence")
    path.chmod(0o600)
    descriptors: list[int] = []
    open_file = os.open
    fstat = os.fstat
    stat_calls = 0

    def open_and_record(*args: Any, **kwargs: Any) -> int:
        descriptor = open_file(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("private OS diagnostic")

    def unavailable_after_read(descriptor: int) -> os.stat_result:
        nonlocal stat_calls
        stat_calls += 1
        if stat_calls == 2:
            raise PermissionError("private OS diagnostic")
        return fstat(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", fail if stage == "open" else open_and_record)
        if stage == "final-fstat":
            patch.setattr(os, "fstat", unavailable_after_read)
        elif stage != "open":
            patch.setattr(os, stage, fail)
        expected_error = PermissionError if stage == "fstat" else CaseFormatError
        with pytest.raises(
            expected_error,
            match="private OS diagnostic" if stage == "fstat" else "cannot read case file",
        ):
            filesystem.read_safe_bytes(path, max_bytes=100, label="evidence")

    assert path.read_bytes() == b"captured evidence"
    for descriptor in descriptors:
        with pytest.raises(OSError, match=os.strerror(errno.EBADF)):
            os.fstat(descriptor)


def test_safe_read_refuses_a_file_changed_between_validation_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.bin"
    path.write_bytes(b"original")
    path.chmod(0o600)
    open_file = os.open

    def changed(*args: Any, **kwargs: Any) -> int:
        descriptor = open_file(*args, **kwargs)
        path.write_bytes(b"replacement content")
        return descriptor

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", changed)
        with pytest.raises(UnsafeCasePathError, match="changed while opening"):
            filesystem.read_safe_bytes(path, max_bytes=100, label="evidence")
    assert path.read_bytes() == b"replacement content"


def test_safe_text_read_rejects_invalid_utf8_without_exposing_content(tmp_path: Path) -> None:
    path = tmp_path / "evidence.bin"
    path.write_bytes(b"private\xff")
    path.chmod(0o600)
    with pytest.raises(CaseFormatError) as caught:
        filesystem.read_safe_text(path, max_bytes=100, label="evidence")
    assert str(caught.value) == "cannot read case file: evidence"


@pytest.mark.parametrize("entry", ["missing", "file", "symlink", "insecure-directory"])
def test_required_private_directory_refuses_unsafe_entries(tmp_path: Path, entry: str) -> None:
    path = tmp_path / "sources"
    if entry == "file":
        path.write_bytes(b"existing evidence")
    elif entry == "symlink":
        path.symlink_to(tmp_path / "missing", target_is_directory=True)
    elif entry == "insecure-directory":
        if os.name != "posix":
            pytest.skip("POSIX permission contract")
        path.mkdir(mode=0o755)
        path.chmod(0o755)
    with pytest.raises((CaseFormatError, UnsafeCasePathError)):
        filesystem.require_private_directory(path, label="sources")
    if entry == "file":
        assert path.read_bytes() == b"existing evidence"
