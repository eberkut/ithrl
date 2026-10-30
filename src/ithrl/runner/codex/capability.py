"""Detect a supported Codex final-response execution profile without model calls."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import time
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import BinaryIO, Protocol

from ithrl.case import CaseError
from ithrl.case.filesystem import atomic_write_bytes
from ithrl.execution import CapabilityProfile
from ithrl.runner.codex.isolation import (
    CodexIsolationError,
    CodexRuntime,
    _bubblewrap_argv,
    _bubblewrap_boundary,
    _memory_file,
    _worker_environment,
)
from ithrl.runner.codex.policy import (
    CAPTURE_TYPE,
    CONTEXT_OPERATIONS,
    DISABLED_FEATURES,
    MATERIALIZATION,
    POLICY_VERSION,
    RECONSTRUCTION_METHOD,
    RUNNER_VERSION,
    SOURCE_SCHEMA,
    TARGET_TYPE,
    configuration_deviation,
    runtime_deviation,
)
from ithrl.runner.codex.protocol import SANDBOX_ROOT
from ithrl.runner.codex.protocol_contracts import MAX_INFERENCE_REQUESTS
from ithrl.runner.codex.runtime_configuration import validate_runner_configuration
from ithrl.runner.codex.schema import CodexSchemaError, verify_schema_bundle
from ithrl.runner.runtime import PROCESS_CANCELLATION_GRACE_SECONDS

_PROBE_TIMEOUT_SECONDS = 15
_MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
_MAX_SCHEMA_ENTRIES = 2_000
_MAX_SCHEMA_TOTAL_BYTES = 64 * 1024 * 1024
_PROBE_POLL_SECONDS = 0.05
_PROBE_TERMINATION_GRACE_SECONDS = 1.0
_VERSION = re.compile(r"^codex-cli (?P<version>[^\s]+)$")
_ISOLATION_PROBE_RESULT = b"ithrl-isolation-ok\n"
_ISOLATION_HOST_MARKER = b"ithrl-readable-host-marker\n"
_ISOLATION_AUTH_MARKER = b'{"ithrl_probe":"not-a-credential"}\n'
_ISOLATION_UNAVAILABLE = "Codex isolation runtime is unavailable or incompatible"
_ISOLATION_HOST_STORAGE_UNAVAILABLE = (
    "Codex isolation probe requires writable /var/tmp temporary storage"
)
_ISOLATION_HOST_PARENT = Path("/var/tmp")  # noqa: S108 - visible through the read-only root
_ISOLATION_PROBE_SOURCE = f"""\
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
protected = Path(request["protected"])
if (protected / "readable-marker").read_bytes() != {_ISOLATION_HOST_MARKER!r}:
    raise SystemExit(69)
auth = Path(sys.argv[1]).parent / "codex-home" / "auth.json"
if auth.read_bytes() != {_ISOLATION_AUTH_MARKER!r}:
    raise SystemExit(68)
try:
    auth.write_bytes(b"unsafe")
except OSError:
    pass
else:
    raise SystemExit(67)
try:
    (protected / "unexpected-write").write_bytes(b"unsafe")
except OSError:
    pass
else:
    raise SystemExit(70)

