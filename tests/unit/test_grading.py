"""Pure task-bundle and grading contract checks."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import tracemalloc
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, override
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from ithrl.grading import GradeEvidence, PatchGradingConfig, TestSuite, write_bundle
from ithrl.grading.bundle import GradingError, _read_tree
from ithrl.grading.contracts import BundleManifest
from ithrl.grading.runtime import _execute, _patch_text
from ithrl.path_safety import UnsafePathError


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


def test_snapshot_allocates_for_file_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ithrl.grading.bundle as bundle

    (tmp_path / "small").write_bytes(b"x")
    monkeypatch.setattr(bundle, "MAX_BYTES", 8 * 1024 * 1024)
    tracemalloc.start()
    try:
        assert _read_tree(tmp_path) == {"small": b"x"}
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < bundle.MAX_BYTES


def test_bundle_requires_an_existing_controlled_parent_and_uses_private_modes(
    tmp_path: Path,
) -> None:
    source, checks = tmp_path / "source", tmp_path / "checks"
    (source / "nested/deeper").mkdir(parents=True)
    (source / "nested/deeper/app.py").write_bytes(b"source")
    (checks / "nested").mkdir(parents=True)
    (checks / "nested/test_app.py").write_bytes(b"checks")

    def publish(destination: Path) -> None:
        write_bundle(
            source=source,
            checks=checks,
            destination=destination,
            editable_paths=("nested/deeper/app.py",),
            python_path=".",
            source_suite=TestSuite(kind="unittest", selector="tests", expected_count=1),
            independent_suite=TestSuite(
                kind="unittest", selector="nested/test_app.py", expected_count=1
            ),
        )

    missing = tmp_path / "missing"
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    destination = tmp_path / "bundle"
    previous_umask = os.umask(0)
    try:
        with pytest.raises(FileNotFoundError):
            publish(missing / "bundle")
        assert not missing.exists()
        for parent in (unsafe, linked):
            with pytest.raises(UnsafePathError):
                publish(parent / "bundle")
        publish(destination)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for path in destination.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize(("timeout", "expected"), [(0, None), (0.25, 0.25), (10, 2)])
def test_grading_runtime_probe_timeout_is_content_safe(
    monkeypatch: pytest.MonkeyPatch, timeout: float, expected: float | None
) -> None:
    from ithrl.grading import runtime
    from ithrl.grading.bundle import ValidatedGrading

    inputs = Mock(spec=ValidatedGrading, python_version="3.12")
    run = Mock(side_effect=subprocess.TimeoutExpired("private command", 2, stderr=b"private"))
    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(GradingError, match=r"^grading-runtime-unavailable$"):
        runtime.validate_grading_runtime(inputs, timeout_seconds=timeout)
    if expected is None:
        run.assert_not_called()
        return
    (command,), options = run.call_args
    assert command[-5:] == ["/usr/bin/python3.12", "-I", "-S", "-c", "pass"]
    assert options["timeout"] == expected
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL


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
    with pytest.raises(ValidationError, match="independent verifier"):
        BundleManifest(
            files={"source/app.py": "a" * 64},
            editable_paths=("app.py",),
            python_path=".",
            source_suite=TestSuite(kind="unittest", selector="tests", expected_count=1),
            independent_suite=TestSuite(kind="unittest", selector="test_app.py", expected_count=1),
        )


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"python_path": "../outside"}, "remain relative"),
        ({"editable_paths": ("app.py", "app.py")}, "must be unique"),
        ({"files": {"other/app.py": "a" * 64}}, "belong to source or checks"),
        ({"editable_paths": ("missing.py",)}, "must exist in source"),
        (
            {
                "independent_suite": TestSuite(
                    kind="pytest", selector="test_app.py", expected_count=1
                )
            },
            "use unittest discovery",
        ),
    ],
)
def test_bundle_manifest_rejects_unsafe_or_inconsistent_paths(
    update: dict[str, object], message: str
) -> None:
    manifest = {
        "files": {"source/app.py": "a" * 64, "checks/test_app.py": "b" * 64},
        "editable_paths": ("app.py",),
        "python_path": ".",
        "source_suite": TestSuite(kind="unittest", selector="tests", expected_count=1),
        "independent_suite": TestSuite(kind="unittest", selector="test_app.py", expected_count=1),
    }
    with pytest.raises(ValidationError, match=message):
        BundleManifest.model_validate({**manifest, **update})


def test_passing_grade_requires_two_complete_successful_suites() -> None:
    with pytest.raises(ValidationError, match="both complete successful suites"):
        GradeEvidence(
            config_digest="a" * 64,
            candidate_digest="b" * 64,
            response_digest="c" * 64,
            attempt_id="attempt-" + "d" * 32,
            status="pass",
            elapsed_milliseconds=0,
        )


@pytest.mark.parametrize("limit", ["MAX_DEPTH", "MAX_FILES", "MAX_BYTES"])
def test_snapshot_refuses_excessive_depth_entries_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    from ithrl.grading import bundle

    (tmp_path / "nested/deeper").mkdir(parents=True)
    (tmp_path / "nested/deeper/app.py").write_bytes(b"source")
    monkeypatch.setattr(bundle, limit, 0)
    with pytest.raises(GradingError, match="grading-input-limit"):
        _read_tree(tmp_path)


def test_snapshot_refuses_special_files_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(GradingError, match="grading-unsafe-file"):
        _read_tree(tmp_path)


def test_snapshot_refuses_a_file_changed_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    (tmp_path / "app.py").write_bytes(b"source")
    original = os.fstat
    calls = 0

    def changed(fd: int):
        nonlocal calls
        calls += 1
        result = original(fd)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns,
            st_ctime_ns=result.st_ctime_ns + int(calls == 2),
        )

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(GradingError, match="grading-input-changed"):
        _read_tree(tmp_path)


@pytest.mark.parametrize("metadata", [None, "home = /elsewhere\nversion = 3.12.0\n"])
def test_environment_refuses_missing_metadata_or_non_system_python(
    tmp_path: Path, metadata: str | None
) -> None:
    from ithrl.grading.bundle import bind_environment

    if metadata is not None:
        (tmp_path / "pyvenv.cfg").write_text(metadata)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin/python").symlink_to(sys.executable)
    reason = "grading-unsafe-environment" if metadata is None else "grading-system-python-required"
    with pytest.raises(GradingError, match=reason):
        bind_environment(tmp_path)


@pytest.mark.parametrize("manifest", [None, b"not json"])
def test_invalid_bundle_manifest_is_refused_before_environment_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: bytes | None
) -> None:
    from ithrl.grading import bundle

    monkeypatch.setattr(bundle, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(bundle.os, "access", Mock(return_value=True))

    if manifest is not None:
        (tmp_path / "manifest.json").write_bytes(manifest)
    config = PatchGradingConfig(
        bundle_path=str(tmp_path),
        bundle_digest="a" * 64,
        environment_path=str(tmp_path / "missing-environment"),
        environment_digest="b" * 64,
    )
    with pytest.raises(GradingError, match="grading-invalid-manifest"):
        bundle.validate_grading(config)


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
