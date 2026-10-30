"""CLI flow for bounded native full-context calibration."""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from contextlib import nullcontext, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Never, cast

import typer
from platformdirs import user_data_path

from ithrl.calibration import (
    CalibrationWallTimeBudgetError,
    SampledCalibrationResult,
    run_sampled_calibration,
    save_sampled_calibration_result,
)
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.case.filesystem import reject_symlink_components
from ithrl.cli.output import (
    ExitCode,
    OutputFormat,
    SampledOutput,
    fail_sampled,
    last_runner_error,
    render_sampled,
    shell_command,
)
from ithrl.cli.progress import calibration_status, progress_display
from ithrl.execution import calibration_budget_bounds
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.experiment import SampledExperimentDefinition
from ithrl.observations import Observation, ObservationArm, ObservationRole, ObservationStore
from ithrl.results import CalibrationSnapshot
from ithrl.runner import prepare_candidate
from ithrl.workflow import PreparedExperiment

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9._+-]{1,80}\Z")
_SAFE_ENDPOINT = re.compile(
    r"(?:https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?"
    r"(?:/[A-Za-z0-9._~/-]{0,160})?|copilot://managed)\Z"
)


def handle_sampled_shrink(
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    *,
    dry_run: bool,
    calibrate_only: bool,
    acknowledge_model_execution: bool,
    original_workspace: Path | None,
    observation_store: Path | None,
    output_format: OutputFormat,
    harness_started: bool = True,
    max_wall_time_seconds: int | None = None,
    inter_attempt_delay_seconds: int = 0,
    experiment_path: Path | None = None,
) -> None:
    """Present a sampled preflight, then run only an acknowledged calibration."""

    started = time.monotonic()
    if not isinstance(prepared.experiment, SampledExperimentDefinition):
        fail_sampled(
            error_code="sampled-experiment-required",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )
    if not dry_run and not calibrate_only:
        fail_sampled(
            error_code="sampled-search-unavailable",
            code=ExitCode.UNSUPPORTED,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )
    if dry_run and calibrate_only:
        fail_sampled(
            error_code="invalid-options",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )
    if original_workspace is None:
        fail_sampled(
            error_code="original-workspace-unavailable",
            code=ExitCode.UNSUPPORTED,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )

    try:
        if not original_workspace.is_dir():
            raise ValueError("original workspace is unavailable")
        store_path = _observation_store_path(workspace, original_workspace, observation_store)
        preflight = _preflight_report(
            workspace=workspace,
            prepared=prepared,
            original_workspace=original_workspace,
            observation_store=store_path,
            harness_started=harness_started,
            max_wall_time_seconds=max_wall_time_seconds,
            inter_attempt_delay_seconds=inter_attempt_delay_seconds,
            experiment_path=experiment_path,
        )
    except KeyboardInterrupt:
        _interrupt_before_execution(output_format, harness_started)
    except (OSError, TypeError, ValueError):
        fail_sampled(
            error_code="sampled-preflight-invalid",
            code=ExitCode.UNSUPPORTED,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )
    try:
        if dry_run:
            render_sampled(preflight, output_format)
            return
        # Keep stdout available for the one final JSON document.
        with redirect_stdout(sys.stderr) if output_format is OutputFormat.JSON else nullcontext():
            render_sampled(preflight, output_format)
    except KeyboardInterrupt:
        _interrupt_before_execution(output_format, harness_started)
    except Exception:
        fail_sampled(
            error_code="sampled-preflight-output-unavailable",
            code=ExitCode.INTERNAL,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )
    if not acknowledge_model_execution:
        _require_model_acknowledgement(output_format, harness_started)

    try:
        # Recheck after acknowledgement before any SQLite or lock-file writes.
        store_path = _observation_store_path(workspace, original_workspace, store_path)
        store = ObservationStore(store_path)
    except KeyboardInterrupt:
        _interrupt_before_execution(output_format, harness_started)
    except (OSError, ValueError):
        fail_sampled(
            error_code="observation-store-unavailable",
            code=ExitCode.INTERNAL,
            output_format=output_format,
            status="refused",
            harness_started=harness_started,
            provider_call_accounting="not-started",
        )

    def show_calibration(observations: tuple[Observation, ...]) -> None:
        valid = sum(observation.valid for observation in observations)
        required = cast(SampledExperimentDefinition, prepared.experiment).calibration.required_valid
        progress_update(calibration_status(valid, required, len(observations)))

    def admit() -> None:
        candidate = prepare_candidate(evidence, prepared.execution_plan)
        observations = store.list_compatible(
            prepared.identity,
            candidate_digest=candidate.digest,
            role=ObservationRole.CALIBRATION,
            arm=ObservationArm.BASELINE,
        )
        show_calibration(observations)
        remaining = preflight.max_attempts - len(observations)
        delay = 0.0
        if observations:
            elapsed = (datetime.now(UTC) - observations[-1].recorded_at).total_seconds()
            if elapsed < 0:
                raise ValueError("calibration clock precedes recorded progress")
            delay = max(0.0, preflight.inter_attempt_delay_seconds - elapsed)
        cost = sampling_attempt_cost(prepared.execution_plan.capability, prepared.resource_limits)
        future = (
            remaining * cost.runner_seconds
            + max(0, remaining - 1) * preflight.inter_attempt_delay_seconds
        )
        limit = preflight.max_wall_time_seconds
        if limit is not None and time.monotonic() - started + delay + future > limit:
            raise CalibrationWallTimeBudgetError()
        if delay:
            time.sleep(delay)
        if limit is not None and time.monotonic() - started + future > limit:
            raise CalibrationWallTimeBudgetError()

    try:
        with progress_display() as progress_update:
            result = run_sampled_calibration(
                evidence=evidence, prepared=prepared, store=store, before_attempt=admit
            )
            show_calibration(result.observations)
        snapshot = save_sampled_calibration_result(
            workspace=workspace,
            evidence=evidence,
            prepared=prepared,
            store=store,
            result=result,
        )
    except KeyboardInterrupt:
        fail_sampled(
            error_code="calibration-interrupted",
            code=ExitCode.CANCELLED,
            output_format=output_format,
            status="interrupted",
            harness_started=harness_started,
            provider_call_accounting="started-unknown",
        )
    except Exception:
        # The runner may have dispatched before it failed, and there may be an
        # unresolved in-flight marker. Never claim zero calls in this branch.
        fail_sampled(
            error_code="calibration-incomplete",
            code=ExitCode.PROVIDER,
            output_format=output_format,
            status="incomplete",
            harness_started=harness_started,
            provider_call_accounting="started-unknown",
        )

    try:
        summary = snapshot.summary
        report = _calibration_report(preflight, result, snapshot)
        report = report.model_copy(update={"wall_time_seconds": time.monotonic() - started})
        render_sampled(report, output_format)
    except KeyboardInterrupt:
        fail_sampled(
            error_code="calibration-interrupted",
            code=ExitCode.CANCELLED,
            output_format=output_format,
            status="interrupted",
            harness_started=harness_started,
            provider_call_accounting="started-unknown",
        )
    except Exception:
        fail_sampled(
            error_code="calibration-accounting-invalid",
            code=ExitCode.INTERNAL,
            output_format=output_format,
            status="incomplete",
            harness_started=harness_started,
            provider_call_accounting="started-unknown",
        )
    if report.runner_error_category == "provider-call-limit":
        raise typer.Exit(ExitCode.BUDGET)
    if report.status == "interrupted":
        raise typer.Exit(ExitCode.CANCELLED)
    if summary.outcome in {"inconclusive", "not-measurable"}:
        raise typer.Exit(ExitCode.INCONCLUSIVE)
    if summary.outcome == "refused":
        raise typer.Exit(ExitCode.UNSUPPORTED)
    if summary.outcome == "incomplete":
        raise typer.Exit(
            ExitCode.BUDGET
            if summary.stop_reason in {"attempt-limit", "wall-time-budget"}
            else ExitCode.PROVIDER
        )


