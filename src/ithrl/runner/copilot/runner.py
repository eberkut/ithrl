"""GitHub Copilot runner composed with the shared isolated runtime."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ithrl.context import content_digest
from ithrl.execution import CapabilityProfile
from ithrl.runner.contracts import (
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    RunResult,
)
from ithrl.runner.copilot.capability import DetectedCopilotCapability
from ithrl.runner.copilot.isolation import (
    CopilotIsolationError,
    prepare_copilot_process,
    program_digest,
)
from ithrl.runner.copilot.materialize import (
    CopilotMaterializationError,
    materialize_copilot_attempt,
)
from ithrl.runner.copilot.policy import (
    CAPTURE_TYPE,
    CONTEXT_OPERATION,
    ENDPOINT,
    MATERIALIZATION,
    POLICY_VERSION,
    PROTOCOL_VERSION,
    PROVIDER,
    RECONSTRUCTION_METHOD,
    RUNNER_VERSION,
    SOURCE_SCHEMA,
    CopilotTarget,
)
from ithrl.runner.copilot.protocol import CopilotProtocolError, CopilotProtocolOutcome, parse_output
from ithrl.runner.runtime import (
    PROCESS_CANCELLATION_GRACE_SECONDS,
    NativeOutputStoreError,
    ProcessRuntimeCancelled,
    ProcessRuntimeError,
    ProtectedOutputStore,
    ProtectedPath,
    RuntimeReport,
    run_isolated_process,
)
from ithrl.runner.validation import validate_run_result

_MAX_PROTECTED_ENTRIES = 250_000
_MAX_PROTECTED_BYTES = 256 * 1024 * 1024


class CopilotRunner:
    """Execute one reconstructed Copilot turn under a target-specific policy."""

    def __init__(
        self,
        *,
        detected_capability: DetectedCopilotCapability,
        state_parent: Path,
        output_root: Path,
        source_paths: Iterable[ProtectedPath],
        workspace_paths: Iterable[ProtectedPath],
    ) -> None:
        capability_profile = detected_capability.profile
        runtime = detected_capability.runtime
        configuration = detected_capability.configuration
        target = _validate_capability(capability_profile)
        sources = tuple(source_paths)
        workspaces = tuple(workspace_paths)
        if not sources or not workspaces:
            raise ValueError("Copilot runner requires protected source and workspace paths")
        state_root = state_parent.resolve(strict=False)
        absolute_output_root = output_root.absolute()
        resolved_output_root = absolute_output_root.resolve(strict=False)
        protected_roots = tuple(
            entry.path.resolve(strict=False) for entry in (*sources, *workspaces)
        )
        if _paths_overlap(state_root, resolved_output_root) or any(
            _paths_overlap(owned, protected)
            for owned in (state_root, resolved_output_root)
            for protected in protected_roots
        ):
            raise ValueError("Copilot runner storage overlaps protected state")
        self._capability = capability_profile
        self._runtime = runtime
        self._configuration = configuration
        self._target = target
        self._worker_source = Path(__file__).with_name("worker.py")
        self._worker_digest = program_digest(self._worker_source)
        self._state_parent = state_root
        self._output_store = ProtectedOutputStore.open(absolute_output_root)
        self._source_paths = sources
        self._workspace_paths = workspaces

    @property
    def capability_profile(self) -> CapabilityProfile:
        return self._capability

    @property
    def fingerprint(self) -> str:
        identity = {
            "runner": "copilot",
            "runner_version": RUNNER_VERSION,
            "capability": self._capability.digest,
            "configuration": self._configuration.public_digest,
            "materialization": self._capability.reconstruction_method,
            "policy": POLICY_VERSION,
            "runtime": self._runtime.identity,
            "target": self._target,
            "worker": self._worker_digest,
        }
        return f"copilot-v{RUNNER_VERSION}:{content_digest(identity)}"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        """Execute one prepared source-backed Copilot attempt."""

        self._validate_attempt(attempt)
        model = attempt.execution_plan.model_requested
        if model is None:
            raise RunnerError(
                RunnerErrorCode.CONTRACT,
                "Copilot execution requires a requested model",
            )
        try:
            materialized = materialize_copilot_attempt(attempt)
        except CopilotMaterializationError as error:
            raise _preparation_error(attempt, str(error), self._capability) from error

        try:
            execution = await run_isolated_process(
                attempt_id=attempt.attempt_id,
                state_parent=self._state_parent,
                output_store=self._output_store,
                prepare=lambda state: prepare_copilot_process(
                    state,
                    runtime=self._runtime,
                    configuration=self._configuration,
                    semantic_session=materialized.semantic_session,
                    trigger=materialized.trigger,
                    model=model,
                    target=self._target,
                    worker_source=self._worker_source,
                    worker_digest=self._worker_digest,
                ),
                resource_limits=attempt.resource_limits,
                source_paths=self._source_paths,
                workspace_paths=self._workspace_paths,
                max_protected_entries=_MAX_PROTECTED_ENTRIES,
                max_protected_bytes=_MAX_PROTECTED_BYTES,
            )
        except ProcessRuntimeCancelled as cancelled:
            error = _runtime_error(
                attempt,
                RunnerErrorCode.CANCELLED,
                "Copilot attempt was cancelled",
                cancelled.report,
                cancelled.native_output,
            )
            raise RunnerCancelled(error) from None
        except ProcessRuntimeError as error:
            raise _runtime_error(
                attempt,
                error.code,
                str(error),
                error.report,
                error.native_output,
            ) from error
        except CopilotIsolationError as error:
            raise _preparation_error(attempt, str(error), self._capability) from error

        if execution.native_output is None:
            raise _runtime_error(
                attempt,
                RunnerErrorCode.CONTRACT,
                "Copilot runner produced no protected native output",
                execution.report,
                None,
            )
        try:
            protected = self._output_store.read(execution.native_output)
            outcome = parse_output(protected.stdout, expected=self._target)
        except (CopilotProtocolError, NativeOutputStoreError) as error:
            raise _runtime_error(
                attempt,
                RunnerErrorCode.PROTOCOL,
                "Copilot runner output could not be validated",
                execution.report,
                execution.native_output,
            ) from error

        _validate_completion(attempt, self._target, execution.returncode, execution.report, outcome)
        result = _run_result(attempt, outcome, execution.report, execution.native_output)
        validate_run_result(attempt, result)
        return result

    def _validate_attempt(self, attempt: PreparedAttempt) -> None:
        try:
            self._runtime.assert_unchanged()
            worker_unchanged = program_digest(self._worker_source) == self._worker_digest
        except CopilotIsolationError as error:
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                "Copilot runner inputs changed after capability detection",
            ) from error
        if not worker_unchanged:
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                "Copilot runner inputs changed after capability detection",
            )
        plan = attempt.execution_plan
        if plan.capability != self._capability or plan.runner_fingerprint != self.fingerprint:
            raise RunnerError(
                RunnerErrorCode.CONTRACT,
                "Copilot runner does not match the resolved execution plan",
            )
        if attempt.target.type != self._target:
            raise RunnerError(
                RunnerErrorCode.CONTRACT,
                "Copilot runner target differs from the resolved execution plan",
            )
        if attempt.safety_policy.provider_transport != "allowed":
            raise RunnerError(
                RunnerErrorCode.SAFETY,
                "Copilot provider transport is not allowed by the resolved policy",
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _validate_capability(capability: CapabilityProfile) -> CopilotTarget:
    if len(capability.supported_targets) != 1 or capability.supported_targets[0] not in {
        "final-response",
        "shell-action",
    }:
        raise ValueError("Copilot capability must select one supported target")
    target = capability.supported_targets[0]
    calls = 2 if target == "shell-action" else 1
    visibility = "visible" if target == "shell-action" else "not-applicable"
    expected = {
        "runner_type": "copilot",
        "runner_version": RUNNER_VERSION,
        "harness": "copilot",
        "protocol_version": PROTOCOL_VERSION,
        "source_schema": SOURCE_SCHEMA,
        "deterministic": False,
        "supported_capture_types": (CAPTURE_TYPE,),
        "supported_materializations": (MATERIALIZATION,),
        "supported_context_operations": (CONTEXT_OPERATION,),
        "reconstruction_method": RECONSTRUCTION_METHOD,
        "provider_transport": "required",
        "provider_calls_per_attempt": calls,
        "tool_network_access": "forbidden",
        "action_visibility": visibility,
        "action_denial": "enforced",
        "action_execution": "denied",
        "source_store_effect": "unchanged",
        "original_workspace_effect": "unchanged",
        "protected_state_enforcement": "preventive",
        "disposable_state_effect": "writes",
        "disposable_state_limit_enforcement": "enforced",
        "process_containment": "process-group",
        "supports_timeout": True,
        "supports_cancellation": True,
        "cancellation_grace_seconds": PROCESS_CANCELLATION_GRACE_SECONDS,
        "reports_usage": False,
        "input_token_limit_enforcement": "unavailable",
        "provider": PROVIDER,
        "endpoint": ENDPOINT,
    }
    document = capability.model_dump(mode="python")
    if any(document[name] != value for name, value in expected.items()):
        raise ValueError("Copilot capability does not satisfy the runner contract")
    return target


def _validate_completion(
    attempt: PreparedAttempt,
    target: CopilotTarget,
    returncode: int,
    report: RuntimeReport,
    outcome: CopilotProtocolOutcome,
) -> None:
    if target == "final-response":
        valid = returncode == 0 and not report.stdout_stop_matched
    else:
        valid = report.stdout_stop_matched and report.process_outcome in {"exited", "terminated"}
    if valid:
        return
    raise RunnerError(
        RunnerErrorCode.PROTOCOL,
        "Copilot execution did not produce the expected controlled completion",
        provider_acceptance_state=ProviderAcceptanceState.ACCEPTED,
        model_requested=attempt.execution_plan.model_requested,
        model_returned=outcome.model_returned,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        usage=outcome.usage,
        accounting=_accounting(report, outcome),
        effective_policy_digest=attempt.safety_policy.digest,
    )


def _run_result(
    attempt: PreparedAttempt,
    outcome: CopilotProtocolOutcome,
    report: RuntimeReport,
    native_output: NativeOutputReference,
) -> RunResult:
    tool_calls = (outcome.tool_call,) if outcome.tool_call is not None else ()
    return RunResult(
        attempt_id=attempt.attempt_id,
        effective_policy_digest=attempt.safety_policy.digest,
        tool_calls=tool_calls,
        final_text=outcome.final_text,
        model_requested=attempt.execution_plan.model_requested,
        model_returned=outcome.model_returned,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        usage=outcome.usage,
        accounting=_accounting(report, outcome),
        native_output=native_output,
    )


def _preparation_error(
    attempt: PreparedAttempt,
    message: str,
    capability: CapabilityProfile,
) -> RunnerError:
    return RunnerError(
        RunnerErrorCode.PREPARATION,
        message,
        provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
        model_requested=attempt.execution_plan.model_requested,
        provider=capability.provider,
        endpoint=capability.endpoint,
        effective_policy_digest=attempt.safety_policy.digest,
    )


def _runtime_error(
    attempt: PreparedAttempt,
    code: RunnerErrorCode,
    message: str,
    report: RuntimeReport,
    native_output: NativeOutputReference | None,
) -> RunnerError:
    started = report.process_outcome != "not-started"
    return RunnerError(
        code,
        message,
        provider_acceptance_state=(
            ProviderAcceptanceState.UNKNOWN if started else ProviderAcceptanceState.NOT_STARTED
        ),
        model_requested=attempt.execution_plan.model_requested,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        accounting=_accounting(report, provider_call_may_have_started=started),
        native_output=native_output,
        effective_policy_digest=attempt.safety_policy.digest,
    )


def _accounting(
    report: RuntimeReport,
    outcome: CopilotProtocolOutcome | None = None,
    *,
    provider_call_may_have_started: bool = False,
) -> RunAccounting:
    calls_started = (
        outcome.provider_calls_started
        if outcome is not None
        else int(provider_call_may_have_started)
    )
    return RunAccounting(
        provider_calls_started=calls_started,
        provider_calls_completed=(outcome.provider_calls_completed if outcome is not None else 0),
        provider_connections_started=calls_started,
        actions_requested=outcome.actions_requested if outcome is not None else 0,
        actions_denied=outcome.actions_denied if outcome is not None else 0,
        actions_executed=outcome.actions_executed if outcome is not None else 0,
        elapsed_milliseconds=report.elapsed_milliseconds,
        native_output_bytes=report.native_output_bytes,
        disposable_state_bytes=report.disposable_state_bytes,
        residual_disposable_state_bytes=report.residual_disposable_state_bytes,
        disposable_state_entries=report.disposable_state_entries,
        residual_disposable_state_entries=report.residual_disposable_state_entries,
        process_outcome=report.process_outcome,
        forced_termination=report.forced_termination,
        cleanup_outcome=report.cleanup_outcome,
        source_store_effect=report.source_store_effect,
        original_workspace_effect=report.original_workspace_effect,
    )


__all__ = ["CopilotRunner"]
