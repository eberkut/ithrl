"""Disposable standard-library worker for one Codex app-server turn."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TextIO, cast

if TYPE_CHECKING or __package__:
    from ithrl.runner.codex.protocol import (
        MAX_WORKER_REQUEST_BYTES,
        SANDBOX_WORKSPACE,
        ProtocolOutcome,
        run_session,
    )
else:
    from protocol import MAX_WORKER_REQUEST_BYTES, SANDBOX_WORKSPACE, ProtocolOutcome, run_session

_SHUTDOWN_SECONDS = 2.0


class WorkerError(RuntimeError):
    """A content-safe worker startup or shutdown failure."""


def main(argv: list[str] | None = None) -> int:
    """Run one request and return a stable process status."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        _safe_error("Codex worker requires a request and executable")
        return 64
    try:
        request = _read_private_request(Path(arguments[0]))
        executable = _require_executable(Path(arguments[1]))
        outcome = _run_app_server(executable, request, audit=sys.stdout)
    except WorkerError as error:
        _safe_error(str(error))
        return 70
    return 0 if outcome.status == "completed" else 20


def _read_private_request(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 1
            or metadata.st_size > MAX_WORKER_REQUEST_BYTES
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise WorkerError("Codex worker request is unsafe")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise WorkerError("Codex worker request changed while opening")
            raw = stream.read(MAX_WORKER_REQUEST_BYTES + 1)
        path.unlink()
    except WorkerError:
        raise
    except OSError as error:
        raise WorkerError("Codex worker request is unavailable") from error
    if len(raw) != metadata.st_size:
        raise WorkerError("Codex worker request changed while reading")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerError("Codex worker request contains invalid JSON") from error
    if not isinstance(value, dict):
        raise WorkerError("Codex worker request must be a JSON object")
    return cast(dict[str, object], value)


def _require_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise WorkerError("Codex executable is unavailable") from error
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
    ):
        raise WorkerError("Codex executable is unavailable")
    return resolved


def _run_app_server(
    executable: Path,
    request: dict[str, object],
    *,
    audit: TextIO,
) -> ProtocolOutcome:
    try:
        process = subprocess.Popen(  # noqa: S603 - validated direct executable; no shell
            (os.fspath(executable), "app-server"),
            cwd=SANDBOX_WORKSPACE,
            env=dict(os.environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as error:
        raise WorkerError("Codex app-server could not start") from error
    stdin = _pipe(cast(BinaryIO | None, process.stdin))
    stdout = _pipe(cast(BinaryIO | None, process.stdout))
    stderr = _pipe(cast(BinaryIO | None, process.stderr))
    stderr_thread = threading.Thread(
        target=_discard_stderr,
        args=(stderr,),
        name="codex-stderr",
        daemon=True,
    )
    stderr_thread.start()
    try:
        return run_session(stdout, stdin, audit, request)
    finally:
        _close(stdin)
        _stop(process)
        _close(stdout)
        _close(stderr)
        stderr_thread.join(timeout=_SHUTDOWN_SECONDS)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_SHUTDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise WorkerError("Codex app-server did not stop") from error


def _pipe(value: BinaryIO | None) -> BinaryIO:
    if value is None:
        raise WorkerError("Codex app-server pipes are unavailable")
    return value


def _discard_stderr(source: BinaryIO) -> None:
    try:
        while source.read(64 * 1024):
            pass
    except OSError:
        return


def _close(stream: BinaryIO) -> None:
    try:
        stream.close()
    except OSError:
        return


def _safe_error(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
