"""Detect supported Copilot runner profiles without using a real provider."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from operator import itemgetter
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import BinaryIO

from ithrl.case import CaseError
from ithrl.case.filesystem import atomic_write_bytes
from ithrl.execution import CapabilityProfile
from ithrl.runner.copilot.configuration import CopilotConfiguration
from ithrl.runner.copilot.isolation import (
    DEFAULT_DISPOSABLE_STATE_BYTES,
    SANDBOX_ROOT,
    CopilotIsolationError,
    CopilotRuntime,
    _bubblewrap_argv,
    _discovery_argv,
    _worker_environment,
)
from ithrl.runner.copilot.policy import (
    CAPTURE_TYPE,
    DISCOVERY_POLICY_VERSION,
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
_MAX_PROBE_STATE_BYTES = DEFAULT_DISPOSABLE_STATE_BYTES
_MAX_PROBE_STATE_ENTRIES = 2_000
_MAX_PROBE_STATE_DEPTH = 64
_VERSION = re.compile(r"^GitHub Copilot CLI (?P<version>[^\s]+)$")
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

    def __init__(
        self,
        code: CopilotCapabilityErrorCode,
        message: str,
        *,
        probe_stage: str | None = None,
        probe_exit_code: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
        state_limit_details: dict[str, int | str] | None = None,
        state_files: tuple[tuple[str, int], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.probe_stage = probe_stage
        self.probe_exit_code = probe_exit_code
        self.stdout = stdout[:_MAX_PROBE_OUTPUT_BYTES]
        self.stderr = stderr[:_MAX_PROBE_OUTPUT_BYTES]
        self.state_limit_details = state_limit_details
        self.state_files = state_files[:16]


@dataclass(frozen=True, slots=True)
class _CommandResult:
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


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
        _private_directory(root / "home")
        copilot_home = _private_directory(root / "copilot-home")
        _private_directory(root / "copilot-cache")
        workspace = _private_directory(root / "workspace")
        _private_directory(root / "runner")
        _private_directory(root / "tmp")
        try:
            atomic_write_bytes(copilot_home / "settings.json", configuration.settings_data)
        except CaseError as error:
            raise CopilotCapabilityError(
                CopilotCapabilityErrorCode.PROBE_FAILED,
                "Copilot capability state could not be prepared",
            ) from error
        environment = _discovery_environment()
        _probe_isolation_boundary(runtime, root)
        version = _probe_version(runtime, environment, workspace)
        _probe_sdk_protocol(runtime, environment, workspace, target)
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
    calls = 1
    action_visibility = "visible" if shell_action else "not-applicable"
    action_denial = "enforced"
    deviations = [
        "context is restored from the native evidence prefix without adding a user message",
        "Copilot custom instructions and skill retrieval are disabled; "
        "captured system guidance is restored exactly when available",
        "remote session sync and export are disabled",
        policy_deviation(target),
        f"runner policy {POLICY_VERSION}",
        f"credential-free structural discovery is filesystem-isolated and network-denied; "
        f"discovery policy {DISCOVERY_POLICY_VERSION}",
        f"structural discovery state is bounded to {_MAX_PROBE_STATE_BYTES // (1024 * 1024)} MiB "
        f"and {_MAX_PROBE_STATE_ENTRIES:,} entries",
        f"configuration {configuration.public_digest}",
        f"runtime executables {runtime.identity}",
        "native event and target behavior require live calibration before application selection",
    ]
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
        supported_context_operations=target_policy(target).context_operations,
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
    result: _CommandResult | None = None
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
            result = _run_probe(
                _bubblewrap_argv(
                    runtime,
                    state=probe_root,
                    worker=worker.name,
                    network_denied=True,
                ),
                _discovery_environment(),
                workspace,
                failure_code=CopilotCapabilityErrorCode.UNAVAILABLE,
                state_root=probe_root,
                probe_stage="isolation",
            )
            _verify_isolation_probe(workspace, protected_root)
    except CopilotCapabilityError as error:
        error.probe_stage = "isolation"
        if result is not None:
            _attach_result(error, result, "isolation")
        raise
    except (CaseError, CopilotIsolationError, OSError, ValueError) as error:
        refusal = CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
            probe_stage="isolation",
        )
        if result is not None:
            _attach_result(refusal, result, "isolation")
        raise refusal from error
    if (
        probe_root is None
        or protected_root is None
        or probe_root.exists()
        or protected_root.exists()
    ):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            "Copilot isolation runtime could not clean up its temporary state",
            probe_stage="isolation",
        )


def _prepare_isolation_probe(
    probe_root: Path,
    protected_root: Path,
) -> tuple[Path, Path]:
    workspace = _private_directory(probe_root / "workspace")
    for name in ("home", "copilot-home", "copilot-cache", "runner", "tmp"):
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


def _probe_version(runtime: CopilotRuntime, environment: dict[str, str], cwd: Path) -> str:
    result = _run_discovery(runtime, ("--version",), environment, cwd, probe_stage="version")
    try:
        text = result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot returned an invalid version",
            probe_stage="version",
            probe_exit_code=0,
            stdout=result.stdout,
            stderr=result.stderr,
        ) from error
    lines = text.splitlines()
    match = _VERSION.fullmatch(lines[0]) if lines else None
    if match is None:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot returned an unsupported version shape",
            probe_stage="version",
            probe_exit_code=0,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return match.group("version").removesuffix(".")


def _probe_sdk_protocol(
    runtime: CopilotRuntime,
    environment: dict[str, str],
    cwd: Path,
    target: CopilotTarget,
) -> None:
    """Prove public RPC compatibility through a credential-free SDK handshake."""
    source = f"""import asyncio
