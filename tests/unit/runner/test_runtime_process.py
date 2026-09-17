from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from ithrl.execution import ResourceLimits
from ithrl.runner import RunnerErrorCode
from ithrl.runner.contracts import ProcessOutcome
from ithrl.runner.runtime import process as runtime_process
from ithrl.runner.runtime import state as runtime_state
from ithrl.runner.runtime.contracts import (
    ProcessExecution,
    ProcessRuntimeCancelled,
    ProcessRuntimeError,
    ProcessSpec,
)
from ithrl.runner.runtime.output import ProtectedOutputStore
from ithrl.runner.runtime.process import run_isolated_process
from ithrl.runner.runtime.protection import ProtectedPath
from ithrl.runner.runtime.state import (
    AttemptStateError,
    CleanupResult,
    OwnedAttemptState,
    StateMeasurement,
)

HELPER = Path(__file__).parents[2] / "fixtures" / "runtime_helper.py"
ATTEMPT_ID = "attempt-0123456789abcdef0123456789abcdef"


def _protected_paths(tmp_path: Path) -> tuple[tuple[ProtectedPath, ...], tuple[ProtectedPath, ...]]:
    source = tmp_path / "source.jsonl"
    workspace = tmp_path / "workspace"
    source.write_text('{"source":true}\n', encoding="utf-8")
    workspace.mkdir()
    (workspace / "tracked.txt").write_text("unchanged", encoding="utf-8")
    return (
        (ProtectedPath("source", source),),
        (ProtectedPath("workspace", workspace),),
    )


def _prepare(
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> Callable[[OwnedAttemptState], ProcessSpec]:
    def prepare(state: OwnedAttemptState) -> ProcessSpec:
        work = state.member("work")
        work.mkdir(mode=0o700)
        return ProcessSpec(
            argv=(sys.executable, str(HELPER), *arguments),
            cwd=work,
            environment=environment or {},
        )

    return prepare


async def _run(
    tmp_path: Path,
    prepare: Callable[[OwnedAttemptState], ProcessSpec],
    *,
    limits: ResourceLimits | None = None,
) -> tuple[ProtectedOutputStore, ProcessExecution]:
    source_paths, workspace_paths = _protected_paths(tmp_path)
    store = ProtectedOutputStore.open(tmp_path / "outputs")
    result = await run_isolated_process(
        attempt_id=ATTEMPT_ID,
        state_parent=tmp_path / "state",
        output_store=store,
        prepare=prepare,
        resource_limits=limits
        or ResourceLimits(
            max_wall_time_seconds=5,
            max_native_output_bytes=4096,
            max_disposable_state_bytes=4096,
        ),
        source_paths=source_paths,
        workspace_paths=workspace_paths,
        max_protected_entries=16,
        max_protected_bytes=4096,
    )
    return store, result


def test_process_runs_with_direct_arguments_and_an_explicit_environment(tmp_path: Path) -> None:
    store, result = asyncio.run(
        _run(
            tmp_path,
            _prepare("describe", "literal;not-a-shell", environment={"ITHRL_TEST_MARKER": "set"}),
        )
    )

    assert result.native_output is not None
    output = store.read(result.native_output)
    description = json.loads(output.stdout)
    assert result.returncode == 0
    assert result.report.process_outcome == "exited"
    assert result.report.cleanup_outcome == "verified"
    assert result.report.residual_disposable_state_bytes == 0
    assert not (tmp_path / "state" / ATTEMPT_ID).exists()
    assert description == {
        "args": ["literal;not-a-shell"],
        "cwd": "work",
        "marker": "set",
    }
    assert output.stderr == b""
    assert output.complete


def test_nonzero_exit_is_complete_process_output(tmp_path: Path) -> None:
    store, result = asyncio.run(_run(tmp_path, _prepare("exit", "7")))

    assert result.returncode == 7
    assert result.native_output is not None
    assert store.read(result.native_output).complete


def test_output_close_failure_marks_the_artifact_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_output = runtime_process._close_output
    close_calls = 0

    def fail_first_close(output: BinaryIO) -> bool:
        nonlocal close_calls
        close_calls += 1
        closed = close_output(output)
        return closed and close_calls != 1

    monkeypatch.setattr(runtime_process, "_close_output", fail_first_close)

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, _prepare("exit", "0")))

    assert raised.value.code is RunnerErrorCode.INTERNAL
    assert raised.value.native_output is not None
    assert not raised.value.native_output.complete
    assert raised.value.report.process_outcome == "exited"
    assert raised.value.report.cleanup_outcome == "verified"


