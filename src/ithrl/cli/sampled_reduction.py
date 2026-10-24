"""Compose the strict sampled workflow for acknowledged shrink commands."""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import typer

from ithrl.calibration import SampledCalibrationResult, _inflight_path, run_sampled_calibration
from ithrl.case import CaseEvidence, CaseWorkspace
from ithrl.cli.native import NativeSelection
from ithrl.cli.output import (
    ExitCode,
    OutputFormat,
    error_console,
    fail_sampled,
    print_plain,
    shell_command,
)
from ithrl.cli.sampled import (
    _observation_store_path,
    _preflight_report,
    _require_model_acknowledgement,
    _safe_result_path,
)
from ithrl.cli.sampled_output import (
    SampledReductionOutput,
    render_sampled_reduction,
    sampled_reduction_exit_code,
)
from ithrl.cli.sampled_session import (
    SampledCliSession,
    SampledCliSessionError,
    load_cli_run_state,
    load_cli_session,
    save_cli_result,
    save_cli_session,
)
from ithrl.execution.budget import sampling_attempt_cost
from ithrl.experiment import SampledExperimentDefinition
from ithrl.extraction import load_extracted_case
from ithrl.observations import (
    ObservationArm,
    ObservationOutcome,
    ObservationRole,
    ObservationStore,
    SamplingBlockEvidence,
    SamplingBudgetAllowance,
)
from ithrl.reduce import SampledConfirmation
from ithrl.results import SampledReductionEvidence, validate_sampled_result
from ithrl.run_state import (
    SampledRunState,
    SampledRunStateFile,
    SampledRunUsage,
    SamplingBudgetApproval,
)
from ithrl.runner import prepare_candidate
from ithrl.sampling import sampling_pacing_delay
from ithrl.statistics import (
    BinomialSample,
    FixedSampleOutcome,
    classify_fixed_sample,
    optimistic_attainability,
)
from ithrl.workflow import PreparedExperiment, prepare_experiment, run_sampled_phases


@dataclass(frozen=True, slots=True)
class SampledCommandLimits:
    """Administrative command limits, separate from the frozen statistical plan."""

    provider_calls: int | None
    wall_seconds: int | None
    candidates: int | None
    inter_attempt_delay_seconds: int | None = None


class _CalibrationBudgetStop(RuntimeError):
    """Stop before a reservation when complete remaining work no longer fits."""

    def __init__(self, reason: Literal["provider-call-budget", "wall-time-budget"]) -> None:
        self.reason = reason
        super().__init__(reason)


def resume_sampled_shrink(
    source: Path,
    *,
    select: Callable[[CaseWorkspace, CaseEvidence, SampledCliSession], NativeSelection],
    acknowledge_model_execution: bool,
    limits: SampledCommandLimits,
    original_workspace: Path | None,
    observation_store: Path | None,
    output_format: OutputFormat,
    verbose: bool,
) -> None:
    """Reconstruct the saved inputs; live authority is checked before any continuation."""
    try:
        session = load_cli_session(source)
        if (
            original_workspace is not None
            and original_workspace.absolute() != session.original_workspace
        ):
            raise ValueError("workspace changed")
        if (
            observation_store is not None
            and observation_store.absolute() != session.observation_store
        ):
            raise ValueError("observation store changed")
        workspace = CaseWorkspace.open(session.source_case)
        evidence = load_extracted_case(workspace)
        selection = select(workspace, evidence, session)
        if selection.resource_limits != session.resource_limits:
            raise ValueError("attempt limits changed")
        prepared = prepare_experiment(
            evidence,
            registry=selection.registry,
            safety_policy=selection.safety_policy,
            resource_limits=selection.resource_limits,
            experiment=selection.experiment,
        )
        if prepared.identity != session.original_identity:
            raise ValueError("execution identity changed")
    except Exception:
        fail_sampled(
            error_code="sampled-resume-mismatch",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            provider_call_accounting="not-started",
        )
    handle_sampled_reduction(
        workspace,
        evidence,
        prepared,
        dry_run=False,
        acknowledge_model_execution=acknowledge_model_execution,
        original_workspace=session.original_workspace,
        observation_store=session.observation_store,
        limits=limits,
        experiment_path=None,
        output_format=output_format,
        verbose=verbose,
        session=session,
    )


