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
from ithrl.runner.copilot.configuration import CopilotConfiguration
from ithrl.runner.copilot.policy import CopilotTarget, target_policy
from ithrl.runner.copilot.protocol import is_denied_tool_completion
from ithrl.runner.runtime import OwnedAttemptState, ProcessSpec

SANDBOX_ROOT = "/tmp/ithrl-state"  # noqa: S108 - private tmpfs path inside Bubblewrap
MAX_SEMANTIC_SESSION_BYTES = 64 * 1024 * 1024
MAX_TRIGGER_BYTES = 1024 * 1024
_MAX_WORKER_BYTES = 1024 * 1024
_MAX_MODEL_BYTES = 1024


class CopilotIsolationError(RuntimeError):
    """A preventive Copilot process boundary cannot be prepared."""


@dataclass(frozen=True, slots=True)
class CopilotRuntime:
    """Content-bound Copilot and Bubblewrap executables."""

    copilot: Path = field(repr=False)
    bubblewrap: Path = field(repr=False)
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
            }
        )

    def assert_unchanged(self) -> None:
        """Refuse when either selected executable changed after detection."""

        current = tuple(_read_executable(path)[1] for path in (self.copilot, self.bubblewrap))
        if current != self._digests:
            raise CopilotIsolationError("a required Copilot runtime executable changed")


def prepare_copilot_process(
    state: OwnedAttemptState,
    *,
    runtime: CopilotRuntime,
    configuration: CopilotConfiguration,
    semantic_session: bytes,
    trigger: str,
    model: str,
    target: CopilotTarget,
    worker_source: Path,
    worker_digest: str,
) -> ProcessSpec:
    """Write private worker inputs and return one direct Bubblewrap invocation."""

    state.assert_unchanged()
    runtime.assert_unchanged()
    _validate_private_input(semantic_session, trigger=trigger, model=model)
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
        trigger=trigger,
        model=model,
        target=target,
        command_flags=policy.command_flags,
        secret_environment_names=configuration.environment_names,
    )
    try:
        atomic_write_bytes(state.root / "session.jsonl", semantic_session)
        atomic_write_bytes(state.root / "request.json", request)
        atomic_write_bytes(copilot_home / "settings.json", configuration.settings_data)
        if configuration.config_data is not None:
            atomic_write_bytes(copilot_home / "config.json", configuration.config_data)
    except CaseError as error:
        raise CopilotIsolationError("cannot prepare private Copilot attempt state") from error

    return ProcessSpec(
        argv=_bubblewrap_argv(runtime, state=state.root, worker=worker.name),
        cwd=workspace,
        environment=_worker_environment(configuration.environment),
        stop_after_stdout_line=(is_denied_tool_completion if target == "shell-action" else None),
    )


def _validate_private_input(semantic_session: bytes, *, trigger: str, model: str) -> None:
    if not semantic_session or len(semantic_session) > MAX_SEMANTIC_SESSION_BYTES:
        raise CopilotIsolationError("Copilot semantic session exceeds its byte limit")
    try:
        trigger_size = len(trigger.encode("utf-8"))
        model_size = len(model.encode("utf-8"))
    except UnicodeError as error:
        raise CopilotIsolationError("Copilot worker input is invalid") from error
    if not trigger or trigger_size > MAX_TRIGGER_BYTES:
        raise CopilotIsolationError("Copilot trigger exceeds its byte limit")
    if not model or model_size > _MAX_MODEL_BYTES or "\0" in model:
        raise CopilotIsolationError("Copilot model identity is invalid")


def _worker_request(
    *,
    trigger: str,
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
        "semanticSession": f"{SANDBOX_ROOT}/session.jsonl",
        "target": target,
        "trigger": trigger,
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
) -> tuple[str, ...]:
    copilot = runtime.copilot.resolve(strict=True)
    return (
        os.fspath(runtime.bubblewrap.resolve(strict=True)),
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/home",
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
        "--chdir",
        f"{SANDBOX_ROOT}/workspace",
        f"{SANDBOX_ROOT}/{worker}",
        f"{SANDBOX_ROOT}/request.json",
        f"{SANDBOX_ROOT}/runner/copilot",
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
    "MAX_SEMANTIC_SESSION_BYTES",
    "MAX_TRIGGER_BYTES",
    "SANDBOX_ROOT",
    "CopilotIsolationError",
    "CopilotRuntime",
    "prepare_copilot_process",
    "program_digest",
]
