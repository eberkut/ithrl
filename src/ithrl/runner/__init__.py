"""Runner contracts, built-in implementations, and composition helpers."""

from ithrl.runner.contracts import (
    Candidate,
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    Runner,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    TokenUsage,
    ToolCall,
    prepare_candidate,
)
from ithrl.runner.evidence import AttemptEvidence
from ithrl.runner.fake import FakeRunner
from ithrl.runner.registry import RunnerRegistry, RunnerResolution, built_in_runner_registry
from ithrl.runner.validation import validate_run_result, validate_runner_error

__all__ = [
    "AttemptEvidence",
    "Candidate",
    "FakeRunner",
    "NativeOutputReference",
    "PreparedAttempt",
    "ProviderAcceptanceState",
    "RunAccounting",
    "RunResult",
    "Runner",
    "RunnerError",
    "RunnerErrorCode",
    "RunnerRegistry",
    "RunnerResolution",
    "TokenUsage",
    "ToolCall",
    "built_in_runner_registry",
    "prepare_candidate",
    "validate_run_result",
    "validate_runner_error",
]