def test_startup_failure_is_safe_and_leaves_no_attempt_state(tmp_path: Path) -> None:
    def prepare(state: OwnedAttemptState) -> ProcessSpec:
        return ProcessSpec(
            argv=(str(state.member("missing-executable")),),
            cwd=state.root,
            environment={},
        )

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, prepare))

    assert raised.value.code is RunnerErrorCode.STARTUP
    assert raised.value.report.process_outcome == "not-started"
    assert raised.value.report.cleanup_outcome == "verified"
    assert raised.value.native_output is None
    assert str(tmp_path) not in str(raised.value)


def test_unsafe_cleanup_primitive_refuses_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsafeRmtree:
        avoids_symlink_attacks = False

        def __call__(self, path: Path) -> None:
            raise AssertionError(f"cleanup must not run for {path}")

    monkeypatch.setattr(runtime_state.shutil, "rmtree", UnsafeRmtree())

    with pytest.raises(AttemptStateError, match="unavailable"):
        asyncio.run(_run(tmp_path, _prepare("exit", "0")))

    assert not (tmp_path / "state").exists()


def test_slow_preparation_cannot_start_after_the_attempt_deadline(tmp_path: Path) -> None:
    process_started = False

    def prepare(state: OwnedAttemptState) -> ProcessSpec:
        nonlocal process_started
        time.sleep(1.05)
        process_started = True
        return ProcessSpec(
            argv=(sys.executable, str(HELPER), "exit", "0"),
            cwd=state.root,
            environment={},
        )

    limits = ResourceLimits(
        max_wall_time_seconds=1,
        max_native_output_bytes=4096,
        max_disposable_state_bytes=4096,
    )

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, prepare, limits=limits))

    assert process_started
    assert raised.value.code is RunnerErrorCode.TIMEOUT
    assert raised.value.report.process_outcome == "not-started"
    assert raised.value.report.cleanup_outcome == "verified"


def test_combined_output_limit_stops_the_process_and_preserves_a_partial_artifact(
    tmp_path: Path,
) -> None:
    limits = ResourceLimits(
        max_wall_time_seconds=5,
        max_native_output_bytes=128,
        max_disposable_state_bytes=4096,
    )
    source_paths, workspace_paths = _protected_paths(tmp_path)
    store = ProtectedOutputStore.open(tmp_path / "outputs")

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(
            run_isolated_process(
                attempt_id=ATTEMPT_ID,
                state_parent=tmp_path / "state",
                output_store=store,
                prepare=_prepare("output", "4096", "4096"),
                resource_limits=limits,
                source_paths=source_paths,
                workspace_paths=workspace_paths,
                max_protected_entries=16,
                max_protected_bytes=4096,
            )
        )

    assert raised.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert raised.value.native_output is not None
    assert raised.value.native_output.size_bytes == limits.max_native_output_bytes
    assert not raised.value.native_output.complete
    output = store.read(raised.value.native_output)
    assert len(output.stdout) + len(output.stderr) == store.payload_capacity(
        limits.max_native_output_bytes
    )
    assert raised.value.report.cleanup_outcome == "verified"


def test_output_reader_failure_cancels_the_other_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_calls = 0
    sibling_cancelled = False

    async def fail_one_capture(*args: Any, **kwargs: Any) -> None:
        nonlocal capture_calls, sibling_cancelled
        del args, kwargs
        capture_calls += 1
        if capture_calls == 1:
            raise OSError("synthetic output failure")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled = True
            raise

    monkeypatch.setattr(runtime_process, "_capture", fail_one_capture)

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, _prepare("sleep", "60")))

    assert raised.value.code is RunnerErrorCode.INTERNAL
    assert raised.value.report.process_outcome == "terminated"
    assert sibling_cancelled


def test_timeout_forces_a_term_ignoring_process_to_stop(tmp_path: Path) -> None:
    limits = ResourceLimits(
        max_wall_time_seconds=1,
        max_native_output_bytes=4096,
        max_disposable_state_bytes=4096,
    )

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, _prepare("ignore-term", "60"), limits=limits))

    assert raised.value.code is RunnerErrorCode.TIMEOUT
    assert raised.value.report.process_outcome == "terminated"
    assert raised.value.report.forced_termination
    assert raised.value.report.cleanup_outcome == "verified"