def _require_model_acknowledgement(
    output_format: OutputFormat,
    harness_started: bool,
    *,
    prompt: str = "Send this case to the configured provider for bounded calibration?",
) -> None:
    """Require interactive consent without dispatching or writing observation state."""

    try:
        if not sys.stdin.isatty():
            _stop_before_execution(output_format, "acknowledgement-required", harness_started)
        with redirect_stdout(sys.stderr) if output_format is OutputFormat.JSON else nullcontext():
            acknowledged = typer.confirm(
                prompt,
                default=False,
            )
    except (KeyboardInterrupt, typer.Abort):
        _interrupt_before_execution(output_format, harness_started)
    except EOFError:
        acknowledged = False
    if not acknowledged:
        _stop_before_execution(output_format, "acknowledgement-declined", harness_started)


def _calibration_report(
    preflight: SampledOutput,
    result: SampledCalibrationResult,
    snapshot: CalibrationSnapshot,
) -> SampledOutput:
    """Derive bounded accounting and safe continuation from saved calibration evidence."""

    summary = snapshot.summary
    runner_error_category, runner_error_reason_code = last_runner_error(result.observations)
    accounting: Literal["accounted", "started-unknown"] = (
        "accounted"
        if summary.provider_calls_started is not None
        and summary.provider_calls_possible == 0
        and summary.provider_calls_may_have_started == summary.provider_calls_started
        and summary.provider_calls_started <= preflight.max_provider_calls
        else "started-unknown"
    )
    status = "interrupted" if summary.stop_reason == "user-interrupt" else summary.outcome
    continuation_safe = (
        status in {"incomplete", "interrupted"}
        and summary.observations_recorded < preflight.max_attempts
        and all(
            item.allows_fresh_attempt(preflight.eligibility_policy) for item in result.observations
        )
    )
    report_data = preflight.model_dump(mode="python")
    report_data.update(
        {
            "status": status,
            "stop_reason": summary.stop_reason,
            "matches": summary.matches,
            "no_matches": summary.no_matches,
            "invalid_observations": summary.invalid_observations,
            "runner_errors": summary.runner_errors,
            "runner_error_category": runner_error_category,
            "runner_error_reason_code": runner_error_reason_code,
            "actions_executed": summary.actions_executed,
            "provider_call_accounting": accounting,
            "provider_calls": summary.provider_calls_started if accounting == "accounted" else None,
            "attempts_used": summary.observations_recorded,
            "runner_seconds": (
                summary.elapsed_milliseconds / 1000
                if summary.elapsed_milliseconds is not None
                else None
            ),
            "result_path": _safe_result_path(snapshot.root),
            "next_action": (
                "Continue the compatible calibration using the saved observations and remaining attempt budget."
                if continuation_safe
                else _outcome_next_action(status)
            ),
            "next_command": preflight.next_command if continuation_safe else None,
            "next_command_shell": preflight.next_command_shell if continuation_safe else None,
        }
    )
    if runner_error_category == "provider-call-limit":
        report_data.update(
            next_action="The per-attempt provider call budget was exhausted. Inspect the saved evidence; a different limit requires a new run.",
            next_command=None,
            next_command_shell=None,
        )
    return SampledOutput.model_validate(report_data)