def handle_sampled_reduction(
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    *,
    dry_run: bool,
    acknowledge_model_execution: bool,
    original_workspace: Path | None,
    observation_store: Path | None,
    limits: SampledCommandLimits,
    experiment_path: Path | None,
    output_format: OutputFormat,
    verbose: bool = False,
    session: SampledCliSession | None = None,
) -> None:
    """Preflight, acknowledge, durably calibrate, search and independently confirm."""
    try:
        if original_workspace is None or not original_workspace.is_dir():
            raise ValueError("original workspace missing")
        store_path = _observation_store_path(workspace, original_workspace, observation_store)
        state_file = None if session is None else SampledRunStateFile.open(session.run_directory)
        # Reading saved metadata does not create a SQLite database or acquire a lock.
        if session is not None:
            saved = load_cli_run_state(session.run_directory)
            current = saved.allowance
            if (
                limits.inter_attempt_delay_seconds is not None
                and limits.inter_attempt_delay_seconds != current.inter_attempt_delay_seconds
            ):
                raise ValueError("saved inter-attempt delay cannot change")
            limits = SampledCommandLimits(
                current.max_provider_calls
                if limits.provider_calls is None
                else limits.provider_calls,
                current.max_wall_time_seconds
                if limits.wall_seconds is None
                else limits.wall_seconds,
                current.max_candidate_comparisons
                if limits.candidates is None
                else limits.candidates,
                current.inter_attempt_delay_seconds,
            )
            if (
                limits.provider_calls is None
                or limits.wall_seconds is None
                or limits.candidates is None
                or limits.provider_calls < current.max_provider_calls
                or limits.wall_seconds < current.max_wall_time_seconds
                or limits.candidates < current.max_candidate_comparisons
            ):
                raise ValueError("budgets cannot decrease")
        elif limits.candidates is None:
            limits = SampledCommandLimits(
                limits.provider_calls, limits.wall_seconds, 100, limits.inter_attempt_delay_seconds
            )
        preflight = _reduction_preflight(
            workspace,
            evidence,
            prepared,
            original_workspace,
            store_path,
            limits,
            experiment_path,
            session,
            output_format,
        )
    except typer.Exit:
        raise
    except Exception:
        fail_sampled(
            error_code="sampled-preflight-invalid",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            provider_call_accounting="not-started",
        )
    if dry_run:
        render_sampled_reduction(preflight, output_format, verbose=verbose)
        return
    if limits.provider_calls is None or limits.wall_seconds is None:
        fail_sampled(
            error_code="sampling-budget-required",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            provider_call_accounting="not-started",
            minimum_provider_calls=preflight.minimum_provider_calls,
            minimum_wall_time_seconds=int(preflight.minimum_wall_time_seconds),
        )
    # Preflight belongs on stderr so stdout contains exactly one final JSON document.
    with redirect_stdout(sys.stderr):
        render_sampled_reduction(preflight, output_format, verbose=verbose)
    if not acknowledge_model_execution:
        _require_model_acknowledgement(
            output_format,
            True,
            prompt="Send this case to the configured provider for bounded calibration, search, and fresh confirmation?",
        )
    store: ObservationStore | None = None
    try:
        store_path = _observation_store_path(workspace, original_workspace, store_path)
        store = ObservationStore(store_path)
        store.initialize()
        if session is None:
            now = datetime.now(UTC)
            run_id = f"sampled-{uuid4().hex}"
            allowance = _allowance(preflight, run_id)
            original = prepare_candidate(evidence, prepared.execution_plan)
            state_file = SampledRunStateFile.create(
                case_root=workspace.root,
                run_id=run_id,
                store=store,
                started_at=now,
                original_identity=prepared.identity,
                original_candidate_digest=original.digest,
                provider_calls_per_attempt=prepared.execution_plan.capability.provider_calls_per_attempt,
                approval=SamplingBudgetApproval(
                    approval_id=f"approval-{uuid4().hex}", approved_at=now, allowance=allowance
                ),
            )
            session = SampledCliSession(
                run_id=run_id,
                run_directory=state_file.path.parent,
                source_case=workspace.root.absolute(),
                original_workspace=original_workspace.absolute(),
                observation_store=store.path.absolute(),
                experiment=prepared.experiment,
                resource_limits=prepared.resource_limits,
                original_identity=prepared.identity,
                started_at=now,
            )
            save_cli_session(state_file.path.parent, session)
        if state_file is None:
            raise ValueError("run state unavailable")
        with state_file.acquire_lock(store):
            state = state_file.load(store)
            if state.original_identity != prepared.identity:
                raise ValueError("saved execution identity changed")
            allowance = _allowance(preflight, state.run_id)
            if allowance != state.allowance:
                state = state_file.extend_budget(
                    store,
                    SamplingBudgetApproval(
                        approval_id=f"approval-{uuid4().hex}",
                        approved_at=datetime.now(UTC),
                        allowance=allowance,
                    ),
                )
            refusal = _continuation_refusal(
                state, state_file.evidence(store), datetime.now(UTC), store
            )
            if refusal is not None:
                report = _run_report(
                    preflight,
                    state,
                    state_file,
                    store,
                    None,
                    refusal,
                    calibration="incomplete" if state.checkpoint is None else "measurable",
                )
                render_sampled_reduction(report, output_format, verbose=verbose)
                raise typer.Exit(sampled_reduction_exit_code(report))
            if state.checkpoint is None:
                calibration = _calibrate_for_run(evidence, prepared, store, state_file, preflight)
                state = state_file.load(store)
                if isinstance(calibration, str):
                    report = _run_report(
                        preflight,
                        state,
                        state_file,
                        store,
                        None,
                        calibration,
                        calibration="incomplete",
                    )
                    render_sampled_reduction(report, output_format, verbose=verbose)
                    raise typer.Exit(sampled_reduction_exit_code(report))
                if (
                    calibration.outcome != "measurable"
                    or calibration.stop_reason == "user-interrupt"
                ):
                    report = _run_report(
                        preflight,
                        state,
                        state_file,
                        store,
                        None,
                        calibration.stop_reason,
                        calibration=calibration.outcome,
                    )
                    render_sampled_reduction(report, output_format, verbose=verbose)
                    raise typer.Exit(sampled_reduction_exit_code(report))
        outcome = asyncio.run(
            run_sampled_phases(
                evidence=evidence,
                prepared=prepared,
                store=store,
                state_file=state_file,
                progress=lambda message: print_plain(error_console, message),
            )
        )
        asyncio.run(
            validate_sampled_result(
                result=outcome.result,
                evidence=evidence,
                prepared=prepared,
                store=store,
                state_file=state_file,
            )
        )
        with state_file.acquire_lock(store):
            state = state_file.load(store)
            report = _run_report(
                preflight, state, state_file, store, outcome.result, outcome.stop_reason
            )
            destination = save_cli_result(
                workspace,
                evidence,
                state,
                store,
                outcome.result,
                session,
                report.model_dump(mode="json"),
            )
        report = report.model_copy(update={"result_path": _safe_result_path(destination)})
        render_sampled_reduction(report, output_format, verbose=verbose)
        raise typer.Exit(sampled_reduction_exit_code(report))
    except typer.Exit:
        raise
    except (KeyboardInterrupt, asyncio.CancelledError):
        _recover_output(preflight, state_file, store, output_format, verbose, "user-interrupt")
    except Exception:
        _recover_output(preflight, state_file, store, output_format, verbose, "execution-refused")