def test_cancellation_waits_for_termination_and_cleanup(tmp_path: Path) -> None:
    async def cancel() -> ProcessRuntimeCancelled:
        task = asyncio.create_task(_run(tmp_path, _prepare("sleep", "60")))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(ProcessRuntimeCancelled) as raised:
            await task
        return raised.value

    cancelled = asyncio.run(cancel())

    assert cancelled.report.process_outcome == "terminated"
    assert cancelled.report.cleanup_outcome == "verified"
    assert cancelled.report.residual_disposable_state_bytes == 0
    assert not (tmp_path / "state" / ATTEMPT_ID).exists()


def test_repeated_cancellation_cannot_interrupt_forced_termination(tmp_path: Path) -> None:
    ready = tmp_path / "state" / ATTEMPT_ID / "ready"

    async def cancel_repeatedly() -> ProcessRuntimeCancelled:
        task = asyncio.create_task(_run(tmp_path, _prepare("ignore-term", "60", str(ready))))
        for _ in range(200):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("helper process did not become ready")
        for _ in range(5):
            task.cancel()
            await asyncio.sleep(0.1)
        with pytest.raises(ProcessRuntimeCancelled) as raised:
            await task
        return raised.value

    cancelled = asyncio.run(cancel_repeatedly())

    assert cancelled.report.process_outcome == "terminated"
    assert cancelled.report.forced_termination
    assert cancelled.report.cleanup_outcome == "verified"
    assert cancelled.report.residual_disposable_state_bytes == 0
    assert not (tmp_path / "state" / ATTEMPT_ID).exists()


def test_startup_cancellation_retains_process_ownership_after_delayed_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen = runtime_process.subprocess.Popen
    started_process: subprocess.Popen[bytes] | None = None

    def delayed_popen(
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.Popen[bytes]:
        nonlocal started_process
        started_process = popen(*args, **kwargs)
        time.sleep(1.2)
        return started_process

    monkeypatch.setattr(
        runtime_process.subprocess,
        "Popen",
        delayed_popen,
    )

    async def cancel_during_startup() -> ProcessRuntimeCancelled:
        task = asyncio.create_task(_run(tmp_path, _prepare("ignore-term", "60")))
        asyncio.get_running_loop().call_later(0.1, task.cancel)
        with pytest.raises(ProcessRuntimeCancelled) as raised:
            await task
        return raised.value

    cancelled = asyncio.run(cancel_during_startup())

    assert started_process is not None
    assert cancelled.report.process_outcome == "terminated"
    assert cancelled.report.forced_termination
    with pytest.raises(ProcessLookupError):
        os.kill(started_process.pid, 0)


def test_timeout_terminates_a_descendant_in_the_owned_process_group(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    limits = ResourceLimits(
        max_wall_time_seconds=1,
        max_native_output_bytes=4096,
        max_disposable_state_bytes=4096,
    )

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(
            _run(
                tmp_path,
                _prepare("spawn-child", str(child_pid_file)),
                limits=limits,
            )
        )

    assert raised.value.code is RunnerErrorCode.TIMEOUT
    assert raised.value.report.process_outcome == "terminated"
    assert raised.value.report.forced_termination
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_preparation_state_limit_fails_before_process_start(tmp_path: Path) -> None:
    def prepare(state: OwnedAttemptState) -> ProcessSpec:
        state.member("large.bin").write_bytes(b"x" * 64)
        return ProcessSpec(
            argv=(sys.executable, str(HELPER), "exit", "0"),
            cwd=state.root,
            environment={},
        )

    limits = ResourceLimits(
        max_wall_time_seconds=5,
        max_native_output_bytes=4096,
        max_disposable_state_bytes=32,
    )
    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, prepare, limits=limits))

    assert raised.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert raised.value.report.process_outcome == "not-started"
    assert raised.value.report.cleanup_outcome == "verified"


def test_preparation_state_entry_limit_fails_before_process_start(tmp_path: Path) -> None:
    def prepare(state: OwnedAttemptState) -> ProcessSpec:
        for index in range(3):
            state.member(f"empty-{index}").touch()
        return ProcessSpec(
            argv=(sys.executable, str(HELPER), "exit", "0"),
            cwd=state.root,
            environment={},
        )

    limits = ResourceLimits(
        max_wall_time_seconds=5,
        max_native_output_bytes=4096,
        max_disposable_state_bytes=4096,
        max_disposable_state_entries=2,
    )
    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, prepare, limits=limits))

    assert raised.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert raised.value.report.process_outcome == "not-started"
    assert raised.value.report.disposable_state_entries == 3
    assert raised.value.report.cleanup_outcome == "verified"