def _preflight_report(
    *,
    workspace: CaseWorkspace,
    prepared: PreparedExperiment,
    original_workspace: Path,
    observation_store: Path | None,
    harness_started: bool = True,
    max_wall_time_seconds: int | None = None,
    inter_attempt_delay_seconds: int = 0,
    experiment_path: Path | None = None,
) -> SampledOutput:
    experiment = prepared.experiment
    if not isinstance(experiment, SampledExperimentDefinition):
        raise ValueError("sampled experiment required")
    plan = prepared.execution_plan
    capability = plan.capability
    bounds = calibration_budget_bounds(experiment.calibration, capability, prepared.resource_limits)
    harness = capability.harness
    version = capability.runtime_harness_version
    if harness is None or _SAFE_NAME.fullmatch(harness) is None:
        raise ValueError("unsafe or missing harness")
    if version is None or _SAFE_VERSION.fullmatch(version) is None:
        raise ValueError("unsafe or missing harness version")
    provider = capability.provider
    if provider is None or _SAFE_NAME.fullmatch(provider) is None:
        raise ValueError("provider cannot be safely displayed")
    endpoint = capability.endpoint
    if endpoint is None or _SAFE_ENDPOINT.fullmatch(endpoint) is None:
        raise ValueError("provider endpoint cannot be safely displayed")
    arguments = [
        "ithrl",
        "shrink",
        str(workspace.root.absolute()),
        "--original-workspace",
        str(original_workspace.absolute()),
        "--calibrate-only",
        "--acknowledge-model-execution",
        "--required-valid",
        str(experiment.calibration.required_valid),
        "--minimum-matches",
        str(experiment.calibration.minimum_matches),
        "--max-attempts",
        str(experiment.calibration.max_attempts),
        "--attempt-timeout",
        str(prepared.resource_limits.max_wall_time_seconds),
        "--max-provider-calls",
        str(bounds.provider_calls),
    ]
    if prepared.resource_limits.max_input_tokens is not None:
        arguments.extend(("--max-input-tokens", str(prepared.resource_limits.max_input_tokens)))
    if max_wall_time_seconds is not None:
        arguments.extend(("--max-wall-time", str(max_wall_time_seconds)))
    arguments.extend(("--inter-attempt-delay", str(inter_attempt_delay_seconds)))
    if experiment_path is not None:
        arguments.extend(("--experiment", str(experiment_path.absolute())))
    if observation_store is None:
        from ithrl.cli.paths import private_data_root

        observation_store = private_data_root() / "observations.sqlite"
    arguments.extend(("--observation-store", str(observation_store.absolute())))
    next_command, next_shell = shell_command(tuple(arguments))
    if len(next_command) > 2048 or not next_command.isprintable():
        next_command = None
        next_shell = None
    return SampledOutput(
        status="ready",
        eligibility_policy=(
            experiment.reduction.eligibility_policy
            if experiment.reduction is not None
            else "independent-safe-preaccept-only"
        ),
        harness_started=harness_started,
        plan_digest=plan.digest,
        capability_digest=capability.digest,
        harness=harness,
        harness_version=version,
        target=plan.target_type,
        controlled_surface=capability.reconstruction_method,
        decision_digest=plan.decision_digest,
        requested_model=plan.model_requested,
        supported_context_operations=capability.supported_context_operations,
        protected_state_enforcement=capability.protected_state_enforcement,
        disposable_state_limit_enforcement=capability.disposable_state_limit_enforcement,
        process_containment=capability.process_containment,
        input_token_limit_enforcement=capability.input_token_limit_enforcement,
        action_visibility=capability.action_visibility,
        action_denial=capability.action_denial,
        deviations=capability.deviations,
        safety_policy_digest=prepared.safety_policy.digest,
        safety_effects=(
            "tool-network-forbidden",
            "actions-denied",
            "source-store-unchanged",
            "original-workspace-unchanged",
        ),
        provider=provider,
        endpoint=endpoint,
        max_attempts=bounds.attempts,
        required_valid=experiment.calibration.required_valid,
        minimum_matches=experiment.calibration.minimum_matches,
        max_provider_calls=bounds.provider_calls,
        max_provider_calls_per_attempt=capability.provider_calls_per_attempt,
        max_runner_seconds=bounds.runner_seconds,
        max_wall_time_seconds=max_wall_time_seconds,
        inter_attempt_delay_seconds=inter_attempt_delay_seconds,
        max_tokens=bounds.input_tokens,
        attempt_timeout_seconds=prepared.resource_limits.max_wall_time_seconds,
        max_native_output_bytes=prepared.resource_limits.max_native_output_bytes,
        max_disposable_state_bytes=prepared.resource_limits.max_disposable_state_bytes,
        max_disposable_state_entries=prepared.resource_limits.max_disposable_state_entries,
        provider_call_accounting="not-started",
        provider_calls=0,
        attempts_used=0,
        actions_executed=0,
        next_action="Run only the bounded full-context calibration after acknowledgement.",
        next_command=next_command,
        next_command_shell=next_shell,
    )


