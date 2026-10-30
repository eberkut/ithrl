"""Disposable standard-library worker for one Codex app-server turn."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, TextIO, cast

if TYPE_CHECKING or __package__:
    from ithrl.runner.codex.protocol import (
        MAX_WORKER_REQUEST_BYTES,
        SANDBOX_ROOT,
        SANDBOX_WORKSPACE,
        ProtocolOutcome,
        audit_record,
        run_session,
        write_summary,
    )
    from ithrl.runner.codex.protocol_contracts import MAX_INFERENCE_REQUESTS
    from ithrl.runner.codex.relay import ResponsesRelay
else:
    from protocol import (
        MAX_WORKER_REQUEST_BYTES,
        SANDBOX_ROOT,
        SANDBOX_WORKSPACE,
        ProtocolOutcome,
        audit_record,
        run_session,
        write_summary,
    )
    from protocol_contracts import MAX_INFERENCE_REQUESTS
    from relay import ResponsesRelay

_SHUTDOWN_SECONDS = 2.0


class WorkerError(RuntimeError):
    """A content-safe worker startup or shutdown failure."""


def main(argv: list[str] | None = None) -> int:
    """Run one request and return a stable process status."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        _safe_error("Codex worker requires a request")
        return 64
    try:
        request = _read_private_request(Path(arguments[0]))
        executable = _require_executable(Path(SANDBOX_ROOT) / "runner" / "codex")
        outcome = _run_app_server(executable, request, audit=sys.stdout)
    except WorkerError as error:
        _safe_error(str(error))
        return 70
    except Exception:
        # Unexpected native/transport failures must never print private values.
        _safe_error("Codex worker failed")
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
    native_request = dict(request)
    transport = native_request.pop("transport", None)
    expected = {"endpoint", "max_requests", "timeout_seconds", "max_response_bytes"}
    if (
        not isinstance(transport, dict)
        or set(transport) != expected
        or not isinstance(transport.get("endpoint"), str)
        or type(transport.get("max_requests")) is not int
        or not 1 <= transport["max_requests"] <= MAX_INFERENCE_REQUESTS
        or type(transport.get("timeout_seconds")) is not int
        or not 1 <= transport["timeout_seconds"] <= 86400
        or type(transport.get("max_response_bytes")) is not int
        or not 1 <= transport["max_response_bytes"] <= MAX_WORKER_REQUEST_BYTES
    ):
        raise WorkerError("Codex worker transport is invalid")
    provider = native_request.get("provider")
    if (
        not isinstance(provider, str)
        or not provider
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in provider
        )
    ):
        raise WorkerError("Codex worker provider is invalid")
    callback_lock = threading.Lock()
    callbacks_open = True

    def on_event(event: dict[str, object]) -> None:
        with callback_lock:
            if callbacks_open:
                audit_record(audit, "provider", event)

    try:
        relay = ResponsesRelay(
            endpoint=transport["endpoint"],
            max_requests=transport["max_requests"],
            timeout_seconds=transport["timeout_seconds"],
            max_request_bytes=MAX_WORKER_REQUEST_BYTES,
            max_response_bytes=transport["max_response_bytes"],
            on_event=on_event,
        )
        with relay:
            outcome = _exchange(executable, native_request, relay.base_url, audit=audit)
    except (OSError, ValueError) as error:
        raise WorkerError("Codex worker transport is unavailable") from error
    with callback_lock:
        callbacks_open = False
        outcome = replace(
            outcome,
            provider_calls_started=relay.requests_started,
            provider_calls_completed=relay.responses_completed,
            provider_acceptance=(
                outcome.provider_acceptance if relay.requests_started else "not-started"
            ),
        )
        if (
            relay.failed
            or relay.requests_rejected
            or (
                outcome.status == "completed"
                and (
                    not relay.requests_started
                    or relay.requests_started != relay.responses_completed
                )
            )
        ):
            outcome = replace(
                outcome,
                status="error",
                error_code=(
                    "provider-call-limit"
                    if relay.selected_request_limit_hit
                    else "provider-transport"
                ),
                final_text=None,
                provider_acceptance="unknown" if relay.requests_started else "not-started",
            )
        audit_record(
            audit,
            "provider",
            {
                "kind": "closed",
                "ordinal": relay.requests_reserved,
                "count": relay.requests_started,
                "status": 0,
                "request_bytes": relay.request_bytes,
                "response_bytes": relay.response_bytes,
            },
        )
        write_summary(audit, outcome)
    return outcome


def _exchange(
    executable: Path,
    request: dict[str, object],
    base_url: str,
    *,
    audit: TextIO,
) -> ProtocolOutcome:
    child_env = dict(os.environ)
    existing_no_proxy = child_env.get("NO_PROXY", child_env.get("no_proxy", ""))
    loopback_no_proxy = f"{existing_no_proxy},127.0.0.1" if existing_no_proxy else "127.0.0.1"
    child_env["NO_PROXY"] = loopback_no_proxy
    child_env["no_proxy"] = loopback_no_proxy
    try:
        process = subprocess.Popen(  # noqa: S603 - validated direct executable; no shell
            (
                os.fspath(executable),
                "-c",
                f"model_providers.{request['provider']}.base_url={json.dumps(base_url)}",
                "app-server",
            ),
            cwd=SANDBOX_WORKSPACE,
            env=child_env,
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
        stderr_thread.join(timeout=_SHUTDOWN_SECONDS)
        if not stderr_thread.is_alive():
            _close(stderr)


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
        except subprocess.TimeoutExpired:
            # The owning runtime settles the whole process group and refuses
            # unverified cleanup. Preserve native and relay evidence for it.
            return


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