Path(sys.argv[1]).parent.joinpath("workspace", "isolation-probe.txt").write_bytes(
    {_ISOLATION_PROBE_RESULT!r}
)
""".encode()


class CodexConfigurationContract(Protocol):
    """Credential-free configuration facts needed by capability detection."""

    @property
    def source_provider_id(self) -> str: ...

    @property
    def endpoint(self) -> str: ...

    @property
    def environment_names(self) -> tuple[str, ...]: ...

    @property
    def public_digest(self) -> str: ...

    @property
    def rendered_config(self) -> bytes: ...


class CodexCapabilityErrorCode(StrEnum):
    """Stable capability-probe refusal categories."""

    UNAVAILABLE = "unavailable"
    PROBE_FAILED = "probe-failed"
    UNSUPPORTED_PROTOCOL = "unsupported-protocol"
    UNSAFE_POLICY = "unsafe-policy"


class CodexCapabilityError(RuntimeError):
    """The installed Codex runtime cannot satisfy the runner contract."""

    def __init__(self, code: CodexCapabilityErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def detect_codex_capability(
    runtime: CodexRuntime,
    configuration: CodexConfigurationContract,
    *,
    max_provider_calls_per_attempt: int = MAX_INFERENCE_REQUESTS,
) -> CapabilityProfile:
    """Probe local Codex structure and return one immutable supported profile."""

    if (
        type(max_provider_calls_per_attempt) is not int
        or not 1 <= max_provider_calls_per_attempt <= MAX_INFERENCE_REQUESTS
    ):
        raise ValueError("Codex per-attempt provider-call limit must be an integer from 1 to 16")
    _require_runtime_unchanged(runtime)
    try:
        validate_runner_configuration(
            configuration.rendered_config,
            endpoint=configuration.endpoint,
            environment_names=configuration.environment_names,
        )
    except ValueError as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNSAFE_POLICY,
            "Codex runner configuration does not satisfy the safety policy",
        ) from error
    with TemporaryDirectory(prefix="ithrl-codex-capability-") as temporary:
        root = Path(temporary)
        codex_home = _private_directory(root / "codex-home")
        home = _private_directory(root / "home")
        workspace = _private_directory(root / "workspace")
        schema = _private_directory(root / "schema")
        _private_directory(root / "runner")
        _private_directory(root / "tmp")
        try:
            atomic_write_bytes(codex_home / "config.toml", configuration.rendered_config)
        except CaseError as error:
            raise CodexCapabilityError(
                CodexCapabilityErrorCode.PROBE_FAILED,
                "Codex capability configuration could not be prepared",
            ) from error

        environment = _probe_environment(
            codex_home,
            home,
            configuration.environment_names,
        )
        _probe_isolation_boundary(runtime, root)
        version = _probe_version(runtime, environment, workspace)
        _verify_policy(runtime, environment, workspace)
        _generate_schema(runtime, environment, workspace)
        _require_bounded_schema(schema)
        try:
            protocol_digest = verify_schema_bundle(schema)
        except CodexSchemaError as error:
            raise CodexCapabilityError(
                CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
                str(error),
            ) from error
        _require_runtime_unchanged(runtime)

    return CapabilityProfile(
        runner_type="codex",
        runner_version=RUNNER_VERSION,
        harness="codex",
        runtime_harness_version=version,
        protocol_version=f"app-server-schema-{protocol_digest}",
        source_schema=SOURCE_SCHEMA,
        deterministic=False,
        supported_capture_types=(CAPTURE_TYPE,),
        supported_materializations=(MATERIALIZATION,),
        supported_context_operations=CONTEXT_OPERATIONS,
        reconstruction_method=RECONSTRUCTION_METHOD,
        supported_targets=(TARGET_TYPE,),
        provider_transport="required",
        provider_calls_per_attempt=max_provider_calls_per_attempt,
        tool_network_access="forbidden",
        action_visibility="not-applicable",
        action_denial="enforced",
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
        reports_usage=True,
        input_token_limit_enforcement="unavailable",
        provider=configuration.source_provider_id,
        endpoint=configuration.endpoint,
        deviations=(
            "context is reconstructed through an ephemeral app-server thread",
            "provider request and stream retries are disabled",
            "a fixed-destination Responses relay enforces the selected administrative "
            f"limit of {max_provider_calls_per_attempt} requests per attempt",
            "all model-returned actions are rejected",
            "structural discovery is host-write isolated and network denied",
            f"runner policy {POLICY_VERSION}",
            configuration_deviation(configuration.public_digest),
            runtime_deviation(runtime.identity),
        ),
    )


def _require_runtime_unchanged(runtime: CodexRuntime) -> None:
    try:
        runtime.assert_unchanged()
    except (OSError, RuntimeError) as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            "Codex runtime changed during capability detection",
        ) from error


def _probe_isolation_boundary(runtime: CodexRuntime, root: Path) -> None:
    probe_root: Path | None = None
    protected_root: Path | None = None
    try:
        with TemporaryDirectory(prefix="isolation-", dir=root) as temporary:
            try:
                protected_directory = TemporaryDirectory(
                    prefix="ithrl-codex-protected-",
                    dir=_ISOLATION_HOST_PARENT,
                )
            except OSError as error:
                raise CodexCapabilityError(
                    CodexCapabilityErrorCode.UNAVAILABLE,
                    _ISOLATION_HOST_STORAGE_UNAVAILABLE,
                ) from error
            with protected_directory as protected_temporary:
                probe_root = Path(temporary)
                protected_root = Path(protected_temporary)
                workspace, worker, request = _prepare_isolation_probe(
                    probe_root,
                    protected_root,
                )
                _run_isolation_probe(runtime, probe_root, workspace, worker, request)
                _verify_isolation_probe(workspace, protected_root)
    except CodexCapabilityError:
        raise
    except (CaseError, CodexIsolationError, OSError, ValueError) as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
        ) from error
    if not _isolation_probe_cleaned(probe_root, protected_root):
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            "Codex isolation runtime could not clean up its temporary state",
        )


def _prepare_isolation_probe(
    probe_root: Path,
    protected_root: Path,
) -> tuple[Path, Path, Path]:
    workspace = _private_directory(probe_root / "workspace")
    _private_directory(probe_root / "home")
    _private_directory(probe_root / "codex-home")
    _private_directory(probe_root / "runner")
    worker = probe_root / "isolation_probe.py"
    request = probe_root / "isolation_request.json"
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
    return workspace, worker, request


def _run_isolation_probe(
    runtime: CodexRuntime,
    probe_root: Path,
    workspace: Path,
    worker: Path,
    request: Path,
) -> None:
    auth_descriptor = _memory_file(_ISOLATION_AUTH_MARKER)
    if auth_descriptor is None:
        raise CodexIsolationError("cannot prepare probe authentication")
    try:
        argv = _bubblewrap_argv(
            runtime,
            state=probe_root,
            worker=worker.name,
            request=request.name,
            auth_descriptor=auth_descriptor,
        )
        _run_probe(
            argv,
            _worker_environment({}),
            workspace,
            failure_code=CodexCapabilityErrorCode.UNAVAILABLE,
            inherited_fds=(auth_descriptor,),
        )
    except CodexCapabilityError as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
        ) from error
    finally:
        os.close(auth_descriptor)


def _verify_isolation_probe(workspace: Path, protected_root: Path) -> None:
    if (
        (workspace / "isolation-probe.txt").read_bytes() != _ISOLATION_PROBE_RESULT
        or _probe_authentication_was_copied(workspace.parent / "codex-home" / "auth.json")
        or (protected_root / "readable-marker").read_bytes() != _ISOLATION_HOST_MARKER
        or (protected_root / "unexpected-write").exists()
    ):
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
        )


def _probe_authentication_was_copied(auth_path: Path) -> bool:
    return auth_path.exists() and auth_path.stat().st_size != 0


def _isolation_probe_cleaned(
    probe_root: Path | None,
    protected_root: Path | None,
) -> bool:
    return (
        probe_root is not None
        and protected_root is not None
        and not probe_root.exists()
        and not protected_root.exists()
    )


def _probe_version(runtime: CodexRuntime, environment: Mapping[str, str], cwd: Path) -> str:
    result = _run_structural_probe(runtime, ("--version",), environment, cwd)
    try:
        match = _VERSION.fullmatch(result.stdout.decode("utf-8", errors="strict").strip())
    except UnicodeDecodeError:
        match = None
    if match is None:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            "Codex reported an unsupported version string",
        )
    return match.group("version")


def _verify_policy(runtime: CodexRuntime, environment: Mapping[str, str], cwd: Path) -> None:
    result = _run_structural_probe(
        runtime,
        ("features", "list"),
        environment,
        cwd,
        failure_code=CodexCapabilityErrorCode.UNSAFE_POLICY,
    )
    try:
        lines = result.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNSAFE_POLICY,
            "Codex feature policy output is invalid",
        ) from error
    effective: dict[str, str] = {}
    for line in lines:
        fields = line.split()
        if len(fields) >= 3:
            effective[fields[0]] = fields[-1]
    if any(effective.get(feature) != "false" for feature in DISABLED_FEATURES):
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNSAFE_POLICY,
            "Codex cannot prove that every runner tool surface is disabled",
        )


def _generate_schema(
    runtime: CodexRuntime,
    environment: Mapping[str, str],
    cwd: Path,
) -> None:
    _run_structural_probe(
        runtime,
        (
            "app-server",
            "generate-json-schema",
            "--out",
            f"{SANDBOX_ROOT}/schema",
        ),
        environment,
        cwd,
        failure_code=CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
    )


def _run_structural_probe(
    runtime: CodexRuntime,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
    cwd: Path,
    *,
    failure_code: CodexCapabilityErrorCode = CodexCapabilityErrorCode.UNAVAILABLE,
) -> subprocess.CompletedProcess[bytes]:
    """Run credential-free CLI introspection inside a preventive host boundary."""

    try:
        argv = (
            *_bubblewrap_boundary(runtime, state=cwd.parent),
            "--remount-ro",
            "/home",
            "--remount-ro",
            "/root",
            "--remount-ro",
            "/tmp",  # noqa: S108 - discovery scratch must stay in bounded owned state
            "--remount-ro",
            "/run",
            "--unshare-net",
            "--chdir",
            f"{SANDBOX_ROOT}/workspace",
            f"{SANDBOX_ROOT}/runner/codex",
            *arguments,
        )
    except (CodexIsolationError, OSError, ValueError) as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNAVAILABLE,
            _ISOLATION_UNAVAILABLE,
        ) from error
    isolated_environment = dict(environment)
    isolated_environment.update(
        HOME=f"{SANDBOX_ROOT}/home",
        CODEX_HOME=f"{SANDBOX_ROOT}/codex-home",
        TMPDIR=f"{SANDBOX_ROOT}/tmp",  # noqa: S108 - bounded private discovery scratch
    )
    return _run_probe(
        argv,
        isolated_environment,
        cwd,
        failure_code=failure_code,
        state_root=cwd.parent,
    )


def _run_probe(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    cwd: Path,
    *,
    failure_code: CodexCapabilityErrorCode = CodexCapabilityErrorCode.UNAVAILABLE,
    state_root: Path | None = None,
    inherited_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    process: subprocess.Popen[bytes] | None = None
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(  # noqa: S603 - validated absolute executable; no shell
                argv,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                pass_fds=inherited_fds,
                start_new_session=True,
            )
            returncode = _wait_for_probe(
                process,
                stdout,
                stderr,
                failure_code=failure_code,
                state_root=state_root,
            )
        except CodexCapabilityError:
            if process is not None and not _stop_probe_group(process):
                raise CodexCapabilityError(
                    failure_code,
                    "Codex capability probe processes could not be stopped",
                ) from None
            raise
        except TimeoutError:
            if process is not None and not _stop_probe_group(process):
                raise CodexCapabilityError(
                    failure_code,
                    "Codex capability probe processes could not be stopped",
                ) from None
            raise CodexCapabilityError(
                failure_code, "Codex capability probe did not pass"
            ) from None
        except OSError as error:
            if process is not None and not _stop_probe_group(process):
                raise CodexCapabilityError(
                    failure_code,
                    "Codex capability probe processes could not be stopped",
                ) from error
            raise CodexCapabilityError(
                failure_code, "Codex capability probe could not complete"
            ) from error
        except BaseException:
            if process is not None:
                _stop_probe_group(process)
            raise
        if _probe_group_exists(process.pid):
            stopped = _stop_probe_group(process)
            message = (
                "Codex capability probe left a descendant process"
                if stopped
                else "Codex capability probe processes could not be stopped"
            )
            raise CodexCapabilityError(failure_code, message)
        stdout.seek(0)
        stderr.seek(0)
        result = subprocess.CompletedProcess(
            argv,
            returncode,
            stdout.read(_MAX_PROBE_OUTPUT_BYTES + 1),
            stderr.read(_MAX_PROBE_OUTPUT_BYTES + 1),
        )
    if result.returncode != 0:
        raise CodexCapabilityError(failure_code, "Codex capability probe did not pass")
    return result


def _wait_for_probe(
    process: subprocess.Popen[bytes],
    stdout: BinaryIO,
    stderr: BinaryIO,
    *,
    failure_code: CodexCapabilityErrorCode,
    state_root: Path | None,
) -> int:
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    while process.poll() is None:
        if time.monotonic() >= deadline:
            raise TimeoutError
        _require_bounded_probe_output(stdout.tell(), stderr.tell(), failure_code)
        if state_root is not None:
            _require_bounded_discovery_state(state_root)
        time.sleep(_PROBE_POLL_SECONDS)
    _require_bounded_probe_output(stdout.tell(), stderr.tell(), failure_code)
    if state_root is not None:
        _require_bounded_discovery_state(state_root)
    return process.wait()


def _require_bounded_probe_output(
    stdout_bytes: int,
    stderr_bytes: int,
    failure_code: CodexCapabilityErrorCode,
) -> None:
    if stdout_bytes > _MAX_PROBE_OUTPUT_BYTES or stderr_bytes > _MAX_PROBE_OUTPUT_BYTES:
        raise CodexCapabilityError(
            failure_code,
            "Codex capability probe output exceeds the byte limit",
        )


def _stop_probe_group(process: subprocess.Popen[bytes]) -> bool:
    """Stop every process in one probe group and confirm that the group is gone."""

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


def _probe_environment(
    codex_home: Path,
    home: Path,
    required_environment: tuple[str, ...],
) -> dict[str, str]:
    environment = {
        "CODEX_HOME": os.fspath(codex_home),
        "HOME": os.fspath(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    environment.update((name, "ithrl-capability-probe") for name in required_environment)
    return environment


def _require_bounded_schema(root: Path) -> None:
    _require_bounded_tree(root, allow_symlinks=False)


def _require_bounded_discovery_state(root: Path) -> None:
    _require_bounded_tree(root, allow_symlinks=True)


def _require_bounded_tree(root: Path, *, allow_symlinks: bool) -> None:
    entries = 0
    size = 0
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise CodexCapabilityError(
                CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
                "Codex capability state root is not a directory",
            )
        for directory, names, files in root.walk(
            top_down=True,
            on_error=_raise_walk_error,
            follow_symlinks=False,
        ):
            entries += len(names) + len(files)
            if entries > _MAX_SCHEMA_ENTRIES:
                raise CodexCapabilityError(
                    CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
                    "Codex app-server schema exceeds the file limit",
                )
            for name in (*names, *files):
                path = directory / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    if not allow_symlinks:
                        raise CodexCapabilityError(
                            CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
                            "Codex app-server schema contains a symbolic link",
                        )
                elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    raise CodexCapabilityError(
                        CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
                        "Codex capability state contains an unsupported file type",
                    )
                size += metadata.st_size if not stat.S_ISDIR(metadata.st_mode) else 0
                if size > _MAX_SCHEMA_TOTAL_BYTES:
                    raise CodexCapabilityError(
                        CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
                        "Codex app-server schema exceeds the byte limit",
                    )
    except OSError as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.UNSUPPORTED_PROTOCOL,
            "Codex app-server schema could not be inspected",
        ) from error


def _raise_walk_error(error: OSError) -> None:
    raise error


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
        metadata = path.lstat()
    except OSError as error:
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.PROBE_FAILED,
            "Codex capability state could not be prepared",
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CodexCapabilityError(
            CodexCapabilityErrorCode.PROBE_FAILED,
            "Codex capability state is unsafe",
        )
    return path