def _reduction_preflight(
    workspace: CaseWorkspace,
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    original_workspace: Path,
    store_path: Path,
    limits: SampledCommandLimits,
    experiment_path: Path | None,
    session: SampledCliSession | None,
    output_format: OutputFormat,
) -> SampledReductionOutput:
    experiment = prepared.experiment
    if not isinstance(experiment, SampledExperimentDefinition) or experiment.reduction is None:
        raise ValueError("complete sampled reduction required")
    reduction = experiment.reduction
    cost = sampling_attempt_cost(prepared.execution_plan.capability, prepared.resource_limits)
    delay = limits.inter_attempt_delay_seconds or 0
    if type(delay) is not int or not 0 <= delay <= 86_400:
        raise ValueError("inter-attempt delay must be bounded nonnegative seconds")
    # Conservatively admit a full pause for every future attempt; the first may need none.
    wall_seconds_per_attempt = cost.runner_seconds + delay
    calibration = experiment.calibration.max_attempts
    search = reduction.search.original.max_attempts + reduction.search.candidate.max_attempts
    confirmation = (
        reduction.confirmation.original.max_attempts + reduction.confirmation.candidate.max_attempts
    )
    minimum_attempts = calibration + search + confirmation
    if limits.candidates is None or limits.candidates < 1:
        raise ValueError("candidate budget unavailable")
    if session is not None:
        saved = load_cli_run_state(session.run_directory)
        if saved.selection is not None:
            block = next((b for b in saved.blocks if b.plan.role == "confirmation"), None)
            minimum_attempts = confirmation - (0 if block is None else len(block.observations))
        elif saved.checkpoint is not None:
            minimum_attempts = confirmation
            if not saved.checkpoint.final_sweep_complete and len(saved.blocks) < limits.candidates:
                minimum_attempts += search
        else:
            minimum_attempts -= len(saved.calibration_observation_ids)
    minimum_calls = minimum_attempts * cost.provider_calls
    minimum_seconds = minimum_attempts * wall_seconds_per_attempt
    if session is None and (
        (limits.provider_calls is not None and limits.provider_calls < minimum_calls)
        or (limits.wall_seconds is not None and limits.wall_seconds < minimum_seconds)
    ):
        fail_sampled(
            error_code="sampling-budget-too-small",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            provider_call_accounting="not-started",
            minimum_provider_calls=minimum_calls,
            minimum_wall_time_seconds=minimum_seconds,
        )
    if max(search, confirmation) * wall_seconds_per_attempt >= reduction.freshness_seconds:
        fail_sampled(
            error_code="sampling-freshness-too-small",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            provider_call_accounting="not-started",
        )
    attainability = optimistic_attainability(
        required_candidate=reduction.confirmation.candidate.required_valid,
        required_original=reduction.confirmation.original.required_valid,
        confidence=reduction.confirmation.one_sided_confidence,
        margin=reduction.margin,
    )
    if not attainability.preservation_possible:
        fail_sampled(
            error_code="confirmation-unattainable",
            code=ExitCode.USAGE,
            output_format=output_format,
            status="refused",
            provider_call_accounting="not-started",
        )
    attempts = calibration + limits.candidates * search + confirmation
    calls = (
        limits.provider_calls
        if limits.provider_calls is not None
        else attempts * cost.provider_calls
    )
    seconds = (
        limits.wall_seconds
        if limits.wall_seconds is not None
        else attempts * wall_seconds_per_attempt
    )
    capacity = max(
        0,
        min(
            limits.candidates,
            (calls - (calibration + confirmation) * cost.provider_calls)
            // (search * cost.provider_calls),
            (seconds - (calibration + confirmation) * wall_seconds_per_attempt)
            // (search * wall_seconds_per_attempt),
        ),
    )
    base = _preflight_report(
        workspace=workspace,
        prepared=prepared,
        original_workspace=original_workspace,
        observation_store=store_path,
    )
    values = {
        k: v
        for k, v in base.model_dump(mode="python").items()
        if k in SampledReductionOutput.model_fields and v is not None
    }
    arguments = [
        "ithrl",
        "shrink",
        str(workspace.root.absolute()),
        "--original-workspace",
        str(original_workspace.absolute()),
        "--observation-store",
        str(store_path),
        "--max-provider-calls",
        str(calls),
        "--max-wall-time",
        str(seconds),
        "--max-candidates",
        str(limits.candidates),
        "--acknowledge-model-execution",
    ]
    if session is not None:
        arguments = [
            "ithrl",
            "shrink",
            str(session.run_directory),
            "--resume",
            "--acknowledge-model-execution",
            "--max-provider-calls",
            str(calls),
            "--max-wall-time",
            str(seconds),
            "--max-candidates",
            str(limits.candidates),
        ]
    elif experiment_path is not None:
        arguments.extend(("--experiment", str(experiment_path.absolute())))
    if session is None:
        arguments.extend(
            (
                "--required-valid",
                str(experiment.calibration.required_valid),
                "--minimum-matches",
                str(experiment.calibration.minimum_matches),
                "--max-attempts",
                str(experiment.calibration.max_attempts),
                "--attempt-timeout",
                str(prepared.resource_limits.max_wall_time_seconds),
            )
        )
        if prepared.resource_limits.max_input_tokens is not None:
            arguments.extend(("--max-input-tokens", str(prepared.resource_limits.max_input_tokens)))
    if delay:
        arguments.extend(("--inter-attempt-delay", str(delay)))
    command, shell = shell_command(tuple(arguments))
    if len(command) > 2048 or not command.isprintable():
        command = shell = None
    values.update(
        status="ready",
        phase="calibration",
        max_attempts=attempts,
        max_provider_calls=calls,
        max_runner_seconds=attempts * cost.runner_seconds,
        max_wall_time_seconds=seconds,
        inter_attempt_delay_seconds=delay,
        max_candidate_comparisons=limits.candidates,
        candidate_capacity=capacity,
        minimum_provider_calls=minimum_calls,
        minimum_wall_time_seconds=minimum_seconds,
        max_tokens=None,
        search_original_required_valid=reduction.search.original.required_valid,
        search_original_max_attempts=reduction.search.original.max_attempts,
        search_candidate_required_valid=reduction.search.candidate.required_valid,
        search_candidate_max_attempts=reduction.search.candidate.max_attempts,
        confirmation_original_required_valid=reduction.confirmation.original.required_valid,
        confirmation_original_max_attempts=reduction.confirmation.original.max_attempts,
        confirmation_candidate_required_valid=reduction.confirmation.candidate.required_valid,
        confirmation_candidate_max_attempts=reduction.confirmation.candidate.max_attempts,
        confirmation_provider_calls=confirmation * cost.provider_calls,
        confirmation_runner_seconds=confirmation * cost.runner_seconds,
        confirmation_capacity="protected",
        freshness_seconds=reduction.freshness_seconds,
        eligibility_policy=reduction.eligibility_policy,
        margin=reduction.margin,
        one_sided_confidence=reduction.confirmation.one_sided_confidence,
        original_context_count=len(evidence.context.nodes),
        retained_context_count=len(evidence.context.nodes),
        removed_context_count=0,
        attempts_used=0,
        provider_calls=0,
        next_action="Inspect the fixed plan and budget, then acknowledge model execution. Local overhead consumes the overall wall-time allowance.",
        next_command=command,
        next_command_shell=shell,
    )
    if reduction.eligibility_policy == "safe-provider-overload":
        values["assumptions"] = (
            *SampledReductionOutput.model_fields["assumptions"].default,
            "independent-stationary-attempts-with-prespecified-validity",
            "overload-replacement-estimates-behavior-conditional-on-validity",
        )
    return SampledReductionOutput.model_validate(values)


