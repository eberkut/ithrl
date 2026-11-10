from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ithrl.runner.codex.isolation import CodexIsolationError
from ithrl.runner.codex.isolation import _copy_private_program as copy_codex_program
from ithrl.runner.codex.isolation import program_digest as codex_program_digest
from ithrl.runner.copilot.isolation import CopilotIsolationError
from ithrl.runner.copilot.isolation import _copy_private_program as copy_copilot_program
from ithrl.runner.copilot.isolation import program_digest as copilot_program_digest

READERS = (
    (codex_program_digest, CodexIsolationError, "Codex"),
    (copilot_program_digest, CopilotIsolationError, "Copilot"),
)


@pytest.mark.parametrize(("reader", "error_type", "harness"), READERS)
@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize(
    "content",
    [b"", b"# sanitized worker\npass\n", bytes(1024 * 1024)],
    ids=["empty", "small", "limit"],
)
def test_program_digest_reads_exact_bytes_and_resolves_installed_symlinks(
    tmp_path: Path,
    reader: Callable[[Path], str],
    error_type: type[RuntimeError],
    harness: str,
    symlink: bool,
    content: bytes,
) -> None:
    source = tmp_path / "worker.py"
    source.write_bytes(content)
    selected = source
    if symlink:
        selected = tmp_path / "installed-worker.py"
        selected.symlink_to(source)

    assert reader(selected) == hashlib.sha256(content).hexdigest()
    assert source.read_bytes() == content


@pytest.mark.parametrize(("reader", "error_type", "harness"), READERS)
@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "oversized"])
def test_program_digest_refuses_nonregular_or_unbounded_sources(
    tmp_path: Path,
    reader: Callable[[Path], str],
    error_type: type[RuntimeError],
    harness: str,
    kind: str,
) -> None:
    source = tmp_path / "worker.py"
    if kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "oversized":
        with source.open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)

    with pytest.raises(error_type) as captured:
        reader(source)
    if kind == "missing":
        assert str(captured.value) == f"cannot read the private {harness} worker"
        assert isinstance(captured.value.__cause__, FileNotFoundError)
    else:
        assert str(captured.value) == f"the {harness} worker source is invalid"
        assert captured.value.__cause__ is None


@pytest.mark.parametrize(("reader", "error_type", "harness"), READERS)
@pytest.mark.parametrize("when", ["opening", "reading", "replacement", "removed", "io-error"])
def test_program_digest_refuses_source_races_and_preserves_error_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[Path], str],
    error_type: type[RuntimeError],
    harness: str,
    when: str,
) -> None:
    source = tmp_path / "worker.py"
    source.write_bytes(b"pass\n")
    original_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if when == "io-error":
            raise PermissionError("sanitized read refusal")
        if calls == (1 if when == "opening" else 2):
            if when in {"opening", "reading"}:
                source.write_bytes(b"changed\n")
            elif when == "replacement":
                replacement = tmp_path / "replacement.py"
                replacement.write_bytes(b"pass\n")
                replacement.replace(source)
            elif when == "removed":
                source.unlink()
        return original_fstat(descriptor)

    # Pytest's FD capture also calls fstat; restore the fault before capture resumes.
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", changed_fstat)
        with pytest.raises(error_type) as captured:
            reader(source)
    if when == "opening":
        assert str(captured.value) == f"the {harness} worker source changed while opening"
        assert captured.value.__cause__ is None
    elif when in {"reading", "replacement", "removed"}:
        assert str(captured.value) == f"the {harness} worker source changed while it was read"
        assert captured.value.__cause__ is None
    else:
        assert str(captured.value) == f"cannot read the private {harness} worker"
        assert isinstance(captured.value.__cause__, PermissionError)


@pytest.mark.parametrize(
    ("copy_program", "error_type", "harness"),
    [
        (copy_codex_program, CodexIsolationError, "Codex"),
        (copy_copilot_program, CopilotIsolationError, "Copilot"),
    ],
)
def test_worker_digest_mismatch_preserves_existing_destination(
    tmp_path: Path,
    copy_program: Callable[..., Path],
    error_type: type[RuntimeError],
    harness: str,
) -> None:
    source = tmp_path / "worker.py"
    source.write_bytes(b"pass\n")
    destination = tmp_path / "private-worker.py"
    destination.write_bytes(b"unchanged\n")

    with pytest.raises(error_type) as captured:
        copy_program(source, destination, expected_digest="0" * 64)

    assert str(captured.value) == f"the {harness} worker source changed"
    assert captured.value.__cause__ is None
    assert source.read_bytes() == b"pass\n"
    assert destination.read_bytes() == b"unchanged\n"


@pytest.mark.parametrize(("reader", "error_type", "harness"), READERS)
def test_worker_path_replacement_after_final_descriptor_stat_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[Path], str],
    error_type: type[RuntimeError],
    harness: str,
) -> None:
    source = tmp_path / "worker.py"
    source.write_bytes(b"pass\n")
    original_fstat = os.fstat
    original_stat = Path.stat
    descriptor_stats = 0
    pathname_stats = 0

    def replace_after_stat(descriptor: int) -> os.stat_result:
        nonlocal descriptor_stats
        metadata = original_fstat(descriptor)
        descriptor_stats += 1
        if descriptor_stats == 2:
            replacement = tmp_path / "replacement.py"
            replacement.write_bytes(b"pass\n")
            replacement.replace(source)
        return metadata

    def record_final_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal pathname_stats
        if path == source and descriptor_stats == 2:
            pathname_stats += 1
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "fstat", replace_after_stat)
    monkeypatch.setattr(Path, "stat", record_final_stat)
    with pytest.raises(error_type, match=f"the {harness} worker source changed while it was read"):
        reader(source)
    assert descriptor_stats == 2
    assert pathname_stats == 1