def test_runtime_state_limit_stops_a_running_process(tmp_path: Path) -> None:
    state_file = tmp_path / "state" / ATTEMPT_ID / "large.bin"
    limits = ResourceLimits(
        max_wall_time_seconds=5,
        max_native_output_bytes=4096,
        max_disposable_state_bytes=128,
    )

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(
            _run(
                tmp_path,
                _prepare("write-and-sleep", str(state_file), "1024", "60"),
                limits=limits,
            )
        )

    assert raised.value.code is RunnerErrorCode.OUTPUT_LIMIT
    assert raised.value.report.process_outcome == "terminated"
    assert raised.value.report.disposable_state_bytes >= 1024
    assert raised.value.report.cleanup_outcome == "verified"


def test_state_monitor_finishes_inspection_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    measure = OwnedAttemptState.measure
    cleanup = OwnedAttemptState.cleanup
    measurement_started = threading.Event()
    measurement_finished = threading.Event()
    measurement_release = threading.Event()
    measure_calls = 0
    termination_started_before_measurement_finished = False

    def slow_monitor_measure(
        state: OwnedAttemptState,
        *,
        max_entries: int,
        max_bytes: int,
    ) -> StateMeasurement:
        nonlocal measure_calls
        measure_calls += 1
        if measure_calls == 2:
            measurement_started.set()
            measurement_release.wait(timeout=2)
            measurement_finished.set()
        return measure(state, max_entries=max_entries, max_bytes=max_bytes)

    def guarded_cleanup(
        state: OwnedAttemptState,
        measurement: StateMeasurement | None = None,
    ) -> CleanupResult:
        assert measurement_finished.is_set()
        return cleanup(state, measurement)

    monkeypatch.setattr(OwnedAttemptState, "measure", slow_monitor_measure)
    monkeypatch.setattr(OwnedAttemptState, "cleanup", guarded_cleanup)
    terminate_group = runtime_process._terminate_group

    async def release_measurement_for_termination(
        process: subprocess.Popen[bytes],
    ) -> tuple[ProcessOutcome, bool]:
        nonlocal termination_started_before_measurement_finished
        termination_started_before_measurement_finished = not measurement_finished.is_set()
        measurement_release.set()
        return await terminate_group(process)

    monkeypatch.setattr(runtime_process, "_terminate_group", release_measurement_for_termination)

    async def cancel_during_measurement() -> ProcessRuntimeCancelled:
        task = asyncio.create_task(_run(tmp_path, _prepare("sleep", "60")))
        for _ in range(200):
            if measurement_started.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("state monitor did not start")
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
        with pytest.raises(ProcessRuntimeCancelled) as raised:
            await task
        return raised.value

    cancelled = asyncio.run(cancel_during_measurement())

    assert termination_started_before_measurement_finished
    assert cancelled.report.cleanup_outcome == "verified"


def test_unmeasurable_final_state_is_a_safety_failure(tmp_path: Path) -> None:
    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(_run(tmp_path, _prepare("deep-tree", "70")))

    assert raised.value.code is RunnerErrorCode.SAFETY
    assert raised.value.report.cleanup_outcome == "verified"
    assert not (tmp_path / "state" / ATTEMPT_ID).exists()


def test_protected_workspace_mutation_is_a_safety_failure(tmp_path: Path) -> None:
    source_paths, workspace_paths = _protected_paths(tmp_path)
    workspace_file = tmp_path / "workspace" / "tracked.txt"
    store = ProtectedOutputStore.open(tmp_path / "outputs")

    with pytest.raises(ProcessRuntimeError) as raised:
        asyncio.run(
            run_isolated_process(
                attempt_id=ATTEMPT_ID,
                state_parent=tmp_path / "state",
                output_store=store,
                prepare=_prepare("write", str(workspace_file), "changed"),
                resource_limits=ResourceLimits(
                    max_wall_time_seconds=5,
                    max_native_output_bytes=4096,
                    max_disposable_state_bytes=4096,
                ),
                source_paths=source_paths,
                workspace_paths=workspace_paths,
                max_protected_entries=16,
                max_protected_bytes=4096,
            )
        )

    assert raised.value.code is RunnerErrorCode.SAFETY
    assert raised.value.report.original_workspace_effect == "may-change"
    assert raised.value.report.cleanup_outcome == "verified"


@pytest.mark.skipif(os.name == "posix", reason="POSIX has process-group containment")
def test_unsupported_host_refuses_before_creating_state(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(_run(tmp_path, _prepare("exit", "0")))

    assert not (tmp_path / "state").exists()
