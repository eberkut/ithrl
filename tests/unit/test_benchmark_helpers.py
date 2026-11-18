"""Public-task replay snapshots and independent reporting preserve their own evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[2] / "research/benchmarks"


@pytest.fixture
def helpers(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    def load(name: str) -> ModuleType:
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    return load("reproduce"), load("prepare_grading")


@pytest.mark.parametrize(
    ("summary", "code", "expected"),
    [
        ("42 passed in 0.1s", 0, True),
        ("40 passed, 2 failed in 0.1s", 1, False),
        ("41 passed, 1 skipped in 0.1s", 0, None),
        ("41 passed, 1 error in 0.1s", 1, None),
        ("41 passed, 1 xfailed in 0.1s", 0, None),
        ("41 passed, 1 xpassed in 0.1s", 0, None),
    ],
)
def test_reference_pytest_requires_completed_assertions(
    helpers: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    summary: str,
    code: int,
    expected: bool | None,
) -> None:
    replay, _prepare = helpers
    monkeypatch.setattr(
        replay.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=code, stdout=summary, stderr="")),
    )
    args = (
        tmp_path / "env/bin/python",
        Path("/usr"),
        tmp_path / "source",
        tmp_path / "checks/test.py",
        ".",
        ["-m", "pytest"],
        42,
        tmp_path / "log",
    )
    if expected is None:
        with pytest.raises(ValueError, match="complete every"):
            replay.run_suite(*args)
    else:
        assert replay.run_suite(*args) is expected


@pytest.mark.parametrize(
    "summary", ["OK (skipped=1)", "OK (expected failures=1)", "FAILED (unexpected successes=1)"]
)
def test_reference_unittest_rejects_excluded_results(
    helpers: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    summary: str,
) -> None:
    replay, _prepare = helpers
    output = f"Ran 2 tests in 0.01s\n\n{summary}\n"
    monkeypatch.setattr(
        replay.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")),
    )
    with pytest.raises(ValueError, match="complete every"):
        replay.run_suite(
            tmp_path / "env/bin/python",
            Path("/usr"),
            tmp_path,
            tmp_path / "checks/test.py",
            ".",
            ["-c", "fixture"],
            2,
            tmp_path / "log",
        )


@pytest.mark.parametrize("separator", ["\u2028", "\x85", "\r"])
def test_repair_renderer_uses_lf_boundaries(
    helpers: tuple[ModuleType, ModuleType],
    tmp_path: Path,
    separator: str,
) -> None:
    _replay, prepare = helpers
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "app.py").write_bytes(f"value = 'a{separator}b'\n".encode())
    (after / "app.py").write_bytes(b"value = 'fixed'\n")
    patch = prepare.repair_patch(before, after, ("app.py",), ())
    assert "@@ -1 +1 @@" in patch
    assert f"-value = 'a{separator}b'\n" in patch


def test_repair_renderer_marks_unterminated_lines(
    helpers: tuple[ModuleType, ModuleType],
    tmp_path: Path,
) -> None:
    _replay, prepare = helpers
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "app.py").write_bytes(b"value = 1")
    (after / "app.py").write_bytes(b"value = 2")
    patch = prepare.repair_patch(before, after, ("app.py",), ())
    assert patch.count("\\ No newline at end of file\n") == 2


def test_patch_receipt_hashes_the_bytes_actually_applied(
    helpers: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    replay, _prepare = helpers
    fixtures, source = tmp_path / "fixtures", tmp_path / "source"
    fixtures.mkdir()
    source.mkdir()
    (source / "app.py").write_bytes(b"value = 1\n")
    patch = fixtures / "repair.patch"
    original = b"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    patch.write_bytes(original)
    read = Path.read_bytes

    def change_after_read(path: Path) -> bytes:
        raw = read(path)
        if path == patch:
            path.write_bytes(b"replacement bytes")
        return raw

    applied: list[bytes] = []

    def apply(_command: list[str], *, input: bytes, **_kwargs: object) -> SimpleNamespace:
        applied.append(input)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(replay, "ROOT", fixtures)
    monkeypatch.setattr(Path, "read_bytes", change_after_read)
    monkeypatch.setattr(replay.subprocess, "run", apply)
    hashes: dict[str, str] = {}
    replay.apply_changes(source, {"patches": [{"file": "repair.patch"}]}, hashes)
    assert applied == [original]
    assert hashes == {"repair.patch": hashlib.sha256(original).hexdigest()}

    with pytest.raises(ValueError, match="fixture changed"):
        replay.apply_changes(source, {"patches": [{"file": "repair.patch"}]}, hashes)
    assert applied == [original]