def _stop_before_execution(
    output_format: OutputFormat, reason: str, harness_started: bool = True
) -> Never:
    fail_sampled(
        error_code=reason,
        code=ExitCode.USAGE,
        output_format=output_format,
        status="refused",
        harness_started=harness_started,
        provider_call_accounting="not-started",
    )


def _interrupt_before_execution(output_format: OutputFormat, harness_started: bool) -> Never:
    fail_sampled(
        error_code="calibration-interrupted",
        code=ExitCode.CANCELLED,
        output_format=output_format,
        status="interrupted",
        harness_started=harness_started,
        provider_call_accounting="not-started",
    )


def _observation_store_path(
    workspace: CaseWorkspace, original_workspace: Path, requested: Path | None
) -> Path:
    """Keep SQLite and its sibling lifecycle files outside protected native state."""

    data_root = user_data_path("ithrl", ensure_exists=False).absolute()
    path = requested if requested is not None else data_root / "observations.sqlite"
    if ".." in path.parts:
        raise ValueError("unsafe observation path")
    reject_symlink_components(path)
    resolved = path.resolve(strict=False)
    protected = (
        workspace.root.resolve(strict=True),
        original_workspace.resolve(strict=True),
        (data_root / "native-attempts").resolve(strict=False),
        (data_root / "native-output").resolve(strict=False),
    )
    lifecycle_paths = (
        resolved,
        resolved.with_name(f"{resolved.name}.calibration.lock"),
        resolved.with_name(f"{resolved.name}.calibration.inflight"),
        resolved.with_name(f"{resolved.name}.sampling.lock"),
        resolved.with_name(f"{resolved.name}-journal"),
        resolved.with_name(f"{resolved.name}-wal"),
        resolved.with_name(f"{resolved.name}-shm"),
    )
    for candidate in lifecycle_paths:
        if any(
            candidate == root or root in candidate.parents or candidate in root.parents
            for root in protected
        ):
            raise ValueError("observation storage overlaps protected state")
        if os.path.lexists(candidate):
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("unsafe observation storage entry")
    return resolved


def _safe_result_path(path: Path) -> str | None:
    display = str(path.absolute())
    return display if len(display) <= 240 and display.isprintable() else None


def _outcome_next_action(outcome: str) -> str:
    if outcome == "measurable":
        return "Baseline measurable; sampled search is not yet available."
    if outcome == "not-measurable":
        return "The selected behavior was not observed in the valid baseline samples."
    if outcome == "inconclusive":
        return "Baseline samples did not reach a stable behavior classification."
    if outcome == "refused":
        return "Inspect the saved safety refusal before another attempt."
    return "Inspect the saved partial calibration before another attempt."
