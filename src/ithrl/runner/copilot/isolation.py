"""Prepare a GitHub Copilot worker inside a restricted Bubblewrap process."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ithrl.case import CaseError
from ithrl.case.filesystem import atomic_write_bytes
from ithrl.context import content_digest
from ithrl.runner.copilot.configuration import MAX_CONFIG_BYTES, CopilotConfiguration
from ithrl.runner.copilot.policy import CopilotTarget, target_policy
from ithrl.runner.copilot.sdk_runtime import SDKRuntime
from ithrl.runner.runtime import OwnedAttemptState, ProcessSpec
from ithrl.runner.runtime.certificates import snapshot_certificate_files

SANDBOX_ROOT = "/tmp/ithrl-state"  # noqa: S108 - private tmpfs path inside Bubblewrap
MAX_NATIVE_SESSION_BYTES = 64 * 1024 * 1024
# Bundled tooling is extracted into each isolated cache before even --version.
# Reserve room for those assets as well as the bounded semantic session and output.
DEFAULT_DISPOSABLE_STATE_BYTES = 256 * 1024 * 1024
_MAX_WORKER_BYTES = 1024 * 1024
_MAX_MODEL_BYTES = 1024


class CopilotIsolationError(RuntimeError):
    """A preventive Copilot process boundary cannot be prepared."""


@dataclass(frozen=True, slots=True)
class CopilotRuntime:
    """Content-bound Copilot and Bubblewrap executables."""

    copilot: Path = field(repr=False)
    bubblewrap: Path = field(repr=False)
    sdk: SDKRuntime = field(default_factory=SDKRuntime, repr=False)
    _digests: tuple[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        programs = tuple(_read_executable(path) for path in (self.copilot, self.bubblewrap))
        object.__setattr__(self, "copilot", programs[0][0])
        object.__setattr__(self, "bubblewrap", programs[1][0])
        object.__setattr__(self, "_digests", (programs[0][1], programs[1][1]))

    @property
    def identity(self) -> str:
        """Return a credential-free identity for the selected executables."""

        return content_digest(
            {
                "bubblewrap": self._digests[1],
                "copilot": self._digests[0],
                "sdk": self.sdk.identity,
            }
        )

    def assert_unchanged(self) -> None:
        """Refuse when either selected executable changed after detection."""

        current = tuple(_read_executable(path)[1] for path in (self.copilot, self.bubblewrap))
        try:
            self.sdk.assert_unchanged()
        except (OSError, ValueError) as error:
            raise CopilotIsolationError("the selected Copilot SDK runtime changed") from error
        if current != self._digests:
            raise CopilotIsolationError("a required Copilot runtime executable changed")


def prepare_copilot_process(
    state: OwnedAttemptState,
    *,
    runtime: CopilotRuntime,
    configuration: CopilotConfiguration,
    native_session: bytes,
    session_id: str,
    system_message: str | None,
    model: str,
    target: CopilotTarget,
    worker_source: Path,
    worker_digest: str,
) -> ProcessSpec:
    """Write private worker inputs and return one direct Bubblewrap invocation."""

    state.assert_unchanged()
    runtime.assert_unchanged()
    _validate_private_input(
        native_session, session_id=session_id, system_message=system_message, model=model
    )
    policy = target_policy(target)

    workspace = _private_directory(state.root / "workspace")
    copilot_home = _private_directory(state.root / "copilot-home")
    _private_directory(state.root / "copilot-cache")
    _private_directory(state.root / "home")
    _private_directory(state.root / "runner")
    worker = _copy_private_program(
        worker_source,
        state.root / "worker",
        expected_digest=worker_digest,
    )
    request = _worker_request(
        session_id=session_id,
        system_message=system_message,
        model=model,
        target=target,
        command_flags=policy.command_flags,
        secret_environment_names=configuration.environment_names,
    )
    try:
        session_state = _private_directory(copilot_home / "session-state")
        session_directory = _private_directory(session_state / session_id)
        atomic_write_bytes(session_directory / "events.jsonl", native_session)
        atomic_write_bytes(state.root / "request.json", request)
        atomic_write_bytes(copilot_home / "settings.json", configuration.settings_data)
        if configuration.config_data is not None:
            atomic_write_bytes(copilot_home / "config.json", configuration.config_data)
    except CaseError as error:
        raise CopilotIsolationError("cannot prepare private Copilot attempt state") from error

    try:
        environment = snapshot_certificate_files(
            state.root, configuration.environment, sandbox_root=SANDBOX_ROOT
        )
    except ValueError as error:
        raise CopilotIsolationError("cannot snapshot a configured Copilot CA bundle") from error

    return ProcessSpec(
        argv=_bubblewrap_argv(runtime, state=state.root, worker=worker.name),
        cwd=workspace,
        environment=_worker_environment(environment),
    )


def _validate_private_input(
    native_session: bytes, *, session_id: str, system_message: str | None, model: str
) -> None:
    import re

    if not native_session or len(native_session) > MAX_NATIVE_SESSION_BYTES:
        raise CopilotIsolationError("Copilot native session exceeds its byte limit")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", session_id):
        raise CopilotIsolationError("Copilot native session identity is invalid")
    try:
        model_size = len(model.encode("utf-8"))
        system_size = 0 if system_message is None else len(system_message.encode("utf-8"))
    except UnicodeError as error:
        raise CopilotIsolationError("Copilot worker input is invalid") from error
    if system_size > MAX_CONFIG_BYTES or (
        system_message is not None and (not system_message or "\0" in system_message)
    ):
        raise CopilotIsolationError("Copilot system message exceeds its byte limit")
    if not model or model_size > _MAX_MODEL_BYTES or "\0" in model:
        raise CopilotIsolationError("Copilot model identity is invalid")


def _worker_request(
    *,
    session_id: str,
    system_message: str | None,
    model: str,
    target: CopilotTarget,
    command_flags: tuple[str, ...],
    secret_environment_names: tuple[str, ...],
) -> bytes:
    flags = list(command_flags)
    if secret_environment_names:
        flags.append(f"--secret-env-vars={','.join(secret_environment_names)}")
    document = {
        "commandFlags": flags,
        "model": model,
        "nativeSession": f"{SANDBOX_ROOT}/copilot-home/session-state/{session_id}/events.jsonl",
        "sessionId": session_id,
        "systemMessage": system_message,
        "target": target,
        "workspace": f"{SANDBOX_ROOT}/workspace",
    }
    try:
        return json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise CopilotIsolationError("Copilot worker input is invalid") from error


def _bubblewrap_argv(
    runtime: CopilotRuntime,
    *,
    state: Path,
    worker: str,
    network_denied: bool = False,
) -> tuple[str, ...]:
    return (
        *_boundary_argv(runtime, state=state, network_denied=network_denied),
        *runtime.sdk.mounts(SANDBOX_ROOT),
        "--setenv",
        "PYTHONHOME",
        f"{SANDBOX_ROOT}/runner/python-runtime",
        f"{SANDBOX_ROOT}/runner/python-runtime/bin/python",
        "-X",
        f"pycache_prefix={SANDBOX_ROOT}/runner/bytecode-cache",
        "-S",
        "-P",
        f"{SANDBOX_ROOT}/{worker}",
    )


def _discovery_argv(
    runtime: CopilotRuntime,
    *,
    state: Path,
    arguments: tuple[str, ...],
) -> tuple[str, ...]:
    """Run a structural command without writable host state or network access."""

    runtime.assert_unchanged()
    return (
        *_boundary_argv(runtime, state=state, network_denied=True),
        f"{SANDBOX_ROOT}/runner/copilot",
        *arguments,
    )


def _boundary_argv(
    runtime: CopilotRuntime,
    *,
    state: Path,
    network_denied: bool,
) -> tuple[str, ...]:
    copilot = runtime.copilot.resolve(strict=True)
    return (
        os.fspath(runtime.bubblewrap.resolve(strict=True)),
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        *(("--unshare-net",) if network_denied else ()),
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/root",
        "--tmpfs",
        "/tmp",  # noqa: S108 - private tmpfs inside Bubblewrap
        "--tmpfs",
        "/run",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--dir",
        SANDBOX_ROOT,
        "--bind",
        os.fspath(state),
        SANDBOX_ROOT,
        "--ro-bind",
        os.fspath(copilot),
        f"{SANDBOX_ROOT}/runner/copilot",
        *(
            (
                "--remount-ro",
                "/home",
                "--remount-ro",
                "/root",
                "--remount-ro",
                "/tmp",  # noqa: S108 - private tmpfs inside Bubblewrap
                "--remount-ro",
                "/run",
            )
            if network_denied
            else ()
        ),
        "--chdir",
        f"{SANDBOX_ROOT}/workspace",
    )


def _worker_environment(supplied: Mapping[str, str]) -> dict[str, str]:
    environment = dict(supplied)
    environment.update(
        {
            "COPILOT_AUTO_UPDATE": "false",
            "COPILOT_CACHE_HOME": f"{SANDBOX_ROOT}/copilot-cache",
            "COPILOT_HOME": f"{SANDBOX_ROOT}/copilot-home",
            "HOME": f"{SANDBOX_ROOT}/home",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": "/usr/bin:/bin",
            "LD_LIBRARY_PATH": f"{SANDBOX_ROOT}/runner/python-runtime/lib",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "COPILOT_CLI_DISABLE_WEBSOCKET_RESPONSES": "true",
        }
    )
    return environment


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
        metadata = path.lstat()
    except OSError as error:
        raise CopilotIsolationError("cannot prepare private Copilot attempt state") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CopilotIsolationError("Copilot attempt state contains an unsafe directory")
    return path


def program_digest(source: Path) -> str:
    """Return the content identity of one bounded Copilot worker."""

    _, content = _read_program(source)
    return hashlib.sha256(content).hexdigest()


def _copy_private_program(source: Path, destination: Path, *, expected_digest: str) -> Path:
    try:
        _, content = _read_program(source)
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise CopilotIsolationError("the Copilot worker source changed")
        atomic_write_bytes(destination, content)
        destination.chmod(0o500)
    except CopilotIsolationError:
        raise
    except (CaseError, OSError) as error:
        raise CopilotIsolationError("cannot prepare the private Copilot worker") from error
    return destination


def _read_program(source: Path) -> tuple[Path, bytes]:
    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_WORKER_BYTES:
            raise CopilotIsolationError("the Copilot worker source is invalid")
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_state(opened) != _file_state(metadata):
                raise CopilotIsolationError("the Copilot worker source changed while opening")
            content = stream.read(_MAX_WORKER_BYTES + 1)
            finished = os.fstat(stream.fileno())
        if (
            len(content) != metadata.st_size
            or _file_state(finished) != _file_state(metadata)
            or _file_state(resolved.stat()) != _file_state(metadata)
        ):
            raise CopilotIsolationError("the Copilot worker source changed while it was read")
    except CopilotIsolationError:
        raise
    except OSError as error:
        raise CopilotIsolationError("cannot read the private Copilot worker") from error
    return resolved, content


def _read_executable(path: Path) -> tuple[Path, str]:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("Copilot runtime paths must be absolute")
    try:
        target = path.resolve(strict=True)
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
            raise OSError
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_state(opened) != _file_state(metadata):
                raise CopilotIsolationError("a required Copilot runtime executable changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            finished = os.fstat(stream.fileno())
        if _file_state(finished) != _file_state(metadata) or _file_state(
            target.stat()
        ) != _file_state(metadata):
            raise CopilotIsolationError("a required Copilot runtime executable changed")
    except CopilotIsolationError:
        raise
    except OSError as error:
        raise CopilotIsolationError(
            "a required Copilot runtime executable is unavailable"
        ) from error
    return target, digest.hexdigest()


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


__all__ = [
    "MAX_NATIVE_SESSION_BYTES",
    "SANDBOX_ROOT",
    "CopilotIsolationError",
    "CopilotRuntime",
    "prepare_copilot_process",
    "program_digest",
]
