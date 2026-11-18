"""Exercise patch grading through real network-denied Bubblewrap processes."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import select
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ithrl.grading import (
    GradingError,
    PatchGradingConfig,
    TestSuite,
    bind_environment,
    grade_response,
    validate_grading,
    validate_grading_runtime,
    write_bundle,
)
from ithrl.grading.contracts import SuiteEvidence

PATCH = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"


@pytest.fixture
def grading_config(tmp_path: Path) -> PatchGradingConfig:
    if any(not os.access(path, os.X_OK) for path in ("/usr/bin/git", "/usr/bin/bwrap")):
        pytest.skip("system Git and Bubblewrap required")
    python = next(
        (
            Path(f"/usr/bin/python3.{minor}")
            for minor in (14, 13, 12)
            if Path(f"/usr/bin/python3.{minor}").is_file()
        ),
        None,
    )
    if python is None:
        pytest.skip("system Python 3.12 or newer required")
    result = subprocess.run(  # noqa: S603 - explicit installed system Python, fixed metadata query.
        [
            str(python),
            "-I",
            "-S",
            "-c",
            "import sys;print('.'.join(map(str,sys.version_info[:3])))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    version = result.stdout.strip()
    short = ".".join(version.split(".")[:2])
    environment = tmp_path / "env"
    (environment / "bin").mkdir(parents=True)
    (environment / f"lib/python{short}/site-packages").mkdir(parents=True)
    (environment / "bin/python").symlink_to(python)
    (environment / "pyvenv.cfg").write_text(
        f"home = /usr/bin\nversion_info = {version}\ninclude-system-site-packages = false\n"
    )
    source, checks = tmp_path / "source", tmp_path / "checks"
    source.mkdir()
    checks.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (source / "tests").mkdir()
    (source / "tests/__init__.py").write_text("")
    (source / "tests/test_add.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    (checks / "test_extra.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class TestExtra(unittest.TestCase):\n"
        "    def test_other(self):\n        self.assertEqual(add(-1, 2), 1)\n"
    )
    bundle = tmp_path / "bundle"
    identity = write_bundle(
        source=source,
        checks=checks,
        destination=bundle,
        editable_paths=("calc.py",),
        python_path=".",
        source_suite=TestSuite(kind="unittest", selector="tests.test_add", expected_count=1),
        independent_suite=TestSuite(kind="unittest", selector="test_extra.py", expected_count=1),
    )
    return PatchGradingConfig(
        bundle_path=str(bundle),
        bundle_digest=identity,
        environment_path=str(environment),
        environment_digest=bind_environment(environment),
    )


def grade(config: PatchGradingConfig, patch: str = PATCH):
    return asyncio.run(
        grade_response(config, patch, candidate_digest="b" * 64, attempt_id="attempt-" + "a" * 32)
    )


def test_grading_runtime_starts_without_task_code(grading_config: PatchGradingConfig) -> None:
    validate_grading_runtime(validate_grading(grading_config))


@pytest.mark.parametrize("failure", ["missing-mount", "unsupported-option"])
def test_grading_runtime_refuses_startup_failures(
    grading_config: PatchGradingConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from ithrl.grading import runtime

    namespace = runtime._namespace

    def unavailable(source: Path, *, writable: bool) -> list[str]:
        command = namespace(source, writable=writable)
        if failure == "missing-mount":
            return [*command, "--ro-bind", str(tmp_path / "missing"), "/missing"]
        return [*command, "--ithrl-invalid-test-option"]

    monkeypatch.setattr(runtime, "_namespace", unavailable)
    with pytest.raises(GradingError, match=r"^grading-runtime-unavailable$"):
        validate_grading_runtime(validate_grading(grading_config))


def patch_body(body: str) -> str:
    return "".join(
        difflib.unified_diff(
            "def add(a, b):\n    return a - b\n".splitlines(keepends=True),
            ("def add(a, b):\n" + body).splitlines(keepends=True),
            fromfile="a/calc.py",
            tofile="b/calc.py",
        )
    )


def test_correct_and_incorrect_repairs_have_durable_suite_counts(
    grading_config: PatchGradingConfig,
) -> None:
    accepted = grade(grading_config)
    assert accepted.status == "pass", accepted.reason_code
    assert [(suite.collected, suite.passed, suite.failed) for suite in accepted.suites] == [
        (1, 1, 0),
        (1, 1, 0),
    ]
    rejected = grade(grading_config, PATCH.replace("+    return a + b", "+    return a + b + 1"))
    assert rejected.status == "fail"
    assert [
        (suite.role, suite.collected, suite.passed, suite.failed) for suite in rejected.suites
    ] == [
        ("source", 1, 0, 1),
        ("independent", 1, 0, 1),
    ]
    assert accepted.response_digest != rejected.response_digest


def test_wrong_direction_patch_is_not_automatically_reversed(
    grading_config: PatchGradingConfig,
) -> None:
    reversed_patch = PATCH.replace("-    return a - b", "-    return a + b").replace(
        "+    return a + b", "+    return a - b"
    )
    result = grade(grading_config, reversed_patch)
    assert result.status == "fail"
    assert result.suites == ()


def test_patch_write_failure_is_operational(
    grading_config: PatchGradingConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ithrl.grading.runtime as runtime

    original = runtime._namespace

    def readonly(source: Path, *, writable: bool) -> list[str]:
        return original(source, writable=False)

    monkeypatch.setattr(runtime, "_namespace", readonly)
    result = grade(grading_config)
    assert result.status == "error"
    assert result.reason_code == "grading-patch-runtime-failed"
    assert result.suites == ()


def test_validated_package_bytes_are_snapshotted(
    grading_config: PatchGradingConfig,
) -> None:
    from ithrl.grading.bundle import validate_grading
    from ithrl.grading.runtime import _grade

    environment = Path(grading_config.environment_path)
    packages = next((environment / "lib").glob("python*/site-packages"))
    dependency = packages / "grade_dependency.py"
    dependency.write_text("offset = 0\n")
    config = grading_config.model_copy(update={"environment_digest": bind_environment(environment)})
    inputs = validate_grading(config)
    dependency.write_text("offset = 100\n")
    results = []
    status = asyncio.run(
        _grade(
            inputs,
            patch_body("    import grade_dependency\n    return a + b + grade_dependency.offset\n"),
            time.monotonic() + 10,
            results,
        )
    )
    assert status == "pass"
    assert len(results) == 2
    assert grade(config).reason_code == "grading-environment-changed"


@pytest.mark.parametrize("name", ["pyvenv.cfg", "python-binary", "git-binary", "bubblewrap-binary"])
def test_package_names_cannot_collide_with_environment_metadata(
    grading_config: PatchGradingConfig, name: str
) -> None:
    from ithrl.grading.bundle import GradingError, validate_grading

    environment = Path(grading_config.environment_path)
    package_file = next((environment / "lib").glob("python*/site-packages")) / name
    package_file.write_bytes(b"original")
    config = grading_config.model_copy(update={"environment_digest": bind_environment(environment)})
    assert validate_grading(config).packages[name] == b"original"
    package_file.write_bytes(b"changed")
    with pytest.raises(GradingError, match="grading-environment-changed"):
        validate_grading(config)


@pytest.mark.parametrize("late_validation", [False, True])
def test_synchronous_validation_overrun_is_never_accepted(
    grading_config: PatchGradingConfig,
    monkeypatch: pytest.MonkeyPatch,
    late_validation: bool,
) -> None:
    import ithrl.grading.runtime as runtime

    original = runtime.validate_grading
    elapsed = [0.0]
    calls = 0

    def validate(config: PatchGradingConfig):
        nonlocal calls
        result = original(config)
        calls += 1
        if calls == (2 if late_validation else 1):
            elapsed[0] = 11.0
        return result

    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(runtime, "validate_grading", validate)
    result = grade(grading_config, PATCH if late_validation else "")
    assert result.status == "error"
    assert result.reason_code == "grading-timeout"
    assert len(result.suites) == (2 if late_validation else 0)


def test_changed_bundle_is_operational_failure(grading_config: PatchGradingConfig) -> None:
    (Path(grading_config.bundle_path) / "checks/test_extra.py").write_text("broken")
    result = grade(grading_config)
    assert result.status == "error"
    assert result.reason_code == "grading-bundle-changed"


def test_changed_implementation_is_refused_before_execution(
    grading_config: PatchGradingConfig,
) -> None:
    result = grade(grading_config.model_copy(update={"implementation_digest": "0" * 64}))
    assert result.status == "error"
    assert result.reason_code == "grading-implementation-changed"


def test_syntax_invalid_repair_fails_without_running_test_collection(
    grading_config: PatchGradingConfig,
) -> None:
    result = grade(grading_config, PATCH.replace("+    return a + b", "+    return ("))
    assert result.status == "fail"
    assert result.suites == ()


def test_changed_collection_is_not_a_behavioral_failure(
    grading_config: PatchGradingConfig,
) -> None:
    from ithrl.grading.contracts import BundleManifest

    manifest_path = Path(grading_config.bundle_path) / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["source_suite"]["expected_count"] = 2
    manifest = BundleManifest.model_validate_json(json.dumps(raw))
    manifest_path.write_text(manifest.model_dump_json())
    config = grading_config.model_copy(update={"bundle_digest": manifest.digest})
    result = grade(config)
    assert result.status == "error"
    assert result.reason_code == "grading-collection-failed"


def test_failing_subtests_count_one_failed_parent_method(
    grading_config: PatchGradingConfig,
) -> None:
    from ithrl.grading.contracts import BundleManifest

    bundle = Path(grading_config.bundle_path)
    check = bundle / "checks/test_extra.py"
    check.write_text(
        "import unittest\nfrom calc import add\n"
        "class TestExtra(unittest.TestCase):\n"
        "    def test_other(self):\n"
        "        for a, b in [(1, 2), (4, 5), (-1, 2)]:\n"
        "            with self.subTest(a=a, b=b):\n"
        "                self.assertEqual(add(a, b), a + b)\n"
    )
    manifest_path = bundle / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["files"]["checks/test_extra.py"] = hashlib.sha256(check.read_bytes()).hexdigest()
    manifest = BundleManifest.model_validate_json(json.dumps(raw))
    manifest_path.write_text(manifest.model_dump_json())
    config = grading_config.model_copy(update={"bundle_digest": manifest.digest})
    result = grade(config, PATCH.replace("+    return a + b", "+    return a + b + 1"))
    assert result.status == "fail", result.reason_code
    assert result.suites[1].collected == 1
    assert result.suites[1].failed == 1
    assert result.suites[1].passed == 0


def test_early_success_exit_is_not_a_passing_grade(grading_config: PatchGradingConfig) -> None:
    patch = PATCH.replace("+    return a + b", "+    import os; os._exit(0)")
    result = grade(grading_config, patch)
    assert result.status == "error"
    assert result.reason_code == "grading-invalid-report"


def test_verifier_timeout_is_not_a_failed_repair(grading_config: PatchGradingConfig) -> None:
    patch = PATCH.replace("+    return a + b", "+    while True: pass")
    result = grade(grading_config.model_copy(update={"timeout_seconds": 1}), patch)
    assert result.status == "error"
    assert result.reason_code == "grading-timeout"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("import os; os._exit(0)", "grading-invalid-report"),
        ("import os; os._exit(7)", "grading-verifier-failed"),
        ("while True: pass", "grading-timeout"),
    ],
)
def test_later_verifier_error_retains_completed_source_grade(
    grading_config: PatchGradingConfig,
    failure: str,
    expected: str,
) -> None:
    repair = patch_body(f"    if a == -1:\n        {failure}\n    return a + b\n")
    result = grade(grading_config.model_copy(update={"timeout_seconds": 1}), repair)
    assert result.status == "error"
    assert result.reason_code == expected
    assert len(result.suites) == 1
    assert result.suites[0].role == "source"
    assert result.suites[0].passed == 1


def test_only_bounded_scratch_filesystems_are_writable(
    grading_config: PatchGradingConfig,
) -> None:
    repair = patch_body(
        "    import errno, os\n"
        "    from pathlib import Path\n"
        "    for base in ['/', '/var', '/dev', '/dev/shm']:\n"
        "        try:\n"
        "            (Path(base) / 'ithrl-inert-write').write_text('inert')\n"
        "        except OSError as error:\n"
        "            assert error.errno == errno.EROFS, (base, error.errno)\n"
        "        else:\n"
        "            raise AssertionError(base)\n"
        "    for base in ['/tmp', '/var/tmp']:\n"
        "        state = os.statvfs(base)\n"
        "        assert state.f_blocks * state.f_frsize <= 16 * 1024 * 1024\n"
        "        (Path(base) / 'ithrl-inert-write').write_text('inert')\n"
        "    return a + b\n"
    )
    result = grade(grading_config, repair)
    assert result.status == "pass", result.reason_code


def test_cancelled_grade_reaps_its_namespace(
    grading_config: PatchGradingConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ithrl.grading.runtime as runtime

    original_spawn = asyncio.create_subprocess_exec
    original_namespace = runtime._namespace
    processes: list[asyncio.subprocess.Process] = []
    signal_directory = tmp_path / "signal"
    signal_directory.mkdir()
    started = signal_directory / "started"

    def namespace(source: Path, *, writable: bool) -> list[str]:
        return [
            *original_namespace(source, writable=writable),
            "--bind",
            str(signal_directory),
            "/signal",
        ]

    monkeypatch.setattr(runtime, "_namespace", namespace)

    def descendants(pid: int) -> list[int]:
        children = [
            int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        ]
        return [*children, *(child for value in children for child in descendants(value))]

    def verifier_pid(pid: int) -> int:
        for child in descendants(pid):
            command = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")
            if (
                Path(os.fsdecode(command[0])).name.startswith("python3.")
                and b"/driver/run.py" in command
            ):
                return child
        raise AssertionError("sandboxed verifier not found")

    async def exercise() -> None:
        async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(
            grade_response(
                grading_config,
                patch_body(
                    "    from pathlib import Path\n"
                    "    Path('/signal/started').write_text('ready')\n"
                    "    while True: pass\n"
                ),
                candidate_digest="b" * 64,
                attempt_id="attempt-" + "a" * 32,
            )
        )
        try:
            async with asyncio.timeout(5):
                while not started.exists():
                    if task.done():
                        pytest.fail(f"verifier did not start: {(await task).reason_code}")
                    await asyncio.sleep(0.01)
            descriptor = os.pidfd_open(verifier_pid(processes[-1].pid))
            try:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert select.select([descriptor], [], [], 2)[0] == [descriptor]
            finally:
                os.close(descriptor)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(exercise())
    assert processes
    assert all(process.returncode is not None for process in processes)
    for process in processes:
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)


def test_grade_has_no_host_network_or_inherited_secret(
    grading_config: PatchGradingConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ITHRL_INERT_TEST_SECRET", "not-forwarded")
    (Path(grading_config.environment_path) / "unbound-secret").write_text("not-mounted")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        original = "def add(a, b):\n    return a - b\n"
        changed = (
            "def add(a, b):\n"
            "    import os, socket\n"
            "    from pathlib import Path\n"
            "    assert 'ITHRL_INERT_TEST_SECRET' not in os.environ\n"
            "    assert not Path('/env/unbound-secret').exists()\n"
            "    try:\n"
            f"        socket.create_connection(('127.0.0.1', {port}), .1).close()\n"
            "    except OSError:\n"
            "        return a + b\n"
            "    return 0\n"
        )
        patch = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                changed.splitlines(keepends=True),
                fromfile="a/calc.py",
                tofile="b/calc.py",
            )
        )
        result = grade(grading_config, patch)
    assert result.status == "pass", result.reason_code


def test_git_accepts_asymmetric_context_without_repairing_the_patch(
    grading_config: PatchGradingConfig,
) -> None:
    # The hunk is anchored at the start, not EOF: GNU patch --fuzz=0 rejects
    # this valid asymmetric context. Git applies the exact counted hunk.
    from ithrl.grading.bundle import validate_grading
    from ithrl.grading.runtime import _grade

    inputs = validate_grading(grading_config)
    prefix = "# one\n# two\n# three\n# four\n# five\n"
    suffix = "# six\n# seven\n# eight\n# nine\n"
    inputs.files["source/calc.py"] = (
        prefix + "def add(a, b):\n    return a - b\n" + suffix
    ).encode()
    patch = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,9 +1,9 @@\n"
        + "".join(" " + line + "\n" for line in prefix.splitlines())
        + " def add(a, b):\n-    return a - b\n+    return a + b\n # six\n # seven\n"
    )
    suites = []
    status = asyncio.run(_grade(inputs, patch, time.monotonic() + 10, suites))
    assert status == "pass"
    assert len(suites) == 2


@pytest.mark.parametrize("change", ["mode", "symlink", "directory", "hardlink"])
def test_tree_changes_are_refused_before_compilation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from ithrl.grading import patch_driver

    source = tmp_path / "source"
    source.mkdir()
    target = source / "app.py"
    target.write_text("raise AssertionError('must not compile')\n")

    def fake_apply(command: list[str], **kwargs: object) -> SimpleNamespace:
        if "--numstat" in command:
            return SimpleNamespace(returncode=0, stdout=b"1\t1\tapp.py\0")
        if "--check" in command:
            return SimpleNamespace(returncode=0)
        if change == "mode":
            target.chmod(0o755)
        elif change == "symlink":
            target.unlink()
            target.symlink_to(tmp_path / "outside")
        elif change == "directory":
            (source / "extra").mkdir()
        else:
            os.link(target, source / "extra.py")
        return SimpleNamespace(returncode=0)

    def refuse_compile(*args: object, **kwargs: object) -> None:
        pytest.fail("compilation occurred before tree verification")

    monkeypatch.setattr(patch_driver.subprocess, "run", fake_apply)
    monkeypatch.setattr("builtins.compile", refuse_compile)
    assert patch_driver.apply_patch(source, tmp_path / "repair.patch", ["app.py"]) == 1


def test_small_repair_to_large_existing_file_uses_bundle_size_limit(
    grading_config: PatchGradingConfig,
) -> None:
    from ithrl.grading.bundle import validate_grading
    from ithrl.grading.runtime import _grade

    inputs = validate_grading(grading_config)
    # Keep the added context short while making the existing file exceed 1 MiB.
    inputs.files["source/calc.py"] += b"# context\n" * 160_000
    patch = PATCH.replace("@@ -1,2 +1,2 @@", "@@ -1,5 +1,5 @@") + " # context\n" * 3
    suites = []
    status = asyncio.run(_grade(inputs, patch, time.monotonic() + 10, suites))
    assert status == "pass"
    assert len(suites) == 2


@pytest.mark.parametrize(
    "extra",
    [
        "diff --git a/calc.py b/calc.py\nold mode 100644\nnew mode 100755\n",
        "diff --git a/calc.py b/other.py\nsimilarity index 100%\nrename from calc.py\nrename to other.py\n",
        "diff --git a/calc.py b/other.py\nsimilarity index 100%\ncopy from calc.py\ncopy to other.py\n",
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n",
        "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-old\n+new\n",
        "--- a/tests/test_add.py\n+++ b/tests/test_add.py\n@@ -1 +1 @@\n-old\n+new\n",
    ],
)
def test_git_rejects_disallowed_operations_mixed_with_a_valid_repair(
    grading_config: PatchGradingConfig,
    extra: str,
) -> None:
    result = grade(grading_config, PATCH + extra)
    assert result.status == "fail"
    assert result.suites == ()


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_git_refuses_nonregular_index_types(grading_config: PatchGradingConfig, mode: str) -> None:
    result = grade(
        grading_config, f"diff --git a/calc.py b/calc.py\nindex aaaaaaa..bbbbbbb {mode}\n" + PATCH
    )
    assert result.status == "fail"
    assert result.suites == ()


@pytest.mark.parametrize(
    "comment",
    ["-- SQL comment", "café", "unicode \u2028 inside a line", "carriage \r inside a line"],
)
def test_git_parses_hunk_content_without_treating_it_as_headers(
    grading_config: PatchGradingConfig,
    comment: str,
) -> None:
    from ithrl.grading.bundle import validate_grading
    from ithrl.grading.runtime import _grade

    inputs = validate_grading(grading_config)
    original = f"'''\n{comment}\n'''\ndef add(a, b):\n    return a - b\n"
    repaired = "def add(a, b):\n    return a + b\n"
    # LF-only framing preserves Unicode separators embedded in source lines.
    patch = "".join(
        difflib.unified_diff(
            [s + "\n" for s in original.split("\n")[:-1]],
            repaired.splitlines(keepends=True),
            fromfile="a/calc.py",
            tofile="b/calc.py",
        )
    )
    inputs.files["source/calc.py"] = original.encode()
    suites: list[SuiteEvidence] = []
    assert asyncio.run(_grade(inputs, patch, time.monotonic() + 10, suites)) == "pass"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            "home = /usr/bin\nversion_info = 3.11.0\ninclude-system-site-packages = false\n",
            "grading-environment-version",
        ),
        ("x" * 4097, "grading-unsafe-environment"),
    ],
)
def test_environment_refuses_unusable_metadata_before_execution(
    grading_config: PatchGradingConfig,
    content: str,
    reason: str,
) -> None:
    from ithrl.grading.bundle import GradingError

    (Path(grading_config.environment_path) / "pyvenv.cfg").write_text(content)
    with pytest.raises(GradingError, match=reason):
        bind_environment(Path(grading_config.environment_path))


def test_tighter_attempt_deadline_retains_completed_suite_counts(
    grading_config: PatchGradingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ithrl.grading.runtime as runtime
    from ithrl.grading.contracts import SuiteEvidence

    async def partial(
        _inputs: object, _response: str | None, _deadline: float, results: list[SuiteEvidence]
    ) -> str:
        results.append(
            SuiteEvidence(role="source", expected=1, collected=1, passed=1, failed=0, errors=0)
        )
        await asyncio.sleep(1)
        return "pass"

    snapshot = runtime.validate_grading(grading_config)

    def validate(_config: PatchGradingConfig):
        return snapshot

    monkeypatch.setattr(runtime, "validate_grading", validate)
    monkeypatch.setattr(runtime, "_grade", partial)
    result = asyncio.run(
        runtime.grade_response(
            grading_config,
            PATCH,
            candidate_digest="b" * 64,
            attempt_id="attempt-" + "a" * 32,
            timeout_seconds=0.1,
        )
    )
    assert result.status == "error"
    assert result.reason_code == "grading-timeout"
    assert len(result.suites) == 1
    assert result.suites[0].passed == 1
