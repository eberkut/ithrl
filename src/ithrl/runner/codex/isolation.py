"""Prepare a Codex app-server worker inside a restricted Bubblewrap process."""

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
from ithrl.context import JsonObject, content_digest, validate_json_limits
from ithrl.runner.codex.configuration import MAX_AUTH_BYTES, CodexProviderConfiguration
from ithrl.runner.codex.protocol import MAX_WORKER_REQUEST_BYTES, SANDBOX_ROOT
from ithrl.runner.runtime import OwnedAttemptState, ProcessSpec

_MAX_WORKER_BYTES = 1024 * 1024


class CodexIsolationError(RuntimeError):
    """A preventive Codex process boundary cannot be prepared."""


@dataclass(frozen=True, slots=True)
class CodexRuntime:
    """Validated local executables required by the Codex runner."""

    codex: Path = field(repr=False)
    bubblewrap: Path = field(repr=False)
    python: Path = field(repr=False)
    _digests: tuple[str, str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        programs = tuple(
            _read_executable(path) for path in (self.codex, self.bubblewrap, self.python)
        )
        object.__setattr__(self, "codex", programs[0][0])
        object.__setattr__(self, "bubblewrap", programs[1][0])
        object.__setattr__(self, "python", programs[2][0])
        object.__setattr__(self, "_digests", tuple(program[1] for program in programs))

    @property
    def identity(self) -> str:
        return content_digest(
            {
                "codex": self._digests[0],
                "bubblewrap": self._digests[1],
                "python": self._digests[2],
            }
        )

    def assert_unchanged(self) -> None:
        current = tuple(
            _read_executable(path)[1] for path in (self.codex, self.bubblewrap, self.python)
        )
        if current != self._digests:
            raise CodexIsolationError("a required Codex runtime executable changed")


def prepare_codex_process(
    state: OwnedAttemptState,
    *,
    runtime: CodexRuntime,
    configuration: CodexProviderConfiguration,
    request: JsonObject,
    worker_source: Path,
    worker_digest: str,
    support_programs: tuple[tuple[Path, str], ...],
) -> ProcessSpec:
    """Create private worker inputs and return one restricted direct invocation."""

    state.assert_unchanged()
    runtime.assert_unchanged()
    validate_json_limits(request)
    encoded_request = json.dumps(
        request,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded_request) > MAX_WORKER_REQUEST_BYTES:
        raise CodexIsolationError("Codex candidate exceeds the reconstruction byte limit")

    workspace = _private_directory(state.root / "workspace")
    codex_home = _private_directory(state.root / "codex-home")
    _private_directory(state.root / "home")
    _private_directory(state.root / "runner")
    worker = _copy_private_program(
        worker_source,
        state.root / "worker.py",
        expected_digest=worker_digest,
    )
    _copy_support_programs(state.root, support_programs)
    request_path = state.root / "request.json"
    try:
        atomic_write_bytes(request_path, encoded_request)
        atomic_write_bytes(codex_home / "config.toml", configuration.rendered_config)
    except CaseError as error:
        raise CodexIsolationError("cannot prepare private Codex attempt state") from error

    environment = _worker_environment(configuration.environment)
    auth_descriptor = _memory_file(configuration.auth_data)
    try:
        argv = _bubblewrap_argv(
            runtime,
            state=state.root,
            worker=worker.name,
            request=request_path.name,
            auth_descriptor=auth_descriptor,
        )
        inherited_fds = () if auth_descriptor is None else (auth_descriptor,)
        return ProcessSpec(
            argv=argv,
            cwd=workspace,
            environment=environment,
            inherited_fds=inherited_fds,
        )
    except Exception:
        if auth_descriptor is not None:
            os.close(auth_descriptor)
        raise


def _copy_support_programs(
    state_root: Path,
    programs: tuple[tuple[Path, str], ...],
) -> None:
    names = tuple(source.name for source, _ in programs)
    if not names or len(set(names)) != len(names) or "worker.py" in names:
        raise CodexIsolationError("Codex worker support programs are invalid")
    for source, expected_digest in programs:
        _copy_private_program(
            source,
            state_root / source.name,
            expected_digest=expected_digest,
        )


def _bubblewrap_argv(
    runtime: CodexRuntime,
    *,
    state: Path,
    worker: str,
    request: str,
    auth_descriptor: int | None,
) -> tuple[str, ...]:
    codex = runtime.codex.resolve(strict=True)
    python = runtime.python.resolve(strict=True)
    arguments = (
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
        "/tmp",  # noqa: S108 - private tmpfs inside the process sandbox
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
        os.fspath(codex),
        f"{SANDBOX_ROOT}/runner/codex",
    )
    if auth_descriptor is not None:
        arguments += (
            "--perms",
            "0600",
            "--ro-bind-data",
            str(auth_descriptor),
            f"{SANDBOX_ROOT}/codex-home/auth.json",
        )
    return (
        *arguments,
        "--chdir",
        f"{SANDBOX_ROOT}/workspace",
        os.fspath(python),
        f"{SANDBOX_ROOT}/{worker}",
        f"{SANDBOX_ROOT}/{request}",
        f"{SANDBOX_ROOT}/runner/codex",
    )


def _worker_environment(
    supplied: Mapping[str, str],
) -> dict[str, str]:
    environment = dict(supplied)
    environment.update(
        {
            "HOME": f"{SANDBOX_ROOT}/home",
            "CODEX_HOME": f"{SANDBOX_ROOT}/codex-home",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _memory_file(content: bytes | None) -> int | None:
    if content is None:
        return None
    if not content or len(content) > MAX_AUTH_BYTES or not hasattr(os, "memfd_create"):
        raise CodexIsolationError("cannot prepare private Codex authentication")
    descriptor = -1
    try:
        descriptor = os.memfd_create("ithrl-codex-auth", os.MFD_CLOEXEC)
        if descriptor < 3:
            import fcntl

            replacement = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(descriptor)
            descriptor = replacement
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.fchmod(descriptor, 0o600)
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise CodexIsolationError("cannot prepare private Codex authentication") from error


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
        metadata = path.lstat()
    except OSError as error:
        raise CodexIsolationError("cannot prepare private Codex attempt state") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CodexIsolationError("Codex attempt state contains an unsafe directory")
    return path


def program_digest(source: Path) -> str:
    """Return the content identity of one bounded runner program."""

    _, content = _read_private_program(source)
    return hashlib.sha256(content).hexdigest()


def _copy_private_program(source: Path, destination: Path, *, expected_digest: str) -> Path:
    try:
        _, content = _read_private_program(source)
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise CodexIsolationError("the Codex worker source changed")
        atomic_write_bytes(destination, content)
        destination.chmod(0o500)
    except CodexIsolationError:
        raise
    except (CaseError, OSError) as error:
        raise CodexIsolationError("cannot prepare the private Codex worker") from error
    return destination


def _read_private_program(source: Path) -> tuple[Path, bytes]:
    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_WORKER_BYTES:
            raise CodexIsolationError("the Codex worker source is invalid")
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_state(opened) != _file_state(metadata):
                raise CodexIsolationError("the Codex worker source changed while opening")
            content = stream.read(_MAX_WORKER_BYTES + 1)
            finished = os.fstat(stream.fileno())
        if (
            len(content) != metadata.st_size
            or _file_state(finished) != _file_state(metadata)
            or _file_state(resolved.stat()) != _file_state(metadata)
        ):
            raise CodexIsolationError("the Codex worker source changed while it was read")
    except CodexIsolationError:
        raise
    except OSError as error:
        raise CodexIsolationError("cannot read the private Codex worker") from error
    return resolved, content


def _read_executable(path: Path) -> tuple[Path, str]:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("Codex runtime paths must be absolute")
    try:
        target = path.resolve(strict=True)
        metadata = target.stat()
    except OSError as error:
        raise CodexIsolationError("a required Codex runtime executable is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
        raise CodexIsolationError("a required Codex runtime executable is unavailable")
    digest = hashlib.sha256()
    try:
        with target.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_state(opened) != _file_state(metadata):
                raise CodexIsolationError("a required Codex runtime executable changed")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            finished = os.fstat(stream.fileno())
    except OSError as error:
        raise CodexIsolationError("a required Codex runtime executable is unavailable") from error
    if _file_state(finished) != _file_state(metadata) or _file_state(target.stat()) != _file_state(
        metadata
    ):
        raise CodexIsolationError("a required Codex runtime executable changed")
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
