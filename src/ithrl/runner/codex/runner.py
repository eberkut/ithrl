"""Codex final-response runner composed with the shared isolated runtime."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import cast

from ithrl.behavior import MAX_BEHAVIOR_TEXT_LENGTH
from ithrl.context import JsonObject, JsonValue, content_digest
from ithrl.execution import CapabilityProfile
from ithrl.runner.codex.configuration import CodexProviderConfiguration
from ithrl.runner.codex.isolation import (
    CodexIsolationError,
    CodexRuntime,
    prepare_codex_process,
    program_digest,
)
from ithrl.runner.codex.materialize import (
    CodexMaterialization,
    CodexMaterializationError,
    CodexMaterializationErrorCode,
    materialize_codex_attempt,
)
from ithrl.runner.codex.policy import (
    CAPTURE_TYPE,
    CONTEXT_OPERATION,
    MATERIALIZATION,
    POLICY_VERSION,
    RECONSTRUCTION_METHOD,
    RUNNER_VERSION,
    SOURCE_SCHEMA,
    TARGET_TYPE,
    configuration_deviation,
    runtime_deviation,
)
from ithrl.runner.codex.protocol import CodexProtocolError, ProtocolOutcome
from ithrl.runner.codex.runtime_configuration import validate_runner_configuration
from ithrl.runner.codex.transcript import parse_transcript
from ithrl.runner.contracts import (
    MAX_MODEL_NAME_LENGTH,
    MAX_SERVICE_TIER_LENGTH,
    NativeOutputReference,
    PreparedAttempt,
    ProviderAcceptanceState,
    RunAccounting,
    RunnerCancelled,
    RunnerError,
    RunnerErrorCode,
    RunResult,
    TokenUsage,
)
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


class CodexRunner:
    """Execute one reconstructed Codex turn with every action surface disabled."""

    def __init__(
        self,
        *,
        capability_profile: CapabilityProfile,
        runtime: CodexRuntime,
        configuration: CodexProviderConfiguration,
        state_parent: Path,
        output_root: Path,
        source_paths: Iterable[ProtectedPath],
        workspace_paths: Iterable[ProtectedPath],
    ) -> None:
        _validate_capability(capability_profile)
        _validate_configuration(capability_profile, configuration, runtime)
        sources = tuple(source_paths)
        workspaces = tuple(workspace_paths)
        if not sources or not workspaces:
            raise ValueError("Codex runner requires protected source and workspace paths")
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
            raise ValueError("Codex runner storage overlaps protected state")
        self._capability = capability_profile
        self._runtime = runtime
        self._configuration = configuration
        self._worker_source = Path(__file__).with_name("worker.py")
        self._worker_digest = program_digest(self._worker_source)
        support_sources = tuple(
            Path(__file__).with_name(name) for name in ("protocol.py", "protocol_contracts.py")
        )
        self._support_programs = tuple(
            (source, program_digest(source)) for source in support_sources
        )
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
            "runner": "codex",
            "runner_version": RUNNER_VERSION,
            "capability": self._capability.digest,
            "materialization": self._capability.reconstruction_method,
            "policy": POLICY_VERSION,
            "provider_configuration": self._configuration.public_digest,
            "runtime": self._runtime.identity,
            "worker": self._worker_digest,
            "support_programs": {source.name: digest for source, digest in self._support_programs},
        }
        return f"codex-v{RUNNER_VERSION}:{content_digest(identity)}"

    async def run(self, attempt: PreparedAttempt) -> RunResult:
        """Execute one prepared source-backed final-response attempt."""

        self._validate_attempt(attempt)
        try:
            materialized = materialize_codex_attempt(attempt)
            _validate_materialized_identity(materialized, attempt)
        except CodexMaterializationError as error:
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                str(error),
                provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                model_requested=attempt.execution_plan.model_requested,
                provider=self._capability.provider,
                endpoint=self._capability.endpoint,
                effective_policy_digest=attempt.safety_policy.digest,
            ) from error

        worker_request = _worker_request(materialized, self._configuration.provider_id)
        try:
            execution = await run_isolated_process(
                attempt_id=attempt.attempt_id,
                state_parent=self._state_parent,
                output_store=self._output_store,
                prepare=lambda state: prepare_codex_process(
                    state,
                    runtime=self._runtime,
                    configuration=self._configuration,
                    request=worker_request,
                    worker_source=self._worker_source,
                    worker_digest=self._worker_digest,
                    support_programs=self._support_programs,
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
                "Codex attempt was cancelled",
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
        except CodexIsolationError as error:
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                str(error),
                provider_acceptance_state=ProviderAcceptanceState.NOT_STARTED,
                model_requested=attempt.execution_plan.model_requested,
                provider=self._capability.provider,
                endpoint=self._capability.endpoint,
                effective_policy_digest=attempt.safety_policy.digest,
            ) from error

        if execution.native_output is None:
            raise _runtime_error(
                attempt,
                RunnerErrorCode.CONTRACT,
                "Codex runner produced no protected native output",
                execution.report,
                None,
            )
        try:
            protected = self._output_store.read(execution.native_output)
            outcome = parse_transcript(protected.stdout)
        except (CodexProtocolError, NativeOutputStoreError) as error:
            raise _runtime_error(
                attempt,
                RunnerErrorCode.PROTOCOL,
                "Codex runner output could not be validated",
                execution.report,
                execution.native_output,
            ) from error
        _require_normalized_outcome(attempt, outcome, execution.report, execution.native_output)
        if execution.returncode != 0 or outcome.status != "completed":
            raise _outcome_error(attempt, outcome, execution.report, execution.native_output)

        result = _run_result(attempt, outcome, execution.report, execution.native_output)
        validate_run_result(attempt, result)
        return result

    def _validate_attempt(self, attempt: PreparedAttempt) -> None:
        try:
            self._runtime.assert_unchanged()
            programs_unchanged = program_digest(self._worker_source) == self._worker_digest and all(
                program_digest(source) == digest for source, digest in self._support_programs
            )
        except CodexIsolationError as error:
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                "Codex runner inputs changed after capability detection",
            ) from error
        if not programs_unchanged:
            raise RunnerError(
                RunnerErrorCode.PREPARATION,
                "Codex runner inputs changed after capability detection",
            )
        plan = attempt.execution_plan
        if plan.capability != self._capability or plan.runner_fingerprint != self.fingerprint:
            raise RunnerError(
                RunnerErrorCode.CONTRACT,
                "Codex runner does not match the resolved execution plan",
            )
        if attempt.safety_policy.provider_transport != "allowed":
            raise RunnerError(
                RunnerErrorCode.SAFETY,
                "Codex provider transport is not allowed by the resolved policy",
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _validate_capability(capability: CapabilityProfile) -> None:
    expected = {
        "runner_type": "codex",
        "runner_version": RUNNER_VERSION,
        "harness": "codex",
        "source_schema": SOURCE_SCHEMA,
        "deterministic": False,
        "supported_capture_types": (CAPTURE_TYPE,),
        "supported_materializations": (MATERIALIZATION,),
        "supported_context_operations": (CONTEXT_OPERATION,),
        "reconstruction_method": RECONSTRUCTION_METHOD,
        "supported_targets": (TARGET_TYPE,),
        "provider_transport": "required",
        "provider_calls_per_attempt": 1,
        "tool_network_access": "forbidden",
        "action_visibility": "not-applicable",
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
        "reports_usage": True,
        "input_token_limit_enforcement": "unavailable",
    }
    document = capability.model_dump(mode="python")
    if any(document[name] != value for name, value in expected.items()):
        raise ValueError("Codex capability does not satisfy the final-response runner contract")
    if capability.provider is None or capability.endpoint is None:
        raise ValueError("Codex capability requires a verified provider destination")


def _validate_configuration(
    capability: CapabilityProfile,
    configuration: CodexProviderConfiguration,
    runtime: CodexRuntime,
) -> None:
    validate_runner_configuration(
        configuration.rendered_config,
        endpoint=configuration.endpoint,
        environment_names=configuration.environment_names,
    )
    if (
        capability.provider != configuration.source_provider_id
        or capability.endpoint != configuration.endpoint
        or configuration_deviation(configuration.public_digest) not in capability.deviations
        or runtime_deviation(runtime.identity) not in capability.deviations
    ):
        raise ValueError("Codex provider configuration does not match its capability profile")


def _validate_materialized_identity(
    materialized: CodexMaterialization,
    attempt: PreparedAttempt,
) -> None:
    capability = attempt.execution_plan.capability
    if materialized.model != attempt.execution_plan.model_requested:
        raise CodexMaterializationError(
            code=CodexMaterializationErrorCode.INCOMPATIBLE_CASE,
            message="the selected Codex model differs from the execution plan",
        )
    if materialized.provider != capability.provider:
        raise CodexMaterializationError(
            code=CodexMaterializationErrorCode.INCOMPATIBLE_CASE,
            message="the selected Codex provider differs from the execution plan",
        )


def _worker_request(materialized: CodexMaterialization, runtime_provider_id: str) -> JsonObject:
    return {
        "schema_version": 1,
        "model": materialized.model,
        "provider": runtime_provider_id,
        "effort": materialized.effort,
        "personality": materialized.personality,
        "baseInstructions": materialized.base_instructions,
        "developerInstructions": materialized.developer_instructions,
        "history": cast(JsonValue, list(materialized.history)),
        "input": cast(JsonValue, list(materialized.turn_input.items)),
        "toolOutput": materialized.turn_input.tool_output,
    }


def _run_result(
    attempt: PreparedAttempt,
    outcome: ProtocolOutcome,
    report: RuntimeReport,
    native_output: NativeOutputReference,
) -> RunResult:
    if outcome.usage is None or outcome.final_text is None:
        raise RunnerError(RunnerErrorCode.CONTRACT, "Codex completed without required evidence")
    return RunResult(
        attempt_id=attempt.attempt_id,
        effective_policy_digest=attempt.safety_policy.digest,
        final_text=outcome.final_text,
        model_requested=attempt.execution_plan.model_requested,
        model_returned=outcome.model_returned,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        service_tier=outcome.service_tier,
        usage=_token_usage(outcome),
        accounting=_accounting(report, outcome),
        native_output=native_output,
    )


def _require_normalized_outcome(
    attempt: PreparedAttempt,
    outcome: ProtocolOutcome,
    report: RuntimeReport,
    native_output: NativeOutputReference,
) -> None:
    error: tuple[RunnerErrorCode, str] | None = None
    if outcome.final_text is not None and len(outcome.final_text) > MAX_BEHAVIOR_TEXT_LENGTH:
        error = (
            RunnerErrorCode.OUTPUT_LIMIT,
            "Codex final response exceeds the normalized text limit",
        )
    elif (
        outcome.model_returned is not None
        and not 0 < len(outcome.model_returned) <= MAX_MODEL_NAME_LENGTH
    ) or (
        outcome.service_tier is not None
        and not 0 < len(outcome.service_tier) <= MAX_SERVICE_TIER_LENGTH
    ):
        error = (
            RunnerErrorCode.PROTOCOL,
            "Codex returned invalid model identity metadata",
        )
    if error is None:
        return
    raise RunnerError(
        *error,
        provider_acceptance_state=ProviderAcceptanceState(outcome.provider_acceptance),
        model_requested=attempt.execution_plan.model_requested,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        usage=_token_usage(outcome),
        accounting=_accounting(report, outcome),
        native_output=native_output,
        effective_policy_digest=attempt.safety_policy.digest,
    )


def _token_usage(outcome: ProtocolOutcome) -> TokenUsage | None:
    if outcome.usage is None:
        return None
    return TokenUsage(
        input_tokens=outcome.usage.input_tokens,
        output_tokens=outcome.usage.output_tokens,
        cached_input_tokens=outcome.usage.cached_input_tokens,
    )


def _outcome_error(
    attempt: PreparedAttempt,
    outcome: ProtocolOutcome,
    report: RuntimeReport,
    native_output: NativeOutputReference,
) -> RunnerError:
    code = (
        RunnerErrorCode.SAFETY
        if outcome.error_code
        in {"action-requested", "unintercepted-action", "unsupported-server-request"}
        else RunnerErrorCode.PROTOCOL
    )
    acceptance = ProviderAcceptanceState(outcome.provider_acceptance)
    return RunnerError(
        code,
        "Codex final-response execution did not produce a valid completion",
        provider_acceptance_state=acceptance,
        model_requested=attempt.execution_plan.model_requested,
        model_returned=outcome.model_returned,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        service_tier=outcome.service_tier,
        usage=_token_usage(outcome),
        accounting=_accounting(report, outcome),
        native_output=native_output,
        effective_policy_digest=attempt.safety_policy.digest,
    )


def _runtime_error(
    attempt: PreparedAttempt,
    code: RunnerErrorCode,
    message: str,
    report: RuntimeReport,
    native_output: NativeOutputReference | None,
) -> RunnerError:
    acceptance = (
        ProviderAcceptanceState.NOT_STARTED
        if report.process_outcome == "not-started"
        else ProviderAcceptanceState.UNKNOWN
    )
    return RunnerError(
        code,
        message,
        provider_acceptance_state=acceptance,
        model_requested=attempt.execution_plan.model_requested,
        provider=attempt.execution_plan.capability.provider,
        endpoint=attempt.execution_plan.capability.endpoint,
        accounting=_accounting(report, provider_call_may_have_started=True),
        native_output=native_output,
        effective_policy_digest=attempt.safety_policy.digest,
    )


def _accounting(
    report: RuntimeReport,
    outcome: ProtocolOutcome | None = None,
    *,
    provider_call_may_have_started: bool = False,
) -> RunAccounting:
    provider_calls_started = (
        outcome.provider_calls_started
        if outcome is not None
        else int(provider_call_may_have_started and report.process_outcome != "not-started")
    )
    return RunAccounting(
        provider_calls_started=provider_calls_started,
        provider_calls_completed=outcome.provider_calls_completed if outcome is not None else 0,
        provider_connections_started=provider_calls_started,
        actions_requested=outcome.actions_requested if outcome is not None else 0,
        actions_denied=outcome.actions_denied if outcome is not None else 0,
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
