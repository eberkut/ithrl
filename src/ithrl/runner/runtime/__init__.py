"""Shared isolated-process runtime for harness runners."""

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
    ProtectedOutput,
    ProtectedOutputStore,
)
from ithrl.runner.runtime.process import PROCESS_CANCELLATION_GRACE_SECONDS, run_isolated_process
from ithrl.runner.runtime.protection import (
    ProtectedPath,
    ProtectedPathError,
    ProtectedPathSnapshot,
    ProtectionErrorCode,
)
from ithrl.runner.runtime.state import (
    AttemptStateError,
    CleanupResult,
    OwnedAttemptState,
)

__all__ = [
    "PROCESS_CANCELLATION_GRACE_SECONDS",
    "AttemptStateError",
    "CleanupResult",
    "NativeOutputLimitError",
    "NativeOutputStoreError",
    "OwnedAttemptState",
    "ProcessExecution",
    "ProcessRuntimeCancelled",
    "ProcessRuntimeError",
    "ProcessSpec",
    "ProtectedOutput",
    "ProtectedOutputStore",
    "ProtectedPath",
    "ProtectedPathError",
    "ProtectedPathSnapshot",
    "ProtectionErrorCode",
    "RuntimeReport",
    "require_process_group_support",
    "run_isolated_process",
]
