from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import ithrl.runner.runtime.state as state_module
from ithrl.runner.runtime.state import (
    AttemptStateError,
    AttemptStateLimitError,
    OwnedAttemptState,
    StateMeasurement,
)

ATTEMPT_1 = "attempt-00000000000000000000000000000001"
ATTEMPT_2 = "attempt-00000000000000000000000000000002"


def test_attempt_roots_are_private_and_disjoint(tmp_path: Path) -> None:
    parent = tmp_path / "ithrl" / "attempts"

    first = OwnedAttemptState.create(parent, ATTEMPT_1)
    second = OwnedAttemptState.create(parent, ATTEMPT_2)

    assert first.root != second.root
    assert first.identity != second.identity
    assert first.root.parent == second.root.parent == parent.absolute()
    if os.name == "posix":
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(first.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(second.root.stat().st_mode) == 0o700


def test_attempt_state_refuses_to_reuse_an_existing_root(tmp_path: Path) -> None:
    parent = tmp_path / "attempts"
    state = OwnedAttemptState.create(parent, ATTEMPT_1)

    with pytest.raises(AttemptStateError, match="cannot create"):
        OwnedAttemptState.create(parent, ATTEMPT_1)

    assert state.root.is_dir()


def test_attempt_state_measures_content_and_verifies_cleanup(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    state.member("stdout").write_bytes(b"output")
    nested = state.root / "nested"
    nested.mkdir()
    (nested / "state.bin").write_bytes(b"state")

    assert state.measure_bytes() == 11

    result = state.cleanup()

    assert result.state_bytes == 11
    assert result.state_entries == 3
    assert result.residual_bytes == 0
    assert not os.path.lexists(state.root)
    assert state.root.parent.is_dir()


def test_attempt_state_enforces_the_entry_limit_during_enumeration(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    for index in range(3):
        state.member(f"empty-{index}").touch()

    with pytest.raises(AttemptStateLimitError) as raised:
        state.measure(max_entries=2, max_bytes=1)

    assert raised.value.measurement.entries == 3
    assert raised.value.measurement.bytes == 0


def test_attempt_state_counts_a_member_deleted_before_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    consumed = state.member("consumed-request")
    consumed.write_bytes(b"synthetic fixture")
    inspect_entry = state_module._measure_entry

    def delete_before_inspection(
        parent_descriptor: int,
        entry: os.DirEntry[str],
        *,
        measurement: StateMeasurement,
        flags: int,
        max_entries: int,
        max_bytes: int,
        depth: int,
    ) -> StateMeasurement:
        assert entry.name == consumed.name
        consumed.unlink()
        return inspect_entry(
            parent_descriptor,
            entry,
            measurement=measurement,
            flags=flags,
            max_entries=max_entries,
            max_bytes=max_bytes,
            depth=depth,
        )

    monkeypatch.setattr(state_module, "_measure_entry", delete_before_inspection)

    measurement = state.measure(max_entries=1, max_bytes=1)

    assert measurement == StateMeasurement(entries=1, bytes=0)


@pytest.mark.parametrize("change", ["deleted", "replaced", "permission-denied"])
def test_attempt_state_handles_a_directory_changed_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    nested = state.member("nested")
    nested.mkdir()
    open_path = state_module.os.open

    def change_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "nested" and dir_fd is not None:
            if change == "permission-denied":
                raise PermissionError("synthetic permission refusal")
            if change == "replaced":
                nested.rename(tmp_path / "original-nested")
                nested.mkdir()
            else:
                nested.rmdir()
        return open_path(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(state_module.os, "open", change_before_open)

    if change == "deleted":
        assert state.measure(max_entries=1, max_bytes=1) == StateMeasurement(entries=1, bytes=0)
    else:
        message = "changed during inspection" if change == "replaced" else "cannot inspect"
        with pytest.raises(AttemptStateError, match=message):
            state.measure()


def test_attempt_state_opens_wide_directories_one_at_a_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    for index in range(80):
        state.member(f"directory-{index}").mkdir()
    open_descriptor = state_module.os.open
    close_descriptor = state_module.os.close
    active_descriptors: set[int] = set()
    peak_descriptors = 0

    def tracked_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal peak_descriptors
        descriptor = open_descriptor(path, flags, mode, dir_fd=dir_fd)
        active_descriptors.add(descriptor)
        peak_descriptors = max(peak_descriptors, len(active_descriptors))
        return descriptor

    def tracked_close(descriptor: int) -> None:
        active_descriptors.discard(descriptor)
        close_descriptor(descriptor)

    monkeypatch.setattr(state_module.os, "open", tracked_open)
    monkeypatch.setattr(state_module.os, "close", tracked_close)

    measurement = state.measure(max_entries=100, max_bytes=1)

    assert measurement.entries == 80
    assert peak_descriptors <= 2
    assert not active_descriptors


def test_attempt_state_refuses_excessive_directory_nesting(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    current = state.root
    for index in range(state_module._MAX_DIRECTORY_DEPTH + 1):
        current = current / f"level-{index}"
        current.mkdir()

    with pytest.raises(AttemptStateError, match="nesting limit"):
        state.measure()


def test_attempt_state_classifies_recursive_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)

    class FailingRmtree:
        avoids_symlink_attacks = True

        def __call__(self, path: Path) -> None:
            assert path == state.root
            raise RecursionError

    monkeypatch.setattr(state_module.shutil, "rmtree", FailingRmtree())

    with pytest.raises(AttemptStateError, match="cannot remove"):
        state.cleanup()

    assert state.root.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="directory descriptors are POSIX-specific")
def test_attempt_state_refuses_a_directory_swapped_for_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    nested = state.member("nested")
    nested.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    protected = external / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    open_path = state_module.os.open
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

    monkeypatch.setattr(state_module.os, "open", swap_before_open)

    with pytest.raises(AttemptStateError, match="cannot inspect"):
        state.measure()

    assert protected.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "posix", reason="symbolic link semantics are platform-specific")
def test_cleanup_unlinks_a_member_symlink_without_following_it(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    protected = external / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    state.member("external-link").symlink_to(external, target_is_directory=True)

    state.cleanup()

    assert protected.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "posix", reason="symbolic link semantics are platform-specific")
def test_cleanup_refuses_a_replaced_root(tmp_path: Path) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)
    original = tmp_path / "original"
    state.root.rename(original)
    external = tmp_path / "external"
    external.mkdir()
    protected = external / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    state.root.symlink_to(external, target_is_directory=True)

    with pytest.raises(AttemptStateError, match="replaced"):
        state.cleanup()

    assert protected.read_text(encoding="utf-8") == "keep"
    assert original.is_dir()


@pytest.mark.parametrize(
    "name",
    ["../stdout", "nested/stdout", str(Path(os.sep) / "stdout"), ".", "..", ""],
)
def test_attempt_state_rejects_non_child_member_names(tmp_path: Path, name: str) -> None:
    state = OwnedAttemptState.create(tmp_path / "attempts", ATTEMPT_1)

    with pytest.raises(AttemptStateError, match="single relative name"):
        state.member(name)
