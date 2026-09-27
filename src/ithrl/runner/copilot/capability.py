"""Detect supported Copilot runner profiles without using a real provider."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import BinaryIO

from ithrl.case import CaseError
from ithrl.case.filesystem import atomic_write_bytes
from ithrl.execution import CapabilityProfile
from ithrl.runner.copilot.configuration import CopilotConfiguration
from ithrl.runner.copilot.isolation import (
    CopilotIsolationError,
    CopilotRuntime,
    _bubblewrap_argv,
    _worker_environment,
)
from ithrl.runner.copilot.policy import (
    CAPTURE_TYPE,
    CONTEXT_OPERATION,
    ENDPOINT,
    MATERIALIZATION,
    POLICY_VERSION,
    PROTOCOL_VERSION,
    PROVIDER,
    RECONSTRUCTION_METHOD,
    RUNNER_VERSION,
    SOURCE_SCHEMA,
    CopilotTarget,
    policy_deviation,
    target_policy,
)
from ithrl.runner.runtime import PROCESS_CANCELLATION_GRACE_SECONDS

_PROBE_TIMEOUT_SECONDS = 15
_PROBE_POLL_SECONDS = 0.05
_PROBE_TERMINATION_GRACE_SECONDS = 1.0
_MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
_VERSION = re.compile(r"^GitHub Copilot CLI (?P<version>[^\s]+)$")
_OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_IMPORT_OPTIONS = frozenset({"--dry-run", "--output", "--working-directory"})
_SEMANTIC_SESSION = b"""\
{"type":"session","version":1,"externalId":"ithrl-capability","createdAt":"2026-01-01T00:00:00.000Z"}
{"type":"message","id":"user-1","timestamp":"2026-01-01T00:00:01.000Z","role":"user","content":[{"type":"text","text":"fixture"}]}
{"type":"message","id":"assistant-1","timestamp":"2026-01-01T00:00:02.000Z","role":"assistant","content":[{"type":"text","text":"fixture"}]}
"""
_ISOLATION_RESULT = b"ithrl-copilot-isolation-ok\n"
_ISOLATION_HOST_MARKER = b"ithrl-copilot-readable-host-marker\n"
_ISOLATION_HOST_PARENT = Path("/var/tmp")  # noqa: S108 - host view outside private /tmp
_ISOLATION_UNAVAILABLE = "Copilot isolation runtime is unavailable or incompatible"
_ISOLATION_PROBE_SOURCE = f"""\
#!/usr/bin/python3
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
protected = Path(request["protected"])
copilot = Path(sys.argv[2])
if not copilot.is_file() or copilot.stat().st_mode & 0o111 == 0:
    raise SystemExit(68)
if (protected / "readable-marker").read_bytes() != {_ISOLATION_HOST_MARKER!r}:
    raise SystemExit(69)
try:
    (protected / "unexpected-write").write_bytes(b"unsafe")
except OSError:
    pass
else:
    raise SystemExit(70)
