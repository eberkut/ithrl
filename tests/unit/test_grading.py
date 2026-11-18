"""Pure task-bundle and grading contract checks."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any, override
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from ithrl.grading import GradeEvidence, PatchGradingConfig, TestSuite, write_bundle
from ithrl.grading.bundle import GradingError, _read_tree
from ithrl.grading.runtime import _execute, _patch_text


def test_grading_evidence_is_content_safe_and_errors_are_distinct() -> None:
    evidence = GradeEvidence(
        config_digest="a" * 64,
        candidate_digest="b" * 64,
        response_digest="c" * 64,
        attempt_id="attempt-" + "d" * 32,
        status="fail",
        elapsed_milliseconds=2,
    )
    assert evidence == GradeEvidence.model_validate_json(evidence.model_dump_json())
    assert evidence.digest != evidence.model_copy(update={"elapsed_milliseconds": 3}).digest
    with pytest.raises(ValidationError, match="reason"):
        GradeEvidence.model_validate({**evidence.model_dump(), "status": "error"})
    config = PatchGradingConfig(
        bundle_path="/private/task",
        bundle_digest="a" * 64,
        environment_path="/private/environment",
        environment_digest="b" * 64,
    )
    assert "/private" not in repr(config)
    with pytest.raises(ValidationError, match="absolute"):
        PatchGradingConfig.model_validate({**config.model_dump(), "bundle_path": "../task"})


@pytest.mark.parametrize("selector", ["../test.py", "/test.py", "-p", "tests; exit 1"])
def test_suite_selectors_are_data(selector: str) -> None:
    with pytest.raises(ValidationError):
        TestSuite(kind="pytest", selector=selector, expected_count=1)


def test_bundle_snapshots_and_rejects_links(tmp_path: Path) -> None:
    source, checks = tmp_path / "source", tmp_path / "checks"
    source.mkdir()
    checks.mkdir()
    (source / "app.py").write_text("value = 1\n")
    (checks / "test_value.py").write_text("import unittest\n")
    destination = tmp_path / "bundle"
    identity = write_bundle(
        source=source,
        checks=checks,
        destination=destination,
        editable_paths=("app.py",),
        python_path=".",
        source_suite=TestSuite(kind="unittest", selector="tests.test_app", expected_count=1),
        independent_suite=TestSuite(kind="unittest", selector="test_value.py", expected_count=1),
    )
    assert len(identity) == 64
    assert (destination / "source/app.py").read_bytes() == (source / "app.py").read_bytes()
    (source / "link").symlink_to(checks / "test_value.py")
    with pytest.raises(OSError, match="Too many levels of symbolic links"):
        _read_tree(source)


@pytest.mark.parametrize(
    "response",
    [None, "", "```", "```diff", "```patch", "```diff\n```"],
)
def test_missing_repairs_are_rejected(response: str | None) -> None:
    assert _patch_text(response) is None


def test_diff_fence_preserves_patch() -> None:
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n"
    assert _patch_text(f"```diff\n{patch}```") == patch


def test_diff_trailing_blank_context_is_preserved() -> None:
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-value = 0\n+value = 1\n \n"
    assert _patch_text(patch) == patch


def test_report_capture_reads_all_chunks_and_enforces_total_limit() -> None:
    code = "import sys,time;sys.stdout.write('a');sys.stdout.flush();time.sleep(.02);print('b')"
    status, output = asyncio.run(_execute([sys.executable, "-I", "-c", code], capture=True))
    assert status == 0
    assert output == b"ab\n"
    overflow = code.replace("print('b')", "sys.stdout.write('b' * 4096)")
    with pytest.raises(GradingError, match="grading-report-limit"):
        asyncio.run(_execute([sys.executable, "-I", "-c", overflow], capture=True))


@pytest.mark.parametrize("name", [".git", ".git/config", "sub/.gitattributes"])
def test_source_cannot_supply_git_configuration(name: str) -> None:
    from ithrl.grading.bundle import _validate_source_paths

    with pytest.raises(GradingError, match="grading-unsafe-source"):
        _validate_source_paths({"source/" + name: b""})


@pytest.mark.parametrize("separator", ["\u2028", "\x85", "\v", "\r"])
def test_patch_framing_preserves_source_characters(separator: str) -> None:
    patch = f"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = '{separator}'\n+value = 'new'\n"
    assert _patch_text(patch) == patch


@pytest.mark.parametrize(
    "parsed",
    [
        b"-\t-\tapp.py\0",  # Git reports binary counts with '-'.
        b"0\t0\tapp.py\0 delete mode 100644 app.py\n",
        b"1\t1\tother.py\0",
    ],
)
def test_git_metadata_refusal_precedes_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parsed: bytes,
) -> None:
    from types import SimpleNamespace

    from ithrl.grading import patch_driver

    source = tmp_path / "app.py"
    source.write_text("value = 1\n")

    def parse_only(command: list[str], **_kwargs: object):
        assert "--numstat" in command
        return SimpleNamespace(returncode=0, stdout=parsed)

    monkeypatch.setattr(patch_driver.subprocess, "run", parse_only)
    assert patch_driver.apply_patch(tmp_path, tmp_path / "repair.patch", ["app.py"]) == 1
    assert source.read_text() == "value = 1\n"


@pytest.mark.parametrize(
    ("limit", "maximum"), [("MAX_BYTES", 512), ("MAX_FILES", 4), ("MAX_DEPTH", 1)]
)
def test_bundle_limits_apply_to_the_complete_published_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    maximum: int,
) -> None:
    import ithrl.grading.bundle as bundle

    source, checks = tmp_path / "source", tmp_path / "checks"
    source.mkdir()
    checks.mkdir()
    (source / "nested/deeper").mkdir(parents=True)
    (source / "nested/deeper/app.py").write_bytes(b"a" * 300)
    (checks / "test_app.py").write_bytes(b"b" * 300)
    monkeypatch.setattr(bundle, limit, maximum)
    destination = tmp_path / "bundle"
    with pytest.raises(GradingError, match="grading-input-limit"):
        write_bundle(
            source=source,
            checks=checks,
            destination=destination,
            editable_paths=("nested/deeper/app.py",),
            python_path=".",
            source_suite=TestSuite(kind="unittest", selector="tests", expected_count=1),
            independent_suite=TestSuite(kind="unittest", selector="test_app.py", expected_count=1),
        )
    assert not destination.exists()


def test_patch_driver_keeps_io_errors_operational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ithrl.grading import patch_driver

    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    monkeypatch.setattr(
        patch_driver.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=b"1\t1\tapp.py\0")),
    )
    original = Path.read_bytes

    def unreadable(path: Path) -> bytes:
        if path == source:
            raise OSError("fixture read failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(OSError, match="fixture read failure"):
        patch_driver.apply_patch(tmp_path, tmp_path / "repair.patch", ["app.py"])


@pytest.mark.parametrize("phase", ["setup", "body", "teardown", "cleanup", "early-cleanup"])
@pytest.mark.parametrize("exception", [AssertionError, OSError])
def test_unittest_fixture_exceptions_are_operational(
    monkeypatch: pytest.MonkeyPatch, phase: str, exception: type[Exception]
) -> None:
    from ithrl.grading.driver import _unittest

    def fail_in(stage: str) -> None:
        if phase == stage or (phase == "early-cleanup" and stage == "cleanup"):
            raise exception("inert failure")

    class Case(unittest.TestCase):
        @override
        def setUp(self) -> None:
            self.addCleanup(fail_in, "cleanup")
            fail_in("setup")

        def test_repair(self) -> None:
            if phase == "early-cleanup":
                self.doCleanups()
            fail_in("body")

        @override
        def tearDown(self) -> None:
            fail_in("teardown")

    module = ModuleType("grading_fixture")
    module.__dict__["Case"] = Case
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert _unittest({"selector": "grading_fixture.Case"}, False) == {
        "collected": 1,
        "passed": 0,
        "failed": int(phase == "body"),
        "errors": int(phase != "body"),
    }


def test_bundle_rejects_a_missing_independent_verifier() -> None:
    from ithrl.grading.contracts import BundleManifest

    with pytest.raises(ValidationError, match="independent verifier"):
        BundleManifest(
            files={"source/app.py": "a" * 64},
            editable_paths=("app.py",),
            python_path=".",
            source_suite=TestSuite(kind="unittest", selector="tests", expected_count=1),
            independent_suite=TestSuite(kind="unittest", selector="test_app.py", expected_count=1),
        )


@pytest.mark.parametrize("failure", ["overflow", "cancel"])
def test_report_failure_closes_the_subprocess_pipe(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def exercise() -> None:
        launched = asyncio.Event()

        async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            process = await original(*args, **kwargs)
            processes.append(process)
            launched.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        size = 262144 if failure == "overflow" else 1
        code = f"import sys,time;sys.stdout.write('x'*{size});sys.stdout.flush();time.sleep(60)"
        task = asyncio.create_task(_execute([sys.executable, "-I", "-c", code], capture=True))
        async with asyncio.timeout(5):
            await launched.wait()
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(GradingError, match="grading-report-limit"):
                    await task
        process = processes[0]
        assert process.returncode is not None
        assert process.stdout is not None
        assert process.stdout.at_eof()

    asyncio.run(exercise())
