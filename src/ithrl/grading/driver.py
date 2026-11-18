"""Fixed test driver executed inside the grading namespace, using only its Python stdlib."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import resource
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast, override


class _UnittestCounts(unittest.TextTestResult):
    """Separate fixture errors from test failures; subtests share their parent method."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.passed_count = 0
        self.failed_count = 0
        self.excluded_count = 0
        self.accounted_events = 0
        self.before = (0, 0)
        self.fixture_events = 0
        self.in_test = False

    def _events(self) -> tuple[int, int]:
        return (
            len(self.failures)
            + len(self.errors)
            + len(self.unexpectedSuccesses)
            - self.fixture_events,
            len(self.skipped) + len(self.expectedFailures) + self.fixture_events,
        )

    @override
    def startTest(self, test: unittest.TestCase) -> None:
        self.before = self._events()
        self.in_test = False
        # TestResult does not expose phases. These hooks distinguish synchronous
        # and asynchronous test bodies from setup, teardown and cleanups. They
        # exist on TestCase but are omitted from its public typing interface.
        case = cast(Any, test)
        self.test_method = case._callTestMethod
        self.tear_down = case._callTearDown
        self.cleanups = case.doCleanups

        def call_test_method(method: Callable[..., Any]) -> None:
            self.in_test = True
            self.test_method(method)

        def call_tear_down() -> None:
            self.in_test = False
            self.tear_down()

        def do_cleanups() -> bool:
            in_test, self.in_test = self.in_test, False
            try:
                return self.cleanups()
            finally:
                self.in_test = in_test

        case._callTestMethod = call_test_method
        case._callTearDown = call_tear_down
        case.doCleanups = do_cleanups
        super().startTest(test)

    @override
    def addError(self, test: unittest.TestCase, err: Any) -> None:
        self.fixture_events += int(not self.in_test)
        super().addError(test, err)

    @override
    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        self.fixture_events += int(not self.in_test)
        super().addFailure(test, err)

    @override
    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any) -> None:
        self.fixture_events += int(err is not None and not self.in_test)
        super().addSubTest(test, subtest, err)

    @override
    def stopTest(self, test: unittest.TestCase) -> None:
        failed, excluded = self._events()
        failed -= self.before[0]
        excluded -= self.before[1]
        self.failed_count += int(failed > 0)
        self.excluded_count += int(excluded > 0)
        self.passed_count += int(not failed and not excluded)
        self.accounted_events += failed + excluded
        case = cast(Any, test)
        case._callTestMethod = self.test_method
        case._callTearDown = self.tear_down
        case.doCleanups = self.cleanups
        super().stopTest(test)


def _unittest(spec: dict[str, Any], independent: bool) -> dict[str, int]:
    loader = unittest.TestLoader()
    suite = (
        loader.discover("/checks", pattern=spec["selector"])
        if independent
        else loader.loadTestsFromName(spec["selector"])
    )
    if loader.errors:
        return {"collected": 0, "passed": 0, "failed": 0, "errors": len(loader.errors)}
    count = suite.countTestCases()
    result = cast(
        _UnittestCounts,
        unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, resultclass=_UnittestCounts).run(
            suite
        ),
    )
    # A fixture can fail outside a test lifecycle; it is not a failed task assertion.
    unassigned = sum(result._events()) - result.accounted_events
    return {
        "collected": count,
        "passed": result.passed_count,
        "failed": result.failed_count,
        "errors": result.excluded_count + unassigned + int(result.testsRun != count),
    }


class _PytestCounts:
    def __init__(self) -> None:
        self.collected = 0
        self.passed = 0
        self.failed = 0
        self.errors = 0

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)

    def pytest_collectreport(self, report: Any) -> None:
        self.errors += int(report.failed)

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "call":
            if hasattr(report, "wasxfail"):
                self.errors += 1
            else:
                self.passed += int(report.passed)
                self.failed += int(report.failed)
                self.errors += int(report.skipped)
        elif report.failed or report.skipped:
            self.errors += 1


def _pytest(spec: dict[str, Any]) -> dict[str, int]:
    pytest = importlib.import_module("pytest")
    counts = _PytestCounts()
    code = pytest.main(
        ["-o", "addopts=", "-p", "no:cacheprovider", "-q", spec["selector"]], plugins=[counts]
    )
    return {
        "collected": counts.collected,
        "passed": counts.passed,
        "failed": counts.failed,
        "errors": counts.errors + int(code not in (0, 1)),
    }


def main() -> None:
    # These hard limits also apply to code imported by the test suite.
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    spec = json.loads(Path("/driver/suite.json").read_text())
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    sys.path[:0] = [f"/work/{spec['python_path']}", f"/env/lib/python{version}/site-packages"]
    with (
        Path("/dev/null").open("w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        result = (
            _pytest(spec["suite"])
            if spec["suite"]["kind"] == "pytest"
            else _unittest(spec["suite"], spec["role"] == "independent")
        )
    sys.stdout.write(json.dumps({"nonce": spec["nonce"], **result}))


if __name__ == "__main__":
    main()
