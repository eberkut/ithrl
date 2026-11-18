"""Apply proposed repairs and run fixed test suites in disposable, network-denied namespaces."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ithrl.context import content_digest
from ithrl.grading.bundle import (
    MAX_BYTES,
    GradingError,
    ValidatedGrading,
    _read_tree,
    validate_grading,
)
from ithrl.grading.contracts import (
    GRADING_CLEANUP_SECONDS,
    GradeEvidence,
    PatchGradingConfig,
    SuiteEvidence,
)

MAX_RESPONSE_BYTES = 64 * 1024
MAX_REPORT_BYTES = 4096
RUNTIME_STARTUP_TIMEOUT_SECONDS = 2


def _patch_text(response: str | None) -> str | None:
    """Remove only an outer code fence; Git owns patch parsing and path decoding."""

    if response is None or len(response.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return None
    lines = deque(response.split("\n"))
    while lines and lines[0] in ("", "\r"):
        lines.popleft()
    while lines and lines[-1] in ("", "\r"):
        lines.pop()
    if lines and lines[0].removesuffix("\r") in ("```diff", "```patch", "```"):
        if len(lines) < 2 or lines[-1].removesuffix("\r") != "```":
            return None
        lines.popleft()
        lines.pop()
    return "\n".join(lines) + "\n" if lines else None


def _materialize(root: Path, files: dict[str, bytes]) -> None:
    """Write one validated snapshot into a private tree."""

    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _namespace(source: Path, *, writable: bool) -> list[str]:
    command = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--clearenv",
        "--cap-drop",
        "ALL",
    ]
    for directory in ("/usr", "/lib", "/lib64"):
        command += ["--ro-bind", directory, directory]
    command += ["--bind" if writable else "--ro-bind", str(source), "/work"]
    command += ["--proc", "/proc", "--dev", "/dev", "--chdir", "/work"]
    for directory in ("/tmp", "/var/tmp"):  # noqa: S108 - private namespace tmpfs mounts.
        command += ["--size", str(16 * 1024 * 1024), "--tmpfs", directory]
    command += ["--setenv", "PATH", "/usr/bin:/bin"]
    command += ["--setenv", "PYTHONDONTWRITEBYTECODE", "1"]
    command += ["--setenv", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"]
    return command


def _isolated_python(inputs: ValidatedGrading) -> list[str]:
    return [
        "--remount-ro",
        "/dev",
        "--remount-ro",
        "/",
        f"/usr/bin/python{inputs.python_version}",
        "-I",
        "-S",
    ]


def validate_grading_runtime(
    inputs: ValidatedGrading, *, timeout_seconds: float = RUNTIME_STARTUP_TIMEOUT_SECONDS
) -> None:
    """Start the configured grader namespace without loading task files or packages."""

    if not timeout_seconds > 0:
        raise GradingError("grading-runtime-unavailable")
    try:
        with tempfile.TemporaryDirectory(prefix="ithrl-grade-probe-") as temporary:
            command = _namespace(Path(temporary), writable=False)
            command += [*_isolated_python(inputs), "-c", "pass"]
            subprocess.run(  # noqa: S603 - fixed system interpreter in the grading namespace.
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                timeout=min(timeout_seconds, RUNTIME_STARTUP_TIMEOUT_SECONDS),
                check=True,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError):
        raise GradingError("grading-runtime-unavailable") from None


async def _execute(command: list[str], *, capture: bool = False) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        start_new_session=True,
    )
    try:
        output = b""
        if process.stdout is not None:
            try:
                output = await process.stdout.readexactly(MAX_REPORT_BYTES + 1)
            except asyncio.IncompleteReadError as incomplete:
                output = incomplete.partial
            if len(output) > MAX_REPORT_BYTES:
                raise GradingError("grading-report-limit")
        return await process.wait(), output
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            async with asyncio.timeout(GRADING_CLEANUP_SECONDS):
                # Discard remaining output in bounded chunks so the pipe transport
                # reaches EOF before the event loop can close.
                if process.stdout is not None:
                    while await process.stdout.read(MAX_REPORT_BYTES + 1):
                        pass
                await process.wait()
        except TimeoutError:
            raise GradingError("grading-cleanup-failed") from None


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise GradingError("grading-timeout")


async def _grade(
    inputs: ValidatedGrading,
    response: str | None,
    deadline: float,
    results: list[SuiteEvidence],
) -> Literal["pass", "fail", "error"]:
    patch = _patch_text(response)
    _check_deadline(deadline)
    if patch is None:
        return "fail"
    with tempfile.TemporaryDirectory(prefix="ithrl-grade-") as temporary:
        root = Path(temporary)
        _materialize(root, inputs.files)
        source, checks = root / "source", root / "checks"
        _check_deadline(deadline)
        driver = root / "driver"
        driver.mkdir()
        nonce = uuid4().hex
        (driver / "suite.json").write_text(
            json.dumps(
                {
                    "nonce": nonce,
                    "max_bytes": MAX_BYTES,
                    "editable_paths": inputs.manifest.editable_paths,
                }
            )
        )
        (driver / "patch.py").write_bytes(inputs.patch_driver)
        patch_file = root / "repair.patch"
        patch_file.write_text(patch, encoding="utf-8")
        command = _namespace(source, writable=True)
        command += ["--ro-bind", str(patch_file), "/repair.patch"]
        command += ["--ro-bind", str(driver), "/driver"]
        command += [*_isolated_python(inputs), "/driver/patch.py"]
        _check_deadline(deadline)
        code, raw = await _execute(command, capture=True)
        try:
            report = json.loads(raw)
            if code or report["nonce"] != nonce or type(report["patch_exit"]) is not int:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise GradingError("grading-patch-runtime-failed") from None
        if report["patch_exit"] < 0:
            raise GradingError("grading-patch-runtime-failed")
        if report["patch_exit"] != 0:
            return "fail"
        after = _read_tree(source)
        before = {
            name[7:]: data for name, data in inputs.files.items() if name.startswith("source/")
        }
        if set(after) != set(before) or any(
            before[name] != data and name not in inputs.manifest.editable_paths
            for name, data in after.items()
        ):
            return "fail"
        # Rejected patches need neither dependency copies nor verifier processes.
        packages = root / "packages"
        packages.mkdir()
        _materialize(packages, inputs.packages)
        _check_deadline(deadline)
        (driver / "run.py").write_bytes(inputs.driver)
        for role, suite in (
            ("source", inputs.manifest.source_suite),
            ("independent", inputs.manifest.independent_suite),
        ):
            nonce = uuid4().hex
            (driver / "suite.json").write_text(
                json.dumps(
                    {
                        "nonce": nonce,
                        "role": role,
                        "python_path": inputs.manifest.python_path,
                        "suite": suite.model_dump(mode="json"),
                    }
                )
            )
            command = _namespace(source, writable=False)
            command += [
                "--ro-bind",
                str(packages),
                f"/env/lib/python{inputs.python_version}/site-packages",
            ]
            command += ["--ro-bind", str(checks), "/checks"]
            command += ["--ro-bind", str(driver), "/driver"]
            command += [*_isolated_python(inputs), "/driver/run.py"]
            _check_deadline(deadline)
            code, raw = await _execute(command, capture=True)
            if code != 0:
                raise GradingError("grading-verifier-failed")
            try:
                report = json.loads(raw)
                if report.pop("nonce") != nonce:
                    raise ValueError
                evidence = SuiteEvidence(role=role, expected=suite.expected_count, **report)
            except (KeyError, TypeError, ValueError):
                raise GradingError("grading-invalid-report") from None
            results.append(evidence)
            if (
                evidence.collected != evidence.expected
                or evidence.errors
                or evidence.passed + evidence.failed != evidence.collected
            ):
                raise GradingError("grading-collection-failed")
        if _read_tree(source) != after:
            raise GradingError("grading-source-changed")
        _check_deadline(deadline)
        return "pass" if all(row.failed == 0 for row in results) else "fail"


async def grade_response(
    config: PatchGradingConfig,
    response: str | None,
    *,
    candidate_digest: str,
    attempt_id: str,
    timeout_seconds: float | None = None,
) -> GradeEvidence:
    """Grade one response; cancellation kills the namespace before propagating.

    Input reads/copies are bounded but synchronous. A deadline overrun during them is
    refused before launching another process or accepting any grade; those reads cannot
    be preempted by asyncio's timer.
    """

    started = time.monotonic()
    timeout = (
        config.timeout_seconds
        if timeout_seconds is None
        else min(config.timeout_seconds, timeout_seconds)
    )
    deadline = started + timeout
    status: Literal["pass", "fail", "error"]
    reason = None
    results: list[SuiteEvidence] = []
    try:
        async with asyncio.timeout(timeout):
            inputs = validate_grading(config)
            _check_deadline(deadline)
            status = await _grade(inputs, response, deadline, results)
            del inputs
            validate_grading(config)
            _check_deadline(deadline)
    except TimeoutError:
        status, reason = "error", "grading-timeout"
    except GradingError as error:
        status, reason = "error", error.code
    except (OSError, ValueError, TypeError):
        status, reason = "error", "grading-unavailable"
    if status != "error" and time.monotonic() >= deadline:
        status, reason = "error", "grading-timeout"
    return GradeEvidence(
        config_digest=config.digest,
        candidate_digest=candidate_digest,
        response_digest=hashlib.sha256((response or "").encode("utf-8")).hexdigest(),
        attempt_id=attempt_id,
        status=status,
        reason_code=reason,
        elapsed_milliseconds=int((time.monotonic() - started) * 1000),
        suites=tuple(results),
        report_digest=content_digest(
            {
                "status": status,
                "reason_code": reason,
                "suites": [row.model_dump(mode="json") for row in results],
            }
        ),
    )