def _allowance(report: SampledReductionOutput, run_id: str) -> SamplingBudgetAllowance:
    return SamplingBudgetAllowance(
        run_id=run_id,
        max_provider_calls=report.max_provider_calls,
        max_wall_time_seconds=int(report.max_wall_time_seconds),
        max_candidate_comparisons=report.max_candidate_comparisons,
        confirmation_provider_calls=report.confirmation_provider_calls,
        confirmation_runner_seconds=int(report.confirmation_runner_seconds),
        inter_attempt_delay_seconds=report.inter_attempt_delay_seconds,
    )


def _calibrate_for_run(
    evidence: CaseEvidence,
    prepared: PreparedExperiment,
    store: ObservationStore,
    state_file: SampledRunStateFile,
    report: SampledReductionOutput,
) -> SampledCalibrationResult | Literal["provider-call-budget", "wall-time-budget"]:
    original = prepare_candidate(evidence, prepared.execution_plan)

    def refresh_calibration_prefix() -> SampledRunState:
        observed = store.list_compatible(
            prepared.identity,
            candidate_digest=original.digest,
            role=ObservationRole.CALIBRATION,
            arm=ObservationArm.BASELINE,
        )
        return state_file.record_calibration(store, tuple(o.observation_id for o in observed))

    refresh_calibration_prefix()
    cost = sampling_attempt_cost(prepared.execution_plan.capability, prepared.resource_limits)
    experiment = prepared.experiment
    if not isinstance(experiment, SampledExperimentDefinition) or experiment.reduction is None:
        raise ValueError("complete sampled reduction required")
    phase = experiment.calibration
    reduction = experiment.reduction
    search = reduction.search.original.max_attempts + reduction.search.candidate.max_attempts

    def admit_attempt() -> None:
        state = refresh_calibration_prefix()
        delay = sampling_pacing_delay(state, store, datetime.now(UTC))
        if delay:
            time.sleep(delay)
        usage = state_file.usage(store, now=datetime.now(UTC))
        remaining = max(0, phase.max_attempts - len(state.calibration_observation_ids))
        if (
            usage.charged_provider_calls
            + (remaining + search) * cost.provider_calls
            + report.confirmation_provider_calls
            > state.allowance.max_provider_calls
        ):
            raise _CalibrationBudgetStop("provider-call-budget")
        if (
            usage.wall_time_seconds
            + (remaining + search) * cost.runner_seconds
            + report.confirmation_runner_seconds
            + (
                remaining
                + search
                + reduction.confirmation.original.max_attempts
                + reduction.confirmation.candidate.max_attempts
            )
            * state.allowance.inter_attempt_delay_seconds
            > state.allowance.max_wall_time_seconds
        ):
            raise _CalibrationBudgetStop("wall-time-budget")

    print_plain(error_console, "Calibrating the immutable original context")
    try:
        result = run_sampled_calibration(
            evidence=evidence, prepared=prepared, store=store, before_attempt=admit_attempt
        )
    except _CalibrationBudgetStop as stop:
        return stop.reason
    finally:
        # Charge every durable observation even when collection raises before returning.
        refresh_calibration_prefix()
    return result