Path("isolation-probe.txt").write_bytes({_ISOLATION_RESULT!r})
""".encode()


@dataclass(frozen=True, slots=True)
class DetectedCopilotCapability:
    """A public profile bound to the exact private inputs that produced it."""

    profile: CapabilityProfile
    runtime: CopilotRuntime = field(repr=False, compare=False)
    configuration: CopilotConfiguration = field(repr=False, compare=False)


class CopilotCapabilityErrorCode(StrEnum):
    """Stable capability-probe refusal categories."""

    UNAVAILABLE = "unavailable"
    PROBE_FAILED = "probe-failed"
    UNSUPPORTED_PROTOCOL = "unsupported-protocol"
    UNSAFE_POLICY = "unsafe-policy"


class CopilotCapabilityError(RuntimeError):
    """The installed Copilot runtime cannot satisfy the runner contract."""

    def __init__(self, code: CopilotCapabilityErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _CommandResult:
    stdout: bytes
    stderr: bytes


def detect_copilot_capability(
    runtime: CopilotRuntime,
    configuration: CopilotConfiguration,
    *,
    target: CopilotTarget,
) -> DetectedCopilotCapability:
    """Return a target-specific structural profile bound to its private inputs."""

    _require_runtime_unchanged(runtime)
    with TemporaryDirectory(prefix="ithrl-copilot-capability-") as temporary:
        root = Path(temporary)
        home = _private_directory(root / "home")
        copilot_home = _private_directory(root / "copilot-home")
        cache = _private_directory(root / "copilot-cache")
        workspace = _private_directory(root / "workspace")
        transcript = root / "session.jsonl"
        try:
            atomic_write_bytes(copilot_home / "settings.json", configuration.settings_data)
            atomic_write_bytes(transcript, _SEMANTIC_SESSION)
        except CaseError as error:
            raise CopilotCapabilityError(
                CopilotCapabilityErrorCode.PROBE_FAILED,
                "Copilot capability state could not be prepared",
            ) from error
        environment = _probe_environment(home, copilot_home, cache)
        _probe_isolation_boundary(runtime, root)
        version = _probe_version(runtime.copilot, environment, workspace)
        root_help = _probe_help(runtime.copilot, environment, workspace, ("--help",))
        _require_root_options(root_help, configuration, target)
        permission_help = _probe_help(
            runtime.copilot,
            environment,
            workspace,
            ("help", "permissions"),
        )
        _require_permission_contract(permission_help)
        import_help = _probe_help(
            runtime.copilot,
            environment,
            workspace,
            ("sessions", "import", "--help"),
        )
        _require_import_options(import_help)
        _probe_semantic_import(
            runtime.copilot,
            environment,
            workspace,
            transcript,
        )
    _require_runtime_unchanged(runtime)
    return DetectedCopilotCapability(
        profile=_profile(
            runtime=runtime,
            configuration=configuration,
            version=version,
            target=target,
        ),
        runtime=runtime,
        configuration=configuration,
    )


def _profile(
    *,
    runtime: CopilotRuntime,
    configuration: CopilotConfiguration,
    version: str,
    target: CopilotTarget,
) -> CapabilityProfile:
    shell_action = target == "shell-action"
    calls = 2 if shell_action else 1
    action_visibility = "visible" if shell_action else "not-applicable"
    action_denial = "enforced"
    deviations = [
        "context is reconstructed through a fresh semantic session import",
        "Copilot custom instructions and skill retrieval are disabled; "
        "captured assignment context is not imported",
        "remote session sync and export are disabled",
        policy_deviation(target),
        f"runner policy {POLICY_VERSION}",
        f"configuration {configuration.public_digest}",
        f"runtime executables {runtime.identity}",
        "native event and target behavior require live calibration before application selection",
    ]
    if shell_action:
        deviations.append(
            "two provider calls are reserved because Copilot may begin a continuation after denial"
        )
    return CapabilityProfile(
        runner_type="copilot",
        runner_version=RUNNER_VERSION,
        harness="copilot",
        runtime_harness_version=version,
        protocol_version=PROTOCOL_VERSION,
        source_schema=SOURCE_SCHEMA,
        deterministic=False,
        supported_capture_types=(CAPTURE_TYPE,),
        supported_materializations=(MATERIALIZATION,),
        supported_context_operations=(CONTEXT_OPERATION,),
        reconstruction_method=RECONSTRUCTION_METHOD,
        supported_targets=(target,),
        provider_transport="required",
        provider_calls_per_attempt=calls,
        tool_network_access="forbidden",
        action_visibility=action_visibility,
        action_denial=action_denial,
        action_execution="denied",
        source_store_effect="unchanged",
        original_workspace_effect="unchanged",
        protected_state_enforcement="preventive",
        disposable_state_effect="writes",
        disposable_state_limit_enforcement="enforced",
        process_containment="process-group",
        supports_timeout=True,
        supports_cancellation=True,
        cancellation_grace_seconds=PROCESS_CANCELLATION_GRACE_SECONDS,
        reports_usage=False,
        input_token_limit_enforcement="unavailable",
        provider=PROVIDER,
        endpoint=ENDPOINT,
        deviations=tuple(deviations),
    )


def _probe_isolation_boundary(runtime: CopilotRuntime, root: Path) -> None:
    probe_root: Path | None = None
    protected_root: Path | None = None
    try:
        with (
            TemporaryDirectory(prefix="isolation-", dir=root) as temporary,
            TemporaryDirectory(
                prefix="ithrl-copilot-protected-",
                dir=_ISOLATION_HOST_PARENT,
            ) as protected_temporary,
        ):
            probe_root = Path(temporary)
            protected_root = Path(protected_temporary)
            workspace, worker = _prepare_isolation_probe(
                probe_root,
                protected_root,
            )
            _run_probe(
                _bubblewrap_argv(
                    runtime,
                    state=probe_root,
                    worker=worker.name,
                ),
                _worker_environment({}),
                workspace,
                failure_code=CopilotCapabilityErrorCode.UNAVAILABLE,
            )
            _verify_isolation_probe(workspace, protected_root)
    except CopilotCapabilityError:
        raise
    except (CaseError, CopilotIsolationError, OSError, ValueError) as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
        ) from error
    if (
        probe_root is None
        or protected_root is None
        or probe_root.exists()
        or protected_root.exists()
    ):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            "Copilot isolation runtime could not clean up its temporary state",
        )


def _prepare_isolation_probe(
    probe_root: Path,
    protected_root: Path,
) -> tuple[Path, Path]:
    workspace = _private_directory(probe_root / "workspace")
    for name in ("home", "copilot-home", "copilot-cache", "runner"):
        _private_directory(probe_root / name)
    worker = probe_root / "worker"
    request = probe_root / "request.json"
    atomic_write_bytes(worker, _ISOLATION_PROBE_SOURCE)
    atomic_write_bytes(protected_root / "readable-marker", _ISOLATION_HOST_MARKER)
    atomic_write_bytes(
        request,
        json.dumps(
            {"protected": os.fspath(protected_root)},
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    worker.chmod(0o500)
    return workspace, worker


def _verify_isolation_probe(workspace: Path, protected_root: Path) -> None:
    if (
        (workspace / "isolation-probe.txt").read_bytes() != _ISOLATION_RESULT
        or (protected_root / "readable-marker").read_bytes() != _ISOLATION_HOST_MARKER
        or (protected_root / "unexpected-write").exists()
    ):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
        )


def _probe_version(executable: Path, environment: dict[str, str], cwd: Path) -> str:
    result = _run_probe((os.fspath(executable), "--version"), environment, cwd)
    try:
        text = result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot returned an invalid version",
        ) from error
    lines = text.splitlines()
    match = _VERSION.fullmatch(lines[0]) if lines else None
    if match is None:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot returned an unsupported version shape",
        )
    return match.group("version").removesuffix(".")


def _probe_help(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    arguments: tuple[str, ...],
) -> str:
    result = _run_probe((os.fspath(executable), *arguments), environment, cwd)
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot returned invalid help output",
        ) from error


def _require_root_options(
    help_text: str,
    configuration: CopilotConfiguration,
    target: CopilotTarget,
) -> None:
    required = {argument.partition("=")[0] for argument in target_policy(target).command_flags}
    required.update({"--model", "--resume"})
    if configuration.environment_names:
        required.add("--secret-env-vars")
    if not required.issubset(_help_options(help_text)):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSAFE_POLICY,
            "Copilot lacks a required non-interactive safety control",
        )


def _require_permission_contract(help_text: str) -> None:
    normalized = " ".join(help_text.lower().split())
    if (
        "--available-tools" not in normalized
        or "disables all other tools" not in normalized
        or "denial rules always take precedence" not in normalized
    ):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSAFE_POLICY,
            "Copilot tool availability or denial precedence is unsupported",
        )


def _require_import_options(help_text: str) -> None:
    if not _IMPORT_OPTIONS.issubset(_help_options(help_text)):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot semantic session import is unsupported",
        )


def _help_options(help_text: str) -> frozenset[str]:
    return frozenset(_OPTION.findall(help_text))


def _probe_semantic_import(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    transcript: Path,
) -> None:
    result = _run_probe(
        (
            os.fspath(executable),
            "sessions",
            "import",
            "--dry-run",
            "--output",
            "json",
            "--working-directory",
            os.fspath(cwd),
            os.fspath(transcript),
        ),
        environment,
        cwd,
    )
    try:
        document = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot semantic import returned invalid output",
        ) from error
    if not isinstance(document, dict) or document.get("ok") is not True:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot semantic session import is unsupported",
        )


def _run_probe(
    argv: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    *,
    failure_code: CopilotCapabilityErrorCode = CopilotCapabilityErrorCode.PROBE_FAILED,
) -> _CommandResult:
    process: subprocess.Popen[bytes] | None = None
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(  # noqa: S603 - validated executable and fixed arguments
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            returncode = _wait_for_probe(process, stdout, stderr, failure_code)
        except CopilotCapabilityError:
            if process is not None and not _stop_probe_group(process):
                raise CopilotCapabilityError(
                    failure_code,
                    "Copilot capability probe processes could not be stopped",
                ) from None
            raise
        except TimeoutError:
            if process is not None and not _stop_probe_group(process):
                raise CopilotCapabilityError(
                    failure_code,
                    "Copilot capability probe processes could not be stopped",
                ) from None
            raise CopilotCapabilityError(
                failure_code,
                "Copilot capability probe timed out",
            ) from None
        except OSError as error:
            if process is not None and not _stop_probe_group(process):
                raise CopilotCapabilityError(
                    failure_code,
                    "Copilot capability probe processes could not be stopped",
                ) from error
            raise CopilotCapabilityError(
                CopilotCapabilityErrorCode.UNAVAILABLE,
                "Copilot capability probe could not start",
            ) from error
        except BaseException:
            if process is not None:
                _stop_probe_group(process)
            raise
        if _probe_group_exists(process.pid):
            stopped = _stop_probe_group(process)
            message = (
                "Copilot capability probe left a descendant process"
                if stopped
                else "Copilot capability probe processes could not be stopped"
            )
            raise CopilotCapabilityError(failure_code, message)
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(_MAX_PROBE_OUTPUT_BYTES + 1)
        errors = stderr.read(_MAX_PROBE_OUTPUT_BYTES + 1)
    if returncode != 0:
        raise CopilotCapabilityError(
            failure_code,
            "Copilot capability probe failed",
        )
    return _CommandResult(stdout=output, stderr=errors)


def _wait_for_probe(
    process: subprocess.Popen[bytes],
    stdout: BinaryIO,
    stderr: BinaryIO,
    failure_code: CopilotCapabilityErrorCode,
) -> int:
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    while process.poll() is None:
        if time.monotonic() >= deadline:
            raise TimeoutError
        if stdout.tell() > _MAX_PROBE_OUTPUT_BYTES or stderr.tell() > _MAX_PROBE_OUTPUT_BYTES:
            raise CopilotCapabilityError(
                failure_code,
                "Copilot capability probe exceeded its output limit",
            )
        time.sleep(_PROBE_POLL_SECONDS)
    if stdout.tell() > _MAX_PROBE_OUTPUT_BYTES or stderr.tell() > _MAX_PROBE_OUTPUT_BYTES:
        raise CopilotCapabilityError(
            failure_code,
            "Copilot capability probe exceeded its output limit",
        )
    return process.wait()


def _stop_probe_group(process: subprocess.Popen[bytes]) -> bool:
    _signal_probe_group(process.pid, signal.SIGTERM)
    if _wait_probe_group_gone(process):
        return True
    _signal_probe_group(process.pid, signal.SIGKILL)
    return _wait_probe_group_gone(process)


def _wait_probe_group_gone(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + _PROBE_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not _probe_group_exists(process.pid):
            return True
        time.sleep(_PROBE_POLL_SECONDS)
    process.poll()
    return not _probe_group_exists(process.pid)


def _signal_probe_group(process_group: int, selected_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, selected_signal)
    except OSError:
        return


def _probe_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe_environment(home: Path, copilot_home: Path, cache: Path) -> dict[str, str]:
    return {
        "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_CACHE_HOME": os.fspath(cache),
        "COPILOT_HOME": os.fspath(copilot_home),
        "HOME": os.fspath(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": "/usr/bin:/bin",
    }


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    except OSError as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.PROBE_FAILED,
            "Copilot capability state could not be prepared",
        ) from error
    return path


def _require_runtime_unchanged(runtime: CopilotRuntime) -> None:
    try:
        runtime.assert_unchanged()
    except (CopilotIsolationError, OSError, ValueError) as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            "Copilot runtime changed during capability detection",
        ) from error


__all__ = [
    "CopilotCapabilityError",
    "CopilotCapabilityErrorCode",
    "DetectedCopilotCapability",
    "detect_copilot_capability",
]
