"""Contracts for isolated local runner processes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ithrl.execution import ProtectedStateEffect
from ithrl.runner.contracts import (
    CleanupOutcome,
    NativeOutputReference,
    ProcessOutcome,
    RunnerErrorCode,
)


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """One direct executable invocation inside an owned attempt directory."""

    argv: tuple[str, ...] = field(repr=False)
    cwd: Path = field(repr=False)
    environment: Mapping[str, str] = field(repr=False, compare=False)
    inherited_fds: tuple[int, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_argv(self.argv)
        _validate_cwd(self.cwd)
        if (
            any(type(descriptor) is not int or descriptor < 3 for descriptor in self.inherited_fds)
            or tuple(sorted(set(self.inherited_fds))) != self.inherited_fds
        ):
            raise ValueError("inherited file descriptors must be sorted, unique, and at least 3")
        object.__setattr__(self, "environment", _validated_environment(self.environment))


@dataclass(frozen=True, slots=True)
class RuntimeReport:
    """Bounded lifecycle facts collected by the isolated process runtime."""

    elapsed_milliseconds: int
    native_output_bytes: int
    disposable_state_bytes: int
    residual_disposable_state_bytes: int
    disposable_state_entries: int
    residual_disposable_state_entries: int
    process_outcome: ProcessOutcome
    forced_termination: bool
    cleanup_outcome: CleanupOutcome
    source_store_effect: ProtectedStateEffect
    original_workspace_effect: ProtectedStateEffect

    def __post_init__(self) -> None:
        if (
            min(
                self.elapsed_milliseconds,
                self.native_output_bytes,
                self.disposable_state_bytes,
                self.residual_disposable_state_bytes,
                self.disposable_state_entries,
                self.residual_disposable_state_entries,
            )
            < 0
        ):
            raise ValueError("runtime measurements cannot be negative")
        if self.residual_disposable_state_bytes > self.disposable_state_bytes:
            raise ValueError("residual state cannot exceed the observed state peak")
        if self.residual_disposable_state_entries > self.disposable_state_entries:
            raise ValueError("residual entries cannot exceed the observed entry peak")


@dataclass(frozen=True, slots=True)
class ProcessExecution:
    """A completed child process and its protected native output."""

    returncode: int
    native_output: NativeOutputReference | None
    report: RuntimeReport


class ProcessRuntimeError(RuntimeError):
    """A safely classified isolated-runtime failure."""

    def __init__(
        self,
        code: RunnerErrorCode,
        message: str,
        *,
        report: RuntimeReport,
        native_output: NativeOutputReference | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.report = report
        self.native_output = native_output


class ProcessRuntimeCancelled(asyncio.CancelledError):
    """External cancellation after bounded termination and cleanup."""

    def __init__(
        self,
        *,
        report: RuntimeReport,
        native_output: NativeOutputReference | None,
    ) -> None:
        super().__init__("runner process was cancelled")
        self.report = report
        self.native_output = native_output


def require_process_group_support() -> None:
    """Refuse execution when the host cannot contain a descendant process group."""

    if os.name != "posix" or not hasattr(os, "killpg"):
        raise RuntimeError("isolated process groups are unavailable on this platform")


def _validate_argv(argv: tuple[str, ...]) -> None:
    if not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError("process arguments must be strings")
    if not Path(argv[0]).is_absolute():
        raise ValueError("process executable must be an absolute path")
    if any(not item or "\0" in item for item in argv):
        raise ValueError("process arguments must be non-empty and contain no null bytes")


def _validate_cwd(cwd: Path) -> None:
    if not isinstance(cwd, Path) or not cwd.is_absolute() or ".." in cwd.parts:
        raise ValueError("process working directory must be absolute without parent traversal")


def _validated_environment(environment: Mapping[str, str]) -> Mapping[str, str]:
    copied = dict(environment)
    if any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or not name
        or "=" in name
        or "\0" in name
        or "\0" in value
        for name, value in copied.items()
    ):
        raise ValueError("process environment contains an invalid name or value")
    return MappingProxyType(copied)