def _continuation_refusal(
    state: SampledRunState,
    blocks: tuple[SamplingBlockEvidence, ...],
    now: datetime,
    store: ObservationStore | None = None,
) -> str | None:
    """Use frozen evidence to refuse retries that cannot repair the same comparison."""
    if state.pending is not None:
        pending = state.pending
        plan = state.blocks[-1].plan
        identity = plan.original_identity if pending.arm == "baseline" else plan.candidate_identity
        digest = (
            plan.original_candidate_digest if pending.arm == "baseline" else plan.candidate_digest
        )
        if (
            store is None
            or store.get_attempt(
                identity,
                candidate_digest=digest,
                role=ObservationRole(plan.role),
                arm=ObservationArm(pending.arm),
                attempt=pending.attempt,
            )
            is None
        ):
            return "unresolved-dispatch"
    if any(not block.eligible for block in blocks):
        return "ineligible-prior-block"
    for block in blocks:
        # An archived partial search may be followed only by its selected holdout.
        if block.complete or (state.selection is not None and block.plan.role == "search"):
            continue
        if not block.plan.frozen_at <= now < block.plan.expires_at:
            return "freshness-expired"
        if block.plan.role == "confirmation" and block.next_schedule_position is None:
            return "attempt-limit"
    if state.selection is not None:
        confirmation = next((b for b in blocks if b.plan.role == "confirmation"), None)
        if (
            confirmation is None or not confirmation.complete
        ) and not state.selection.plan.frozen_at <= now < state.selection.plan.expires_at:
            return "freshness-expired"
    return None


