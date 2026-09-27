#!/usr/bin/python3
"""Disposable standard-library worker for one GitHub Copilot CLI turn."""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, cast

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_IMPORT_OUTPUT_BYTES = 1024 * 1024
_IMPORT_TIMEOUT_SECONDS = 30
_MAX_FLAGS = 64
_MAX_FLAG_BYTES = 4096
_MAX_MODEL_BYTES = 1024
_MAX_TRIGGER_BYTES = 1024 * 1024


class WorkerError(RuntimeError):
    """A content-safe Copilot worker failure."""


def main(argv: list[str] | None = None) -> int:
    """Import a private semantic session and run one controlled turn."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        _safe_error("Copilot worker requires a request and executable")
        return 64
    try:
        request_path = Path(arguments[0])
        request = _read_private_request(request_path)
        executable = _require_executable(Path(arguments[1]))
        session_id = _import_session(executable, request)
        return _resume_session(executable, request, session_id)
    except WorkerError as error:
        _safe_error(str(error))
        return 70


def _read_private_request(path: Path) -> dict[str, object]:
    raw = _read_private_file(path)
    return _parse_request(raw)


def _read_private_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= MAX_REQUEST_BYTES
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise WorkerError("Copilot worker request is unsafe")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_identity(opened) != _file_identity(metadata):
                raise WorkerError("Copilot worker request changed while opening")
            raw = stream.read(MAX_REQUEST_BYTES + 1)
            finished = os.fstat(stream.fileno())
        path.unlink()
    except WorkerError:
        raise
    except OSError as error:
        raise WorkerError("Copilot worker request is unavailable") from error
    if len(raw) != metadata.st_size or _file_identity(finished) != _file_identity(metadata):
        raise WorkerError("Copilot worker request changed while reading")
    return raw


def _parse_request(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkerError("Copilot worker request contains invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "commandFlags",
        "model",
        "semanticSession",
        "target",
        "trigger",
        "workspace",
    }:
        raise WorkerError("Copilot worker request has an unsupported shape")
    _validate_request(value)
    return cast(dict[str, object], value)


def _validate_request(request: dict[str, object]) -> None:
    flags = request.get("commandFlags")
    if (
        not isinstance(flags, list)
        or len(flags) > _MAX_FLAGS
        or any(
            not isinstance(flag, str)
            or not flag.startswith("--")
            or not flag
            or "\0" in flag
            or len(flag.encode("utf-8")) > _MAX_FLAG_BYTES
            for flag in flags
        )
    ):
        raise WorkerError("Copilot worker request has invalid fields")
    if (
        not _bounded_text(request.get("model"), _MAX_MODEL_BYTES)
        or not _bounded_text(request.get("trigger"), _MAX_TRIGGER_BYTES)
        or request.get("target") not in {"final-response", "shell-action"}
        or not _absolute_text_path(request.get("semanticSession"))
        or not _absolute_text_path(request.get("workspace"))
    ):
        raise WorkerError("Copilot worker request has invalid fields")


def _import_session(executable: Path, request: dict[str, object]) -> str:
    semantic_session = cast(str, request["semanticSession"])
    workspace = cast(str, request["workspace"])
    argv = (
        os.fspath(executable),
        "--no-auto-update",
        "sessions",
        "import",
        "--output",
        "json",
        "--working-directory",
        workspace,
        semantic_session,
    )
    returncode, output = _run_import(argv, workspace)
    if returncode != 0:
        raise WorkerError("Copilot semantic session import failed")
    return _parse_import_output(output)


def _run_import(argv: tuple[str, ...], workspace: str) -> tuple[int, bytes]:
    try:
        process = subprocess.Popen(  # noqa: S603 - validated executable and fixed arguments
            argv,
            cwd=workspace,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as error:
        raise WorkerError("Copilot semantic session import could not start") from error
    try:
        return _capture_import_output(process)
    finally:
        _stop_import(process)


def _capture_import_output(process: subprocess.Popen[bytes]) -> tuple[int, bytes]:
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:
        raise WorkerError("Copilot semantic session import output is unavailable")
    output = (bytearray(), bytearray())
    deadline = time.monotonic() + _IMPORT_TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ, 0)
            selector.register(stderr, selectors.EVENT_READ, 1)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("Copilot semantic session import timed out")
                ready = selector.select(remaining)
                if not ready:
                    raise WorkerError("Copilot semantic session import timed out")
                for key, _ in ready:
                    stream = cast(BinaryIO, key.fileobj)
                    index = cast(int, key.data)
                    content = os.read(
                        stream.fileno(),
                        MAX_IMPORT_OUTPUT_BYTES + 1 - len(output[index]),
                    )
                    if not content:
                        selector.unregister(stream)
                        continue
                    output[index].extend(content)
                    if len(output[index]) > MAX_IMPORT_OUTPUT_BYTES:
                        raise WorkerError(
                            "Copilot semantic session import exceeded its output limit"
                        )
        try:
            return process.wait(timeout=max(0, deadline - time.monotonic())), bytes(output[0])
        except subprocess.TimeoutExpired:
            raise WorkerError("Copilot semantic session import timed out") from None
    except OSError as error:
        raise WorkerError("Copilot semantic session import output could not be captured") from error


def _stop_import(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkerError("Copilot semantic session import could not stop") from error
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _parse_import_output(output: bytes) -> str:
    try:
        document = json.loads(
            output,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkerError("Copilot semantic session import returned invalid output") from error
    session_id = document.get("sessionId") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("ok") is not True
        or document.get("code") != "session_imported"
        or not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
    ):
        raise WorkerError("Copilot semantic session import returned invalid output")
    return session_id


def _resume_session(
    executable: Path,
    request: dict[str, object],
    session_id: str,
) -> int:
    flags = cast(list[str], request["commandFlags"])
    model = cast(str, request["model"])
    trigger = cast(str, request["trigger"])
    workspace = cast(str, request["workspace"])
    argv = (
        os.fspath(executable),
        *flags,
        "--model",
        model,
        "--resume",
        session_id,
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - validated executable and fixed arguments
            argv,
            cwd=workspace,
            env=dict(os.environ),
            stdin=subprocess.PIPE,
            stdout=None,
            stderr=None,
            close_fds=True,
        )
        encoded = trigger.encode("utf-8") + b"\n"
        stdin = cast(BinaryIO | None, process.stdin)
        if stdin is None:
            raise WorkerError("Copilot standard input is unavailable")
        try:
            stdin.write(encoded)
            stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            stdin.close()
        return process.wait()
    except WorkerError:
        raise
    except OSError as error:
        raise WorkerError("Copilot controlled session could not start") from error


def _require_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise WorkerError("Copilot executable is unavailable") from error
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o111 == 0
    ):
        raise WorkerError("Copilot executable is unavailable")
    return resolved


def _bounded_text(value: object, maximum: int) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _absolute_text_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\0" not in value
        and Path(value).is_absolute()
        and ".." not in Path(value).parts
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _reject_constant(_value: str) -> None:
    raise ValueError


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _safe_error(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
