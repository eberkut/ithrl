from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import ithrl.runner.runtime.protection as protection
from ithrl.runner.runtime.protection import (
    ProtectedPath,
    ProtectedPathError,
    ProtectedPathSnapshot,
    ProtectionErrorCode,
)


def _capture(*paths: ProtectedPath, max_entries: int = 32, max_bytes: int = 4096):
    return ProtectedPathSnapshot.capture(
        paths,
        max_entries=max_entries,
        max_bytes=max_bytes,
    )


def test_unchanged_tree_ignores_timestamp_updates(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    file = protected / "result.txt"
    file.write_text("same content")
    snapshot = _capture(ProtectedPath("workspace", protected))

    metadata = file.stat()
    os.utime(file, ns=(metadata.st_atime_ns + 1_000_000, metadata.st_mtime_ns + 1_000_000))

    assert snapshot.verify() == ()


def test_content_change_returns_its_label(tmp_path: Path) -> None:
    file = tmp_path / "result.txt"
    file.write_text("before")
    snapshot = _capture(ProtectedPath("source-store", file))

    file.write_text("after")

    assert snapshot.verify() == ("source-store",)


def test_structure_and_entry_type_changes_return_their_labels(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "old.txt").write_text("content")
    typed_entry = second / "entry"
    typed_entry.write_text("content")
    snapshot = _capture(
        ProtectedPath("first", first),
        ProtectedPath("second", second),
    )

    (first / "old.txt").rename(first / "new.txt")
    typed_entry.unlink()
    typed_entry.mkdir()

    assert snapshot.verify() == ("first", "second")


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are required")
def test_mode_change_returns_its_label(tmp_path: Path) -> None:
    file = tmp_path / "result.txt"
    file.write_text("content")
    file.chmod(0o600)
    snapshot = _capture(ProtectedPath("case", file))

    file.chmod(0o640)

    assert snapshot.verify() == ("case",)


def test_deleted_root_returns_its_label(tmp_path: Path) -> None:
    file = tmp_path / "result.txt"
    file.write_text("content")
    snapshot = _capture(ProtectedPath("case", file))

    file.unlink()

    assert snapshot.verify() == ("case",)


def test_relative_path_identity_does_not_follow_later_working_directory_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.mkdir()
    other.mkdir()
    (original / "tracked.txt").write_text("content")
    monkeypatch.chdir(original)
    snapshot = _capture(ProtectedPath("case", Path("tracked.txt")))

    monkeypatch.chdir(other)

    assert snapshot.verify() == ()


@pytest.mark.parametrize(
    "paths",
    [
        (
            ProtectedPath("duplicate", Path("one")),
            ProtectedPath("duplicate", Path("two")),
        ),
        (
            ProtectedPath("one", Path("same")),
            ProtectedPath("two", Path("same") / "."),
        ),
    ],
)
def test_duplicate_labels_and_paths_are_rejected(paths: tuple[ProtectedPath, ...]) -> None:
    with pytest.raises(ProtectedPathError) as raised:
        _capture(*paths)

    assert raised.value.code is ProtectionErrorCode.INVALID_SPECIFICATION


@pytest.mark.parametrize(
    ("max_entries", "max_bytes"),
    [(0, 1), (True, 1), (1, -1), (1, False)],
)
def test_invalid_limits_are_rejected(max_entries: int, max_bytes: int) -> None:
    with pytest.raises(ProtectedPathError) as raised:
        _capture(
            ProtectedPath("case", Path("case")),
            max_entries=max_entries,
            max_bytes=max_bytes,
        )

    assert raised.value.code is ProtectionErrorCode.INVALID_SPECIFICATION


def test_parent_traversal_is_rejected_without_reading_it() -> None:
    with pytest.raises(ProtectedPathError) as raised:
        _capture(ProtectedPath("case", Path("safe") / ".." / "outside"))

    assert raised.value.code is ProtectionErrorCode.INVALID_SPECIFICATION


def test_entry_and_byte_limits_cover_all_protected_paths(tmp_path: Path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    (directory / "one.txt").write_text("12")
    (directory / "two.txt").write_text("34")

    with pytest.raises(ProtectedPathError) as entry_error:
        _capture(ProtectedPath("case", directory), max_entries=2)
    with pytest.raises(ProtectedPathError) as byte_error:
        _capture(ProtectedPath("case", directory), max_bytes=3)

    assert entry_error.value.code is ProtectionErrorCode.LIMIT_EXCEEDED
    assert byte_error.value.code is ProtectionErrorCode.LIMIT_EXCEEDED


def test_growth_beyond_the_original_byte_limit_fails_verification(tmp_path: Path) -> None:
    file = tmp_path / "result.txt"
    file.write_text("12")
    snapshot = _capture(ProtectedPath("case", file), max_bytes=3)
    file.write_text("1234")

    with pytest.raises(ProtectedPathError) as raised:
        snapshot.verify()

    assert raised.value.code is ProtectionErrorCode.LIMIT_EXCEEDED


def test_root_and_nested_symlinks_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("content")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target)
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "nested-link").symlink_to(target)

    for path in (root_link, directory):
        with pytest.raises(ProtectedPathError) as raised:
            _capture(ProtectedPath("case", path))
        assert raised.value.code is ProtectionErrorCode.UNSAFE_PATH


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are not available")
def test_special_files_are_rejected_without_opening_them(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(ProtectedPathError) as raised:
        _capture(ProtectedPath("case", fifo))

    assert raised.value.code is ProtectionErrorCode.UNSAFE_PATH


def test_file_capture_refuses_when_no_follow_open_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = tmp_path / "result.txt"
    file.write_text("content")
    monkeypatch.delattr(protection.os, "O_NOFOLLOW")

    with pytest.raises(ProtectedPathError) as raised:
        _capture(ProtectedPath("case", file))

    assert raised.value.code is ProtectionErrorCode.UNSAFE_PATH


def test_file_replacement_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = tmp_path / "result.txt"
    file.write_text("original")
    original_reader = protection._read_file_content

    def replace_after_read(
        descriptor: int,
        *,
        expected_size: int,
        digest: Any,
        budget: Any,
    ) -> None:
        original_reader(
            descriptor,
            expected_size=expected_size,
            digest=digest,
            budget=budget,
        )
        file.unlink()
        file.write_text("replacement")

    monkeypatch.setattr(protection, "_read_file_content", replace_after_read)

    with pytest.raises(ProtectedPathError) as raised:
        _capture(ProtectedPath("case", file))

    assert raised.value.code is ProtectionErrorCode.CHANGED_DURING_READ


@pytest.mark.skipif(os.name != "posix", reason="directory descriptors are POSIX-specific")
def test_directory_replacement_during_read_is_rejected_without_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    nested = protected_root / "nested"
    nested.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    open_path = protection.os.open
    swapped = False

    def swap_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "nested" and dir_fd is not None and not swapped:
            swapped = True
            nested.rmdir()
            nested.symlink_to(external, target_is_directory=True)
        return open_path(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(protection.os, "open", swap_before_open)

    with pytest.raises(ProtectedPathError) as raised:
        _capture(ProtectedPath("workspace", protected_root))

    assert raised.value.code in {
        ProtectionErrorCode.UNAVAILABLE,
        ProtectionErrorCode.CHANGED_DURING_READ,
    }
    assert secret.read_text(encoding="utf-8") == "private"


def test_errors_do_not_expose_paths_or_file_content(tmp_path: Path) -> None:
    secret_path = tmp_path / "private-project-name"
    secret_content = "private source content"
    secret_path.write_text(secret_content)

    with pytest.raises(ProtectedPathError) as raised:
        _capture(ProtectedPath("case", secret_path), max_bytes=1)

    message = str(raised.value)
    assert str(secret_path) not in message
    assert secret_content not in message