def _run_report(
    preflight: SampledReductionOutput,
    state: SampledRunState,
    state_file: SampledRunStateFile,
    store: ObservationStore,
    result: SampledReductionEvidence | None,
    stop_reason: str | None,
    *,
    calibration: str = "measurable",
) -> SampledReductionOutput:
    snapshot = state_file.snapshot(store, now=datetime.now(UTC))
    if snapshot.state != state:
        raise ValueError("sampled report differs from current owned state")
    usage = snapshot.usage
    blocks = snapshot.blocks
    calibration_observations = snapshot.calibration
    observations = [
        *calibration_observations,
        *(a.observation for b in blocks for a in b.attempts),
    ]
    unknown = bool(
        usage.unknown_provider_attempts
        or usage.possible_provider_calls
        or usage.pending_provider_calls
    )
    removed = () if state.checkpoint is None else state.checkpoint.removed_context_ids
    holdout = next((b for b in blocks if b.plan.role == "confirmation"), None)
    if result is not None:
        confirmation = result.confirmation_result
    elif holdout is None:
        confirmation = "not-run"
    else:
        outcome = SampledConfirmation.from_evidence(holdout).outcome
        confirmation = {
            FixedSampleOutcome.PRESERVES: "confirmed-preserving",
            FixedSampleOutcome.LOSES: "loses",
            FixedSampleOutcome.INCONCLUSIVE: "inconclusive",
            FixedSampleOutcome.INCOMPLETE: "incomplete",
            None: "refused",
        }[outcome]
    eligible = (
        state.pending is None
        and all(b.eligible for b in blocks)
        and all(
            o.allows_fresh_attempt(preflight.eligibility_policy) for o in calibration_observations
        )
    )
    status: Literal[
        "confirmed", "confirmation-failed", "inconclusive", "incomplete", "refused", "interrupted"
    ] = "incomplete"
    if stop_reason == "user-interrupt":
        status = "interrupted"
    elif (
        not eligible
        or calibration == "refused"
        or confirmation == "refused"
        or stop_reason
        in {
            "execution-refused",
            "freshness-expired",
            "unresolved-dispatch",
            "ineligible-prior-block",
            "unsafe-retry",
        }
    ):
        status = "refused"
    elif confirmation == "confirmed-preserving":
        status = "confirmed"
    elif confirmation == "loses":
        status = "confirmation-failed"
    elif confirmation == "inconclusive" or calibration in {"not-measurable", "inconclusive"}:
        status = "inconclusive"
    safe_resume = (
        eligible
        and status in {"interrupted", "incomplete"}
        and _continuation_refusal(state, blocks, datetime.now(UTC)) is None
        and stop_reason not in {"attempt-limit", "unsafe-retry", "safety-refusal"}
    )
    next_action = (
        "Resume this exact run; administrative budgets may be increased."
        if safe_resume
        else "Inspect the private run evidence. Exhausted, stale, or unsafe observations cannot be repaired by retrying this run."
    )
    if confirmation in {"confirmed-preserving", "loses", "inconclusive"}:
        next_action = "Inspect the private result and its fresh confirmation evidence. This completed confirmation is never repeated."
    elif calibration in {"not-measurable", "inconclusive"}:
        next_action = "Inspect baseline validity and matches; a revised target or calibration plan requires a new run."
    elif calibration == "refused":
        next_action = (
            "Inspect the private safety evidence and resolve the refusal before starting a new run."
        )
    if safe_resume:
        try:
            load_cli_session(state_file.path.parent)
        except SampledCliSessionError:
            safe_resume = False
            next_action = (
                "Private resume metadata is unavailable. Start a new run with fresh acknowledgement; no attempt was dispatched."
                if not observations and state.pending is None
                else "Private resume metadata is unavailable. Inspect the retained private run evidence before starting another run."
            )
    args = [
        "ithrl",
        "shrink",
        str(state_file.path.parent),
        "--resume",
        "--acknowledge-model-execution",
    ]
    if safe_resume and stop_reason in {
        "provider-call-budget",
        "wall-time-budget",
        "wall-time-exhausted",
    }:
        calls, seconds = _continuation_budget(preflight, state, blocks, usage)
        args.extend(("--max-provider-calls", str(calls), "--max-wall-time", str(seconds)))
        next_action = "Resume with the displayed budget proposal; local overhead also consumes the overall wall-time allowance."
    command, shell = shell_command(tuple(args)) if safe_resume else (None, None)
    if command is not None and (len(command) > 2048 or not command.isprintable()):
        command = shell = None
    if result is not None:
        search_result = result.search_result
    elif state.checkpoint is None:
        search_result = "not-run"
    elif state.checkpoint.final_sweep_complete:
        search_result = "search-reduced"
    else:
        search_result = "search-incomplete"
    values = preflight.model_dump(mode="python")
    values.update(
        status=status,
        phase="finished",
        calibration_result=calibration,
        search_result=search_result,
        confirmation_result=confirmation,
        evidence_eligibility="eligible" if eligible else "refused",
        reliability_status="unknown"
        if unknown
        else (
            "complete"
            if status in {"confirmed", "confirmation-failed", "inconclusive"}
            and all(o.valid for o in observations)
            else "incomplete"
        ),
        retained_context_count=preflight.original_context_count - len(removed),
        removed_context_count=len(removed),
        candidate_comparisons_used=sum(b.plan.role == "search" for b in blocks),
        provider_call_accounting="started-unknown" if unknown else "accounted",
        provider_calls=None if unknown else usage.actual_provider_calls,
        actual_provider_calls=usage.actual_provider_calls,
        possible_provider_calls=usage.possible_provider_calls,
        pending_provider_calls=usage.pending_provider_calls,
        charged_provider_calls=usage.charged_provider_calls,
        unknown_provider_attempts=usage.unknown_provider_attempts,
        unknown_acceptance_attempts=sum(
            o.attempt_evidence.acceptance_unknown for o in observations
        ),
        attempts_used=len(observations),
        runner_seconds=None if usage.unknown_runner_attempts else usage.runner_milliseconds / 1000,
        unknown_runner_attempts=usage.unknown_runner_attempts,
        pending_runner_seconds=usage.pending_runner_seconds,
        wall_time_seconds=usage.wall_time_seconds,
        known_tokens=usage.known_tokens,
        unknown_token_attempts=usage.unknown_token_attempts,
        matches=sum(o.outcome is ObservationOutcome.MATCH for o in observations),
        no_matches=sum(o.outcome is ObservationOutcome.NO_MATCH for o in observations),
        invalid_observations=sum(
            o.outcome is ObservationOutcome.INVALID_OBSERVATION for o in observations
        ),
        runner_errors=sum(o.outcome is ObservationOutcome.RUNNER_ERROR for o in observations),
        stop_reason=stop_reason,
        run_state_path=_safe_result_path(state_file.path.parent),
        confirmation_capacity="consumed"
        if confirmation in {"confirmed-preserving", "loses", "inconclusive"}
        else ("in-use" if state.selection is not None else "protected"),
        next_action=next_action,
        next_command=command,
        next_command_shell=shell,
    )
    if holdout is not None:
        values.update(
            confirmation_original=holdout.original_counts,
            confirmation_candidate=holdout.candidate_counts,
            confirmation_difference=holdout.point_difference,
        )
        if holdout.complete and holdout.eligible:
            classification = classify_fixed_sample(
                BinomialSample(holdout.candidate_counts.matches, holdout.candidate_counts.valid),
                BinomialSample(holdout.original_counts.matches, holdout.original_counts.valid),
                required_candidate=holdout.plan.preservation.confirmation.candidate.required_valid,
                required_original=holdout.plan.preservation.confirmation.original.required_valid,
                confidence=holdout.plan.preservation.confirmation.one_sided_confidence,
                margin=holdout.plan.preservation.margin,
            )
            if classification.interval is None:
                raise ValueError("complete eligible confirmation lacks an interval")
            values.update(
                confirmation_lower_bound=classification.interval.lower,
                confirmation_upper_bound=classification.interval.upper,
            )
    return SampledReductionOutput.model_validate(values)


