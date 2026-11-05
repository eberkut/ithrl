from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ithrl.runner.codex.isolation import CodexIsolationError
from ithrl.runner.codex.isolation import _read_executable as read_codex
from ithrl.runner.copilot.isolation import CopilotIsolationError
from ithrl.runner.copilot.isolation import _read_executable as read_copilot

READERS = ((read_codex, CodexIsolationError), (read_copilot, CopilotIsolationError))


@pytest.mark.parametrize(("reader", "error_type"), READERS)
def test_executable_digest_resolves_symlink_and_reads_multiple_chunks(
    tmp_path: Path, reader: Callable[[Path], tuple[Path, str]], error_type: type[RuntimeError]
) -> None:
    source = tmp_path / "program"
    content = b"x" * (1024 * 1024 + 7)
    source.write_bytes(content)
    source.chmod(0o700)
    link = tmp_path / "selected"
    link.symlink_to(source)
    assert reader(link) == (source, hashlib.sha256(content).hexdigest())


@pytest.mark.parametrize(("reader", "error_type"), READERS)
@pytest.mark.parametrize("when", ["opening", "reading", "replacement", "removed", "io-error"])
def test_executable_read_races_preserve_error_and_final_stat_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[Path], tuple[Path, str]],
    error_type: type[RuntimeError],
    when: str,
) -> None:
    source = tmp_path / "program"
    source.write_bytes(b"fixture")
    source.chmod(0o700)
    original_fstat = os.fstat
    original_stat = Path.stat
    calls = 0
    final_stat_calls = 0

    def selected_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal final_stat_calls
        if path == source and calls:
            final_stat_calls += 1
        return original_stat(path, follow_symlinks=follow_symlinks)

    def changed_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if when == "io-error":
            raise PermissionError("sanitized refusal")
        if calls == (1 if when == "opening" else 2):
            if when in {"opening", "reading"}:
                source.write_bytes(b"changed-content")
            elif when == "replacement":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(b"fixture")
                replacement.chmod(0o700)
                replacement.replace(source)
            else:
                source.unlink()
        return original_fstat(descriptor)

    monkeypatch.setattr(Path, "stat", selected_stat)
    monkeypatch.setattr(os, "fstat", changed_fstat)
    with pytest.raises(error_type) as captured:
        reader(source)
    if when == "io-error":
        assert "unavailable" in str(captured.value)
        assert isinstance(captured.value.__cause__, PermissionError)
    else:
        assert "changed" in str(captured.value)
        assert captured.value.__cause__ is None
    # Descriptor mutation (including unlink/replace ctime changes) short-circuits pathname stat.
    assert final_stat_calls == 0


@pytest.mark.parametrize(("reader", "error_type"), READERS)
def test_final_executable_stat_failure_retains_harness_exception_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[Path], tuple[Path, str]],
    error_type: type[RuntimeError],
) -> None:
    source = tmp_path / "program"
    source.write_bytes(b"fixture")
    source.chmod(0o700)
    original_stat = Path.stat
    original_fstat = os.fstat
    finished = False
    calls = 0

    def record_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls, finished
        calls += 1
        finished = calls == 2
        return original_fstat(descriptor)

    def selected_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if finished and path == source:
            raise PermissionError("sanitized final stat refusal")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "fstat", record_fstat)
    monkeypatch.setattr(Path, "stat", selected_stat)
    expected = PermissionError if reader is read_codex else error_type
    with pytest.raises(expected) as captured:
        reader(source)
    if reader is read_codex:
        assert captured.value.__cause__ is None
    else:
        assert isinstance(captured.value.__cause__, PermissionError)
