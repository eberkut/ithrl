"""Classify test outcomes and configure the isolated grading drivers."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

from ithrl.grading import driver, patch_driver


def test_unittest_import_errors_are_operational() -> None:
    assert driver._unittest({"selector": "no_such_grading_suite.Case"}, False) == {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 1,
    }


def test_unittest_subtests_count_one_parent_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    class Case(unittest.TestCase):
        def test_repair(self) -> None:
            for value in (1, 2):
                with self.subTest(value=value):
                    assert value == 0

    module = ModuleType("grading_subtests")
    module.__dict__["Case"] = Case
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert driver._unittest({"selector": "grading_subtests.Case"}, False) == {
        "collected": 1,
        "passed": 0,
        "failed": 1,
        "errors": 0,
    }


@pytest.mark.parametrize(
    ("phase", "outcome", "expected"),
    [
        ("call", "passed", (1, 0, 0)),
        ("call", "failed", (0, 1, 0)),
        ("call", "skipped", (0, 0, 1)),
        ("call", "xfail", (0, 0, 1)),
        ("call", "xpass", (0, 0, 1)),
        ("setup", "failed", (0, 0, 1)),
        ("setup", "skipped", (0, 0, 1)),
        ("teardown", "failed", (0, 0, 1)),
        ("teardown", "passed", (0, 0, 0)),
    ],
)
def test_pytest_reports_separate_test_outcomes_from_operational_errors(
    phase: str, outcome: str, expected: tuple[int, int, int]
) -> None:
    report = SimpleNamespace(
        when=phase,
        passed=outcome in ("passed", "xpass"),
        failed=outcome == "failed",
        skipped=outcome in ("skipped", "xfail"),
    )
    if outcome in ("xfail", "xpass"):
        report.wasxfail = "expected failure"
    counts = driver._PytestCounts()
    counts.pytest_runtest_logreport(report)
    assert (counts.passed, counts.failed, counts.errors) == expected


@pytest.mark.parametrize("broken_collection", [False, True])
def test_pytest_driver_runs_a_real_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_collection: bool
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    suite = tmp_path / ("test_collection.py" if broken_collection else "test_repair.py")
    suite.write_text(
        "raise ValueError('collection failed')\n"
        if broken_collection
        else "def test_pass():\n    assert True\ndef test_fail():\n    assert False\n"
    )
    result = driver._pytest({"selector": str(suite)})
    assert result == {
        "collected": 0 if broken_collection else 2,
        "passed": 0 if broken_collection else 1,
        "failed": 0 if broken_collection else 1,
        "errors": 2 if broken_collection else 0,
    }


@pytest.mark.parametrize("kind", ["pytest", "unittest"])
def test_test_driver_main_sets_limits_and_emits_only_the_bound_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    spec = {
        "python_path": "src",
        "suite": {"kind": kind, "selector": "tests"},
        "role": "independent",
        "nonce": "bound-report",
    }
    monkeypatch.setattr(Path, "read_text", Mock(return_value=json.dumps(spec)))
    limits = Mock()
    monkeypatch.setattr(driver.resource, "setrlimit", limits)
    monkeypatch.setattr(sys, "path", list(sys.path))

    def grade(*args: Any) -> dict[str, int]:
        assert args == (spec["suite"],) + ((True,) if kind == "unittest" else ())
        sys.stdout.write("private test output")
        sys.stderr.write("private test diagnostics")
        return {"collected": 1, "passed": 1, "failed": 0, "errors": 0}

    monkeypatch.setattr(driver, "_pytest" if kind == "pytest" else "_unittest", grade)
    driver.main()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "nonce": "bound-report",
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
    }
    assert sys.path[:2] == [
        "/work/src",
        f"/env/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages",
    ]
    assert limits.call_args_list == [
        call(driver.resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2),
        call(driver.resource.RLIMIT_CPU, (30, 30)),
        call(driver.resource.RLIMIT_FSIZE, (1024 * 1024,) * 2),
        call(driver.resource.RLIMIT_NOFILE, (64, 64)),
        call(driver.resource.RLIMIT_NPROC, (0, 0)),
    ]


def test_patch_driver_main_binds_application_and_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = {"editable_paths": ["app.py"], "max_bytes": 4096, "nonce": "bound-patch"}
    monkeypatch.setattr(Path, "read_text", Mock(return_value=json.dumps(spec)))
    limits = Mock()
    monkeypatch.setattr(patch_driver.resource, "setrlimit", limits)
    apply = Mock(return_value=1)
    monkeypatch.setattr(patch_driver, "apply_patch", apply)
    patch_driver.main()
    apply.assert_called_once_with(Path("/work"), Path("/repair.patch"), ["app.py"])
    assert json.loads(capsys.readouterr().out) == {"nonce": "bound-patch", "patch_exit": 1}
    limits.assert_any_call(patch_driver.resource.RLIMIT_FSIZE, (4096, 4096))


@pytest.mark.parametrize("stage", ["parse", "check", "apply"])
def test_patch_driver_stops_at_the_first_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    (tmp_path / "app.py").write_text("value = 1\n")
    count = {"parse": 1, "check": 2, "apply": 3}[stage]
    run = Mock(
        side_effect=[
            SimpleNamespace(returncode=7 if index == count else 0, stdout=b"1\t1\tapp.py\0")
            for index in range(1, count + 1)
        ]
    )
    monkeypatch.setattr(patch_driver.subprocess, "run", run)
    if stage == "apply":
        with pytest.raises(OSError, match="patch application failed"):
            patch_driver.apply_patch(tmp_path, tmp_path / "repair.patch", ["app.py"])
    else:
        assert patch_driver.apply_patch(tmp_path, tmp_path / "repair.patch", ["app.py"]) == 7
    assert run.call_count == count
    assert (tmp_path / "app.py").read_text() == "value = 1\n"