def _continuation_budget(
    preflight: SampledReductionOutput,
    state: SampledRunState,
    blocks: tuple[SamplingBlockEvidence, ...],
    usage: SampledRunUsage,
) -> tuple[int, int]:
    """Propose allowance for remaining frozen work and protected confirmation."""
    plan = state.selection.plan if state.selection is not None else None
    active = next(
        (
            b
            for b in reversed(blocks)
            if not b.complete and (state.selection is None or b.plan.role == "confirmation")
        ),
        None,
    )
    remaining = 0
    if active is not None:
        position = active.next_schedule_position
        unfinished = {
            arm
            for arm in ("baseline", "candidate")
            if active.counts_for(arm).valid < active.plan.targets_for(arm).required_valid
        }
        remaining = sum(arm in unfinished for arm in active.plan.schedule[position or 0 :])
    elif plan is not None:
        remaining = len(plan.schedule)
    else:
        search_attempts = (
            preflight.search_original_max_attempts + preflight.search_candidate_max_attempts
        )
        if state.checkpoint is None:
            calibration_attempts = (
                preflight.max_attempts
                - preflight.max_candidate_comparisons * search_attempts
                - preflight.confirmation_original_max_attempts
                - preflight.confirmation_candidate_max_attempts
            )
            remaining = (
                max(0, calibration_attempts - len(state.calibration_observation_ids))
                + search_attempts
            )
        elif (
            not state.checkpoint.final_sweep_complete
            and len(blocks) < state.allowance.max_candidate_comparisons
        ):
            remaining = search_attempts
    per_attempt_seconds = preflight.confirmation_runner_seconds / (
        preflight.confirmation_original_max_attempts + preflight.confirmation_candidate_max_attempts
    )
    future_calls = remaining * state.provider_calls_per_attempt
    future_seconds = remaining * (per_attempt_seconds + state.allowance.inter_attempt_delay_seconds)
    if state.selection is None:
        future_calls += preflight.confirmation_provider_calls
        future_seconds += preflight.confirmation_runner_seconds
        future_seconds += (
            preflight.confirmation_original_max_attempts
            + preflight.confirmation_candidate_max_attempts
        ) * state.allowance.inter_attempt_delay_seconds
    calls = max(state.allowance.max_provider_calls, usage.charged_provider_calls + future_calls)
    seconds = max(
        state.allowance.max_wall_time_seconds,
        math.ceil(usage.wall_time_seconds + future_seconds) + 1,
    )
    return calls, seconds


