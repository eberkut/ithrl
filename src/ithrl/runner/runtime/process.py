"""Bounded POSIX process execution for harness runners."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, BinaryIO

from ithrl.execution import ProtectedStateEffect, ResourceLimits
from ithrl.runner.contracts import (
    CleanupOutcome,
    NativeOutputReference,
    ProcessOutcome,
    RunnerErrorCode,
)
from ithrl.runner.runtime.contracts import (
    ProcessExecution,
    ProcessRuntimeCancelled,
    ProcessRuntimeError,
    ProcessSpec,
    RuntimeReport,
    require_process_group_support,
)
from ithrl.runner.runtime.output import (
    NativeOutputLimitError,
    NativeOutputStoreError,
    ProtectedOutputStore,
)
from ithrl.runner.runtime.protection import (
    ProtectedPath,
    ProtectedPathError,
    ProtectedPathSnapshot,
)
from ithrl.runner.runtime.state import (
    AttemptStateError,
    AttemptStateLimitError,
    OwnedAttemptState,
    StateMeasurement,
    require_safe_attempt_state_support,
)

_OUTPUT_CHUNK_BYTES = 64 * 1024
_STATE_POLL_SECONDS = 0.05
_TERMINATION_GRACE_SECONDS = 1.0
_KILL_GRACE_SECONDS = 1.0
PROCESS_CANCELLATION_GRACE_SECONDS = 3


class _OutputLimitExceeded(RuntimeError):
    pass


class _StateLimitExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _Usage:
    output_bytes: int = 0
    state_bytes: int = 0
    state_entries: int = 0


@dataclass(frozen=True, slots=True)
class _ChildOutcome:
    returncode: int | None
    error_code: RunnerErrorCode | None
    cancelled: bool
    process_outcome: ProcessOutcome
    forced_termination: bool
    output_complete: bool
    stdout_stop_matched: bool = False


@dataclass(frozen=True, slots=True)
class _FinalizedAttempt:
    child: _ChildOutcome
    native_output: NativeOutputReference | None
    cleanup_outcome: CleanupOutcome
    residual_bytes: int
    residual_entries: int


async def run_isolated_process(
    *,
    attempt_id: str,
    state_parent: Path,
    output_store: ProtectedOutputStore,
    prepare: Callable[[OwnedAttemptState], ProcessSpec],
    resource_limits: ResourceLimits,
    source_paths: Iterable[ProtectedPath],
    workspace_paths: Iterable[ProtectedPath],
    max_protected_entries: int,
    max_protected_bytes: int,
) -> ProcessExecution:
    """Run one direct child in fresh state, then preserve output and verify cleanup."""

    require_process_group_support()
    require_safe_attempt_state_support()
    started = time.monotonic_ns()
    deadline = time.monotonic() + resource_limits.max_wall_time_seconds
    try:
        source_snapshot = ProtectedPathSnapshot.capture(
            source_paths,
            max_entries=max_protected_entries,
            max_bytes=max_protected_bytes,
        )
        workspace_snapshot = ProtectedPathSnapshot.capture(
            workspace_paths,
            max_entries=max_protected_entries,
            max_bytes=max_protected_bytes,
        )
    except ProtectedPathError as error:
        raise ProcessRuntimeError(
            RunnerErrorCode.PREPARATION,
            f"cannot snapshot protected inputs before execution: {error}",
            reason_code="protected-input-snapshot",
            report=RuntimeReport(
                elapsed_milliseconds=(time.monotonic_ns() - started) // 1_000_000,
                native_output_bytes=0,
                disposable_state_bytes=0,
                residual_disposable_state_bytes=0,
                disposable_state_entries=0,
                residual_disposable_state_entries=0,
                process_outcome="not-started",
                forced_termination=False,
                stdout_stop_matched=False,
                cleanup_outcome="not-required",
                source_store_effect="unchanged",
                original_workspace_effect="unchanged",
            ),
        ) from error
    usage = _Usage()
    state, child = await _create_and_run(
        attempt_id=attempt_id,
        state_parent=state_parent,
        prepare=prepare,
        limits=resource_limits,
        usage=usage,
        deadline=deadline,
    )
    finalized = _finalize_attempt(
        state=state,
        child=child,
        output_store=output_store,
        limits=resource_limits,
        usage=usage,
    )

    source_effect = _verify_protected(source_snapshot)
    workspace_effect = _verify_protected(workspace_snapshot)
    elapsed_milliseconds = (time.monotonic_ns() - started) // 1_000_000
    report = RuntimeReport(
        elapsed_milliseconds=elapsed_milliseconds,
        native_output_bytes=(
            finalized.native_output.size_bytes if finalized.native_output is not None else 0
        ),
        disposable_state_bytes=usage.state_bytes,
        residual_disposable_state_bytes=finalized.residual_bytes,
        disposable_state_entries=usage.state_entries,
        residual_disposable_state_entries=finalized.residual_entries,
        process_outcome=finalized.child.process_outcome,
        forced_termination=finalized.child.forced_termination,
        stdout_stop_matched=finalized.child.stdout_stop_matched,
        cleanup_outcome=finalized.cleanup_outcome,
        source_store_effect=source_effect,
        original_workspace_effect=workspace_effect,
    )
    return _resolve_outcome(finalized, report, resource_limits)


async def _create_and_run(
    *,
    attempt_id: str,
    state_parent: Path,
    prepare: Callable[[OwnedAttemptState], ProcessSpec],
    limits: ResourceLimits,
    usage: _Usage,
    deadline: float,
) -> tuple[OwnedAttemptState | None, _ChildOutcome]:
    state: OwnedAttemptState | None = None
    spec: ProcessSpec | None = None
    try:
        _require_before_deadline(deadline)
        state = OwnedAttemptState.create(state_parent, attempt_id)
        spec = prepare(state)
        _validate_spec(state, spec)
        _measure_state(state, usage, limits)
        _require_before_deadline(deadline)
        return state, await _run_child(state, spec, limits, usage, deadline=deadline)
    except asyncio.CancelledError:
        _clear_pending_cancellation()
        return state, _ChildOutcome(None, None, True, "unknown", False, False)
    except (AttemptStateLimitError, _StateLimitExceeded):
        return state, _ChildOutcome(
            None, RunnerErrorCode.OUTPUT_LIMIT, False, "not-started", False, False
        )
    except TimeoutError:
        return state, _ChildOutcome(
            None, RunnerErrorCode.TIMEOUT, False, "not-started", False, False
        )
    except (AttemptStateError, NativeOutputLimitError, OSError, ValueError):
        return state, _ChildOutcome(
            None, RunnerErrorCode.PREPARATION, False, "not-started", False, False
        )
    except Exception:
        return state, _ChildOutcome(
            None, RunnerErrorCode.INTERNAL, False, "not-started", False, False
        )
    finally:
        if spec is not None:
            _close_inherited_fds(spec.inherited_fds)


def _finalize_attempt(
    *,
    state: OwnedAttemptState | None,
    child: _ChildOutcome,
    output_store: ProtectedOutputStore,
    limits: ResourceLimits,
    usage: _Usage,
) -> _FinalizedAttempt:
    if state is None:
        return _FinalizedAttempt(child, None, "not-required", 0, 0)
    native_output, child = _publish_output(
        output_store,
        state,
        child,
        max_bytes=limits.max_native_output_bytes,
    )
    if state_error := _measure_peak(state, usage, limits):
        child = _replace_error(child, state_error)
    cleanup_outcome, residual_bytes, residual_entries = _cleanup_state(state, usage, limits)
    return _FinalizedAttempt(
        child,
        native_output,
        cleanup_outcome,
        residual_bytes,
        residual_entries,
    )


def _resolve_outcome(
    finalized: _FinalizedAttempt,
    report: RuntimeReport,
    limits: ResourceLimits,
) -> ProcessExecution:
    child = finalized.child
    native_output = finalized.native_output
    if child.cancelled:
        raise ProcessRuntimeCancelled(report=report, native_output=native_output)
    if code := _runtime_failure_code(child, report, limits):
        raise ProcessRuntimeError(
            code,
            _safe_failure_message(code),
            report=report,
            native_output=native_output,
        )
    if child.returncode is None:
        raise ProcessRuntimeError(
            RunnerErrorCode.INTERNAL,
            "isolated runner did not report a process exit status",
            report=report,
            native_output=native_output,
        )
    return ProcessExecution(
        returncode=child.returncode,
        native_output=native_output,
        report=report,
    )


def _runtime_failure_code(
    child: _ChildOutcome,
    report: RuntimeReport,
    limits: ResourceLimits,
) -> RunnerErrorCode | None:
    if (
        child.process_outcome == "unknown"
        or report.cleanup_outcome == "failed"
        or report.source_store_effect != "unchanged"
        or report.original_workspace_effect != "unchanged"
    ):
        return RunnerErrorCode.SAFETY
    if report.elapsed_milliseconds > limits.max_wall_time_seconds * 1000:
        return RunnerErrorCode.TIMEOUT
    return child.error_code


async def _run_child(
    state: OwnedAttemptState,
    spec: ProcessSpec,
    limits: ResourceLimits,
    usage: _Usage,
    *,
    deadline: float,
) -> _ChildOutcome:
    stdout_path = state.member("stdout.bin")
    stderr_path = state.member("stderr.bin")
    capacity = ProtectedOutputStore.payload_capacity(limits.max_native_output_bytes)
    stdout = _open_output(stdout_path)
    try:
        stderr = _open_output(stderr_path)
    except OSError:
        _close_output(stdout)
        raise
    try:
        child = await _run_child_with_output(
            state,
            spec,
            limits,
            usage,
            stdout=stdout,
            stderr=stderr,
            output_capacity=capacity,
            stop_after_stdout_line=spec.stop_after_stdout_line,
            deadline=deadline,
        )
    finally:
        stdout_closed = _close_output(stdout)
        stderr_closed = _close_output(stderr)
    if not stdout_closed or not stderr_closed:
        return _mark_output_incomplete(child)
    return child


async def _run_child_with_output(
    state: OwnedAttemptState,
    spec: ProcessSpec,
    limits: ResourceLimits,
    usage: _Usage,
    *,
    stdout: BinaryIO,
    stderr: BinaryIO,
    output_capacity: int,
    stop_after_stdout_line: Callable[[bytes], bool] | None,
    deadline: float,
) -> _ChildOutcome:
    process: subprocess.Popen[bytes] | None = None
    waiter: asyncio.Task[None] | None = None
    stdout_stop_event = asyncio.Event() if stop_after_stdout_line is not None else None
    try:
        _require_before_deadline(deadline)
        process = _start_process(spec)
        _require_before_deadline(deadline)
        waiter = asyncio.create_task(
            _wait_for_process(
                process,
                stdout=stdout,
                stderr=stderr,
                state=state,
                usage=usage,
                output_capacity=output_capacity,
                stop_after_stdout_line=stop_after_stdout_line,
                stdout_stop_event=stdout_stop_event,
                state_capacity=limits.max_disposable_state_bytes,
                state_entry_capacity=limits.max_disposable_state_entries,
                deadline=deadline,
            )
        )
        stdout_stop_matched = await _wait_until_complete_or_stop(waiter, stdout_stop_event)
        return await _finalize_child(
            process,
            waiter,
            error=None,
            cancelled=False,
            drain_output=True,
            stdout_stop_matched=stdout_stop_matched,
        )
    except asyncio.CancelledError:
        _clear_pending_cancellation()
        return await _finalize_child(
            process,
            waiter,
            error=None,
            cancelled=True,
            drain_output=False,
            stdout_stop_matched=(stdout_stop_event is not None and stdout_stop_event.is_set()),
        )
    except Exception as error:
        return await _finalize_child(
            process,
            waiter,
            error=error,
            cancelled=False,
            drain_output=False,
            stdout_stop_matched=(stdout_stop_event is not None and stdout_stop_event.is_set()),
        )


async def _wait_until_complete_or_stop(
    waiter: asyncio.Task[None],
    stdout_stop_event: asyncio.Event | None,
) -> bool:
    if stdout_stop_event is None:
        await asyncio.shield(waiter)
        return False

    stop_waiter = asyncio.create_task(stdout_stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, stop_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if waiter in done:
            await asyncio.shield(waiter)
        return stdout_stop_event.is_set()
    finally:
        await _cancel_and_wait(stop_waiter)


async def _finalize_child(
    process: subprocess.Popen[bytes] | None,
    waiter: asyncio.Task[None] | None,
    *,
    error: Exception | None,
    cancelled: bool,
    drain_output: bool,
    stdout_stop_matched: bool,
) -> _ChildOutcome:
    termination = asyncio.create_task(_stop_after_interruption(process))
    if waiter is not None and not drain_output and not waiter.done():
        waiter.cancel()
    settlement = asyncio.create_task(_settle_waiter(waiter)) if waiter is not None else None
    (process_outcome, forced), cancelled_during_termination = await _await_despite_cancellation(
        termination
    )
    waiter_error: BaseException | None = None
    cancelled_during_settlement = False
    if settlement is not None:
        waiter_error, cancelled_during_settlement = await _await_despite_cancellation(settlement)
    effective_error = error or (waiter_error if isinstance(waiter_error, Exception) else None)
    return _ChildOutcome(
        process.returncode if process is not None else None,
        (
            _child_error_code(effective_error, process_started=process is not None)
            if effective_error is not None
            else None
        ),
        cancelled or cancelled_during_termination or cancelled_during_settlement,
        process_outcome,
        forced,
        drain_output and waiter_error is None,
        stdout_stop_matched,
    )


async def _settle_waiter(task: asyncio.Task[None]) -> BaseException | None:
    try:
        await task
    except (asyncio.CancelledError, Exception) as error:
        return error
    return None


async def _await_despite_cancellation[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    """Finish bounded safety work while remembering repeated cancellation."""

    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            _clear_pending_cancellation()


def _child_error_code(error: Exception, *, process_started: bool) -> RunnerErrorCode:
    if isinstance(error, TimeoutError):
        return RunnerErrorCode.TIMEOUT
    if isinstance(error, (_OutputLimitExceeded, _StateLimitExceeded)):
        return RunnerErrorCode.OUTPUT_LIMIT
    if isinstance(error, AttemptStateError):
        return RunnerErrorCode.SAFETY
    if isinstance(error, OSError) and not process_started:
        return RunnerErrorCode.STARTUP
    return RunnerErrorCode.INTERNAL


def _start_process(spec: ProcessSpec) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - validated direct argv; no shell is involved
        spec.argv,
        cwd=spec.cwd,
        env=dict(spec.environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=spec.inherited_fds,
        start_new_session=True,
        close_fds=True,
    )


def _close_inherited_fds(descriptors: tuple[int, ...]) -> None:
    for descriptor in descriptors:
        with contextlib.suppress(OSError):
            os.close(descriptor)


async def _wait_for_process(
    process: subprocess.Popen[bytes],
    *,
    stdout: BinaryIO,
    stderr: BinaryIO,
    state: OwnedAttemptState,
    usage: _Usage,
    output_capacity: int,
    stop_after_stdout_line: Callable[[bytes], bool] | None,
    stdout_stop_event: asyncio.Event | None,
    state_capacity: int,
    state_entry_capacity: int,
    deadline: float,
) -> None:
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    if stdout_pipe is None or stderr_pipe is None:
        raise RuntimeError("isolated process output pipes are unavailable")
    stdout_transport: asyncio.ReadTransport | None = None
    stderr_transport: asyncio.ReadTransport | None = None
    try:
        stdout_reader, stdout_transport = await _connect_reader(stdout_pipe)
        stderr_reader, stderr_transport = await _connect_reader(stderr_pipe)
        output_lock = asyncio.Lock()
        work = asyncio.create_task(
            _communicate(
                process,
                stdout_reader=stdout_reader,
                stderr_reader=stderr_reader,
                stdout=stdout,
                stderr=stderr,
                usage=usage,
                output_capacity=output_capacity,
                output_lock=output_lock,
                stop_after_stdout_line=stop_after_stdout_line,
                stdout_stop_event=stdout_stop_event,
            )
        )
        monitor = asyncio.create_task(
            _monitor_state(state, usage, state_capacity, state_entry_capacity)
        )
        try:
            async with asyncio.timeout_at(deadline):
                done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
                if monitor in done:
                    await monitor
                await work
        finally:
            await _cancel_and_wait(work, monitor)
    finally:
        _close_pipe(stdout_pipe, stdout_transport)
        _close_pipe(stderr_pipe, stderr_transport)


async def _connect_reader(
    pipe: IO[Any],
) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)
    return reader, transport


def _close_pipe(pipe: IO[Any], transport: asyncio.ReadTransport | None) -> None:
    if transport is not None:
        transport.close()
        return
    with contextlib.suppress(OSError):
        pipe.close()


async def _communicate(
    process: subprocess.Popen[bytes],
    *,
    stdout_reader: asyncio.StreamReader,
    stderr_reader: asyncio.StreamReader,
    stdout: BinaryIO,
    stderr: BinaryIO,
    usage: _Usage,
    output_capacity: int,
    output_lock: asyncio.Lock,
    stop_after_stdout_line: Callable[[bytes], bool] | None,
    stdout_stop_event: asyncio.Event | None,
) -> None:
    tasks = (
        asyncio.create_task(_wait_for_exit(process)),
        asyncio.create_task(
            _capture(
                stdout_reader,
                stdout,
                usage,
                output_capacity,
                output_lock,
                stop_after_line=stop_after_stdout_line,
                stop_event=stdout_stop_event,
            )
        ),
        asyncio.create_task(_capture(stderr_reader, stderr, usage, output_capacity, output_lock)),
    )
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
        await asyncio.gather(*pending)
    finally:
        await _cancel_and_wait(*tasks)


async def _cancel_and_wait(*tasks: asyncio.Task[Any]) -> None:
    finalizer = asyncio.create_task(_finish_cancelled_tasks(tasks))
    _, cancelled = await _await_despite_cancellation(finalizer)
    if cancelled:
        raise asyncio.CancelledError from None


async def _finish_cancelled_tasks(tasks: tuple[asyncio.Task[Any], ...]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_for_exit(process: subprocess.Popen[bytes]) -> int:
    return await asyncio.to_thread(process.wait)


async def _capture(
    stream: asyncio.StreamReader,
    destination: BinaryIO,
    usage: _Usage,
    capacity: int,
    lock: asyncio.Lock,
    stop_after_line: Callable[[bytes], bool] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    if stop_after_line is not None:
        if stop_event is None:
            raise RuntimeError("stdout stop event is unavailable")
        await _capture_lines(
            stream,
            destination,
            usage,
            capacity,
            lock,
            stop_after_line,
            stop_event,
        )
        return
    while chunk := await stream.read(_OUTPUT_CHUNK_BYTES):
        await _write_output(destination, chunk, usage, capacity, lock)


async def _capture_lines(
    stream: asyncio.StreamReader,
    destination: BinaryIO,
    usage: _Usage,
    capacity: int,
    lock: asyncio.Lock,
    stop_after_line: Callable[[bytes], bool],
    stop_event: asyncio.Event,
) -> None:
    pending = bytearray()
    while chunk := await stream.read(_OUTPUT_CHUNK_BYTES):
        pending.extend(chunk)
        if len(pending) > capacity:
            raise _OutputLimitExceeded
        matched_in_chunk = False
        while (line_end := pending.find(b"\n")) >= 0:
            encoded = bytes(pending[: line_end + 1])
            del pending[: line_end + 1]
            await _write_output(destination, encoded, usage, capacity, lock)
            if stop_after_line(encoded.removesuffix(b"\n").removesuffix(b"\r")):
                matched_in_chunk = True
        if matched_in_chunk:
            stop_event.set()
    if pending:
        await _write_output(destination, bytes(pending), usage, capacity, lock)


async def _write_output(
    destination: BinaryIO,
    chunk: bytes,
    usage: _Usage,
    capacity: int,
    lock: asyncio.Lock,
) -> None:
    async with lock:
        available = capacity - usage.output_bytes
        accepted = chunk[: max(available, 0)]
        if accepted:
            destination.write(accepted)
            usage.output_bytes += len(accepted)
        if len(accepted) != len(chunk):
            raise _OutputLimitExceeded


async def _monitor_state(
    state: OwnedAttemptState,
    usage: _Usage,
    capacity: int,
    entry_capacity: int,
) -> None:
    while True:
        try:
            current = await _measure_state_without_abandoning(
                state,
                max_entries=entry_capacity,
                max_bytes=capacity,
            )
        except AttemptStateLimitError as error:
            _record_state_measurement(usage, error.measurement)
            raise _StateLimitExceeded from error
        _record_state_measurement(usage, current)
        await asyncio.sleep(_STATE_POLL_SECONDS)


async def _measure_state_without_abandoning(
    state: OwnedAttemptState,
    *,
    max_entries: int,
    max_bytes: int,
) -> StateMeasurement:
    measurement = asyncio.create_task(
        asyncio.to_thread(
            state.measure,
            max_entries=max_entries,
            max_bytes=max_bytes,
        )
    )
    try:
        return await asyncio.shield(measurement)
    except asyncio.CancelledError:
        _clear_pending_cancellation()
        with contextlib.suppress(AttemptStateError):
            await _await_despite_cancellation(measurement)
        raise asyncio.CancelledError from None


async def _stop_after_interruption(
    process: subprocess.Popen[bytes] | None,
) -> tuple[ProcessOutcome, bool]:
    if process is None:
        return "not-started", False
    return await _terminate_group(process)


async def _terminate_group(
    process: subprocess.Popen[bytes],
) -> tuple[ProcessOutcome, bool]:
    if not _signal_group(process.pid, signal.SIGTERM):
        await _await_process(process, _TERMINATION_GRACE_SECONDS)
        return ("exited", False) if not _process_group_exists(process.pid) else ("unknown", False)
    if await _wait_group_gone(process, _TERMINATION_GRACE_SECONDS):
        return "terminated", False
    if not _signal_group(process.pid, signal.SIGKILL):
        return "unknown", True
    if await _wait_group_gone(process, _KILL_GRACE_SECONDS):
        return "terminated", True
    return "unknown", True


async def _wait_group_gone(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
) -> bool:
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        await _await_process(process, min(0.05, max(deadline - time.monotonic(), 0.001)))
        if not _process_group_exists(process.pid):
            return True
        await asyncio.sleep(0.01)
    return not _process_group_exists(process.pid)


async def _await_process(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    try:
        await asyncio.to_thread(process.wait, timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return


def _signal_group(process_group: int, selected_signal: signal.Signals) -> bool:
    try:
        os.killpg(process_group, selected_signal)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _open_output(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb", buffering=0)


def _close_output(output: BinaryIO) -> bool:
    if not output.closed:
        try:
            output.flush()
            os.fsync(output.fileno())
            output.close()
        except OSError:
            with contextlib.suppress(OSError):
                output.close()
            return False
    return True


def _validate_spec(state: OwnedAttemptState, spec: ProcessSpec) -> None:
    state.assert_unchanged()
    try:
        relative = spec.cwd.relative_to(state.root)
    except ValueError as error:
        raise ValueError("process working directory must be inside disposable state") from error
    current = state.root
    for part in relative.parts:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("process working directory is unsafe")


def _require_before_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError


def _publish_output(
    store: ProtectedOutputStore,
    state: OwnedAttemptState,
    child: _ChildOutcome,
    *,
    max_bytes: int,
) -> tuple[NativeOutputReference | None, _ChildOutcome]:
    if child.process_outcome == "not-started":
        return None, child
    try:
        reference = store.publish(
            state,
            stdout_name="stdout.bin",
            stderr_name="stderr.bin",
            max_bytes=max_bytes,
            complete=child.output_complete,
        )
    except NativeOutputLimitError:
        return None, _replace_error(child, RunnerErrorCode.OUTPUT_LIMIT)
    except (AttemptStateError, NativeOutputStoreError):
        return None, _replace_error(child, RunnerErrorCode.INTERNAL)
    return reference, child


def _measure_state(
    state: OwnedAttemptState,
    usage: _Usage,
    limits: ResourceLimits,
) -> None:
    try:
        measurement = state.measure(
            max_entries=limits.max_disposable_state_entries,
            max_bytes=limits.max_disposable_state_bytes,
        )
    except AttemptStateLimitError as error:
        _record_state_measurement(usage, error.measurement)
        raise
    _record_state_measurement(usage, measurement)


def _measure_peak(
    state: OwnedAttemptState,
    usage: _Usage,
    limits: ResourceLimits,
) -> RunnerErrorCode | None:
    try:
        _measure_state(state, usage, limits)
    except AttemptStateLimitError:
        return RunnerErrorCode.OUTPUT_LIMIT
    except AttemptStateError:
        return RunnerErrorCode.SAFETY
    return None


def _cleanup_state(
    state: OwnedAttemptState,
    usage: _Usage,
    limits: ResourceLimits,
) -> tuple[CleanupOutcome, int, int]:
    try:
        result = state.cleanup(
            StateMeasurement(entries=usage.state_entries, bytes=usage.state_bytes)
        )
    except AttemptStateError:
        try:
            residual = state.measure(
                max_entries=limits.max_disposable_state_entries,
                max_bytes=limits.max_disposable_state_bytes,
            )
        except AttemptStateLimitError as error:
            residual = error.measurement
        except AttemptStateError:
            residual = StateMeasurement()
        _record_state_measurement(usage, residual)
        return "failed", residual.bytes, residual.entries
    usage.state_bytes = max(usage.state_bytes, result.state_bytes)
    usage.state_entries = max(usage.state_entries, result.state_entries)
    return "verified", result.residual_bytes, result.residual_entries


def _record_state_measurement(usage: _Usage, measurement: StateMeasurement) -> None:
    usage.state_bytes = max(usage.state_bytes, measurement.bytes)
    usage.state_entries = max(usage.state_entries, measurement.entries)


def _verify_protected(snapshot: ProtectedPathSnapshot) -> ProtectedStateEffect:
    try:
        return "may-change" if snapshot.verify() else "unchanged"
    except ProtectedPathError:
        return "may-change"


def _replace_error(child: _ChildOutcome, code: RunnerErrorCode) -> _ChildOutcome:
    return replace(child, error_code=code)


def _mark_output_incomplete(child: _ChildOutcome) -> _ChildOutcome:
    return replace(
        child,
        error_code=child.error_code or (None if child.cancelled else RunnerErrorCode.INTERNAL),
        output_complete=False,
    )


def _clear_pending_cancellation() -> None:
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling():
        task.uncancel()


def _safe_failure_message(code: RunnerErrorCode) -> str:
    return {
        RunnerErrorCode.PREPARATION: "isolated runner preparation failed",
        RunnerErrorCode.STARTUP: "isolated runner process did not start",
        RunnerErrorCode.TIMEOUT: "isolated runner exceeded its wall-time limit",
        RunnerErrorCode.OUTPUT_LIMIT: "isolated runner exceeded a resource limit",
        RunnerErrorCode.INTERNAL: "isolated runner failed internally",
    }.get(code, "isolated runner failed")