import sys
sys.path.insert(0, {f"{SANDBOX_ROOT}/runner/sdk-packages"!r})
from copilot import CopilotClient, RuntimeConnection
async def main():
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=sys.argv[2], args={list(target_policy(target).command_flags)!r}),
        mode="empty", base_directory={f"{SANDBOX_ROOT}/copilot-home"!r},
        working_directory={f"{SANDBOX_ROOT}/workspace"!r}, log_level="error")
    try:
        await client.start()
        result = await client.ping("ithrl-credential-free-protocol")
        if result.message != "pong: ithrl-credential-free-protocol" or not isinstance(result.protocol_version, int):
            raise ValueError("unsupported protocol")
    finally:
        await client.stop()
asyncio.run(main())
print("ithrl-copilot-sdk-protocol-ok")
""".encode()
    worker = cwd.parent / "protocol-worker.py"
    atomic_write_bytes(worker, source)
    result = _run_probe(
        _bubblewrap_argv(runtime, state=cwd.parent, worker=worker.name, network_denied=True),
        environment,
        cwd,
        state_root=cwd.parent,
        probe_stage="sdk-protocol",
        failure_code=CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
    )
    if result.stdout != b"ithrl-copilot-sdk-protocol-ok\n":
        error = CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Copilot SDK non-interactive protocol is unsupported",
        )
        _attach_result(error, result, "sdk-protocol")
        raise error


def _run_discovery(
    runtime: CopilotRuntime,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    *,
    probe_stage: str,
) -> _CommandResult:
    try:
        argv = _discovery_argv(runtime, state=cwd.parent, arguments=arguments)
    except (CopilotIsolationError, OSError, ValueError) as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
            probe_stage=probe_stage,
        ) from error
    return _run_probe(argv, environment, cwd, state_root=cwd.parent, probe_stage=probe_stage)


def _attach_result(error: CopilotCapabilityError, result: _CommandResult, stage: str) -> None:
    error.probe_stage = stage
    error.probe_exit_code = 0
    error.stdout = result.stdout[:_MAX_PROBE_OUTPUT_BYTES]
    error.stderr = result.stderr[:_MAX_PROBE_OUTPUT_BYTES]


def _run_probe(
    argv: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    *,
    failure_code: CopilotCapabilityErrorCode = CopilotCapabilityErrorCode.PROBE_FAILED,
    state_root: Path | None = None,
    probe_stage: str | None = None,
) -> _CommandResult:
    process: subprocess.Popen[bytes] | None = None
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        try:
            if state_root is not None:
                _require_bounded_probe_state(state_root)
            process = subprocess.Popen(  # noqa: S603 - validated executable and fixed arguments
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            returncode = _wait_for_probe(process, stdout, stderr, failure_code, state_root)
        except CopilotCapabilityError as error:
            refusal = error
            if process is not None and not _stop_probe_group(process):
                refusal = CopilotCapabilityError(
                    failure_code,
                    "Copilot capability probe processes could not be stopped",
                )
            _capture_probe_error(refusal, process, stdout, stderr, probe_stage)
            raise refusal from None
        except TimeoutError:
            message = "Copilot capability probe timed out"
            if process is not None and not _stop_probe_group(process):
                message = "Copilot capability probe processes could not be stopped"
            refusal = CopilotCapabilityError(failure_code, message)
            _capture_probe_error(refusal, process, stdout, stderr, probe_stage)
            raise refusal from None
        except OSError as error:
            refusal = CopilotCapabilityError(
                CopilotCapabilityErrorCode.UNAVAILABLE,
                "Copilot capability probe could not start",
            )
            if process is not None and not _stop_probe_group(process):
                refusal = CopilotCapabilityError(
                    failure_code,
                    "Copilot capability probe processes could not be stopped",
                )
            _capture_probe_error(refusal, process, stdout, stderr, probe_stage)
            raise refusal from error
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
            refusal = CopilotCapabilityError(failure_code, message)
            _capture_probe_error(refusal, process, stdout, stderr, probe_stage)
            raise refusal
        if returncode != 0:
            refusal = CopilotCapabilityError(failure_code, "Copilot capability probe failed")
            _capture_probe_error(refusal, process, stdout, stderr, probe_stage)
            raise refusal
        try:
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read(_MAX_PROBE_OUTPUT_BYTES)
            errors = stderr.read(_MAX_PROBE_OUTPUT_BYTES)
        except (OSError, ValueError) as error:
            refusal = CopilotCapabilityError(
                failure_code,
                "Copilot capability probe output could not be read",
            )
            _capture_probe_error(refusal, process, stdout, stderr, probe_stage)
            raise refusal from error
    return _CommandResult(stdout=output, stderr=errors)


def _capture_probe_error(
    error: CopilotCapabilityError,
    process: subprocess.Popen[bytes] | None,
    stdout: BinaryIO,
    stderr: BinaryIO,
    probe_stage: str | None,
) -> None:
    """Retain only bounded private bytes after the process termination attempt."""

    error.probe_stage = probe_stage
    error.probe_exit_code = process.returncode if process is not None else None
    for name, stream in (("stdout", stdout), ("stderr", stderr)):
        try:
            stream.seek(0)
            content = stream.read(_MAX_PROBE_OUTPUT_BYTES)
        except (OSError, ValueError):
            content = b""
        setattr(error, name, content)


def _wait_for_probe(
    process: subprocess.Popen[bytes],
    stdout: BinaryIO,
    stderr: BinaryIO,
    failure_code: CopilotCapabilityErrorCode,
    state_root: Path | None,
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
        if state_root is not None:
            _require_bounded_probe_state(state_root)
        time.sleep(_PROBE_POLL_SECONDS)
    if stdout.tell() > _MAX_PROBE_OUTPUT_BYTES or stderr.tell() > _MAX_PROBE_OUTPUT_BYTES:
        raise CopilotCapabilityError(
            failure_code,
            "Copilot capability probe exceeded its output limit",
        )
    if state_root is not None:
        _require_bounded_probe_state(state_root)
    return process.wait()


def _require_bounded_probe_state(root: Path) -> None:
    """Inspect bounded disposable state using no-follow directory descriptors."""

    entries = 0
    size = 0
    files: list[tuple[str, int]] = []
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.UNAVAILABLE,
            "Copilot capability state inspection is unavailable",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def inspect(descriptor: int, depth: int, relative: Path) -> None:
        nonlocal entries, size
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                entries += 1
                if entries > _MAX_PROBE_STATE_ENTRIES:
                    raise CopilotCapabilityError(
                        CopilotCapabilityErrorCode.PROBE_FAILED,
                        "Copilot capability state exceeded its entry limit",
                    )
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A running SDK can remove its own temporary state.
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= _MAX_PROBE_STATE_DEPTH:
                        raise CopilotCapabilityError(
                            CopilotCapabilityErrorCode.PROBE_FAILED,
                            "Copilot capability state exceeded its nesting limit",
                        )
                    try:
                        child = os.open(entry.name, flags, dir_fd=descriptor)
                    except FileNotFoundError:
                        continue
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                            raise CopilotCapabilityError(
                                CopilotCapabilityErrorCode.PROBE_FAILED,
                                "Copilot capability state changed during inspection",
                            )
                        inspect(child, depth + 1, relative / entry.name)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(metadata.st_mode):
                    size += metadata.st_size
                    files.append(((relative / entry.name).as_posix(), metadata.st_size))
                    if size > _MAX_PROBE_STATE_BYTES:
                        raise CopilotCapabilityError(
                            CopilotCapabilityErrorCode.PROBE_FAILED,
                            "Copilot capability state exceeded its byte limit",
                            state_limit_details={
                                "limit_bytes": _MAX_PROBE_STATE_BYTES,
                                "accounted_bytes": size,
                                "file_path": (relative / entry.name).as_posix(),
                                "file_bytes": metadata.st_size,
                            },
                            state_files=tuple(sorted(files, key=itemgetter(1), reverse=True)[:16]),
                        )
                else:
                    raise CopilotCapabilityError(
                        CopilotCapabilityErrorCode.PROBE_FAILED,
                        "Copilot capability state contains an unsafe entry",
                    )

    try:
        descriptor = os.open(root, flags)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise CopilotCapabilityError(
                    CopilotCapabilityErrorCode.PROBE_FAILED,
                    "Copilot capability state lost its private permissions",
                )
            inspect(descriptor, 0, Path())
        finally:
            os.close(descriptor)
    except OSError as error:
        raise CopilotCapabilityError(
            CopilotCapabilityErrorCode.PROBE_FAILED,
            "Copilot capability state could not be inspected",
        ) from error


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


def _discovery_environment() -> dict[str, str]:
    return {
        **_worker_environment({}),
        "TMPDIR": f"{SANDBOX_ROOT}/tmp",  # noqa: S108 - bounded private state
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