def _recover_output(
    preflight: SampledReductionOutput,
    state_file: SampledRunStateFile | None,
    store: ObservationStore | None,
    output_format: OutputFormat,
    verbose: bool,
    reason: str,
) -> None:
    try:
        if state_file is None or store is None:
            raise ValueError("run unavailable")
        if os.path.lexists(_inflight_path(store)):
            # The calibration service alone can reconcile its exact durable marker.
            # A missing observation may hide a provider call; never report accounted zero.
            raise ValueError("calibration dispatch remains unresolved")
        with state_file.acquire_lock(store):
            state = state_file.load(store)
            report = _run_report(
                preflight,
                state,
                state_file,
                store,
                None,
                reason,
                calibration="incomplete" if state.checkpoint is None else "measurable",
            )
        render_sampled_reduction(report, output_format, verbose=verbose)
    except Exception:
        fail_sampled(
            error_code="sampled-run-incomplete",
            code=ExitCode.CANCELLED if reason == "user-interrupt" else ExitCode.INTERNAL,
            output_format=output_format,
            status="interrupted" if reason == "user-interrupt" else "incomplete",
            provider_call_accounting="started-unknown",
            diagnostic_path=None
            if state_file is None
            else _safe_result_path(state_file.path.parent),
        )
    raise typer.Exit(ExitCode.CANCELLED if reason == "user-interrupt" else ExitCode.UNSUPPORTED)
