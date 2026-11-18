"""Public-task replay snapshots and independent reporting preserve their own evidence."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import subprocess
import sys
import textwrap
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


@pytest.mark.parametrize("selection", ["names", "discover"])
@pytest.mark.parametrize(
    ("methods", "result"),
    [
        ("def test_control(self): pass", True),
        ("def test_control(self): raise TypeError('broken candidate')", False),
        ("def test_control(self): self.fail('regression')", False),
        ("async def test_control(self): self.fail('regression')", False),
        ("async def test_control(self): raise TypeError('broken candidate')", False),
        (
            "def test_control(self):\n    with self.subTest():\n        self.fail('regression')",
            False,
        ),
        ("def setUp(self): raise TypeError('broken fixture')\ndef test_control(self): pass", None),
        (
            "async def asyncSetUp(self): self.fail('broken fixture')\n"
            "async def test_control(self): pass",
            None,
        ),
        ("def tearDown(self): self.fail('broken fixture')\ndef test_control(self): pass", None),
        (
            "def test_control(self): self.addCleanup(self.fail, 'broken fixture')",
            None,
        ),
        (
            "def setUp(self):\n    with self.subTest():\n        self.fail('broken fixture')\n"
            "def test_control(self): pass",
            None,
        ),
    ],
)
def test_reference_unittest_classifies_failures_by_phase(
    helpers: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selection: str,
    methods: str,
    result: bool | None,
) -> None:
    replay, _prepare = helpers
    check = tmp_path / "test_control.py"
    base = "IsolatedAsyncioTestCase" if "async def" in methods else "TestCase"
    check.write_text(
        f"import unittest\nclass Control(unittest.{base}):\n" + textwrap.indent(methods, "    ")
    )
    run = subprocess.run

    def run_fixture(
        command: list[str], *, capture_output: bool, text: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert text
        invocation = command[command.index("/env/bin/python") + 1 :]
        invocation[1] = invocation[1].replace("'/checks'", repr(str(tmp_path)))
        return run(
            [sys.executable, "-B", *invocation],
            cwd=tmp_path,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )

    monkeypatch.setattr(replay.subprocess, "run", run_fixture)
    arguments = (
        ["-m", "unittest", "test_control", "-q"]
        if selection == "names"
        else ["-c", replay.UNITTEST_SUITE, "discover", check.name]
    )
    args = (
        tmp_path / "env/bin/python",
        Path("/usr"),
        tmp_path,
        check,
        ".",
        arguments,
        1,
        tmp_path / "log",
    )
    if result is None:
        with pytest.raises(ValueError, match="did not return a behavioral pass/failure"):
            replay.run_suite(*args)
        assert "Reference test fixture failed" in (tmp_path / "log").read_text()
    else:
        assert replay.run_suite(*args) is result


@pytest.mark.parametrize("layout", ["missing", "symlink", "writable"])
def test_prepare_refuses_unsafe_output_parent(
    helpers: tuple[ModuleType, ModuleType], tmp_path: Path, layout: str
) -> None:
    _replay, prepare = helpers
    parent = tmp_path / "output-parent"
    if layout == "symlink":
        parent.symlink_to(tmp_path, target_is_directory=True)
    elif layout == "writable":
        parent.mkdir()
        parent.chmod(0o777)

    with pytest.raises(ValueError, match=r"existing parent|symlink|writable by other users"):
        prepare.prepare(tmp_path, tmp_path / "env/bin/python", parent / "grading")
    assert not (parent / "grading").exists()
    if layout == "missing":
        assert not parent.exists()


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


def test_contained_returns_the_checked_normalized_path(
    helpers: tuple[ModuleType, ModuleType],
    tmp_path: Path,
) -> None:
    replay, _prepare = helpers
    root = tmp_path / "owned"
    root.mkdir()
    (root / "nested").mkdir()
    assert replay.contained(root, "nested/file") == root / "nested/file"
    assert replay.contained(root, ".") == root
    (root / "alias").symlink_to(root / "nested", target_is_directory=True)
    assert replay.contained(root, "alias/file") == root / "nested/file"
    sibling = tmp_path / "owned-other"
    sibling.mkdir()
    (root / "escape").symlink_to(sibling, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        replay.contained(root, "escape/secret")


@pytest.mark.parametrize("name", ["../secret", "/secret"])
def test_contained_refuses_absolute_and_parent_paths(
    helpers: tuple[ModuleType, ModuleType],
    tmp_path: Path,
    name: str,
) -> None:
    replay, _prepare = helpers
    with pytest.raises(ValueError, match="owning directory"):
        replay.contained(tmp_path, name)


def test_archive_is_bound_to_a_reviewed_leaf_in_the_selected_cache(
    helpers: tuple[ModuleType, ModuleType],
    tmp_path: Path,
) -> None:
    replay, _prepare = helpers
    archive = tmp_path / "iniconfig-source.tar.gz"
    archive.write_bytes(b"reviewed archive bytes")
    assert replay.archive_bytes(tmp_path, "iniconfig") == archive.read_bytes()
    target = tmp_path / "other"
    target.write_bytes(archive.read_bytes())
    archive.unlink()
    archive.symlink_to(target)
    with pytest.raises(OSError, check=lambda error: error.errno == errno.ELOOP):
        replay.archive_bytes(tmp_path, "iniconfig")


def test_task_cannot_supply_an_archive_path(
    helpers: tuple[ModuleType, ModuleType],
    tmp_path: Path,
) -> None:
    replay, _prepare = helpers
    with pytest.raises(ValueError, match="reviewed task"):
        replay.prepare_task(
            tmp_path,
            {"key": "iniconfig", "archive": {"file": "other-source.tar.gz"}},
            tmp_path,
            {},
            {},
        )
